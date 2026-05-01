"""Single entry point for gating agent-initiated outbound messages.

Every code path that sends a message on behalf of the agent — direct
replies in the agent loop, the generic ``message`` tool, the
``send_buttons`` tool, and ``ask_principal`` — funnels through here.
The alternative is what we used to have: four copies of the same
self-reply bypass + policy.evaluate + request_approval block, drifting
out of sync every time one of them was patched.

Two helpers:

- :func:`evaluate_outbound_send` — pure, side-effect-free. Applies the
  self-reply bypass (target chat == inbound chat → unconditional allow,
  it's a reaction to the user's turn, not fan-out), otherwise delegates
  to the policy engine. Callers that need fine control over the ASK
  branch (e.g. ``ask_principal``, which owns its own pending-ask state)
  use this and handle the decision themselves.

- :func:`gate_outbound_send` — full flow. Evaluates, and on ASK also
  parks the outbound via :func:`familia.policy.approval.request_approval`
  so approvers get the confirm/reject buttons. Returns a
  :class:`GateResult` the caller maps to its own path-specific response
  (publish, error string, "waiting for approval" notice to the user).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Awaitable, Callable, Literal

from familia import audit
from familia.policy.approval import request_approval
from familia.policy.pending import get_pending_store
from familia.policy.engine import (
    Decision,
    PolicyContext,
    PolicyDecision,
    PolicyRule,
    get_engine,
)
from familia.principals import get_current_actor, get_current_channel

if TYPE_CHECKING:
    from nanobot.bus.events import OutboundMessage


_SELF_REPLY_RULE = PolicyRule(
    name="__self_reply__",
    decision=Decision.ALLOW,
)


def evaluate_outbound_send(
    *,
    action: str,
    to_channel: str,
    to_chat: str,
    inbound_channel: str | None,
    inbound_chat_id: str | None,
) -> PolicyDecision:
    """Resolve the policy decision for a proposed outbound send.

    Self-reply (target == inbound origin) short-circuits to ALLOW
    without touching the engine — reply-in-kind to the user's turn is
    never fan-out.  Otherwise the engine evaluates
    ``PolicyContext(action, actor=current, channel=inbound_channel,
    to_channel, to_chat)``.
    """
    if (
        inbound_channel is not None
        and to_channel == inbound_channel
        and to_chat == inbound_chat_id
    ):
        return PolicyDecision(decision=Decision.ALLOW, rule=_SELF_REPLY_RULE)

    actor = get_current_actor()
    return get_engine().evaluate(
        PolicyContext(
            action=action,
            actor=actor,
            channel=inbound_channel,
            to_channel=to_channel,
            to_chat=to_chat,
        )
    )


@dataclass
class GateResult:
    """Outcome of :func:`gate_outbound_send`.

    - ``allow``: caller should publish the outbound normally.
    - ``deny``: caller should surface ``reason`` (error string, dropped
      reply, etc.) and NOT publish.
    - ``asked``: the outbound has been parked and approvers have been
      prompted. Caller should tell the user that an approval request is
      out, using ``approvers_label`` / ``reason`` for the wording.
    """

    kind: Literal["allow", "deny", "asked"]
    reason: str = ""
    approvers_label: str = ""
    rule_name: str = ""


async def gate_outbound_send(
    *,
    action: str,
    outbound: "OutboundMessage",
    inbound_channel: str | None,
    inbound_chat_id: str | None,
    publish_outbound: Callable[["OutboundMessage"], Awaitable[None]],
) -> GateResult:
    """Evaluate the policy and, on ASK, park the outbound + notify approvers.

    ``publish_outbound`` is used only by the ASK branch (and only to
    deliver the approval prompts + later the parked outbound on
    approval). The ALLOW branch intentionally does NOT publish — that
    stays with the caller so its path-specific bookkeeping (message_id
    echo, pending-ask watchdog, etc.) still runs.
    """
    decision = evaluate_outbound_send(
        action=action,
        to_channel=outbound.channel,
        to_chat=outbound.chat_id,
        inbound_channel=inbound_channel,
        inbound_chat_id=inbound_chat_id,
    )
    rule_name = decision.rule.name if decision.rule else ""

    if decision.decision is Decision.ALLOW:
        return GateResult("allow", rule_name=rule_name)

    if decision.decision is Decision.DENY:
        return GateResult(
            "deny",
            reason=decision.reason or "policy denied",
            rule_name=rule_name,
        )

    parked, reached = await request_approval(
        action=action,
        outbound=outbound,
        requester_actor=get_current_actor(),
        requester_channel=get_current_channel(),
        approvers=decision.approver,
        reason=decision.reason,
        rule_name=rule_name,
        publish_outbound=publish_outbound,
    )
    if not reached:
        # No approver could actually be notified — parking the outbound
        # would leave it to silently TTL out while the agent told the
        # user "ушёл запрос". Cancel and deny instead.
        get_pending_store().cancel(parked.token)
        audit.log_event(
            "policy_ask_undeliverable",
            token=parked.token,
            action=action,
            approvers=list(parked.approvers),
            rule_name=rule_name,
            target_channel=outbound.channel,
            target_chat_id=outbound.chat_id,
        )
        return GateResult(
            "deny",
            reason="не удалось связаться ни с одним аппрувером",
            rule_name=rule_name,
        )
    approvers_label = ", ".join(reached)
    return GateResult(
        "asked",
        reason=decision.reason or "требуется подтверждение",
        approvers_label=approvers_label,
        rule_name=rule_name,
    )
