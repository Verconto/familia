"""Vocabulary builder tests (SR-1, SR-9 cache invalidation)."""

from __future__ import annotations

import pytest

from familia.acl import vocabulary
from familia.acl.schema import Graph


@pytest.fixture(autouse=True)
def _isolated_cache():
    vocabulary.clear_cache()
    yield
    vocabulary.clear_cache()


@pytest.fixture
def family_graph():
    return Graph.from_dict({
        "nodes": [
            {"id": "owner", "type": "principal", "display_name": "O"},
            {"id": "member_a", "type": "principal", "display_name": "A"},
            {"id": "varya", "type": "principal", "display_name": "V"},
            {"id": "nanny", "type": "principal", "display_name": "N"},
        ],
        "edges": [
            {"from": "owner", "to": "member_a", "rel": "spouse_of"},
            {"from": "owner", "to": "varya", "rel": "parent_of"},
            {"from": "member_a", "to": "varya", "rel": "parent_of"},
            {"from": "nanny", "to": "varya", "rel": "caregiver_of"},
        ],
        "updated_at_ms": 100,
    })


@pytest.fixture
def topics_graph():
    return Graph.from_dict({
        "nodes": [
            {"id": "school", "type": "topic", "kind": "abstract",
             "display_name": "Школа"},
            {"id": "child_therapy", "type": "topic", "kind": "abstract",
             "display_name": "Детская терапия"},
            {"id": "finance", "type": "topic", "kind": "abstract",
             "display_name": "Финансы"},
        ],
        "edges": [
            {"from": "school", "to": "varya", "rel": "concerns",
             "concerns_as": "guardian_of"},
            {"from": "child_therapy", "to": "varya", "rel": "concerns",
             "concerns_as": "guardian_of"},
            {"from": "finance", "to": "owner", "rel": "concerns",
             "concerns_as": "guardian_of"},
        ],
        "updated_at_ms": 200,
    })


@pytest.fixture
def child_roles():
    return {"varya": frozenset({"child"})}


# SR-1: vocabulary leak prevention -------------------------------------------

def test_nanny_does_not_see_finance_or_owner_topics(
    family_graph, topics_graph, child_roles,
):
    """SR-1 critical: even topic *names* must be filtered for non-admin
    actors. Nanny has caregiver_of varya only — must NOT see finance."""
    entries = vocabulary.build_for(
        actor="nanny", family=family_graph, topics=topics_graph,
        principal_roles=child_roles, is_admin=False,
    )
    visible_ids = {e.id for e in entries}
    assert "finance" not in visible_ids
    assert "owner" not in visible_ids
    assert "member_a" not in visible_ids
    # She does see varya (her ward), nanny (self), school + child_therapy
    # (both connected to varya).
    assert visible_ids >= {"varya", "nanny", "school", "child_therapy"}


def test_admin_sees_everything(
    family_graph, topics_graph, child_roles,
):
    entries = vocabulary.build_for(
        actor="owner", family=family_graph, topics=topics_graph,
        principal_roles=child_roles, is_admin=True,
    )
    visible_ids = {e.id for e in entries}
    assert visible_ids >= {"owner", "member_a", "varya", "nanny",
                            "school", "child_therapy", "finance"}


def test_child_role_filtered(family_graph, topics_graph, child_roles):
    """SR-2 + SR-1: child does not see parents' tags via vocabulary either."""
    entries = vocabulary.build_for(
        actor="varya", family=family_graph, topics=topics_graph,
        principal_roles=child_roles, is_admin=False,
    )
    visible_ids = {e.id for e in entries}
    assert "owner" not in visible_ids
    assert "member_a" not in visible_ids
    assert "finance" not in visible_ids
    assert visible_ids >= {"varya", "nanny", "school", "child_therapy"}


# SR-9: cache invalidation by etag -------------------------------------------

def test_cache_returns_same_entries_on_unchanged_etag(family_graph, topics_graph):
    e1 = vocabulary.build_for(
        actor="owner", family=family_graph, topics=topics_graph,
        principal_roles={}, is_admin=True,
    )
    e2 = vocabulary.build_for(
        actor="owner", family=family_graph, topics=topics_graph,
        principal_roles={}, is_admin=True,
    )
    assert e1 == e2


def test_cache_invalidates_on_family_etag_bump(family_graph, topics_graph):
    e1 = vocabulary.build_for(
        actor="owner", family=family_graph, topics=topics_graph,
        principal_roles={}, is_admin=True,
    )
    new_family = Graph.from_dict({
        "nodes": list(family_graph.nodes) + [{"id": "new_kid", "type": "principal"}],
        "edges": list(family_graph.edges),
        "updated_at_ms": family_graph.updated_at_ms + 1,
    })
    # The dataclass conversion needs raw dicts:
    new_family_raw = {
        "nodes": [
            {"id": n.id, "type": n.type, "display_name": n.display_name,
             "aliases": list(n.aliases), "kind": n.kind}
            for n in new_family.nodes
        ],
        "edges": [
            {"from": e.src, "to": e.dst, "rel": e.rel}
            for e in new_family.edges
        ],
        "updated_at_ms": new_family.updated_at_ms,
    }
    family_v2 = Graph.from_dict(new_family_raw)
    e2 = vocabulary.build_for(
        actor="owner", family=family_v2, topics=topics_graph,
        principal_roles={}, is_admin=True,
    )
    visible_ids = {x.id for x in e2}
    assert "new_kid" in visible_ids


def test_cache_invalidates_on_topics_etag_bump(family_graph, topics_graph):
    vocabulary.build_for(
        actor="owner", family=family_graph, topics=topics_graph,
        principal_roles={}, is_admin=True,
    )
    new_topics = Graph.from_dict({
        "nodes": [
            {"id": "school", "type": "topic", "kind": "abstract"},
            {"id": "newly_added", "type": "topic", "kind": "abstract"},
        ],
        "edges": [
            {"from": "newly_added", "to": "owner", "rel": "concerns",
             "concerns_as": "guardian_of"},
        ],
        "updated_at_ms": topics_graph.updated_at_ms + 1,
    })
    e2 = vocabulary.build_for(
        actor="owner", family=family_graph, topics=new_topics,
        principal_roles={}, is_admin=True,
    )
    visible_ids = {x.id for x in e2}
    assert "newly_added" in visible_ids
