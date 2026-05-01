"""Ask a principal a multiple-choice question (non-blocking, familia extension).

``ask_principal`` sends an interactive button prompt to another family
member and returns *immediately* — the caller's turn ends with a short
ack ("Asked member_a, awaiting reply"). When the target taps a button, the
callback dispatcher injects a new inbound turn into the **requester's**
session carrying the answer, so the agent can relay it in natural
language. If the watchdog timeout fires first, it injects a timeout
inbound instead.

This deliberately replaces the earlier blocking implementation: we
don't want to hold a chat session open for minutes waiting for another
user, and long timeouts (hours) are useful for "ask when they're free"
style questions.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar
from typing import Any, Awaitable, Callable

from loguru import logger

from familia import audit, pending_asks
from familia.pending_asks import PendingAsk
from familia.policy import gate_outbound_send
from familia.policy.pending import DEFAULT_TTL_SECONDS as APPROVAL_TTL_S
from familia.principals import (
    actor_display,
    get_current_actor,
    get_registry,
    resolve_identity,
)
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import (
    ArraySchema,
    IntegerSchema,
    ObjectSchema,
    StringSchema,
    tool_parameters_schema,
)
from nanobot.bus.events import InboundMessage, OutboundMessage


_BUTTON_SCHEMA = ObjectSchema(
    label=StringSchema("Button caption shown to the asked principal"),
    payload=StringSchema(
        "Opaque payload associated with this choice (JSON string or short id). "
        "Returned verbatim to the caller when the button is pressed. "
        "Omit to use the label itself.",
        nullable=True,
    ),
    color=StringSchema(
        "Button color: primary, secondary, positive, negative.",
        nullable=True,
        enum=("primary", "secondary", "positive", "negative"),
    ),
    required=["label"],
    additional_properties=False,
)


@tool_parameters(
    tool_parameters_schema(
        actor=StringSchema(
            "Principal id of the person to ask (e.g. 'member_a', 'child_a'). "
            "Must be a registered principal with at least one channel identity."
        ),
        question=StringSchema("The question text that accompanies the buttons"),
        rows=ArraySchema(
            ArraySchema(_BUTTON_SCHEMA),
            description=(
                "Keyboard as a 2-D grid: list of rows, each row a list of buttons. "
                "VK allows up to 6 rows x 5 inline buttons."
            ),
        ),
        timeout=IntegerSchema(
            description=(
                "Seconds to wait for a reply before giving up. Default 1800 (30 min). "
                "Max 86400 (24h). Does NOT block the current turn — on timeout a "
                "synthetic 'не ответил(а)' turn is injected into your session."
            ),
            nullable=True,
            minimum=30,
            maximum=86400,
        ),
        required=["actor", "question", "rows"],
    )
)
class AskPrincipalTool(Tool):
    """Send a button prompt to a principal; answer arrives as a new turn."""

    def __init__(
        self,
        publish_outbound: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        publish_inbound: Callable[[InboundMessage], Awaitable[None]] | None = None,
        default_channel: str = "",
    ) -> None:
        self._publish_outbound = publish_outbound
        self._publish_inbound = publish_inbound
        self._default_channel: ContextVar[str] = ContextVar(
            "ask_principal_default_channel", default=default_channel
        )
        self._default_chat_id: ContextVar[str] = ContextVar(
            "ask_principal_default_chat_id", default=""
        )

    def set_context(self, channel: str, chat_id: str) -> None:
        self._default_channel.set(channel)
        self._default_chat_id.set(chat_id)

    def set_send_callback(
        self, publish: Callable[[OutboundMessage], Awaitable[None]]
    ) -> None:
        self._publish_outbound = publish

    def set_inbound_callback(
        self, publish: Callable[[InboundMessage], Awaitable[None]]
    ) -> None:
        self._publish_inbound = publish

    @property
    def name(self) -> str:
        return "ask_principal"

    @property
    def description(self) -> str:
        return (
            "DEPRECATED 2026-04-27: do not call. Inter-principal actions are "
            "decided by the family graph (peer-edge ACL + policy.yaml); no "
            "consent prompt to the addressee or owner is required. Tool is "
            "kept for callback-state compatibility only and no longer "
            "registered with the agent."
        )

    async def execute(
        self,
        actor: str,
        question: str,
        rows: list[list[dict[str, Any]]],
        timeout: int | None = None,
        **kwargs: Any,
    ) -> str:
        from nanobot.utils.helpers import strip_think
        question = strip_think(question)

        if self._publish_outbound is None:
            return "Error: outbound publishing not configured"
        if self._publish_inbound is None:
            return "Error: inbound publishing not configured"
        if not rows or not any(rows):
            return "Error: rows must contain at least one button"

        principal = get_registry().get(actor)
        if principal is None:
            return f"Error: unknown principal '{actor}'"

        requester_channel = self._default_channel.get() or ""
        requester_chat_id = self._default_chat_id.get() or ""
        if not requester_channel or not requester_chat_id:
            return "Error: requester context not set (channel/chat_id)"

        ident = resolve_identity(actor, requester_channel or None)
        if ident is None:
            return (
                f"Error: principal '{actor}' has no channel identity registered — "
                "cannot reach them"
            )
        target_channel, target_chat_id = ident

        requester_actor = get_current_actor()
        display = actor_display(actor) or actor

        normalized: list[list[dict[str, Any]]] = []
        for row in rows:
            norm_row: list[dict[str, Any]] = []
            for btn in row:
                label = btn.get("label", "")
                norm_row.append({
                    "label": label,
                    "payload": btn.get("payload") or label,
                    "color": btn.get("color"),
                })
            normalized.append(norm_row)

        correlation_id = uuid.uuid4().hex
        wait_s = int(timeout) if timeout else 1800
        wait_s = max(30, min(86400, wait_s))

        ask = PendingAsk(
            correlation_id=correlation_id,
            target_actor=actor,
            question=question,
            requester_channel=requester_channel,
            requester_chat_id=requester_chat_id,
            requester_sender_id=requester_chat_id,
            requester_actor=requester_actor,
            wait_s=wait_s,
            publish_inbound=self._publish_inbound,
        )
        pending_asks.register(ask)

        keyboard_spec = {
            "inline": True,
            "rows": normalized,
            "correlation_id": correlation_id,
        }
        out = OutboundMessage(
            channel=target_channel,
            chat_id=target_chat_id,
            content=question,
            metadata={"keyboard": keyboard_spec},
        )

        # Route through the central gate.  On ASK, the gate parks `out`
        # (with its correlation_id-bearing keyboard) and publishes approve/
        # reject buttons to the approver via publish_outbound.  On approval
        # the same publish_outbound delivers the parked ask to the target,
        # whose button press will then correlate back to PendingAsk.
        gate = await gate_outbound_send(
            action="ask.send",
            outbound=out,
            inbound_channel=requester_channel or None,
            inbound_chat_id=requester_chat_id or None,
            publish_outbound=self._publish_outbound,
        )
        if gate.kind == "deny":
            pending_asks.pop(correlation_id)
            return f"Policy denied ask.send к {display}: {gate.reason}"
        if gate.kind == "allow":
            try:
                await self._publish_outbound(out)
            except Exception as e:
                pending_asks.pop(correlation_id)
                return f"Error sending question to {actor}: {e}"
            # Question is in front of the target — start the timeout
            # clock now.
            pending_asks.start_watchdog(correlation_id)
        else:
            # ASK: the outbound is parked pending approver press. Start
            # a watchdog over (approval_TTL + wait_s) so we always
            # notify the requester even if the approval lapses with no
            # press (PendingApproval would otherwise silently GC and
            # leak the PendingAsk). On approve-publish the dispatcher
            # restarts the watchdog with a fresh wait_s window.
            pending_asks.start_watchdog(
                correlation_id, extra_wait_s=APPROVAL_TTL_S
            )

        logger.info(
            "ask_principal: sent to {} on {}:{} (cid={}, timeout={}s)",
            actor, target_channel, target_chat_id, correlation_id, wait_s,
        )
        audit.log_event(
            "ask_sent",
            cid=correlation_id,
            target_actor=actor,
            requester_actor=requester_actor,
            requester_channel=requester_channel,
            requester_chat_id=requester_chat_id,
            question_preview=question[:200],
            timeout_s=wait_s,
        )
        if gate.kind == "asked":
            return (
                f"Запрос на подтверждение отправки вопроса к {display} "
                f"ушёл ({gate.approvers_label}). "
                f"После одобрения вопрос уйдёт адресату; жду ответа до {wait_s} сек."
            )
        return (
            f"Вопрос отправлен: {display}. Жду ответа (до {wait_s} сек). "
            "Ответ придёт отдельным сообщением в эту же сессию."
        )
