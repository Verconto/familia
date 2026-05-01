"""Send an interactive message with inline buttons (familia extension).

A thin counterpart to :class:`~nanobot.agent.tools.message.MessageTool`
that lets the agent deliver a message together with a channel-agnostic
keyboard spec.  The channel layer (e.g. :mod:`nanobot.channels.vk`)
translates ``metadata["keyboard"]`` into the native format and routes
callback presses back through ``MessageBus.publish_callback``.

Scenarios then consume :class:`~nanobot.bus.events.CallbackEvent` via
``bus.consume_callback()`` and correlate presses by the ``payload`` we
put on each button here.
"""

from contextvars import ContextVar
from typing import Any, Awaitable, Callable

from familia.policy import gate_outbound_send
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import (
    ArraySchema,
    BooleanSchema,
    ObjectSchema,
    StringSchema,
    tool_parameters_schema,
)
from nanobot.bus.events import OutboundMessage


_BUTTON_SCHEMA = ObjectSchema(
    label=StringSchema("Button caption shown to the user"),
    payload=StringSchema(
        "Opaque payload returned on press (JSON string recommended, ≤255 chars). "
        "Omit for link or plain-text buttons.",
        nullable=True,
    ),
    link=StringSchema(
        "URL for open-link buttons. Mutually exclusive with payload.",
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
        content=StringSchema("Message text that accompanies the buttons"),
        rows=ArraySchema(
            ArraySchema(_BUTTON_SCHEMA),
            description=(
                "Keyboard as a 2-D grid: list of rows, each row a list of buttons. "
                "VK allows up to 6 rows x 5 inline buttons; payload max 255 chars."
            ),
        ),
        inline=BooleanSchema(
            description=(
                "If true (default), buttons attach to the message; if false, render as a "
                "persistent bottom keyboard (channel-dependent)."
            ),
            nullable=True,
        ),
        one_time=BooleanSchema(
            description="For persistent keyboards only: hide after first press.",
            nullable=True,
        ),
        channel=StringSchema("Optional: target channel. Defaults to current.", nullable=True),
        chat_id=StringSchema("Optional: target chat id. Defaults to current.", nullable=True),
        required=["content", "rows"],
    )
)
class SendButtonsTool(Tool):
    """Send a message with an inline/persistent keyboard."""

    def __init__(
        self,
        send_callback: Callable[[OutboundMessage], Awaitable[None]] | None = None,
        default_channel: str = "",
        default_chat_id: str = "",
    ):
        self._send_callback = send_callback
        self._default_channel: ContextVar[str] = ContextVar(
            "send_buttons_default_channel", default=default_channel
        )
        self._default_chat_id: ContextVar[str] = ContextVar(
            "send_buttons_default_chat_id", default=default_chat_id
        )

    def set_context(self, channel: str, chat_id: str) -> None:
        self._default_channel.set(channel)
        self._default_chat_id.set(chat_id)

    def set_send_callback(
        self, callback: Callable[[OutboundMessage], Awaitable[None]]
    ) -> None:
        self._send_callback = callback

    @property
    def name(self) -> str:
        return "send_buttons"

    @property
    def description(self) -> str:
        return (
            "Send a message with interactive buttons to the CURRENT chat only. Use this "
            "when the SAME user you're talking to should pick one of a few explicit "
            "options (confirm/deny, choose a time, choose a mode). Each button can "
            "carry a short JSON payload (≤255 chars) that comes back on press; or be "
            "a link; or just a label.\n\n"
            "DO NOT use this to ask a DIFFERENT family member a question or to seek "
            "consent from another principal. Cross-principal coordination is decided "
            "automatically by the family graph (peer-edge ACL + policy.yaml); no "
            "interactive confirmation is required. `send_buttons` is fire-and-forget: "
            "presses by the recipient become new turns in THEIR session, not yours, "
            "so any cross-principal poll would lose the reply.\n"
            "Also: do NOT use this as a substitute for the plain `message` tool — only "
            "when structured choices make the interaction clearer.\n\n"
            "STYLE: prefix every button label with a single fitting emoji so it reads "
            "well in the chat UI — e.g. '✅ Подтвердить', '❌ Отмена', '⏰ Через час', "
            "'📅 Завтра', '🔗 Открыть'. Pick an emoji that matches the button's meaning "
            "(not just decorative), keep labels short (2–4 words)."
        )

    async def execute(
        self,
        content: str,
        rows: list[list[dict[str, Any]]],
        inline: bool | None = None,
        one_time: bool | None = None,
        channel: str | None = None,
        chat_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        from nanobot.utils.helpers import strip_think
        content = strip_think(content)

        default_channel = self._default_channel.get()
        default_chat_id = self._default_chat_id.get()
        channel = channel or default_channel
        chat_id = chat_id or default_chat_id

        if not channel or not chat_id:
            return "Error: No target channel/chat specified"
        if not self._send_callback:
            return "Error: Message sending not configured"
        if not rows or not any(rows):
            return "Error: rows must contain at least one button"

        for r_idx, row in enumerate(rows):
            for b_idx, btn in enumerate(row):
                if "link" in btn and btn["link"] and btn.get("payload"):
                    return (
                        f"Error: button [{r_idx}][{b_idx}] has both link and payload — "
                        "pick one"
                    )
                payload = btn.get("payload")
                if isinstance(payload, str) and len(payload) > 255:
                    return (
                        f"Error: button [{r_idx}][{b_idx}] payload is {len(payload)} chars, "
                        "limit is 255"
                    )

        keyboard_spec = {
            "inline": True if inline is None else bool(inline),
            "one_time": bool(one_time) if one_time is not None else False,
            "rows": rows,
        }

        msg = OutboundMessage(
            channel=channel,
            chat_id=chat_id,
            content=content,
            metadata={"keyboard": keyboard_spec},
        )

        result = await gate_outbound_send(
            action="message.send",
            outbound=msg,
            inbound_channel=default_channel or None,
            inbound_chat_id=default_chat_id or None,
            publish_outbound=self._send_callback,
        )
        if result.kind == "deny":
            return f"Policy denied message.send to {channel}:{chat_id}: {result.reason}"
        if result.kind == "asked":
            return (
                f"Отправлен запрос на подтверждение ({result.approvers_label}). "
                "Жду ответа (до 15 минут)."
            )

        try:
            await self._send_callback(msg)
            n = sum(len(r) for r in rows)
            return f"Sent {n} buttons to {channel}:{chat_id}"
        except Exception as e:
            return f"Error sending buttons: {str(e)}"
