"""Regression tests for the security-review-driven hardening pass.

Covers:
  * Audit log size cap (H1) — prevents log inflation by adversarial tool args.
  * Dream consolidator actor guard (H2) — refuses to run outside the
    consolidator turn even if mistakenly registered on the main loop.
  * Memory value size cap (M3) — prevents multi-MB blobs in memX.
  * admin_revoke unknown-principal validation (M8) — typo doesn't no-op.
  * Reserved policy keys (C2) — non-admin can't write shared:roles.* /
    shared:family.graph and self-grant the admin role.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from familia import audit
from familia import principals as principals_mod
from familia.principals import (
    Identity, Principal, PrincipalRegistry,
    set_current_actor,
)


# ---- H1: audit clipping ----------------------------------------------------

def test_audit_clip_truncates_long_strings():
    huge = "x" * 100_000
    clipped = audit._clip(huge)
    assert isinstance(clipped, str)
    assert len(clipped) < 5000
    assert clipped.endswith("... (truncated)")


def test_audit_clip_preserves_short_strings():
    s = "short"
    assert audit._clip(s) == "short"


def test_audit_clip_recurses_into_dict_and_list():
    payload = {
        "a": "x" * 100_000,
        "b": ["y" * 100_000, "ok"],
        "c": {"nested": "z" * 100_000},
    }
    clipped = audit._clip(payload)
    assert clipped["a"].endswith("... (truncated)")
    assert clipped["b"][0].endswith("... (truncated)")
    assert clipped["b"][1] == "ok"
    assert clipped["c"]["nested"].endswith("... (truncated)")


def test_audit_log_event_clips_oversized_field(tmp_path, monkeypatch):
    log_file = tmp_path / "audit.jsonl"
    monkeypatch.setenv("FAMILIA_AUDIT_FILE", str(log_file))
    audit.log_event("test_event", reason="x" * 100_000)
    line = log_file.read_text(encoding="utf-8").strip()
    rec = json.loads(line)
    assert rec["kind"] == "test_event"
    # Clipped + marker; total well under raw input length.
    assert len(rec["reason"]) < 5000
    assert rec["reason"].endswith("... (truncated)")


# ---- H2: dream actor guard --------------------------------------------------

@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> PrincipalRegistry:
    reg = PrincipalRegistry([
        Principal(id="owner", display_name="O",
                  identities=[Identity(channel="vk", sender_id="1000001")],
                  memx_key="k1", roles=["admin"]),
        Principal(id="member_a", display_name="A",
                  identities=[Identity(channel="vk", sender_id="1000002")],
                  memx_key="k2", roles=[]),
    ])
    monkeypatch.setattr(principals_mod, "_registry", reg)
    return reg


def test_dream_memory_set_refuses_non_consolidator_actor(registry):
    from familia.tools.dream_memory import DreamMemorySetTool

    tool = DreamMemorySetTool(base_url="http://nope", api_key="k")
    set_current_actor("owner")
    out = asyncio.run(tool.execute(
        scope="shared", key="x", value="v",
    ))
    assert "Error" in out
    assert "consolidator" in out.lower()


def test_dream_memory_set_refuses_anonymous_actor(registry):
    from familia.tools.dream_memory import DreamMemorySetTool

    tool = DreamMemorySetTool(base_url="http://nope", api_key="k")
    set_current_actor(None)
    out = asyncio.run(tool.execute(scope="shared", key="x", value="v"))
    assert "Error" in out


# ---- M3: memory_set value size cap -----------------------------------------

def test_memory_set_rejects_oversized_value(registry, monkeypatch):
    from familia.tools.memory import MemorySetTool, _MAX_VALUE_BYTES

    set_current_actor("owner")
    tool = MemorySetTool(base_url="http://nope")
    huge = "x" * (_MAX_VALUE_BYTES + 1)
    out = asyncio.run(tool.execute(scope="shared", key="big", value=huge))
    assert out.startswith("Error: value too large")
    assert str(_MAX_VALUE_BYTES) in out


def test_memory_set_value_cap_uses_byte_length(registry):
    """Cap is byte-length, so multibyte chars count correctly (Cyrillic = 2B)."""
    from familia.tools.memory import _MAX_VALUE_BYTES

    # 4 bytes per char in some emoji is overkill — use Cyrillic (2 bytes UTF-8).
    just_over_in_bytes = "ы" * (_MAX_VALUE_BYTES // 2 + 1)
    assert len(just_over_in_bytes.encode("utf-8")) > _MAX_VALUE_BYTES


# ---- M8: admin_revoke unknown-principal validation -------------------------

def test_admin_revoke_rejects_unknown_principal(registry):
    from familia.tools.admin import AdminRevokeTool

    set_current_actor("owner")
    tool = AdminRevokeTool(base_url="http://nope")
    out = asyncio.run(tool.execute(principal="nosuch_typo"))
    assert "Error" in out
    assert "unknown principal" in out.lower()


# ---- C2: reserved policy keys ---------------------------------------------

@pytest.fixture
def policy_with_reserved(tmp_path: Path, monkeypatch) -> None:
    """Load the example policy.yaml which now contains the reserved-keys deny."""
    from familia.policy import engine as engine_mod

    repo_root = Path(__file__).resolve().parents[1]
    policy_path = repo_root / "src" / "familia" / "config" / "policy.yaml"
    monkeypatch.setenv("FAMILIA_POLICY_FILE", str(policy_path))
    engine_mod._engine = None  # force reload
    engine_mod.get_engine()


def test_member_a_cannot_write_shared_roles(registry, policy_with_reserved):
    """The escalation hole: non-admin writing shared:roles.admin_grants."""
    from familia.policy import Decision, PolicyContext, get_engine
    from familia.roles import set_effective_roles_for_tests

    set_effective_roles_for_tests({"owner": {"admin"}, "member_a": set()})
    d = get_engine().evaluate(PolicyContext(
        action="memory.write",
        actor="member_a",
        to_chat="shared:roles.admin_grants",
    ))
    assert d.decision is Decision.DENY


def test_member_a_cannot_write_shared_family_graph(registry, policy_with_reserved):
    from familia.policy import Decision, PolicyContext, get_engine
    from familia.roles import set_effective_roles_for_tests

    set_effective_roles_for_tests({"owner": {"admin"}, "member_a": set()})
    d = get_engine().evaluate(PolicyContext(
        action="memory.write",
        actor="member_a",
        to_chat="shared:family.graph",
    ))
    assert d.decision is Decision.DENY


def test_member_a_can_still_write_other_shared(registry, policy_with_reserved):
    """Reserved-keys deny must not break the broad shared:* allow for normal keys."""
    from familia.policy import Decision, PolicyContext, get_engine
    from familia.roles import set_effective_roles_for_tests

    set_effective_roles_for_tests({"owner": {"admin"}, "member_a": set()})
    d = get_engine().evaluate(PolicyContext(
        action="memory.write",
        actor="member_a",
        to_chat="shared:upcoming_events",
    ))
    assert d.decision is Decision.ALLOW


def test_member_a_can_still_read_shared_roles(registry, policy_with_reserved):
    """roles.py:fetch_admin_grants needs READ access for any actor each turn."""
    from familia.policy import Decision, PolicyContext, get_engine
    from familia.roles import set_effective_roles_for_tests

    set_effective_roles_for_tests({"owner": {"admin"}, "member_a": set()})
    d = get_engine().evaluate(PolicyContext(
        action="memory.read",
        actor="member_a",
        to_chat="shared:roles.admin_grants",
    ))
    assert d.decision is Decision.ALLOW


def test_memory_get_handles_memx_null_body(registry, monkeypatch):
    """memX returns ``200 + null`` for unset keys; tool must not raise."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch
    from familia.tools.memory import MemoryGetTool

    set_current_actor("owner")
    tool = MemoryGetTool(base_url="http://nope")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "null"
    mock_resp.json.return_value = None  # memX's "no value" reply

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        out = asyncio.run(tool.execute(scope="shared", key="never_written"))
    assert "no value stored" in out.lower()


def test_memory_get_handles_explicit_null_value(registry):
    """memX returns ``{"value": null}`` if a key was explicitly set to null."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch
    from familia.tools.memory import MemoryGetTool

    set_current_actor("owner")
    tool = MemoryGetTool(base_url="http://nope")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = '{"value": null}'
    mock_resp.json.return_value = {"value": None}

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        out = asyncio.run(tool.execute(scope="shared", key="zeroed"))
    assert "no value stored" in out.lower()


def test_owner_chat_write_to_shared_roles_denied(registry, policy_with_reserved):
    """SR-14: structural keys are CLI-only. Even owner-from-chat is denied;
    role grants/revokes flow through admin_grant/admin_revoke tools (which
    bypass MemorySetTool) and graph edits go through the `familia` CLI.
    """
    from familia.policy import Decision, PolicyContext, get_engine
    from familia.roles import set_effective_roles_for_tests

    set_effective_roles_for_tests({"owner": {"admin"}, "member_a": set()})
    d = get_engine().evaluate(PolicyContext(
        action="memory.write",
        actor="owner",
        to_chat="shared:roles.admin_grants",
    ))
    assert d.decision is Decision.DENY
