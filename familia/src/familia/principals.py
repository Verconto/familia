"""Principals registry (familia).

Maps a ``(channel, sender_id)`` pair to a stable ``principal_id`` used
throughout the agent loop and policy engine.  The registry is loaded once
from a JSON file whose path is taken from the ``FAMILIA_PRINCIPALS_FILE``
environment variable, falling back to ``principals.json`` in the current
working directory.  Absent or malformed file → empty registry, and
``resolve_actor`` returns ``None`` (preserves upstream behavior).

Expected file shape::

    {
      "principals": [
        {
          "id": "owner",
          "display_name": "Owner",
          "roles": ["admin"],
          "identities": [
            {"channel": "telegram", "sender_id": "12345|username"}
          ]
        }
      ]
    }
"""

from __future__ import annotations

import json
import os
import threading
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger


@dataclass(frozen=True)
class Identity:
    channel: str
    sender_id: str


@dataclass
class Principal:
    id: str
    display_name: str = ""
    identities: list[Identity] = field(default_factory=list)
    memx_key: str = ""
    roles: list[str] = field(default_factory=list)


class PrincipalRegistry:
    def __init__(self, principals: list[Principal] | None = None) -> None:
        self._by_id: dict[str, Principal] = {}
        self._index: dict[tuple[str, str], str] = {}
        for p in principals or []:
            self.add(p)

    def add(self, principal: Principal) -> None:
        self._by_id[principal.id] = principal
        for ident in principal.identities:
            sid = str(ident.sender_id)
            self._index[(ident.channel, sid)] = principal.id
            # nanobot's Telegram channel emits sender_id as
            # ``<numeric_chat_id>|<username>`` (see
            # ``TelegramChannel._sender_id``) so the per-channel
            # allowlist can match either form. Internal callers that
            # only have the chat_id (cron's ``payload.to``, scheduled
            # jobs from ``familia cron`` CLI, anything triggered by
            # ``/health``) would otherwise miss the lookup and be
            # routed as ``actor=None`` → pending queue.
            #
            # Index both forms so ``resolve_actor("telegram",
            # "33143799")`` and ``resolve_actor("telegram",
            # "33143799|Borisskin")`` both return the principal.
            if ident.channel == "telegram" and "|" in sid:
                chat_id = sid.split("|", 1)[0]
                if chat_id and (ident.channel, chat_id) not in self._index:
                    self._index[(ident.channel, chat_id)] = principal.id

    def resolve(self, channel: str, sender_id: str) -> str | None:
        return self._index.get((channel, str(sender_id)))

    def get(self, principal_id: str) -> Principal | None:
        return self._by_id.get(principal_id)

    @property
    def ids(self) -> list[str]:
        return list(self._by_id.keys())


def _default_path() -> Path:
    return Path(os.environ.get("FAMILIA_PRINCIPALS_FILE", "principals.json"))


def load_registry(path: Path | None = None) -> PrincipalRegistry:
    path = path or _default_path()
    if not path.exists():
        logger.debug("familia principals: no file at {}, registry is empty", path)
        return PrincipalRegistry()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("familia principals: failed to read {}: {}", path, exc)
        return PrincipalRegistry()

    principals: list[Principal] = []
    for entry in raw.get("principals", []):
        pid = entry.get("id")
        if not pid:
            continue
        identities = [
            Identity(channel=i["channel"], sender_id=str(i["sender_id"]))
            for i in entry.get("identities", [])
            if i.get("channel") and i.get("sender_id") is not None
        ]
        raw_roles = entry.get("roles", [])
        roles = [str(r) for r in raw_roles if isinstance(r, str) and r]
        principals.append(
            Principal(
                id=pid,
                display_name=entry.get("display_name", ""),
                identities=identities,
                memx_key=str(entry.get("memx_key", "")),
                roles=roles,
            )
        )

    _check_unique_memx_keys(principals, path)

    logger.info("familia principals: loaded {} principals from {}", len(principals), path)
    return PrincipalRegistry(principals)


_PLACEHOLDER_KEYS = {
    "",
    "<replace_with_unique_key>",
    "<replace_with_unique_key_2>",
}


def _check_unique_memx_keys(principals: list[Principal], path: Path) -> None:
    """Refuse to load if two principals share a non-empty memx_key.

    A duplicate non-placeholder key means two principals would read each
    other's `private:*:*` scopes — the entire identity model is broken.
    Raises ValueError with a clear message naming the colliding ids.
    Empty / placeholder values are not checked (operator hasn't filled
    them in yet — ACL itself will deny everything).
    """
    seen: dict[str, str] = {}
    collisions: list[tuple[str, str, str]] = []
    for p in principals:
        key = p.memx_key.strip()
        if not key or key in _PLACEHOLDER_KEYS:
            continue
        prev = seen.get(key)
        if prev is not None:
            collisions.append((prev, p.id, key[:8] + "…"))
        else:
            seen[key] = p.id
    if collisions:
        details = "; ".join(
            f"{a} and {b} share key {k}" for a, b, k in collisions
        )
        raise ValueError(
            f"familia principals: duplicate memx_key in {path} — {details}. "
            "Generate a unique key per principal "
            "(e.g. `openssl rand -hex 32`) and update memx-config/acl.json."
        )


_registry: PrincipalRegistry | None = None
_lock = threading.Lock()


def get_registry() -> PrincipalRegistry:
    global _registry
    if _registry is None:
        with _lock:
            if _registry is None:
                _registry = load_registry()
    return _registry


def reload_registry(path: Path | None = None) -> PrincipalRegistry:
    global _registry
    with _lock:
        _registry = load_registry(path)
    return _registry


def resolve_actor(channel: str, sender_id: str) -> str | None:
    return get_registry().resolve(channel, sender_id)


_current_actor: ContextVar[str | None] = ContextVar("familia_current_actor", default=None)
_current_channel: ContextVar[str | None] = ContextVar("familia_current_channel", default=None)


def set_current_actor(actor: str | None) -> None:
    """Set the actor for the current async context (one turn of the agent loop)."""
    _current_actor.set(actor)


def get_current_actor() -> str | None:
    """Return the actor resolved for the current async context, or None."""
    return _current_actor.get()


def set_current_channel(channel: str | None) -> None:
    """Set the inbound channel for the current async context.

    Used so approval/ask flows can route their verdict-notifications back
    to the channel the requester is actually present on, rather than
    defaulting to the actor's first-registered identity.
    """
    _current_channel.set(channel)


def get_current_channel() -> str | None:
    return _current_channel.get()


def resolve_identity(
    actor: str, preferred_channel: str | None = None
) -> tuple[str, str] | None:
    """Pick a (channel, sender_id) identity for ``actor``.

    If ``preferred_channel`` is given and the actor has an identity on
    that channel, that one wins (keeps conversations on the channel
    the caller is already using).  If the actor has no identity on the
    preferred channel, fall back to the first identity AND log a
    warning — cross-channel routing is almost always not what the
    caller wants (verdict appears in a chat the user isn't watching).
    None means "actor unknown or has no identities".
    """
    principal = get_registry().get(actor)
    if principal is None or not principal.identities:
        return None
    if preferred_channel:
        for ident in principal.identities:
            if ident.channel == preferred_channel:
                return (ident.channel, str(ident.sender_id))
        logger.warning(
            "familia principals: actor {} has no identity on preferred channel {}; "
            "falling back to {} (cross-channel routing)",
            actor, preferred_channel, principal.identities[0].channel,
        )
    first = principal.identities[0]
    return (first.channel, str(first.sender_id))


def actor_display(actor: str | None) -> str:
    """Return a short human-readable label for an actor id (falls back to the id)."""
    if not actor:
        return ""
    principal = get_registry().get(actor)
    return principal.display_name or actor if principal else actor
