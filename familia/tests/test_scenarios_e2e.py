"""SCENARIOS.md — базовые e2e-сценарии (familia).

Покрывает 3 foundational сценария из SCENARIOS.md, каждый прогоняется
через реальный policy engine + реальный principals registry, подгружая
packaged `policy.yaml` и синтетический `principals.json` под те же
placeholder-идентификаторы.

Сценарии:
- #17  Утренняя сводка (cron/system, actor=None) → только owner.
- #22  Сообщение обоим → owner → два независимых message.send allow.
- #39+#42  Inline approval flow: member_a → посторонний чат → Decision.ASK,
          approver ровно owner, PendingStore round-trip.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from familia.policy import Decision, PolicyContext
from familia.policy.engine import PolicyEngine, load_engine
from familia.policy.pending import PendingStore
from familia.principals import PrincipalRegistry, load_registry
from nanobot.bus.events import OutboundMessage


REPO_ROOT = Path(__file__).resolve().parents[1] / "src" / "familia" / "config"
OWNER_CHAT = "1000001"
MEMBER_A_CHAT = "1000002"
STRANGER_CHAT = "9999999"


@pytest.fixture(scope="module")
def engine() -> PolicyEngine:
    return load_engine(REPO_ROOT / "policy.yaml")


@pytest.fixture(scope="module")
def principals_data() -> dict:
    return {
        "principals": [
            {
                "id": "owner",
                "display_name": "Owner",
                "memx_key": "owner_key",
                "identities": [
                    {"channel": "vk", "sender_id": OWNER_CHAT},
                    {"channel": "telegram", "sender_id": OWNER_CHAT},
                ],
            },
            {
                "id": "member_a",
                "display_name": "Member A",
                "memx_key": "member_a_key",
                "identities": [{"channel": "vk", "sender_id": MEMBER_A_CHAT}],
            },
        ]
    }


@pytest.fixture(scope="module")
def principals(tmp_path_factory, principals_data) -> PrincipalRegistry:
    path = tmp_path_factory.mktemp("familia") / "principals.json"
    path.write_text(json.dumps(principals_data), encoding="utf-8")
    return load_registry(path)


# --- Scenario #17 ----------------------------------------------------------

def test_scenario_17_morning_summary_only_owner(engine: PolicyEngine) -> None:
    """Cron-job без принципала (actor=None) может писать только владельцу."""
    to_owner = engine.evaluate(PolicyContext(
        action="message.send", actor=None, channel="vk",
        to_channel="vk", to_chat=OWNER_CHAT,
    ))
    assert to_owner.decision is Decision.ALLOW
    assert to_owner.rule and "system" in to_owner.rule.name

    # Попытка системы написать non-owner (например, ошибочный cron)
    to_other = engine.evaluate(PolicyContext(
        action="message.send", actor=None, channel="vk",
        to_channel="vk", to_chat=MEMBER_A_CHAT,
    ))
    assert to_other.decision is Decision.DENY


# --- Scenario #22 ----------------------------------------------------------

def test_scenario_22_dog_health_to_both(engine: PolicyEngine) -> None:
    """«Обоим» = два отдельных message.send от owner-actor.

    Проверяем, что каждый из двух отправок от владельца проходит policy.
    """
    for chat in (OWNER_CHAT, MEMBER_A_CHAT):
        r = engine.evaluate(PolicyContext(
            action="message.send", actor="owner",
            channel="vk", to_channel="vk", to_chat=chat,
        ))
        assert r.decision is Decision.ALLOW, f"send to {chat}: {r.decision}"
        assert r.rule and "owner" in r.rule.name


# --- Scenario #39 + #42 ----------------------------------------------------

def test_scenario_39_42_ask_flow_roundtrip(engine: PolicyEngine) -> None:
    """member_a → посторонний чат → ASK(approver=owner) → park/take."""
    # 1. Policy отдаёт ASK с правильным approver'ом.
    res = engine.evaluate(PolicyContext(
        action="message.send", actor="member_a",
        channel="vk", to_channel="vk", to_chat=STRANGER_CHAT,
    ))
    assert res.decision is Decision.ASK
    assert res.rule is not None
    assert "owner" in (res.rule.approver or [])

    # 2. Park outbound в PendingStore.
    store = PendingStore()
    outbound = OutboundMessage(
        channel="vk", chat_id=STRANGER_CHAT,
        content="member_a → stranger через approve",
    )
    parked = store.park(
        action="message.send",
        outbound=outbound,
        requester_actor="member_a",
        approvers=list(res.rule.approver),
        reason=res.rule.reason or "",
        rule_name=res.rule.name,
    )
    assert parked.token
    assert parked.allows_approver("owner")
    assert not parked.allows_approver("member_a")
    assert not parked.allows_approver("stranger")

    # 3. Эмулируем /approve <token> от owner: take → отдаёт outbound.
    taken = store.take(parked.token)
    assert taken is not None
    assert taken.outbound.chat_id == STRANGER_CHAT
    assert taken.outbound.content.startswith("member_a → stranger")

    # 4. Повторный take — None (one-shot).
    assert store.take(parked.token) is None


# --- Дополнительный инвариант: routing principal → chat id -----------------

def test_principal_resolution_matches_policy_chats(principals: PrincipalRegistry) -> None:
    """principals.json и policy.yaml сходятся по chat_id'ам."""
    assert principals.resolve("vk", OWNER_CHAT) == "owner"
    assert principals.resolve("vk", MEMBER_A_CHAT) == "member_a"
    assert principals.resolve("vk", STRANGER_CHAT) is None
