"""SR-6: audit.jsonl is chmod 0600 on first write (POSIX-only)."""

from __future__ import annotations

import os
import stat

import pytest

from familia import audit


@pytest.mark.skipif(os.name != "posix", reason="POSIX-only mode test")
def test_audit_file_gets_0600_on_first_write(tmp_path, monkeypatch):
    p = tmp_path / "audit.jsonl"
    monkeypatch.setenv("FAMILIA_AUDIT_FILE", str(p))
    audit._chmod_done.clear()

    audit.log_event("test_event", x=1)
    assert p.is_file()
    mode = stat.S_IMODE(p.stat().st_mode)
    assert mode == 0o600


def test_chmod_cache_per_path(tmp_path, monkeypatch):
    """Track-by-absolute-path so different test files don't skip each other."""
    p = tmp_path / "a.jsonl"
    q = tmp_path / "b.jsonl"
    audit._chmod_done.clear()

    monkeypatch.setenv("FAMILIA_AUDIT_FILE", str(p))
    audit.log_event("e", x=1)
    monkeypatch.setenv("FAMILIA_AUDIT_FILE", str(q))
    audit.log_event("e", x=1)

    assert str(p.resolve()) in audit._chmod_done
    assert str(q.resolve()) in audit._chmod_done
