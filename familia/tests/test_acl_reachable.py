"""Reachability tests covering SR-2 (child asymmetry), SR-10 (fail-closed).

Test ids stay neutral (owner/member_a/member_b/varya/syava/nanny) — the
pre-commit hook scans for personal data, and these are the placeholders
documented in policy.example.yaml.
"""

from __future__ import annotations

import pytest

from familia.acl.reachable import (
    reachable_persons,
    reachable_tag_ids,
    reachable_topics_for,
)
from familia.acl.schema import Graph


# --- Fixtures ---------------------------------------------------------------

@pytest.fixture
def family_basic() -> Graph:
    """Two adults + one teen child + nanny (4 principals, post-migration shape).

    Edges:
        owner — spouse_of — member_a
        owner — parent_of — varya       (varya has role: child)
        member_a — parent_of — varya
        nanny — caregiver_of — varya
    """
    return Graph.from_dict({
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
    })


@pytest.fixture
def topics_basic() -> Graph:
    """Two topics: school (concerns varya), finance (concerns adults)."""
    return Graph.from_dict({
        "nodes": [
            {"id": "school", "type": "topic", "kind": "abstract"},
            {"id": "finance", "type": "topic", "kind": "abstract"},
            {"id": "orphan_topic", "type": "topic", "kind": "abstract"},
        ],
        "edges": [
            {"from": "school", "to": "varya", "rel": "concerns",
             "concerns_as": "guardian_of"},
            {"from": "finance", "to": "owner", "rel": "concerns",
             "concerns_as": "guardian_of"},
            {"from": "finance", "to": "member_a", "rel": "concerns",
             "concerns_as": "guardian_of"},
        ],
    })


@pytest.fixture
def child_roles() -> dict[str, frozenset[str]]:
    return {"varya": frozenset({"child"})}


# --- reachable_persons baseline (no role asymmetry) -------------------------

def test_actor_includes_self_with_empty_graph():
    g = Graph.from_dict({"nodes": [], "edges": []})
    assert reachable_persons(g, "owner") == {"owner"}


def test_spouse_symmetric(family_basic):
    assert "member_a" in reachable_persons(family_basic, "owner")
    assert "owner" in reachable_persons(family_basic, "member_a")


def test_parent_sees_child(family_basic, child_roles):
    """Forward parent_of always reaches the child."""
    persons = reachable_persons(family_basic, "owner", child_roles)
    assert "varya" in persons
    persons_b = reachable_persons(family_basic, "member_a", child_roles)
    assert "varya" in persons_b


# --- SR-2 — child asymmetry -------------------------------------------------

def test_child_does_not_reach_parents(family_basic, child_roles):
    """Critical: SR-2 — child must NOT see her parents through reverse parent_of."""
    persons = reachable_persons(family_basic, "varya", child_roles)
    assert "owner" not in persons, "child must not reach owner via reverse parent_of"
    assert "member_a" not in persons, "child must not reach member_a via reverse parent_of"
    # She still reaches herself, and the caregiver via direct caregiver_of.
    assert "varya" in persons
    assert "nanny" in persons


def test_child_without_role_set_falls_back_to_symmetric(family_basic):
    """If no roles configured (legacy / standalone), the resolver works
    bidirectionally — keeps backwards compatibility."""
    persons = reachable_persons(family_basic, "varya")  # no roles
    assert "owner" in persons
    assert "member_a" in persons


def test_caregiver_bidirectional(family_basic, child_roles):
    """caregiver_of is access-granting both ways for non-child relations,
    even when the target is a child (the asymmetry is on parent_of only)."""
    nanny_view = reachable_persons(family_basic, "nanny", child_roles)
    assert "varya" in nanny_view
    varya_view = reachable_persons(family_basic, "varya", child_roles)
    assert "nanny" in varya_view


# --- reachable_topics_for ---------------------------------------------------

def test_topic_reached_via_connected_person(family_basic, topics_basic, child_roles):
    persons = reachable_persons(family_basic, "owner", child_roles)
    topics = reachable_topics_for(topics_basic, persons)
    assert "school" in topics  # owner is connected to varya, school concerns varya
    assert "finance" in topics  # finance directly concerns owner


def test_topic_with_no_path_is_not_reached(family_basic, topics_basic, child_roles):
    """Child reaches her own topic (school) but NOT finance (parent-only)."""
    persons = reachable_persons(family_basic, "varya", child_roles)
    topics = reachable_topics_for(topics_basic, persons)
    assert "school" in topics
    assert "finance" not in topics


def test_orphan_topic_unreachable(family_basic, topics_basic, child_roles):
    """SR-1 / SR-10: a topic with no concerns-edges is reachable to nobody
    (admin handles it via bypass at the call site)."""
    persons = reachable_persons(family_basic, "owner", child_roles)
    topics = reachable_topics_for(topics_basic, persons)
    assert "orphan_topic" not in topics


# --- reachable_tag_ids combined --------------------------------------------

def test_combined_includes_self_persons_and_topics(family_basic, topics_basic, child_roles):
    ids = reachable_tag_ids(family_basic, topics_basic, "owner", child_roles)
    assert "owner" in ids
    assert "member_a" in ids
    assert "varya" in ids
    assert "school" in ids
    assert "finance" in ids


def test_combined_for_child_blocks_finance(family_basic, topics_basic, child_roles):
    """End-to-end: child sees only her own scope + caregiver + school topic."""
    ids = reachable_tag_ids(family_basic, topics_basic, "varya", child_roles)
    assert ids == {"varya", "nanny", "school"}


def test_combined_for_nanny_only_sees_varya_scope(family_basic, topics_basic, child_roles):
    ids = reachable_tag_ids(family_basic, topics_basic, "nanny", child_roles)
    # nanny ↔ varya, varya ↔ school (concerns-edge), nanny + varya themselves.
    # No reach to owner/member_a/finance.
    assert "owner" not in ids
    assert "member_a" not in ids
    assert "finance" not in ids
    assert "nanny" in ids
    assert "varya" in ids
    assert "school" in ids


# --- SR-10: corrupt graphs do not raise -------------------------------------

def test_corrupt_family_graph_yields_only_self():
    g = Graph.from_dict("garbage")  # type: ignore[arg-type]
    persons = reachable_persons(g, "owner", {"owner": frozenset({"admin"})})
    assert persons == {"owner"}


def test_corrupt_topics_graph_yields_no_topics(family_basic):
    bad = Graph.from_dict(None)
    persons = reachable_persons(family_basic, "owner")
    topics = reachable_topics_for(bad, persons)
    assert topics == set()


def test_unknown_actor_returns_singleton():
    """An actor not present in any edge still reaches themselves."""
    g = Graph.from_dict({
        "nodes": [{"id": "owner", "type": "principal"}],
        "edges": [],
    })
    assert reachable_persons(g, "ghost") == {"ghost"}


# --- relation-type filtering ------------------------------------------------

def test_unknown_relation_ignored():
    """An edge with rel ∉ ACCESS_RELATIONS is treated as non-access."""
    g = Graph.from_dict({
        "nodes": [
            {"id": "owner", "type": "principal"},
            {"id": "member_a", "type": "principal"},
        ],
        "edges": [
            {"from": "owner", "to": "member_a", "rel": "follows_on_twitter"},
        ],
    })
    assert reachable_persons(g, "owner") == {"owner"}
