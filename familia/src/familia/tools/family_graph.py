"""Family relationship graph: rule-based resolver over shared:family.graph.

Graph is stored as a single JSON object in memX under the key
``shared:family.graph``.  Node schema::

    {"id": "...", "type": "principal"|"subject",
     "display_name": "...", "aliases": [...], "kind": "person"|"pet"}

Edge schema::

    {"from": "<id>", "to": "<id>",
     "rel": "spouse_of"|"parent_of"|"owner_of"}

Relations are asymmetric except ``spouse_of`` (treated as symmetric).
``parent_of`` is stored forward (A is parent of B); "child of" queries
traverse it in reverse.  We intentionally do not encode gender — the
resolver returns all candidates and the LLM picks.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from familia.memx_client import memx_base_url
from familia.principals import get_current_actor, get_registry
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema


GRAPH_KEY = "shared:family.graph"
TOPICS_KEY = "shared:topics.graph"


def _merge_graphs(
    family: dict[str, Any], topics: dict[str, Any]
) -> dict[str, Any]:
    """Combine family + topics into a single resolver-friendly graph.

    Topic ``concerns`` edges are translated into the equivalent
    family-shape edges by swapping endpoints and using ``concerns_as``
    as the rel. Concretely: ``topic kid — concerns(parent_of) → parent``
    becomes ``from=parent, to=kid, rel=parent_of`` — same shape as the
    pre-migration family edge that connected the parent to their child.

    This lets ``_kinship_ids`` (which expects family-shape edges) work
    transparently across both graphs without per-call dispatch logic.
    """
    nodes = list(family.get("nodes") or [])
    nodes.extend(topics.get("nodes") or [])
    edges: list[dict[str, Any]] = list(family.get("edges") or [])
    for e in topics.get("edges") or []:
        if not isinstance(e, dict):
            continue
        if e.get("rel") != "concerns":
            edges.append(e)
            continue
        concerns_as = e.get("concerns_as", "")
        if not concerns_as:
            continue
        # Swap: concerns edge goes (topic → person), but the equivalent
        # family relation is (person → topic) for parent_of / owner_of /
        # guardian_of / caregiver_of / spouse_of.
        edges.append({
            "from": e.get("to"),
            "to": e.get("from"),
            "rel": concerns_as,
        })
    return {"nodes": nodes, "edges": edges}


# Russian kinship lexicon → abstract relation kind.
KINSHIP_RU: dict[str, str] = {
    "муж": "spouse",
    "жена": "spouse",
    "супруг": "spouse",
    "супруга": "spouse",
    "мать": "parent",
    "мама": "parent",
    "отец": "parent",
    "папа": "parent",
    "родитель": "parent",
    "дочь": "child",
    "сын": "child",
    "ребёнок": "child",
    "ребенок": "child",
    "дети": "child",
    "тёща": "parent_in_law",
    "теща": "parent_in_law",
    "тесть": "parent_in_law",
    "свекровь": "parent_in_law",
    "свёкор": "parent_in_law",
    "свекор": "parent_in_law",
    "пёс": "pet",
    "пес": "pet",
    "собака": "pet",
    "кот": "pet",
    "кошка": "pet",
    "питомец": "pet",
    "няня": "caregiver",
    "опекун": "caregiver",
    "сиделка": "caregiver",
}


def _node_by_id(graph: dict[str, Any], node_id: str) -> dict[str, Any] | None:
    for n in graph.get("nodes", []):
        if n.get("id") == node_id:
            return n
    return None


def _direct_match(graph: dict[str, Any], q: str) -> list[dict[str, Any]]:
    q = q.strip().lower()
    if not q:
        return []
    out: list[dict[str, Any]] = []
    for n in graph.get("nodes", []):
        names = [n.get("display_name", "").lower()]
        names.extend(a.lower() for a in n.get("aliases", []) or [])
        names.append(n.get("id", "").lower())
        if q in [s for s in names if s]:
            out.append(n)
    return out


def _edges_with(graph: dict[str, Any], **filters: str) -> list[dict[str, Any]]:
    return [
        e for e in graph.get("edges", [])
        if all(e.get(k) == v for k, v in filters.items())
    ]


def _kinship_ids(graph: dict[str, Any], from_actor: str, kind: str) -> list[str]:
    """Return node ids related to ``from_actor`` by abstract ``kind``."""
    out: list[str] = []
    if kind == "spouse":
        for e in _edges_with(graph, rel="spouse_of"):
            if e["from"] == from_actor:
                out.append(e["to"])
            elif e["to"] == from_actor:
                out.append(e["from"])
    elif kind == "parent":
        out.extend(e["from"] for e in _edges_with(graph, rel="parent_of", to=from_actor))
    elif kind == "child":
        out.extend(e["to"] for e in _edges_with(graph, rel="parent_of", **{"from": from_actor}))
    elif kind == "parent_in_law":
        for spouse_id in _kinship_ids(graph, from_actor, "spouse"):
            out.extend(_kinship_ids(graph, spouse_id, "parent"))
    elif kind == "pet":
        out.extend(e["to"] for e in _edges_with(graph, rel="owner_of", **{"from": from_actor}))
    elif kind == "caregiver":
        # Bidirectional: nanny is connected to a person via caregiver_of.
        # KINSHIP_RU "няня" is asked from the perspective of the person being
        # cared for, so we walk both directions.
        for e in _edges_with(graph, rel="caregiver_of"):
            if e.get("from") == from_actor:
                out.append(e["to"])
            elif e.get("to") == from_actor:
                out.append(e["from"])
    seen: set[str] = set()
    result: list[str] = []
    for nid in out:
        if nid not in seen:
            seen.add(nid)
            result.append(nid)
    return result


def resolve(
    graph: dict[str, Any],
    query: str,
    from_actor: str | None = None,
) -> list[dict[str, Any]]:
    """Return list of node dicts matching ``query``.  Empty list = no match.

    Resolution order:
      1. Direct match against ``display_name``, ``aliases``, or ``id``.
      2. Kinship term (requires ``from_actor``) via the abstract relations
         above.
    """
    direct = _direct_match(graph, query)
    if direct:
        return direct
    q = query.strip().lower()
    kind = KINSHIP_RU.get(q)
    if kind and from_actor:
        ids = _kinship_ids(graph, from_actor, kind)
        return [n for n in (_node_by_id(graph, i) for i in ids) if n is not None]
    return []


def _enrich(node: dict[str, Any]) -> dict[str, Any]:
    """Attach channel info for principals from the registry."""
    out = {
        "id": node.get("id"),
        "type": node.get("type"),
        "display_name": node.get("display_name"),
    }
    if "kind" in node:
        out["kind"] = node["kind"]
    if node.get("type") == "principal":
        p = get_registry().get(node["id"])
        if p is not None and p.identities:
            out["channels"] = {ident.channel: ident.sender_id for ident in p.identities}
    return out


@tool_parameters(
    tool_parameters_schema(
        query=StringSchema(
            "Name, alias, or kinship term to resolve. Examples: a person's "
            "name or a Russian kinship term ('жена', 'мать', 'дочь', 'тёща', "
            "'пёс'). For kinship terms, pass from_actor so the tool knows "
            "whose relatives to return."
        ),
        from_actor=StringSchema(
            "Principal id whose perspective the kinship term is anchored to "
            "(e.g. 'owner', 'member_a'). Omit for direct name lookups. "
            "Defaults to the current actor.",
            nullable=True,
        ),
        required=["query"],
    )
)
class ResolvePersonTool(Tool):
    """Resolve a name or kinship term to family-graph nodes."""

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url_override = base_url

    @property
    def _base_url(self) -> str:
        return self._base_url_override or memx_base_url()

    @property
    def name(self) -> str:
        return "resolve_person"

    @property
    def description(self) -> str:
        return (
            "Resolve a name or Russian kinship term ('жена', 'мать', 'дочь', 'тёща', "
            "'пёс', ...) to one or more family-graph nodes.  Returns a JSON list: "
            "each item has id, type, display_name, and (for principals) channels. "
            "Empty list means no match — don't invent people. Multiple matches "
            "mean ambiguity — ask the user or pick the most plausible."
        )

    @property
    def read_only(self) -> bool:
        return True

    async def _fetch_one(
        self, api_key: str, key: str, *, required: bool = False,
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Fetch a single graph by key. ``required`` controls whether 404 is
        an error (family.graph) or a benign empty (topics.graph)."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(
                    f"{self._base_url}/get",
                    headers={"x-api-key": api_key},
                    params={"key": key},
                )
        except httpx.HTTPError as exc:
            return None, f"Error: memX unreachable ({type(exc).__name__}: {exc})"
        if r.status_code == 403:
            return None, f"Error: access denied by memX ACL for '{key}'"
        if r.status_code == 404:
            if required:
                return None, f"Error: graph not seeded at '{key}'"
            return {"nodes": [], "edges": []}, None
        if r.status_code >= 400:
            return None, f"Error: memX {r.status_code}: {r.text[:200]}"
        try:
            payload = r.json()
        except ValueError:
            return None, "Error: memX returned non-JSON"
        if payload is None:
            return {"nodes": [], "edges": []}, None
        value = payload.get("value", payload) if isinstance(payload, dict) else payload
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                # SR-10: fail-closed treats malformed graph as empty rather
                # than raising. Caller still sees an error if family.graph
                # was required and absent.
                return {"nodes": [], "edges": []}, None
        if not isinstance(value, dict):
            return {"nodes": [], "edges": []}, None
        return value, None

    async def execute(
        self,
        query: str,
        from_actor: str | None = None,
        **kwargs: Any,
    ) -> str:
        actor_id = get_current_actor()
        if not actor_id:
            return "Error: no actor in context"
        principal = get_registry().get(actor_id)
        if principal is None or not principal.memx_key:
            return f"Error: principal '{actor_id}' has no memx_key"
        if from_actor is None:
            from_actor = actor_id
        family, err = await self._fetch_one(principal.memx_key, GRAPH_KEY, required=True)
        if err or family is None:
            return err or "Error: family graph unavailable"
        topics, err = await self._fetch_one(principal.memx_key, TOPICS_KEY)
        if err:
            # topics is optional in this codepath — degrade to family-only
            # search (SR-10 fail-closed: don't raise; just lose topics).
            topics = {"nodes": [], "edges": []}
        merged = _merge_graphs(family, topics)
        matches = resolve(merged, query, from_actor)
        enriched = [_enrich(n) for n in matches]
        return json.dumps(enriched, ensure_ascii=False)
