"""Dream-only tool for writing scoped memory under a privileged identity.

The per-scope Dream (#44 from SCENARIOS.md) routes private facts extracted
from one principal's history into their ``private:<actor>:*`` memX scope,
so that shared ``MEMORY.md`` stops being a leakage path.  Doing this from
the regular ``memory_set`` tool would be inconvenient — it can only write
to the *current* actor's private scope, and Dream has no single current
actor (it runs as system, summarizing many principals in one pass).

This tool therefore:

* takes an explicit ``scope`` + optional ``actor`` / ``other`` arguments,
* talks to memX using the key from ``$DREAM_CONSOLIDATOR_MEMX_KEY``
  (falls back to ``local_dev_key`` for single-node dev setups; in prod
  provision a dedicated full-ACL key in ``memx/config/acl.json``),
* evaluates policy as ``actor='dream_consolidator'`` — a single rule gates
  all writes in one place and they land in the audit log.

Only registered on the Dream agent's tool registry.  Not exposed to the
normal agent loop.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
from loguru import logger

from familia.memx_client import memx_base_url
from familia.policy import Decision, PolicyContext, get_engine
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import StringSchema, tool_parameters_schema


CONSOLIDATOR_ACTOR = "dream_consolidator"
CONSOLIDATOR_KEY_ENV = "DREAM_CONSOLIDATOR_MEMX_KEY"


def _resolve_full_key(
    scope: str, key: str, actor: str | None, other: str | None
) -> tuple[str | None, str | None]:
    scope = (scope or "").strip()
    if not key:
        return None, "Error: 'key' is required"
    if scope == "shared":
        return f"shared:{key}", None
    if scope == "private":
        if not actor:
            return None, "Error: scope='private' requires 'actor' (the principal whose private scope to write)"
        return f"private:{actor}:{key}", None
    if scope == "pair":
        if not actor or not other:
            return None, "Error: scope='pair' requires both 'actor' and 'other'"
        if actor == other:
            return None, "Error: 'actor' and 'other' must be different principals"
        a, b = sorted([actor, other])
        return f"pair:{a}_{b}:{key}", None
    return None, f"Error: unknown scope '{scope}'. Use 'shared', 'private', or 'pair'."


@tool_parameters(
    tool_parameters_schema(
        scope=StringSchema(
            "Memory scope: 'shared' (family-wide), 'private' (of one principal; "
            "set 'actor' to the target), or 'pair' (set 'actor' + 'other')."
        ),
        actor=StringSchema(
            "For scope='private': the principal whose private scope to write. "
            "For scope='pair': one of the two principals. Omit for 'shared'."
        ),
        other=StringSchema(
            "For scope='pair': the second principal of the pair. Omit otherwise."
        ),
        key=StringSchema("Bare memory key without scope prefix (e.g. 'daily_routine')."),
        value=StringSchema("Value to store (use JSON-encoded string for structured data)."),
        required=["scope", "key", "value"],
    )
)
class DreamMemorySetTool(Tool):
    """Write scoped family memory on behalf of the Dream consolidator."""

    def __init__(self, base_url: str | None = None, api_key: str | None = None) -> None:
        self._base_url_override = base_url
        self._api_key = api_key or os.environ.get(CONSOLIDATOR_KEY_ENV, "local_dev_key")

    @property
    def _base_url(self) -> str:
        return self._base_url_override or memx_base_url()

    @property
    def name(self) -> str:
        return "dream_memory_set"

    @property
    def description(self) -> str:
        return (
            "Write a scoped memory value extracted during Dream consolidation. "
            "Use scope='private' + actor=<id> to store facts private to that principal "
            "(e.g. a private confession one principal made about work) — NEVER put such facts into "
            "MEMORY.md. scope='pair' + actor/other for facts only relevant to a pair. "
            "scope='shared' for facts the whole family knows or needs."
        )

    async def execute(
        self,
        scope: str,
        key: str,
        value: str,
        actor: str | None = None,
        other: str | None = None,
        **_: Any,
    ) -> str:
        # Defense-in-depth: this tool is only meant to be registered on the
        # Dream agent's loop (which pins ``set_current_actor(CONSOLIDATOR_ACTOR)``
        # for its turn). If it ever ends up on the main agent's registry by
        # mistake, the policy gate would still rubber-stamp anything as the
        # consolidator since ``actor=CONSOLIDATOR_ACTOR`` is hardcoded below.
        # Refuse to run unless the calling context actually IS the consolidator.
        from familia.principals import get_current_actor

        current = get_current_actor()
        if current != CONSOLIDATOR_ACTOR:
            return (
                f"Error: dream_memory_set is only callable from the Dream "
                f"consolidator turn (current actor={current!r})"
            )
        full_key, err = _resolve_full_key(scope, key, actor, other)
        if err:
            return err
        decision = get_engine().evaluate(
            PolicyContext(action="memory.write", actor=CONSOLIDATOR_ACTOR, to_chat=full_key)
        )
        if decision.decision is Decision.DENY:
            reason = decision.reason or "policy denied"
            return f"Policy denied memory.write на '{full_key}' для {CONSOLIDATOR_ACTOR}: {reason}"
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.post(
                    f"{self._base_url}/set",
                    headers={"x-api-key": self._api_key},
                    json={"key": full_key, "value": value},
                )
        except httpx.HTTPError as exc:
            return f"Error: memX unreachable ({type(exc).__name__}: {exc})"
        if r.status_code == 403:
            return f"Error: access denied by memX ACL for key '{full_key}' (check the consolidator memx-key ACL)"
        if r.status_code >= 400:
            return f"Error: memX {r.status_code}: {r.text[:200]}"
        logger.info("dream_memory_set stored at {}", full_key)
        return f"Stored at '{full_key}'"
