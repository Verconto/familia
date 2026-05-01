"""Reachability resolver across family.graph + topics.graph (SR-2, SR-9, SR-10).

The whole tag-ACL boils down to: given an actor, which set of tag-ids
can they reach? A tag-id is either a principal (in family.graph), a
topic (in topics.graph), or — pre-migration — a subject (still in
family.graph as a transitional artefact). After the seed-topics
migration, subjects are gone and topic-graph is canonical for
non-principals.

Reachability rules (MVP):

* Edges are **bidirectional** for ACL purposes. Spouses see each other,
  parent and child are connected, owner and pet are connected.
* **Exception (SR-2)**: when traversing ``parent_of`` and the edge
  *target* is a principal with ``role: child``, the reverse direction is
  suppressed. Concretely: parent X has reach to child Y; child Y does
  NOT reach parent X. The asymmetry is hard-wired in this resolver, not
  configurable in the graph data, so a misconfigured graph cannot
  accidentally widen a child's view.
* Topics are reached transitively: the actor's reachable persons set
  determines which concerns-edges in topics.graph fire.
* Admin role short-circuits to "everything reachable" — handled by
  callers (admin bypass at the policy layer), not here. This module is
  pure and role-agnostic; admin bypass is a layer above.

Failure modes (SR-10): if either graph is corrupt (parsed via
:func:`Graph.from_dict` which already empties on garbage), the function
returns whatever the empty graphs imply — typically just ``{actor}``.
Never raises.
"""

from __future__ import annotations

from familia.acl.schema import ACCESS_RELATIONS, Graph


def _principal_role(principal_roles: dict[str, frozenset[str]], pid: str) -> frozenset[str]:
    """Convenience accessor; missing principal → empty role set."""
    return principal_roles.get(pid, frozenset())


def reachable_persons(
    family: Graph,
    actor: str,
    principal_roles: dict[str, frozenset[str]] | None = None,
) -> set[str]:
    """Set of person-ids (principal or pre-migration subject) the actor reaches.

    Includes ``actor`` itself.

    ``principal_roles`` — mapping ``principal_id -> frozenset of role names``.
    Used solely to apply the SR-2 child asymmetry. Pass ``{}`` (or ``None``)
    when no roles are configured; the function then degrades to symmetric
    bidirectional traversal, equivalent to the pre-child-role baseline.
    """
    roles = principal_roles or {}
    out: set[str] = {actor}
    # Single-pass over edges; MVP rules are direct (1 hop), no transit.
    # If we ever add transit, switch to BFS keyed on (current_node, hops).
    for edge in family.edges:
        if edge.rel not in ACCESS_RELATIONS:
            continue
        # Direct forward (actor is src)
        if edge.src == actor:
            out.add(edge.dst)
            continue
        # Reverse direction (actor is dst). Suppressed for parent_of when
        # the *destination* (the would-be reach result) is a parent of a
        # role: child principal. Concretely: edge says "parent_of_X →
        # parent_of_Y means X parent_of Y". If actor is Y (the child) and
        # Y has role child → DO NOT add X.
        if edge.dst == actor:
            if edge.rel == "parent_of" and "child" in _principal_role(roles, actor):
                continue
            out.add(edge.src)
    return out


def reachable_topics_for(
    topics: Graph,
    reachable_person_ids: set[str],
) -> set[str]:
    """Topic-ids reached via ``concerns`` edges to any reachable person.

    A concerns-edge points from topic to person (``src=topic, dst=person``).
    A topic is reachable if any of its concerns-edges' destinations is in
    ``reachable_person_ids``. Orphan topics (zero concerns-edges) are
    NOT reachable for non-admins — admin bypass happens upstream.
    """
    out: set[str] = set()
    for edge in topics.edges:
        if edge.rel != "concerns":
            continue
        if edge.dst in reachable_person_ids:
            out.add(edge.src)
    return out


def reachable_tag_ids(
    family: Graph,
    topics: Graph,
    actor: str,
    principal_roles: dict[str, frozenset[str]] | None = None,
) -> set[str]:
    """Combined reachable id set across both graphs, plus actor itself.

    Returns the canonical input to ACL checks: ``record.tags ∩ this_set``.
    Admin bypass is *not* applied here — callers do it explicitly so the
    audit log can record both sets.
    """
    persons = reachable_persons(family, actor, principal_roles)
    topics_set = reachable_topics_for(topics, persons)
    # ``persons`` already includes actor; explicitly union for clarity.
    return persons | topics_set
