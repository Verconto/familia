"""CLI graph admin smoke tests with mocked memX I/O.

These tests don't go to network. They patch ``set_raw``/``load_graph_value``
so each test starts with a known graph state, runs a CLI ``main()`` call
with crafted argv, and inspects the saved-state side effect.

Covers SR-7, SR-8, SR-15, SR-16, SR-17, SR-18 enforcement at the CLI
boundary.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest

from familia.cli import graph_admin


@pytest.fixture
def fake_store():
    """Returns (store_dict, save_calls) — store backs load_graph_value
    and save_graph appends to save_calls (key, value) tuples."""
    store: dict[str, dict[str, Any]] = {
        graph_admin.FAMILY_KEY: {"nodes": [], "edges": [], "updated_at_ms": 0},
        graph_admin.TOPICS_KEY: {"nodes": [], "edges": [], "updated_at_ms": 0},
    }
    save_calls: list[tuple[str, dict]] = []

    def fake_load(key: str, *, api_key=None) -> dict[str, Any]:
        return json.loads(json.dumps(store.get(key, {"nodes": [], "edges": []})))

    def fake_set(key: str, value: Any, *, api_key=None) -> None:
        store[key] = json.loads(json.dumps(value))
        save_calls.append((key, store[key]))

    with patch.object(graph_admin, "load_graph_value", side_effect=fake_load), \
         patch.object(graph_admin, "set_raw", side_effect=fake_set):
        yield store, save_calls


def _run(argv: list[str]) -> int:
    return graph_admin.main(argv)


# ---- topic add-node ---------------------------------------------------------

def test_topic_add_node_persists(fake_store):
    store, _ = fake_store
    rc = _run(["graph", "topic", "add-node", "school",
               "--kind", "abstract", "--display-name", "Школа",
               "--aliases", "школу", "уроки"])
    assert rc == 0
    nodes = store[graph_admin.TOPICS_KEY]["nodes"]
    assert len(nodes) == 1
    assert nodes[0]["id"] == "school"
    assert nodes[0]["kind"] == "abstract"
    assert nodes[0]["aliases"] == ["школу", "уроки"]


def test_topic_add_node_rejects_kinship_alias(fake_store):
    """SR-18: aliases cannot be kinship terms."""
    rc = _run(["graph", "topic", "add-node", "kid",
               "--kind", "person", "--aliases", "дочь"])
    assert rc == 2  # GraphIOError


def test_topic_add_node_rejects_id_collision_with_existing_topic(fake_store):
    """SR-8: id-uniqueness."""
    store, _ = fake_store
    store[graph_admin.TOPICS_KEY]["nodes"].append(
        {"id": "school", "type": "topic", "kind": "abstract"}
    )
    rc = _run(["graph", "topic", "add-node", "school",
               "--kind", "abstract"])
    assert rc == 2


def test_topic_add_node_rejects_id_collision_with_principal(fake_store):
    """SR-8: even cross-graph collisions blocked."""
    store, _ = fake_store
    store[graph_admin.FAMILY_KEY]["nodes"].append(
        {"id": "owner", "type": "principal"}
    )
    rc = _run(["graph", "topic", "add-node", "owner",
               "--kind", "abstract"])
    assert rc == 2


def test_topic_add_node_invalid_kind_rejected(fake_store):
    """argparse kind validation runs before our code; exits 2 hard."""
    with pytest.raises(SystemExit) as exc:
        _run(["graph", "topic", "add-node", "x", "--kind", "concept"])
    assert exc.value.code == 2


# ---- topic add-edge --------------------------------------------------------

def test_topic_add_edge_requires_concerns_as(fake_store):
    """SR-16: concerns_as is required by argparse."""
    store, _ = fake_store
    store[graph_admin.FAMILY_KEY]["nodes"].append({"id": "owner", "type": "principal"})
    store[graph_admin.TOPICS_KEY]["nodes"].append(
        {"id": "school", "type": "topic", "kind": "abstract"}
    )
    with pytest.raises(SystemExit):  # argparse hard-fails on missing required
        _run(["graph", "topic", "add-edge", "school", "owner"])


def test_topic_add_edge_persists_with_concerns_as(fake_store):
    """SR-16: edge stored carries concerns_as field."""
    store, _ = fake_store
    store[graph_admin.FAMILY_KEY]["nodes"].append({"id": "owner", "type": "principal"})
    store[graph_admin.TOPICS_KEY]["nodes"].append(
        {"id": "school", "type": "topic", "kind": "abstract"}
    )
    rc = _run(["graph", "topic", "add-edge", "school",
               "--concerns-as", "guardian_of", "owner"])
    assert rc == 0
    edges = store[graph_admin.TOPICS_KEY]["edges"]
    assert len(edges) == 1
    assert edges[0]["from"] == "school"
    assert edges[0]["to"] == "owner"
    assert edges[0]["concerns_as"] == "guardian_of"


def test_topic_add_edge_unknown_person_rejected(fake_store):
    store, _ = fake_store
    store[graph_admin.TOPICS_KEY]["nodes"].append(
        {"id": "school", "type": "topic", "kind": "abstract"}
    )
    rc = _run(["graph", "topic", "add-edge", "school",
               "--concerns-as", "guardian_of", "ghost"])
    assert rc == 2


# ---- person add-edge --------------------------------------------------------

def test_person_add_edge_requires_existing_endpoints(fake_store):
    rc = _run(["graph", "person", "add-edge", "owner", "spouse_of", "member_a"])
    assert rc == 2


def test_person_add_edge_creates(fake_store):
    store, _ = fake_store
    store[graph_admin.FAMILY_KEY]["nodes"] = [
        {"id": "owner", "type": "principal"},
        {"id": "member_a", "type": "principal"},
    ]
    rc = _run(["graph", "person", "add-edge", "owner", "spouse_of", "member_a"])
    assert rc == 0
    assert len(store[graph_admin.FAMILY_KEY]["edges"]) == 1


# ---- updated_at_ms etag bumped ---------------------------------------------

def test_updated_at_ms_changes_on_write(fake_store):
    store, _ = fake_store
    store[graph_admin.TOPICS_KEY]["updated_at_ms"] = 0
    rc = _run(["graph", "topic", "add-node", "x", "--kind", "abstract"])
    assert rc == 0
    assert store[graph_admin.TOPICS_KEY]["updated_at_ms"] > 0


# ---- migrate seed-topics-from-subjects (SR-15) -----------------------------

def test_seed_topics_dry_run_changes_nothing(fake_store):
    store, _ = fake_store
    store[graph_admin.FAMILY_KEY]["nodes"] = [
        {"id": "owner", "type": "principal"},
        {"id": "varya", "type": "subject", "kind": "person",
         "aliases": ["alias_a"]},
    ]
    store[graph_admin.FAMILY_KEY]["edges"] = [
        {"from": "owner", "to": "varya", "rel": "parent_of"},
    ]
    rc = _run(["migrate", "seed-topics-from-subjects"])  # dry-run default
    assert rc == 0
    # Subject still in family.graph; topics.graph untouched
    assert any(n["id"] == "varya"
               for n in store[graph_admin.FAMILY_KEY]["nodes"])
    assert store[graph_admin.TOPICS_KEY]["nodes"] == []


def test_seed_topics_apply_moves_subject(fake_store):
    store, _ = fake_store
    store[graph_admin.FAMILY_KEY]["nodes"] = [
        {"id": "owner", "type": "principal"},
        {"id": "varya", "type": "subject", "kind": "person",
         "aliases": ["alias_a"]},
        {"id": "syava", "type": "subject", "kind": "pet",
         "aliases": ["alias_b"]},
    ]
    store[graph_admin.FAMILY_KEY]["edges"] = [
        {"from": "owner", "to": "varya", "rel": "parent_of"},
        {"from": "owner", "to": "syava", "rel": "owner_of"},
    ]
    rc = _run(["migrate", "seed-topics-from-subjects", "--apply"])
    assert rc == 0
    family_ids = [n["id"] for n in store[graph_admin.FAMILY_KEY]["nodes"]]
    assert "owner" in family_ids
    assert "varya" not in family_ids
    assert "syava" not in family_ids
    topic_ids = [n["id"] for n in store[graph_admin.TOPICS_KEY]["nodes"]]
    assert sorted(topic_ids) == ["syava", "varya"]
    # concerns edges with proper concerns_as
    edges = store[graph_admin.TOPICS_KEY]["edges"]
    by_topic = {e["from"]: e for e in edges}
    assert by_topic["varya"]["concerns_as"] == "parent_of"
    assert by_topic["syava"]["concerns_as"] == "owner_of"


def test_seed_topics_idempotent(fake_store):
    """Re-run after a successful apply: no-op, exit 0."""
    store, _ = fake_store
    store[graph_admin.FAMILY_KEY]["nodes"] = [{"id": "owner", "type": "principal"}]
    store[graph_admin.TOPICS_KEY]["nodes"] = [
        {"id": "varya", "type": "topic", "kind": "person", "aliases": ["alias_a"]}
    ]
    rc = _run(["migrate", "seed-topics-from-subjects", "--apply"])
    assert rc == 0


# ---- migrate topic-to-principal (SR-17) ------------------------------------

def test_topic_to_principal_dry_run(fake_store):
    store, _ = fake_store
    store[graph_admin.FAMILY_KEY]["nodes"] = [
        {"id": "owner", "type": "principal"},
        {"id": "member_a", "type": "principal"},
    ]
    store[graph_admin.TOPICS_KEY]["nodes"] = [
        {"id": "varya", "type": "topic", "kind": "person", "aliases": ["alias_a"]}
    ]
    store[graph_admin.TOPICS_KEY]["edges"] = [
        {"from": "varya", "to": "owner", "rel": "concerns",
         "concerns_as": "parent_of"},
        {"from": "varya", "to": "member_a", "rel": "concerns",
         "concerns_as": "parent_of"},
    ]
    rc = _run(["migrate", "topic-to-principal", "varya"])  # dry default
    assert rc == 0
    # nothing changed
    assert any(n["id"] == "varya"
               for n in store[graph_admin.TOPICS_KEY]["nodes"])


def test_topic_to_principal_apply_atomic(fake_store):
    store, _ = fake_store
    store[graph_admin.FAMILY_KEY]["nodes"] = [
        {"id": "owner", "type": "principal"},
        {"id": "member_a", "type": "principal"},
    ]
    store[graph_admin.TOPICS_KEY]["nodes"] = [
        {"id": "varya", "type": "topic", "kind": "person", "aliases": ["alias_a"]}
    ]
    store[graph_admin.TOPICS_KEY]["edges"] = [
        {"from": "varya", "to": "owner", "rel": "concerns",
         "concerns_as": "parent_of"},
        {"from": "varya", "to": "member_a", "rel": "concerns",
         "concerns_as": "parent_of"},
    ]
    rc = _run(["migrate", "topic-to-principal", "varya", "--apply"])
    assert rc == 0
    family_ids = [n["id"] for n in store[graph_admin.FAMILY_KEY]["nodes"]]
    assert "varya" in family_ids
    family_edges = store[graph_admin.FAMILY_KEY]["edges"]
    rels = sorted(
        (e["from"], e["rel"], e["to"]) for e in family_edges
    )
    assert ("member_a", "parent_of", "varya") in rels
    assert ("owner", "parent_of", "varya") in rels
    # topic gone
    assert all(n["id"] != "varya"
               for n in store[graph_admin.TOPICS_KEY]["nodes"])


def test_topic_to_principal_refuses_non_person_kind(fake_store):
    store, _ = fake_store
    store[graph_admin.TOPICS_KEY]["nodes"] = [
        {"id": "syava", "type": "topic", "kind": "pet", "aliases": ["alias_b"]}
    ]
    rc = _run(["migrate", "topic-to-principal", "syava", "--apply"])
    assert rc == 2  # cannot migrate pet
