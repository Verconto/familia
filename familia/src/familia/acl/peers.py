"""Peer-relationship resolver for the policy engine.

Given two principal ids, decide whether they are *peers* — i.e. equal
adults whose access to each other should be symmetric and complete
(memory, reminders, paired/private scopes). The current rule is
narrow on purpose so simply adding a child principal later does NOT
silently grant the child parent-level access:

* The family graph (``shared:family.graph``) must contain an edge
  between the two ids whose ``rel`` is one of ``PEER_RELATIONS``
  (``spouse_of`` or ``guardian_of``). Direction does not matter; the
  semantics are symmetric.
* Neither side may have ``role: child`` in ``principals.json``.
* Self-checks are False (``actor == target``).

The result is cached per ``(actor, target)`` pair, keyed by the
graph's ``updated_at_ms``, so repeated policy checks within one turn
don't hit memX. The cache busts automatically on the next
``familia graph person add-edge|remove-edge`` because that bumps
``updated_at_ms``.

Failure modes are deliberately silent — the policy engine treats a
False return as "no peer relationship", which is the safer default
under the project-wide fail-closed convention (SR-10).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from loguru import logger

PEER_RELATIONS: frozenset[str] = frozenset({"spouse_of", "guardian_of"})

# How long we trust a previously-loaded family-graph probe before
# re-fetching from memX. The probe load is a synchronous HTTP call —
# without a TTL, every policy check on a peer pattern would hit memX,
# which is unfriendly to the asyncio event loop. With ``PROBE_TTL_MS``
# at a few seconds the worst case is one HTTP call per few seconds
# even under bursts; correctness is preserved because graph edits
# bump ``updated_at_ms`` and the cache busts on key mismatch.
PROBE_TTL_MS = 5_000

_lock = threading.Lock()
# Per-(kind, actor, target) decision cache, keyed by
# ``probe.updated_at_ms`` so any graph edit invalidates entries
# automatically. ``kind`` distinguishes the two predicates that share
# this cache: ``"peer"`` (is_peer) and ``"family"`` (is_family_member).
_cache: dict[tuple[str, str, str], tuple[int, bool]] = {}
# Most recent probe + the wall-clock time it was loaded; reused while
# fresh to avoid repeating the HTTP call.
_probe_state: dict[str, object] = {"probe": None, "loaded_at_ms": 0}


@dataclass(frozen=True)
class _Probe:
    """Minimal view of the data we need from the family graph + registry.

    Pulled out so the lookup path is easy to reason about: get a Probe,
    answer the question. Loading is best-effort: any exception ⇒ a
    Probe whose ``edges`` is empty and ``children`` is empty, which
    forces ``is_peer`` to False without crashing the caller.
    """

    updated_at_ms: int
    edges: tuple[tuple[str, str, str], ...]  # (from, to, rel)
    children: frozenset[str]  # principal ids with role=child


def _load_probe() -> _Probe:
    try:
        from familia.acl import graph_io
        from familia import principals as principals_mod

        graph = graph_io.load_graph_value("shared:family.graph")
        edges_raw = graph.get("edges") or []
        edges: list[tuple[str, str, str]] = []
        for e in edges_raw:
            if not isinstance(e, dict):
                continue
            f = e.get("from")
            t = e.get("to")
            rel = e.get("rel") or e.get("concerns_as") or ""
            if isinstance(f, str) and isinstance(t, str) and isinstance(rel, str):
                edges.append((f, t, rel))

        registry = principals_mod.get_registry()
        children: set[str] = set()
        for pid in registry.ids:
            p = registry.get(pid)
            if p and "child" in p.roles:
                children.add(pid)

        return _Probe(
            updated_at_ms=int(graph.get("updated_at_ms") or 0),
            edges=tuple(edges),
            children=frozenset(children),
        )
    except Exception as exc:  # noqa: BLE001 — fail-closed
        logger.warning("acl.peers: probe load failed, treating as no-peers: {}", exc)
        return _Probe(updated_at_ms=0, edges=(), children=frozenset())


def _get_probe() -> _Probe:
    """Cached probe accessor. TTL-bounded so we don't hit memX on every
    policy check. The decision cache that wraps this also keys on
    ``updated_at_ms`` so a stale-but-still-fresh probe doesn't hide a
    just-applied graph edit beyond the TTL window.
    """
    now_ms = int(time.time() * 1000)
    with _lock:
        existing = _probe_state.get("probe")
        loaded_at = int(_probe_state.get("loaded_at_ms") or 0)
        if existing is not None and now_ms - loaded_at < PROBE_TTL_MS:
            return existing  # type: ignore[return-value]

    probe = _load_probe()
    with _lock:
        _probe_state["probe"] = probe
        _probe_state["loaded_at_ms"] = now_ms
    return probe


def is_peer(actor: str | None, target: str) -> bool:
    """True iff ``actor`` and ``target`` are peer principals.

    See module docstring for the exact rule. ``actor`` may be ``None``
    (system turn) — never a peer.
    """
    if not actor or not target or actor == target:
        return False

    probe = _get_probe()

    # Children are excluded from the peer rule, even with a matching
    # edge. This keeps "any family edge ⇒ full visibility" from
    # silently regressing to "child sees parents' private memory".
    # The check fires BEFORE the cache lookup so a role flip in
    # principals.json takes effect on the next call (registry is
    # re-read inside _load_probe; the decision cache keys on the
    # graph's updated_at_ms only).
    if actor in probe.children or target in probe.children:
        return False

    cache_key = ("peer", actor, target)
    with _lock:
        cached = _cache.get(cache_key)
        if cached is not None and cached[0] == probe.updated_at_ms:
            return cached[1]

    found = False
    for f, t, rel in probe.edges:
        if rel not in PEER_RELATIONS:
            continue
        if (f == actor and t == target) or (f == target and t == actor):
            found = True
            break

    with _lock:
        _cache[cache_key] = (probe.updated_at_ms, found)
    return found


def is_family_member(actor: str | None, target: str) -> bool:
    """True iff ``actor`` and ``target`` share ANY edge in the family graph.

    Looser than :func:`is_peer`: any ``rel`` counts (``spouse_of``,
    ``guardian_of``, ``parent_of``, ``sibling_of``, …), and the
    ``role: child`` exclusion is NOT applied. Used by the context
    builder to surface ``shared:`` keys across family ties — a child
    needs to see their mother's shared-index entries even though the
    child is *not* a peer for private-memory purposes.

    Self-checks return False; ``actor=None`` always returns False.
    Cached the same way as :func:`is_peer`, keyed on the graph's
    ``updated_at_ms`` so a graph edit busts entries automatically.
    """
    if not actor or not target or actor == target:
        return False

    probe = _get_probe()
    cache_key = ("family", actor, target)
    with _lock:
        cached = _cache.get(cache_key)
        if cached is not None and cached[0] == probe.updated_at_ms:
            return cached[1]

    found = False
    for f, t, _rel in probe.edges:
        if (f == actor and t == target) or (f == target and t == actor):
            found = True
            break

    with _lock:
        _cache[cache_key] = (probe.updated_at_ms, found)
    return found


def reset_cache() -> None:
    """Test hook + safety valve for reload paths."""
    with _lock:
        _cache.clear()
        _probe_state["probe"] = None
        _probe_state["loaded_at_ms"] = 0
