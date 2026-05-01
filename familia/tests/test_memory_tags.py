"""Tag-based ACL on memory_set / memory_get (SR-7, SR-10, SR-11).

Mocks httpx so memX traffic is fake; relies on the Graph fixtures to
shape who-sees-what.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from familia import principals as principals_mod
from familia.acl import codec
from familia.principals import Identity, Principal, PrincipalRegistry
from familia.tools.memory import MemoryGetTool, MemorySetTool


# ---- shared graphs fixture --------------------------------------------------

FAMILY_GRAPH = {
    "nodes": [
        {"id": "owner", "type": "principal"},
        {"id": "member_a", "type": "principal"},
        {"id": "varya", "type": "principal"},
        {"id": "nanny", "type": "principal"},
    ],
    "edges": [
        {"from": "owner", "to": "member_a", "rel": "spouse_of"},
        {"from": "owner", "to": "varya", "rel": "parent_of"},
        {"from": "member_a", "to": "varya", "rel": "parent_of"},
        {"from": "nanny", "to": "varya", "rel": "caregiver_of"},
    ],
    "updated_at_ms": 100,
}

TOPICS_GRAPH = {
    "nodes": [
        {"id": "school", "type": "topic", "kind": "abstract"},
        {"id": "finance", "type": "topic", "kind": "abstract"},
    ],
    "edges": [
        {"from": "school", "to": "varya", "rel": "concerns",
         "concerns_as": "guardian_of"},
        {"from": "finance", "to": "owner", "rel": "concerns",
         "concerns_as": "guardian_of"},
        {"from": "finance", "to": "member_a", "rel": "concerns",
         "concerns_as": "guardian_of"},
    ],
    "updated_at_ms": 200,
}


def _store_get_response(value: Any) -> MagicMock:
    r = MagicMock()
    r.status_code = 200
    r.text = json.dumps({"value": value}, ensure_ascii=False)
    r.json.return_value = {"value": value}
    return r


def _patched_client(values_by_key: dict[str, Any]):
    """httpx.AsyncClient that returns canned responses by key."""
    captured_writes: list[dict[str, Any]] = []

    async def get(url, headers=None, params=None):
        key = (params or {}).get("key", "")
        return _store_get_response(values_by_key.get(key))

    async def post(url, headers=None, json=None, **_):
        captured_writes.append(json or {})
        r = MagicMock()
        r.status_code = 200
        r.text = "ok"
        return r

    client = AsyncMock()
    client.get = AsyncMock(side_effect=get)
    client.post = AsyncMock(side_effect=post)
    return client, captured_writes


def _make_registry(monkeypatch, role_overrides=None):
    overrides = role_overrides or {}
    reg = PrincipalRegistry([
        Principal(id="owner", display_name="O",
                  identities=[Identity(channel="vk", sender_id="1000001")],
                  memx_key="k_owner",
                  roles=["admin"]),
        Principal(id="member_a", display_name="A",
                  identities=[Identity(channel="vk", sender_id="1000002")],
                  memx_key="k_member_a", roles=overrides.get("member_a", [])),
        Principal(id="varya", display_name="V",
                  identities=[Identity(channel="tg", sender_id="3000001")],
                  memx_key="k_varya", roles=overrides.get("varya", ["child"])),
        Principal(id="nanny", display_name="N",
                  identities=[Identity(channel="vk", sender_id="1000003")],
                  memx_key="k_nanny", roles=overrides.get("nanny", [])),
    ])
    monkeypatch.setattr(principals_mod, "_registry", reg)
    return reg


@pytest.fixture
def graphs_in_memx():
    return {
        "shared:family.graph": FAMILY_GRAPH,
        "shared:topics.graph": TOPICS_GRAPH,
    }


# ---- write-side --- SR-7 ---------------------------------------------------

def test_set_with_unreachable_tag_rejected(monkeypatch, graphs_in_memx):
    _make_registry(monkeypatch)
    monkeypatch.setattr("familia.policy.engine._engine", None)
    # member_a tries to tag with "finance" — she IS connected to finance, ok.
    # Try with an actually unreachable tag like "ghost_topic" not in graph.
    principals_mod.set_current_actor("member_a")
    from familia.roles import set_effective_roles_for_tests
    set_effective_roles_for_tests({"owner": frozenset({"admin"}), "member_a": frozenset()})

    client, writes = _patched_client(graphs_in_memx)
    with patch("httpx.AsyncClient") as cls:
        cls.return_value.__aenter__.return_value = client
        tool = MemorySetTool(base_url="http://nope")
        # `nanny`-tag is reachable for member_a (parent_of varya, caregiver_of varya)?
        # member_a is connected to varya (parent_of); nanny is connected to varya
        # (caregiver_of). They share varya, so are reciprocally reachable through
        # varya? No — reachable_persons does direct hops only. member_a reaches
        # {member_a, owner, varya}; nanny is NOT in that set. Use it.
        out = asyncio.run(tool.execute(
            scope="shared", key="x", value="v",
            tags=["nanny"],
        ))
    assert out.startswith("Error:")
    assert "nanny" in out
    assert writes == []  # nothing should have been POSTed


def test_set_with_reachable_tags_succeeds_wrapped(monkeypatch, graphs_in_memx):
    _make_registry(monkeypatch)
    monkeypatch.setattr("familia.policy.engine._engine", None)
    principals_mod.set_current_actor("member_a")
    from familia.roles import set_effective_roles_for_tests
    set_effective_roles_for_tests({"owner": frozenset({"admin"}), "member_a": frozenset()})

    client, writes = _patched_client(graphs_in_memx)
    with patch("httpx.AsyncClient") as cls:
        cls.return_value.__aenter__.return_value = client
        tool = MemorySetTool(base_url="http://nope")
        out = asyncio.run(tool.execute(
            scope="shared", key="varya.school_supplies",
            value="тетради, ручки",
            tags=["varya", "school"],
        ))
    assert "Stored at" in out
    assert "теги: school, varya" in out
    # MemorySetTool writes BOTH the value AND a private_index entry
    # (``private:<actor>:value:shared_index`` / ``private_index``) so
    # the agent can later list its own writes without an audit grep.
    # The value goes first, the index second — verify both, but pin
    # the value-write to writes[0] so the assertions below stay
    # readable.
    assert len(writes) == 2
    value_write = writes[0]
    assert value_write["key"] == "shared:varya.school_supplies"
    parsed = json.loads(value_write["value"])
    assert parsed["__familia_acl_v1"] is True
    assert sorted(parsed["tags"]) == ["school", "varya"]
    assert parsed["value"] == "тетради, ручки"
    # Sanity-check the index entry — full schema is exercised by
    # test_memory_indexes; here we only confirm it's pointed at the
    # actor and references the just-written key.
    index_write = writes[1]
    assert index_write["key"] == "private:member_a:value:shared_index"


def test_admin_can_tag_anything(monkeypatch, graphs_in_memx):
    _make_registry(monkeypatch)
    monkeypatch.setattr("familia.policy.engine._engine", None)
    principals_mod.set_current_actor("owner")
    from familia.roles import set_effective_roles_for_tests
    set_effective_roles_for_tests({"owner": frozenset({"admin"})})

    client, writes = _patched_client(graphs_in_memx)
    with patch("httpx.AsyncClient") as cls:
        cls.return_value.__aenter__.return_value = client
        tool = MemorySetTool(base_url="http://nope")
        out = asyncio.run(tool.execute(
            scope="shared", key="x", value="v",
            tags=["definitely_not_in_graph"],
        ))
    assert "Stored at" in out


def test_set_without_tags_legacy_path(monkeypatch, graphs_in_memx):
    """Backward compat: missing tags param keeps legacy raw value write."""
    _make_registry(monkeypatch)
    monkeypatch.setattr("familia.policy.engine._engine", None)
    principals_mod.set_current_actor("member_a")
    from familia.roles import set_effective_roles_for_tests
    set_effective_roles_for_tests({"owner": frozenset({"admin"}), "member_a": frozenset()})

    client, writes = _patched_client(graphs_in_memx)
    with patch("httpx.AsyncClient") as cls:
        cls.return_value.__aenter__.return_value = client
        tool = MemorySetTool(base_url="http://nope")
        out = asyncio.run(tool.execute(
            scope="private", key="note", value="legacy text",
        ))
    assert "Stored at" in out
    assert "теги:" not in out
    assert writes[0]["value"] == "legacy text"  # unwrapped


# ---- read-side --- SR-10 fail-closed --------------------------------------

def test_get_wrapped_visible_through_tag_intersection(monkeypatch, graphs_in_memx):
    _make_registry(monkeypatch)
    monkeypatch.setattr("familia.policy.engine._engine", None)
    principals_mod.set_current_actor("member_a")
    from familia.roles import set_effective_roles_for_tests
    set_effective_roles_for_tests({"owner": frozenset({"admin"}), "member_a": frozenset()})

    wrapped = codec.encode("тетради, ручки", ["varya", "school"])
    values = dict(graphs_in_memx)
    values["shared:varya.school_supplies"] = wrapped

    client, _ = _patched_client(values)
    with patch("httpx.AsyncClient") as cls:
        cls.return_value.__aenter__.return_value = client
        tool = MemoryGetTool(base_url="http://nope")
        out = asyncio.run(tool.execute(
            scope="shared", key="varya.school_supplies",
        ))
    assert out == "тетради, ручки"


def test_get_wrapped_invisible_when_no_intersection(monkeypatch, graphs_in_memx):
    _make_registry(monkeypatch)
    monkeypatch.setattr("familia.policy.engine._engine", None)
    principals_mod.set_current_actor("nanny")
    from familia.roles import set_effective_roles_for_tests
    set_effective_roles_for_tests({"owner": frozenset({"admin"}), "nanny": frozenset()})

    # Record about finance, nanny has no path to it.
    wrapped = codec.encode("плати налог", ["finance"])
    values = dict(graphs_in_memx)
    values["shared:money_topic"] = wrapped

    client, _ = _patched_client(values)
    with patch("httpx.AsyncClient") as cls:
        cls.return_value.__aenter__.return_value = client
        tool = MemoryGetTool(base_url="http://nope")
        out = asyncio.run(tool.execute(scope="shared", key="money_topic"))
    # Fail-closed: not even existence leaked
    assert "no value stored" in out


def test_get_legacy_lookalike_value_treated_as_legacy(monkeypatch, graphs_in_memx):
    """SR-4 critical: a pre-feature value with shape ``{"tags": [...]}`` but
    without sentinel must not be misread as wrapped (and accidentally leak)."""
    _make_registry(monkeypatch)
    monkeypatch.setattr("familia.policy.engine._engine", None)
    principals_mod.set_current_actor("nanny")
    from familia.roles import set_effective_roles_for_tests
    set_effective_roles_for_tests({"owner": frozenset({"admin"}), "nanny": frozenset()})

    legacy_lookalike = json.dumps({"tags": ["finance"], "value": "leak"})
    values = dict(graphs_in_memx)
    values["shared:adversarial"] = legacy_lookalike

    client, _ = _patched_client(values)
    with patch("httpx.AsyncClient") as cls:
        cls.return_value.__aenter__.return_value = client
        tool = MemoryGetTool(base_url="http://nope")
        out = asyncio.run(tool.execute(scope="shared", key="adversarial"))
    # Returned as legacy raw string (or json'd dict). It must NOT have been
    # interpreted as a tagged record — i.e., we shouldn't have run an ACL
    # check that produced "no value stored".
    assert "no value stored" not in out
    # And the actual content (parsed legacy JSON) reaches the reader.
    assert "leak" in out


# ---- audit (SR-11) --------------------------------------------------------

def test_set_audit_emitted_on_deny(monkeypatch, graphs_in_memx, tmp_path):
    _make_registry(monkeypatch)
    monkeypatch.setattr("familia.policy.engine._engine", None)
    principals_mod.set_current_actor("nanny")
    from familia.roles import set_effective_roles_for_tests
    set_effective_roles_for_tests({"owner": frozenset({"admin"}), "nanny": frozenset()})

    audit_file = tmp_path / "audit.jsonl"
    monkeypatch.setenv("FAMILIA_AUDIT_FILE", str(audit_file))
    # Reset chmod cache between tests.
    from familia import audit as audit_mod
    audit_mod._chmod_done.clear()

    client, _ = _patched_client(graphs_in_memx)
    with patch("httpx.AsyncClient") as cls:
        cls.return_value.__aenter__.return_value = client
        tool = MemorySetTool(base_url="http://nope")
        asyncio.run(tool.execute(
            scope="shared", key="x", value="v",
            tags=["finance"],
        ))
    text = audit_file.read_text(encoding="utf-8")
    events = [json.loads(line) for line in text.splitlines()]
    decisions = [e for e in events if e.get("kind") == "tag_acl_decision"]
    assert any(e["decision"] == "deny" for e in decisions)
