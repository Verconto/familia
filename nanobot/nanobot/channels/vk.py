"""VK Community Bots channel (familia extension).

Native VK channel using the Bots Long Poll API — no third-party SDK,
just ``httpx``.  Replaces the ``tg-vk-bridge`` wrapper we used while
the Telegram Bot API was easier to integrate than VK directly.

Config (``config.channels.vk``)::

    {
      "enabled": true,
      "group_id": 123456789,
      "access_token": "<community-token>",
      "api_version": "5.199",
      "allow_from": ["12345"],
      "long_poll_wait": 25
    }

The community token must be issued with ``messages`` (and ``photos`` /
``docs`` if you want outbound media) scope in the community management
UI.  ``long_poll_wait`` is the VK server hold time (25s is
VK-recommended).
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import random
from pathlib import Path
from typing import Any

import httpx
from loguru import logger
from pydantic import Field

from familia.principals import resolve_actor
from nanobot.bus.events import CallbackEvent, OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.paths import get_media_dir
from nanobot.config.schema import Base


VK_API_BASE = "https://api.vk.com/method"
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
_VK_COLORS = {"primary", "secondary", "positive", "negative"}


def _build_vk_keyboard(spec: dict[str, Any]) -> str:
    """Translate the channel-agnostic keyboard shape into a VK keyboard JSON string.

    Input schema (shared across channels; unknown fields are ignored)::

        {
          "inline":   bool,          # default False
          "one_time": bool,          # persistent keyboards only
          "rows": [                  # list of rows
            [                        # row = list of buttons
              {"label": "…",
               "payload": {...} | str | None,   # → callback button
               "link":    "…"        | None,    # → open_link button
               "color":   "primary|secondary|positive|negative" | None},
              ...
            ],
            ...
          ],
        }
    """
    rows_in = spec.get("rows") or []
    correlation_id = spec.get("correlation_id")
    vk_rows: list[list[dict[str, Any]]] = []
    for row in rows_in:
        vk_row: list[dict[str, Any]] = []
        for btn in row:
            label = btn.get("label", "")
            link = btn.get("link")
            payload = btn.get("payload")
            color = btn.get("color")
            if link:
                action = {"type": "open_link", "label": label, "link": link}
                entry: dict[str, Any] = {"action": action}
            elif payload is not None:
                # Wrap user payload with the label (so the receiving side
                # knows which button was pressed — VK's callback delivers
                # payload only) and optional correlation id (for blocking
                # ask_principal calls).  Dispatcher unwraps these before
                # emitting CallbackEvent, so consumers see the original
                # payload.
                wrapper: dict[str, Any] = {"_l": label, "_p": payload}
                if correlation_id:
                    wrapper["_cid"] = correlation_id
                payload_str = json.dumps(
                    wrapper, ensure_ascii=False, separators=(",", ":")
                )
                if len(payload_str) > 255:
                    raise ValueError(
                        f"VK keyboard payload exceeds 255 chars: {len(payload_str)}"
                    )
                action = {"type": "callback", "label": label, "payload": payload_str}
                entry = {"action": action}
                entry["color"] = color if color in _VK_COLORS else "primary"
            else:
                entry = {"action": {"type": "text", "label": label}}
                if color in _VK_COLORS:
                    entry["color"] = color
            vk_row.append(entry)
        vk_rows.append(vk_row)
    kb: dict[str, Any] = {"inline": bool(spec.get("inline", False)), "buttons": vk_rows}
    if not kb["inline"] and spec.get("one_time"):
        kb["one_time"] = True
    return json.dumps(kb, ensure_ascii=False)


class VKConfig(Base):
    enabled: bool = False
    group_id: int = 0
    access_token: str = ""
    api_version: str = "5.199"
    allow_from: list[str] = Field(default_factory=list)
    long_poll_wait: int = 25
    streaming: bool = False
    # General proxy URL for ALL VK traffic (long poll, API calls, media
    # fetches). Accepts ``socks5://`` or ``http(s)://`` with optional
    # inline credentials. When empty, falls back to ``media_proxy`` /
    # the ``VK_MEDIA_PROXY`` env var for media-only routing.
    proxy: str = ""
    # Legacy: media-only override. Kept so existing config.json with
    # only this field set keeps working. Prefer ``proxy`` for new
    # installs — it covers media too.
    media_proxy: str = ""


class VKChannel(BaseChannel):
    """VK Community Bots Long Poll channel."""

    name = "vk"
    display_name = "VK"

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return VKConfig().model_dump(by_alias=True)

    @staticmethod
    def _mask_proxy(url: str) -> str:
        """Strip ``user:pass@`` from a proxy URL for safe logging.

        Keeps scheme + host:port visible (useful for ops); blanks
        credentials. Mirrors the same logic the admin CLI does in
        ``_redact_proxy_url``.
        """
        if not url or "://" not in url:
            return url
        scheme, rest = url.split("://", 1)
        if "@" not in rest:
            return url
        _creds, host = rest.rsplit("@", 1)
        return f"{scheme}://***@{host}"

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = VKConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: VKConfig = config
        self._client: httpx.AsyncClient | None = None
        self._lp_server: str | None = None
        self._lp_key: str | None = None
        self._lp_ts: str | None = None
        # chat_id → running typing task; lets send() cancel whichever
        # message_new started it.
        self._typing_tasks: dict[str, asyncio.Task[None]] = {}

    async def start(self) -> None:
        if not self.config.access_token or not self.config.group_id:
            logger.error("VK: access_token or group_id not configured")
            return

        self._running = True
        client_kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(self.config.long_poll_wait + 10),
        }
        # General VK proxy (long poll + api). When unset, traffic goes
        # direct. The narrower ``media_proxy`` is consulted only for
        # media-fetch in ``_download_attachment``; it's intentionally
        # NOT used here so users who set ``proxy`` get a single proxy
        # that covers everything.
        if self.config.proxy:
            client_kwargs["proxy"] = self.config.proxy
        self._client = httpx.AsyncClient(**client_kwargs)
        logger.info(
            "VK channel starting (group_id={}{})",
            self.config.group_id,
            f", proxy={self._mask_proxy(self.config.proxy)}" if self.config.proxy else "",
        )

        try:
            await self._refresh_long_poll_server()
        except Exception as e:
            logger.error("VK: failed to get long poll server: {}", e)
            self._running = False
            return

        while self._running:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(
                    "VK: poll error ({}: {!r}) — retrying in 3s",
                    type(e).__name__, e,
                )
                await asyncio.sleep(3)

    async def stop(self) -> None:
        self._running = False
        if self._client:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None

    # --- VK API plumbing ---------------------------------------------------

    async def _api(self, method: str, **params: Any) -> Any:
        assert self._client is not None
        params = {k: v for k, v in params.items() if v is not None}
        params.setdefault("v", self.config.api_version)
        params.setdefault("access_token", self.config.access_token)
        r = await self._client.post(f"{VK_API_BASE}/{method}", data=params)
        payload = r.json()
        if "error" in payload:
            raise RuntimeError(f"VK {method}: {payload['error']}")
        return payload.get("response")

    # --- Typing indicator --------------------------------------------------

    async def _typing_loop(self, peer_id: int, max_seconds: int = 180) -> None:
        """Refresh VK 'typing...' activity until cancelled or timeout.

        VK's ``messages.setActivity`` expires after ~10 s, so we re-issue
        it every 4 s. Silent on errors — this is a cosmetic signal.
        """
        deadline = asyncio.get_event_loop().time() + max_seconds
        try:
            while asyncio.get_event_loop().time() < deadline:
                try:
                    await self._api(
                        "messages.setActivity",
                        peer_id=peer_id,
                        type="typing",
                        group_id=self.config.group_id,
                    )
                except Exception as e:
                    logger.debug("VK: setActivity failed: {}", e)
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            return

    def _start_typing(self, peer_id: str) -> None:
        """Start (or refresh) the typing indicator for a chat."""
        old = self._typing_tasks.get(peer_id)
        if old is not None and not old.done():
            return
        try:
            task = asyncio.create_task(
                self._typing_loop(int(peer_id)),
                name=f"vk-typing-{peer_id}",
            )
        except ValueError:
            return
        self._typing_tasks[peer_id] = task

    def _stop_typing(self, peer_id: str) -> None:
        task = self._typing_tasks.pop(peer_id, None)
        if task is not None and not task.done():
            task.cancel()

    # --- Outbound ----------------------------------------------------------

    async def send(self, msg: OutboundMessage) -> None:
        # Stop the "typing…" indicator as soon as anything is being
        # delivered — including stream deltas, since the user already
        # sees text appearing.
        self._stop_typing(str(msg.chat_id))
        if not self._client:
            logger.warning("VK: client not running")
            return
        if not msg.content and not msg.media:
            return

        attachments: list[str] = []
        for path in msg.media or []:
            try:
                attachments.append(await self._upload_media(int(msg.chat_id), path))
            except Exception as e:
                logger.error("VK: failed to upload {}: {}", path, e)

        keyboard_json: str | None = None
        kb_spec = (msg.metadata or {}).get("keyboard")
        if kb_spec:
            try:
                keyboard_json = _build_vk_keyboard(kb_spec)
            except Exception as e:
                logger.error("VK: invalid keyboard spec: {}", e)

        await self._api(
            "messages.send",
            peer_id=msg.chat_id,
            message=msg.content or "",
            random_id=random.randint(1, 2**31 - 1),
            disable_mentions=1,
            attachment=",".join(attachments) if attachments else None,
            keyboard=keyboard_json,
        )

    async def _upload_media(self, peer_id: int, path: str) -> str:
        """Upload a local file to VK and return its ``<type><owner>_<id>`` ref."""
        assert self._client is not None
        p = Path(path)
        data = p.read_bytes()
        mime, _ = mimetypes.guess_type(p.name)
        is_image = p.suffix.lower() in _IMAGE_EXTS

        if is_image:
            server = await self._api("photos.getMessagesUploadServer", peer_id=peer_id)
            files = {"photo": (p.name, data, mime or "image/jpeg")}
            up = (await self._client.post(server["upload_url"], files=files)).json()
            saved = await self._api(
                "photos.saveMessagesPhoto",
                photo=up.get("photo"), server=up.get("server"), hash=up.get("hash"),
            )
            item = saved[0]
            return f"photo{item['owner_id']}_{item['id']}"

        # Fallback: upload as a generic doc attachment.
        server = await self._api("docs.getMessagesUploadServer", peer_id=peer_id, type="doc")
        files = {"file": (p.name, data, mime or "application/octet-stream")}
        up = (await self._client.post(server["upload_url"], files=files)).json()
        saved = await self._api("docs.save", file=up.get("file"))
        doc_type = saved.get("type") or "doc"
        item = saved.get(doc_type) or saved.get("doc") or {}
        return f"doc{item['owner_id']}_{item['id']}"

    # --- Long poll ---------------------------------------------------------

    async def _refresh_long_poll_server(self) -> None:
        resp = await self._api("groups.getLongPollServer", group_id=self.config.group_id)
        self._lp_server = resp["server"]
        self._lp_key = resp["key"]
        self._lp_ts = resp["ts"]
        logger.info("VK: long poll server acquired (ts={})", self._lp_ts)

    async def _poll_once(self) -> None:
        assert self._client is not None and self._lp_server
        params = {
            "act": "a_check",
            "key": self._lp_key,
            "ts": self._lp_ts,
            "wait": self.config.long_poll_wait,
        }
        r = await self._client.get(self._lp_server, params=params)
        payload = r.json()

        # 1 → ts stale; 2 → key expired; 3 → both gone.
        if "failed" in payload:
            code = payload["failed"]
            if code == 1 and "ts" in payload:
                self._lp_ts = payload["ts"]
                return
            logger.info("VK: long poll failed={}, refreshing server", code)
            await self._refresh_long_poll_server()
            return

        self._lp_ts = payload.get("ts", self._lp_ts)
        # Handle updates concurrently so one slow attachment download doesn't
        # freeze the poll loop and let VK events pile up server-side.
        for update in payload.get("updates", []):
            asyncio.create_task(self._safe_handle_update(update))

    async def _safe_handle_update(self, update: dict[str, Any]) -> None:
        try:
            await self._handle_update(update)
        except Exception:
            logger.exception("VK: unhandled error in update handler")

    async def _handle_update(self, update: dict[str, Any]) -> None:
        ev_type = update.get("type")
        logger.debug("VK update: type={} keys={}", ev_type, list((update.get("object") or {}).keys()))
        if ev_type == "message_event":
            await self._handle_message_event(update.get("object") or {})
            return
        if ev_type != "message_new":
            return
        obj = update.get("object") or {}
        message = obj.get("message") or obj
        from_id = message.get("from_id")
        peer_id = message.get("peer_id")
        text = message.get("text") or ""
        if from_id is None or peer_id is None:
            return
        if int(from_id) < 0:  # community-sent message (ours)
            return

        # Show "typing…" in the chat while the agent processes the message;
        # send() cancels this once the reply starts going out.
        self._start_typing(str(peer_id))

        media_paths, content_extras = await self._download_attachments(
            message.get("attachments") or []
        )
        content = text
        if content_extras:
            content = (content + "\n" if content else "") + "\n".join(content_extras)

        try:
            await self._handle_message(
                sender_id=str(from_id),
                chat_id=str(peer_id),
                content=content,
                media=media_paths,
                metadata={"vk": {"message": message}},
            )
        except Exception:
            logger.exception("VK: error handling message from {}", from_id)
            self._stop_typing(str(peer_id))

    async def _collapse_keyboard(
        self,
        peer_id: int,
        conversation_message_id: int,
        label: str,
        wrapped_payload: Any,
    ) -> None:
        """After a press, delete the whole message with buttons.

        VK's ``messages.edit`` cannot reliably strip an inline keyboard from
        a community-sent message, so we remove the message entirely via
        ``messages.delete`` with ``delete_for_all=1``.  The chosen payload
        still reaches the agent through the CallbackEvent path; the agent's
        next reply supplies whatever confirmation/continuation the user
        needs.
        """
        del label, wrapped_payload  # kept in signature for backward compat
        await self._api(
            "messages.delete",
            peer_id=peer_id,
            cmids=conversation_message_id,
            delete_for_all=1,
        )

    async def _handle_message_event(self, obj: dict[str, Any]) -> None:
        """Handle an inline-keyboard callback press.

        ACK immediately via ``messages.sendMessageEventAnswer`` so VK drops
        the spinner on the client, then publish a ``CallbackEvent`` for
        scenarios to consume.  Kept deliberately thin — no policy, no
        business logic — so any scenario can layer its own handling on
        top via ``bus.consume_callback()``.
        """
        event_id = obj.get("event_id")
        user_id = obj.get("user_id")
        peer_id = obj.get("peer_id")
        payload_raw = obj.get("payload")
        conv_msg_id = obj.get("conversation_message_id")
        if event_id is None or user_id is None or peer_id is None:
            return

        # Ack first so the button stops spinning even if downstream is slow.
        try:
            await self._api(
                "messages.sendMessageEventAnswer",
                event_id=event_id,
                user_id=user_id,
                peer_id=peer_id,
            )
        except Exception as e:
            logger.warning("VK: sendMessageEventAnswer failed: {}", e)

        decoded: Any = payload_raw
        if isinstance(payload_raw, str):
            try:
                decoded = json.loads(payload_raw)
            except (ValueError, TypeError):
                decoded = payload_raw

        # Unwrap the {"_l": label, "_p": user_payload} envelope that
        # _build_vk_keyboard added on send.  Keeping the label lets us
        # collapse the keyboard down to just the pressed button — the
        # CallbackEvent below carries only the original user payload.
        pressed_label: str | None = None
        correlation_id: str | None = None
        user_payload: Any = decoded
        if isinstance(decoded, dict) and "_l" in decoded and "_p" in decoded:
            pressed_label = str(decoded.get("_l") or "")
            user_payload = decoded.get("_p")
            cid = decoded.get("_cid")
            if isinstance(cid, str) and cid:
                correlation_id = cid

        if pressed_label and conv_msg_id is not None:
            try:
                await self._collapse_keyboard(peer_id, conv_msg_id, pressed_label, payload_raw)
            except Exception as e:
                logger.warning("VK: failed to collapse keyboard: {}", e)

        try:
            actor = resolve_actor("vk", str(user_id))
        except Exception:
            actor = None

        await self.bus.publish_callback(
            CallbackEvent(
                channel="vk",
                sender_id=str(user_id),
                chat_id=str(peer_id),
                payload=user_payload,
                actor=actor,
                metadata={
                    "conversation_message_id": conv_msg_id,
                    "event_id": event_id,
                    "pressed_label": pressed_label,
                    "correlation_id": correlation_id,
                },
            )
        )

    # --- Inbound media -----------------------------------------------------

    async def _download_attachments(
        self, attachments: list[dict[str, Any]]
    ) -> tuple[list[str], list[str]]:
        """Return (local_paths, content_annotations) for inbound attachments."""
        media_paths: list[str] = []
        notes: list[str] = []
        media_dir = get_media_dir("vk")

        for att in attachments:
            att_type = att.get("type")
            try:
                if att_type == "photo":
                    path = await self._download_photo(att.get("photo") or {}, media_dir)
                    if path:
                        media_paths.append(path)
                        notes.append(f"[photo: {path}]")
                elif att_type == "audio_message":
                    am = att.get("audio_message") or {}
                    url = am.get("link_ogg") or am.get("link_mp3")
                    path = await self._download_to(url, media_dir, f"voice_{am.get('id')}.ogg")
                    if path:
                        media_paths.append(path)
                        transcription = await self.transcribe_audio(path)
                        if transcription:
                            logger.info("VK voice transcribed: {}...", transcription[:50])
                            notes.append(f"[transcription: {transcription}]")
                        else:
                            notes.append(f"[voice: {path}]")
                elif att_type == "doc":
                    doc = att.get("doc") or {}
                    name = doc.get("title") or f"doc_{doc.get('id')}"
                    ext = doc.get("ext")
                    if ext and not name.endswith(f".{ext}"):
                        name = f"{name}.{ext}"
                    path = await self._download_to(doc.get("url"), media_dir, name)
                    if path:
                        media_paths.append(path)
                        notes.append(f"[doc: {path}]")
                elif att_type == "sticker":
                    sticker = att.get("sticker") or {}
                    images = sticker.get("images") or []
                    biggest = max(
                        images,
                        key=lambda s: (s.get("width") or 0) * (s.get("height") or 0),
                        default=None,
                    )
                    if biggest:
                        path = await self._download_to(
                            biggest.get("url"), media_dir, f"sticker_{sticker.get('sticker_id')}.png"
                        )
                        if path:
                            media_paths.append(path)
                            notes.append(f"[sticker: {path}]")
                else:
                    notes.append(f"[{att_type}: unsupported]")
            except Exception as e:
                logger.warning(
                    "VK: failed to download {} attachment: {!r} — raw: {}",
                    att_type, e, att,
                )

        return media_paths, notes

    async def _download_photo(self, photo: dict[str, Any], media_dir: Path) -> str | None:
        sizes = [s for s in (photo.get("sizes") or []) if s.get("url")]
        if not sizes:
            return None
        capped = [s for s in sizes if (s.get("width") or 0) <= 1280]
        pool = capped or sizes
        biggest = max(pool, key=lambda s: (s.get("width") or 0) * (s.get("height") or 0))
        name = f"photo_{photo.get('owner_id')}_{photo.get('id')}.jpg"
        return await self._download_to(biggest["url"], media_dir, name)

    async def _download_to(
        self, url: str | None, media_dir: Path, filename: str
    ) -> str | None:
        if not url:
            return None
        # Use a fresh client for CDN fetches: VK user-content hosts (sun*.userapi.com)
        # occasionally stall, and the long-poll client's 35s timeout isn't always
        # enough. Socks-proxies (VK_MEDIA_PROXY) add extra latency, so 60s read
        # budget + 3 retries with backoff is a more realistic envelope.
        timeout = httpx.Timeout(60.0, connect=10.0)
        # Priority for media: explicit media_proxy (legacy), then
        # general proxy (new), then env-var (legacy ops setup),
        # then direct.
        proxy = (
            self.config.media_proxy
            or self.config.proxy
            or os.environ.get("VK_MEDIA_PROXY")
            or None
        )
        client_kwargs: dict[str, Any] = {"timeout": timeout}
        if proxy:
            client_kwargs["proxy"] = proxy
        last_err: Exception | None = None
        async with httpx.AsyncClient(**client_kwargs) as client:
            for attempt in range(3):
                try:
                    r = await client.get(url, follow_redirects=True)
                    r.raise_for_status()
                    path = media_dir / filename
                    path.write_bytes(r.content)
                    return str(path)
                except (httpx.TimeoutException, httpx.TransportError) as e:
                    last_err = e
                    if attempt < 2:
                        await asyncio.sleep(2 ** attempt)
        assert last_err is not None
        raise last_err
