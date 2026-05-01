"""resolve_person across both graphs after the migration (A2).

After ``migrate seed-topics-from-subjects``, person-subjects (kid,
parent-in-law) and pets live in topics.graph as concerns-edges to the
remaining principals. ``resolve_person("дочь", from_actor=parent)``
must find the kid topic regardless of which graph holds it.
"""

from __future__ import annotations

from familia.tools.family_graph import (
    KINSHIP_RU,
    _merge_graphs,
    resolve,
)


# ---- merging --------------------------------------------------------------

def test_merge_inverts_concerns_edges_to_family_shape():
    family = {"nodes": [{"id": "owner", "type": "principal"}], "edges": []}
    topics = {
        "nodes": [{"id": "kid", "type": "topic", "kind": "person"}],
        "edges": [
            {"from": "kid", "to": "owner", "rel": "concerns",
             "concerns_as": "parent_of"},
        ],
    }
    merged = _merge_graphs(family, topics)
    # Topic concerns(parent_of) → translated to from=owner, to=kid, rel=parent_of
    edges = merged["edges"]
    assert any(
        e["from"] == "owner" and e["to"] == "kid" and e["rel"] == "parent_of"
        for e in edges
    )


def test_merge_preserves_owner_of_direction():
    family = {"nodes": [{"id": "owner", "type": "principal"}], "edges": []}
    topics = {
        "nodes": [{"id": "pet", "type": "topic", "kind": "pet"}],
        "edges": [
            {"from": "pet", "to": "owner", "rel": "concerns",
             "concerns_as": "owner_of"},
        ],
    }
    merged = _merge_graphs(family, topics)
    assert any(
        e["from"] == "owner" and e["to"] == "pet" and e["rel"] == "owner_of"
        for e in merged["edges"]
    )


def test_merge_skips_concerns_without_concerns_as():
    family = {"nodes": [], "edges": []}
    topics = {
        "nodes": [{"id": "x", "type": "topic"}],
        "edges": [
            {"from": "x", "to": "y", "rel": "concerns"},  # no concerns_as
        ],
    }
    merged = _merge_graphs(family, topics)
    assert all(e["rel"] != "" for e in merged["edges"])


# ---- direct match across both graphs --------------------------------------

def test_direct_match_finds_topic_alias():
    family = {"nodes": [{"id": "owner", "type": "principal"}], "edges": []}
    topics = {
        "nodes": [
            {"id": "varya", "type": "topic", "kind": "person",
             "display_name": "kid", "aliases": ["alias_a"]},
        ],
        "edges": [],
    }
    merged = _merge_graphs(family, topics)
    matches = resolve(merged, "alias_a", from_actor="owner")
    assert [m["id"] for m in matches] == ["varya"]


def test_direct_match_finds_principal_alias():
    family = {
        "nodes": [{"id": "owner", "type": "principal",
                   "aliases": ["bossman"]}],
        "edges": [],
    }
    topics = {"nodes": [], "edges": []}
    merged = _merge_graphs(family, topics)
    matches = resolve(merged, "bossman")
    assert [m["id"] for m in matches] == ["owner"]


# ---- kinship resolves through topics --------------------------------------

def test_child_kinship_resolves_to_topic_post_migration():
    """Critical: after migration, 'дочь' from a parent must find the
    topic-person via the translated concerns edge."""
    family = {
        "nodes": [
            {"id": "owner", "type": "principal"},
            {"id": "spouse", "type": "principal"},
        ],
        "edges": [
            {"from": "owner", "to": "spouse", "rel": "spouse_of"},
        ],
    }
    topics = {
        "nodes": [{"id": "kid", "type": "topic", "kind": "person",
                   "display_name": "Kid"}],
        "edges": [
            {"from": "kid", "to": "owner", "rel": "concerns",
             "concerns_as": "parent_of"},
            {"from": "kid", "to": "spouse", "rel": "concerns",
             "concerns_as": "parent_of"},
        ],
    }
    merged = _merge_graphs(family, topics)
    matches_owner = resolve(merged, "дочь", from_actor="owner")
    matches_spouse = resolve(merged, "дочь", from_actor="spouse")
    assert [m["id"] for m in matches_owner] == ["kid"]
    assert [m["id"] for m in matches_spouse] == ["kid"]


def test_parent_kinship_resolves_via_reverse_concerns():
    """Reverse direction: 'мать' / 'отец' from the topic-person → both
    parents through the inverted concerns edges."""
    family = {
        "nodes": [
            {"id": "p1", "type": "principal"},
            {"id": "p2", "type": "principal"},
        ],
        "edges": [],
    }
    topics = {
        "nodes": [{"id": "kid", "type": "topic", "kind": "person"}],
        "edges": [
            {"from": "kid", "to": "p1", "rel": "concerns",
             "concerns_as": "parent_of"},
            {"from": "kid", "to": "p2", "rel": "concerns",
             "concerns_as": "parent_of"},
        ],
    }
    merged = _merge_graphs(family, topics)
    parents = resolve(merged, "мать", from_actor="kid")
    ids = sorted(m["id"] for m in parents)
    assert ids == ["p1", "p2"]


def test_pet_kinship_resolves_through_topics():
    family = {
        "nodes": [{"id": "owner", "type": "principal"}],
        "edges": [],
    }
    topics = {
        "nodes": [{"id": "pet", "type": "topic", "kind": "pet"}],
        "edges": [
            {"from": "pet", "to": "owner", "rel": "concerns",
             "concerns_as": "owner_of"},
        ],
    }
    merged = _merge_graphs(family, topics)
    matches = resolve(merged, "питомец", from_actor="owner")
    assert [m["id"] for m in matches] == ["pet"]


def test_caregiver_kinship_supported():
    """SR-16 introduced caregiver_of; KINSHIP_RU 'няня' should resolve."""
    assert "няня" in KINSHIP_RU
    family = {
        "nodes": [
            {"id": "kid", "type": "principal"},
            {"id": "n", "type": "principal"},
        ],
        "edges": [
            {"from": "n", "to": "kid", "rel": "caregiver_of"},
        ],
    }
    topics = {"nodes": [], "edges": []}
    merged = _merge_graphs(family, topics)
    matches = resolve(merged, "няня", from_actor="kid")
    assert [m["id"] for m in matches] == ["n"]


# NOTE: parent-in-law (тёща) resolution is intentionally not tested here.
# The current seed-topics convention stores `from=subject, to=person,
# concerns_as=rel` regardless of whether the subject was on the active or
# passive side of the original relation. _merge_graphs swaps endpoints,
# which is correct when subject was the *passive* side (kid for parent_of,
# pet for owner_of) — the typical case. When subject was the *active* side
# (e.g. mother_subject — parent of an adult), the swap inverts the relation
# direction. Resolving "тёща" through that path will return wrong results
# until seed-topics is taught to disambiguate (post-MVP). For now: тёща /
# тесть aliases work via direct alias-match, not kinship traversal.
