"""Approval-flow helper: park a gated action and ping approvers with buttons.

Replaces the older "return /approve TOKEN to the user" pattern.  The tool
that hit ``Decision.ASK`` calls :func:`request_approval`, which:

1. Parks the outbound via :class:`~familia.policy.pending.PendingStore`.
2. Emits a ``policy_park`` audit event.
3. For every approver principal, resolves their channel identity and
   sends a short prompt message with inline ✅/❌ buttons whose payloads
   are ``approve:<token>`` / ``reject:<token>``.

The callback dispatcher (``familia.bus.callback_dispatcher``) recognizes
those payloads and completes the flow: publishes the parked outbound on
approve, or cancels it on reject, auditing either way and notifying the
original requester through an inbound injection.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Awaitable, Callable

from loguru import logger

from familia import audit
from familia.policy.pending import PendingApproval, get_pending_store
from familia.principals import actor_display, resolve_identity

if TYPE_CHECKING:
    from nanobot.bus.events import OutboundMessage


async def request_approval(
    *,
    action: str,
    outbound: "OutboundMessage",
    requester_actor: str | None,
    requester_channel: str | None,
    approvers: list[str],
    reason: str,
    rule_name: str,
    publish_outbound: Callable[["OutboundMessage"], Awaitable[None]],
    extra: dict[str, Any] | None = None,
) -> tuple[PendingApproval, list[str]]:
    """Park ``outbound`` and prompt each approver with Confirm/Reject buttons.

    Returns ``(pending, reached_displays)``.  ``reached_displays`` lists
    the human-readable names of approvers that were successfully sent a
    prompt — callers can use it to build a status line for the agent.
    """
    from nanobot.bus.events import OutboundMessage

    pending = get_pending_store().park(
        action=action,
        outbound=outbound,
        requester_actor=requester_actor,
        requester_channel=requester_channel,
        approvers=approvers,
        reason=reason,
        rule_name=rule_name,
        extra=extra,
    )
    audit.log_event(
        "policy_park",
        token=pending.token,
        action=action,
        requester_actor=requester_actor,
        approvers=list(approvers),
        reason=reason,
        rule_name=rule_name,
        target_channel=outbound.channel,
        target_chat_id=outbound.chat_id,
    )

    requester_display = (
        actor_display(requester_actor) if requester_actor else None
    ) or (requester_actor or "—")

    reached: list[str] = []
    preview_line = ""
    preview_src = (outbound.content or "").strip().splitlines()
    if preview_src:
        preview_line = preview_src[0][:200]

    for approver_id in approvers:
        # Resolve to the approver's OWN primary identity, not to
        # ``outbound.channel``. The latter is where the agent wants to
        # send the parked message — the approver may not be registered
        # there at all, and ``resolve_identity`` would silently fall
        # back to ``identities[0]`` on a different channel than the
        # caller implied. Reach the approver at their own primary.
        ident = resolve_identity(approver_id)
        if ident is None:
            logger.warning(
                "request_approval: approver '{}' has no channel identity — skipped",
                approver_id,
            )
            continue
        appr_channel, appr_chat_id = ident

        prompt_lines = [
            "🛡 Запрос подтверждения",
            f"Кто: {requester_display}",
            f"Действие: {action} → {outbound.channel}:{outbound.chat_id}",
        ]
        if reason:
            prompt_lines.append(f"Причина: {reason}")
        if preview_line:
            prompt_lines.append(f"Текст: «{preview_line}»")
        prompt_lines.append(f"Живёт 15 минут. Токен: {pending.token}")

        keyboard = {
            "inline": True,
            "rows": [[
                {
                    "label": "✅ Подтвердить",
                    "payload": f"approve:{pending.token}",
                    "color": "positive",
                },
                {
                    "label": "❌ Отклонить",
                    "payload": f"reject:{pending.token}",
                    "color": "negative",
                },
            ]],
        }
        prompt_msg = OutboundMessage(
            channel=appr_channel,
            chat_id=appr_chat_id,
            content="\n".join(prompt_lines),
            metadata={
                "keyboard": keyboard,
                "approval_prompt": True,
                "approval_token": pending.token,
            },
        )
        try:
            await publish_outbound(prompt_msg)
        except Exception as exc:
            logger.warning(
                "request_approval: failed to notify approver {}: {}",
                approver_id, exc,
            )
            continue
        reached.append(actor_display(approver_id) or approver_id)

    return pending, reached
