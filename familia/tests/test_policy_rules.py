"""Policy rules regression test (familia).

Loads the real ``policy.yaml`` from the packaged config and checks every
meaningful combination of (action, actor, to_chat) against the expected
decision.  Any rule change that breaks existing behavior fails here.

Actor ids (`owner`, `member_a`) and chat ids below are the sanitized
placeholders shipped in ``familia/src/familia/config/policy.yaml``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from familia.policy import Decision, PolicyContext
from familia.policy.engine import PolicyEngine, load_engine


OWNER_CHAT = "1000001"
MEMBER_A_CHAT = "1000002"
STRANGER_CHAT = "9999999"


@pytest.fixture(scope="module")
def engine() -> PolicyEngine:
    path = Path(__file__).resolve().parents[1] / "src" / "familia" / "config" / "policy.yaml"
    assert path.exists(), f"policy.yaml not found at {path}"
    eng = load_engine(path)
    assert eng.rules, "policy.yaml loaded 0 rules — check path and file contents"
    return eng


# (action, actor, to_chat, expected_decision, rule_name_contains)
_CASES = [
    # Owner — разрешено всё
    ("message.send", "owner", OWNER_CHAT, Decision.ALLOW, "owner"),
    ("message.send", "owner", MEMBER_A_CHAT, Decision.ALLOW, "owner"),
    ("message.send", "owner", STRANGER_CHAT, Decision.ALLOW, "owner"),
    ("ask.send",     "owner", MEMBER_A_CHAT, Decision.ALLOW, "owner"),
    ("ask.send",     "owner", STRANGER_CHAT, Decision.ALLOW, "owner"),

    # member_a → owner allow
    ("message.send", "member_a", OWNER_CHAT, Decision.ALLOW, "member_a"),
    ("ask.send",     "member_a", OWNER_CHAT, Decision.ALLOW, "member_a"),

    # member_a → любой другой — требует подтверждения владельца (ASK)
    ("message.send", "member_a", MEMBER_A_CHAT, Decision.ASK, "требует подтверждения"),
    ("message.send", "member_a", STRANGER_CHAT, Decision.ASK, "требует подтверждения"),
    ("ask.send",     "member_a", STRANGER_CHAT, Decision.ASK, "требует подтверждения"),

    # System (actor=None) → owner allow, остальное — fallback deny
    ("message.send", None, OWNER_CHAT, Decision.ALLOW, "system"),
    ("message.send", None, STRANGER_CHAT, Decision.DENY, "__default_deny__"),

    # Неизвестный actor — default deny
    ("message.send", "stranger", OWNER_CHAT, Decision.DENY, "__default_deny__"),

    # memory.* — owner: всё разрешено
    ("memory.read",  "owner", "shared:todo",                  Decision.ALLOW, "owner: memory"),
    ("memory.write", "owner", "shared:todo",                  Decision.ALLOW, "owner: memory"),
    ("memory.read",  "owner", "private:owner:secret",         Decision.ALLOW, "owner: memory"),
    ("memory.write", "owner", "private:member_a:x",           Decision.ALLOW, "owner: memory"),
    ("memory.read",  "owner", "pair:member_a_owner:note",     Decision.ALLOW, "owner: memory"),

    # memory.* — member_a
    ("memory.read",  "member_a", "shared:todo",                  Decision.ALLOW, "member_a: memory shared"),
    ("memory.write", "member_a", "shared:todo",                  Decision.ALLOW, "member_a: memory shared"),
    ("memory.read",  "member_a", "private:member_a:secret",      Decision.ALLOW, "member_a: memory private"),
    ("memory.write", "member_a", "private:member_a:secret",      Decision.ALLOW, "member_a: memory private"),
    ("memory.read",  "member_a", "pair:member_a_owner:note",     Decision.ALLOW, "member_a: memory pair"),
    ("memory.write", "member_a", "pair:member_a_owner:note",     Decision.ALLOW, "member_a: memory pair"),
    ("memory.read",  "member_a", "private:owner:secret",         Decision.DENY,  "member_a: memory"),
    ("memory.write", "member_a", "private:owner:secret",         Decision.DENY,  "member_a: memory"),
    ("memory.read",  "member_a", "pair:child_a_owner:note",      Decision.DENY,  "member_a: memory"),

    # memory.* — system: только shared
    ("memory.read",  None, "shared:x",                            Decision.ALLOW, "system: memory shared"),
    ("memory.write", None, "shared:x",                            Decision.ALLOW, "system: memory shared"),
    ("memory.read",  None, "private:owner:x",                     Decision.DENY,  "__default_deny__"),

    # memory.* — dream_consolidator: write любой scope (для per-scope Dream)
    ("memory.write", "dream_consolidator", "shared:x",                     Decision.ALLOW, "dream_consolidator"),
    ("memory.write", "dream_consolidator", "private:member_a:secret",      Decision.ALLOW, "dream_consolidator"),
    ("memory.write", "dream_consolidator", "pair:member_a_owner:note",     Decision.ALLOW, "dream_consolidator"),
    # но читать — нельзя
    ("memory.read",  "dream_consolidator", "private:member_a:secret",      Decision.DENY,  "__default_deny__"),
]


@pytest.mark.parametrize("action,actor,to_chat,expected,rule_hint", _CASES)
def test_policy_decision(
    engine: PolicyEngine,
    action: str,
    actor: str | None,
    to_chat: str,
    expected: Decision,
    rule_hint: str,
) -> None:
    ctx = PolicyContext(
        action=action,
        actor=actor,
        channel="vk",
        to_channel="vk",
        to_chat=to_chat,
    )
    result = engine.evaluate(ctx)
    assert result.decision is expected, (
        f"{action} actor={actor} to={to_chat}: "
        f"expected {expected.value}, got {result.decision.value} "
        f"(rule={result.rule.name if result.rule else '-'})"
    )
    assert result.rule is not None
    assert rule_hint.lower() in result.rule.name.lower(), (
        f"expected rule containing {rule_hint!r}, got {result.rule.name!r}"
    )


def test_empty_engine_allows_everything() -> None:
    eng = PolicyEngine(rules=[])
    ctx = PolicyContext(action="message.send", actor="anyone", to_chat="any")
    result = eng.evaluate(ctx)
    assert result.decision is Decision.ALLOW
    assert result.rule is None


def test_missing_policy_file_yields_empty_engine(tmp_path: Path) -> None:
    eng = load_engine(tmp_path / "no_such_policy.yaml")
    assert eng.empty
    ctx = PolicyContext(action="anything", actor="owner")
    assert eng.evaluate(ctx).decision is Decision.ALLOW


def test_default_deny_when_rules_exist_but_none_match(tmp_path: Path) -> None:
    p = tmp_path / "policy.yaml"
    p.write_text(
        "rules:\n"
        "  - name: 'only owner message'\n"
        "    action: message.send\n"
        "    actor: owner\n"
        "    decision: allow\n",
        encoding="utf-8",
    )
    eng = load_engine(p)
    # Matching rule → allow
    ok = eng.evaluate(PolicyContext(
        action="message.send", actor="owner", to_chat="x",
    ))
    assert ok.decision is Decision.ALLOW

    # No matching rule → default deny
    bad = eng.evaluate(PolicyContext(
        action="message.send", actor="someone_else", to_chat="x",
    ))
    assert bad.decision is Decision.DENY
    assert bad.rule is not None and bad.rule.name == "__default_deny__"

    # Unknown action → default deny
    unknown = eng.evaluate(PolicyContext(
        action="memory.read", actor="owner",
    ))
    assert unknown.decision is Decision.DENY
