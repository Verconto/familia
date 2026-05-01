"""Non-blocking ask-principal correlation store (familia).

When the agent calls ``ask_principal``, we send an interactive message
to the target principal and return immediately — the caller's turn ends.
This module remembers where the answer should be delivered back once
the target taps a button (or the watchdog times out).

Flow:
    1. Tool: ``register(cid, PendingAsk(...))`` — stores requester chat +
       question text + watchdog task.
    2. Tool sends the keyboard with ``cid`` embedded in each button and
       returns a short ack to the agent.
    3. Target presses a button; channel emits ``CallbackEvent`` carrying
       ``metadata["correlation_id"] == cid``.
    4. ``CallbackDispatcher`` calls :func:`pop`; if a record is present,
       the watchdog is cancelled and the dispatcher injects a *new*
       inbound turn into the **requester's** session carrying the answer.
    5. If no press arrives in time, the watchdog fires and injects a
       timeout inbound into the requester's session instead.

The store is process-local; a single gateway process owns all asks.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

from loguru import logger

from familia import audit
from familia.principals import actor_display
from nanobot.bus.events import InboundMessage


@dataclass
class PendingAsk:
    """Bookkeeping for one outstanding ``ask_principal`` call."""

    correlation_id: str
    target_actor: str
    question: str
    # Where to deliver the answer (the original requester's session).
    requester_channel: str
    requester_chat_id: str
    requester_sender_id: str
    requester_actor: str | None = None
    watchdog: asyncio.Task[None] | None = None
    # Seconds the watchdog should wait; set by the ask_principal tool.
    wait_s: int = 1800
    # How to inject the timeout inbound back into the requester's session.
    # Stored here so the watchdog can be started later (e.g. after an
    # approval flow completes) without re-plumbing the tool instance.
    publish_inbound: Callable[..., Awaitable[None]] | None = None


# Invariant: accessed only from the single asyncio event loop that runs
# nanobot's agent + channel dispatchers. No lock is needed because every
# mutation (register/pop/resolve) happens between awaits on that loop.
# If you ever spawn a second loop or a thread that touches this dict,
# add a lock or switch to an asyncio.Queue-style handoff.
_pending: dict[str, PendingAsk] = {}


def register(ask: PendingAsk) -> None:
    """Store ``ask`` under its correlation id.

    Raises ValueError if the id is already registered — callers must use
    unique ids (uuid4 is fine).
    """
    if ask.correlation_id in _pending:
        raise ValueError(
            f"correlation_id already registered: {ask.correlation_id}"
        )
    _pending[ask.correlation_id] = ask


def pop(correlation_id: str) -> PendingAsk | None:
    """Remove and return the ask under ``correlation_id``, or None."""
    return _pending.pop(correlation_id, None)


def get(correlation_id: str) -> PendingAsk | None:
    return _pending.get(correlation_id)


def pending_count() -> int:
    return len(_pending)


async def _watchdog_coro(correlation_id: str, wait_s: int) -> None:
    try:
        await asyncio.sleep(wait_s)
    except asyncio.CancelledError:
        return
    ask = pop(correlation_id)
    if ask is None:
        return
    display = actor_display(ask.target_actor) or ask.target_actor
    content = (
        f"[{display} не ответил(а) на «{ask.question}» за {wait_s} сек — "
        "сообщи об этом тому, кто спрашивал]"
    )
    logger.info(
        "pending_asks: timeout cid={} target={} after {}s",
        correlation_id, ask.target_actor, wait_s,
    )
    audit.log_event(
        "ask_timeout",
        cid=correlation_id,
        target_actor=ask.target_actor,
        requester_actor=ask.requester_actor,
        wait_s=wait_s,
    )
    if ask.publish_inbound is None:
        return
    msg = InboundMessage(
        channel=ask.requester_channel,
        sender_id=ask.requester_sender_id,
        chat_id=ask.requester_chat_id,
        content=content,
        actor=ask.requester_actor,
        metadata={
            "ask_timeout": True,
            "correlation_id": correlation_id,
            "target_actor": ask.target_actor,
        },
    )
    try:
        await ask.publish_inbound(msg)
    except Exception:
        logger.exception("pending_asks: failed to inject timeout inbound")


def start_watchdog(correlation_id: str, extra_wait_s: int = 0) -> bool:
    """Start (or restart) the timeout watchdog for a PendingAsk.

    ``extra_wait_s`` is added to the user-specified ``ask.wait_s`` — the
    ASK-branch in ``tools.ask`` passes the approval TTL here so the
    timer covers BOTH the approval wait and the post-approval answer
    wait without a second task. Any existing watchdog task is cancelled
    first, so ``callback_dispatcher`` can cleanly "reset" the clock to a
    fresh ``ask.wait_s`` the moment approve-publish succeeds.

    Returns True when a new task was created, False if no PendingAsk is
    registered under ``correlation_id``.
    """
    ask = _pending.get(correlation_id)
    if ask is None:
        return False
    if ask.watchdog is not None and not ask.watchdog.done():
        ask.watchdog.cancel()
    ask.watchdog = asyncio.create_task(
        _watchdog_coro(correlation_id, ask.wait_s + extra_wait_s),
        name=f"ask-watchdog-{correlation_id}",
    )
    return True
