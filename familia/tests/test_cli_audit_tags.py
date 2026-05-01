"""`familia audit tags` slice tests (A5)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from familia.cli import graph_admin


@pytest.fixture
def audit_file(tmp_path, monkeypatch):
    p = tmp_path / "audit.jsonl"
    monkeypatch.setenv("FAMILIA_AUDIT_FILE", str(p))
    return p


def _line(**fields) -> str:
    return json.dumps(fields, ensure_ascii=False) + "\n"


def test_audit_tags_no_file_returns_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FAMILIA_AUDIT_FILE", str(tmp_path / "missing.jsonl"))
    rc = graph_admin.main(["audit", "tags"])
    assert rc == 1


def test_audit_tags_empty_file_zero_counts(audit_file, capsys):
    audit_file.touch()
    rc = graph_admin.main(["audit", "tags"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "tag_acl_decision events: 0" in out
    assert "graph_edit events:       0" in out


def test_audit_tags_filters_outside_window(audit_file, capsys):
    now = datetime.now(timezone.utc)
    old_ts = (now - timedelta(days=30)).isoformat()
    recent_ts = (now - timedelta(hours=1)).isoformat()
    audit_file.write_text(
        _line(ts=old_ts, kind="tag_acl_decision",
              actor="member_a", op="write",
              full_key="shared:x", record_tags=["varya"], decision="deny")
        + _line(ts=recent_ts, kind="tag_acl_decision",
                actor="member_a", op="write",
                full_key="shared:y", record_tags=["school"], decision="allow"),
        encoding="utf-8",
    )
    rc = graph_admin.main(["audit", "tags", "--since", "7d"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "tag_acl_decision events: 1" in out
    # Old denial filtered out
    assert "varya" not in out
    # Recent allow shows in tag freq
    assert "school" in out


def test_audit_tags_lists_denials(audit_file, capsys):
    now = datetime.now(timezone.utc).isoformat()
    audit_file.write_text(
        _line(ts=now, kind="tag_acl_decision", actor="nanny", op="write",
              full_key="shared:k1", record_tags=["finance"], decision="deny")
        + _line(ts=now, kind="tag_acl_decision", actor="member_a", op="read",
                full_key="shared:k2", record_tags=["varya"], decision="allow"),
        encoding="utf-8",
    )
    rc = graph_admin.main(["audit", "tags"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Denials (1):" in out
    assert "nanny" in out
    assert "finance" in out


def test_audit_tags_lists_graph_edits(audit_file, capsys):
    now = datetime.now(timezone.utc).isoformat()
    audit_file.write_text(
        _line(ts=now, kind="graph_edit", op="topic_add_node",
              target_key="shared:topics.graph", node_id="school",
              cli_user="root"),
        encoding="utf-8",
    )
    rc = graph_admin.main(["audit", "tags"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "graph_edit events:       1" in out
    assert "Recent graph edits" in out
    assert "school" not in out  # node_id not in summary line, only target_key
    assert "shared:topics.graph" in out


def test_audit_tags_top_tags_aggregation(audit_file, capsys):
    now = datetime.now(timezone.utc).isoformat()
    lines = [
        _line(ts=now, kind="tag_acl_decision", actor="m", op="write",
              full_key="k", record_tags=["varya", "school"], decision="allow"),
        _line(ts=now, kind="tag_acl_decision", actor="m", op="write",
              full_key="k", record_tags=["varya"], decision="allow"),
        _line(ts=now, kind="tag_acl_decision", actor="m", op="read",
              full_key="k", record_tags=["finance"], decision="allow"),
    ]
    audit_file.write_text("".join(lines), encoding="utf-8")
    rc = graph_admin.main(["audit", "tags"])
    assert rc == 0
    out = capsys.readouterr().out
    # varya should rank highest (2 writes), then finance/school (1 each).
    assert "varya" in out
    # Order check: varya line above finance/school
    varya_idx = out.find("varya")
    finance_idx = out.find("finance")
    assert varya_idx >= 0
    assert finance_idx == -1 or varya_idx < finance_idx
