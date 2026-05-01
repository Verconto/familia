"""Global timezone for familia.

Single-TZ model: one IANA timezone for all participants. Storage layer
is always UTC-aware; presentation (prompts, user messages, reminders,
cron next_run rendering) converts from UTC to this global TZ.

Source of truth, in priority order:
  1. File at ``FAMILIA_TZ_STATE_FILE`` (runtime override written by
     ``admin_set_tz``). Default: ``/home/nanobot/.nanobot/tz``.
  2. Env ``FAMILIA_TZ`` (bootstrap default).
  3. ``"UTC"`` (last-resort fallback).

Admin-only writes go through :func:`set_current_tz`; reads via
:func:`get_current_tz` / :func:`current_tz_name` are cheap and pick up
file changes on every call (no caching — the file is small and reads
are not on a hot path).

Helpers:
  - :func:`now_utc` — aware UTC ``datetime``; use instead of
    ``datetime.now()`` / ``datetime.utcnow()``.
  - :func:`to_user_tz` / :func:`format_for_user` — presentation.
  - :func:`parse_user_datetime` — parse user-provided strings
    (reminders, scheduled actions) into aware UTC, honoring DST gaps.
"""

from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Final
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_DEFAULT_STATE_FILE: Final = "/home/nanobot/.nanobot/tz"
_FALLBACK_TZ: Final = "UTC"
# Longest IANA zone name is "America/Argentina/ComodRivadavia" (32 chars).
# Anything beyond this cap means the file is corrupt or tampered.
_STATE_FILE_MAX_BYTES: Final = 256


def _state_path() -> Path:
    return Path(os.environ.get("FAMILIA_TZ_STATE_FILE", _DEFAULT_STATE_FILE))


def _read_state_file() -> str | None:
    try:
        p = _state_path()
        if p.stat().st_size > _STATE_FILE_MAX_BYTES:
            return None
        raw = p.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return raw or None


def _validate(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as e:
        raise ValueError(f"Unknown IANA timezone: {name!r}") from e


def current_tz_name() -> str:
    """Current global TZ name (IANA). Never raises; falls back to UTC."""
    name = _read_state_file() or os.environ.get("FAMILIA_TZ") or _FALLBACK_TZ
    try:
        _validate(name)
    except ValueError:
        return _FALLBACK_TZ
    return name


def get_current_tz() -> ZoneInfo:
    """Current global TZ as ``ZoneInfo``. Never raises."""
    return _validate(current_tz_name())


def set_current_tz(name: str) -> ZoneInfo:
    """Persist a new global TZ. Atomic (unique tmp file + rename).

    Uses :func:`tempfile.mkstemp` so concurrent invocations don't race
    on a shared ``.tmp`` path. Raises :class:`ValueError` if *name* is
    not a valid IANA zone; the state file is not touched in that case.
    """
    zone = _validate(name)
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(name)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return zone


def now_utc() -> datetime:
    """Aware UTC ``datetime``. Use instead of ``datetime.now()``."""
    return datetime.now(timezone.utc)


def to_user_tz(dt: datetime) -> datetime:
    """Convert an aware datetime to the current global TZ."""
    if dt.tzinfo is None:
        raise ValueError("to_user_tz requires an aware datetime")
    return dt.astimezone(get_current_tz())


def format_for_user(dt: datetime, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """Render an aware datetime in the current global TZ."""
    return to_user_tz(dt).strftime(fmt)


# Date components in _FULL_RE are width-strict (YYYY-MM-DD) because the
# offset detector below keys off position 10; the hour is lenient
# (1–2 digits) to match the HH:MM shorthand's tolerance.
_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")
_FULL_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})[ T](\d{1,2}):(\d{2})$")
_OFFSET_TAIL_RE = re.compile(r"[+-]\d{2}:?\d{2}$")


def _has_explicit_offset(s: str) -> bool:
    """Detect trailing timezone marker on an ISO-ish string.

    Requires a full ``YYYY-MM-DDTHH:MM`` or ``YYYY-MM-DD HH:MM`` prefix
    (16 chars) before the offset so that bare-hour forms like
    ``"2026-05-01T09Z"`` are not silently accepted — such strings fall
    through to the other branches and fail cleanly with
    ``"Unrecognized datetime format"``.
    """
    if len(s) < 17:
        return False
    tail = s[16:]
    return tail.endswith("Z") or bool(_OFFSET_TAIL_RE.search(tail))


def _check_not_in_dst_gap(local_dt: datetime) -> None:
    """Raise if *local_dt* falls in a DST spring-forward gap.

    Python constructs such datetimes silently but they don't
    round-trip through UTC. A reminder at a non-existent local time is
    almost always a user error — better to raise than to silently shift
    it by an hour.

    Comparison is done on *naive* wall-clock components explicitly:
    aware-datetime ``__eq__`` for same-``tzinfo`` operands ignores
    ``utcoffset()`` and fold on some CPython versions, which would hide
    gaps in zones whose DST shift is not a full hour.
    """
    tz = local_dt.tzinfo
    if tz is None:
        raise ValueError("local_dt must be timezone-aware")
    back = local_dt.astimezone(timezone.utc).astimezone(tz)
    if back.replace(tzinfo=None) != local_dt.replace(tzinfo=None):
        raise ValueError(
            f"Local time {local_dt.strftime('%Y-%m-%d %H:%M')} does not exist "
            f"in timezone {tz} (DST gap)"
        )


def parse_user_datetime(raw: str, *, _now: datetime | None = None) -> datetime:
    """Parse a user-facing datetime string into aware UTC.

    Supported formats (all minute-precision):

    - ``"HH:MM"`` / ``"H:MM"`` — today in the global TZ; if the time
      already passed today, rolls to tomorrow. This is the reminder
      shortcut.
    - ``"YYYY-MM-DD HH:MM"`` / ``"YYYY-MM-DDTHH:MM"`` — specific local
      moment in the global TZ. Date fields require the full width
      (``2026-05-01``, not ``2026-5-1``).
    - ISO 8601 with explicit offset (e.g. ``"2026-05-01T09:00+02:00"``
      or trailing ``Z``) — honored verbatim and then truncated to
      minute precision so round-trip via :func:`format_for_user` is
      lossless.

    DST handling:
      - Non-existent local times (spring-forward gap) raise
        ``ValueError`` with the computed date so the user can tell
        whether the gap is today or tomorrow.
      - Ambiguous local times (fall-back) resolve to ``fold=0`` — the
        first (earlier UTC) occurrence. Rationale: falling back to the
        later one would fire a reminder one hour late; firing early is
        the safer default for user-set reminders.

    The ``_now`` kwarg is for tests; production callers omit it. If
    provided, it must be timezone-aware.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Empty datetime string")
    s = raw.strip()

    # Offset-bearing inputs are routed here before _FULL_RE.match so that
    # "YYYY-MM-DD HH:MM±offset" is treated as an explicit-offset input,
    # not reinterpreted in the global TZ. Order is load-bearing.
    if _has_explicit_offset(s):
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError as e:
            raise ValueError(f"Unparseable datetime: {s!r}") from e
        if dt.tzinfo is None:
            raise ValueError(f"Datetime has no offset: {s!r}")
        return dt.astimezone(timezone.utc).replace(second=0, microsecond=0)

    tz = get_current_tz()
    now = _now if _now is not None else now_utc()
    if now.tzinfo is None:
        raise ValueError("_now must be timezone-aware")

    m = _HHMM_RE.match(s)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        if not (0 <= hh < 24 and 0 <= mm < 60):
            raise ValueError(f"HH:MM out of range: {s!r}")
        now_local = now.astimezone(tz)
        target = now_local.date()
        candidate = datetime(target.year, target.month, target.day, hh, mm, tzinfo=tz)
        if candidate.astimezone(timezone.utc) <= now:
            target = target + timedelta(days=1)
            candidate = datetime(
                target.year, target.month, target.day, hh, mm, tzinfo=tz
            )
        try:
            _check_not_in_dst_gap(candidate)
        except ValueError as e:
            raise ValueError(
                f"{e} — try a time after the clock jumps forward"
            ) from e
        return candidate.astimezone(timezone.utc)

    m = _FULL_RE.match(s)
    if m:
        y, mo, d, hh, mm = (int(m.group(i)) for i in (1, 2, 3, 4, 5))
        try:
            local = datetime(y, mo, d, hh, mm, tzinfo=tz)
        except ValueError as e:
            raise ValueError(f"Bad date components in {s!r}: {e}") from e
        _check_not_in_dst_gap(local)
        return local.astimezone(timezone.utc)

    raise ValueError(f"Unrecognized datetime format: {s!r}")
