"""SR-14: structural shared keys are write-denied for ALL chat actors.

The reserved-keys policy rule must NOT carry an `actor: "!@admin"` clause
— even an admin actor making the request through the chat-flow gets
denied. Graphs and roles are edited only through the `familia` CLI.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from familia import principals as principals_mod
from familia.policy import engine as engine_mod
from familia.principals import Identity, Principal, PrincipalRegistry
from familia.tools.memory import MemorySetTool


@pytest.fixture
def example_policy_engine(monkeypatch):
    repo_policy = (
        Path(__file__).resolve().parents[1]
        / "src" / "familia" / "config" / "policy.yaml"
    )
    monkeypatch.setenv("FAMILIA_POLICY_FILE", str(repo_policy))
    engine_mod._engine = None
    return engine_mod.get_engine()


@pytest.fixture
def registry(monkeypatch):
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


# ---- policy denies even admin ---------------------------------------------

def test_policy_denies_admin_write_to_family_graph(example_policy_engine, registry):
    from familia.policy import Decision, PolicyContext
    d = example_policy_engine.evaluate(PolicyContext(
        action="memory.write", actor="owner",
        to_chat="shared:family.graph",
    ))
    assert d.decision is Decision.DENY


def test_policy_denies_admin_write_to_topics_graph(example_policy_engine, registry):
    from familia.policy import Decision, PolicyContext
    d = example_policy_engine.evaluate(PolicyContext(
        action="memory.write", actor="owner",
        to_chat="shared:topics.graph",
    ))
    assert d.decision is Decision.DENY


def test_policy_denies_admin_write_to_roles(example_policy_engine, registry):
    from familia.policy import Decision, PolicyContext
    d = example_policy_engine.evaluate(PolicyContext(
        action="memory.write", actor="owner",
        to_chat="shared:roles.admin_grants",
    ))
    assert d.decision is Decision.DENY


def test_policy_still_allows_owner_write_to_normal_shared(example_policy_engine, registry):
    """Reserved-keys deny must not leak into regular shared writes."""
    from familia.policy import Decision, PolicyContext
    d = example_policy_engine.evaluate(PolicyContext(
        action="memory.write", actor="owner",
        to_chat="shared:family.upcoming",
    ))
    assert d.decision is Decision.ALLOW


# ---- read still allowed for everyone -------------------------------------

def test_policy_still_allows_member_read_of_admin_grants(example_policy_engine, registry):
    """roles.fetch_admin_grants needs read access for any actor."""
    from familia.policy import Decision, PolicyContext
    d = example_policy_engine.evaluate(PolicyContext(
        action="memory.read", actor="member_a",
        to_chat="shared:roles.admin_grants",
    ))
    assert d.decision is Decision.ALLOW


# ---- tool-level guard (defense-in-depth, SR-14) --------------------------

def test_memory_set_refuses_family_graph_even_for_admin(registry):
    from familia import principals as p
    p.set_current_actor("owner")
    tool = MemorySetTool(base_url="http://nope")
    out = asyncio.run(tool.execute(
        scope="shared", key="family.graph", value="garbage",
    ))
    assert "structural key" in out.lower()
    assert "cli" in out.lower()


def test_memory_set_refuses_topics_graph_even_for_admin(registry):
    from familia import principals as p
    p.set_current_actor("owner")
    tool = MemorySetTool(base_url="http://nope")
    out = asyncio.run(tool.execute(
        scope="shared", key="topics.graph", value="garbage",
    ))
    assert "structural key" in out.lower()


def test_memory_set_refuses_roles_subkeys(registry):
    from familia import principals as p
    p.set_current_actor("owner")
    tool = MemorySetTool(base_url="http://nope")
    out = asyncio.run(tool.execute(
        scope="shared", key="roles.admin_grants",
        value="[]",
    ))
    assert "structural key" in out.lower()
