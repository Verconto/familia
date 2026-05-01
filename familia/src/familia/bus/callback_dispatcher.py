"""Consume button-press callbacks and feed them back to the agent (familia extension).

A ``CallbackEvent`` (emitted by a channel when the user taps an inline
keyboard button) is deliberately not an ``InboundMessage`` — we want
channels to be free to attach buttons without making every tap a full
user turn.  But for the baseline "agent reacts to the press" behaviour
we still want *some* consumer: without one, presses would just pile up
in ``bus.callbacks`` forever.

Two delivery paths:

1. **ask_principal match** — if the event carries a correlation id
   registered in ``pending_asks``, the dispatcher cancels the watchdog,
   pops the record, and injects a new inbound turn into the
   **requester's** session (not the presser's) with content like
   ``[Ответ от <Name> на «<вопрос>»]: <label>``.  This is the
   non-blocking ``ask_principal`` reply path.

2. **Fallback** — for any other press, the dispatcher synthesizes an
   inbound turn in the presser's own session tagged with
   ``metadata["callback"] = True``.  The agent reacts in natural
   language so presses are never silently dropped.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any

from loguru import logger

from familia import audit, pending_asks
from familia.policy import get_pending_store
from familia.principals import actor_display, resolve_identity
from nanobot.bus.events import CallbackEvent, InboundMessage, OutboundMessage
from nanobot.bus.queue import MessageBus


def _owner_actor() -> str:
    """Principal id of the owner for orphan-press rerouting."""
    return os.environ.get("FAMILIA_OWNER_ACTOR", "").strip()


def _ask_cid_from_outbound(outbound: OutboundMessage) -> str | None:
    """Pull an ``ask_principal`` correlation_id off a parked outbound.

    The ``ask_principal`` tool embeds the correlation id in the
    keyboard metadata of the outbound it parks; approval-flow handlers
    use this to keep ``pending_asks`` consistent with
    ``PendingStore`` state.
    """
    kb = (outbound.metadata or {}).get("keyboard") or {}
    if not isinstance(kb, dict):
        return None
    cid = kb.get("correlation_id")
    return cid if isinstance(cid, str) and cid else None


def _format_payload(payload: Any) -> str:
    """Render the callback payload as a compact string for the agent prompt."""
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload
    try:
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return repr(payload)


class CallbackDispatcher:
    """Background task that drains ``bus.callbacks`` into ``bus.inbound``."""

    def __init__(self, bus: MessageBus) -> None:
        self.bus = bus
        self._task: asyncio.Task[None] | None = None
        self._running = False

    async def start(self) -> None:
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="callback-dispatcher")
        logger.info("Callback dispatcher started")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning("Callback dispatcher task raised on shutdown: {}", exc)
            self._task = None

    async def _run(self) -> None:
        while self._running:
            try:
                evt = await self.bus.consume_callback()
            except asyncio.CancelledError:
                break
            try:
                await self._handle(evt)
            except Exception:
                logger.exception(
                    "Callback dispatcher: handler error for evt={!r}", evt
                )

    async def _handle(self, evt: CallbackEvent) -> None:
        cid = (evt.metadata or {}).get("correlation_id")
        if isinstance(cid, str) and cid:
            ask = pending_asks.pop(cid)
            if ask is not None:
                if ask.watchdog is not None:
                    ask.watchdog.cancel()
                await self._deliver_ask_answer(ask, evt)
                return

        payload_str = _format_payload(evt.payload)
        if payload_str.startswith(("approve:", "reject:")):
            verb, _, token = payload_str.partition(":")
            await self._handle_approval(evt, verb, token.strip())
            return

        pressed_label = (evt.metadata or {}).get("pressed_label") or ""

        owner = _owner_actor()
        if owner and evt.actor and evt.actor != owner:
            ident = resolve_identity(owner, evt.channel)
            if ident is not None:
                owner_channel, owner_chat_id = ident
                presser = actor_display(evt.actor) or evt.actor
                parts = [f"[Orphan-нажатие от {presser}"]
                if pressed_label:
                    parts.append(f"«{pressed_label}»")
                if payload_str:
                    parts.append(f"payload={payload_str}")
                content = " ".join(parts) + " — контекст запроса не сматчен, сообщи об этом]"
                logger.warning(
                    "Callback orphan → owner: presser={} chan={} chat={} payload={} "
                    "→ routing to owner {} at {}:{}",
                    evt.actor, evt.channel, evt.chat_id, payload_str,
                    owner, owner_channel, owner_chat_id,
                )
                audit.log_event(
                    "callback_orphan_rerouted",
                    presser_actor=evt.actor,
                    presser_channel=evt.channel,
                    presser_chat=evt.chat_id,
                    owner_actor=owner,
                    pressed_label=pressed_label,
                    payload_preview=payload_str[:200],
                )
                msg = InboundMessage(
                    channel=owner_channel,
                    sender_id=owner_chat_id,
                    chat_id=owner_chat_id,
                    content=content,
                    actor=owner,
                    metadata={
                        "callback": True,
                        "callback_orphan": True,
                        "presser_actor": evt.actor,
                        "presser_channel": evt.channel,
                        "presser_chat_id": evt.chat_id,
                        "callback_payload": evt.payload,
                        "pressed_label": pressed_label,
                    },
                )
                await self.bus.publish_inbound(msg)
                return

        content = (
            f"[Пользователь нажал кнопку] payload={payload_str}"
            if payload_str
            else "[Пользователь нажал кнопку]"
        )
        logger.info(
            "Callback → inbound: channel={} chat={} actor={} payload={}",
            evt.channel, evt.chat_id, evt.actor, payload_str,
        )
        msg = InboundMessage(
            channel=evt.channel,
            sender_id=evt.sender_id,
            chat_id=evt.chat_id,
            content=content,
            actor=evt.actor,
            metadata={
                "callback": True,
                "callback_payload": evt.payload,
                **(evt.metadata or {}),
            },
        )
        await self.bus.publish_inbound(msg)

    async def _handle_approval(
        self, evt: CallbackEvent, verb: str, token: str
    ) -> None:
        """Close the approval loop for an approve:/reject: button press."""
        store = get_pending_store()
        pending = store.peek(token)
        if pending is None:
            audit.log_event(
                "policy_expired",
                token=token,
                attempted_by=evt.actor,
                verb=verb,
            )
            await self._reply_to_approver(
                evt, f"Действие уже неактивно (токен {token})."
            )
            # PendingApproval already lapsed — we can't recover the
            # ask_principal correlation here. The extended watchdog
            # started by ask.py (covering approval_TTL + wait_s) will
            # eventually pop the PendingAsk and notify the requester.
            return

        if not pending.allows_approver(evt.actor):
            approvers = ", ".join(pending.approvers) if pending.approvers else "—"
            audit.log_event(
                "policy_approve_denied",
                token=token,
                attempted_by=evt.actor,
                approvers=list(pending.approvers),
            )
            await self._reply_to_approver(
                evt,
                f"Ты не в списке утверждающих (требуется: {approvers}).",
            )
            return

        taken = store.take(token)
        if taken is None:
            audit.log_event(
                "policy_expired",
                token=token,
                attempted_by=evt.actor,
                verb=verb,
            )
            await self._reply_to_approver(
                evt, f"Действие истекло до нажатия (токен {token})."
            )
            # peek-then-take race: PendingApproval expired between the
            # two calls. `pending` (from peek) still holds the parked
            # outbound; use it to pop the ask_principal correlation so
            # the extended watchdog stops holding state.
            ask_cid = _ask_cid_from_outbound(pending.outbound)
            if ask_cid:
                pending_asks.pop(ask_cid)
            return

        target = f"{taken.outbound.channel}:{taken.outbound.chat_id}"
        if verb == "approve":
            try:
                await self.bus.publish_outbound(taken.outbound)
            except Exception as exc:
                logger.exception("approval publish failed")
                audit.log_event(
                    "policy_approve_failed",
                    token=token,
                    approver=evt.actor,
                    action=taken.action,
                    target_channel=taken.outbound.channel,
                    target_chat_id=taken.outbound.chat_id,
                    requester_actor=taken.requester_actor,
                    error=str(exc),
                )
                await self._reply_to_approver(
                    evt, f"❌ Не удалось отправить: {exc}"
                )
                # Drop the ask_principal correlation — the extended
                # watchdog started in ask.py would otherwise fire a
                # misleading "не ответил" inbound for a question that
                # never arrived.
                ask_cid = _ask_cid_from_outbound(taken.outbound)
                if ask_cid:
                    pending_asks.pop(ask_cid)
                return
            # Approved publish succeeded. Reset the ask_principal
            # watchdog (was covering approval_TTL + wait_s) to a fresh
            # wait_s window starting now — the question just reached
            # the target.
            ask_cid = _ask_cid_from_outbound(taken.outbound)
            if ask_cid:
                pending_asks.start_watchdog(ask_cid)
            audit.log_event(
                "policy_approve",
                token=token,
                approver=evt.actor,
                action=taken.action,
                target_channel=taken.outbound.channel,
                target_chat_id=taken.outbound.chat_id,
                requester_actor=taken.requester_actor,
            )
            await self._reply_to_approver(evt, f"✅ Отправлено в {target}.")
            await self._notify_requester(taken, approved=True, approver=evt.actor)
        else:  # reject
            audit.log_event(
                "policy_reject",
                token=token,
                approver=evt.actor,
                action=taken.action,
                target_channel=taken.outbound.channel,
                target_chat_id=taken.outbound.chat_id,
                requester_actor=taken.requester_actor,
            )
            await self._reply_to_approver(evt, f"❌ Отклонено ({taken.action}).")
            await self._notify_requester(taken, approved=False, approver=evt.actor)
            # Reject on an ask_principal parked outbound means the
            # question never reaches the target — drop the correlation.
            ask_cid = _ask_cid_from_outbound(taken.outbound)
            if ask_cid:
                pending_asks.pop(ask_cid)

    async def _reply_to_approver(self, evt: CallbackEvent, text: str) -> None:
        """Short confirmation back into the approver's chat."""
        try:
            await self.bus.publish_outbound(
                OutboundMessage(
                    channel=evt.channel,
                    chat_id=evt.chat_id,
                    content=text,
                )
            )
        except Exception:
            logger.exception("approval: failed to reply to approver")

    async def _notify_requester(
        self, taken: Any, *, approved: bool, approver: str | None
    ) -> None:
        """Inject an inbound turn into the requester's session so the agent
        can relay the outcome to the person who triggered the action."""
        requester = taken.requester_actor
        if not requester:
            return
        # Route the verdict back to the channel the requester was on
        # when they triggered the gate (captured at park time from the
        # inbound ContextVar). Falls back to their primary identity if
        # that channel is unknown — resolve_identity will log a warning
        # on cross-channel fallback.
        ident = resolve_identity(requester, taken.requester_channel)
        if ident is None:
            return
        req_channel, req_chat_id = ident
        approver_display = actor_display(approver) if approver else (approver or "?")
        verdict = "одобрено" if approved else "отклонено"
        content = (
            f"[Твой запрос «{taken.action} → {taken.outbound.channel}:"
            f"{taken.outbound.chat_id}» {verdict} — {approver_display}]"
        )
        msg = InboundMessage(
            channel=req_channel,
            sender_id=req_chat_id,
            chat_id=req_chat_id,
            content=content,
            actor=requester,
            metadata={
                "approval_verdict": "approved" if approved else "rejected",
                "approval_token": taken.token,
                "approver_actor": approver,
            },
        )
        try:
            await self.bus.publish_inbound(msg)
        except Exception:
            logger.exception("approval: failed to notify requester")

    async def _deliver_ask_answer(self, ask: Any, evt: CallbackEvent) -> None:
        """Inject the answer as a new inbound turn into the requester's session."""
        pressed_label = (evt.metadata or {}).get("pressed_label") or ""
        payload_str = _format_payload(evt.payload)
        display = actor_display(ask.target_actor) or ask.target_actor

        if pressed_label:
            content = f"[Ответ от {display} на «{ask.question}»]: {pressed_label}"
        elif payload_str:
            content = f"[Ответ от {display} на «{ask.question}»] payload={payload_str}"
        else:
            content = f"[Ответ от {display} на «{ask.question}»]"

        logger.info(
            "ask_principal reply: cid={} target={} → requester {}:{} label={!r}",
            ask.correlation_id, ask.target_actor,
            ask.requester_channel, ask.requester_chat_id, pressed_label,
        )
        audit.log_event(
            "ask_answered",
            cid=ask.correlation_id,
            target_actor=ask.target_actor,
            requester_actor=ask.requester_actor,
            pressed_label=pressed_label,
        )
        msg = InboundMessage(
            channel=ask.requester_channel,
            sender_id=ask.requester_sender_id,
            chat_id=ask.requester_chat_id,
            content=content,
            actor=ask.requester_actor,
            metadata={
                "ask_reply": True,
                "correlation_id": ask.correlation_id,
                "target_actor": ask.target_actor,
                "pressed_label": pressed_label,
                "callback_payload": evt.payload,
            },
        )
        await self.bus.publish_inbound(msg)
