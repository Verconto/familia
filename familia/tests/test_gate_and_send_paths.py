"""Regression tests for the central policy gate + all 4 agent-initiated
send paths.

Covers every combination of {self-reply, allow, deny, ask} × {direct reply
from the agent loop, MessageTool, SendButtonsTool, AskPrincipalTool}.
The self-reply slot is the whole reason the gate exists: replies into the
same chat the user just wrote from must bypass policy unconditionally.

These tests deliberately exercise the real engine/approval plumbing —
only the channel publisher is stubbed (captured into a list), plus the
principals registry is swapped for a fixture-local one so ``resolve_identity``
can actually route approval prompts.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from familia.policy import Decision, GateResult, evaluate_outbound_send, gate_outbound_send
from familia.policy.engine import reload_engine
from familia.policy.pending import get_pending_store
from familia.principals import (
    Identity,
    Principal,
    PrincipalRegistry,
    set_current_actor,
)
from familia.tools.ask import AskPrincipalTool
from familia.tools.buttons import SendButtonsTool
from nanobot.agent.loop import AgentLoop
from nanobot.agent.tools.message import MessageTool
from nanobot.bus.events import InboundMessage, OutboundMessage


OWNER = "owner"
OWNER_CHAT = "1000001"
MEMBER_A = "member_a"
MEMBER_A_CHAT = "1000002"
STRANGER_CHAT = "9999999"


# ---------- fixtures ----------


@pytest.fixture
def policy(tmp_path: Path):
    """Install a known policy.yaml and reload the engine pointing at it.

    Rules mirror the production policy slice relevant to message.send /
    ask.send: owner can send anywhere, Member_a→owner is allowed, Member_a→
    anyone else is ASK with owner as approver, everything else
    defaults to DENY.
    """
    p = tmp_path / "policy.yaml"
    p.write_text(
        """
rules:
  - name: "owner: message/ask anywhere"
    action: [message.send, ask.send]
    actor: owner
    decision: allow

  - name: "member_a -> owner allow"
    action: [message.send, ask.send]
    actor: member_a
    to_chat: "1000001"
    decision: allow

  - name: "member_a -> stranger: deny"
    action: [message.send, ask.send]
    actor: member_a
    to_chat: "9999999"
    decision: deny
    reason: "Member_a нельзя писать чужим"

  - name: "member_a: ask-for-approval catch-all"
    action: [message.send, ask.send]
    actor: member_a
    decision: ask
    approver: owner
    reason: "нужно подтверждение владельца"
""".strip(),
        encoding="utf-8",
    )
    eng = reload_engine(p)
    yield eng
    # Re-load the default (empty) engine so later tests in the session
    # that don't install a policy see the permissive baseline.
    reload_engine(tmp_path / "_nonexistent.yaml")


@pytest.fixture
def registry():
    """Principals: owner + member_a, both on VK."""
    reg = PrincipalRegistry([
        Principal(
            id=OWNER,
            display_name="Owner",
            identities=[Identity(channel="vk", sender_id=OWNER_CHAT)],
        ),
        Principal(
            id=MEMBER_A,
            display_name="Member A",
            identities=[Identity(channel="vk", sender_id=MEMBER_A_CHAT)],
        ),
    ])
    resolver = lambda aid, ch=None: (
        ("vk", OWNER_CHAT) if aid == OWNER else
        ("vk", MEMBER_A_CHAT) if aid == MEMBER_A else None
    )
    # Patch every module that imported get_registry / resolve_identity
    # by name — `from familia.principals import X` copies the reference
    # into the importing module, so patching only `familia.principals.X`
    # misses those consumers.
    with patch("familia.principals.get_registry", return_value=reg), \
         patch("familia.tools.ask.get_registry", return_value=reg), \
         patch("familia.policy.gate.get_current_actor", return_value=MEMBER_A), \
         patch("familia.policy.approval.resolve_identity", side_effect=resolver), \
         patch("familia.tools.ask.resolve_identity", side_effect=resolver), \
         patch("familia.bus.callback_dispatcher.resolve_identity", side_effect=resolver):
        set_current_actor(MEMBER_A)
        yield reg
        set_current_actor(None)


@pytest.fixture
def sink():
    """Captures every OutboundMessage published through the stub callback."""
    bucket: list[OutboundMessage] = []

    async def publish(msg: OutboundMessage) -> None:
        bucket.append(msg)

    return SimpleNamespace(published=bucket, publish=publish)


@pytest.fixture(autouse=True)
def _clear_pending_state():
    """Pending approvals + pending asks are process-global; drop both
    before and after each test so cases don't leak into one another."""
    from familia import pending_asks as _pa

    store = get_pending_store()
    store._by_token.clear()  # type: ignore[attr-defined]
    _pa._pending.clear()  # type: ignore[attr-defined]
    yield
    # Cancel any still-alive watchdog tasks before dropping their refs.
    for ask in list(_pa._pending.values()):  # type: ignore[attr-defined]
        if ask.watchdog is not None:
            ask.watchdog.cancel()
    store._by_token.clear()  # type: ignore[attr-defined]
    _pa._pending.clear()  # type: ignore[attr-defined]


# ---------- pure gate tests ----------


class TestEvaluateOutboundSend:
    """Self-reply bypass and engine delegation of the pure evaluator."""

    def test_self_reply_bypasses_engine(self, policy, registry):
        """Same channel + chat as inbound → ALLOW via __self_reply__ rule,
        without touching the policy engine even when the engine would ASK."""
        dec = evaluate_outbound_send(
            action="message.send",
            to_channel="vk",
            to_chat=MEMBER_A_CHAT,
            inbound_channel="vk",
            inbound_chat_id=MEMBER_A_CHAT,
        )
        assert dec.decision is Decision.ALLOW
        assert dec.rule is not None
        assert dec.rule.name == "__self_reply__"

    def test_allow_rule_passes_through(self, policy, registry):
        dec = evaluate_outbound_send(
            action="message.send",
            to_channel="vk",
            to_chat=OWNER_CHAT,
            inbound_channel="vk",
            inbound_chat_id=MEMBER_A_CHAT,
        )
        assert dec.decision is Decision.ALLOW
        assert dec.rule.name == "member_a -> owner allow"

    def test_deny_rule_passes_through(self, policy, registry):
        dec = evaluate_outbound_send(
            action="message.send",
            to_channel="vk",
            to_chat=STRANGER_CHAT,
            inbound_channel="vk",
            inbound_chat_id=MEMBER_A_CHAT,
        )
        assert dec.decision is Decision.DENY

    def test_ask_rule_passes_through(self, policy, registry):
        dec = evaluate_outbound_send(
            action="message.send",
            to_channel="vk",
            to_chat="7777777",
            inbound_channel="vk",
            inbound_chat_id=MEMBER_A_CHAT,
        )
        assert dec.decision is Decision.ASK
        assert "owner" in (dec.approver or [])

    def test_no_inbound_channel_no_bypass(self, policy, registry):
        """Cron/dream paths have no inbound; bypass must not fire on chat
        equality alone — the channel check guards that."""
        dec = evaluate_outbound_send(
            action="message.send",
            to_channel="vk",
            to_chat=MEMBER_A_CHAT,
            inbound_channel=None,
            inbound_chat_id=MEMBER_A_CHAT,
        )
        assert dec.decision is Decision.ASK
        assert dec.rule.name != "__self_reply__"


class TestGateOutboundSend:
    """End-to-end gate: bypass, allow, deny, ask+parking."""

    @pytest.mark.asyncio
    async def test_self_reply_returns_allow_without_publishing(
        self, policy, registry, sink,
    ):
        out = OutboundMessage(channel="vk", chat_id=MEMBER_A_CHAT, content="ok")
        res = await gate_outbound_send(
            action="message.send",
            outbound=out,
            inbound_channel="vk",
            inbound_chat_id=MEMBER_A_CHAT,
            publish_outbound=sink.publish,
        )
        assert res.kind == "allow"
        assert res.rule_name == "__self_reply__"
        assert sink.published == []  # gate never publishes on allow

    @pytest.mark.asyncio
    async def test_allow_rule_returns_allow(self, policy, registry, sink):
        out = OutboundMessage(channel="vk", chat_id=OWNER_CHAT, content="ok")
        res = await gate_outbound_send(
            action="message.send",
            outbound=out,
            inbound_channel="vk",
            inbound_chat_id=MEMBER_A_CHAT,
            publish_outbound=sink.publish,
        )
        assert res.kind == "allow"
        assert sink.published == []

    @pytest.mark.asyncio
    async def test_deny_rule_returns_deny(self, policy, registry, sink):
        out = OutboundMessage(channel="vk", chat_id=STRANGER_CHAT, content="x")
        res = await gate_outbound_send(
            action="message.send",
            outbound=out,
            inbound_channel="vk",
            inbound_chat_id=MEMBER_A_CHAT,
            publish_outbound=sink.publish,
        )
        assert res.kind == "deny"
        assert sink.published == []

    @pytest.mark.asyncio
    async def test_ask_parks_and_notifies_approver(self, policy, registry, sink):
        out = OutboundMessage(channel="vk", chat_id="7777777", content="spy")
        res = await gate_outbound_send(
            action="message.send",
            outbound=out,
            inbound_channel="vk",
            inbound_chat_id=MEMBER_A_CHAT,
            publish_outbound=sink.publish,
        )
        assert res.kind == "asked"
        # Exactly one approval prompt was sent, to the owner, with inline buttons.
        assert len(sink.published) == 1
        prompt = sink.published[0]
        assert prompt.chat_id == OWNER_CHAT
        assert prompt.metadata.get("approval_prompt") is True
        assert "keyboard" in prompt.metadata
        # The outbound itself was parked, not delivered.
        assert all(p.content != "spy" for p in sink.published)
        assert len(list(get_pending_store()._by_token)) == 1  # type: ignore[attr-defined]


# ---------- send path: direct reply (agent loop) ----------


async def _invoke_publish_reply(bus_publish, inbound, outbound):
    """Call ``AgentLoop._publish_reply_with_policy`` without constructing
    the whole loop — it only ever reaches into ``self.bus.publish_outbound``."""
    fake_self = SimpleNamespace(bus=SimpleNamespace(publish_outbound=bus_publish))
    await AgentLoop._publish_reply_with_policy(fake_self, inbound, outbound)


def _inbound_from_member_a(chat_id: str = MEMBER_A_CHAT) -> InboundMessage:
    return InboundMessage(
        channel="vk",
        sender_id=chat_id,
        chat_id=chat_id,
        content="hi",
        actor=MEMBER_A,
    )


class TestDirectReplyPath:
    @pytest.mark.asyncio
    async def test_self_reply_published(self, policy, registry, sink):
        inbound = _inbound_from_member_a()
        outbound = OutboundMessage(channel="vk", chat_id=MEMBER_A_CHAT, content="hi back")
        await _invoke_publish_reply(sink.publish, inbound, outbound)
        assert [m.content for m in sink.published] == ["hi back"]

    @pytest.mark.asyncio
    async def test_cross_chat_allowed_published(self, policy, registry, sink):
        inbound = _inbound_from_member_a()
        outbound = OutboundMessage(channel="vk", chat_id=OWNER_CHAT, content="to owner")
        await _invoke_publish_reply(sink.publish, inbound, outbound)
        assert [m.content for m in sink.published] == ["to owner"]

    @pytest.mark.asyncio
    async def test_cross_chat_denied_dropped(self, policy, registry, sink):
        inbound = _inbound_from_member_a()
        outbound = OutboundMessage(channel="vk", chat_id=STRANGER_CHAT, content="leak")
        await _invoke_publish_reply(sink.publish, inbound, outbound)
        assert sink.published == []

    @pytest.mark.asyncio
    async def test_ask_sends_approval_prompt_and_waiting_notice(
        self, policy, registry, sink,
    ):
        inbound = _inbound_from_member_a()
        outbound = OutboundMessage(channel="vk", chat_id="7777777", content="q")
        await _invoke_publish_reply(sink.publish, inbound, outbound)
        # One prompt to approver, one "reply held" notice back to Member_a.
        targets = [(m.chat_id, bool(m.metadata.get("approval_prompt"))) for m in sink.published]
        assert (OWNER_CHAT, True) in targets
        assert any(
            cid == MEMBER_A_CHAT and not appr for cid, appr in targets
        )
        # The real "q" outbound stays parked.
        assert all(m.content != "q" for m in sink.published)


# ---------- send path: MessageTool ----------


class TestMessageTool:
    def _tool(self, sink, chat_id: str = MEMBER_A_CHAT) -> MessageTool:
        t = MessageTool(send_callback=sink.publish)
        t.set_context("vk", chat_id)
        return t

    @pytest.mark.asyncio
    async def test_self_reply_published(self, policy, registry, sink):
        t = self._tool(sink, MEMBER_A_CHAT)
        result = await t.execute(content="same chat")
        assert "sent" in result.lower()
        assert [m.content for m in sink.published] == ["same chat"]

    @pytest.mark.asyncio
    async def test_cross_chat_allowed_published(self, policy, registry, sink):
        t = self._tool(sink, MEMBER_A_CHAT)
        result = await t.execute(content="hi owner", chat_id=OWNER_CHAT)
        assert "sent" in result.lower()
        assert [m.chat_id for m in sink.published] == [OWNER_CHAT]

    @pytest.mark.asyncio
    async def test_cross_chat_denied(self, policy, registry, sink):
        t = self._tool(sink, MEMBER_A_CHAT)
        result = await t.execute(content="nope", chat_id=STRANGER_CHAT)
        assert result.lower().startswith("policy denied")
        assert sink.published == []

    @pytest.mark.asyncio
    async def test_ask_parks(self, policy, registry, sink):
        t = self._tool(sink, MEMBER_A_CHAT)
        result = await t.execute(content="pls", chat_id="7777777")
        assert "подтвержд" in result.lower()
        # Approval prompt went out; original "pls" did not.
        assert any(m.metadata.get("approval_prompt") for m in sink.published)
        assert all(m.content != "pls" for m in sink.published)


# ---------- send path: SendButtonsTool ----------


BTN_ROWS = [[{"label": "✅ Да", "payload": "yes"}, {"label": "❌ Нет", "payload": "no"}]]


class TestSendButtonsTool:
    def _tool(self, sink, chat_id: str = MEMBER_A_CHAT) -> SendButtonsTool:
        t = SendButtonsTool(send_callback=sink.publish)
        t.set_context("vk", chat_id)
        return t

    @pytest.mark.asyncio
    async def test_self_reply_published(self, policy, registry, sink):
        t = self._tool(sink, MEMBER_A_CHAT)
        result = await t.execute(content="pick", rows=BTN_ROWS)
        assert "sent" in result.lower()
        assert len(sink.published) == 1
        assert "keyboard" in sink.published[0].metadata

    @pytest.mark.asyncio
    async def test_cross_chat_allowed_published(self, policy, registry, sink):
        t = self._tool(sink, MEMBER_A_CHAT)
        result = await t.execute(
            content="pick", rows=BTN_ROWS, chat_id=OWNER_CHAT,
        )
        assert "sent" in result.lower()
        assert sink.published[0].chat_id == OWNER_CHAT

    @pytest.mark.asyncio
    async def test_cross_chat_denied(self, policy, registry, sink):
        t = self._tool(sink, MEMBER_A_CHAT)
        result = await t.execute(
            content="pick", rows=BTN_ROWS, chat_id=STRANGER_CHAT,
        )
        assert result.lower().startswith("policy denied")
        assert sink.published == []

    @pytest.mark.asyncio
    async def test_ask_parks(self, policy, registry, sink):
        t = self._tool(sink, MEMBER_A_CHAT)
        result = await t.execute(
            content="pick", rows=BTN_ROWS, chat_id="7777777",
        )
        assert "подтвержд" in result.lower()
        # Approval prompt, not the original button message.
        assert any(m.metadata.get("approval_prompt") for m in sink.published)
        assert all(m.content != "pick" for m in sink.published)


# ---------- send path: AskPrincipalTool ----------


class TestAskPrincipalTool:
    def _tool(self, sink, chat_id: str = MEMBER_A_CHAT) -> AskPrincipalTool:
        async def publish_inbound(_msg: Any) -> None:
            # Not exercised in these tests (no timeout / no press).
            return None

        t = AskPrincipalTool(
            publish_outbound=sink.publish,
            publish_inbound=publish_inbound,
        )
        t.set_context("vk", chat_id)
        return t

    @pytest.mark.asyncio
    async def test_ask_owner_allowed(self, policy, registry, sink):
        """Member_a asking owner: policy ALLOW by explicit rule.
        The question lands in the owner's chat with a correlation id."""
        t = self._tool(sink, MEMBER_A_CHAT)
        result = await t.execute(
            actor=OWNER,
            question="Child A дома?",
            rows=BTN_ROWS,
        )
        assert "отправлен" in result.lower()
        assert len(sink.published) == 1
        q = sink.published[0]
        assert q.chat_id == OWNER_CHAT
        assert q.content == "Child A дома?"
        assert "correlation_id" in q.metadata.get("keyboard", {})
        # Cancel watchdog so pytest-asyncio doesn't warn about pending tasks.
        from familia import pending_asks
        for ask in list(pending_asks._pending.values()):  # type: ignore[attr-defined]
            if ask.watchdog is not None:
                ask.watchdog.cancel()

    @pytest.mark.asyncio
    async def test_ask_self_is_self_reply(self, policy, registry, sink):
        """Edge case: asking yourself (member_a→member_a). Target chat equals
        inbound chat, so gate hits self-reply bypass — published directly."""
        t = self._tool(sink, MEMBER_A_CHAT)
        result = await t.execute(
            actor=MEMBER_A,
            question="?",
            rows=BTN_ROWS,
        )
        assert "отправлен" in result.lower()
        assert sink.published[0].chat_id == MEMBER_A_CHAT
        from familia import pending_asks
        for ask in list(pending_asks._pending.values()):  # type: ignore[attr-defined]
            if ask.watchdog is not None:
                ask.watchdog.cancel()

    @pytest.mark.asyncio
    async def test_ask_denied_by_policy(self, policy, registry, sink, tmp_path: Path):
        """Overlay a tighter rule that denies ask.send member_a→owner, then
        confirm the tool refuses and cleans up its PendingAsk state."""
        p = tmp_path / "tight.yaml"
        p.write_text(
            "rules:\n"
            "  - name: 'member_a: ask.send → owner deny'\n"
            "    action: ask.send\n"
            "    actor: member_a\n"
            "    to_chat: '1000001'\n"
            "    decision: deny\n"
            "    reason: 'закрыто'\n",
            encoding="utf-8",
        )
        reload_engine(p)
        t = self._tool(sink, MEMBER_A_CHAT)
        result = await t.execute(
            actor=OWNER,
            question="?",
            rows=BTN_ROWS,
        )
        assert result.lower().startswith("policy denied")
        assert sink.published == []
        from familia import pending_asks
        assert pending_asks._pending == {}  # type: ignore[attr-defined]


# ---------- corner cases: misbehaving / late / racing clicks ----------


class _FakeBus:
    """Minimal MessageBus-shaped stub for driving CallbackDispatcher._handle.

    Captures every outbound/inbound publish so tests can assert what the
    dispatcher emitted in response to a corner-case click.
    """

    def __init__(self) -> None:
        self.outbound: list[OutboundMessage] = []
        self.inbound: list[InboundMessage] = []
        self.fail_on_outbound = False

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        if self.fail_on_outbound:
            raise RuntimeError("channel is down")
        self.outbound.append(msg)

    async def publish_inbound(self, msg: InboundMessage) -> None:
        self.inbound.append(msg)


def _callback(
    *,
    payload: Any = None,
    correlation_id: str | None = None,
    pressed_label: str = "",
    actor: str | None = OWNER,
    channel: str = "vk",
    chat_id: str = OWNER_CHAT,
) -> Any:
    from nanobot.bus.events import CallbackEvent

    md: dict[str, Any] = {}
    if correlation_id is not None:
        md["correlation_id"] = correlation_id
    if pressed_label:
        md["pressed_label"] = pressed_label
    return CallbackEvent(
        channel=channel,
        sender_id=chat_id,
        chat_id=chat_id,
        payload=payload,
        actor=actor,
        metadata=md,
    )


class TestCallbackCornerCases:
    """Covers: late press, double-tap, orphan press, approve after expiry,
    non-approver approve, approve-with-publish-failure."""

    @pytest.mark.asyncio
    async def test_press_after_watchdog_timeout_goes_to_fallback(
        self, policy, registry, monkeypatch,
    ):
        """Watchdog fired → PendingAsk was popped. A late button press with
        the same cid must fall through to the generic orphan/fallback
        branch (not crash, not silently drop)."""
        from familia.bus.callback_dispatcher import CallbackDispatcher

        monkeypatch.delenv("FAMILIA_OWNER_ACTOR", raising=False)
        bus = _FakeBus()
        disp = CallbackDispatcher(bus)  # type: ignore[arg-type]

        evt = _callback(
            correlation_id="stale-cid-123",
            pressed_label="✅ Да",
            payload="yes",
            actor=OWNER,
            chat_id=OWNER_CHAT,
        )
        await disp._handle(evt)

        assert len(bus.inbound) == 1
        assert bus.inbound[0].chat_id == OWNER_CHAT
        assert "нажал кнопку" in bus.inbound[0].content

    @pytest.mark.asyncio
    async def test_double_tap_on_ask_answer(self, policy, registry, monkeypatch):
        """First press delivers the answer into requester's session; second
        press with the same cid lands in fallback (cid already popped)."""
        from familia import pending_asks as _pa
        from familia.bus.callback_dispatcher import CallbackDispatcher
        from familia.pending_asks import PendingAsk

        monkeypatch.delenv("FAMILIA_OWNER_ACTOR", raising=False)
        bus = _FakeBus()
        disp = CallbackDispatcher(bus)  # type: ignore[arg-type]

        ask = PendingAsk(
            correlation_id="cid-dbl",
            target_actor=OWNER,
            question="Child A дома?",
            requester_channel="vk",
            requester_chat_id=MEMBER_A_CHAT,
            requester_sender_id=MEMBER_A_CHAT,
            requester_actor=MEMBER_A,
        )
        _pa.register(ask)

        first = _callback(
            correlation_id="cid-dbl", pressed_label="✅ Да",
            payload="yes", actor=OWNER, chat_id=OWNER_CHAT,
        )
        await disp._handle(first)
        await disp._handle(first)  # identical second tap

        assert len(bus.inbound) == 2
        # First press: routed to requester's session with the answer payload.
        assert bus.inbound[0].chat_id == MEMBER_A_CHAT
        assert "Ответ от" in bus.inbound[0].content
        # Second press: orphan fallback (no owner rerouting configured).
        assert bus.inbound[1].chat_id == OWNER_CHAT
        assert "нажал кнопку" in bus.inbound[1].content

    @pytest.mark.asyncio
    async def test_approve_after_token_expired(self, policy, registry):
        """Approve payload for a token no longer in the store → short reply
        'уже неактивно' into approver's chat, no outbound to target."""
        from familia.bus.callback_dispatcher import CallbackDispatcher

        bus = _FakeBus()
        disp = CallbackDispatcher(bus)  # type: ignore[arg-type]

        evt = _callback(
            payload="approve:vanished-token",
            actor=OWNER,
            chat_id=OWNER_CHAT,
        )
        await disp._handle(evt)

        assert len(bus.outbound) == 1
        assert bus.outbound[0].chat_id == OWNER_CHAT
        assert "неактивно" in bus.outbound[0].content
        assert bus.inbound == []

    @pytest.mark.asyncio
    async def test_non_approver_press_rejected(self, policy, registry, sink):
        """Park with approvers=[owner], press comes from member_a → store
        stays intact, approver gets 'не в списке утверждающих' reply."""
        from familia.bus.callback_dispatcher import CallbackDispatcher

        # Park one pending approval directly.
        parked = OutboundMessage(
            channel="vk", chat_id=STRANGER_CHAT, content="payload",
        )
        pending = get_pending_store().park(
            action="message.send",
            outbound=parked,
            requester_actor=MEMBER_A,
            approvers=[OWNER],
            reason="test",
            rule_name="test-rule",
        )

        bus = _FakeBus()
        disp = CallbackDispatcher(bus)  # type: ignore[arg-type]

        evt = _callback(
            payload=f"approve:{pending.token}",
            actor=MEMBER_A,  # not an approver
            chat_id=MEMBER_A_CHAT,
        )
        await disp._handle(evt)

        assert len(bus.outbound) == 1
        assert "не в списке" in bus.outbound[0].content.lower()
        # The parked action must still be in the store — untouched.
        assert get_pending_store().peek(pending.token) is not None

    @pytest.mark.asyncio
    async def test_approve_publish_failure_reports_error(self, policy, registry):
        """Approver hits ✅, but downstream publish raises. Dispatcher must
        not swallow silently: send an error reply to the approver, and the
        token is consumed (take already removed it)."""
        from familia.bus.callback_dispatcher import CallbackDispatcher

        parked = OutboundMessage(
            channel="vk", chat_id=STRANGER_CHAT, content="payload",
        )
        pending = get_pending_store().park(
            action="message.send",
            outbound=parked,
            requester_actor=MEMBER_A,
            approvers=[OWNER],
            reason="",
            rule_name="test",
        )

        bus = _FakeBus()
        bus.fail_on_outbound = True  # first call (parked publish) will raise
        disp = CallbackDispatcher(bus)  # type: ignore[arg-type]

        # Re-enable publishing for the error reply (second outbound).
        call_log: list[str] = []
        original = bus.publish_outbound

        async def flaky(msg: OutboundMessage) -> None:
            if not call_log:
                call_log.append("first")
                raise RuntimeError("channel is down")
            call_log.append("second")
            bus.outbound.append(msg)

        bus.publish_outbound = flaky  # type: ignore[method-assign]

        evt = _callback(
            payload=f"approve:{pending.token}",
            actor=OWNER,
            chat_id=OWNER_CHAT,
        )
        await disp._handle(evt)

        assert call_log == ["first", "second"]
        assert len(bus.outbound) == 1
        assert "не удалось" in bus.outbound[0].content.lower()
        # Token was consumed by take() before the publish attempt.
        assert get_pending_store().peek(pending.token) is None

    @pytest.mark.asyncio
    async def test_orphan_press_reroutes_to_owner(
        self, policy, registry, monkeypatch,
    ):
        """Press with no matching cid and no approve/reject prefix, from a
        non-owner, with FAMILIA_OWNER_ACTOR set → inbound goes to owner's
        chat (not the presser's), tagged callback_orphan=True."""
        from familia.bus.callback_dispatcher import CallbackDispatcher

        monkeypatch.setenv("FAMILIA_OWNER_ACTOR", OWNER)
        bus = _FakeBus()
        disp = CallbackDispatcher(bus)  # type: ignore[arg-type]

        evt = _callback(
            payload="stale-random",
            pressed_label="🤷 Старая кнопка",
            actor=MEMBER_A,
            chat_id=MEMBER_A_CHAT,
        )
        await disp._handle(evt)

        assert len(bus.inbound) == 1
        assert bus.inbound[0].chat_id == OWNER_CHAT
        assert bus.inbound[0].actor == OWNER
        assert bus.inbound[0].metadata.get("callback_orphan") is True
        assert "Orphan-нажатие" in bus.inbound[0].content


# ---------- corner cases: tool input validation ----------


class TestSendButtonsValidation:
    @pytest.mark.asyncio
    async def test_empty_rows_rejected(self, policy, registry, sink):
        t = SendButtonsTool(send_callback=sink.publish)
        t.set_context("vk", MEMBER_A_CHAT)
        result = await t.execute(content="x", rows=[])
        assert result.lower().startswith("error")
        assert sink.published == []

    @pytest.mark.asyncio
    async def test_link_plus_payload_rejected(self, policy, registry, sink):
        t = SendButtonsTool(send_callback=sink.publish)
        t.set_context("vk", MEMBER_A_CHAT)
        rows = [[{"label": "go", "link": "https://example.com", "payload": "p"}]]
        result = await t.execute(content="x", rows=rows)
        assert "link and payload" in result.lower()
        assert sink.published == []

    @pytest.mark.asyncio
    async def test_payload_too_long_rejected(self, policy, registry, sink):
        t = SendButtonsTool(send_callback=sink.publish)
        t.set_context("vk", MEMBER_A_CHAT)
        rows = [[{"label": "x", "payload": "A" * 300}]]
        result = await t.execute(content="x", rows=rows)
        assert "limit is 255" in result.lower()
        assert sink.published == []


class TestAskPrincipalValidation:
    def _tool(self, sink) -> AskPrincipalTool:
        async def pub_inbound(_msg: Any) -> None:
            return None

        t = AskPrincipalTool(
            publish_outbound=sink.publish, publish_inbound=pub_inbound,
        )
        t.set_context("vk", MEMBER_A_CHAT)
        return t

    @pytest.mark.asyncio
    async def test_unknown_principal_rejected(self, policy, registry, sink):
        t = self._tool(sink)
        result = await t.execute(
            actor="who_is_this", question="?", rows=BTN_ROWS,
        )
        assert "unknown principal" in result.lower()
        assert sink.published == []

    @pytest.mark.asyncio
    async def test_principal_without_identity_rejected(
        self, policy, registry, sink,
    ):
        """Add a principal with zero identities — resolve_identity returns
        None, the tool aborts with a clear error, no outbound sent."""
        from familia.principals import Identity, Principal, PrincipalRegistry

        reg = PrincipalRegistry([
            Principal(
                id=OWNER, display_name="Owner",
                identities=[Identity(channel="vk", sender_id=OWNER_CHAT)],
            ),
            Principal(
                id=MEMBER_A, display_name="Member A",
                identities=[Identity(channel="vk", sender_id=MEMBER_A_CHAT)],
            ),
            Principal(id="ghost", display_name="Призрак", identities=[]),
        ])
        resolver = lambda aid, ch=None: (
            ("vk", OWNER_CHAT) if aid == OWNER else
            ("vk", MEMBER_A_CHAT) if aid == MEMBER_A else None
        )
        with patch("familia.principals.get_registry", return_value=reg), \
             patch("familia.tools.ask.get_registry", return_value=reg), \
             patch("familia.policy.approval.resolve_identity", side_effect=resolver), \
             patch("familia.tools.ask.resolve_identity", side_effect=resolver):
            t = self._tool(sink)
            result = await t.execute(
                actor="ghost", question="?", rows=BTN_ROWS,
            )
        assert "no channel identity" in result.lower()
        assert sink.published == []

    @pytest.mark.asyncio
    async def test_empty_rows_rejected(self, policy, registry, sink):
        t = self._tool(sink)
        result = await t.execute(actor=OWNER, question="?", rows=[])
        assert "rows must contain" in result.lower()
        assert sink.published == []
