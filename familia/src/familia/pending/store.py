"""Atomic JSON-backed store for pending-principal entries.

File: ``$FAMILIA_PENDING_FILE`` (default ``~/.nanobot/pending_principals.json``).

Schema sentinel ``__familia_pending_v1: true`` matches the convention
used by other structural files in the project. Every read tolerates a
missing/corrupt file (returns empty), every write goes through
``tmp + os.replace`` for atomic semantics.

The ``record`` API is idempotent on ``(channel, sender_id)``: first
call creates a row, subsequent calls update ``last_seen_ms``,
``last_message_preview`` and bump ``msg_count``. This means a
flooding stranger generates one row, not N. A hard cap
(``PENDING_CAP``) keeps the file bounded — overflow events are
audit-logged but produce no row and no reply.

``reject`` is a soft block: rejected ``(channel, sender_id)`` lands
in ``rejected`` for ``REJECT_COOLDOWN_SECS``; another message before
that wakes nothing (no row, no reply). After the cooldown the same
sender is observed again as fresh.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from loguru import logger

from familia import audit

SCHEMA_KEY = "__familia_pending_v1"
PENDING_CAP = 100
REJECT_COOLDOWN_SECS = 24 * 3600
PREVIEW_MAX_CHARS = 200

_lock = threading.Lock()


@dataclass
class PendingEntry:
    channel: str
    sender_id: str
    display_name: str = ""
    first_seen_ms: int = 0
    last_seen_ms: int = 0
    last_message_preview: str = ""
    msg_count: int = 0


@dataclass
class _State:
    entries: list[PendingEntry] = field(default_factory=list)
    rejected: dict[str, int] = field(default_factory=dict)  # "ch|sid" -> ts_secs

    def to_disk(self) -> dict[str, Any]:
        return {
            SCHEMA_KEY: True,
            "entries": [asdict(e) for e in self.entries],
            "rejected": dict(self.rejected),
        }

    @classmethod
    def from_disk(cls, raw: Any) -> "_State":
        if not isinstance(raw, dict) or raw.get(SCHEMA_KEY) is not True:
            return cls()
        entries: list[PendingEntry] = []
        for d in raw.get("entries") or []:
            if not isinstance(d, dict):
                continue
            try:
                entries.append(
                    PendingEntry(
                        channel=str(d["channel"]),
                        sender_id=str(d["sender_id"]),
                        display_name=str(d.get("display_name", "")),
                        first_seen_ms=int(d.get("first_seen_ms", 0) or 0),
                        last_seen_ms=int(d.get("last_seen_ms", 0) or 0),
                        last_message_preview=str(d.get("last_message_preview", "")),
                        msg_count=int(d.get("msg_count", 0) or 0),
                    )
                )
            except (KeyError, TypeError, ValueError):
                # Skip malformed row, keep the rest. Fail-closed at row
                # granularity so one bad entry doesn't drop the whole list.
                logger.warning("pending: skipping malformed entry: {!r}", d)
        rejected_raw = raw.get("rejected") or {}
        rejected: dict[str, int] = {}
        if isinstance(rejected_raw, dict):
            for k, v in rejected_raw.items():
                try:
                    rejected[str(k)] = int(v)
                except (TypeError, ValueError):
                    continue
        return cls(entries=entries, rejected=rejected)


def _path() -> Path:
    """Resolve the pending-store path. Re-reads env vars on every call so
    tests with monkey-patched ``$FAMILIA_PENDING_FILE`` keep working."""
    p = os.environ.get("FAMILIA_PENDING_FILE")
    if p:
        return Path(p)
    home = os.environ.get("HOME") or "."
    return Path(home) / ".nanobot" / "pending_principals.json"


def _load(path: Path | None = None) -> _State:
    """Read state from disk. ``path`` is computed once per public-API
    call to avoid an env-var race between read and write."""
    path = path or _path()
    if not path.exists():
        return _State()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("pending: failed to read {}: {} — treating as empty", path, exc)
        return _State()
    return _State.from_disk(raw)


def _save(state: _State, path: Path | None = None) -> None:
    path = path or _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state.to_disk(), ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        # Windows / non-POSIX: best-effort only.
        pass
    os.replace(tmp, path)


def _key(channel: str, sender_id: str) -> str:
    return f"{channel}|{sender_id}"


def _gc_rejected(state: _State, now_secs: int) -> None:
    expired = [k for k, ts in state.rejected.items() if now_secs - ts >= REJECT_COOLDOWN_SECS]
    for k in expired:
        state.rejected.pop(k, None)


def is_rejected(channel: str, sender_id: str) -> bool:
    """True if the sender was rejected within the cooldown window."""
    with _lock:
        path = _path()
        state = _load(path)
        _gc_rejected(state, int(time.time()))
        return _key(channel, sender_id) in state.rejected


def record(
    channel: str,
    sender_id: str,
    display_name: str,
    message_preview: str,
) -> PendingEntry | None:
    """Idempotent insert/update.

    Returns the resulting entry, or ``None`` if the request was
    suppressed (active reject cooldown, or pending cap hit). Callers
    use the return value to decide whether to send the templated reply
    — silence on overflow/rejected is intentional, anti-flood.
    """
    now_ms = int(time.time() * 1000)
    now_secs = now_ms // 1000
    preview = (message_preview or "").strip().replace("\n", " ")
    if len(preview) > PREVIEW_MAX_CHARS:
        preview = preview[: PREVIEW_MAX_CHARS - 1] + "…"
    with _lock:
        path = _path()
        state = _load(path)
        _gc_rejected(state, now_secs)
        if _key(channel, sender_id) in state.rejected:
            audit.log_event(
                "pending_record_skipped",
                actor=None,
                channel=channel,
                to_chat=sender_id,
                reason="rejected_cooldown",
            )
            return None

        existing = next(
            (
                e
                for e in state.entries
                if e.channel == channel and e.sender_id == sender_id
            ),
            None,
        )
        if existing is None:
            if len(state.entries) >= PENDING_CAP:
                audit.log_event(
                    "pending_overflow",
                    actor=None,
                    channel=channel,
                    to_chat=sender_id,
                    reason=f"cap={PENDING_CAP}",
                )
                return None
            entry = PendingEntry(
                channel=channel,
                sender_id=str(sender_id),
                display_name=display_name or "",
                first_seen_ms=now_ms,
                last_seen_ms=now_ms,
                last_message_preview=preview,
                msg_count=1,
            )
            state.entries.append(entry)
            audit.log_event(
                "pending_record",
                actor=None,
                channel=channel,
                to_chat=sender_id,
                reason="new",
            )
        else:
            existing.last_seen_ms = now_ms
            if preview:
                existing.last_message_preview = preview
            if display_name and not existing.display_name:
                existing.display_name = display_name
            existing.msg_count += 1
            entry = existing
            audit.log_event(
                "pending_record",
                actor=None,
                channel=channel,
                to_chat=sender_id,
                reason="dup",
            )
        _save(state, path)
        return entry


def list_all() -> list[PendingEntry]:
    with _lock:
        state = _load()
    return list(state.entries)


def approve(
    channel: str,
    sender_id: str,
) -> PendingEntry:
    """Drop the row from pending and return it. Caller mutates principals.json."""
    with _lock:
        path = _path()
        state = _load(path)
        for i, e in enumerate(state.entries):
            if e.channel == channel and e.sender_id == sender_id:
                approved = state.entries.pop(i)
                _save(state, path)
                audit.log_event(
                    "pending_approve",
                    actor=None,
                    channel=channel,
                    to_chat=sender_id,
                )
                return approved
    raise KeyError(f"no pending entry for {channel}:{sender_id}")


def reject(channel: str, sender_id: str) -> None:
    with _lock:
        path = _path()
        state = _load(path)
        before = len(state.entries)
        state.entries = [
            e
            for e in state.entries
            if not (e.channel == channel and e.sender_id == sender_id)
        ]
        # TODO: when the first repeat-flooder shows up, add a permanent
        # block list (separate file or sentinel value in ``rejected``)
        # so admin can opt-in to "never re-notice this sender". 24h
        # cooldown is fine for the unattended-misclick case but does
        # nothing against a determined adversary.
        state.rejected[_key(channel, sender_id)] = int(time.time())
        _save(state, path)
        audit.log_event(
            "pending_reject",
            actor=None,
            channel=channel,
            to_chat=sender_id,
            reason=f"removed={before - len(state.entries)}",
        )
