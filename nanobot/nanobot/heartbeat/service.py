"""Heartbeat service - periodic agent wake-up to check for tasks."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Coroutine

from loguru import logger

if TYPE_CHECKING:
    from nanobot.providers.base import LLMProvider

_HEARTBEAT_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "heartbeat",
            "description": "Report heartbeat decision after reviewing tasks.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["skip", "run"],
                        "description": "skip = nothing to do, run = has active tasks",
                    },
                    "tasks": {
                        "type": "string",
                        "description": "Natural-language summary of active tasks (required for run)",
                    },
                },
                "required": ["action"],
            },
        },
    }
]


class HeartbeatService:
    """
    Periodic heartbeat service that wakes the agent to check for tasks.

    Phase 1 (decision): reads HEARTBEAT.md and asks the LLM — via a virtual
    tool call — whether there are active tasks.  This avoids free-text parsing
    and the unreliable HEARTBEAT_OK token.

    Phase 2 (execution): only triggered when Phase 1 returns ``run``.  The
    ``on_execute`` callback runs the task through the full agent loop and
    returns the result to deliver.
    """

    def __init__(
        self,
        workspace: Path,
        provider: LLMProvider,
        model: str,
        on_execute: Callable[[str], Coroutine[Any, Any, str]] | None = None,
        on_notify: Callable[[str], Coroutine[Any, Any, None]] | None = None,
        interval_s: int = 30 * 60,
        enabled: bool = True,
        timezone: str | None = None,
    ):
        self.workspace = workspace
        self.provider = provider
        self.model = model
        self.on_execute = on_execute
        self.on_notify = on_notify
        self.interval_s = interval_s
        self.enabled = enabled
        self.timezone = timezone
        self._running = False
        self._task: asyncio.Task | None = None

    @property
    def heartbeat_file(self) -> Path:
        # Legacy fallback path. Used only when familia is not installed
        # OR no admin principal can be resolved.
        return self.workspace / "HEARTBEAT.md"

    def _admin_principal_id(self) -> str | None:
        """Resolve the principal id whose namespace owns heartbeat.

        Per project decision: heartbeat addresses ONLY the admin. We
        require *exactly one* principal carrying ``role: admin`` in
        principals.json. With zero admins, return None (nothing to
        target). With more than one — refuse to guess: log an error
        and return None so the heartbeat tick noops loudly. The
        operator can then either (a) demote one admin, or (b) switch
        to an explicit ``heartbeat_target_actor`` mechanism we'll add
        when the multi-admin case actually appears.

        Lex-smallest auto-pick was rejected during review: silent
        retargeting on principal add is a class of bug we don't want.
        """
        try:
            from familia.principals import get_registry
        except ImportError:
            return None
        try:
            registry = get_registry()
        except Exception:  # noqa: BLE001
            return None
        admins = sorted(
            pid for pid in registry.ids
            if (p := registry.get(pid)) is not None
            and "admin" in (p.roles or [])
        )
        if not admins:
            return None
        if len(admins) > 1:
            logger.error(
                "Heartbeat: refusing to target — multiple admin principals: {}. "
                "Demote one or add explicit heartbeat_target_actor support.",
                admins,
            )
            return None
        return admins[0]

    def _read_heartbeat_content(self) -> tuple[str | None, str | None]:
        """Return ``(content, source)`` for the next tick.

        Source is one of ``"memx"`` or ``"file"`` (or None on miss). We
        log it once per tick so the operator knows which path served.
        """
        admin_id = self._admin_principal_id()
        if admin_id is not None:
            try:
                from familia.principals import get_registry
                from familia.acl.principal_memory import PrincipalMemoryClient
            except ImportError:
                pass
            else:
                principal = get_registry().get(admin_id)
                if principal is not None and principal.memx_key:
                    try:
                        client = PrincipalMemoryClient(admin_id, principal.memx_key)
                        text = client.get("value:heartbeat")
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "Heartbeat: memX read failed for {}: {}",
                            admin_id, exc,
                        )
                        text = None
                    if text and text.strip():
                        return text, "memx"
        # Legacy fallback: workspace/HEARTBEAT.md (single-tenant or
        # pre-migration). Keeps standalone nanobot unchanged.
        if self.heartbeat_file.exists():
            try:
                content = self.heartbeat_file.read_text(encoding="utf-8")
            except Exception:
                return None, None
            if content.strip():
                return content, "file"
        return None, None

    async def _decide(self, content: str) -> tuple[str, str]:
        """Phase 1: ask LLM to decide skip/run via virtual tool call.

        Returns (action, tasks) where action is 'skip' or 'run'.
        """
        from nanobot.utils.helpers import current_time_str

        response = await self.provider.chat_with_retry(
            messages=[
                {"role": "system", "content": "You are a heartbeat agent. Call the heartbeat tool to report your decision."},
                {"role": "user", "content": (
                    f"Current Time: {current_time_str(self.timezone)}\n\n"
                    "Review the following HEARTBEAT.md and decide whether there are active tasks.\n\n"
                    f"{content}"
                )},
            ],
            tools=_HEARTBEAT_TOOL,
            model=self.model,
        )

        if not response.should_execute_tools:
            if response.has_tool_calls:
                logger.warning(
                    "Ignoring heartbeat tool calls under finish_reason='{}'",
                    response.finish_reason,
                )
            return "skip", ""

        args = response.tool_calls[0].arguments
        return args.get("action", "skip"), args.get("tasks", "")

    async def start(self) -> None:
        """Start the heartbeat service."""
        if not self.enabled:
            logger.info("Heartbeat disabled")
            return
        if self._running:
            logger.warning("Heartbeat already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Heartbeat started (every {}s)", self.interval_s)

    def stop(self) -> None:
        """Stop the heartbeat service."""
        self._running = False
        if self._task:
            self._task.cancel()
            self._task = None

    async def _run_loop(self) -> None:
        """Main heartbeat loop."""
        while self._running:
            try:
                await asyncio.sleep(self.interval_s)
                if self._running:
                    await self._tick()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Heartbeat error: {}", e)

    async def _tick(self) -> None:
        """Execute a single heartbeat tick."""
        from nanobot.utils.evaluator import evaluate_response

        content, source = self._read_heartbeat_content()
        if not content:
            logger.debug("Heartbeat: no source has tasks")
            return
        logger.debug("Heartbeat: source={}", source)

        logger.info("Heartbeat: checking for tasks...")

        try:
            action, tasks = await self._decide(content)

            if action != "run":
                logger.info("Heartbeat: OK (nothing to report)")
                return

            logger.info("Heartbeat: tasks found, executing...")
            if self.on_execute:
                response = await self.on_execute(tasks)

                if response:
                    should_notify = await evaluate_response(
                        response, tasks, self.provider, self.model,
                    )
                    if should_notify and self.on_notify:
                        logger.info("Heartbeat: completed, delivering response")
                        await self.on_notify(response)
                    else:
                        logger.info("Heartbeat: silenced by post-run evaluation")
        except Exception:
            logger.exception("Heartbeat execution failed")

    async def trigger_now(self) -> str | None:
        """Manually trigger a heartbeat."""
        content, _source = self._read_heartbeat_content()
        if not content:
            return None
        action, tasks = await self._decide(content)
        if action != "run" or not self.on_execute:
            return None
        return await self.on_execute(tasks)
