"""seed-default-topics CLI command (B4) + audit untagged-topics (B1)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import pytest

from familia.cli import graph_admin


# ---- shared fake-store fixture (mirrors test_cli_graph_admin) -------------

@pytest.fixture
def fake_store():
    store: dict[str, dict[str, Any]] = {
        graph_admin.FAMILY_KEY: {"nodes": [], "edges": [], "updated_at_ms": 0},
        graph_admin.TOPICS_KEY: {"nodes": [], "edges": [], "updated_at_ms": 0},
    }
    save_calls: list[tuple[str, dict]] = []

    def fake_load(key: str, *, api_key=None) -> dict[str, Any]:
        return json.loads(json.dumps(store.get(key, {"nodes": [], "edges": []})))

    def fake_set(key: str, value: Any, *, api_key=None) -> None:
        store[key] = json.loads(json.dumps(value))
        save_calls.append((key, store[key]))

    with patch.object(graph_admin, "load_graph_value", side_effect=fake_load), \
         patch.object(graph_admin, "set_raw", side_effect=fake_set):
        yield store, save_calls


def _run(argv: list[str]) -> int:
    return graph_admin.main(argv)


# ---- B4: seed-default-topics ---------------------------------------------

def test_seed_defaults_dry_run_persists_nothing(fake_store):
    store, _ = fake_store
    rc = _run(["migrate", "seed-default-topics"])  # default = dry-run
    assert rc == 0
    assert store[graph_admin.TOPICS_KEY]["nodes"] == []


def test_seed_defaults_apply_creates_all_known_ids(fake_store):
    store, _ = fake_store
    store[graph_admin.FAMILY_KEY]["nodes"] = [
        {"id": "owner", "type": "principal"},
        {"id": "member_a", "type": "principal"},
    ]
    rc = _run(["migrate", "seed-default-topics", "--apply"])
    assert rc == 0
    ids = {n["id"] for n in store[graph_admin.TOPICS_KEY]["nodes"]}
    assert {"school", "finance", "health", "work", "travel",
            "home", "holidays", "shopping"} == ids


def test_seed_defaults_connects_to_each_principal(fake_store):
    store, _ = fake_store
    store[graph_admin.FAMILY_KEY]["nodes"] = [
        {"id": "owner", "type": "principal"},
        {"id": "member_a", "type": "principal"},
    ]
    rc = _run(["migrate", "seed-default-topics", "--apply"])
    assert rc == 0
    edges = store[graph_admin.TOPICS_KEY]["edges"]
    # 8 topics × 2 principals = 16 concerns edges
    assert len(edges) == 16
    for e in edges:
        assert e["rel"] == "concerns"
        assert e["concerns_as"] == "guardian_of"
        assert e["to"] in {"owner", "member_a"}


def test_seed_defaults_idempotent(fake_store):
    store, _ = fake_store
    store[graph_admin.FAMILY_KEY]["nodes"] = [{"id": "owner", "type": "principal"}]
    _run(["migrate", "seed-default-topics", "--apply"])
    edges_after_first = list(store[graph_admin.TOPICS_KEY]["edges"])
    rc = _run(["migrate", "seed-default-topics", "--apply"])
    assert rc == 0
    # No duplicates: edges count unchanged
    assert store[graph_admin.TOPICS_KEY]["edges"] == edges_after_first


def test_seed_defaults_skips_id_already_in_principals(fake_store):
    """SR-8: if a principal happens to be named 'school', skip the seed."""
    store, _ = fake_store
    store[graph_admin.FAMILY_KEY]["nodes"] = [
        {"id": "school", "type": "principal"},  # collision
        {"id": "owner", "type": "principal"},
    ]
    rc = _run(["migrate", "seed-default-topics", "--apply"])
    assert rc == 0
    ids = {n["id"] for n in store[graph_admin.TOPICS_KEY]["nodes"]}
    assert "school" not in ids  # skipped due to collision
    assert "finance" in ids  # others created


# ---- B1: audit untagged-topics --------------------------------------------

@pytest.fixture
def audit_file(tmp_path, monkeypatch):
    p = tmp_path / "audit.jsonl"
    monkeypatch.setenv("FAMILIA_AUDIT_FILE", str(p))
    return p


def _audit_line(**fields) -> str:
    return json.dumps(fields, ensure_ascii=False) + "\n"


def test_untagged_topics_no_audit_file_returns_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FAMILIA_AUDIT_FILE", str(tmp_path / "missing.jsonl"))
    rc = _run(["audit", "untagged-topics"])
    assert rc == 1


def test_untagged_topics_empty_audit(audit_file, fake_store, capsys):
    audit_file.touch()
    rc = _run(["audit", "untagged-topics"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "no untagged-topic candidates" in out


def test_untagged_topics_surfaces_frequent_word(audit_file, fake_store, capsys):
    """Word 'тренировка' appears 3 times in untagged records, not in vocab —
    should rank near the top."""
    now = datetime.now(timezone.utc).isoformat()
    lines = [
        _audit_line(ts=now, kind="tool_call", tool="cron",
                    args_preview='{"action": "add", "message": "тренировка вечером"}'),
        _audit_line(ts=now, kind="tool_call", tool="cron",
                    args_preview='{"action": "add", "message": "тренировка завтра"}'),
        _audit_line(ts=now, kind="tool_call", tool="memory_set",
                    args_preview='{"value": "тренировка прошла"}'),
    ]
    audit_file.write_text("".join(lines), encoding="utf-8")
    rc = _run(["audit", "untagged-topics"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "тренировка" in out
    assert "Frequent words" in out


def test_untagged_topics_skips_already_tagged(audit_file, fake_store, capsys):
    now = datetime.now(timezone.utc).isoformat()
    lines = [
        _audit_line(ts=now, kind="tool_call", tool="memory_set",
                    args_preview='{"value": "тренировка", "tags": ["work"]}'),
    ]
    audit_file.write_text("".join(lines), encoding="utf-8")
    rc = _run(["audit", "untagged-topics"])
    assert rc == 0
    out = capsys.readouterr().out
    # tagged record should be excluded — no тренировка surface
    assert "тренировка" not in out


def test_untagged_topics_filters_vocab_words(audit_file, fake_store, capsys):
    """A word that's already a topic alias should NOT appear as a candidate
    entry (the sample text in the right-hand column may still show it —
    that's the raw audit-args-preview, not a candidate)."""
    store, _ = fake_store
    store[graph_admin.TOPICS_KEY]["nodes"] = [
        {"id": "fitness", "type": "topic", "kind": "abstract",
         "display_name": "Тренировки", "aliases": ["тренировка"]},
    ]
    now = datetime.now(timezone.utc).isoformat()
    lines = [
        _audit_line(ts=now, kind="tool_call", tool="cron",
                    args_preview='{"message": "тренировка завтра"}'),
    ]
    audit_file.write_text("".join(lines), encoding="utf-8")
    rc = _run(["audit", "untagged-topics"])
    assert rc == 0
    out = capsys.readouterr().out
    # No candidate row begins with тренировка (after the count). Sample text
    # may include it, that's fine.
    candidate_lines = [
        line for line in out.splitlines()
        if line.lstrip().startswith(tuple("0123456789"))
    ]
    for line in candidate_lines:
        # split: "  3  word     ← sample"
        parts = line.split("←", 1)
        head = parts[0].strip()  # "3 word"
        candidate_word = head.split()[1] if len(head.split()) >= 2 else ""
        assert candidate_word != "тренировка"


def test_untagged_topics_skips_old_records(audit_file, fake_store, capsys):
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    lines = [
        _audit_line(ts=old, kind="tool_call", tool="cron",
                    args_preview='{"message": "велогонка"}'),
    ]
    audit_file.write_text("".join(lines), encoding="utf-8")
    rc = _run(["audit", "untagged-topics", "--since", "7d"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "велогонка" not in out


def test_untagged_topics_skips_stopwords_and_short_words(audit_file, fake_store, capsys):
    now = datetime.now(timezone.utc).isoformat()
    lines = [
        _audit_line(ts=now, kind="tool_call", tool="cron",
                    args_preview='{"message": "напомни мне в 7 это надо завтра"}'),
    ]
    audit_file.write_text("".join(lines), encoding="utf-8")
    rc = _run(["audit", "untagged-topics"])
    assert rc == 0
    out = capsys.readouterr().out
    # Stopwords must not appear AS CANDIDATES (in the count-prefixed list).
    # The sample column may show the original message; that's the source.
    candidate_lines = [
        line for line in out.splitlines()
        if line.lstrip().startswith(tuple("0123456789"))
    ]
    candidate_words = []
    for line in candidate_lines:
        parts = line.split("←", 1)
        head = parts[0].strip()
        if len(head.split()) >= 2:
            candidate_words.append(head.split()[1])
    for word in ("напомни", "надо", "завтра", "мне"):
        assert word not in candidate_words
