"""Tests for familia admin role system.

Covers:
- ``compute_effective_roles``: pure merge of static + active grants, with
  expired grants filtered out.
- Policy engine ``@admin`` actor pattern: resolves against
  ``get_effective_roles`` via ContextVar.
- Tool self-gating: ``AdminGrantTool`` / ``AdminRevokeTool`` / ``AdminListTool``
  refuse non-admin actors (policy denies) and accept admins.
"""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest

from familia.policy import Decision, PolicyContext
from familia.policy.engine import PolicyEngine, PolicyRule
from familia.principals import (
    Principal,
    PrincipalRegistry,
    set_current_actor,
)
from familia.roles import (
    compute_effective_roles,
    set_effective_roles_for_tests,
)
from familia.tools.admin import AdminGrantTool, AdminListTool, AdminRevokeTool


# ---------- compute_effective_roles ----------

@pytest.fixture
def fake_registry():
    reg = PrincipalRegistry([
        Principal(id="owner", display_name="Owner", memx_key="owner_key", roles=["admin"]),
        Principal(id="alice", display_name="Alice", memx_key="alice_key"),
        Principal(id="bob",   display_name="Bob",   memx_key="bob_key"),
    ])
    with patch("familia.roles.get_registry", return_value=reg), \
         patch("familia.tools.admin.get_registry", return_value=reg), \
         patch("familia.principals.get_registry", return_value=reg):
        yield reg


def test_compute_static_only(fake_registry):
    roles = compute_effective_roles([], now_ts=1000.0)
    assert roles == {"owner": frozenset({"admin"})}


def test_compute_active_grant_adds_admin(fake_registry):
    grants = [{"principal": "alice", "granted_by": "owner", "until_ts_utc": 2000.0}]
    roles = compute_effective_roles(grants, now_ts=1000.0)
    assert roles["owner"] == frozenset({"admin"})
    assert roles["alice"] == frozenset({"admin"})


def test_compute_expired_grant_ignored(fake_registry):
    grants = [{"principal": "alice", "granted_by": "owner", "until_ts_utc": 500.0}]
    roles = compute_effective_roles(grants, now_ts=1000.0)
    assert "alice" not in roles


def test_compute_string_payload_json(fake_registry):
    import json
    payload = json.dumps([
        {"principal": "bob", "granted_by": "owner", "until_ts_utc": 2000.0}
    ])
    roles = compute_effective_roles(payload, now_ts=1000.0)
    assert roles["bob"] == frozenset({"admin"})


# ---------- policy engine @admin pattern ----------

def _engine_with_admin_rule() -> PolicyEngine:
    return PolicyEngine([
        PolicyRule(
            name="admins-only",
            action=["role.admin.grant"],
            actor=["@admin"],
            decision=Decision.ALLOW,
        ),
    ])


def test_policy_admin_allows_static_admin(fake_registry):
    set_effective_roles_for_tests({"owner": {"admin"}})
    eng = _engine_with_admin_rule()
    d = eng.evaluate(PolicyContext(action="role.admin.grant", actor="owner"))
    assert d.decision is Decision.ALLOW


def test_policy_admin_denies_non_admin(fake_registry):
    set_effective_roles_for_tests({"owner": {"admin"}})
    eng = _engine_with_admin_rule()
    d = eng.evaluate(PolicyContext(action="role.admin.grant", actor="alice"))
    assert d.decision is Decision.DENY


def test_policy_admin_allows_granted(fake_registry):
    set_effective_roles_for_tests({
        "owner": {"admin"},
        "alice": {"admin"},
    })
    eng = _engine_with_admin_rule()
    d = eng.evaluate(PolicyContext(action="role.admin.grant", actor="alice"))
    assert d.decision is Decision.ALLOW


def test_policy_negated_admin(fake_registry):
    set_effective_roles_for_tests({"owner": {"admin"}})
    eng = PolicyEngine([
        PolicyRule(
            name="block-admins",
            action=["message.send"],
            actor=["!@admin"],
            decision=Decision.ALLOW,
        ),
    ])
    # non-admin: matches !@admin → allow
    d = eng.evaluate(PolicyContext(action="message.send", actor="alice"))
    assert d.decision is Decision.ALLOW
    # admin: doesn't match !@admin → default deny
    d = eng.evaluate(PolicyContext(action="message.send", actor="owner"))
    assert d.decision is Decision.DENY


# ---------- tool self-gating ----------

class _FakeMemX:
    """Captures memX reads/writes so tools don't hit the network."""

    def __init__(self, initial=None):
        self.grants = list(initial or [])
        self.writes = []

    async def fetch(self, api_key, base_url=None):
        return list(self.grants)

    async def write(self, grants, api_key, base_url=None):
        self.writes.append(list(grants))
        self.grants = list(grants)
        return True, ""


@pytest.fixture
def fake_memx():
    mem = _FakeMemX()
    with patch("familia.tools.admin.fetch_admin_grants", side_effect=mem.fetch), \
         patch("familia.tools.admin.write_admin_grants", side_effect=mem.write):
        yield mem


@pytest.fixture
def admin_engine():
    eng = PolicyEngine([
        PolicyRule(name="grant",  action=["role.admin.grant"],
                   actor=["@admin"], decision=Decision.ALLOW),
        PolicyRule(name="revoke", action=["role.admin.revoke"],
                   actor=["@admin"], decision=Decision.ALLOW),
        PolicyRule(name="list",   action=["role.admin.list"],
                   actor=["@admin"], decision=Decision.ALLOW),
    ])
    with patch("familia.tools.admin.get_engine", return_value=eng):
        yield eng


@pytest.mark.asyncio
async def test_grant_tool_admin_ok(fake_registry, fake_memx, admin_engine):
    set_effective_roles_for_tests({"owner": {"admin"}})
    set_current_actor("owner")
    tool = AdminGrantTool()
    result = await tool.execute(principal="alice", hours=2, reason="тест")
    assert "Granted admin to 'alice'" in result
    assert len(fake_memx.writes) == 1
    assert fake_memx.writes[0][0]["principal"] == "alice"
    assert fake_memx.writes[0][0]["granted_by"] == "owner"


@pytest.mark.asyncio
async def test_grant_tool_non_admin_denied(fake_registry, fake_memx, admin_engine):
    set_effective_roles_for_tests({"owner": {"admin"}})
    set_current_actor("alice")
    tool = AdminGrantTool()
    result = await tool.execute(principal="bob", hours=1)
    assert result.startswith("Policy denied role.admin.grant")
    assert fake_memx.writes == []


@pytest.mark.asyncio
async def test_grant_tool_rejects_self_grant(fake_registry, fake_memx, admin_engine):
    set_effective_roles_for_tests({"owner": {"admin"}})
    set_current_actor("owner")
    tool = AdminGrantTool()
    result = await tool.execute(principal="owner")
    assert "cannot grant admin to yourself" in result


@pytest.mark.asyncio
async def test_grant_tool_rejects_unknown_principal(fake_registry, fake_memx, admin_engine):
    set_effective_roles_for_tests({"owner": {"admin"}})
    set_current_actor("owner")
    tool = AdminGrantTool()
    result = await tool.execute(principal="ghost")
    assert "unknown principal" in result


@pytest.mark.asyncio
async def test_grant_tool_rejects_bad_hours(fake_registry, fake_memx, admin_engine):
    set_effective_roles_for_tests({"owner": {"admin"}})
    set_current_actor("owner")
    tool = AdminGrantTool()
    assert "hours must be" in await tool.execute(principal="alice", hours=0)
    assert "hours must be" in await tool.execute(principal="alice", hours=200)


@pytest.mark.asyncio
async def test_revoke_tool_admin_ok(fake_registry, fake_memx, admin_engine):
    fake_memx.grants = [{
        "principal": "alice", "granted_by": "owner",
        "until_ts_utc": time.time() + 3600, "reason": None,
    }]
    set_effective_roles_for_tests({"owner": {"admin"}, "alice": {"admin"}})
    set_current_actor("owner")
    tool = AdminRevokeTool()
    result = await tool.execute(principal="alice")
    assert "Revoked admin grant" in result
    assert fake_memx.writes[-1] == []


@pytest.mark.asyncio
async def test_revoke_tool_non_admin_denied(fake_registry, fake_memx, admin_engine):
    set_effective_roles_for_tests({"owner": {"admin"}})
    set_current_actor("bob")
    tool = AdminRevokeTool()
    result = await tool.execute(principal="alice")
    assert result.startswith("Policy denied role.admin.revoke")


@pytest.mark.asyncio
async def test_revoke_tool_no_active_grant(fake_registry, fake_memx, admin_engine):
    set_effective_roles_for_tests({"owner": {"admin"}})
    set_current_actor("owner")
    tool = AdminRevokeTool()
    result = await tool.execute(principal="alice")
    assert "No active admin grant" in result


@pytest.mark.asyncio
async def test_list_tool_returns_json(fake_registry, fake_memx, admin_engine):
    fake_memx.grants = [{
        "principal": "alice", "granted_by": "owner",
        "until_ts_utc": time.time() + 3600, "reason": "тест",
    }]
    set_effective_roles_for_tests({"owner": {"admin"}, "alice": {"admin"}})
    set_current_actor("owner")
    tool = AdminListTool()
    result = await tool.execute()
    import json
    payload = json.loads(result)
    assert "owner" in payload["static"]
    assert len(payload["grants"]) == 1
    assert payload["grants"][0]["principal"] == "alice"


@pytest.mark.asyncio
async def test_list_tool_non_admin_denied(fake_registry, fake_memx, admin_engine):
    set_effective_roles_for_tests({"owner": {"admin"}})
    set_current_actor("bob")
    tool = AdminListTool()
    result = await tool.execute()
    assert result.startswith("Policy denied role.admin.list")
