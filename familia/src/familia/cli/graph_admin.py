"""``familia`` CLI: graph admin + migrations (SR-7, SR-8, SR-14..SR-18).

Reads/writes ``shared:family.graph`` and ``shared:topics.graph`` directly
through :mod:`familia.acl.graph_io`, bypassing the chat-side
``MemorySetTool`` (which is policy-denied for these keys). Every
mutation:

* validates schema invariants (id uniqueness, kinship-aliases banned,
  required ``concerns_as``, etc.);
* bumps ``updated_at_ms`` so the per-actor reachable cache invalidates
  next time it's consulted (SR-9);
* emits a ``graph_edit`` audit event with the diff.

Subcommands (argparse):

* ``graph print`` — dump both graphs.
* ``graph person add-edge|remove-edge`` — manipulate family.graph.
* ``graph person add-node|remove-node`` — limited; use migrations for
  most cases.
* ``graph topic add-node|remove-node`` — manipulate topics.graph.
* ``graph topic add-edge|remove-edge`` — concerns edges.
* ``migrate seed-topics-from-subjects`` — one-shot move of legacy
  subject nodes from family.graph into topics.graph.
* ``migrate topic-to-principal`` — atomic promotion of a topic-person
  to a principal (when a kid grows up and gets a phone, etc.).

Exit codes: ``0`` ok, ``2`` bad args / validation, ``1`` runtime error.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from loguru import logger

from familia import audit
from familia.acl.graph_io import GraphIOError, load_graph_value, set_raw
from familia.acl.schema import (
    ALLOWED_RELATIONS,
    TOPIC_KINDS,
)


FAMILY_KEY = "shared:family.graph"
TOPICS_KEY = "shared:topics.graph"

# Channels eligible for runtime config-add via the admin app. Restricted
# to those that don't need a public endpoint (long-poll, IMAP-poll, or
# persistent-socket adapters).
# WeXin (微信 Official Account) was dropped 2026-04-28: production mode
# requires a public callback URL (incompatible with our poll-only design),
# and sandbox mode dies whenever the admin logs out of mp.weixin.qq.com —
# both fail the "works for non-technical owner without ops" bar. Re-add
# when we have a public-domain story.
POLL_CHANNEL_KINDS = frozenset({
    "telegram", "vk", "discord", "slack", "matrix",
    "email", "dingtalk", "feishu", "qq", "wecom", "mochat",
})

# Speech-to-Text providers we support. ``off`` and ``inherit`` are
# control words used by the per-channel override (see ``channels set-stt``).
# Only the names in ``STT_CRED_PROVIDERS`` actually own credentials in
# config.providers — ``off`` and ``inherit`` are bookkeeping values.
STT_CRED_PROVIDERS = frozenset({"groq", "openai", "yandex"})
STT_PROVIDER_CHOICES = frozenset({"off", "inherit", *STT_CRED_PROVIDERS})


# Mapping channel kind → import-name + pip-spec for runtime extras
# install. Channels not listed here ship with no extras (use stdlib /
# nanobot core deps).
CHANNEL_DEPS: dict[str, tuple[str, str]] = {
    "discord":  ("discord",          "discord.py"),
    "slack":    ("slack_sdk",        "slack-sdk"),
    "matrix":   ("nio",              "matrix-nio"),
    "dingtalk": ("dingtalk_stream",  "dingtalk-stream"),
    "feishu":   ("lark_oapi",        "lark-oapi"),
    "qq":       ("botpy",            "qq-botpy"),
    "wecom":    ("wecom_aibot_sdk",  "wecom-aibot-sdk"),
    "mochat":   ("socketio",         "python-socketio"),
}

# SR-18: kinship terms must be resolved contextually via KINSHIP_RU, never
# hard-coded as a node alias. CLI rejects on add.
try:
    from familia.tools.family_graph import KINSHIP_RU
    _KINSHIP_TERMS = frozenset(KINSHIP_RU.keys())
except Exception:  # pragma: no cover - tool import only fails in odd setups
    _KINSHIP_TERMS = frozenset()


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _now_ms() -> int:
    return int(time.time() * 1000)


def _all_known_ids(family: dict[str, Any], topics: dict[str, Any]) -> set[str]:
    """Union of every node id across both graphs and principals registry.

    Used for SR-8 uniqueness checks. Principals registry is consulted
    lazily so the CLI works in test fixtures with no registry.
    """
    out: set[str] = set()
    for n in family.get("nodes", []):
        if isinstance(n, dict) and isinstance(n.get("id"), str):
            out.add(n["id"])
    for n in topics.get("nodes", []):
        if isinstance(n, dict) and isinstance(n.get("id"), str):
            out.add(n["id"])
    try:
        from familia.principals import get_registry
        out |= set(get_registry().ids)
    except Exception:
        pass
    return out


def _validate_aliases(aliases: list[str]) -> None:
    """SR-18: refuse aliases that are kinship terms."""
    bad = [a for a in aliases if a.strip().lower() in _KINSHIP_TERMS]
    if bad:
        raise GraphIOError(
            f"aliases contain kinship terms {bad}; kinship is resolved "
            "contextually via KINSHIP_RU and must not be hard-coded as alias"
        )


def _node_index(graph: dict[str, Any], node_id: str) -> int | None:
    for i, n in enumerate(graph.get("nodes", [])):
        if isinstance(n, dict) and n.get("id") == node_id:
            return i
    return None


def _edge_match(e: dict[str, Any], src: str, dst: str, rel: str) -> bool:
    if not isinstance(e, dict):
        return False
    return (
        e.get("from") == src
        and e.get("to") == dst
        and e.get("rel") == rel
    )


def _save_graph(key: str, data: dict[str, Any]) -> None:
    data["updated_at_ms"] = _now_ms()
    set_raw(key, data)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def _audit_edit(op: str, target_key: str, **fields: Any) -> None:
    audit.log_event(
        "graph_edit",
        op=op,
        target_key=target_key,
        cli_user=_caller_user(),
        **fields,
    )


def _caller_user() -> str:
    import getpass
    try:
        return getpass.getuser()
    except Exception:
        return "?"


# ---------------------------------------------------------------------------
# JSON emit helpers (S0 — admin panel contract)
# ---------------------------------------------------------------------------

# All --json outputs carry schema_version so the admin panel can detect
# protocol drift (CLI newer than admin panel) and degrade gracefully.
SCHEMA_VERSION = 1


def _emit_json(data: dict[str, Any]) -> None:
    """Print a structured payload to stdout. Always prefixed with
    ``schema_version`` so the consumer can detect drift.

    The actual print uses ``ensure_ascii=False`` so Cyrillic display
    names round-trip. Newlines added explicitly so tail-style consumers
    can split safely.
    """
    payload = {"schema_version": SCHEMA_VERSION, **data}
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, default=str))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _emit_error_json(message: str, code: str = "ERROR") -> None:
    """Errors go to stderr as a single-line JSON. stdout stays clean for
    pipelines that pipe `--json` output through `jq`/admin parsers."""
    sys.stderr.write(json.dumps(
        {"schema_version": SCHEMA_VERSION, "error": message, "code": code},
        ensure_ascii=False,
    ))
    sys.stderr.write("\n")
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# health / version (top-level admin-panel reads)
# ---------------------------------------------------------------------------

def _familia_version() -> str:
    """Best-effort: read package version from importlib.metadata."""
    try:
        from importlib.metadata import version
        return version("familia")
    except Exception:
        return "unknown"


def cmd_version(args: argparse.Namespace) -> int:
    """Emit version info — used by admin panel to detect CLI drift."""
    if getattr(args, "json", False):
        _emit_json({"version": _familia_version()})
        return 0
    print(f"familia {_familia_version()}")
    return 0


def cmd_health(args: argparse.Namespace) -> int:
    """Health snapshot for the admin panel dashboard.

    Probes:
      * memX reachability (via the family graph fetch).
      * principals.json loaded (count).
      * topics graph node count.
      * familia version (for drift detection).

    A degraded but non-failing memX still returns rc=0 with status fields
    so the panel can render specifically-which-thing-is-down.
    """
    info: dict[str, Any] = {
        "version": _familia_version(),
        "memx": "ok",
        "principals_count": 0,
        "topics_count": 0,
        "family_nodes": 0,
        "topics_nodes": 0,
    }
    try:
        from familia.principals import get_registry
        info["principals_count"] = len(get_registry().ids)
    except Exception as exc:
        info["principals_error"] = str(exc)
    try:
        family = load_graph_value(FAMILY_KEY)
        info["family_nodes"] = len(family.get("nodes") or [])
        info["family_updated_at_ms"] = family.get("updated_at_ms", 0)
        topics = load_graph_value(TOPICS_KEY)
        info["topics_nodes"] = len(topics.get("nodes") or [])
        info["topics_count"] = info["topics_nodes"]
        info["topics_updated_at_ms"] = topics.get("updated_at_ms", 0)
    except GraphIOError as exc:
        info["memx"] = f"error: {exc}"
    if getattr(args, "json", False):
        _emit_json(info)
        return 0
    print(f"familia {info['version']}")
    print(f"  memx:        {info['memx']}")
    print(f"  principals:  {info['principals_count']}")
    print(f"  family:      {info['family_nodes']} nodes")
    print(f"  topics:      {info['topics_nodes']} nodes")
    return 0


# ---------------------------------------------------------------------------
# audit tail (raw recent events for admin panel live-stream)
# ---------------------------------------------------------------------------

def cmd_audit_tail(args: argparse.Namespace) -> int:
    """Recent audit lines, optionally filtered by since.

    JSON mode: each event is emitted as its own line (NDJSON). The admin
    panel streams these via Tauri events (one event per line). Non-json
    mode pretty-prints for human eyes.
    """
    audit_path = Path(os.environ.get("FAMILIA_AUDIT_FILE", "audit.jsonl"))
    if not audit_path.exists():
        if getattr(args, "json", False):
            _emit_error_json(f"audit file not found: {audit_path}",
                             code="AUDIT_FILE_MISSING")
        else:
            print(f"audit file not found: {audit_path}", file=sys.stderr)
        return 1
    since = _parse_since(args.since) if args.since else None

    events: list[dict[str, Any]] = []
    with audit_path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if since:
                ts_str = e.get("ts")
                if not ts_str:
                    continue
                try:
                    from datetime import datetime
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                except ValueError:
                    continue
                if ts < since:
                    continue
            events.append(e)
    # Tail-N
    if args.limit > 0:
        events = events[-args.limit:]

    if getattr(args, "json", False):
        # NDJSON: one event per line for streaming consumers. Header
        # event with metadata so the consumer knows the slice bounds.
        sys.stdout.write(json.dumps(
            {"schema_version": SCHEMA_VERSION,
             "header": True, "count": len(events),
             "since": since.isoformat() if since else None,
             "limit": args.limit},
            ensure_ascii=False,
        ) + "\n")
        for e in events:
            sys.stdout.write(json.dumps(e, ensure_ascii=False, default=str) + "\n")
        sys.stdout.flush()
        return 0
    for e in events:
        ts = (e.get("ts") or "")[:19].replace("T", " ")
        print(f"{ts} {e.get('kind','?'):<14} {e.get('actor','-'):<14} {e.get('action','')}")
    return 0


# ---------------------------------------------------------------------------
# graph print
# ---------------------------------------------------------------------------

def cmd_graph_print(args: argparse.Namespace) -> int:
    family = load_graph_value(FAMILY_KEY)
    topics = load_graph_value(TOPICS_KEY)
    if getattr(args, "json", False):
        _emit_json({"family": family, "topics": topics})
        return 0
    print(f"=== {FAMILY_KEY} (updated_at_ms={family.get('updated_at_ms', 0)}) ===")
    _print_graph(family)
    print(f"=== {TOPICS_KEY} (updated_at_ms={topics.get('updated_at_ms', 0)}) ===")
    _print_graph(topics)
    return 0


def _print_graph(g: dict[str, Any]) -> None:
    nodes = g.get("nodes", [])
    edges = g.get("edges", [])
    print(f"  nodes ({len(nodes)}):")
    for n in nodes:
        if not isinstance(n, dict):
            continue
        kind = f", kind={n.get('kind')}" if n.get("kind") else ""
        aliases = n.get("aliases") or []
        alias_str = f" aliases={aliases}" if aliases else ""
        print(f"    {n.get('id'):<20} type={n.get('type','?')}{kind}{alias_str}")
    print(f"  edges ({len(edges)}):")
    for e in edges:
        if not isinstance(e, dict):
            continue
        suffix = ""
        if e.get("concerns_as"):
            suffix = f" concerns_as={e['concerns_as']}"
        print(f"    {e.get('from')} -- {e.get('rel')} --> {e.get('to')}{suffix}")


# ---------------------------------------------------------------------------
# graph person add-node / remove-node
# ---------------------------------------------------------------------------

def cmd_person_add_node(args: argparse.Namespace) -> int:
    family = load_graph_value(FAMILY_KEY)
    topics = load_graph_value(TOPICS_KEY)
    node_id = args.id
    if node_id in _all_known_ids(family, topics):
        raise GraphIOError(f"id '{node_id}' already exists in some graph or principals.json")
    aliases = args.aliases or []
    _validate_aliases(aliases)
    family.setdefault("nodes", []).append({
        "id": node_id,
        "type": "principal",
        "display_name": args.display_name or "",
        "aliases": aliases,
    })
    _save_graph(FAMILY_KEY, family)
    _audit_edit("person_add_node", FAMILY_KEY, node_id=node_id)
    print(f"added principal node '{node_id}'")
    return 0


def cmd_person_set_display_name(args: argparse.Namespace) -> int:
    """Rename the human-facing label of a principal.

    Updates BOTH the family.graph node ``display_name`` and the same
    field in ``principals.json`` if the id is registered there. Both
    are read on every gateway turn (memX for the graph, file reload
    for principals), so the new name is visible without restart.

    The id stays untouched — that's a separate, riskier operation
    because it ripples through identities, edges, audit history, and
    memX-key bindings.
    """
    new_name = (args.display_name or "").strip()
    family = load_graph_value(FAMILY_KEY)
    idx = _node_index(family, args.id)
    if idx is None:
        # Allow rename for registry-only principals too — those that
        # exist in principals.json but never made it into the graph.
        # For an unknown id, fail loud.
        path, raw = _load_principals_json()
        target = next(
            (p for p in (raw.get("principals") or []) if p.get("id") == args.id),
            None,
        )
        if target is None:
            raise GraphIOError(f"no principal '{args.id}' in family.graph or principals.json")
        old = target.get("display_name", "")
        target["display_name"] = new_name
        _save_principals_json(path, raw)
    else:
        old = family["nodes"][idx].get("display_name", "")
        family["nodes"][idx]["display_name"] = new_name
        _save_graph(FAMILY_KEY, family)
        # Mirror into principals.json if registered there. Keeping the
        # two sources in sync avoids the jarring case where /graph
        # shows the new display_name but the agent prompt still greets the old id.
        path, raw = _load_principals_json()
        target = next(
            (p for p in (raw.get("principals") or []) if p.get("id") == args.id),
            None,
        )
        if target is not None:
            target["display_name"] = new_name
            _save_principals_json(path, raw)
    _audit_edit(
        "person_set_display_name", FAMILY_KEY,
        node_id=args.id, old=old, new=new_name,
    )
    print(f"renamed '{args.id}': {old!r} -> {new_name!r}")
    return 0


def cmd_person_remove_node(args: argparse.Namespace) -> int:
    family = load_graph_value(FAMILY_KEY)
    idx = _node_index(family, args.id)
    if idx is None:
        raise GraphIOError(f"no person node '{args.id}'")
    family["nodes"].pop(idx)
    family["edges"] = [
        e for e in family.get("edges", [])
        if not (isinstance(e, dict)
                and (e.get("from") == args.id or e.get("to") == args.id))
    ]
    _save_graph(FAMILY_KEY, family)
    _audit_edit("person_remove_node", FAMILY_KEY, node_id=args.id)
    print(f"removed principal node '{args.id}' (and its edges)")
    return 0


# ---------------------------------------------------------------------------
# graph person add-edge / remove-edge
# ---------------------------------------------------------------------------

def cmd_person_add_edge(args: argparse.Namespace) -> int:
    family = load_graph_value(FAMILY_KEY)
    if args.rel not in ALLOWED_RELATIONS:
        raise GraphIOError(
            f"rel '{args.rel}' not in allowed {sorted(ALLOWED_RELATIONS)}"
        )
    src_idx = _node_index(family, args.src)
    dst_idx = _node_index(family, args.dst)
    if src_idx is None:
        raise GraphIOError(f"no such person node: '{args.src}'")
    if dst_idx is None:
        raise GraphIOError(f"no such person node: '{args.dst}'")
    if any(_edge_match(e, args.src, args.dst, args.rel) for e in family.get("edges", [])):
        raise GraphIOError("edge already present")
    family.setdefault("edges", []).append({
        "from": args.src, "to": args.dst, "rel": args.rel,
    })
    _save_graph(FAMILY_KEY, family)
    _audit_edit("person_add_edge", FAMILY_KEY,
                src=args.src, dst=args.dst, rel=args.rel)
    print(f"added edge {args.src} -- {args.rel} --> {args.dst}")
    return 0


def cmd_person_remove_edge(args: argparse.Namespace) -> int:
    family = load_graph_value(FAMILY_KEY)
    before = len(family.get("edges", []))
    family["edges"] = [
        e for e in family.get("edges", [])
        if not _edge_match(e, args.src, args.dst, args.rel)
    ]
    if len(family["edges"]) == before:
        raise GraphIOError("no matching edge")
    _save_graph(FAMILY_KEY, family)
    _audit_edit("person_remove_edge", FAMILY_KEY,
                src=args.src, dst=args.dst, rel=args.rel)
    print(f"removed edge {args.src} -- {args.rel} --> {args.dst}")
    return 0


# ---------------------------------------------------------------------------
# graph topic add-node / remove-node
# ---------------------------------------------------------------------------

def cmd_topic_add_node(args: argparse.Namespace) -> int:
    family = load_graph_value(FAMILY_KEY)
    topics = load_graph_value(TOPICS_KEY)
    node_id = args.id
    if node_id in _all_known_ids(family, topics):
        raise GraphIOError(f"id '{node_id}' already exists in some graph or principals.json")
    if args.kind not in TOPIC_KINDS:
        raise GraphIOError(f"kind '{args.kind}' not in allowed {sorted(TOPIC_KINDS)}")
    aliases = args.aliases or []
    _validate_aliases(aliases)
    topics.setdefault("nodes", []).append({
        "id": node_id,
        "type": "topic",
        "kind": args.kind,
        "display_name": args.display_name or "",
        "aliases": aliases,
    })
    _save_graph(TOPICS_KEY, topics)
    _audit_edit("topic_add_node", TOPICS_KEY, node_id=node_id, topic_kind=args.kind)
    print(f"added topic node '{node_id}' ({args.kind})")
    return 0


def cmd_topic_remove_node(args: argparse.Namespace) -> int:
    topics = load_graph_value(TOPICS_KEY)
    idx = _node_index(topics, args.id)
    if idx is None:
        raise GraphIOError(f"no topic node '{args.id}'")
    topics["nodes"].pop(idx)
    topics["edges"] = [
        e for e in topics.get("edges", [])
        if not (isinstance(e, dict)
                and (e.get("from") == args.id or e.get("to") == args.id))
    ]
    _save_graph(TOPICS_KEY, topics)
    _audit_edit("topic_remove_node", TOPICS_KEY, node_id=args.id)
    print(f"removed topic node '{args.id}' (and its edges)")
    return 0


# ---------------------------------------------------------------------------
# graph topic add-edge / remove-edge (concerns)
# ---------------------------------------------------------------------------

def cmd_topic_add_edge(args: argparse.Namespace) -> int:
    family = load_graph_value(FAMILY_KEY)
    topics = load_graph_value(TOPICS_KEY)
    # SR-16: concerns_as MUST match a family.graph relation.
    if args.concerns_as not in ALLOWED_RELATIONS:
        raise GraphIOError(
            f"concerns_as '{args.concerns_as}' must be one of "
            f"{sorted(ALLOWED_RELATIONS)}"
        )
    if _node_index(topics, args.topic) is None:
        raise GraphIOError(f"no topic node '{args.topic}' in topics graph")
    if (_node_index(family, args.person) is None
            and args.person not in _all_known_ids(family, topics)):
        # Person can be either in family.graph or principals.json (the
        # registry side wasn't replicated as a node — registry-only
        # principals are valid edge targets).
        try:
            from familia.principals import get_registry
            if args.person not in get_registry().ids:
                raise GraphIOError(
                    f"person '{args.person}' is not a known principal "
                    "(checked family.graph nodes and principals.json)"
                )
        except ImportError:
            raise GraphIOError(f"person '{args.person}' is not in family.graph")
    edge = {
        "from": args.topic, "to": args.person, "rel": "concerns",
        "concerns_as": args.concerns_as,
    }
    if any(_edge_match(e, args.topic, args.person, "concerns")
           and (e.get("concerns_as") == args.concerns_as)
           for e in topics.get("edges", [])):
        raise GraphIOError("edge already present")
    topics.setdefault("edges", []).append(edge)
    _save_graph(TOPICS_KEY, topics)
    _audit_edit("topic_add_edge", TOPICS_KEY,
                topic=args.topic, person=args.person, concerns_as=args.concerns_as)
    print(f"added edge {args.topic} -- concerns({args.concerns_as}) --> {args.person}")
    return 0


def cmd_topic_remove_edge(args: argparse.Namespace) -> int:
    topics = load_graph_value(TOPICS_KEY)
    before = len(topics.get("edges", []))
    topics["edges"] = [
        e for e in topics.get("edges", [])
        if not (_edge_match(e, args.topic, args.person, "concerns")
                and (not args.concerns_as
                     or e.get("concerns_as") == args.concerns_as))
    ]
    if len(topics["edges"]) == before:
        raise GraphIOError("no matching edge")
    _save_graph(TOPICS_KEY, topics)
    _audit_edit("topic_remove_edge", TOPICS_KEY,
                topic=args.topic, person=args.person)
    print(f"removed edge {args.topic} -- concerns --> {args.person}")
    return 0


# ---------------------------------------------------------------------------
# migrate seed-topics-from-subjects (SR-15)
# ---------------------------------------------------------------------------

def cmd_migrate_seed_topics(args: argparse.Namespace) -> int:
    """One-shot: move every ``type=subject`` node from family.graph into
    topics.graph; translate ``parent_of/owner_of`` edges where one endpoint
    is a subject into concerns-edges with ``concerns_as`` matching the
    relation name. Idempotent: re-run is a no-op if nothing left to move.
    """
    family = load_graph_value(FAMILY_KEY)
    topics = load_graph_value(TOPICS_KEY)

    subjects: list[dict[str, Any]] = [
        n for n in family.get("nodes", [])
        if isinstance(n, dict) and n.get("type") == "subject"
    ]
    if not subjects:
        print("no subjects in family.graph — nothing to migrate")
        return 0

    moved_nodes: list[str] = []
    moved_edges: list[tuple[str, str, str]] = []
    topic_ids = {n["id"] for n in topics.get("nodes", []) if isinstance(n, dict)}

    for subj in subjects:
        sid = subj.get("id")
        if not sid or sid in topic_ids:
            continue
        kind = subj.get("kind") or "person"
        if kind not in TOPIC_KINDS:
            kind = "person"
        topics.setdefault("nodes", []).append({
            "id": sid,
            "type": "topic",
            "kind": kind,
            "display_name": subj.get("display_name", ""),
            "aliases": list(subj.get("aliases", []) or []),
        })
        moved_nodes.append(sid)

    subj_ids = {s["id"] for s in subjects if isinstance(s, dict) and s.get("id")}
    new_family_edges: list[dict[str, Any]] = []
    for e in family.get("edges", []):
        if not isinstance(e, dict):
            continue
        if e.get("from") in subj_ids or e.get("to") in subj_ids:
            # Translate to concerns. The subject endpoint becomes the
            # topic side of the concerns edge.
            rel = e.get("rel", "")
            if e.get("from") in subj_ids:
                topic, person = e["from"], e["to"]
            else:
                topic, person = e["to"], e["from"]
            if rel not in ALLOWED_RELATIONS:
                logger.warning(
                    "skipping edge with unknown rel '{}': {}", rel, e,
                )
                continue
            # Avoid duplicate concerns-edges if seed-topics ran before.
            if any(_edge_match(te, topic, person, "concerns")
                   and (te.get("concerns_as") == rel)
                   for te in topics.get("edges", [])):
                continue
            topics.setdefault("edges", []).append({
                "from": topic, "to": person, "rel": "concerns",
                "concerns_as": rel,
            })
            moved_edges.append((topic, rel, person))
        else:
            new_family_edges.append(e)
    family["edges"] = new_family_edges
    family["nodes"] = [
        n for n in family.get("nodes", [])
        if not (isinstance(n, dict) and n.get("id") in subj_ids)
    ]
    if args.dry_run:
        if getattr(args, "json", False):
            _emit_json({
                "dry_run": True,
                "moved_nodes": moved_nodes,
                "moved_edges": [
                    {"topic": t, "concerns_as": r, "person": p}
                    for t, r, p in moved_edges
                ],
            })
            return 0
        print(f"DRY RUN — would move {len(moved_nodes)} nodes, "
              f"{len(moved_edges)} edges")
        for nid in moved_nodes:
            print(f"  node: {nid}")
        for t, r, p in moved_edges:
            print(f"  edge: {t} -- concerns({r}) --> {p}")
        return 0

    _save_graph(FAMILY_KEY, family)
    _save_graph(TOPICS_KEY, topics)
    _audit_edit("migrate_seed_topics", "(both graphs)",
                moved_nodes=moved_nodes, moved_edges_count=len(moved_edges))
    print(f"migrated {len(moved_nodes)} subject nodes "
          f"and {len(moved_edges)} edges into topics.graph")
    return 0


# ---------------------------------------------------------------------------
# migrate topic-to-principal (SR-17) — atomic with snapshot
# ---------------------------------------------------------------------------

def cmd_migrate_topic_to_principal(args: argparse.Namespace) -> int:
    """Atomic promotion of a topic-person to a principal.

    Steps with rollback on any failure:
      1. Snapshot both graphs.
      2. Pre-flight: topic exists with kind=person, principal id free,
         channel/sender pair free.
      3. Build new family.graph (add principal node + translate concerns
         edges into family.graph relations).
      4. Build new topics.graph (drop topic + its concerns edges).
      5. Persist both. On any error, restore snapshots.
      6. Audit ``migrate_topic_to_principal`` with diff.

    Note: principals.json + acl.json updates are out of scope for this
    pure-graph CLI command and are reported in the output for the admin
    to apply manually (they're files, not memX). Future panel will
    automate them.
    """
    family_pre = load_graph_value(FAMILY_KEY)
    topics_pre = load_graph_value(TOPICS_KEY)

    topic_idx = _node_index(topics_pre, args.topic)
    if topic_idx is None:
        raise GraphIOError(f"no topic '{args.topic}' to migrate")
    topic_node = topics_pre["nodes"][topic_idx]
    if topic_node.get("kind") != "person":
        raise GraphIOError(
            f"topic '{args.topic}' has kind={topic_node.get('kind')!r}; "
            "only person-topics can be promoted to principals"
        )
    if args.topic in _node_index_set(family_pre):
        raise GraphIOError(
            f"id '{args.topic}' already exists in family.graph as a node"
        )
    try:
        from familia.principals import get_registry
        if args.topic in get_registry().ids:
            raise GraphIOError(
                f"id '{args.topic}' already exists in principals.json"
            )
    except ImportError:
        pass

    # Build new family graph: add principal node + translate concerns.
    family_post = json.loads(json.dumps(family_pre))  # deep copy
    family_post.setdefault("nodes", []).append({
        "id": topic_node["id"],
        "type": "principal",
        "display_name": topic_node.get("display_name", ""),
        "aliases": list(topic_node.get("aliases", []) or []),
    })
    translated_edges = 0
    for e in topics_pre.get("edges", []):
        if not isinstance(e, dict):
            continue
        if e.get("rel") != "concerns":
            continue
        if e.get("from") == args.topic:
            person = e.get("to")
            rel = e.get("concerns_as")
            if not rel or rel not in ALLOWED_RELATIONS:
                continue
            family_post.setdefault("edges", []).append({
                "from": person, "to": args.topic, "rel": rel,
            })
            translated_edges += 1

    # Build new topics graph: drop the topic + its concerns edges.
    topics_post = json.loads(json.dumps(topics_pre))
    topics_post["nodes"] = [
        n for n in topics_post.get("nodes", [])
        if not (isinstance(n, dict) and n.get("id") == args.topic)
    ]
    topics_post["edges"] = [
        e for e in topics_post.get("edges", [])
        if not (isinstance(e, dict) and e.get("from") == args.topic)
    ]

    if args.dry_run:
        if getattr(args, "json", False):
            _emit_json({
                "dry_run": True,
                "topic": args.topic,
                "role": args.role,
                "translated_edges": translated_edges,
            })
            return 0
        print(f"DRY RUN — would promote topic '{args.topic}' to principal")
        print(f"  default role: {args.role}")
        print(f"  translated {translated_edges} concerns edges into family.graph")
        return 0

    # Persist with rollback. Two writes; if the second fails, restore the first.
    _save_graph(FAMILY_KEY, family_post)
    try:
        _save_graph(TOPICS_KEY, topics_post)
    except Exception as exc:
        logger.error("rolling back family.graph after topics.graph write failed: {}", exc)
        _save_graph(FAMILY_KEY, family_pre)
        raise GraphIOError(f"migration aborted: {exc}") from exc

    _audit_edit("migrate_topic_to_principal", "(both graphs)",
                topic_id=args.topic, role=args.role,
                translated_edges=translated_edges)
    print(f"promoted topic '{args.topic}' to principal (role: {args.role}).")
    print("Manual steps still required:")
    print(f"  1. Add principal entry to principals.json (id={args.topic}, role={args.role}).")
    print("  2. Issue memx_key and add to acl.json with appropriate scopes.")
    print("  3. Restart memX so the new key is picked up.")
    return 0


def _node_index_set(g: dict[str, Any]) -> set[str]:
    return {n["id"] for n in g.get("nodes", []) if isinstance(n, dict) and "id" in n}


# ---------------------------------------------------------------------------
# migrate seed-default-topics (B4)
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# memory CLI (admin-only memX raw access + hybrid-storage migration)
# ---------------------------------------------------------------------------


def cmd_memory_get(args: argparse.Namespace) -> int:
    """Read a memX value by full key. Used by admin app and operators.

    Uses the admin api_key (resolved via the standard
    ``resolve_admin_key`` chain). Output: raw value if it's a string,
    JSON-encoded otherwise. ``(missing)`` for 404.
    """
    from familia.acl.graph_io import get_raw, GraphIOError
    try:
        raw = get_raw(args.key)
    except GraphIOError as exc:
        raise GraphIOError(str(exc)) from exc
    if raw is None:
        if getattr(args, "json", False):
            print(json.dumps({"key": args.key, "value": None}, ensure_ascii=False))
        else:
            print("(missing)")
        return 0
    if getattr(args, "json", False):
        print(json.dumps({"key": args.key, "value": raw}, ensure_ascii=False))
    else:
        if isinstance(raw, str):
            print(raw)
        else:
            print(json.dumps(raw, ensure_ascii=False, indent=2))
    return 0


def cmd_memory_set(args: argparse.Namespace) -> int:
    """Write a memX value by full key. Refuses structural keys (SR-14)."""
    # Same belt-and-suspenders rejection list as MemorySetTool, plus
    # extra reserved namespaces that admin-CLI must never write through:
    # audit/policy/cron/reminders structural rooting points are reserved
    # for proper subcommands.
    structural = (
        "shared:roles.",
        "shared:family.graph",
        "shared:topics.graph",
        "shared:audit.",
        "shared:policy.",
        "shared:cron.",
        "shared:reminders.",
    )
    if any(args.key.startswith(p) for p in structural):
        raise GraphIOError(
            f"refusing to set structural key {args.key!r} via memory set; "
            "use the dedicated CLI subcommands (graph person/topic/edge, "
            "admin grant/revoke)"
        )
    if args.stdin:
        body = sys.stdin.read()
    else:
        body = args.value or ""
    # Defence-in-depth: chat-facing memX writes go through codec.encode
    # which wraps payload in ``__familia_acl_v1`` envelope. Admin CLI
    # writes raw bytes; if an operator (or compromised admin tool)
    # accidentally pastes a wrapped envelope here, we'd plant a record
    # that LOOKS tag-ACL-protected but actually is whatever the operator
    # wrote, bypassing tag-ACL on read. Refuse the obvious shape unless
    # the operator types --raw (not exposed by default).
    if "__familia_acl_v1" in body:
        raise GraphIOError(
            "body looks like a tag-ACL envelope (__familia_acl_v1). "
            "Use the chat-facing memory_set tool, not the CLI, to write "
            "tag-protected records."
        )
    from familia.acl.graph_io import set_raw
    set_raw(args.key, body)
    # Audit with a content fingerprint so post-incident investigation
    # can verify what was overwritten without storing the body itself.
    import hashlib
    body_sha = hashlib.sha256(body.encode("utf-8", errors="replace")).hexdigest()[:32]
    audit.log_event(
        "memory_cli_set", actor=None, key=args.key,
        bytes=len(body), body_sha=body_sha,
    )
    print(f"set {args.key} ({len(body)} bytes)")
    return 0


def cmd_migrate_hybrid_storage(args: argparse.Namespace) -> int:
    """Copy flat workspace files → per-principal memX namespaces.

    For each principal in ``principals.json``, reads the workspace
    files (USER.md, memory/MEMORY.md, HEARTBEAT.md) and writes their
    bodies into:

        private:<P>:value:user_profile
        private:<P>:value:memory
        private:<P>:value:heartbeat

    On the first run all principals get the same content (because the
    legacy files were one-per-stack); from there each principal's
    namespace diverges as the agent updates them.

    After successful write, the legacy files are renamed to
    ``legacy/USER.md`` etc. so the fallback path in ContextBuilder
    still works for one transitional release.

    Idempotent: re-running checks if memX already has non-empty
    content and skips that principal. Audit-event ``migrate_hybrid_storage``
    fires on every invocation.
    """
    from familia.acl.graph_io import get_raw, set_raw, GraphIOError
    nanobot_home_env = os.environ.get("NANOBOT_HOME") or os.environ.get(
        "FAMILIA_NANOBOT_HOME"
    )
    if nanobot_home_env:
        home = Path(nanobot_home_env)
    else:
        # Default: same dir as principals.json's parent.
        path, _raw = _load_principals_json()
        home = path.parent
    workspace = home / "workspace"
    user_md = workspace / "USER.md"
    memory_md = workspace / "memory" / "MEMORY.md"
    heartbeat_md = workspace / "HEARTBEAT.md"
    legacy_dir = workspace / "legacy"

    _, raw = _load_principals_json()
    principals = list(raw.get("principals") or [])
    if not principals:
        print("no principals — nothing to migrate")
        return 0

    # Identify which source files are pristine templates (just the
    # nanobot-shipped boilerplate). We DON'T migrate those — copying
    # "(your name)" stub into every principal's memX would just inject
    # junk into prompts. Compare against the packaged templates.
    def _is_template(content: str, template_subpath: str) -> bool:
        try:
            from importlib.resources import files as _pkg_files
            tpl = _pkg_files("nanobot") / "templates" / template_subpath
            if tpl.is_file():
                return content.strip() == tpl.read_text(encoding="utf-8").strip()
        except Exception:
            pass
        return False

    plan: list[dict[str, Any]] = []
    for entry in principals:
        pid = entry.get("id")
        if not pid:
            continue
        for suffix, src_path, tpl_name in (
            ("value:user_profile", user_md, "USER.md"),
            ("value:memory", memory_md, "memory/MEMORY.md"),
            ("value:heartbeat", heartbeat_md, "HEARTBEAT.md"),
        ):
            full_key = f"private:{pid}:{suffix}"
            try:
                existing = get_raw(full_key)
            except GraphIOError as exc:
                print(f"warn: cannot probe {full_key}: {exc}")
                existing = None
            already_present = bool(existing and (
                isinstance(existing, str) and existing.strip()
            ))
            if already_present:
                plan.append({
                    "principal": pid, "key": full_key,
                    "action": "skip", "reason": "memX already non-empty",
                })
                continue
            if not src_path.exists():
                plan.append({
                    "principal": pid, "key": full_key,
                    "action": "skip", "reason": f"source missing: {src_path}",
                })
                continue
            try:
                content = src_path.read_text(encoding="utf-8")
            except OSError as exc:
                plan.append({
                    "principal": pid, "key": full_key,
                    "action": "error", "reason": f"read failed: {exc}",
                })
                continue
            if not content.strip():
                plan.append({
                    "principal": pid, "key": full_key,
                    "action": "skip", "reason": "source empty",
                })
                continue
            if _is_template(content, tpl_name):
                plan.append({
                    "principal": pid, "key": full_key,
                    "action": "skip", "reason": "source is unmodified template",
                })
                continue
            plan.append({
                "principal": pid, "key": full_key,
                "action": "write", "src": str(src_path),
                "bytes": len(content.encode("utf-8")),
            })

    if args.dry_run:
        if getattr(args, "json", False):
            print(json.dumps({"plan": plan, "dry_run": True}, ensure_ascii=False))
        else:
            for item in plan:
                print(f"  {item['action']:<5} {item['principal']:<12} {item['key']}"
                      + (f"  ({item.get('reason') or ''})"
                         if item['action'] != 'write'
                         else f"  ({item.get('bytes', 0)} bytes from {item.get('src')})"))
            print(f"\n{sum(1 for x in plan if x['action']=='write')} "
                  "write(s) planned. Re-run with --apply to commit.")
        return 0

    # Per-key write loop. Each successful write is independently audit-
    # logged (SR-11). On the FIRST error we stop — partial-then-skip
    # would leave a half-migrated state with no obvious recovery.
    # Idempotency: re-run picks up where the previous one stopped
    # because already-written keys are skip'd by the planning phase.
    written = 0
    errors: list[str] = []
    written_per_principal: dict[str, int] = {}
    for item in plan:
        if item["action"] != "write":
            continue
        try:
            content = Path(item["src"]).read_text(encoding="utf-8")
            set_raw(item["key"], content)
            written += 1
            written_per_principal[item["principal"]] = (
                written_per_principal.get(item["principal"], 0) + 1
            )
            audit.log_event(
                "migrate_hybrid_storage_write",
                principal=item["principal"],
                key=item["key"],
                bytes=len(content.encode("utf-8")),
            )
        except (OSError, GraphIOError) as exc:
            errors.append(f"{item['key']}: {exc}")
            break  # fail-fast on first per-key write failure

    # Rename legacy files ONLY after a clean run. SR-17 atomicity:
    # never half-rename across principals. The flat workspace files
    # are shared (one USER.md for whole stack), so renaming them is
    # a single global step done iff every planned write succeeded.
    # On error, files stay in place — ContextBuilder's fallback
    # path keeps reading them until the operator resolves the issue
    # and re-runs --apply.
    if written and not errors:
        legacy_dir.mkdir(parents=True, exist_ok=True)
        for src in (user_md, memory_md, heartbeat_md):
            if not src.exists():
                continue
            dest = legacy_dir / src.name
            try:
                src.rename(dest)
            except OSError as exc:
                print(f"warn: failed to move {src} → {dest}: {exc}")

    audit.log_event(
        "migrate_hybrid_storage",
        principals_count=len(principals),
        written=written,
        errors=len(errors),
        per_principal=dict(written_per_principal),
    )

    if errors:
        for e in errors:
            print(f"error: {e}", file=sys.stderr)
        print(
            f"migration aborted on first error. {written} key(s) committed; "
            "re-run after fixing memX. Legacy files NOT renamed.",
            file=sys.stderr,
        )
        return 1
    print(f"migrated {written} key(s) into memX. "
          f"Legacy files moved to {legacy_dir}.")
    return 0


# ---------------------------------------------------------------------------
# default-topic seed
# ---------------------------------------------------------------------------

# Default abstract-topic seed for a new family install. Picks common
# domains so the LLM has something to match against on day one. Admin can
# prune or extend afterwards. Aliases avoid kinship roots ('жен-', 'слав-')
# and don't collide with KINSHIP_RU.
_DEFAULT_TOPICS = [
    ("school",    "Школа",        ["школу", "уроки", "учёба", "учеба"]),
    ("finance",   "Финансы",      ["деньги", "счёт", "счета", "банк", "налог"]),
    ("health",    "Здоровье",     ["врач", "приём", "прием", "анализы", "клиника"]),
    ("work",      "Работа",       ["офис", "встреча", "проект", "командировка"]),
    ("travel",    "Путешествия",  ["поездка", "отпуск", "билет", "виза"]),
    ("home",      "Быт",          ["быт", "уборка", "ремонт", "коммуналка"]),
    ("holidays",  "Праздники",    ["праздник", "юбилей", "годовщина"]),
    ("shopping",  "Покупки",      ["купить", "магазин", "заказать"]),
]


def cmd_migrate_seed_default_topics(args: argparse.Namespace) -> int:
    """Seed a default set of abstract topics + connect to all principals.

    Idempotent: skips topics already present (by id). Each new topic gets
    a ``concerns(guardian_of)`` edge to every principal in family.graph
    so they all see it from day one. Admin can prune individual edges via
    ``familia graph topic remove-edge <topic> <principal>`` after.
    """
    family = load_graph_value(FAMILY_KEY)
    topics = load_graph_value(TOPICS_KEY)
    existing_ids = _all_known_ids(family, topics)
    new_nodes: list[str] = []
    for tid, name, aliases in _DEFAULT_TOPICS:
        if tid in existing_ids:
            continue
        _validate_aliases(aliases)  # cheap: catches kinship coll
        topics.setdefault("nodes", []).append({
            "id": tid,
            "type": "topic",
            "kind": "abstract",
            "display_name": name,
            "aliases": list(aliases),
        })
        new_nodes.append(tid)

    if not new_nodes:
        print("no new topics to seed (vocabulary already covers the defaults)")
        return 0

    principals = [
        n["id"] for n in family.get("nodes", [])
        if isinstance(n, dict) and n.get("type") == "principal" and n.get("id")
    ]

    if args.dry_run:
        if getattr(args, "json", False):
            _emit_json({
                "dry_run": True,
                "new_topics": [
                    {"id": tid,
                     "display_name": next(t[1] for t in _DEFAULT_TOPICS if t[0] == tid)}
                    for tid in new_nodes
                ],
                "principals": principals,
                "edges_to_add": len(new_nodes) * len(principals),
            })
            return 0
        print(f"DRY RUN — would seed {len(new_nodes)} topics:")
        for tid in new_nodes:
            display = next(t[1] for t in _DEFAULT_TOPICS if t[0] == tid)
            print(f"  {tid:<12} {display}")
        print(f"and add {len(new_nodes) * len(principals)} concerns edges "
              f"({len(new_nodes)} topics × {len(principals)} principals)")
        return 0

    for tid in new_nodes:
        for pid in principals:
            topics.setdefault("edges", []).append({
                "from": tid,
                "to": pid,
                "rel": "concerns",
                "concerns_as": "guardian_of",
            })

    _save_graph(TOPICS_KEY, topics)
    _audit_edit(
        "migrate_seed_default_topics", TOPICS_KEY,
        topics_added=new_nodes,
        principals_count=len(principals),
    )
    print(f"seeded {len(new_nodes)} topics; "
          f"connected each to {len(principals)} principals")
    print("\nReview with: familia graph print")
    print("Prune edges per topic: familia graph topic remove-edge <topic> <principal>")
    return 0


# ---------------------------------------------------------------------------
# audit untagged-topics (B1)
# ---------------------------------------------------------------------------

# Russian-language stopwords for the untagged-topic scan. Conservative:
# only function words and verbs that show up in every reminder ("напомни"),
# never substantives that could legitimately be a topic candidate.
_STOPWORDS_RU = frozenset({
    # function words
    "и", "в", "на", "с", "к", "от", "для", "не", "но", "или", "а", "же",
    "у", "до", "из", "за", "над", "под", "через", "без", "по", "про",
    "о", "об", "обо", "со", "ко",
    # demonstratives + interrogatives
    "это", "этот", "эта", "тот", "та", "то", "так", "там", "тут", "здесь",
    "что", "где", "когда", "как", "почему", "зачем", "куда", "откуда",
    # to-be / modals
    "был", "была", "было", "были", "есть", "будет", "будут", "буду",
    "надо", "нужно", "можно", "должен", "должна", "должны",
    # quantifiers
    "все", "всё", "каждый", "каждая", "каждое",
    # conjunctions / discourse
    "если", "хотя", "потому", "поэтому", "уже", "ещё", "еще",
    # bot-prompt verbs that appear in every reminder
    "напомни", "напоминание", "напомнить", "запиши", "записать",
    "передай", "сообщи", "пожалуйста", "сегодня", "завтра", "вчера",
    "утра", "вечера", "дня", "ночи", "часов", "минут",
    # personal pronouns (often in messages)
    "мне", "тебе", "ему", "ей", "нам", "вам", "им",
    "меня", "тебя", "его", "её", "ее", "нас", "вас", "их",
    "мой", "моя", "моё", "мое", "мои", "твой", "твоя", "твоё", "твое", "твои",
    "наш", "наша", "наше", "наши", "ваш", "ваша", "ваше", "ваши",
    "свой", "своя", "своё", "свое", "свои",
    # English: tool-arg field names (always present in args_preview)
    "action", "message", "value", "scope", "key", "name", "args", "tags",
    "every_seconds", "cron_expr", "tz", "at", "deliver", "job_id", "to",
    "kind", "actor", "channel", "tool", "preview", "principal", "role",
    "the", "and", "for", "with", "that", "this", "are", "was", "were",
    "true", "false", "null", "none",
})


def cmd_audit_untagged_topics(args: argparse.Namespace) -> int:
    """Find candidate words that show up often in records BUT aren't already
    in either graph's vocabulary. Admin scans → decides what to add as a
    new topic.

    Heuristic only — false-positives from common verbs/adjectives are
    expected. Stopword list is intentionally short; expand by editing
    :data:`_STOPWORDS_RU` if a real word keeps surfacing as noise.
    """
    import re
    from collections import Counter
    from datetime import datetime

    audit_path = Path(os.environ.get("FAMILIA_AUDIT_FILE", "audit.jsonl"))
    if not audit_path.exists():
        print(f"audit file not found: {audit_path}", file=sys.stderr)
        return 1

    since = _parse_since(args.since)
    family = load_graph_value(FAMILY_KEY)
    topics = load_graph_value(TOPICS_KEY)
    vocab: set[str] = set()
    for n in (family.get("nodes") or []) + (topics.get("nodes") or []):
        if not isinstance(n, dict):
            continue
        for field in ("id", "display_name"):
            v = n.get(field)
            if isinstance(v, str) and v:
                vocab.add(v.lower())
        for a in n.get("aliases") or []:
            if isinstance(a, str) and a:
                vocab.add(a.lower())

    word_pat = re.compile(r"[А-Яа-яёЁa-zA-Z]{3,}")
    counts: Counter[str] = Counter()
    sample: dict[str, str] = {}  # word -> first sample text

    with audit_path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            ts_str = e.get("ts")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except ValueError:
                continue
            if ts < since:
                continue
            if e.get("kind") != "tool_call":
                continue
            tool = e.get("tool", "")
            if tool not in ("memory_set", "cron"):
                continue
            preview = e.get("args_preview", "") or ""
            # Skip records that already declared tags
            if '"tags": [' in preview and '"tags": []' not in preview:
                continue
            for word in word_pat.findall(preview):
                low = word.lower()
                if low in _STOPWORDS_RU or low in vocab:
                    continue
                counts[low] += 1
                sample.setdefault(low, preview[:80])

    if not counts:
        print(f"no untagged-topic candidates since {since.isoformat()}")
        return 0

    print(f"=== since {since.isoformat()} (limit={args.limit}) ===")
    print("Frequent words in untagged records — possible new topics:\n")
    for word, n in counts.most_common(args.limit):
        print(f"  {n:>4}  {word:<24} ← {sample[word]}")
    print()
    print("Pick candidates and run:")
    print("  familia graph topic add-node <id> --kind abstract \\")
    print("    --display-name <DisplayName> --aliases <a1> <a2> ...")
    return 0


# ---------------------------------------------------------------------------
# audit tags (A5)
# ---------------------------------------------------------------------------

def _parse_since(s: str):
    """Accept '30m', '2h', '7d', or ISO datetime."""
    from datetime import datetime, timedelta, timezone
    units = {"m": 60, "h": 3600, "d": 86400}
    if s and s[-1] in units:
        try:
            n = int(s[:-1])
        except ValueError:
            raise GraphIOError(f"bad --since value: {s!r}")
        return datetime.now(timezone.utc) - timedelta(seconds=n * units[s[-1]])
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        raise GraphIOError(f"bad --since value: {s!r}")


def cmd_audit_tags(args: argparse.Namespace) -> int:
    """Slice the audit log down to tag-ACL decisions and graph edits.

    Surfaces the cumulative picture an admin needs after a few days of
    LLM-driven tagging:
      * Who tagged what (write-side decisions, allow + deny).
      * What graph edits the CLI itself emitted (graph_edit events).
      * Top tags by frequency to spot stale/under-used ones.
    """
    from collections import Counter
    from datetime import datetime

    audit_path = Path(os.environ.get("FAMILIA_AUDIT_FILE", "audit.jsonl"))
    if not audit_path.exists():
        print(f"audit file not found: {audit_path}", file=sys.stderr)
        return 1
    since = _parse_since(args.since)
    decisions: list[dict[str, Any]] = []
    edits: list[dict[str, Any]] = []
    with audit_path.open("r", encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            ts_str = e.get("ts")
            if not ts_str:
                continue
            try:
                ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except ValueError:
                continue
            if ts < since:
                continue
            kind = e.get("kind")
            if kind == "tag_acl_decision":
                decisions.append(e)
            elif kind == "graph_edit":
                edits.append(e)

    if getattr(args, "json", False):
        denied = [e for e in decisions if e.get("decision") == "deny"]
        tag_freq: Counter[str] = Counter()
        for e in decisions:
            for t in e.get("record_tags") or []:
                tag_freq[t] += 1
        actor_writes = Counter(
            e.get("actor") for e in decisions
            if e.get("op") == "write" and e.get("actor")
        )
        _emit_json({
            "since": since.isoformat(),
            "decisions_count": len(decisions),
            "edits_count": len(edits),
            "denials": denied[-args.limit:],
            "tag_frequency": [{"tag": t, "count": n}
                              for t, n in tag_freq.most_common(20)],
            "writes_by_actor": [{"actor": a, "count": n}
                                for a, n in actor_writes.most_common()],
            "edits": edits[-args.limit:],
        })
        return 0

    print(f"=== since {since.isoformat()} ===")
    print(f"tag_acl_decision events: {len(decisions)}")
    print(f"graph_edit events:       {len(edits)}")
    if not decisions and not edits:
        return 0

    if decisions:
        denied = [e for e in decisions if e.get("decision") == "deny"]
        print(f"\nDenials ({len(denied)}):")
        for e in denied[-args.limit:]:
            print(f"  {e.get('ts','?')[:19]} {e.get('actor'):<12} "
                  f"{e.get('op','?'):<5} key={e.get('full_key','?')} "
                  f"tags={e.get('record_tags', [])}")

        tag_freq: Counter[str] = Counter()
        for e in decisions:
            for t in e.get("record_tags") or []:
                tag_freq[t] += 1
        print("\nTop tags by frequency:")
        for t, n in tag_freq.most_common(20):
            print(f"  {t:<24} {n}")

        actor_writes = Counter(
            e.get("actor") for e in decisions
            if e.get("op") == "write" and e.get("actor")
        )
        if actor_writes:
            print("\nWrites by actor:")
            for a, n in actor_writes.most_common():
                print(f"  {a:<14} {n}")

    if edits:
        print(f"\nRecent graph edits ({min(len(edits), args.limit)} of {len(edits)}):")
        for e in edits[-args.limit:]:
            print(f"  {e.get('ts','?')[:19]} {e.get('cli_user','?'):<10} "
                  f"{e.get('op','?'):<22} target={e.get('target_key','?')}")
    return 0


# ---------------------------------------------------------------------------
# Argparse plumbing
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="familia",
        description="Familia admin CLI: graph + migrations.",
    )
    # --json is supported by selected subcommands. We inherit it via a
    # parents-list pattern so each subparser explicitly opts in (some
    # subcommands like add-node only print "ok" — JSON shape would be
    # tautological).
    json_parent = argparse.ArgumentParser(add_help=False)
    json_parent.add_argument(
        "--json", action="store_true",
        help="emit structured JSON (admin-panel contract; schema_version=1)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # version
    p_ver = sub.add_parser("version", parents=[json_parent],
                           help="familia CLI version")
    p_ver.set_defaults(func=cmd_version)

    # health
    p_health = sub.add_parser("health", parents=[json_parent],
                              help="health snapshot for admin dashboard")
    p_health.set_defaults(func=cmd_health)

    # graph ...
    pg = sub.add_parser("graph", help="graph admin")
    pg_sub = pg.add_subparsers(dest="graph_cmd", required=True)

    pg_print = pg_sub.add_parser("print", parents=[json_parent],
                                 help="dump both graphs")
    pg_print.set_defaults(func=cmd_graph_print)

    # graph person ...
    pg_person = pg_sub.add_parser("person", help="family.graph (principals)")
    pg_person_sub = pg_person.add_subparsers(dest="person_cmd", required=True)

    p_pan = pg_person_sub.add_parser("add-node")
    p_pan.add_argument("id")
    p_pan.add_argument("--display-name", default="")
    p_pan.add_argument("--aliases", nargs="*")
    p_pan.set_defaults(func=cmd_person_add_node)

    p_prn = pg_person_sub.add_parser("remove-node")
    p_prn.add_argument("id")
    p_prn.set_defaults(func=cmd_person_remove_node)

    p_psn = pg_person_sub.add_parser(
        "set-display-name",
        help="rename the human label of a principal (id is left intact)",
    )
    p_psn.add_argument("id")
    p_psn.add_argument("--display-name", required=True, dest="display_name")
    p_psn.set_defaults(func=cmd_person_set_display_name)

    p_pae = pg_person_sub.add_parser("add-edge")
    p_pae.add_argument("src")
    p_pae.add_argument("rel", choices=sorted(ALLOWED_RELATIONS))
    p_pae.add_argument("dst")
    p_pae.set_defaults(func=cmd_person_add_edge)

    p_pre = pg_person_sub.add_parser("remove-edge")
    p_pre.add_argument("src")
    p_pre.add_argument("rel", choices=sorted(ALLOWED_RELATIONS))
    p_pre.add_argument("dst")
    p_pre.set_defaults(func=cmd_person_remove_edge)

    # graph topic ...
    pg_topic = pg_sub.add_parser("topic", help="topics.graph")
    pg_topic_sub = pg_topic.add_subparsers(dest="topic_cmd", required=True)

    p_tan = pg_topic_sub.add_parser("add-node")
    p_tan.add_argument("id")
    p_tan.add_argument("--kind", required=True, choices=sorted(TOPIC_KINDS))
    p_tan.add_argument("--display-name", default="")
    p_tan.add_argument("--aliases", nargs="*")
    p_tan.set_defaults(func=cmd_topic_add_node)

    p_trn = pg_topic_sub.add_parser("remove-node")
    p_trn.add_argument("id")
    p_trn.set_defaults(func=cmd_topic_remove_node)

    p_tae = pg_topic_sub.add_parser("add-edge")
    p_tae.add_argument("topic")
    p_tae.add_argument("--concerns-as", required=True,
                       choices=sorted(ALLOWED_RELATIONS),
                       dest="concerns_as")
    p_tae.add_argument("person")
    p_tae.set_defaults(func=cmd_topic_add_edge)

    p_tre = pg_topic_sub.add_parser("remove-edge")
    p_tre.add_argument("topic")
    p_tre.add_argument("person")
    p_tre.add_argument("--concerns-as", default="", dest="concerns_as")
    p_tre.set_defaults(func=cmd_topic_remove_edge)

    # audit tags ...
    pa = sub.add_parser("audit", help="audit log slices")
    pa_sub = pa.add_subparsers(dest="audit_cmd", required=True)
    p_at = pa_sub.add_parser("tags", parents=[json_parent],
                             help="recent tag-ACL decisions + graph edits")
    p_at.add_argument("--since", default="7d",
                      help="window: 30m, 2h, 7d, or ISO timestamp")
    p_at.add_argument("--limit", type=int, default=50)
    p_at.set_defaults(func=cmd_audit_tags)

    p_au = pa_sub.add_parser("untagged-topics",
                             help="frequent non-vocab words = topic candidates")
    p_au.add_argument("--since", default="7d")
    p_au.add_argument("--limit", type=int, default=20)
    p_au.set_defaults(func=cmd_audit_untagged_topics)

    p_atl = pa_sub.add_parser("tail", parents=[json_parent],
                              help="recent raw audit events (NDJSON in --json)")
    p_atl.add_argument("--since", default="",
                       help="window like 1h/30m/7d (default: no filter)")
    p_atl.add_argument("--limit", type=int, default=50)
    p_atl.set_defaults(func=cmd_audit_tail)

    # migrate ...
    pm = sub.add_parser("migrate", help="data migrations")
    pm_sub = pm.add_subparsers(dest="migrate_cmd", required=True)

    p_seed = pm_sub.add_parser("seed-topics-from-subjects",
                               parents=[json_parent],
                               help="one-shot move legacy subjects → topics.graph")
    p_seed.add_argument("--apply", dest="dry_run", action="store_false",
                        default=True)
    p_seed.set_defaults(func=cmd_migrate_seed_topics)

    p_seed_def = pm_sub.add_parser("seed-default-topics",
                                   parents=[json_parent],
                                   help="create the default abstract topic set (school/finance/health/etc.)")
    p_seed_def.add_argument("--apply", dest="dry_run", action="store_false",
                            default=True)
    p_seed_def.set_defaults(func=cmd_migrate_seed_default_topics)

    p_t2p = pm_sub.add_parser("topic-to-principal",
                              parents=[json_parent],
                              help="promote topic-person → principal (atomic)")
    p_t2p.add_argument("topic")
    p_t2p.add_argument("--role", default="child",
                       choices=["child", "member", "admin"])
    p_t2p.add_argument("--apply", dest="dry_run", action="store_false",
                       default=True)
    p_t2p.set_defaults(func=cmd_migrate_topic_to_principal)

    # Hybrid storage migration: move per-principal USER/MEMORY/HEARTBEAT
    # from flat workspace files into per-principal memX namespaces. Keep
    # existing files renamed as ``legacy.*`` for fallback.
    p_hyb = pm_sub.add_parser("hybrid-storage",
                              parents=[json_parent],
                              help=("copy workspace USER.md/MEMORY.md/"
                                    "HEARTBEAT.md → per-principal memX keys"))
    p_hyb.add_argument("--apply", dest="dry_run", action="store_false",
                       default=True)
    p_hyb.set_defaults(func=cmd_migrate_hybrid_storage)

    # memory ...  raw memX read/write (admin-side, used by admin app's
    # /personality editor and by the hybrid-storage migration).
    pmem = sub.add_parser("memory", help="raw memX get/set (admin only)")
    pmem_sub = pmem.add_subparsers(dest="memory_cmd", required=True)

    p_mg = pmem_sub.add_parser("get", parents=[json_parent],
                               help="read a memX value by full key")
    p_mg.add_argument("key")
    p_mg.set_defaults(func=cmd_memory_get)

    p_ms = pmem_sub.add_parser("set",
                               help=("write a memX value; pass --value or "
                                     "--stdin to feed body"))
    p_ms.add_argument("key")
    p_ms_src = p_ms.add_mutually_exclusive_group(required=True)
    p_ms_src.add_argument("--value", default=None,
                          help="literal string body")
    p_ms_src.add_argument("--stdin", action="store_true",
                          help="read body from stdin (preserves multi-line)")
    p_ms.set_defaults(func=cmd_memory_set)

    # pending ...  approval queue for unknown principals
    pp = sub.add_parser("pending", help="pending-principal approval queue")
    pp_sub = pp.add_subparsers(dest="pending_cmd", required=True)

    p_pp_list = pp_sub.add_parser("list", parents=[json_parent],
                                  help="list pending approval requests")
    p_pp_list.set_defaults(func=cmd_pending_list)

    p_pp_app = pp_sub.add_parser("approve",
                                 help="approve a request → add to principals.json")
    p_pp_app.add_argument("channel")
    p_pp_app.add_argument("sender_id")
    pp_target = p_pp_app.add_mutually_exclusive_group(required=True)
    pp_target.add_argument("--as-id", dest="as_id", default=None,
                           help="create a new principal with this id")
    pp_target.add_argument("--attach-to", dest="attach_to", default=None,
                           help="attach the (channel, sender_id) identity to an existing principal id")
    p_pp_app.add_argument("--display-name", default="", dest="display_name")
    p_pp_app.set_defaults(func=cmd_pending_approve)

    p_pp_rej = pp_sub.add_parser("reject",
                                 help="drop a request and silence the sender for 24h")
    p_pp_rej.add_argument("channel")
    p_pp_rej.add_argument("sender_id")
    p_pp_rej.set_defaults(func=cmd_pending_reject)

    # identity ...  manage per-principal channel identities
    pid = sub.add_parser("identity", help="manage principals' channel identities")
    pid_sub = pid.add_subparsers(dest="identity_cmd", required=True)

    p_id_list = pid_sub.add_parser("list", parents=[json_parent],
                                   help="list identities of a principal")
    p_id_list.add_argument("principal_id")
    p_id_list.set_defaults(func=cmd_identity_list)

    p_id_rm = pid_sub.add_parser("remove",
                                 help="detach a (channel, sender_id) identity from a principal")
    p_id_rm.add_argument("principal_id")
    p_id_rm.add_argument("channel")
    p_id_rm.add_argument("sender_id")
    p_id_rm.set_defaults(func=cmd_identity_remove)

    # channels ...  manage poll-based channel adapters in config.json
    pch = sub.add_parser("channels", help="manage poll-based channel adapters")
    pch_sub = pch.add_subparsers(dest="channels_cmd", required=True)

    p_ch_list = pch_sub.add_parser("list", parents=[json_parent],
                                   help="list configured channels")
    p_ch_list.set_defaults(func=cmd_channels_list)

    p_ch_add = pch_sub.add_parser("add",
                                  help="enable a poll-based channel; --config is a JSON object")
    p_ch_add.add_argument("name", choices=sorted(POLL_CHANNEL_KINDS))
    p_ch_add.add_argument("--config", required=True, dest="config_json",
                          help="JSON object with channel-specific settings (token, allow_from, …)")
    p_ch_add.set_defaults(func=cmd_channels_add)

    p_ch_rm = pch_sub.add_parser("remove",
                                 help="disable and remove a channel from config.json")
    p_ch_rm.add_argument("name")
    p_ch_rm.set_defaults(func=cmd_channels_remove)

    # ``enable`` / ``disable`` flip the ``enabled`` flag without
    # touching the rest of the channel config — admin UI calls these
    # from a one-click Switch so the operator can pause a channel
    # without re-entering its token.
    p_ch_en = pch_sub.add_parser("enable",
                                 help="set enabled=true on an existing channel")
    p_ch_en.add_argument("name")
    p_ch_en.set_defaults(func=cmd_channels_enable)

    p_ch_dis = pch_sub.add_parser("disable",
                                  help="set enabled=false on an existing channel")
    p_ch_dis.add_argument("name")
    p_ch_dis.set_defaults(func=cmd_channels_disable)

    p_ch_deps = pch_sub.add_parser("deps",
                                   help="manage runtime Python deps for poll channels")
    p_ch_deps_sub = p_ch_deps.add_subparsers(dest="deps_cmd", required=True)

    p_ch_deps_status = p_ch_deps_sub.add_parser("status",
                                                parents=[json_parent],
                                                help="report which channel deps are importable")
    p_ch_deps_status.set_defaults(func=cmd_channels_deps_status)

    p_ch_deps_install = p_ch_deps_sub.add_parser("install",
                                                 help="pip install --user the deps for a kind")
    p_ch_deps_install.add_argument("kind", choices=sorted(CHANNEL_DEPS.keys()))
    p_ch_deps_install.set_defaults(func=cmd_channels_deps_install)

    p_ch_test = pch_sub.add_parser("test", parents=[json_parent],
                                   help="ping the channel API with given config (no save)")
    p_ch_test.add_argument("kind", choices=sorted(POLL_CHANNEL_KINDS))
    p_ch_test.add_argument("--config", required=True, dest="config_json",
                           help="JSON object with creds to test")
    p_ch_test.set_defaults(func=cmd_channels_test)

    # Per-channel STT override. ``inherit`` clears the field so the channel
    # falls back to the global ``channels.transcriptionProvider`` default;
    # ``off`` disables transcription entirely; otherwise stores the named
    # provider on the section.
    p_ch_stt = pch_sub.add_parser("set-stt",
                                  help="pick the STT (voice transcription) provider for a channel")
    p_ch_stt.add_argument("name")
    p_ch_stt.add_argument("provider",
                          choices=sorted(STT_PROVIDER_CHOICES))
    p_ch_stt.set_defaults(func=cmd_channels_set_stt)

    # ``stt`` ... configure STT (voice transcription) providers globally.
    pst = sub.add_parser("stt", help="manage speech-to-text providers")
    pst_sub = pst.add_subparsers(dest="stt_cmd", required=True)

    p_stt_get = pst_sub.add_parser("get", parents=[json_parent],
                                   help="dump STT providers + global default + per-channel overrides (redacted)")
    p_stt_get.set_defaults(func=cmd_stt_get)

    p_stt_set = pst_sub.add_parser("set",
                                   help="store credentials for a STT provider")
    p_stt_set.add_argument("provider", choices=sorted(STT_CRED_PROVIDERS))
    p_stt_set.add_argument("--api-key", default="", dest="api_key")
    p_stt_set.add_argument("--api-base", default="", dest="api_base")
    p_stt_set.add_argument("--folder-id", default="", dest="folder_id",
                           help="Yandex Cloud folder id (yandex only)")
    p_stt_set.set_defaults(func=cmd_stt_set)

    p_stt_default = pst_sub.add_parser("set-default",
                                       help="set the global STT default that channels inherit when they have no override")
    p_stt_default.add_argument("provider",
                               choices=sorted(STT_PROVIDER_CHOICES))
    p_stt_default.set_defaults(func=cmd_stt_set_default)

    # agents ...  read/write the LLM-agent slots (main + fallback) that
    # familia-gateway uses; live in nanobot's config.json.
    pag = sub.add_parser("agents", help="manage LLM agent slots (main + fallback)")
    pag_sub = pag.add_subparsers(dest="agents_cmd", required=True)

    p_ag_get = pag_sub.add_parser("get", parents=[json_parent],
                                  help="dump current agent config (with secrets redacted)")
    p_ag_get.set_defaults(func=cmd_agents_get)

    p_ag_set = pag_sub.add_parser("set",
                                  help="write a slot's config (model + provider + api_key)")
    p_ag_set.add_argument("slot", choices=["main", "fallback"])
    p_ag_set.add_argument("--model", required=True)
    p_ag_set.add_argument("--provider", default="",
                          help="provider key (openai, anthropic, openai-codex, …); auto-detect if empty")
    p_ag_set.add_argument("--api-key", default="", dest="api_key",
                          help="API key for the provider (omit for OAuth providers)")
    p_ag_set.add_argument("--api-base", default="", dest="api_base",
                          help="custom API base URL (optional)")
    p_ag_set.set_defaults(func=cmd_agents_set)

    p_ag_clear = pag_sub.add_parser("clear",
                                    help="remove a slot (main keeps required model)")
    p_ag_clear.add_argument("slot", choices=["fallback"])
    p_ag_clear.set_defaults(func=cmd_agents_clear)

    p_ag_test = pag_sub.add_parser("test", parents=[json_parent],
                                   help="run a tiny round-trip against a slot")
    p_ag_test.add_argument("slot", choices=["main", "fallback"])
    p_ag_test.set_defaults(func=cmd_agents_test)

    p_ag_oauth = pag_sub.add_parser("oauth-status", parents=[json_parent],
                                    help="check whether an OAuth provider is logged in")
    p_ag_oauth.add_argument("provider")
    p_ag_oauth.set_defaults(func=cmd_agents_oauth_status)

    p_ag_refresh = pag_sub.add_parser("refresh-models", parents=[json_parent],
                                      help="pull /v1/models from each configured provider into a local cache")
    p_ag_refresh.add_argument("--provider", default="",
                              help="refresh only this provider (default: all configured)")
    p_ag_refresh.set_defaults(func=cmd_agents_refresh_models)

    return p


def cmd_pending_list(args: argparse.Namespace) -> int:
    from familia.pending import store

    entries = store.list_all()
    if args.json:
        out = {
            "schema_version": 1,
            "count": len(entries),
            "entries": [
                {
                    "channel": e.channel,
                    "sender_id": e.sender_id,
                    "display_name": e.display_name,
                    "first_seen_ms": e.first_seen_ms,
                    "last_seen_ms": e.last_seen_ms,
                    "last_message_preview": e.last_message_preview,
                    "msg_count": e.msg_count,
                }
                for e in entries
            ],
        }
        print(json.dumps(out, ensure_ascii=False))
        return 0
    if not entries:
        print("(no pending requests)")
        return 0
    for e in entries:
        print(
            f"{e.channel}:{e.sender_id}  "
            f"name={e.display_name!r}  "
            f"msgs={e.msg_count}  "
            f"last={e.last_message_preview!r}"
        )
    return 0


def cmd_pending_approve(args: argparse.Namespace) -> int:
    """Promote a pending entry to a real principal.

    Two modes:

    * ``--as-id <new>`` — create a fresh principal entry. Atomically (under
      a POSIX file-lock when available): validate id uniqueness across
      principals.json + family graph + topics graph, rewrite
      principals.json, drop the pending row, add a family-graph node,
      audit-log.
    * ``--attach-to <existing>`` — append the (channel, sender_id) identity
      to an existing principal. Same locking discipline; idempotent
      (no-op if the identity is already there); does not touch any graph.
    """
    from familia.pending import store
    from familia import principals as principals_mod

    import re

    if args.attach_to is not None:
        return _cmd_pending_attach(args, store, principals_mod)

    new_id = args.as_id.strip()
    # Pin the shape: starts with a letter, then [A-Za-z0-9_-], <=64
    # chars. Refuses leading/trailing dashes/underscores so the id is
    # safe in shell, URL, and graph-key contexts.
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", new_id):
        print(f"error: bad principal id {new_id!r} (use letters/digits/_- starting with a letter)", file=sys.stderr)
        return 2

    # Cross-process exclusion around uniqueness-check → write → ack.
    # Without this, two admins approving in parallel can both clear
    # the duplicate-id check and produce two principal entries with
    # the same id. POSIX-only; on Windows we fall through and rely
    # on the "operator is singular" invariant of MVP deployments.
    lock_path = Path(os.environ.get("FAMILIA_PRINCIPALS_FILE",
                                    "principals.json")).with_suffix(".approve.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = None
    try:
        import fcntl

        lock_fd = open(lock_path, "w")
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
    except (ImportError, OSError) as exc:
        logger.debug("approve: file lock unavailable ({}); proceeding without it", exc)
        lock_fd = None

    try:
        # Uniqueness across all three surfaces (principals registry +
        # both graphs). The graph nodes are id-strings too — collision
        # would create ambiguous lookups.
        registry = principals_mod.get_registry()
        if registry.get(new_id) is not None:
            print(f"error: principal id {new_id!r} already exists in principals.json",
                  file=sys.stderr)
            return 2
        family = load_graph_value("shared:family.graph")
        if any((n.get("id") == new_id) for n in (family.get("nodes") or [])):
            print(f"error: id {new_id!r} already used as family-graph node",
                  file=sys.stderr)
            return 2
        topics = load_graph_value("shared:topics.graph")
        if any((n.get("id") == new_id) for n in (topics.get("nodes") or [])):
            print(f"error: id {new_id!r} already used as topics-graph node",
                  file=sys.stderr)
            return 2

        # Order of mutations is intentional and matches a
        # forward-recoverable transaction:
        #
        # 1. Read pending entry (no destructive change yet).
        # 2. Write principals.json — if this fails, pending row is intact
        #    and ``approve`` is safe to re-run after fixing the cause.
        # 3. Drop the pending row (irreversible). At this point the
        #    principal is authoritative.
        # 4. Add family-graph node — non-fatal: warning on failure, the
        #    operator can re-run ``familia graph person add-node`` by hand.
        state = store._load()
        pending_entry = next(
            (e for e in state.entries if e.channel == args.channel and e.sender_id == args.sender_id),
            None,
        )
        if pending_entry is None:
            print(f"error: no pending entry for {args.channel}:{args.sender_id}",
                  file=sys.stderr)
            return 2

        pj_path = Path(os.environ.get("FAMILIA_PRINCIPALS_FILE", "principals.json"))
        raw = json.loads(pj_path.read_text(encoding="utf-8")) if pj_path.exists() else {}
        items = raw.get("principals") or []
        items.append({
            "id": new_id,
            "display_name": args.display_name or pending_entry.display_name or new_id,
            "memx_key": f"{new_id}_key",
            "identities": [{"channel": pending_entry.channel, "sender_id": pending_entry.sender_id}],
        })
        raw["principals"] = items
        tmp = pj_path.with_suffix(pj_path.suffix + ".tmp")
        tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, pj_path)
        principals_mod.reload_registry()

        try:
            entry = store.approve(args.channel, args.sender_id)
        except KeyError:
            # Should be impossible — we just verified the row exists. Treat
            # as benign: the principal is already in registry, so the
            # approval is effectively complete.
            entry = pending_entry

        try:
            from familia.acl.graph_io import set_raw, GraphIOError
            import time as _time

            family = load_graph_value("shared:family.graph")
            nodes = list(family.get("nodes") or [])
            if not any(n.get("id") == new_id for n in nodes):
                nodes.append({
                    "id": new_id,
                    "type": "principal",
                    "display_name": args.display_name or entry.display_name or new_id,
                    "aliases": [],
                })
            family["nodes"] = nodes
            family["updated_at_ms"] = int(_time.time() * 1000)
            set_raw("shared:family.graph", family)
        except GraphIOError as exc:
            print(f"warning: principal added to principals.json but family-graph "
                  f"node creation failed: {exc}", file=sys.stderr)

        audit.log_event(
            "principal_approved",
            actor=None,
            channel=entry.channel,
            to_chat=entry.sender_id,
            reason=f"new_id={new_id}",
        )

        # Defense-in-depth: try to write the matching ACL entry into
        # memx-config/acl.json from the CLI side too. The admin app's
        # pending_approve IPC also does this over SSH (and is the
        # canonical path), but a direct CLI invocation should also leave
        # the deployment in a working state when possible.
        _try_sync_memx_acl(new_id)

        # Welcome the new principal through the channel they just
        # registered on. Best-effort: a network error or missing token
        # logs a warning but doesn't roll the approve back.
        _try_send_welcome(
            channel=entry.channel,
            sender_id=entry.sender_id,
            display_name=args.display_name or entry.display_name or new_id,
        )

        print(f"approved {entry.channel}:{entry.sender_id} as principal {new_id!r}")
        return 0
    finally:
        if lock_fd is not None:
            try:
                lock_fd.close()
            except Exception:
                pass


def _cmd_pending_attach(args: argparse.Namespace, store: Any, principals_mod: Any) -> int:
    """Attach a pending (channel, sender_id) identity to an existing principal."""
    target_id = args.attach_to.strip()
    if not target_id:
        print("error: --attach-to requires a non-empty principal id", file=sys.stderr)
        return 2

    lock_path = Path(os.environ.get("FAMILIA_PRINCIPALS_FILE",
                                    "principals.json")).with_suffix(".approve.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = None
    try:
        import fcntl

        lock_fd = open(lock_path, "w")
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
    except (ImportError, OSError) as exc:
        logger.debug("attach: file lock unavailable ({}); proceeding without it", exc)
        lock_fd = None

    try:
        registry = principals_mod.get_registry()
        if registry.get(target_id) is None:
            print(f"error: principal id {target_id!r} not found in principals.json",
                  file=sys.stderr)
            return 2

        state = store._load()
        pending_entry = next(
            (e for e in state.entries if e.channel == args.channel and e.sender_id == args.sender_id),
            None,
        )
        if pending_entry is None:
            print(f"error: no pending entry for {args.channel}:{args.sender_id}",
                  file=sys.stderr)
            return 2

        pj_path = Path(os.environ.get("FAMILIA_PRINCIPALS_FILE", "principals.json"))
        raw = json.loads(pj_path.read_text(encoding="utf-8")) if pj_path.exists() else {}
        items = raw.get("principals") or []
        target = next((p for p in items if p.get("id") == target_id), None)
        if target is None:
            print(f"error: principal id {target_id!r} disappeared between checks",
                  file=sys.stderr)
            return 2
        # Make sure the (channel, sender_id) is not already attached to a
        # DIFFERENT principal. Cross-principal collisions would break
        # resolve_actor() and produce ambiguous routing.
        for p in items:
            if p.get("id") == target_id:
                continue
            for ident in p.get("identities") or []:
                if (ident.get("channel") == args.channel
                        and str(ident.get("sender_id")) == str(args.sender_id)):
                    print(
                        f"error: identity {args.channel}:{args.sender_id} is already "
                        f"attached to principal {p.get('id')!r}; remove it there first",
                        file=sys.stderr,
                    )
                    return 2

        idents = target.get("identities") or []
        already = any(
            i.get("channel") == args.channel and str(i.get("sender_id")) == str(args.sender_id)
            for i in idents
        )
        if not already:
            idents.append({"channel": args.channel, "sender_id": args.sender_id})
            target["identities"] = idents
            tmp = pj_path.with_suffix(pj_path.suffix + ".tmp")
            tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, pj_path)
            principals_mod.reload_registry()

        try:
            store.approve(args.channel, args.sender_id)
        except KeyError:
            # Race-safe: row already gone, principals.json already mutated;
            # treat as benign success.
            pass

        audit.log_event(
            "principal_attached",
            actor=None,
            channel=args.channel,
            to_chat=args.sender_id,
            reason=f"attached_to={target_id}{' (already-present)' if already else ''}",
        )
        print(f"attached {args.channel}:{args.sender_id} to existing principal {target_id!r}"
              + (" (identity already present, no-op)" if already else ""))
        return 0
    finally:
        if lock_fd is not None:
            try:
                lock_fd.close()
            except Exception:
                pass


def cmd_pending_reject(args: argparse.Namespace) -> int:
    from familia.pending import store

    store.reject(args.channel, args.sender_id)
    print(f"rejected {args.channel}:{args.sender_id} (silenced for 24h)")
    return 0


# ---------------------------------------------------------------------------
# identity (per-principal channel identities)
# ---------------------------------------------------------------------------

def _principals_path() -> Path:
    return Path(os.environ.get("FAMILIA_PRINCIPALS_FILE", "principals.json"))


def _load_principals_json() -> tuple[Path, dict[str, Any]]:
    path = _principals_path()
    if not path.exists():
        return path, {"principals": []}
    return path, json.loads(path.read_text(encoding="utf-8"))


def _save_principals_json(path: Path, raw: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)
    from familia import principals as principals_mod
    principals_mod.reload_registry()


def cmd_identity_list(args: argparse.Namespace) -> int:
    _, raw = _load_principals_json()
    target = next((p for p in (raw.get("principals") or []) if p.get("id") == args.principal_id), None)
    if target is None:
        print(f"error: principal {args.principal_id!r} not found", file=sys.stderr)
        return 2
    idents = target.get("identities") or []
    if args.json:
        out = {
            "schema_version": 1,
            "principal_id": args.principal_id,
            "display_name": target.get("display_name") or args.principal_id,
            "identities": idents,
        }
        print(json.dumps(out, ensure_ascii=False))
        return 0
    if not idents:
        print(f"(no identities for {args.principal_id})")
        return 0
    for i in idents:
        print(f"  {i.get('channel')}:{i.get('sender_id')}")
    return 0


def cmd_identity_remove(args: argparse.Namespace) -> int:
    """Detach a (channel, sender_id) identity from a principal."""
    lock_path = _principals_path().with_suffix(".approve.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = None
    try:
        import fcntl

        lock_fd = open(lock_path, "w")
        fcntl.flock(lock_fd.fileno(), fcntl.LOCK_EX)
    except (ImportError, OSError) as exc:
        logger.debug("identity-remove: file lock unavailable ({}); proceeding", exc)
        lock_fd = None

    try:
        path, raw = _load_principals_json()
        items = raw.get("principals") or []
        target = next((p for p in items if p.get("id") == args.principal_id), None)
        if target is None:
            print(f"error: principal {args.principal_id!r} not found", file=sys.stderr)
            return 2
        before = target.get("identities") or []
        after = [
            i for i in before
            if not (i.get("channel") == args.channel
                    and str(i.get("sender_id")) == str(args.sender_id))
        ]
        if len(after) == len(before):
            print(
                f"error: identity {args.channel}:{args.sender_id} not present on "
                f"principal {args.principal_id!r}",
                file=sys.stderr,
            )
            return 2
        target["identities"] = after
        _save_principals_json(path, raw)
        audit.log_event(
            "principal_identity_removed",
            actor=None,
            channel=args.channel,
            to_chat=args.sender_id,
            reason=f"detached_from={args.principal_id}",
        )
        print(f"detached {args.channel}:{args.sender_id} from principal {args.principal_id!r}")
        return 0
    finally:
        if lock_fd is not None:
            try:
                lock_fd.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Post-approve hooks (acl.json sync + welcome message)
# ---------------------------------------------------------------------------

# Per-principal scope grants for memX. Mirrors the pattern used by
# admin-side sync_acl_for_principal so both code paths produce
# byte-identical entries.
def _scopes_for_principal(pid: str) -> list[str]:
    return [
        "shared:*",
        f"private:{pid}:*",
        f"pair:*:{pid}:*",
        f"pair:{pid}:*",
    ]


def _memx_acl_path() -> Path | None:
    """Best-effort resolve of the memX ACL file path.

    The admin install template doesn't bind-mount this into the gateway
    container by default; in that case ``FAMILIA_MEMX_ACL_FILE`` must be
    set explicitly (or the file must be reachable at one of the legacy
    fallback paths). Returns None if no candidate exists — caller is
    expected to silently skip and let the admin app handle the sync over
    SSH.
    """
    env = os.environ.get("FAMILIA_MEMX_ACL_FILE")
    if env:
        p = Path(env)
        if p.exists():
            return p
        return None
    for candidate in ("/etc/memx-config/acl.json", "/opt/familia/memx-config/acl.json"):
        p = Path(candidate)
        if p.exists():
            return p
    return None


def _try_sync_memx_acl(new_id: str) -> None:
    """Append a per-principal ACL entry to ``memx-config/acl.json``.

    Only runs when the file is actually reachable from inside the
    gateway container (env override or one of the well-known fallback
    paths). Idempotent: existing entries are left untouched.
    """
    path = _memx_acl_path()
    if path is None:
        logger.debug(
            "approve: memx-config/acl.json not reachable from gateway "
            "(FAMILIA_MEMX_ACL_FILE unset and no fallback path); "
            "admin-side IPC will sync over SSH instead"
        )
        return
    new_key = f"{new_id}_key"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if new_key in data:
            logger.debug("approve: {} already in {} — no-op", new_key, path)
            return
        data[new_key] = _scopes_for_principal(new_id)
        backup = path.with_suffix(path.suffix + f".bak.{int(time.time())}")
        backup.write_bytes(path.read_bytes())
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        logger.info("approve: appended {} to {}", new_key, path)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "approve: failed to update {} — admin app should retry over SSH ({})",
            path, exc,
        )


# Greeting text in Russian (the one user-visible language for chat right
# now). Uses Telegram/VK plaintext — no markup, both channels render it
# unchanged.
_WELCOME_TEMPLATE = (
    "👋 Привет, {display_name}!\n\n"
    "Я ассистент familia — общий бот вашей семьи. Меня настроил админ, "
    "он же только что подтвердил твой аккаунт.\n\n"
    "Что я умею:\n"
    "• помнить факты и заметки лично для тебя — например, «запомни, что "
    "я записан к врачу 5 мая»;\n"
    "• ставить напоминания — «напомни в среду в 19:00 о тренировке»;\n"
    "• подсказывать общую семейную информацию (расписания, дни рождения, "
    "бытовые мелочи), которой со мной поделились другие;\n"
    "• рассказывать про близких, к которым админ открыл тебе доступ — "
    "и наоборот, никто не увидит твою личную память без явного согласия.\n\n"
    "Если хочешь, чтобы я знал больше про тебя — просто рассказывай. "
    "Каждое решение «кому что видно» проходит через админа: ничего не уйдёт "
    "за рамки семьи.\n\n"
    "Спрашивай — отвечу."
)


def _try_send_welcome(*, channel: str, sender_id: str, display_name: str) -> None:
    """Best-effort: send a welcome message to a freshly-approved principal.

    Reads channel tokens from ``config.json`` (same file the running
    gateway uses). Failures are logged at WARNING level — never raise,
    never roll back the approve.
    """
    try:
        _, raw = _load_config_json()
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("welcome: failed to read config.json: {}", exc)
        return

    channels = raw.get("channels") or {}
    section = channels.get(channel) or {}
    text = _WELCOME_TEMPLATE.format(display_name=display_name or "")

    if channel == "telegram":
        token = section.get("token")
        if not token:
            logger.warning("welcome: telegram token missing in config.json — skipping")
            return
        # principals.json may store sender_id as ``<num>|<username>`` so
        # the operator can re-bind by username; the Bot API only accepts
        # the numeric chat id.
        chat_id = sender_id.split("|", 1)[0]
        _post_telegram(token, chat_id, text)
    elif channel == "vk":
        token = section.get("access_token") or section.get("token")
        if not token:
            logger.warning("welcome: vk token missing in config.json — skipping")
            return
        _post_vk(token, sender_id, text)
    else:
        logger.info(
            "welcome: channel {} has no greeting handler — skipping", channel
        )


def _post_telegram(token: str, chat_id: str, text: str) -> None:
    try:
        import httpx
        r = httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text},
            timeout=15,
        )
        if r.status_code != 200:
            logger.warning(
                "welcome: telegram sendMessage HTTP {} body={}",
                r.status_code, r.text[:200],
            )
        else:
            logger.info("welcome: telegram → {} ok", chat_id)
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("welcome: telegram send failed: {}", exc)


def _post_vk(token: str, user_id: str, text: str) -> None:
    try:
        import httpx
        import secrets
        r = httpx.post(
            "https://api.vk.com/method/messages.send",
            params={
                "access_token": token,
                "v": "5.131",
                "user_id": user_id,
                "random_id": str(secrets.randbits(31)),
                "message": text,
            },
            timeout=15,
        )
        # VK returns HTTP 200 even on application errors; check the body.
        try:
            body = r.json()
        except ValueError:
            body = {}
        if "error" in body:
            logger.warning(
                "welcome: vk messages.send error: {}",
                body.get("error", {}),
            )
        else:
            logger.info("welcome: vk → {} ok", user_id)
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("welcome: vk send failed: {}", exc)


# ---------------------------------------------------------------------------
# channels (poll-based adapters in config.json)
# ---------------------------------------------------------------------------

def _config_path() -> Path:
    """Resolve nanobot config.json. Prefers FAMILIA_CONFIG_FILE, then
    NANOBOT_CONFIG, then a default next to principals.json."""
    for env in ("FAMILIA_CONFIG_FILE", "NANOBOT_CONFIG"):
        v = os.environ.get(env)
        if v:
            return Path(v)
    return _principals_path().parent / "config.json"


def _load_config_json() -> tuple[Path, dict[str, Any]]:
    path = _config_path()
    if not path.exists():
        return path, {}
    return path, json.loads(path.read_text(encoding="utf-8"))


def _save_config_json(path: Path, raw: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def _redact_channel_section(name: str, section: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with secrets redacted for safe display in admin UI."""
    out = dict(section)
    for key in ("token", "access_token", "password", "api_key"):
        if isinstance(out.get(key), str) and out[key]:
            out[key] = f"***{out[key][-4:]}"
    # Proxy URLs may carry inline credentials (``socks5://user:pass@host``);
    # keep host:port visible for debugging but blank the userinfo so the
    # admin UI doesn't paint passwords.
    for proxy_key in ("proxy", "media_proxy"):
        url = out.get(proxy_key)
        if isinstance(url, str) and url:
            out[proxy_key] = _redact_proxy_url(url)
    return out


def _redact_proxy_url(url: str) -> str:
    """Mask user:pass@ in a proxy URL. Leaves scheme + host:port intact."""
    if "://" not in url:
        return url
    scheme, rest = url.split("://", 1)
    if "@" not in rest:
        return url
    creds, host = rest.rsplit("@", 1)
    if not creds:
        return url
    return f"{scheme}://***@{host}"


def cmd_channels_list(args: argparse.Namespace) -> int:
    _, raw = _load_config_json()
    channels = (raw.get("channels") or {})
    # The transcription-provider key sits next to channel sections in the
    # same dict; carve it out.
    rows: list[dict[str, Any]] = []
    for name, section in channels.items():
        if not isinstance(section, dict):
            continue
        # Skip non-channel keys (e.g. "transcriptionProvider", "sendProgress")
        if "enabled" not in section and "token" not in section and "access_token" not in section:
            continue
        rows.append({
            "name": name,
            "enabled": bool(section.get("enabled", False)),
            "config": _redact_channel_section(name, section),
            "addable": name in POLL_CHANNEL_KINDS,
        })
    rows.sort(key=lambda r: r["name"])
    if args.json:
        out = {
            "schema_version": 1,
            "supported_kinds": sorted(POLL_CHANNEL_KINDS),
            "channels": rows,
        }
        print(json.dumps(out, ensure_ascii=False))
        return 0
    if not rows:
        print("(no channels configured)")
        return 0
    for r in rows:
        on = "✓" if r["enabled"] else "✗"
        print(f"  {on} {r['name']}  {json.dumps(r['config'], ensure_ascii=False)}")
    return 0


_SECRET_FIELDS = (
    "token", "access_token", "app_token", "password",
    "client_secret", "app_secret", "secret",
)


def cmd_channels_add(args: argparse.Namespace) -> int:
    """Add or update a poll-based channel section in ``config.json``.

    Behaviour on update (``args.name`` already exists):
    -  Secret fields (``token`` / ``access_token`` / etc.) that are
       *missing* in the incoming section are preserved from the existing
       section. Lets the admin UI submit edits like "change proxy" or
       "flip enabled" without re-pasting tokens that the form can only
       show as redacted ``***xxxx``.
    -  Required-field validation considers both the incoming section and
       the existing one — so omitting ``token`` on a re-add is fine if
       the previous section had it.
    -  Anything explicitly present in the new section overrides the old
       value for that key (including secrets — that's how a real token
       rotation goes through).
    """
    if args.name not in POLL_CHANNEL_KINDS:
        print(f"error: {args.name!r} is not in supported poll kinds {sorted(POLL_CHANNEL_KINDS)}",
              file=sys.stderr)
        return 2
    try:
        section = json.loads(args.config_json)
    except json.JSONDecodeError as exc:
        print(f"error: --config is not valid JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(section, dict):
        print("error: --config must be a JSON object", file=sys.stderr)
        return 2

    path, raw = _load_config_json()
    channels = raw.setdefault("channels", {})
    existing = channels.get(args.name) if isinstance(channels.get(args.name), dict) else {}

    # Carry over any secret field that the caller didn't include in the
    # new section. We do this BEFORE validation so e.g. a proxy-only
    # edit doesn't fail on "telegram requires 'token'" just because the
    # form left the field blank.
    for key in _SECRET_FIELDS:
        if key not in section and existing.get(key):
            section[key] = existing[key]

    # Empty-string semantics on non-secret keys: the admin UI sends
    # ``""`` for an optional text field that the operator deliberately
    # cleared (e.g. removing a previously-set proxy). The cfg-merge on
    # the frontend can't distinguish "didn't touch" from "explicitly
    # cleared", so it leans on this convention. Drop those keys here
    # so the persisted section reflects the operator's intent — if we
    # left ``proxy: ""`` on disk the running adapter would read it as
    # an empty proxy URL and httpx would refuse to talk to it.
    # Secret fields are out of scope: the UI never sends an empty
    # secret directly (the merge above already filled the gap).
    for key in list(section.keys()):
        if key in _SECRET_FIELDS:
            continue
        v = section[key]
        if isinstance(v, str) and v == "":
            del section[key]

    # Light schema validation per channel — fail loudly so the operator
    # learns about missing tokens before a restart silently drops the
    # adapter from `_init_channels`.
    if args.name == "telegram":
        if not section.get("token"):
            print("error: telegram requires 'token'", file=sys.stderr)
            return 2
    elif args.name == "vk":
        if not section.get("access_token") or not section.get("group_id"):
            print("error: vk requires 'access_token' and 'group_id'", file=sys.stderr)
            return 2

    section.setdefault("enabled", True)

    channels[args.name] = section
    _save_config_json(path, raw)
    audit.log_event(
        "channel_added",
        actor=None,
        channel=args.name,
        reason="restart-required",
    )
    verb = "updated" if existing else "added"
    print(f"channel {args.name!r} {verb} in {path} (gateway restart required)")
    return 0


def _is_dep_importable(import_name: str) -> bool:
    """Return True iff ``import_name`` resolves on the current sys.path."""
    import importlib.util as _u
    return _u.find_spec(import_name) is not None


def cmd_channels_deps_status(args: argparse.Namespace) -> int:
    """List the kinds in CHANNEL_DEPS plus which ones are already
    importable (need no install) vs missing (would need install).
    Channels with no entry in CHANPP_DEPS (telegram, vk) report
    ``installed=true, no_extras=true``."""
    rows: list[dict[str, Any]] = []
    for kind in sorted(POLL_CHANNEL_KINDS):
        dep = CHANNEL_DEPS.get(kind)
        if dep is None:
            rows.append({
                "kind": kind,
                "installed": True,
                "no_extras": True,
                "import_name": None,
                "pip_spec": None,
            })
            continue
        import_name, pip_spec = dep
        rows.append({
            "kind": kind,
            "installed": _is_dep_importable(import_name),
            "no_extras": False,
            "import_name": import_name,
            "pip_spec": pip_spec,
        })
    if args.json:
        print(json.dumps({"schema_version": 1, "channels": rows}, ensure_ascii=False))
        return 0
    for r in rows:
        flag = "✓" if r["installed"] else "·"
        suffix = "(no extras)" if r["no_extras"] else f"pip:{r['pip_spec']}"
        print(f"  {flag} {r['kind']:<12} {suffix}")
    return 0


def cmd_channels_deps_install(args: argparse.Namespace) -> int:
    """Run ``pip install --user <pip_spec>`` for the requested kind.

    Uses the same Python interpreter that's executing this CLI, so it
    lands in that interpreter's user-site (which lives in the persistent
    docker volume mounted at ``/home/nanobot/.local`` per
    docker-compose.yml). Survives image rebuilds; doesn't survive volume
    deletion.
    """
    kind = args.kind
    dep = CHANNEL_DEPS.get(kind)
    if dep is None:
        print(f"channel {kind!r} has no extra deps (no-op)")
        return 0
    import_name, pip_spec = dep
    if _is_dep_importable(import_name):
        print(f"deps for {kind!r} already importable ({import_name}) — no-op")
        return 0
    import subprocess
    cmd = [sys.executable, "-m", "pip", "install", "--user", "--no-cache-dir", pip_spec]
    print(f"running: {' '.join(cmd)}", file=sys.stderr)
    try:
        result = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        print(f"error: pip install for {pip_spec!r} timed out after 300s",
              file=sys.stderr)
        return 1
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.returncode != 0:
        print(f"error: pip install failed (exit={result.returncode})",
              file=sys.stderr)
        return result.returncode
    if not _is_dep_importable(import_name):
        print(
            f"warning: pip reported success but {import_name!r} is still not "
            "importable — interpreter may need a restart to pick up the user-site",
            file=sys.stderr,
        )
    audit.log_event(
        "channel_deps_installed",
        actor=None,
        channel=kind,
        reason=f"pip_spec={pip_spec}",
    )
    print(f"installed deps for {kind!r}: {pip_spec}")
    return 0


def _channel_test_telegram(cfg: dict[str, Any]) -> tuple[bool, str]:
    import httpx
    token = (cfg.get("token") or "").strip()
    if not token:
        return False, "token is empty"
    try:
        r = httpx.get(f"https://api.telegram.org/bot{token}/getMe", timeout=10)
        data = r.json()
        if r.status_code == 200 and data.get("ok"):
            u = data.get("result", {})
            return True, f"connected as @{u.get('username')} (id={u.get('id')})"
        return False, data.get("description") or f"HTTP {r.status_code}"
    except Exception as e:
        return False, f"error: {e}"


def _channel_test_vk(cfg: dict[str, Any]) -> tuple[bool, str]:
    import httpx
    token = (cfg.get("access_token") or "").strip()
    gid = cfg.get("group_id")
    if not token or not gid:
        return False, "access_token or group_id missing"
    try:
        r = httpx.get(
            "https://api.vk.com/method/groups.getById",
            params={
                "group_id": gid,
                "access_token": token,
                "v": "5.131",
            },
            timeout=10,
        )
        data = r.json()
        if "error" in data:
            return False, str(data["error"].get("error_msg") or data["error"])
        # VK has two response shapes for groups.getById depending on
        # the ``extended`` flag and API version:
        #   - dict:  {"response": {"groups": [...]}}  (extended=1)
        #   - list:  {"response": [{...group...}, ...]} (default, v5.0+)
        # We don't request ``extended``, so the live response is a
        # bare list. The previous code chained ``.get("groups")`` on
        # whatever ``response`` was — that blew up with
        # ``'list' object has no attribute 'get'`` on every modern
        # VK token.
        resp = data.get("response")
        if isinstance(resp, dict):
            groups = resp.get("groups") or []
        elif isinstance(resp, list):
            groups = resp
        else:
            groups = []
        if groups:
            g = groups[0]
            return True, f"connected as group «{g.get('name')}» (id={g.get('id')})"
        return True, "connected (response empty)"
    except Exception as e:
        return False, f"error: {e}"


def _channel_test_discord(cfg: dict[str, Any]) -> tuple[bool, str]:
    import httpx
    token = (cfg.get("token") or "").strip()
    if not token:
        return False, "token is empty"
    try:
        r = httpx.get(
            "https://discord.com/api/v10/users/@me",
            headers={"Authorization": f"Bot {token}"},
            timeout=10,
        )
        if r.status_code == 200:
            d = r.json()
            return True, f"connected as {d.get('username')}#{d.get('discriminator', '0')} (id={d.get('id')})"
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, f"error: {e}"


def _channel_test_slack(cfg: dict[str, Any]) -> tuple[bool, str]:
    import httpx
    bot = (cfg.get("bot_token") or "").strip()
    if not bot:
        return False, "bot_token is empty"
    try:
        r = httpx.post(
            "https://slack.com/api/auth.test",
            headers={"Authorization": f"Bearer {bot}"},
            timeout=10,
        )
        d = r.json()
        if d.get("ok"):
            return True, f"connected as {d.get('user')} on team {d.get('team')}"
        return False, str(d.get("error") or "auth.test failed")
    except Exception as e:
        return False, f"error: {e}"


def _channel_test_matrix(cfg: dict[str, Any]) -> tuple[bool, str]:
    import httpx
    hs = (cfg.get("homeserver") or "").strip().rstrip("/")
    tok = (cfg.get("access_token") or "").strip()
    if not hs or not tok:
        return False, "homeserver or access_token missing"
    try:
        r = httpx.get(
            f"{hs}/_matrix/client/r0/account/whoami",
            headers={"Authorization": f"Bearer {tok}"},
            timeout=10,
        )
        if r.status_code == 200:
            return True, f"whoami: {r.json().get('user_id')}"
        return False, f"HTTP {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, f"error: {e}"


def _channel_test_email(cfg: dict[str, Any]) -> tuple[bool, str]:
    import imaplib
    host = (cfg.get("imap_host") or "").strip()
    port = int(cfg.get("imap_port") or 993)
    user = (cfg.get("user") or "").strip()
    pwd = (cfg.get("password") or "").strip()
    if not host or not user or not pwd:
        return False, "imap_host / user / password missing"
    try:
        with imaplib.IMAP4_SSL(host, port, timeout=10) as imap:
            imap.login(user, pwd)
            return True, f"IMAP login ok ({host}:{port}, user={user})"
    except Exception as e:
        return False, f"error: {e}"


_CHANNEL_TESTERS = {
    "telegram": _channel_test_telegram,
    "vk":       _channel_test_vk,
    "discord":  _channel_test_discord,
    "slack":    _channel_test_slack,
    "matrix":   _channel_test_matrix,
    "email":    _channel_test_email,
}


def cmd_channels_test(args: argparse.Namespace) -> int:
    """Live-check creds for a channel kind (no save). Returns JSON.

    Mirrors ``cmd_channels_add``'s secret-merge behaviour: when the
    caller is testing an existing channel and didn't supply secret
    fields (``token``/``access_token``/...), we pull them from the
    on-disk section before invoking the tester. Without this, the
    admin UI's "Проверить коннект" on an edit dialog always failed
    with "access_token or group_id missing" — the form deliberately
    keeps password fields empty so the redacted ``***xxxx`` preview
    isn't shipped back as the actual value.
    """
    try:
        cfg = json.loads(args.config_json)
    except json.JSONDecodeError as exc:
        out = {"ok": False, "message": f"bad --config json: {exc}"}
        print(json.dumps(out, ensure_ascii=False))
        return 0
    if not isinstance(cfg, dict):
        print(json.dumps({"ok": False, "message": "--config must be JSON object"}))
        return 0

    # Secret merge: same _SECRET_FIELDS list as cmd_channels_add.
    try:
        _, raw = _load_config_json()
        existing = (raw.get("channels") or {}).get(args.kind) or {}
        if isinstance(existing, dict):
            for key in _SECRET_FIELDS:
                if not cfg.get(key) and existing.get(key):
                    cfg[key] = existing[key]
    except (OSError, json.JSONDecodeError):
        # Test must keep working when there's no on-disk config yet
        # (fresh install) — fall through with whatever the caller
        # supplied; the tester will surface the missing-secret error.
        pass

    tester = _CHANNEL_TESTERS.get(args.kind)
    if tester is None:
        out = {
            "ok": False,
            "message": f"тест для канала {args.kind!r} ещё не реализован",
            "implemented": False,
        }
        print(json.dumps(out, ensure_ascii=False))
        return 0
    ok, message = tester(cfg)
    out = {"ok": ok, "message": message, "implemented": True}
    print(json.dumps(out, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------------------
# agents (LLM slots: main + fallback)
# ---------------------------------------------------------------------------

def _provider_for_model(model: str) -> str:
    """Best-effort provider key from a model string. Mirrors nanobot's
    keyword detection, but we keep it simple."""
    m = (model or "").lower()
    table = [
        ("openai-codex/", "openai_codex"),
        ("github_copilot/", "github_copilot"),
        ("anthropic/",     "anthropic"),
        ("claude",         "anthropic"),
        ("openai/",        "openai"),
        ("gpt-",           "openai"),
        ("openrouter/",    "openrouter"),
        ("deepseek",       "deepseek"),
        ("gemini",         "gemini"),
        ("groq/",          "groq"),
        ("yandex",         "yandex"),
        ("qwen",           "dashscope"),
        ("zhipu",          "zhipu"),
        ("glm",            "zhipu"),
        ("moonshot",       "moonshot"),
        ("kimi",           "moonshot"),
        ("mistral",        "mistral"),
        ("step",           "stepfun"),
    ]
    for kw, prov in table:
        if kw in m:
            return prov
    return ""


def _redact(s: str | None) -> str | None:
    if not s:
        return s
    if len(s) <= 8:
        return "***"
    return f"***{s[-4:]}"


def cmd_agents_get(args: argparse.Namespace) -> int:
    """Read main + fallback slot from nanobot config.json. fallback is
    a familia-specific block (``agents.familia_fallback``) — nanobot
    ignores unknown keys."""
    path, raw = _load_config_json()
    agents = raw.get("agents") or {}
    main = (agents.get("defaults") or {})
    fallback = (agents.get("familia_fallback") or {})
    providers = raw.get("providers") or {}

    def _slot(d: dict[str, Any]) -> dict[str, Any]:
        provider = d.get("provider") or ""
        if provider in ("", "auto"):
            provider = _provider_for_model(d.get("model") or "")
        prov_cfg = providers.get(provider, {}) if isinstance(providers, dict) else {}
        return {
            "model":    d.get("model", ""),
            "provider": provider,
            "api_key":  _redact(prov_cfg.get("api_key")) if isinstance(prov_cfg, dict) else None,
            "api_base": prov_cfg.get("api_base") if isinstance(prov_cfg, dict) else None,
            "context_window_tokens": d.get("context_window_tokens"),
        }

    # Models from the periodic /v1/models pull. Merged into the curated
    # supported_providers.models list below, so the admin dropdown shows
    # provider-current model ids without needing manual edits. If the
    # cache is older than 24h, fire a background refresh so the next
    # call sees fresh data (subprocess returns immediately).
    import time as _time
    cache = _load_models_cache()
    cache_max_age_ms = 24 * 60 * 60 * 1000
    now_ms = int(_time.time() * 1000)
    stale = any(
        (now_ms - int((cache.get(n) or {}).get("updated_at_ms") or 0)) > cache_max_age_ms
        for n, c in (providers.items() if isinstance(providers, dict) else [])
        if isinstance(c, dict) and (c.get("api_key") or "").strip()
    )
    if stale:
        try:
            import subprocess
            subprocess.Popen(
                [sys.executable, "-m", "familia.cli.graph_admin", "agents",
                 "refresh-models", "--json"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception:  # noqa: BLE001 — best-effort
            pass

    out = {
        "schema_version": 1,
        "main":      _slot(main),
        "fallback":  _slot(fallback) if fallback else None,
        "providers": sorted([
            n for n, c in (providers.items() if isinstance(providers, dict) else [])
            if isinstance(c, dict) and (c.get("api_key") or c.get("api_base"))
        ]),
        "models_cache": {
            n: {
                "count": len((c or {}).get("models") or []),
                "updated_at_ms": (c or {}).get("updated_at_ms"),
            }
            for n, c in (cache.items() if isinstance(cache, dict) else [])
        },
        # Hardcoded list of provider keys we expose in the UI dropdown.
        # Mirrors nanobot.providers.registry but flat. ``models`` is a
        # short curated list of well-known model ids; the UI shows them
        # as a dropdown with a free-text fallback for anything else.
        "supported_providers": [
            {"key": "openai",         "label": "OpenAI",        "is_oauth": False,
             "models": ["openai/gpt-5", "openai/gpt-5-mini", "openai/gpt-4o", "openai/gpt-4o-mini", "openai/o3", "openai/o3-mini"]},
            {"key": "anthropic",      "label": "Anthropic",     "is_oauth": False,
             "models": ["anthropic/claude-opus-4-7", "anthropic/claude-opus-4-6", "anthropic/claude-sonnet-4-6", "anthropic/claude-sonnet-4-5", "anthropic/claude-haiku-4-5"]},
            {"key": "openai_codex",   "label": "OpenAI Codex (ChatGPT subscription)", "is_oauth": True,
             "models": ["openai-codex/gpt-5.4", "openai-codex/gpt-5.1-codex", "openai-codex/gpt-5-codex"]},
            {"key": "github_copilot", "label": "GitHub Copilot", "is_oauth": True,
             "models": ["github_copilot/gpt-5", "github_copilot/claude-opus-4-7", "github_copilot/claude-sonnet-4-6"]},
            {"key": "openrouter",     "label": "OpenRouter",    "is_oauth": False,
             "models": ["openrouter/anthropic/claude-opus-4-7", "openrouter/openai/gpt-5", "openrouter/deepseek/deepseek-chat"]},
            {"key": "deepseek",       "label": "DeepSeek",      "is_oauth": False,
             "models": ["deepseek/deepseek-chat", "deepseek/deepseek-reasoner"]},
            {"key": "gemini",         "label": "Google Gemini", "is_oauth": False,
             "models": ["gemini/gemini-2.5-pro", "gemini/gemini-2.5-flash", "gemini/gemini-2.0-flash"]},
            {"key": "groq",           "label": "Groq",          "is_oauth": False,
             "models": ["groq/llama-3.3-70b-versatile", "groq/qwen/qwen3-32b", "groq/openai/gpt-oss-120b"]},
            {"key": "moonshot",       "label": "Moonshot Kimi", "is_oauth": False,
             "models": ["moonshot/kimi-k2-turbo-preview", "moonshot/kimi-latest"]},
            {"key": "mistral",        "label": "Mistral",       "is_oauth": False,
             "models": ["mistral/mistral-large-latest", "mistral/mistral-medium-latest", "mistral/codestral-latest"]},
            {"key": "dashscope",      "label": "DashScope (Qwen)", "is_oauth": False,
             "models": ["dashscope/qwen3-coder-plus", "dashscope/qwen-max", "dashscope/qwen3-max"]},
            {"key": "zhipu",          "label": "Zhipu GLM",     "is_oauth": False,
             "models": ["zhipu/glm-4.6", "zhipu/glm-4.5"]},
            {"key": "yandex",         "label": "Yandex (STT only)", "is_oauth": False, "models": []},
            {"key": "custom",         "label": "Custom (OpenAI-compatible)", "is_oauth": False, "models": []},
        ],
    }

    # Merge cache models with curated. Cache ids come from /v1/models
    # raw, e.g. "gpt-5", but nanobot expects "openai/gpt-5". Prefix
    # with provider key when the id doesn't already contain a slash.
    for sp in out["supported_providers"]:
        key = sp["key"]
        cached = (cache.get(key) or {}).get("models") or []
        if not cached:
            continue
        prefix_key = key.replace("_", "-") if key in (
            "openai_codex", "github_copilot",
        ) else key
        normalized = []
        for m in cached:
            normalized.append(m if "/" in m else f"{prefix_key}/{m}")
        merged = list(dict.fromkeys([*normalized, *sp.get("models", [])]))
        sp["models"] = merged
        sp["models_updated_at_ms"] = (cache.get(key) or {}).get("updated_at_ms")

    if args.json:
        print(json.dumps(out, ensure_ascii=False))
        return 0
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_agents_set(args: argparse.Namespace) -> int:
    slot = args.slot
    model = args.model.strip()
    provider = args.provider.strip() or _provider_for_model(model)
    if not model:
        print("error: --model is required", file=sys.stderr)
        return 2
    path, raw = _load_config_json()
    raw.setdefault("agents", {})
    raw.setdefault("providers", {})
    target_key = "defaults" if slot == "main" else "familia_fallback"
    section = raw["agents"].get(target_key) or {}
    section["model"] = model
    section["provider"] = provider or "auto"
    raw["agents"][target_key] = section

    if provider and (args.api_key or args.api_base):
        prov = raw["providers"].get(provider) or {}
        if args.api_key:
            prov["api_key"] = args.api_key
        if args.api_base:
            prov["api_base"] = args.api_base
        raw["providers"][provider] = prov

    _save_config_json(path, raw)
    audit.log_event(
        "agent_slot_set",
        actor=None,
        reason=f"slot={slot} model={model} provider={provider or 'auto'}",
    )
    print(f"agent slot {slot!r} set: model={model} provider={provider or 'auto'} "
          f"(gateway restart required)")
    return 0


def cmd_agents_clear(args: argparse.Namespace) -> int:
    path, raw = _load_config_json()
    agents = raw.get("agents") or {}
    if args.slot == "fallback":
        if "familia_fallback" in agents:
            del agents["familia_fallback"]
            raw["agents"] = agents
            _save_config_json(path, raw)
            audit.log_event("agent_slot_cleared", actor=None, reason="slot=fallback")
            print("fallback slot cleared (gateway restart required)")
        else:
            print("(no fallback slot configured)")
        return 0
    print(f"error: cannot clear slot {args.slot!r}", file=sys.stderr)
    return 2


def cmd_agents_test(args: argparse.Namespace) -> int:
    """Send a tiny prompt to the slot's provider and report. Uses the
    OpenAI-compat / Anthropic SDK paths where possible. For OAuth
    providers (codex, copilot) the test reports ``not implemented``."""
    _, raw = _load_config_json()
    agents = raw.get("agents") or {}
    target_key = "defaults" if args.slot == "main" else "familia_fallback"
    section = agents.get(target_key) or {}
    if not section:
        out = {"ok": False, "message": f"slot {args.slot!r} is empty"}
        print(json.dumps(out, ensure_ascii=False))
        return 0

    model = section.get("model") or ""
    provider = section.get("provider") or _provider_for_model(model)
    providers = raw.get("providers") or {}
    prov_cfg = (providers.get(provider) or {}) if isinstance(providers, dict) else {}
    api_key = (prov_cfg.get("api_key") or "").strip()
    api_base = (prov_cfg.get("api_base") or "").strip()

    if provider == "openai_codex":
        # Live ping against the Codex Responses API using the cached
        # OAuth token. No api_key is needed.
        try:
            from oauth_cli_kit import get_token as _get_codex_token  # type: ignore
        except ImportError:
            out = {"ok": False, "message": "oauth_cli_kit not installed in container"}
            print(json.dumps(out, ensure_ascii=False))
            return 0
        try:
            tok = _get_codex_token()
        except Exception as exc:  # noqa: BLE001
            tok = None
            err = str(exc)
        else:
            err = ""
        if not (tok and getattr(tok, "access", None)):
            msg = (
                "OAuth-токен codex не найден"
                + (f" ({err})" if err else "")
                + ". Запусти на VM: docker exec -it familia-gateway "
                "nanobot provider login openai-codex"
            )
            print(json.dumps({"ok": False, "message": msg}, ensure_ascii=False))
            return 0
        try:
            import httpx
            stripped = (model or "").split("/", 1)[-1]
            # Codex Responses API requires stream=true. We open the
            # stream, read the first byte (HTTP status is enough to
            # tell auth/model worked), then close.
            with httpx.stream(
                "POST",
                "https://chatgpt.com/backend-api/codex/responses",
                headers={
                    "Authorization": f"Bearer {tok.access}",
                    "chatgpt-account-id": tok.account_id or "",
                    "OpenAI-Beta": "responses=experimental",
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                    "originator": "nanobot",
                },
                json={
                    "model": stripped or "gpt-5.4",
                    "store": False,
                    "stream": True,
                    "instructions": "ping",
                    "input": [{"type": "message", "role": "user",
                               "content": [{"type": "input_text", "text": "ok"}]}],
                },
                timeout=20,
            ) as r:
                if r.status_code in (200, 201):
                    print(json.dumps({
                        "ok": True,
                        "message": f"codex login активен (account_id={tok.account_id}, model={stripped})",
                    }))
                    return 0
                # Non-2xx: drain a small chunk for the error message.
                body = r.read().decode("utf-8", errors="replace")[:300]
                print(json.dumps({
                    "ok": False,
                    "message": f"HTTP {r.status_code}: {body}",
                }, ensure_ascii=False))
                return 0
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"ok": False, "message": f"error: {exc}"}, ensure_ascii=False))
            return 0

    if provider == "github_copilot":
        try:
            from nanobot.providers.github_copilot_provider import (  # type: ignore
                _load_github_token,
            )
        except ImportError as exc:
            print(json.dumps({"ok": False, "message": f"copilot module: {exc}"}))
            return 0
        tok = _load_github_token()
        if not (tok and getattr(tok, "access", None)):
            print(json.dumps({
                "ok": False,
                "message": "GitHub Copilot токен не найден. Запусти "
                           "`nanobot provider login github-copilot` на VM.",
            }, ensure_ascii=False))
            return 0
        # Successful token presence is enough — full /chat round-trip
        # would require provider scaffolding.
        print(json.dumps({"ok": True, "message": "github_copilot токен присутствует"}))
        return 0

    if not api_key:
        out = {"ok": False, "message": f"providers.{provider}.api_key is empty"}
        print(json.dumps(out, ensure_ascii=False))
        return 0

    try:
        if provider == "anthropic":
            import httpx
            r = httpx.post(
                (api_base or "https://api.anthropic.com") + "/v1/messages",
                json={
                    "model": model.replace("anthropic/", ""),
                    "max_tokens": 8,
                    "messages": [{"role": "user", "content": "ping"}],
                },
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                timeout=15,
            )
            if r.status_code == 200:
                print(json.dumps({"ok": True, "message": "ok"}))
                return 0
            print(json.dumps({"ok": False, "message": f"HTTP {r.status_code}: {r.text[:200]}"}))
            return 0
        # OpenAI-compatible default
        import httpx
        base = api_base or {
            "openai":      "https://api.openai.com/v1",
            "openrouter":  "https://openrouter.ai/api/v1",
            "deepseek":    "https://api.deepseek.com",
            "gemini":      "https://generativelanguage.googleapis.com/v1beta/openai/",
            "groq":        "https://api.groq.com/openai/v1",
            "moonshot":    "https://api.moonshot.cn/v1",
            "mistral":     "https://api.mistral.ai/v1",
            "dashscope":   "https://dashscope.aliyuncs.com/compatible-mode/v1",
            "zhipu":       "https://open.bigmodel.cn/api/paas/v4",
        }.get(provider, "")
        if not base:
            out = {"ok": False, "message": f"unknown provider api_base for {provider!r}"}
            print(json.dumps(out, ensure_ascii=False))
            return 0
        r = httpx.post(
            f"{base.rstrip('/')}/chat/completions",
            json={
                "model": model.split("/", 1)[-1],
                "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 8,
            },
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=15,
        )
        if r.status_code == 200:
            print(json.dumps({"ok": True, "message": "ok"}))
            return 0
        print(json.dumps({"ok": False, "message": f"HTTP {r.status_code}: {r.text[:200]}"}))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "message": f"error: {exc}"}))
        return 0


# ---------------------------------------------------------------------------
# Models cache: per-provider lists pulled from /v1/models, refreshed
# periodically (a daily cron job + on-demand) and merged into the
# `agents get` snapshot so the admin dropdown stays fresh without
# manual edits.
# ---------------------------------------------------------------------------

def _models_cache_path() -> Path:
    return _principals_path().parent / "models_cache.json"


def _load_models_cache() -> dict[str, Any]:
    p = _models_cache_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_models_cache(cache: dict[str, Any]) -> None:
    p = _models_cache_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


# Per-provider /v1/models endpoint shape. Returns (url, headers, parser).
# parser receives the JSON body and returns a list of model id strings.
def _models_fetcher_for(provider: str, api_key: str, api_base: str | None
                        ) -> tuple[str, dict[str, str], Any] | None:
    bearer = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}

    def _data_id(d: dict[str, Any]) -> list[str]:
        items = d.get("data") or d.get("models") or []
        out: list[str] = []
        for it in items:
            if isinstance(it, dict):
                mid = it.get("id") or it.get("name")
                if mid:
                    out.append(str(mid))
            elif isinstance(it, str):
                out.append(it)
        return out

    bases = {
        "openai":      "https://api.openai.com/v1",
        "openrouter":  "https://openrouter.ai/api/v1",
        "deepseek":    "https://api.deepseek.com",
        "gemini":      "https://generativelanguage.googleapis.com/v1beta/openai",
        "groq":        "https://api.groq.com/openai/v1",
        "moonshot":    "https://api.moonshot.cn/v1",
        "mistral":     "https://api.mistral.ai/v1",
        "dashscope":   "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "zhipu":       "https://open.bigmodel.cn/api/paas/v4",
    }
    if provider == "anthropic":
        return (
            (api_base or "https://api.anthropic.com").rstrip("/") + "/v1/models",
            {"x-api-key": api_key, "anthropic-version": "2023-06-01", "Accept": "application/json"},
            _data_id,
        )
    if provider in bases:
        base = (api_base or bases[provider]).rstrip("/")
        return (f"{base}/models", bearer, _data_id)
    return None


def _refresh_one_provider(name: str, api_key: str, api_base: str | None,
                          ) -> tuple[bool, list[str], str]:
    """Call /v1/models for *name*. Returns (ok, models, err_msg)."""
    if not api_key:
        return False, [], "no api_key configured"
    spec = _models_fetcher_for(name, api_key, api_base)
    if spec is None:
        return False, [], f"models endpoint not known for {name!r}"
    url, headers, parser = spec
    try:
        import httpx
        r = httpx.get(url, headers=headers, timeout=15)
        if r.status_code != 200:
            return False, [], f"HTTP {r.status_code}: {r.text[:200]}"
        ids = parser(r.json())
        return True, sorted(set(ids)), ""
    except Exception as exc:  # noqa: BLE001
        return False, [], f"error: {exc}"


def cmd_agents_refresh_models(args: argparse.Namespace) -> int:
    """Pull /v1/models for every configured provider with an api_key and
    write the result into ``models_cache.json``. Prints a per-provider
    summary in JSON when --json, plain otherwise."""
    import time

    _, raw = _load_config_json()
    providers = raw.get("providers") or {}
    cache = _load_models_cache()
    now_ms = int(time.time() * 1000)

    targets: list[str]
    if args.provider:
        targets = [args.provider]
    else:
        targets = sorted([
            n for n, c in providers.items()
            if isinstance(c, dict) and (c.get("api_key") or "").strip()
        ])

    summary: dict[str, dict[str, Any]] = {}
    for name in targets:
        cfg = providers.get(name) if isinstance(providers, dict) else None
        api_key = (cfg or {}).get("api_key", "")
        api_base = (cfg or {}).get("api_base") or None
        ok, models, err = _refresh_one_provider(name, api_key, api_base)
        if ok:
            cache[name] = {
                "models": models,
                "updated_at_ms": now_ms,
            }
            summary[name] = {"ok": True, "count": len(models)}
        else:
            summary[name] = {"ok": False, "error": err}

    _save_models_cache(cache)
    if targets:
        audit.log_event(
            "models_refreshed",
            actor=None,
            reason=f"providers={','.join(targets)}",
        )

    if args.json:
        print(json.dumps({"schema_version": 1, "summary": summary},
                         ensure_ascii=False))
        return 0
    if not summary:
        print("(no providers with api_key configured)")
        return 0
    for name, s in sorted(summary.items()):
        if s.get("ok"):
            print(f"  ✓ {name:<14} {s['count']} models")
        else:
            print(f"  ✗ {name:<14} {s.get('error')}")
    return 0


def cmd_agents_oauth_status(args: argparse.Namespace) -> int:
    """Check if an OAuth provider has a stored, usable token.

    Per-provider lookup, mirroring nanobot's own callsites:

    * ``openai_codex`` — ``oauth_cli_kit.get_token()`` (no args; the
      kit hard-codes the codex provider). Logged in iff
      ``token and token.access`` truthy.
    * ``github_copilot`` — ``FileTokenStorage`` under
      ``~/.config/oauth-cli-kit/github-copilot.json`` (per
      :mod:`nanobot.providers.github_copilot_provider`).
    """
    provider = args.provider
    out: dict[str, Any] = {
        "schema_version": 1,
        "provider": provider,
        "logged_in": False,
        "hint": "",
        "account": None,
    }
    try:
        if provider in ("openai_codex", "openai-codex"):
            from oauth_cli_kit import get_token  # type: ignore

            try:
                tok = get_token()
            except Exception as exc:  # noqa: BLE001
                tok = None
                out["hint"] = f"get_token error: {exc}"
            if tok and getattr(tok, "access", None):
                out["logged_in"] = True
                out["account"] = getattr(tok, "account_id", None)
        elif provider in ("github_copilot", "github-copilot"):
            try:
                from nanobot.providers.github_copilot_provider import (  # type: ignore
                    _load_github_token,
                )
            except ImportError as exc:
                out["hint"] = f"github_copilot module unavailable: {exc}"
            else:
                tok = _load_github_token()
                if tok and getattr(tok, "access", None):
                    out["logged_in"] = True
        else:
            out["hint"] = f"OAuth status check not implemented for {provider!r}"
    except ImportError:
        out["hint"] = "oauth_cli_kit not installed in gateway image"
    if not out["logged_in"] and not out["hint"]:
        out["hint"] = (
            f"run on the VM: docker exec -it familia-gateway "
            f"nanobot provider login {provider.replace('_', '-')}"
        )
    if args.json:
        print(json.dumps(out, ensure_ascii=False))
        return 0
    print(f"provider={provider} logged_in={out['logged_in']}  hint={out['hint']}")
    return 0


def cmd_channels_remove(args: argparse.Namespace) -> int:
    path, raw = _load_config_json()
    channels = raw.get("channels") or {}
    if args.name not in channels:
        print(f"error: channel {args.name!r} is not configured", file=sys.stderr)
        return 2
    del channels[args.name]
    _save_config_json(path, raw)
    audit.log_event(
        "channel_removed",
        actor=None,
        channel=args.name,
        reason="restart-required",
    )
    print(f"channel {args.name!r} removed from {path} (gateway restart required)")
    return 0


def _cmd_channels_set_enabled(args: argparse.Namespace, enabled: bool) -> int:
    path, raw = _load_config_json()
    channels = raw.get("channels") or {}
    section = channels.get(args.name)
    if not isinstance(section, dict):
        print(f"error: channel {args.name!r} is not configured", file=sys.stderr)
        return 2
    if section.get("enabled") == enabled:
        # Idempotent — make the no-op visible to the caller but don't
        # rewrite the file (would also pollute audit log on every UI
        # toggle race).
        print(f"channel {args.name!r} already {'enabled' if enabled else 'disabled'}")
        return 0
    section["enabled"] = enabled
    _save_config_json(path, raw)
    audit.log_event(
        "channel_enabled" if enabled else "channel_disabled",
        actor=None,
        channel=args.name,
        reason="restart-required",
    )
    print(
        f"channel {args.name!r} {'enabled' if enabled else 'disabled'} "
        f"in {path} (gateway restart required)"
    )
    return 0


def cmd_channels_enable(args: argparse.Namespace) -> int:
    return _cmd_channels_set_enabled(args, True)


def cmd_channels_disable(args: argparse.Namespace) -> int:
    return _cmd_channels_set_enabled(args, False)


def cmd_channels_set_stt(args: argparse.Namespace) -> int:
    """Set the per-channel STT (voice transcription) provider override.

    ``inherit`` clears the field so the channel falls back to the global
    ``channels.transcriptionProvider`` default; ``off`` disables
    transcription entirely on that channel; otherwise the named provider
    is stored on the section.
    """
    path, raw = _load_config_json()
    channels = raw.get("channels") or {}
    section = channels.get(args.name)
    if not isinstance(section, dict):
        print(f"error: channel {args.name!r} is not configured", file=sys.stderr)
        return 2
    if args.provider == "inherit":
        section.pop("transcription_provider", None)
        section.pop("transcriptionProvider", None)
    else:
        section["transcription_provider"] = args.provider
        # Strip the legacy camelCase variant if it was lying around so we
        # don't end up with two keys disagreeing.
        section.pop("transcriptionProvider", None)
    _save_config_json(path, raw)
    audit.log_event(
        "channel_stt_set",
        actor=None,
        channel=args.name,
        reason=f"provider={args.provider};restart-required",
    )
    print(
        f"channel {args.name!r} stt={args.provider} in {path} "
        f"(gateway restart required)"
    )
    return 0


def cmd_stt_get(args: argparse.Namespace) -> int:
    """Return STT provider creds (redacted) + global default + per-channel
    overrides. Used by the admin UI to populate the STT card on Channels.
    """
    path, raw = _load_config_json()
    providers_raw = raw.get("providers") or {}
    channels_raw = raw.get("channels") or {}

    def _provider_view(key: str) -> dict[str, Any]:
        section = providers_raw.get(key) or {}
        if not isinstance(section, dict):
            section = {}
        api_key = section.get("apiKey") or section.get("api_key") or ""
        api_base = section.get("apiBase") or section.get("api_base") or ""
        folder_id = section.get("folderId") or section.get("folder_id") or ""
        return {
            "key": key,
            "api_key": f"***{api_key[-4:]}" if api_key else "",
            "api_base": api_base,
            "folder_id": folder_id,
            "configured": bool(api_key),
        }

    global_default = (
        channels_raw.get("transcriptionProvider")
        or channels_raw.get("transcription_provider")
        or "groq"
    )

    overrides: list[dict[str, Any]] = []
    for name, section in channels_raw.items():
        if not isinstance(section, dict):
            continue
        if "enabled" not in section and "token" not in section and "access_token" not in section:
            continue
        ov = (
            section.get("transcriptionProvider")
            or section.get("transcription_provider")
            or ""
        )
        overrides.append({
            "channel": name,
            "provider": ov or "inherit",
        })
    overrides.sort(key=lambda r: r["channel"])

    payload = {
        "global_default": global_default,
        "providers": [_provider_view(k) for k in sorted(STT_CRED_PROVIDERS)],
        "channels": overrides,
        "supported_choices": sorted(STT_PROVIDER_CHOICES),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def cmd_stt_set(args: argparse.Namespace) -> int:
    """Persist creds for a STT provider into ``config.providers.<provider>``."""
    if args.provider not in STT_CRED_PROVIDERS:
        print(f"error: provider {args.provider!r} doesn't own credentials "
              f"(choose from {sorted(STT_CRED_PROVIDERS)})", file=sys.stderr)
        return 2
    if args.provider == "yandex" and args.api_key and not args.folder_id:
        print(
            "warning: yandex requires --folder-id alongside --api-key; "
            "STT calls will fail until you set it.",
            file=sys.stderr,
        )

    path, raw = _load_config_json()
    providers = raw.setdefault("providers", {})
    section = providers.setdefault(args.provider, {})
    if not isinstance(section, dict):
        section = {}
        providers[args.provider] = section
    if args.api_key:
        section["apiKey"] = args.api_key
        section.pop("api_key", None)
    if args.api_base:
        section["apiBase"] = args.api_base
        section.pop("api_base", None)
    if args.folder_id:
        section["folderId"] = args.folder_id
        section.pop("folder_id", None)
    _save_config_json(path, raw)
    audit.log_event(
        "stt_provider_set",
        actor=None,
        reason=f"provider={args.provider};restart-required",
    )
    print(f"stt provider {args.provider!r} updated in {path} "
          f"(gateway restart required)")
    return 0


def cmd_stt_set_default(args: argparse.Namespace) -> int:
    """Set the global default STT provider that channels inherit."""
    path, raw = _load_config_json()
    channels = raw.setdefault("channels", {})
    if args.provider == "inherit":
        # Bookkeeping value isn't valid here — global default has to be a
        # concrete provider or "off". Treat it as "off".
        provider = "off"
    else:
        provider = args.provider
    channels["transcriptionProvider"] = provider
    channels.pop("transcription_provider", None)
    _save_config_json(path, raw)
    audit.log_event(
        "stt_default_set",
        actor=None,
        reason=f"provider={provider};restart-required",
    )
    print(f"global stt default = {provider!r} in {path} "
          f"(gateway restart required)")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    # ``rpc-server`` is the persistent NDJSON dispatcher used by the
    # admin app to avoid re-paying the per-call ``docker exec`` +
    # Python-startup tax (~3-5 s) on every action. It's spawned ONCE
    # per VM session as ``docker exec -i familia-gateway familia
    # rpc-server`` and then admin pipes one JSON request line per
    # action. We handle it specially BEFORE going through the normal
    # argparse machinery so it doesn't get treated as a regular
    # subcommand whose ``--help`` lists every implemented op.
    if argv is None:
        argv = sys.argv[1:]
    if argv == ["rpc-server"]:
        return _rpc_server_loop(p)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except GraphIOError as exc:
        if getattr(args, "json", False):
            _emit_error_json(str(exc), code="GRAPH_IO_ERROR")
        else:
            print(f"error: {exc}", file=sys.stderr)
        return 2
    except SystemExit:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("CLI fatal: {}", exc)
        if getattr(args, "json", False):
            _emit_error_json(str(exc), code="INTERNAL_ERROR")
        return 1


def _rpc_server_loop(parser: argparse.ArgumentParser) -> int:
    """Persistent NDJSON request/response loop on stdin/stdout.

    Wire format (one JSON object per line, no embedded newlines):

      Request:  ``{"id": <int|str>, "argv": [<str>, ...]}``
      Response: ``{"id": ..., "exit": <int>, "stdout": <str>, "stderr": <str>}``

    Exits cleanly on stdin EOF (admin SSH channel close = container
    will be GCed). One request is processed at a time — the SSH
    transport already has a Mutex on the channel handle on the admin
    side, and parallelism is not worth the complexity for our
    request volume.

    The whole point is to import familia ONCE (loguru, httpx,
    memx_client, principals, audit, …) and reuse the warm interpreter
    for every subsequent request. Cold-start is ~3-5 s per call
    through ``docker exec``; reusing this loop drops it to ~50 ms.
    """
    import io
    import json as _json
    import traceback as _traceback

    # Send a small banner so the admin can confirm the channel is live.
    sys.stdout.write(_json.dumps(
        {"banner": "familia rpc-server v1", "ready": True}
    ) + "\n")
    sys.stdout.flush()

    while True:
        try:
            line = sys.stdin.readline()
        except KeyboardInterrupt:
            return 0
        if not line:
            # stdin closed → admin disconnected. Exit cleanly so the
            # container's docker exec wrapper terminates.
            return 0
        line = line.strip()
        if not line:
            continue
        try:
            req = _json.loads(line)
        except ValueError as exc:
            sys.stdout.write(_json.dumps(
                {"id": None, "exit": 2, "stdout": "",
                 "stderr": f"rpc-server: bad request json: {exc}"}
            ) + "\n")
            sys.stdout.flush()
            continue

        req_id = req.get("id")
        argv = req.get("argv") or []
        if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
            sys.stdout.write(_json.dumps(
                {"id": req_id, "exit": 2, "stdout": "",
                 "stderr": "rpc-server: argv must be list of strings"}
            ) + "\n")
            sys.stdout.flush()
            continue

        # Capture stdout + stderr from the command. Each command writes
        # its JSON payload to stdout on success, or a structured error
        # envelope to stdout/stderr on failure — same wire as the
        # one-shot CLI mode, so admin's parser stays the same.
        old_stdout, old_stderr = sys.stdout, sys.stderr
        out_buf, err_buf = io.StringIO(), io.StringIO()
        sys.stdout, sys.stderr = out_buf, err_buf
        try:
            try:
                args = parser.parse_args(argv)
            except SystemExit as exc:
                # argparse already printed help/error to stderr (now
                # captured). Surface as a non-zero exit; admin will see
                # the captured stderr.
                exit_code = exc.code if isinstance(exc.code, int) else 2
                resp = {"id": req_id, "exit": exit_code,
                        "stdout": out_buf.getvalue(),
                        "stderr": err_buf.getvalue()}
            else:
                try:
                    rc = args.func(args)
                except GraphIOError as exc:
                    if getattr(args, "json", False):
                        _emit_error_json(str(exc), code="GRAPH_IO_ERROR")
                    else:
                        print(f"error: {exc}", file=sys.stderr)
                    rc = 2
                except SystemExit as exc:
                    rc = exc.code if isinstance(exc.code, int) else 1
                except Exception as exc:
                    if getattr(args, "json", False):
                        _emit_error_json(str(exc), code="INTERNAL_ERROR")
                    else:
                        print(f"error: {exc}", file=sys.stderr)
                        _traceback.print_exc(file=sys.stderr)
                    rc = 1
                resp = {"id": req_id, "exit": int(rc) if rc is not None else 0,
                        "stdout": out_buf.getvalue(),
                        "stderr": err_buf.getvalue()}
        finally:
            sys.stdout, sys.stderr = old_stdout, old_stderr

        try:
            sys.stdout.write(_json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except (BrokenPipeError, OSError):
            # admin went away — bail.
            return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
