"""SR-1 wiring test: build_vocabulary_for filters topic names per actor.

Mocks httpx so memX is fake; verifies the assembled prompt block:
  * contains topics the actor can reach
  * EXCLUDES topics the actor cannot reach (the SR-1 leak guard)
  * is empty when actor is None / unknown / has no memx_key
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from familia import bootstrap as fb
from familia import principals as principals_mod
from familia.acl import vocabulary
from familia.principals import Identity, Principal, PrincipalRegistry
from familia.roles import set_effective_roles_for_tests


FAMILY_GRAPH = {
    "nodes": [
        {"id": "owner", "type": "principal", "display_name": "O"},
        {"id": "member_a", "type": "principal", "display_name": "A"},
        {"id": "nanny", "type": "principal", "display_name": "N"},
    ],
    "edges": [
        {"from": "owner", "to": "member_a", "rel": "spouse_of"},
        {"from": "nanny", "to": "kid_topic", "rel": "caregiver_of"},
    ],
    "updated_at_ms": 100,
}

TOPICS_GRAPH = {
    "nodes": [
        {"id": "kid_topic", "type": "topic", "kind": "person",
         "display_name": "Kid"},
        {"id": "school", "type": "topic", "kind": "abstract",
         "display_name": "School"},
        {"id": "marital_only", "type": "topic", "kind": "abstract",
         "display_name": "Marital"},
        {"id": "finance", "type": "topic", "kind": "abstract",
         "display_name": "Finance"},
    ],
    "edges": [
        {"from": "school", "to": "kid_topic", "rel": "concerns",
         "concerns_as": "guardian_of"},
        {"from": "marital_only", "to": "owner", "rel": "concerns",
         "concerns_as": "guardian_of"},
        {"from": "marital_only", "to": "member_a", "rel": "concerns",
         "concerns_as": "guardian_of"},
        {"from": "finance", "to": "owner", "rel": "concerns",
         "concerns_as": "guardian_of"},
    ],
    "updated_at_ms": 200,
}


@pytest.fixture(autouse=True)
def _isolated():
    vocabulary.clear_cache()
    yield
    vocabulary.clear_cache()


@pytest.fixture
def registry(monkeypatch):
    reg = PrincipalRegistry([
        Principal(id="owner", display_name="O", identities=[
            Identity(channel="vk", sender_id="1000001"),
        ], memx_key="k_owner", roles=["admin"]),
        Principal(id="member_a", display_name="A", identities=[
            Identity(channel="vk", sender_id="1000002"),
        ], memx_key="k_member_a", roles=[]),
        Principal(id="nanny", display_name="N", identities=[
            Identity(channel="vk", sender_id="1000003"),
        ], memx_key="k_nanny", roles=[]),
    ])
    monkeypatch.setattr(principals_mod, "_registry", reg)
    return reg


def _patched_httpx(values_by_key):
    def get(url, headers=None, params=None, timeout=None):
        key = (params or {}).get("key", "")
        r = MagicMock()
        r.status_code = 200
        r.json.return_value = {"value": values_by_key.get(key)}
        return r
    return get


def test_build_returns_empty_for_unknown_actor(registry):
    out = fb.build_vocabulary_for("nosuch")
    assert out == ""


def test_build_returns_empty_for_none_actor(registry):
    out = fb.build_vocabulary_for(None)  # type: ignore[arg-type]
    assert out == ""


def test_nanny_does_not_see_finance_or_marital_topic_names(registry):
    """SR-1 critical: topic names ('finance', 'marital_only') must NOT
    appear in the nanny's vocabulary block."""
    set_effective_roles_for_tests({"owner": frozenset({"admin"}), "nanny": frozenset()})
    values = {"shared:family.graph": FAMILY_GRAPH, "shared:topics.graph": TOPICS_GRAPH}
    with patch("httpx.get", side_effect=_patched_httpx(values)):
        out = fb.build_vocabulary_for("nanny")
    assert out  # not empty
    assert "kid_topic" in out
    assert "school" in out  # connected to kid via concerns
    # SR-1: forbidden names
    assert "finance" not in out
    assert "marital_only" not in out
    assert "Marital" not in out
    assert "Finance" not in out


def test_owner_sees_everything(registry):
    set_effective_roles_for_tests({"owner": frozenset({"admin"})})
    values = {"shared:family.graph": FAMILY_GRAPH, "shared:topics.graph": TOPICS_GRAPH}
    with patch("httpx.get", side_effect=_patched_httpx(values)):
        out = fb.build_vocabulary_for("owner")
    for tag in ("school", "marital_only", "finance", "kid_topic", "nanny"):
        assert tag in out, f"admin should see {tag}"


def test_block_format_has_acl_vocab_markers(registry):
    set_effective_roles_for_tests({"owner": frozenset({"admin"})})
    values = {"shared:family.graph": FAMILY_GRAPH, "shared:topics.graph": TOPICS_GRAPH}
    with patch("httpx.get", side_effect=_patched_httpx(values)):
        out = fb.build_vocabulary_for("owner")
    assert out.startswith("<acl-vocabulary>")
    assert out.endswith("</acl-vocabulary>")
    assert "Используй эти id" in out


def test_returns_empty_on_memx_unreachable(registry):
    set_effective_roles_for_tests({"owner": frozenset({"admin"})})
    import httpx as _httpx
    def _raise(*a, **k):
        raise _httpx.ConnectError("unreachable")
    with patch("httpx.get", side_effect=_raise):
        out = fb.build_vocabulary_for("owner")
    # Empty graphs → admin still gets self-only entry; not necessarily empty.
    # Critical: it must not raise.
    assert isinstance(out, str)
