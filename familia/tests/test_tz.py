"""Tests for familia.tz — single-TZ model, parsing, and admin tool.

Covers:
- Source-of-truth priority: state file → env → UTC fallback.
- :func:`set_current_tz` validation + atomic write.
- :func:`parse_user_datetime`: HH:MM (today/tomorrow), full datetime,
  ISO-with-offset, DST spring-forward gap, DST fall-back ambiguity,
  bad input.
- Reminder scenarios: UTC stability across TZ changes, midnight
  rollover, DST-day behavior.
- :class:`AdminSetTzTool`: admin-only gate, invalid tz, audit event.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from familia.policy import Decision
from familia.policy.engine import PolicyEngine, PolicyRule
from familia.principals import Principal, PrincipalRegistry, set_current_actor
from familia.roles import set_effective_roles_for_tests
from familia.tools.admin import AdminSetTzTool
from familia.tz import (
    current_tz_name,
    format_for_user,
    get_current_tz,
    now_utc,
    parse_user_datetime,
    set_current_tz,
    to_user_tz,
)


@pytest.fixture
def tz_state_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the TZ state file into a tmp path and clear env default."""
    p = tmp_path / "familia_tz"
    monkeypatch.setenv("FAMILIA_TZ_STATE_FILE", str(p))
    monkeypatch.delenv("FAMILIA_TZ", raising=False)
    return p


# ---------- source-of-truth resolution ----------

def test_fallback_utc_when_no_env_no_file(tz_state_file: Path) -> None:
    assert current_tz_name() == "UTC"
    assert get_current_tz() == ZoneInfo("UTC")


def test_env_default(tz_state_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAMILIA_TZ", "Europe/Moscow")
    assert current_tz_name() == "Europe/Moscow"
    assert get_current_tz() == ZoneInfo("Europe/Moscow")


def test_file_overrides_env(
    tz_state_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAMILIA_TZ", "Europe/Moscow")
    tz_state_file.write_text("America/New_York")
    assert current_tz_name() == "America/New_York"


def test_empty_state_file_falls_back_to_env(
    tz_state_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAMILIA_TZ", "Asia/Tokyo")
    tz_state_file.write_text("   \n")
    assert current_tz_name() == "Asia/Tokyo"


def test_bad_env_falls_back_to_utc(
    tz_state_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FAMILIA_TZ", "Mars/Olympus_Mons")
    assert current_tz_name() == "UTC"


def test_bad_file_falls_back_to_utc(tz_state_file: Path) -> None:
    tz_state_file.write_text("Not/A_Zone")
    assert current_tz_name() == "UTC"


# ---------- set_current_tz ----------

def test_set_current_tz_writes_file(tz_state_file: Path) -> None:
    set_current_tz("Asia/Tokyo")
    assert tz_state_file.read_text() == "Asia/Tokyo"
    assert current_tz_name() == "Asia/Tokyo"


def test_set_current_tz_creates_parent_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nested = tmp_path / "nested" / "deep" / "familia_tz"
    monkeypatch.setenv("FAMILIA_TZ_STATE_FILE", str(nested))
    monkeypatch.delenv("FAMILIA_TZ", raising=False)
    set_current_tz("Europe/Berlin")
    assert nested.read_text() == "Europe/Berlin"


def test_set_invalid_tz_raises_and_no_write(tz_state_file: Path) -> None:
    with pytest.raises(ValueError, match="Unknown IANA timezone"):
        set_current_tz("Bad/Zone")
    assert not tz_state_file.exists()


def test_set_current_tz_overwrites(tz_state_file: Path) -> None:
    set_current_tz("Europe/Berlin")
    set_current_tz("Asia/Tokyo")
    assert tz_state_file.read_text() == "Asia/Tokyo"


def test_set_current_tz_no_tmpfile_leak_on_replace_failure(
    tz_state_file: Path,
) -> None:
    """If os.replace fails (cross-device, locked dest on Windows), the
    temp file must be cleaned up — otherwise the state directory
    accumulates stray .tz.* files on every failed admin_set_tz call.
    """
    with patch("familia.tz.os.replace", side_effect=OSError("rename failed")):
        with pytest.raises(OSError):
            set_current_tz("Asia/Tokyo")
    strays = list(tz_state_file.parent.glob(tz_state_file.name + ".*"))
    assert strays == [], f"leaked tempfiles: {strays}"


# ---------- now / format helpers ----------

def test_now_utc_is_aware() -> None:
    n = now_utc()
    assert n.tzinfo is not None
    assert n.utcoffset() == timezone.utc.utcoffset(n)


def test_format_for_user_moscow(tz_state_file: Path) -> None:
    set_current_tz("Europe/Moscow")
    dt = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert format_for_user(dt) == "2026-01-01 15:00"


def test_to_user_tz_rejects_naive(tz_state_file: Path) -> None:
    with pytest.raises(ValueError, match="aware"):
        to_user_tz(datetime(2026, 1, 1, 12, 0))


# ---------- parse_user_datetime: HH:MM ----------

def test_parse_hhmm_future_today(tz_state_file: Path) -> None:
    set_current_tz("Europe/Moscow")  # +03
    now = datetime(2026, 5, 15, 7, 0, tzinfo=timezone.utc)  # 10:00 Moscow
    result = parse_user_datetime("18:00", _now=now)
    assert result == datetime(2026, 5, 15, 15, 0, tzinfo=timezone.utc)


def test_parse_hhmm_past_rolls_tomorrow(tz_state_file: Path) -> None:
    set_current_tz("Europe/Moscow")
    now = datetime(2026, 5, 15, 18, 0, tzinfo=timezone.utc)  # 21:00 Moscow
    result = parse_user_datetime("09:00", _now=now)
    assert result == datetime(2026, 5, 16, 6, 0, tzinfo=timezone.utc)


def test_parse_hhmm_exact_now_rolls_tomorrow(tz_state_file: Path) -> None:
    """Boundary: time == now → tomorrow, not today (avoid 0-duration reminder)."""
    set_current_tz("Europe/Moscow")
    now = datetime(2026, 5, 15, 6, 0, tzinfo=timezone.utc)  # 09:00 Moscow
    result = parse_user_datetime("09:00", _now=now)
    assert result == datetime(2026, 5, 16, 6, 0, tzinfo=timezone.utc)


def test_parse_hhmm_single_digit_hour(tz_state_file: Path) -> None:
    set_current_tz("Europe/Moscow")
    now = datetime(2026, 5, 15, 0, 0, tzinfo=timezone.utc)  # 03:00 Moscow
    result = parse_user_datetime("9:30", _now=now)
    assert result == datetime(2026, 5, 15, 6, 30, tzinfo=timezone.utc)


def test_parse_hhmm_out_of_range(tz_state_file: Path) -> None:
    with pytest.raises(ValueError):
        parse_user_datetime("25:00")
    with pytest.raises(ValueError):
        parse_user_datetime("12:99")


# ---------- parse_user_datetime: YYYY-MM-DD HH:MM ----------

def test_parse_full_datetime_space(tz_state_file: Path) -> None:
    set_current_tz("Europe/Moscow")
    result = parse_user_datetime("2026-05-01 09:00")
    assert result == datetime(2026, 5, 1, 6, 0, tzinfo=timezone.utc)


def test_parse_full_datetime_T(tz_state_file: Path) -> None:
    set_current_tz("Europe/Moscow")
    result = parse_user_datetime("2026-05-01T09:00")
    assert result == datetime(2026, 5, 1, 6, 0, tzinfo=timezone.utc)


def test_parse_full_datetime_rejects_seconds(tz_state_file: Path) -> None:
    """Minute precision only — seconds in local-format input must be rejected."""
    set_current_tz("Europe/Moscow")
    with pytest.raises(ValueError):
        parse_user_datetime("2026-05-01 09:00:30")


def test_parse_full_datetime_rejects_single_digit_month(tz_state_file: Path) -> None:
    """Date fields require full width: 2026-05-01, not 2026-5-1."""
    set_current_tz("Europe/Moscow")
    with pytest.raises(ValueError):
        parse_user_datetime("2026-5-01 09:00")


def test_parse_full_datetime_bad_day(tz_state_file: Path) -> None:
    set_current_tz("Europe/Moscow")
    with pytest.raises(ValueError):
        parse_user_datetime("2026-02-30 09:00")


# ---------- parse_user_datetime: ISO with offset ----------

def test_parse_iso_with_offset_honored(tz_state_file: Path) -> None:
    """Offset in the string wins over global TZ."""
    set_current_tz("Europe/Moscow")
    result = parse_user_datetime("2026-05-01T09:00+02:00")
    assert result == datetime(2026, 5, 1, 7, 0, tzinfo=timezone.utc)


def test_parse_iso_z(tz_state_file: Path) -> None:
    result = parse_user_datetime("2026-05-01T09:00Z")
    assert result == datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)


def test_parse_iso_negative_offset(tz_state_file: Path) -> None:
    result = parse_user_datetime("2026-05-01T09:00-05:00")
    assert result == datetime(2026, 5, 1, 14, 0, tzinfo=timezone.utc)


def test_parse_iso_compact_offset(tz_state_file: Path) -> None:
    result = parse_user_datetime("2026-05-01T09:00+0200")
    assert result == datetime(2026, 5, 1, 7, 0, tzinfo=timezone.utc)


def test_parse_iso_with_seconds_truncated_to_minute(tz_state_file: Path) -> None:
    """ISO with seconds is accepted but truncated so format_for_user round-trips."""
    result = parse_user_datetime("2026-05-01T09:00:30+00:00")
    assert result == datetime(2026, 5, 1, 9, 0, tzinfo=timezone.utc)


def test_parse_iso_space_separator_with_offset(tz_state_file: Path) -> None:
    """Space separator + explicit offset: accepted, offset honored over global TZ."""
    set_current_tz("Europe/Moscow")
    result = parse_user_datetime("2026-05-01 09:00+03:00")
    assert result == datetime(2026, 5, 1, 6, 0, tzinfo=timezone.utc)


def test_parse_iso_hour_only_rejected(tz_state_file: Path) -> None:
    """Bare-hour ISO like '2026-05-01T09Z' must not silently parse as 09:00."""
    with pytest.raises(ValueError):
        parse_user_datetime("2026-05-01T09Z")
    with pytest.raises(ValueError):
        parse_user_datetime("2026-05-01T09+00:00")


# ---------- DST corner cases ----------

def test_parse_dst_spring_gap_raises(tz_state_file: Path) -> None:
    """Europe/Berlin jumps 02:00→03:00 on 2026-03-29. 02:30 doesn't exist."""
    set_current_tz("Europe/Berlin")
    with pytest.raises(ValueError, match="DST gap"):
        parse_user_datetime("2026-03-29 02:30")


def test_parse_dst_fall_back_ambiguous_picks_first(tz_state_file: Path) -> None:
    """Europe/Berlin falls back 03:00 CEST → 02:00 CET on 2026-10-25.

    02:30 happens twice; fold=0 (first occurrence, CEST, UTC+2) is
    picked by convention — that's 00:30 UTC. fold=1 would be 01:30 UTC
    (CET). Choosing the first occurrence means a reminder fires at the
    earlier possible wall-clock reading, which is the safer default.
    """
    set_current_tz("Europe/Berlin")
    result = parse_user_datetime("2026-10-25 02:30")
    # fold=0 (CEST, UTC+2) = 00:30 UTC. NOT 01:30 UTC (which would be fold=1).
    assert result == datetime(2026, 10, 25, 0, 30, tzinfo=timezone.utc)
    assert result != datetime(2026, 10, 25, 1, 30, tzinfo=timezone.utc)


def test_parse_hhmm_on_dst_gap_day_after_gap_works(tz_state_file: Path) -> None:
    """On DST-day, a time after the jump parses cleanly."""
    set_current_tz("Europe/Berlin")
    # 2026-03-29, before the 01:00 UTC jump (local 01:00 CET).
    now = datetime(2026, 3, 29, 0, 0, tzinfo=timezone.utc)
    result = parse_user_datetime("10:00", _now=now)
    # 10:00 CEST = 08:00 UTC.
    assert result == datetime(2026, 3, 29, 8, 0, tzinfo=timezone.utc)


def test_parse_hhmm_tomorrow_in_dst_gap_raises(tz_state_file: Path) -> None:
    """User types '02:30' late the night before DST spring-forward.

    Today's 02:30 has already passed; the roll-to-tomorrow lands
    exactly in tomorrow's DST gap. Error must mention the gap and
    hint at the workaround.
    """
    set_current_tz("Europe/Berlin")
    # 2026-03-28 23:00 CET = 22:00 UTC. Tomorrow's 02:30 is in the gap.
    now = datetime(2026, 3, 28, 22, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="DST gap"):
        parse_user_datetime("02:30", _now=now)


def test_parse_requires_aware_now(tz_state_file: Path) -> None:
    """_now must be timezone-aware — naive input is a programming error."""
    set_current_tz("Europe/Moscow")
    with pytest.raises(ValueError, match="timezone-aware"):
        parse_user_datetime("10:00", _now=datetime(2026, 5, 15, 12, 0))


# ---------- invalid input ----------

def test_parse_empty_raises(tz_state_file: Path) -> None:
    with pytest.raises(ValueError):
        parse_user_datetime("")


def test_parse_whitespace_raises(tz_state_file: Path) -> None:
    with pytest.raises(ValueError):
        parse_user_datetime("   ")


def test_parse_non_string_raises(tz_state_file: Path) -> None:
    with pytest.raises(ValueError):
        parse_user_datetime(None)  # type: ignore[arg-type]


def test_parse_garbage_raises(tz_state_file: Path) -> None:
    with pytest.raises(ValueError):
        parse_user_datetime("tomorrow at noon")


# ---------- reminder scenarios (the whole point) ----------

def test_reminder_utc_stable_across_tz_change(tz_state_file: Path) -> None:
    """Reminder stored once; admin changes TZ; stored UTC unchanged."""
    set_current_tz("Europe/Moscow")
    now = datetime(2026, 5, 15, 7, 0, tzinfo=timezone.utc)  # 10:00 Moscow
    stored = parse_user_datetime("18:00", _now=now)
    assert stored == datetime(2026, 5, 15, 15, 0, tzinfo=timezone.utc)

    rendered_msk = format_for_user(stored)

    set_current_tz("Europe/Berlin")  # CEST +02 in May
    rendered_ber = format_for_user(stored)

    assert rendered_msk == "2026-05-15 18:00"
    assert rendered_ber == "2026-05-15 17:00"
    # Absolute moment unchanged — same UTC timestamp on both sides.


def test_reminder_crossing_midnight_utc(tz_state_file: Path) -> None:
    """01:00 Moscow tomorrow = 22:00 UTC today."""
    set_current_tz("Europe/Moscow")
    now = datetime(2026, 5, 15, 20, 0, tzinfo=timezone.utc)  # 23:00 Moscow
    stored = parse_user_datetime("01:00", _now=now)
    assert stored == datetime(2026, 5, 15, 22, 0, tzinfo=timezone.utc)


def test_reminder_just_after_midnight_local(tz_state_file: Path) -> None:
    """At 23:30 Moscow, reminder for 00:10 should go tomorrow (21:10 UTC today)."""
    set_current_tz("Europe/Moscow")
    now = datetime(2026, 5, 15, 20, 30, tzinfo=timezone.utc)  # 23:30 Moscow
    stored = parse_user_datetime("00:10", _now=now)
    assert stored == datetime(2026, 5, 15, 21, 10, tzinfo=timezone.utc)


def test_reminder_tz_aware_format_roundtrip(tz_state_file: Path) -> None:
    """Parsing '2026-05-01 09:00' then formatting returns the same string."""
    set_current_tz("Europe/Moscow")
    stored = parse_user_datetime("2026-05-01 09:00")
    assert format_for_user(stored) == "2026-05-01 09:00"


def test_reminder_set_from_utc_tz_presented_correctly(tz_state_file: Path) -> None:
    """Admin sets TZ=UTC; user says 09:00; rendered matches."""
    set_current_tz("UTC")
    now = datetime(2026, 5, 15, 6, 0, tzinfo=timezone.utc)
    stored = parse_user_datetime("09:00", _now=now)
    assert stored == datetime(2026, 5, 15, 9, 0, tzinfo=timezone.utc)
    assert format_for_user(stored) == "2026-05-15 09:00"


# ---------- admin_set_tz tool ----------

@pytest.fixture
def fake_registry():
    reg = PrincipalRegistry([
        Principal(id="owner", display_name="Owner", memx_key="k1", roles=["admin"]),
        Principal(id="alice", display_name="Alice", memx_key="k2"),
    ])
    with patch("familia.roles.get_registry", return_value=reg), \
         patch("familia.tools.admin.get_registry", return_value=reg), \
         patch("familia.principals.get_registry", return_value=reg):
        yield reg


@pytest.fixture
def admin_engine():
    eng = PolicyEngine([
        PolicyRule(
            name="set_tz",
            action=["role.admin.set_tz"],
            actor=["@admin"],
            decision=Decision.ALLOW,
        ),
    ])
    with patch("familia.tools.admin.get_engine", return_value=eng):
        yield eng


@pytest.fixture
def audit_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("FAMILIA_AUDIT_FILE", str(path))
    return path


@pytest.mark.asyncio
async def test_admin_set_tz_admin_ok(
    tz_state_file: Path,
    fake_registry,
    admin_engine,
    audit_file: Path,
) -> None:
    set_effective_roles_for_tests({"owner": {"admin"}})
    set_current_actor("owner")
    tool = AdminSetTzTool()
    result = await tool.execute(tz="Asia/Tokyo")
    assert "Asia/Tokyo" in result
    assert tz_state_file.read_text() == "Asia/Tokyo"
    # audit event emitted
    lines = [
        json.loads(line)
        for line in audit_file.read_text().splitlines()
        if line.strip()
    ]
    assert any(
        r["kind"] == "tz_changed"
        and r["actor"] == "owner"
        and r.get("to_chat") == ""
        and r["extra"]["after"] == "Asia/Tokyo"
        for r in lines
    )


@pytest.mark.asyncio
async def test_admin_set_tz_non_admin_denied(
    tz_state_file: Path,
    fake_registry,
    admin_engine,
    audit_file: Path,
) -> None:
    set_effective_roles_for_tests({"owner": {"admin"}})
    set_current_actor("alice")
    tool = AdminSetTzTool()
    result = await tool.execute(tz="Asia/Tokyo")
    assert result.startswith("Policy denied role.admin.set_tz")
    assert not tz_state_file.exists()


@pytest.mark.asyncio
async def test_admin_set_tz_invalid_tz(
    tz_state_file: Path,
    fake_registry,
    admin_engine,
    audit_file: Path,
) -> None:
    set_effective_roles_for_tests({"owner": {"admin"}})
    set_current_actor("owner")
    tool = AdminSetTzTool()
    result = await tool.execute(tz="Bad/Zone")
    assert result.startswith("Error: Unknown IANA timezone")
    assert not tz_state_file.exists()
    # No audit event — admin tried an invalid zone, nothing happened.
    assert not audit_file.exists() or "tz_changed" not in audit_file.read_text()


@pytest.mark.asyncio
async def test_admin_set_tz_no_actor(
    tz_state_file: Path,
    fake_registry,
    admin_engine,
    audit_file: Path,
) -> None:
    set_current_actor(None)
    tool = AdminSetTzTool()
    result = await tool.execute(tz="Asia/Tokyo")
    assert "no actor in context" in result
    assert not tz_state_file.exists()


@pytest.mark.asyncio
async def test_admin_set_tz_empty_string(
    tz_state_file: Path,
    fake_registry,
    admin_engine,
    audit_file: Path,
) -> None:
    set_effective_roles_for_tests({"owner": {"admin"}})
    set_current_actor("owner")
    tool = AdminSetTzTool()
    result = await tool.execute(tz="   ")
    assert result == "Error: 'tz' is required"
    assert not tz_state_file.exists()
