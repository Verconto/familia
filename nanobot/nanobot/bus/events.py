"""Event types for the message bus."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class InboundMessage:
    """Message received from a chat channel."""

    channel: str  # telegram, discord, slack, whatsapp
    sender_id: str  # User identifier
    chat_id: str  # Chat/channel identifier
    content: str  # Message text
    timestamp: datetime = field(default_factory=datetime.now)
    media: list[str] = field(default_factory=list)  # Media URLs
    metadata: dict[str, Any] = field(default_factory=dict)  # Channel-specific data
    session_key_override: str | None = None  # Optional override for thread-scoped sessions
    actor: str | None = None  # Resolved principal id (familia extension)

    @property
    def session_key(self) -> str:
        """Unique key for session identification."""
        return self.session_key_override or f"{self.channel}:{self.chat_id}"


@dataclass
class OutboundMessage:
    """Message to send to a chat channel."""

    channel: str
    chat_id: str
    content: str
    reply_to: str | None = None
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class CallbackEvent:
    """Inline-keyboard callback press from a channel.

    Separated from ``InboundMessage`` so the agent doesn't treat button
    presses as user turns.  Scenarios consume these via
    ``MessageBus.consume_callback`` (or a dispatcher on top) and correlate
    them with outbound messages they sent using the ``payload``.
    """

    channel: str
    sender_id: str
    chat_id: str
    payload: Any  # JSON-decoded if the channel sent JSON, else raw
    timestamp: datetime = field(default_factory=datetime.now)
    actor: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


