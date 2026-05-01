"""Tests for familia family-graph resolver.

The ``resolve`` function is pure (no httpx, no memX), so we exercise it
directly with hand-built graphs.  The tool wrapper (``ResolvePersonTool``)
just wires httpx + policy + enrichment around ``resolve``; its plumbing
is covered by the memX memory tool tests and live integration smoke.
"""

from __future__ import annotations

import pytest

from familia.tools.family_graph import resolve


GRAPH = {
    "nodes": [
        {"id": "owner", "type": "principal", "display_name": "Owner",
         "aliases": ["Owner_name"]},
        {"id": "member_a", "type": "principal", "display_name": "Member A",
         "aliases": ["Member_a_alias"]},
        {"id": "child_a", "type": "subject", "kind": "person",
         "display_name": "Child A", "aliases": ["Child_a"]},
        {"id": "pet", "type": "subject", "kind": "pet",
         "display_name": "Pet"},
        {"id": "member_a_mother", "type": "subject", "kind": "person",
         "display_name": "Мама Member_a"},
    ],
    "edges": [
        {"from": "owner", "to": "member_a", "rel": "spouse_of"},
        {"from": "member_a", "to": "child_a", "rel": "parent_of"},
        {"from": "member_a_mother", "to": "member_a", "rel": "parent_of"},
        {"from": "owner", "to": "pet", "rel": "owner_of"},
        {"from": "member_a", "to": "pet", "rel": "owner_of"},
    ],
}


def _ids(nodes):
    return [n["id"] for n in nodes]


# --- direct name/alias lookup --------------------------------------------

def test_direct_by_display_name():
    assert _ids(resolve(GRAPH, "Member A")) == ["member_a"]


def test_direct_case_insensitive():
    assert _ids(resolve(GRAPH, "owner_name")) == ["owner"]


def test_direct_by_alias():
    assert _ids(resolve(GRAPH, "Child_a")) == ["child_a"]


def test_direct_by_id():
    assert _ids(resolve(GRAPH, "member_a")) == ["member_a"]


def test_unknown_returns_empty():
    assert resolve(GRAPH, "Гагарин") == []


# --- kinship: spouse (symmetric) -----------------------------------------

def test_spouse_from_owner():
    assert _ids(resolve(GRAPH, "жена", from_actor="owner")) == ["member_a"]


def test_spouse_from_member_a():
    # spouse_of is symmetric even though stored one-way
    assert _ids(resolve(GRAPH, "муж", from_actor="member_a")) == ["owner"]


def test_spouse_requires_from_actor():
    # Without from_actor, kinship terms cannot resolve.
    assert resolve(GRAPH, "жена") == []


# --- kinship: parent / child ---------------------------------------------

def test_child_from_member_a():
    assert _ids(resolve(GRAPH, "дочь", from_actor="member_a")) == ["child_a"]


def test_parent_from_member_a():
    assert _ids(resolve(GRAPH, "мать", from_actor="member_a")) == ["member_a_mother"]


def test_parent_from_owner_empty():
    # Owner has no parent edges in this graph.
    assert resolve(GRAPH, "мать", from_actor="owner") == []


# --- kinship: in-law (2-hop) ---------------------------------------------

def test_parent_in_law_from_owner():
    # тёща = parent of spouse
    assert _ids(resolve(GRAPH, "тёща", from_actor="owner")) == ["member_a_mother"]


# --- kinship: pet --------------------------------------------------------

def test_pet_from_owner():
    assert _ids(resolve(GRAPH, "пёс", from_actor="owner")) == ["pet"]


def test_pet_multiple_owners_resolve_independently():
    # Each owner sees the pet from their side.
    assert _ids(resolve(GRAPH, "собака", from_actor="member_a")) == ["pet"]


# --- edge cases ----------------------------------------------------------

def test_empty_query():
    assert resolve(GRAPH, "") == []


def test_whitespace_only_query():
    assert resolve(GRAPH, "   ") == []


def test_graph_with_no_matching_node_kind():
    # member_a has no sibling edges → "брат" returns empty (we don't even
    # encode siblings yet, but the resolver shouldn't crash).
    from familia.tools.family_graph import KINSHIP_RU
    assert "брат" not in KINSHIP_RU or resolve(GRAPH, "брат", from_actor="member_a") == []
