"""Schema dataclass round-trip + fail-closed Graph parsing (SR-10, SR-13)."""

from __future__ import annotations

import pytest

from familia.acl.schema import (
    ACCESS_RELATIONS,
    ALLOWED_RELATIONS,
    TOPIC_KINDS,
    Graph,
    GraphEdge,
    GraphNode,
)


def test_topic_kinds_locked():
    """Schema should not silently grow new kinds; widening requires
    explicit migration of CLI validators + reachable resolver."""
    assert TOPIC_KINDS == frozenset({"person", "pet", "abstract"})


def test_access_relations_match_allowed():
    """Until we want a relation that is documented but non-access-granting,
    these stay equal — keeps the surface boring and predictable."""
    assert ACCESS_RELATIONS == ALLOWED_RELATIONS


def test_graph_node_from_dict_minimal():
    n = GraphNode.from_dict({"id": "owner", "type": "principal"})
    assert n.id == "owner"
    assert n.type == "principal"
    assert n.aliases == ()
    assert n.kind == ""


def test_graph_node_aliases_tuple():
    n = GraphNode.from_dict(
        {"id": "varya", "type": "topic", "kind": "person",
         "aliases": ["alias_a", "alias_b"]}
    )
    assert isinstance(n.aliases, tuple)
    assert n.aliases == ("alias_a", "alias_b")


def test_graph_edge_accepts_from_to():
    e = GraphEdge.from_dict({"from": "owner", "to": "member_a", "rel": "spouse_of"})
    assert e.src == "owner"
    assert e.dst == "member_a"
    assert e.rel == "spouse_of"


def test_graph_edge_accepts_src_dst():
    e = GraphEdge.from_dict({"src": "a", "dst": "b", "rel": "spouse_of"})
    assert e.src == "a"
    assert e.dst == "b"


def test_graph_from_none_returns_empty():
    g = Graph.from_dict(None)
    assert g.nodes == ()
    assert g.edges == ()
    assert g.updated_at_ms == 0


def test_graph_from_corrupt_returns_empty_not_raise():
    """SR-10: corrupt graph data must yield an empty Graph, not exception."""
    for corrupt in (
        "string-not-dict",
        42,
        [1, 2, 3],
        {"nodes": "should-be-list", "edges": []},
        {"nodes": [], "edges": "should-be-list"},
    ):
        g = Graph.from_dict(corrupt)  # type: ignore[arg-type]
        assert g.nodes == ()
        assert g.edges == ()


def test_graph_drops_non_dict_entries_in_nodes_and_edges():
    g = Graph.from_dict({
        "nodes": [{"id": "x", "type": "principal"}, "garbage", 42],
        "edges": [{"from": "x", "to": "y", "rel": "spouse_of"}, None],
    })
    assert len(g.nodes) == 1
    assert g.nodes[0].id == "x"
    assert len(g.edges) == 1


def test_graph_updated_at_ms_invalid_falls_back_to_zero():
    g = Graph.from_dict({"nodes": [], "edges": [], "updated_at_ms": "not-int"})
    assert g.updated_at_ms == 0


def test_graph_node_lookup():
    g = Graph.from_dict({
        "nodes": [
            {"id": "owner", "type": "principal"},
            {"id": "varya", "type": "topic", "kind": "person"},
        ],
        "edges": [],
    })
    assert g.node("owner").id == "owner"
    assert g.node("varya").kind == "person"
    assert g.node("nope") is None
