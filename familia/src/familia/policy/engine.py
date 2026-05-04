"""Policy engine for familia.

Declarative, first-match-wins rule evaluation for agent-initiated
actions (``message.send`` and, later, others).  Rules are loaded once
from a YAML file whose path is taken from ``FAMILIA_POLICY_FILE`` with a
fallback to ``policy.yaml`` in the current working directory.

Matchers are all optional; a rule fires when every supplied matcher
matches the given context.  Each matcher accepts either a single value
or a list of values; ``"*"`` means "any".  String matching is exact,
with a trailing ``*`` treated as a prefix wildcard (e.g. ``message.*``).

YAML shape::

    rules:
      - name: "owner can message anyone"
        action: message.send
        actor: owner
        decision: allow

      - name: "kids can only message owner"
        action: message.send
        actor: [child_a, child_b]
        to_chat: "!owner_chat"    # leading ! negates
        decision: deny
        reason: "external messaging disabled for children"

Decisions: ``allow``, ``deny``, ``ask`` (ask is accepted and returned
but not wired until the pending-approvals pass).

Default behavior:
- No policy file → empty engine → every action is allowed (upstream
  behavior preserved).
- Policy file present but no rule matches → **deny** with reason
  ``no matching rule``.  Explicit policies must cover their surface.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

import yaml
from loguru import logger

from familia import audit
from familia.acl.peers import is_peer
from familia.roles import get_effective_roles


class Decision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


_MATCHER_FIELDS = ("action", "actor", "channel", "to_channel", "to_chat")


@dataclass
class PolicyRule:
    name: str = ""
    action: list[str] = field(default_factory=list)
    actor: list[str] = field(default_factory=list)
    channel: list[str] = field(default_factory=list)
    to_channel: list[str] = field(default_factory=list)
    to_chat: list[str] = field(default_factory=list)
    decision: Decision = Decision.DENY
    reason: str = ""
    approver: list[str] = field(default_factory=list)

    def matches(self, ctx: PolicyContext) -> bool:
        if self.actor and not _match_actor(self.actor, ctx.actor):
            return False
        # ``@actor`` in to_chat patterns is substituted with the
        # current actor's id at evaluation time. Lets one rule like
        # ``to_chat: "private:@actor:*"`` cover every registered
        # principal's own scope without per-actor rule duplication.
        # When ``ctx.actor`` is falsy (system turns, unresolved sender
        # — both should be handled by their own dedicated rules), any
        # rule that uses ``@actor`` is treated as non-matching to
        # avoid surprises like ``private::*`` matching by accident.
        if self.to_chat and any("@actor" in p for p in self.to_chat):
            if not ctx.actor:
                return False
            to_chat_patterns = [p.replace("@actor", ctx.actor) for p in self.to_chat]
        else:
            to_chat_patterns = self.to_chat
        checks = (
            (self.action, ctx.action),
            (self.channel, ctx.channel),
            (self.to_channel, ctx.to_channel),
            (to_chat_patterns, ctx.to_chat),
        )
        for patterns, value in checks:
            if not patterns:
                continue
            if not _match_any(patterns, value):
                return False
        return True


@dataclass
class PolicyContext:
    action: str
    actor: str | None = None
    channel: str | None = None
    to_channel: str | None = None
    to_chat: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class PolicyDecision:
    decision: Decision
    rule: PolicyRule | None = None

    @property
    def reason(self) -> str:
        return self.rule.reason if self.rule else ""

    @property
    def approver(self) -> list[str]:
        return list(self.rule.approver) if self.rule else []


def _match_any(patterns: list[str], value: str | None) -> bool:
    """True iff `value` matches at least one pattern.

    A leading ``!`` negates (rule matches when value does NOT match the
    rest).  A trailing ``*`` is a prefix wildcard.  A bare ``"*"`` matches
    any non-None value.  An empty pattern list is treated as unrestricted
    by the caller, so this function is only called when patterns exist.
    """
    positive_hit = False
    has_positive = False
    for raw in patterns:
        negate = raw.startswith("!")
        pat = raw[1:] if negate else raw
        hit = _match_one(pat, value)
        if negate:
            if hit:
                return False
        else:
            has_positive = True
            positive_hit = positive_hit or hit
    return positive_hit if has_positive else True


def _match_actor(patterns: list[str], actor: str | None) -> bool:
    """Like ``_match_any`` but with ``@role`` and ``@peer_of:<id>`` matchers.

    ``@admin`` matches any actor whose effective roles (static principal
    roles ∪ active admin grants) include ``admin``.

    ``@peer_of:<id>`` matches when ``actor`` and ``<id>`` are peer
    principals — see :mod:`familia.acl.peers` for the rule. The
    relation is symmetric: ``@peer_of:<owner>`` matches any actor
    iff there is a ``spouse_of``/``guardian_of`` edge between them
    and the named principal in the family graph and neither side has
    ``role: child``.

    ``@family_of:<id>`` matches the looser "any edge in the family
    graph" relation (:func:`familia.acl.peers.is_family_member`).
    Use it for visibility-only rules (e.g. surfacing shared-key
    indexes) where a child should see their parent's listings even
    though they are not peers for private-memory purposes.

    Negation ``!@…`` is supported for both. Id-based patterns still go
    through :func:`_match_any`.
    """
    id_patterns: list[str] = []
    # Each predicate is (negate, kind, arg). ``kind`` is "role",
    # "peer_of", "family_of", or "principal".
    predicates: list[tuple[bool, str, str]] = []
    for raw in patterns:
        negate = raw.startswith("!")
        body = raw[1:] if negate else raw
        if body.startswith("@peer_of:"):
            predicates.append((negate, "peer_of", body[len("@peer_of:"):]))
        elif body.startswith("@family_of:"):
            predicates.append((negate, "family_of", body[len("@family_of:"):]))
        elif body == "@principal":
            # Any actor that resolves to a registered principal id —
            # the "I am someone the system knows" cohort. Lets us
            # write generic rules for "any approved principal" without
            # listing per-actor names.
            predicates.append((negate, "principal", ""))
        elif body.startswith("@"):
            predicates.append((negate, "role", body[1:]))
        else:
            id_patterns.append(raw)

    if not predicates:
        return _match_any(id_patterns, actor)

    # Resolve roles + peers lazily so we don't hit memX or principals.json
    # for rules that don't need them. ``hit`` per predicate is computed
    # below; the early-return on a negation that fires preserves the
    # "first match wins for negative-eliminations" semantics from the
    # role-only path.
    has_positive = False
    positive_hit = False
    cached_roles: set[str] | None = None

    def _roles() -> set[str]:
        nonlocal cached_roles
        if cached_roles is None:
            cached_roles = set(get_effective_roles(actor))
        return cached_roles

    for negate, kind, arg in predicates:
        if kind == "role":
            hit = arg in _roles()
        elif kind == "peer_of":
            hit = is_peer(actor, arg)
        elif kind == "family_of":
            from familia.acl.peers import is_family_member  # noqa: PLC0415
            hit = is_family_member(actor, arg)
        elif kind == "principal":
            if not actor:
                hit = False
            else:
                from familia import principals as principals_mod

                hit = principals_mod.get_registry().get(actor) is not None
        else:
            hit = False
        if negate:
            if hit:
                return False
        else:
            has_positive = True
            positive_hit = positive_hit or hit

    if id_patterns:
        return positive_hit or _match_any(id_patterns, actor)
    return positive_hit if has_positive else True


def _match_one(pattern: str, value: str | None) -> bool:
    # Invariant: the literal empty-string pattern ("") matches a missing
    # (None) value. This lets policy.yaml write `actor: ""` to target the
    # "no actor set" case (e.g. system turns) without also matching real
    # actors. `*` matches any non-None value.
    if pattern == "*":
        return value is not None
    if value is None:
        return pattern == ""
    if pattern.endswith("*"):
        return value.startswith(pattern[:-1])
    return value == pattern


def _as_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    return [str(v)]


class PolicyEngine:
    def __init__(self, rules: list[PolicyRule] | None = None) -> None:
        self.rules: list[PolicyRule] = list(rules or [])

    @property
    def empty(self) -> bool:
        return not self.rules

    def evaluate(self, ctx: PolicyContext) -> PolicyDecision:
        if self.empty:
            # No policy loaded → preserve upstream permissive behavior.
            decision = PolicyDecision(decision=Decision.ALLOW, rule=None)
            self._audit(ctx, decision, rule_name="__empty_engine__")
            return decision
        for rule in self.rules:
            if rule.matches(ctx):
                decision = PolicyDecision(decision=rule.decision, rule=rule)
                self._audit(ctx, decision, rule_name=rule.name)
                return decision
        # Policy defined but no rule matched → deny by default.
        fallback = PolicyRule(
            name="__default_deny__",
            decision=Decision.DENY,
            reason="no matching rule",
        )
        decision = PolicyDecision(decision=Decision.DENY, rule=fallback)
        self._audit(ctx, decision, rule_name=fallback.name)
        return decision

    @staticmethod
    def _audit(ctx: PolicyContext, decision: PolicyDecision, rule_name: str) -> None:
        audit.log_event(
            "policy",
            action=ctx.action,
            actor=ctx.actor,
            channel=ctx.channel,
            to_channel=ctx.to_channel,
            to_chat=ctx.to_chat,
            decision=decision.decision.value,
            rule=rule_name,
            reason=decision.reason,
        )


def _default_path() -> Path:
    return Path(os.environ.get("FAMILIA_POLICY_FILE", "policy.yaml"))


def load_engine(path: Path | None = None) -> PolicyEngine:
    path = path or _default_path()
    if not path.exists():
        logger.debug("familia policy: no file at {}, engine is empty", path)
        return PolicyEngine()
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.warning("familia policy: failed to read {}: {}", path, exc)
        return PolicyEngine()

    rules: list[PolicyRule] = []
    for entry in raw.get("rules", []) or []:
        try:
            decision = Decision(str(entry.get("decision", "deny")).lower())
        except ValueError:
            logger.warning("familia policy: skipping rule with unknown decision: {}", entry)
            continue
        rules.append(
            PolicyRule(
                name=str(entry.get("name", "")),
                action=_as_list(entry.get("action")),
                actor=_as_list(entry.get("actor")),
                channel=_as_list(entry.get("channel")),
                to_channel=_as_list(entry.get("to_channel")),
                to_chat=_as_list(entry.get("to_chat")),
                decision=decision,
                reason=str(entry.get("reason", "")),
                approver=_as_list(entry.get("approver")),
            )
        )
    logger.info("familia policy: loaded {} rules from {}", len(rules), path)
    return PolicyEngine(rules)


_engine: PolicyEngine | None = None
_lock = threading.Lock()


def get_engine() -> PolicyEngine:
    global _engine
    if _engine is None:
        with _lock:
            if _engine is None:
                _engine = load_engine()
    return _engine


def reload_engine(path: Path | None = None) -> PolicyEngine:
    global _engine
    with _lock:
        _engine = load_engine(path)
    return _engine
