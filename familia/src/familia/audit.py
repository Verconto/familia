"""JSONL audit log for familia.

Appends one JSON object per line to a configurable audit file. Covers
tool calls, policy decisions and ``ask_principal`` lifecycle. Swallows
all write errors — a broken audit must never break the agent.

Path: ``FAMILIA_AUDIT_FILE`` env var; default ``audit.jsonl`` in CWD.
In the familia container it resolves to ``~/.nanobot/audit.jsonl``,
which is mounted from ``/opt/familia/config/audit.jsonl`` on the host.

Event shape::

    {"ts": "2026-04-21T14:30:00+00:00", "kind": "tool_call",
     "tool": "ask_principal", "actor": "owner", ...}

Known kinds:
- ``tool_call`` — tool invocation (before execution)
- ``policy`` — policy engine decision
- ``ask_sent`` / ``ask_answered`` / ``ask_timeout`` — ask_principal lifecycle
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from loguru import logger


_lock = threading.Lock()

_ERR_COOLDOWN_SEC = 60.0
_last_err_ts: float = 0.0
_suppressed_since_log: int = 0

# SR-6: audit file gets owner-only permissions on first write per process.
# Tracked per absolute path so test isolation (each test sets its own
# FAMILIA_AUDIT_FILE) doesn't skip the chmod.
_chmod_done: set[str] = set()

# Hard cap on per-field string size to prevent log inflation by adversarial
# tool args (e.g. a 200KB ``reason`` from a jailbroken admin_grant). Truncated
# values get a "... (truncated)" marker so analysts know the value was clipped.
_MAX_FIELD_LEN = 4096

# Size-based rotation: when audit.jsonl crosses this threshold, shift
# .1→.2, .2→.3, … and rename current to .1. Keep last KEEP_ROTATIONS
# generations. 50 MB × 5 ≈ 250 MB upper bound for total audit history;
# typical line is 200-400 bytes so 50 MB ≈ 130-260K events per file.
_MAX_AUDIT_BYTES = 50 * 1024 * 1024
_KEEP_ROTATIONS = 5


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_FIELD_LEN:
        return value[:_MAX_FIELD_LEN] + "... (truncated)"
    if isinstance(value, dict):
        return {k: _clip(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clip(v) for v in value]
    return value


def _path() -> Path:
    return Path(os.environ.get("FAMILIA_AUDIT_FILE", "audit.jsonl"))


def _report_write_error(kind: str, exc: BaseException) -> None:
    global _last_err_ts, _suppressed_since_log
    import time

    now = time.monotonic()
    if now - _last_err_ts < _ERR_COOLDOWN_SEC:
        _suppressed_since_log += 1
        return
    suppressed = _suppressed_since_log
    _suppressed_since_log = 0
    _last_err_ts = now
    if suppressed:
        logger.warning(
            "audit: failed to write {}: {} (and {} similar errors suppressed in last {}s)",
            kind, exc, suppressed, int(_ERR_COOLDOWN_SEC),
        )
    else:
        logger.warning("audit: failed to write {}: {}", kind, exc)


def _ensure_secure_mode(path: Path) -> None:
    """SR-6: enforce 0600 on the audit file.

    Best-effort: any chmod failure is swallowed (typically Windows or a
    bind-mount where chmod is a no-op). We still apply the call once per
    file path so a fresh deploy gets it on the very first write.
    """
    abs_path = str(path.resolve())
    if abs_path in _chmod_done:
        return
    _chmod_done.add(abs_path)
    if os.name != "posix":
        return
    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        logger.debug("audit: chmod 0600 failed on {}: {}", path, exc)


def _maybe_rotate(path: Path) -> None:
    """Rotate audit.jsonl when it crosses :data:`_MAX_AUDIT_BYTES`.

    Shifts ``audit.jsonl.{n} → audit.jsonl.{n+1}`` for n in [N-1..1]
    (oldest first so renames don't clobber), drops anything beyond
    ``_KEEP_ROTATIONS``, then renames the current file to ``.1``.
    Caller must hold ``_lock``. Failures are warned-and-swallowed —
    a rotation hiccup must never break ``log_event``.
    """
    try:
        if not path.exists() or path.stat().st_size < _MAX_AUDIT_BYTES:
            return
    except OSError:
        return
    try:
        # Drop the oldest beyond the keep window.
        beyond = path.with_suffix(path.suffix + f".{_KEEP_ROTATIONS}")
        if beyond.exists():
            try:
                beyond.unlink()
            except OSError:
                pass
        # Shift older rotations up by one (.4 → .5, .3 → .4, …).
        for n in range(_KEEP_ROTATIONS - 1, 0, -1):
            src = path.with_suffix(path.suffix + f".{n}")
            dst = path.with_suffix(path.suffix + f".{n + 1}")
            if src.exists():
                try:
                    src.replace(dst)
                except OSError as exc:
                    logger.debug("audit: rotate {} → {}: {}", src, dst, exc)
        # Move current → .1.
        try:
            path.replace(path.with_suffix(path.suffix + ".1"))
        except OSError as exc:
            logger.warning("audit: rotate current: {}", exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("audit: rotation failed: {}", exc)


def log_event(kind: str, **fields: Any) -> None:
    """Append one JSONL record; never raises."""
    try:
        rec: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "kind": kind,
        }
        rec.update({k: _clip(v) for k, v in fields.items()})
        line = json.dumps(rec, ensure_ascii=False, default=str)
        path = _path()
        with _lock:
            _maybe_rotate(path)
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
            _ensure_secure_mode(path)
    except Exception as e:
        _report_write_error(kind, e)
