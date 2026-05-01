"""Tests for the ``@peer_of`` matcher and ``familia.acl.peers``.

Covers SR-2 (children excluded from peer rule) and SR-12 (negative-test
suite for new ACL primitives).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest

from familia.acl import peers


@pytest.fixture
def stub_graph_and_registry(monkeypatch):
    """Return a callable that swaps ``shared:family.graph`` + principals
    registry contents in place. Used by every test below to set up a
    specific edge / role configuration."""

    def _set(*, edges: list[dict], child_ids: list[str] = ()) -> None:
        # Reset module cache so each test sees a fresh probe.
        peers.reset_cache()
        graph_doc = {
            "nodes": [],  # peers.is_peer doesn't read nodes
            "edges": list(edges),
            "updated_at_ms": 1_000_000_000,
        }

        def _load(key, *_, **__):
            assert key == "shared:family.graph"
            return graph_doc

        monkeypatch.setattr(
            "familia.acl.graph_io.load_graph_value", _load, raising=False
        )

        # Fake registry: each id has the role list we ask for.
        from familia import principals as principals_mod

        registry = principals_mod.PrincipalRegistry()
        all_ids = {e[k] for e in edges for k in ("from", "to")}
        for pid in all_ids | set(child_ids):
            roles = ["child"] if pid in set(child_ids) else []
            registry.add(principals_mod.Principal(id=pid, roles=roles))
        monkeypatch.setattr(principals_mod, "_registry", registry, raising=False)

    return _set


def test_peer_when_spouse_edge(stub_graph_and_registry):
    stub_graph_and_registry(edges=[{"from": "a", "to": "b", "rel": "spouse_of"}])
    assert peers.is_peer("a", "b") is True
    assert peers.is_peer("b", "a") is True  # symmetric


def test_peer_when_guardian_edge(stub_graph_and_registry):
    stub_graph_and_registry(edges=[{"from": "a", "to": "b", "rel": "guardian_of"}])
    assert peers.is_peer("a", "b") is True


def test_not_peer_for_parent_of(stub_graph_and_registry):
    """``parent_of`` is intentionally narrower than the peer rule."""
    stub_graph_and_registry(edges=[{"from": "parent", "to": "child", "rel": "parent_of"}])
    assert peers.is_peer("parent", "child") is False
    assert peers.is_peer("child", "parent") is False


def test_not_peer_for_caregiver_of(stub_graph_and_registry):
    stub_graph_and_registry(edges=[{"from": "nanny", "to": "kid", "rel": "caregiver_of"}])
    assert peers.is_peer("nanny", "kid") is False


def test_child_actor_excluded_even_with_spouse_edge(stub_graph_and_registry):
    """SR-2: a principal flagged ``role: child`` is never a peer, even if
    a (mis-configured) spouse_of edge points at them."""
    stub_graph_and_registry(
        edges=[{"from": "adult", "to": "kid", "rel": "spouse_of"}],
        child_ids=["kid"],
    )
    assert peers.is_peer("adult", "kid") is False
    assert peers.is_peer("kid", "adult") is False


def test_self_is_never_peer(stub_graph_and_registry):
    stub_graph_and_registry(edges=[{"from": "a", "to": "a", "rel": "spouse_of"}])
    assert peers.is_peer("a", "a") is False


def test_none_actor_is_never_peer(stub_graph_and_registry):
    stub_graph_and_registry(edges=[{"from": "a", "to": "b", "rel": "spouse_of"}])
    assert peers.is_peer(None, "a") is False


def test_cache_busts_on_role_flip(stub_graph_and_registry):
    """Flip ``role: child`` mid-cache; the early children-check fires
    before cache lookup so the new role takes effect immediately."""
    stub_graph_and_registry(edges=[{"from": "a", "to": "b", "rel": "spouse_of"}])
    assert peers.is_peer("a", "b") is True

    stub_graph_and_registry(
        edges=[{"from": "a", "to": "b", "rel": "spouse_of"}],
        child_ids=["b"],
    )
    assert peers.is_peer("a", "b") is False
