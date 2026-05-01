"""Schema dataclasses for the tag-ACL layer (SR-4, SR-13, SR-16).

Two flavors of data live here:

1. **Graph** — generic node/edge structure for both ``shared:family.graph``
   (only principals) and ``shared:topics.graph`` (topics + concerns-edges
   to principals). The same dataclasses serve both because the resolver
   reads them uniformly.
2. **WrappedRecord** — the on-disk shape for tagged memory values. The
   sentinel key/value is the *only* thing that distinguishes a wrapped
   record from a legacy untagged string when the legacy happens to look
   like JSON. SR-4 demands strict equality on both.

Frozen dataclasses + ``from_dict`` constructors keep the runtime small
and the parse path explicit. We don't depend on third-party validators
because errors here must be classified as legacy/wrapped fail-closed,
not raised.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# --- Wrapper sentinel (SR-4) ------------------------------------------------

# The wrapper schema MUST contain {WRAP_SENTINEL_KEY: WRAP_SENTINEL}. Any
# deviation (missing key, wrong literal, non-bool, non-object outer JSON)
# is treated as legacy untagged content. This is *intentional* defense
# against an attacker writing values that look JSON-shaped before this
# feature shipped. Bumping the version is also explicit: rename the
# constant and migrate readers; readers without the new sentinel see
# legacy.
WRAP_SENTINEL_KEY = "__familia_acl_v1"
WRAP_SENTINEL = True


# --- Graph nodes/edges (SR-16) ---------------------------------------------

# Allowed kinds for topic nodes. Persons can be migrated to principals
# later; pets and abstracts cannot.
TOPIC_KINDS = frozenset({"person", "pet", "abstract"})

# Allowed relations for both graphs. Concerns-edges in topics.graph carry
# one of these as ``concerns_as`` so a topic→principal migration knows how
# to translate the edge into family.graph as-is.
ALLOWED_RELATIONS = frozenset({
    "spouse_of",
    "parent_of",
    "owner_of",
    "caregiver_of",
    "guardian_of",
})

# Subset that grants ACL reachability (any direction, except ``parent_of``
# which is asymmetric for ``role: child`` targets — see reachable.py).
ACCESS_RELATIONS = ALLOWED_RELATIONS  # all access-granting in MVP


@dataclass(frozen=True)
class GraphNode:
    """Node in either family.graph or topics.graph."""

    id: str
    type: str  # "principal" | "topic" (in family.graph always "principal")
    display_name: str = ""
    aliases: tuple[str, ...] = ()
    # Only for topics. ``""`` for principals.
    kind: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> GraphNode:
        return cls(
            id=str(raw.get("id", "")),
            type=str(raw.get("type", "principal")),
            display_name=str(raw.get("display_name", "")),
            aliases=tuple(str(a) for a in raw.get("aliases", []) or []),
            kind=str(raw.get("kind", "")),
        )


@dataclass(frozen=True)
class GraphEdge:
    """Edge between two nodes.

    For family.graph the ``rel`` is one of :data:`ALLOWED_RELATIONS` and
    refers to two principals. For topics.graph it's always ``concerns``
    with the actual relation kind in ``concerns_as``.
    """

    src: str
    dst: str
    rel: str
    concerns_as: str = ""  # only set on topics.graph concerns edges

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> GraphEdge:
        # Field name compatibility: legacy graph uses "from"/"to"; some
        # callers may send "src"/"dst". Accept either, prefer "from"/"to"
        # if both present (matches how the existing graph is stored).
        src = raw.get("from") or raw.get("src") or ""
        dst = raw.get("to") or raw.get("dst") or ""
        return cls(
            src=str(src),
            dst=str(dst),
            rel=str(raw.get("rel", "")),
            concerns_as=str(raw.get("concerns_as", "")),
        )


@dataclass(frozen=True)
class Graph:
    """Whole family.graph or topics.graph.

    ``updated_at_ms`` is an etag-like field for reachable-cache
    invalidation (SR-9). Bumped by every CLI write.
    """

    nodes: tuple[GraphNode, ...] = ()
    edges: tuple[GraphEdge, ...] = ()
    updated_at_ms: int = 0

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> Graph:
        if not isinstance(raw, dict):
            # SR-10: corrupt → empty, not raise.
            return cls()
        nodes_raw = raw.get("nodes")
        edges_raw = raw.get("edges")
        if not isinstance(nodes_raw, list):
            nodes_raw = []
        if not isinstance(edges_raw, list):
            edges_raw = []
        nodes: list[GraphNode] = []
        for n in nodes_raw:
            if not isinstance(n, dict):
                continue
            nodes.append(GraphNode.from_dict(n))
        edges: list[GraphEdge] = []
        for e in edges_raw:
            if not isinstance(e, dict):
                continue
            edges.append(GraphEdge.from_dict(e))
        ts = raw.get("updated_at_ms", 0)
        if not isinstance(ts, (int, float)):
            ts = 0
        return cls(nodes=tuple(nodes), edges=tuple(edges), updated_at_ms=int(ts))

    def node(self, node_id: str) -> GraphNode | None:
        for n in self.nodes:
            if n.id == node_id:
                return n
        return None


@dataclass(frozen=True)
class WrappedRecord:
    """In-memory representation of a wrapped value (SR-4).

    The sentinel is implicit (encoder always emits it). The decoder
    constructs this only after validating the sentinel matches; otherwise
    it returns ``None`` and the caller falls back to legacy.
    """

    tags: tuple[str, ...]
    value: str
