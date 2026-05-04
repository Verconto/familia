"""Tests for ``@actor`` substitution and the ``@principal`` matcher in
``familia.policy.engine`` (SR-12).
"""

from __future__ import annotations

import pytest

from familia.policy.engine import (
    Decision,
    PolicyContext,
    PolicyEngine,
    PolicyRule,
)


def _rule(**kw):
    """Convenience: build a single PolicyRule from kwargs."""
    kw.setdefault("decision", Decision.ALLOW)
    fields = {
        "name": kw.pop("name", "test"),
        "decision": kw.pop("decision"),
        "reason": kw.pop("reason", ""),
        "action": _as_list(kw.pop("action", None)),
        "actor": _as_list(kw.pop("actor", None)),
        "to_chat": _as_list(kw.pop("to_chat", None)),
    }
    return PolicyRule(**fields)


def _as_list(v):
    if v is None:
        return []
    if isinstance(v, str):
        return [v]
    return list(v)


def _engine_with(*rules):
    return PolicyEngine(list(rules))


def test_at_actor_substituted_in_to_chat():
    """``private:@actor:*`` allows actor==alice into ``private:alice:*``."""
    rule = _rule(action="memory.read", actor="@principal", to_chat="private:@actor:*")
    eng = _engine_with(rule)

    # Use a stub registry that recognises 'alice'.
    from familia import principals as principals_mod

    registry = principals_mod.PrincipalRegistry()
    registry.add(principals_mod.Principal(id="alice"))
    principals_mod._registry = registry

    ctx = PolicyContext(action="memory.read", actor="alice", to_chat="private:alice:notes")
    assert eng.evaluate(ctx).decision is Decision.ALLOW

    # Non-matching to_chat goes to default-deny.
    ctx_other = PolicyContext(action="memory.read", actor="alice", to_chat="private:bob:notes")
    assert eng.evaluate(ctx_other).decision is Decision.DENY


def test_at_actor_skipped_when_actor_none():
    """A rule that uses ``@actor`` should NOT match when ``ctx.actor`` is
    falsy; otherwise ``private::*`` would match an unset to_chat."""
    rule = _rule(action="memory.read", to_chat="private:@actor:*")
    eng = _engine_with(rule)

    ctx = PolicyContext(action="memory.read", actor=None,
                        to_chat="private::notes")
    # Default-deny because the rule was skipped.
    assert eng.evaluate(ctx).decision is Decision.DENY


def test_principal_matcher_excludes_unknown():
    """``@principal`` only matches actors in principals.json."""
    rule = _rule(action="memory.read", actor="@principal", to_chat="shared:*")
    eng = _engine_with(rule)

    from familia import principals as principals_mod

    registry = principals_mod.PrincipalRegistry()
    registry.add(principals_mod.Principal(id="alice"))
    principals_mod._registry = registry

    assert eng.evaluate(
        PolicyContext(action="memory.read", actor="alice", to_chat="shared:weekly")
    ).decision is Decision.ALLOW
    assert eng.evaluate(
        PolicyContext(action="memory.read", actor="stranger", to_chat="shared:weekly")
    ).decision is Decision.DENY


def test_peer_of_matcher_uses_graph(monkeypatch):
    """``@peer_of:<id>`` resolves through ``acl.peers.is_peer``."""
    rule = _rule(action="memory.read", actor="@peer_of:owner", to_chat="private:owner:*")
    eng = _engine_with(rule)

    # Stub the peer resolver — we test its real behaviour separately.
    from familia.policy import engine as engine_mod

    monkeypatch.setattr(engine_mod, "is_peer", lambda actor, target: actor == "spouse")

    assert eng.evaluate(
        PolicyContext(action="memory.read", actor="spouse", to_chat="private:owner:diary")
    ).decision is Decision.ALLOW
    assert eng.evaluate(
        PolicyContext(action="memory.read", actor="stranger", to_chat="private:owner:diary")
    ).decision is Decision.DENY


def test_family_of_matcher_uses_graph(monkeypatch):
    """``@family_of:<id>`` resolves through ``acl.peers.is_family_member``.

    Looser than @peer_of: a child still matches @family_of even though
    they're not a private-memory peer.
    """
    rule = _rule(
        action="memory.read",
        actor="@family_of:owner",
        to_chat="private:owner:value:shared_index",
    )
    eng = _engine_with(rule)

    from familia.acl import peers as peers_mod

    monkeypatch.setattr(
        peers_mod,
        "is_family_member",
        lambda actor, target: actor in {"spouse", "daughter"},
    )

    assert eng.evaluate(
        PolicyContext(
            action="memory.read", actor="spouse",
            to_chat="private:owner:value:shared_index",
        )
    ).decision is Decision.ALLOW
    assert eng.evaluate(
        PolicyContext(
            action="memory.read", actor="daughter",
            to_chat="private:owner:value:shared_index",
        )
    ).decision is Decision.ALLOW
    assert eng.evaluate(
        PolicyContext(
            action="memory.read", actor="stranger",
            to_chat="private:owner:value:shared_index",
        )
    ).decision is Decision.DENY
