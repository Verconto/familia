"""Voice transcription providers (Yandex SpeechKit, Groq, OpenAI Whisper).

All three providers cap the per-request payload size — Yandex's
short-audio endpoint is the most aggressive at 1 MB (~50-60 s of
Opus), OpenAI Whisper and Groq both cap at 25 MB (~25-30 minutes).
For clips over the cap we transparently split the file via ffmpeg
stream-copy (no re-encoding), transcribe each segment, and concatenate
the results. The split helper lives at module level so all three
providers share it; each provider declares its own
``MAX_PAYLOAD_BYTES`` and the segmenter sizes chunks accordingly.

Without chunking, long voice messages on Telegram / VK ended up
forwarded to the LLM as raw ``[voice: /path/...]`` strings (the
channel adapter's fallback when transcription returns ""), and the
model dutifully replied "I can't transcribe voice" while the audio
file sat in ``media/`` unread. The chunking path keeps the user
experience identical for clips of any length, paying for n round-trips
to the STT provider instead of one.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path

import httpx
from loguru import logger


# ---------------------------------------------------------------------------
# Shared chunking helper
# ---------------------------------------------------------------------------

async def split_audio_with_ffmpeg(
    path: Path,
    *,
    target_bytes: int,
    duration_s: int,
    log_prefix: str = "STT",
) -> list[Path] | None:
    """Split an audio file into ``duration_s``-second segments via ffmpeg.

    Returns the list of segment paths inside a fresh temp dir, or None
    on failure. The caller is responsible for cleaning the parent
    directory of any returned path (use ``segments[0].parent`` and
    ``shutil.rmtree``).

    The split uses ``-c copy`` so we don't re-encode — fast, lossless,
    preserves the input codec (Opus stays Opus, MP3 stays MP3, etc.).
    ``-reset_timestamps 1`` makes each segment start at granule 0 so
    a strict decoder doesn't choke on continuation pages.

    ``target_bytes`` is the per-chunk size budget. We don't truncate
    on size directly (ffmpeg-segment splits by time only), but we log
    a warning when a chunk exceeds the budget so the operator can
    drop ``duration_s`` if they're seeing oversize segments
    consistently. At Telegram's typical Opus bitrate ~17 KB/s, 30 s
    ≈ 510 KB which sits comfortably under the strictest provider
    (Yandex 1 MB).
    """
    if shutil.which("ffmpeg") is None:
        logger.warning(
            "{}: ffmpeg unavailable — cannot split audio. Install "
            "ffmpeg in the gateway image to enable long-clip "
            "transcription.",
            log_prefix,
        )
        return None

    segdir = Path(tempfile.mkdtemp(prefix="stt_chunks_"))
    ext = path.suffix or ".ogg"
    segment_pattern = str(segdir / f"seg_%05d{ext}")
    cmd = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "error",
        "-y",
        "-i", str(path),
        "-f", "segment",
        "-segment_time", str(duration_s),
        "-reset_timestamps", "1",
        "-c", "copy",
        segment_pattern,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
    except Exception as exc:  # noqa: BLE001 — best-effort, keep diagnostic
        logger.error("{}: ffmpeg launch failed: {}", log_prefix, exc)
        shutil.rmtree(segdir, ignore_errors=True)
        return None

    if proc.returncode != 0:
        logger.warning(
            "{}: ffmpeg split failed (rc={}): {}",
            log_prefix,
            proc.returncode,
            err.decode("utf-8", "replace")[:300],
        )
        shutil.rmtree(segdir, ignore_errors=True)
        return None

    chunks = sorted(segdir.glob(f"seg_*{ext}"))
    if not chunks:
        logger.warning(
            "{}: ffmpeg produced no segments for {}", log_prefix, path,
        )
        shutil.rmtree(segdir, ignore_errors=True)
        return None

    # Telemetry only — oversize chunks still go through (the provider
    # will reject them and we'll log the actual error there).
    oversize = [c for c in chunks if c.stat().st_size > target_bytes]
    if oversize:
        logger.warning(
            "{}: {}/{} segments exceed target {}B "
            "(consider lowering segment_time)",
            log_prefix, len(oversize), len(chunks), target_bytes,
        )
    logger.info(
        "{}: split {} into {} segments (target {}s, ~{}B each)",
        log_prefix, path.name, len(chunks),
        duration_s, target_bytes,
    )
    return chunks


def _cleanup_chunks(chunks: list[Path] | None) -> None:
    """Delete the temp dir holding ``chunks`` (no-op on None / empty)."""
    if not chunks:
        return
    parent = chunks[0].parent
    shutil.rmtree(parent, ignore_errors=True)


# ---------------------------------------------------------------------------
# OpenAI Whisper
# ---------------------------------------------------------------------------

class OpenAITranscriptionProvider:
    """Voice transcription via OpenAI's Whisper API.

    OpenAI caps the file upload at 25 MB. At Telegram's typical Opus
    bitrate that's ~25-30 minutes — most chats fit in one shot. For
    longer clips the transcribe() call splits via ffmpeg.
    """

    # Real cap is 25 MB; aim for 24 MB so we don't lose to a margin
    # rounding error at the boundary.
    MAX_PAYLOAD_BYTES = 24 * 1024 * 1024
    # OpenAI handles ~25 min in one call comfortably; 600 s segments
    # leave headroom and limit per-chunk re-encode if we ever need it.
    CHUNK_DURATION_S = 600

    def __init__(self, api_key: str | None = None, api_base: str | None = None):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self.api_url = (
            api_base
            or os.environ.get("OPENAI_TRANSCRIPTION_BASE_URL")
            or "https://api.openai.com/v1/audio/transcriptions"
        )

    async def transcribe(self, file_path: str | Path) -> str:
        if not self.api_key:
            logger.warning("OpenAI API key not configured for transcription")
            return ""
        path = Path(file_path)
        if not path.exists():
            logger.error("Audio file not found: {}", file_path)
            return ""
        try:
            size = path.stat().st_size
        except OSError as e:
            logger.error("OpenAI STT: stat failed: {}", e)
            return ""

        if size <= self.MAX_PAYLOAD_BYTES:
            return await self._transcribe_one(path)

        chunks = await split_audio_with_ffmpeg(
            path,
            target_bytes=self.MAX_PAYLOAD_BYTES,
            duration_s=self.CHUNK_DURATION_S,
            log_prefix="OpenAI STT",
        )
        if not chunks:
            return ""
        try:
            parts: list[str] = []
            for chunk in chunks:
                text = await self._transcribe_one(chunk)
                if text:
                    parts.append(text)
            return " ".join(parts).strip()
        finally:
            _cleanup_chunks(chunks)

    async def _transcribe_one(self, path: Path) -> str:
        try:
            async with httpx.AsyncClient() as client:
                with open(path, "rb") as f:
                    files = {"file": (path.name, f), "model": (None, "whisper-1")}
                    headers = {"Authorization": f"Bearer {self.api_key}"}
                    response = await client.post(
                        self.api_url, headers=headers, files=files, timeout=60.0,
                    )
                    response.raise_for_status()
                    return (response.json().get("text") or "").strip()
        except Exception as e:
            logger.error("OpenAI transcription error: {}", e)
            return ""


# ---------------------------------------------------------------------------
# Yandex SpeechKit (short-audio v1)
# ---------------------------------------------------------------------------

class YandexTranscriptionProvider:
    """Voice transcription via Yandex Cloud SpeechKit (short-audio STT v1).

    Requires an Api-Key and folder_id from Yandex Cloud. Input is sent
    as raw bytes; ``oggopus`` is the native VK / Telegram voice format
    so no conversion is needed for chat audio.

    Yandex's short-audio endpoint refuses payloads >1 MB
    (``BAD_REQUEST: audio should be less than 1 mb``) — at Telegram's
    typical Opus bitrate that's ~50-60 seconds. Long clips get split
    via the shared ``split_audio_with_ffmpeg`` helper.
    """

    DEFAULT_URL = "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize"

    # Aim well below the 1 MB cap so a chunk landing exactly on the
    # boundary doesn't waste a round-trip on BAD_REQUEST.
    MAX_PAYLOAD_BYTES = 900 * 1024
    # ~30 s ≈ 500 KB at Telegram bitrate, comfortably under cap.
    CHUNK_DURATION_S = 30

    def __init__(
        self,
        api_key: str | None = None,
        folder_id: str | None = None,
        api_base: str | None = None,
        lang: str | None = None,
        audio_format: str = "oggopus",
    ):
        self.api_key = api_key or os.environ.get("YANDEX_API_KEY")
        self.folder_id = folder_id or os.environ.get("YC_FOLDER_ID")
        self.api_url = api_base or os.environ.get("YANDEX_STT_URL") or self.DEFAULT_URL
        self.lang = lang or os.environ.get("STT_LANG") or "ru-RU"
        self.audio_format = audio_format

    async def transcribe(self, file_path: str | Path) -> str:
        if not self.api_key or not self.folder_id:
            logger.warning("Yandex STT api_key/folder_id not configured")
            return ""
        path = Path(file_path)
        if not path.exists():
            logger.error("Audio file not found: {}", file_path)
            return ""
        try:
            size = path.stat().st_size
        except OSError as e:
            logger.error("Yandex STT: stat failed: {}", e)
            return ""

        if size <= self.MAX_PAYLOAD_BYTES:
            return await self._transcribe_bytes(path.read_bytes())

        chunks = await split_audio_with_ffmpeg(
            path,
            target_bytes=self.MAX_PAYLOAD_BYTES,
            duration_s=self.CHUNK_DURATION_S,
            log_prefix="Yandex STT",
        )
        if not chunks:
            return ""
        try:
            parts: list[str] = []
            for chunk in chunks:
                # Hard-skip a segment Yandex would reject anyway —
                # logged in split helper but not enforced there.
                if chunk.stat().st_size > self.MAX_PAYLOAD_BYTES:
                    continue
                text = await self._transcribe_bytes(chunk.read_bytes())
                if text:
                    parts.append(text)
            return " ".join(parts).strip()
        finally:
            _cleanup_chunks(chunks)

    async def _transcribe_bytes(self, audio: bytes) -> str:
        try:
            params = {
                "folderId": self.folder_id,
                "lang": self.lang,
                "format": self.audio_format,
            }
            headers = {"Authorization": f"Api-Key {self.api_key}"}
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=10.0),
            ) as client:
                r = await client.post(
                    self.api_url, params=params, headers=headers, content=audio,
                )
            data = r.json()
            if "error_code" in data or "error_message" in data:
                logger.warning("Yandex STT error: {}", data)
                return ""
            return (data.get("result") or "").strip()
        except Exception as e:
            logger.error("Yandex transcription error: {}", e)
            return ""


# ---------------------------------------------------------------------------
# Groq Whisper
# ---------------------------------------------------------------------------

class GroqTranscriptionProvider:
    """Voice transcription via Groq's Whisper API.

    Groq offers fast Whisper with the same 25 MB upload cap as OpenAI.
    Long clips get split via the shared ffmpeg helper.
    """

    MAX_PAYLOAD_BYTES = 24 * 1024 * 1024
    CHUNK_DURATION_S = 600

    def __init__(self, api_key: str | None = None, api_base: str | None = None):
        self.api_key = api_key or os.environ.get("GROQ_API_KEY")
        self.api_url = (
            api_base
            or os.environ.get("GROQ_BASE_URL")
            or "https://api.groq.com/openai/v1/audio/transcriptions"
        )

    async def transcribe(self, file_path: str | Path) -> str:
        if not self.api_key:
            logger.warning("Groq API key not configured for transcription")
            return ""
        path = Path(file_path)
        if not path.exists():
            logger.error("Audio file not found: {}", file_path)
            return ""
        try:
            size = path.stat().st_size
        except OSError as e:
            logger.error("Groq STT: stat failed: {}", e)
            return ""

        if size <= self.MAX_PAYLOAD_BYTES:
            return await self._transcribe_one(path)

        chunks = await split_audio_with_ffmpeg(
            path,
            target_bytes=self.MAX_PAYLOAD_BYTES,
            duration_s=self.CHUNK_DURATION_S,
            log_prefix="Groq STT",
        )
        if not chunks:
            return ""
        try:
            parts: list[str] = []
            for chunk in chunks:
                text = await self._transcribe_one(chunk)
                if text:
                    parts.append(text)
            return " ".join(parts).strip()
        finally:
            _cleanup_chunks(chunks)

    async def _transcribe_one(self, path: Path) -> str:
        try:
            async with httpx.AsyncClient() as client:
                with open(path, "rb") as f:
                    files = {
                        "file": (path.name, f),
                        "model": (None, "whisper-large-v3"),
                    }
                    headers = {"Authorization": f"Bearer {self.api_key}"}
                    response = await client.post(
                        self.api_url,
                        headers=headers,
                        files=files,
                        timeout=60.0,
                    )
                    response.raise_for_status()
                    return (response.json().get("text") or "").strip()
        except Exception as e:
            logger.error("Groq transcription error: {}", e)
            return ""
