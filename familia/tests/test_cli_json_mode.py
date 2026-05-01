"""S0: ``--json`` contract for admin-panel consumption.

Every JSON-mode output carries ``schema_version`` so the panel can detect
CLI drift. Errors go to stderr as single-line JSON with ``code``. Tests
parse the output as JSON to lock the contract — typo in field name will
break the parse.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import patch

import pytest

from familia.cli import graph_admin


@pytest.fixture
def fake_store():
    store: dict[str, dict[str, Any]] = {
        graph_admin.FAMILY_KEY: {
            "nodes": [{"id": "owner", "type": "principal"}],
            "edges": [],
            "updated_at_ms": 100,
        },
        graph_admin.TOPICS_KEY: {
            "nodes": [{"id": "school", "type": "topic", "kind": "abstract"}],
            "edges": [],
            "updated_at_ms": 200,
        },
    }

    def fake_load(key: str, *, api_key=None) -> dict[str, Any]:
        return json.loads(json.dumps(store.get(key, {"nodes": [], "edges": []})))

    def fake_set(key: str, value: Any, *, api_key=None) -> None:
        store[key] = json.loads(json.dumps(value))

    with patch.object(graph_admin, "load_graph_value", side_effect=fake_load), \
         patch.object(graph_admin, "set_raw", side_effect=fake_set):
        yield store


def _capture_json(capsys, argv: list[str]) -> dict[str, Any]:
    rc = graph_admin.main(argv)
    out = capsys.readouterr().out
    assert rc == 0, f"non-zero rc={rc}, stderr in test"
    # NDJSON-aware: take the first line (header / single-line payload).
    line = out.splitlines()[0] if out else ""
    return json.loads(line)


# ---- version --------------------------------------------------------------

def test_version_json(capsys, fake_store):
    payload = _capture_json(capsys, ["version", "--json"])
    assert payload["schema_version"] == 1
    assert "version" in payload


def test_version_human_no_json_flag(capsys, fake_store):
    rc = graph_admin.main(["version"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "familia " in out


# ---- health ---------------------------------------------------------------

def test_health_json_shape(capsys, fake_store):
    payload = _capture_json(capsys, ["health", "--json"])
    assert payload["schema_version"] == 1
    for key in (
        "version", "memx", "principals_count",
        "topics_count", "family_nodes", "topics_nodes",
    ):
        assert key in payload, f"missing {key}"
    assert payload["family_nodes"] == 1
    assert payload["topics_nodes"] == 1


def test_health_memx_failure_yields_error_status(capsys, monkeypatch):
    """SR-10 fail-closed: graph_io error doesn't crash; reports memx=error."""
    from familia.acl.graph_io import GraphIOError

    def boom(*a, **k):
        raise GraphIOError("memx unreachable: simulated")

    monkeypatch.setattr(graph_admin, "load_graph_value", boom)
    payload = _capture_json(capsys, ["health", "--json"])
    assert payload["memx"].startswith("error:")


# ---- graph print ----------------------------------------------------------

def test_graph_print_json(capsys, fake_store):
    payload = _capture_json(capsys, ["graph", "print", "--json"])
    assert payload["schema_version"] == 1
    assert "family" in payload
    assert "topics" in payload
    assert payload["family"]["nodes"][0]["id"] == "owner"
    assert payload["topics"]["nodes"][0]["id"] == "school"
    assert payload["family"]["updated_at_ms"] == 100
    assert payload["topics"]["updated_at_ms"] == 200


# ---- audit tail (NDJSON) --------------------------------------------------

@pytest.fixture
def audit_file(tmp_path, monkeypatch):
    p = tmp_path / "audit.jsonl"
    monkeypatch.setenv("FAMILIA_AUDIT_FILE", str(p))
    return p


def test_audit_tail_json_emits_ndjson(audit_file, capsys):
    now = datetime.now(timezone.utc).isoformat()
    lines = [
        json.dumps({"ts": now, "kind": "policy", "actor": "owner",
                    "action": "memory.read", "decision": "allow"})
        + "\n",
        json.dumps({"ts": now, "kind": "tag_acl_decision", "actor": "member_a",
                    "decision": "deny"})
        + "\n",
    ]
    audit_file.write_text("".join(lines), encoding="utf-8")
    rc = graph_admin.main(["audit", "tail", "--json", "--limit", "10"])
    assert rc == 0
    out = capsys.readouterr().out
    parsed_lines = [json.loads(line) for line in out.splitlines() if line]
    # First line is the header, then events
    assert parsed_lines[0].get("header") is True
    assert parsed_lines[0]["count"] == 2
    assert parsed_lines[0]["schema_version"] == 1
    assert len(parsed_lines) == 1 + 2
    assert parsed_lines[1]["kind"] == "policy"


def test_audit_tail_missing_file_emits_error_json(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FAMILIA_AUDIT_FILE", str(tmp_path / "missing.jsonl"))
    rc = graph_admin.main(["audit", "tail", "--json"])
    assert rc == 1
    err = capsys.readouterr().err.strip()
    payload = json.loads(err)
    assert payload["schema_version"] == 1
    assert payload["code"] == "AUDIT_FILE_MISSING"


def test_audit_tail_filters_by_since(audit_file, capsys):
    old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    new_ts = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    audit_file.write_text(
        json.dumps({"ts": old_ts, "kind": "x"}) + "\n"
        + json.dumps({"ts": new_ts, "kind": "y"}) + "\n",
        encoding="utf-8",
    )
    rc = graph_admin.main(["audit", "tail", "--json", "--since", "1h"])
    assert rc == 0
    out = capsys.readouterr().out
    parsed = [json.loads(line) for line in out.splitlines() if line]
    # Header + 1 event
    assert parsed[0]["count"] == 1
    assert len(parsed) == 2
    assert parsed[1]["kind"] == "y"


# ---- audit tags --json ----------------------------------------------------

def test_audit_tags_json_shape(audit_file, capsys, fake_store):
    now = datetime.now(timezone.utc).isoformat()
    audit_file.write_text(
        json.dumps({"ts": now, "kind": "tag_acl_decision",
                    "actor": "member_a", "op": "write",
                    "full_key": "shared:x",
                    "record_tags": ["varya", "school"],
                    "decision": "allow"}) + "\n"
        + json.dumps({"ts": now, "kind": "tag_acl_decision",
                      "actor": "nanny", "op": "write",
                      "full_key": "shared:y",
                      "record_tags": ["finance"],
                      "decision": "deny"}) + "\n"
        + json.dumps({"ts": now, "kind": "graph_edit",
                      "op": "topic_add_node", "target_key": "shared:topics.graph",
                      "cli_user": "root"}) + "\n",
        encoding="utf-8",
    )
    payload = _capture_json(capsys, ["audit", "tags", "--json"])
    assert payload["schema_version"] == 1
    assert payload["decisions_count"] == 2
    assert payload["edits_count"] == 1
    # tag frequency
    tag_freqs = {tf["tag"]: tf["count"] for tf in payload["tag_frequency"]}
    assert tag_freqs.get("varya") == 1
    assert tag_freqs.get("finance") == 1
    # denials surfaced
    assert any(e["actor"] == "nanny" for e in payload["denials"])


# ---- migrations json (dry-run) -------------------------------------------

def test_seed_default_topics_json_dry_run(capsys, fake_store):
    fake_store[graph_admin.FAMILY_KEY]["nodes"].append(
        {"id": "member_a", "type": "principal"}
    )
    payload = _capture_json(
        capsys, ["migrate", "seed-default-topics", "--json"]
    )
    assert payload["schema_version"] == 1
    assert payload["dry_run"] is True
    assert isinstance(payload["new_topics"], list)
    # School already exists in fake_store, so should be filtered out
    new_ids = {t["id"] for t in payload["new_topics"]}
    assert "school" not in new_ids
    assert "finance" in new_ids


def test_topic_to_principal_json_dry_run(capsys, fake_store):
    fake_store[graph_admin.TOPICS_KEY]["nodes"].append(
        {"id": "kid", "type": "topic", "kind": "person"}
    )
    fake_store[graph_admin.TOPICS_KEY]["edges"].append(
        {"from": "kid", "to": "owner", "rel": "concerns",
         "concerns_as": "parent_of"}
    )
    payload = _capture_json(
        capsys, ["migrate", "topic-to-principal", "kid", "--json"]
    )
    assert payload["dry_run"] is True
    assert payload["topic"] == "kid"
    assert payload["role"] == "child"
    assert payload["translated_edges"] == 1


# ---- error → JSON on stderr ----------------------------------------------

def test_graph_io_error_yields_json_on_stderr(capsys, monkeypatch):
    from familia.acl.graph_io import GraphIOError

    def boom(*a, **k):
        raise GraphIOError("simulated boom")

    monkeypatch.setattr(graph_admin, "load_graph_value", boom)
    rc = graph_admin.main(["graph", "print", "--json"])
    assert rc == 2
    err = capsys.readouterr().err.strip()
    assert err
    payload = json.loads(err)
    assert payload["schema_version"] == 1
    assert payload["code"] == "GRAPH_IO_ERROR"
    assert "simulated boom" in payload["error"]
