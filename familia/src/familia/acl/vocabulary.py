"""Per-actor vocabulary builder for the LLM system prompt (SR-1, SR-9).

The LLM needs to know what tag-ids exist so it can pick relevant ones
when calling ``memory_set`` / ``cron add``. We do NOT inject the entire
namespace — only the subset the actor can reach. Topic *names* are
sensitive (``child_therapy``, ``marital_*``) and must not surface in a
sibling's prompt.

The output is a structured list of ``{id, kind, display_name, aliases,
relation_hint}``. The agent loop (or system-prompt builder) can render
this as plain text however it likes.

Cache invalidation (SR-9): per-actor cache keyed on the ``updated_at_ms``
etag of both graphs. Mid-turn admin edits bump the etags; the next
build sees the new state. There's no time-based TTL — etag is the only
truth.
"""

from __future__ import annotations

from dataclasses import dataclass

from familia.acl.reachable import reachable_persons, reachable_tag_ids
from familia.acl.schema import Graph


# In-memory per-actor cache. Tests can clear by importing _CACHE.clear().
# Process-local — restart wipes. Acceptable for the small reachable sets
# we have (~10-30 ids per actor).
_CACHE: dict[str, "VocabularyCacheEntry"] = {}


@dataclass(frozen=True)
class VocabularyEntry:
    """One renderable line in the LLM-facing vocabulary."""

    id: str
    kind: str  # "principal" | "topic" | unknown
    display_name: str
    aliases: tuple[str, ...]
    # Free-form hint about how this id relates to the viewer ("твой муж",
    # "общий питомец", "связан с varya"). Generated at build time so the
    # LLM doesn't need to traverse graphs itself.
    relation_hint: str


@dataclass(frozen=True)
class VocabularyCacheEntry:
    """Cached build output, keyed on per-graph etags AND viewer state.

    SR-9 says etag is the only truth — but a stale cache must also
    invalidate when the *viewer* changes shape: admin grant got revoked
    mid-turn (admin → member), or the viewer's role set changed. We hash
    those into the key.
    """

    family_etag: int
    topics_etag: int
    is_admin: bool
    role_signature: frozenset[str]  # this actor's own roles snapshot
    entries: tuple[VocabularyEntry, ...]


def clear_cache() -> None:
    """Used by tests; safe in production (just costs one rebuild)."""
    _CACHE.clear()


def build_for(
    actor: str,
    family: Graph,
    topics: Graph,
    principal_roles: dict[str, frozenset[str]] | None = None,
    is_admin: bool = False,
) -> list[VocabularyEntry]:
    """Return the vocabulary the LLM should see for ``actor``'s turn.

    Admin sees everything (SR-1 caveat — admin already has full ACL
    bypass; hiding names from them adds nothing). Non-admins get only
    ids in their ``reachable_tag_ids`` set.
    """
    role_sig = (principal_roles or {}).get(actor, frozenset())
    cached = _CACHE.get(actor)
    if (cached
            and cached.family_etag == family.updated_at_ms
            and cached.topics_etag == topics.updated_at_ms
            and cached.is_admin == is_admin
            and cached.role_signature == role_sig
            and cached.entries):
        return list(cached.entries)

    if is_admin:
        reachable = {n.id for n in family.nodes} | {n.id for n in topics.nodes}
        reachable.add(actor)
    else:
        reachable = reachable_tag_ids(family, topics, actor, principal_roles)

    entries: list[VocabularyEntry] = []
    persons_reachable = (
        reachable_persons(family, actor, principal_roles)
        if not is_admin
        else {n.id for n in family.nodes} | {actor}
    )
    for node in family.nodes:
        if node.id not in reachable:
            continue
        entries.append(VocabularyEntry(
            id=node.id,
            kind="principal",
            display_name=node.display_name or node.id,
            aliases=node.aliases,
            relation_hint=_principal_hint(node.id, actor, family, principal_roles),
        ))
    for node in topics.nodes:
        if node.id not in reachable:
            continue
        entries.append(VocabularyEntry(
            id=node.id,
            kind=f"topic-{node.kind}" if node.kind else "topic",
            display_name=node.display_name or node.id,
            aliases=node.aliases,
            relation_hint=_topic_hint(node.id, persons_reachable, topics),
        ))

    out_tuple = tuple(entries)
    _CACHE[actor] = VocabularyCacheEntry(
        family_etag=family.updated_at_ms,
        topics_etag=topics.updated_at_ms,
        is_admin=is_admin,
        role_signature=role_sig,
        entries=out_tuple,
    )
    return list(out_tuple)


def _principal_hint(
    pid: str,
    actor: str,
    family: Graph,
    roles: dict[str, frozenset[str]] | None,
) -> str:
    if pid == actor:
        return "ты"
    # Find a direct edge between actor and pid for a one-word hint.
    for edge in family.edges:
        if {edge.src, edge.dst} == {actor, pid}:
            rel = edge.rel
            return _relation_hint_ru(rel, src=edge.src, dst=edge.dst, viewer=actor)
    return "член семьи"


def _topic_hint(
    topic_id: str,
    persons_reachable: set[str],
    topics: Graph,
) -> str:
    """Brief description of which connected person makes this topic visible."""
    connections: list[str] = []
    for edge in topics.edges:
        if edge.rel != "concerns":
            continue
        if edge.src != topic_id:
            continue
        if edge.dst in persons_reachable:
            connections.append(edge.dst)
    if not connections:
        return ""
    if len(connections) == 1:
        return f"связан с {connections[0]}"
    return f"связан с {', '.join(connections)}"


def _relation_hint_ru(rel: str, src: str, dst: str, viewer: str) -> str:
    """Map (rel, direction-from-viewer) to a Russian short hint."""
    if rel == "spouse_of":
        return "супруг(а)"
    if rel == "parent_of":
        return "родитель" if dst == viewer else "ребёнок"
    if rel == "owner_of":
        return "хозяин/питомец"
    if rel == "caregiver_of":
        return "опекун/подопечный"
    if rel == "guardian_of":
        return "опекун"
    return rel
