"""Tests for ``familia.pending.store`` covering SR-12: cap=100 overflow,
24h reject cooldown, idempotent record updates, approve/reject flow.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from familia.pending import store as pending_store


@pytest.fixture
def pending_file(tmp_path, monkeypatch):
    """Point the store at a tmp file and reset module-level caches."""
    p = tmp_path / "pending_principals.json"
    monkeypatch.setenv("FAMILIA_PENDING_FILE", str(p))
    yield p


def test_record_creates_row(pending_file):
    e = pending_store.record(
        channel="vk", sender_id="42",
        display_name="Boris", message_preview="hi",
    )
    assert e is not None
    assert e.channel == "vk" and e.sender_id == "42"
    assert e.display_name == "Boris"
    assert e.msg_count == 1


def test_record_updates_existing_row(pending_file):
    pending_store.record(channel="vk", sender_id="42",
                         display_name="Boris", message_preview="hi")
    e = pending_store.record(channel="vk", sender_id="42",
                             display_name="Boris", message_preview="again")
    assert e.msg_count == 2
    assert e.last_message_preview == "again"
    # File contains exactly one row.
    data = json.loads(pending_file.read_text(encoding="utf-8"))
    assert len(data["entries"]) == 1


def test_record_returns_none_on_cap(pending_file):
    """101st distinct sender hits the cap=100 limit and is silently dropped."""
    for i in range(pending_store.PENDING_CAP):
        pending_store.record(channel="vk", sender_id=str(i),
                             display_name=f"u{i}", message_preview="x")
    overflow = pending_store.record(
        channel="vk", sender_id="extra",
        display_name="extra", message_preview="x",
    )
    assert overflow is None
    assert len(pending_store.list_all()) == pending_store.PENDING_CAP


def test_reject_silences_for_cooldown(pending_file):
    pending_store.record(channel="vk", sender_id="42",
                         display_name="X", message_preview="hi")
    pending_store.reject("vk", "42")
    again = pending_store.record(channel="vk", sender_id="42",
                                 display_name="X", message_preview="hi again")
    assert again is None
    assert pending_store.is_rejected("vk", "42") is True


def test_reject_cooldown_expires(pending_file, monkeypatch):
    pending_store.record(channel="vk", sender_id="42",
                         display_name="X", message_preview="hi")
    pending_store.reject("vk", "42")
    # Pretend the cooldown elapsed.
    state = pending_store._load(pending_file)
    state.rejected[pending_store._key("vk", "42")] = (
        int(time.time()) - pending_store.REJECT_COOLDOWN_SECS - 1
    )
    pending_store._save(state, pending_file)
    e = pending_store.record(channel="vk", sender_id="42",
                             display_name="X", message_preview="hi again")
    assert e is not None
    assert e.msg_count == 1


def test_approve_drops_row(pending_file):
    pending_store.record(channel="vk", sender_id="42",
                         display_name="X", message_preview="hi")
    approved = pending_store.approve("vk", "42")
    assert approved.sender_id == "42"
    assert pending_store.list_all() == []


def test_approve_unknown_raises(pending_file):
    with pytest.raises(KeyError):
        pending_store.approve("vk", "no-such-sender")
