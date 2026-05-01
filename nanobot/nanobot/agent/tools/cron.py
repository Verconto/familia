"""Cron tool for scheduling reminders and tasks."""

from contextvars import ContextVar
from datetime import datetime
from typing import Any, Callable

from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import (
    ArraySchema,
    BooleanSchema,
    IntegerSchema,
    StringSchema,
    tool_parameters_schema,
)
from nanobot.cron.service import CronService
from nanobot.cron.types import CronJob, CronJobState, CronSchedule

_CRON_PARAMETERS = tool_parameters_schema(
    action=StringSchema("Action to perform", enum=["add", "list", "remove"]),
    name=StringSchema(
        "Optional short human-readable label for the job "
        "(e.g., 'weather-monitor', 'daily-standup'). Defaults to first 30 chars of message."
    ),
    message=StringSchema(
        "REQUIRED when action='add'. Instruction for the agent to execute when the job triggers "
        "(e.g., 'Send a reminder to WeChat: xxx' or 'Check system status and report'). "
        "Not used for action='list' or action='remove'."
    ),
    every_seconds=IntegerSchema(0, description="Interval in seconds (for recurring tasks)"),
    cron_expr=StringSchema("Cron expression like '0 9 * * *' (for scheduled tasks)"),
    tz=StringSchema(
        "Optional IANA timezone for cron expressions (e.g. 'America/Vancouver'). "
        "When omitted with cron_expr, the tool's default timezone applies."
    ),
    at=StringSchema(
        "ISO datetime for one-time execution (e.g. '2026-02-12T10:30:00'). "
        "Naive values use the tool's default timezone."
    ),
    deliver=BooleanSchema(
        description="Whether to deliver the execution result to the user channel (default true)",
        default=True,
    ),
    to=StringSchema(
        "Optional override for the delivery target chat_id. By default the reminder is "
        "delivered to the chat that scheduled it. Set this when scheduling a reminder for "
        "*another* participant: pass their chat_id (e.g. obtained via resolve_person → "
        "channels[<channel>]). The channel stays the same as the current session."
    ),
    tags=ArraySchema(
        StringSchema(""),
        description=(
            "Optional list of tag-ids attached to this job. Same semantics as "
            "memory_set tags: tag-ids must be reachable by the calling actor "
            "(principals or topics in the family graphs). When set, ``cron list`` "
            "filters jobs by intersection of tags with the viewer's reachable set."
        ),
        nullable=True,
    ),
    job_id=StringSchema("REQUIRED when action='remove'. Job ID to remove (obtain via action='list')."),
    required=["action"],
    description=(
        "Action-specific parameters: add requires a non-empty message plus one schedule "
        "(every_seconds, cron_expr, or at); remove requires job_id; list only needs action. "
        "Per-action requirements are enforced at runtime (see field descriptions) so the "
        "top-level schema stays compatible with providers (e.g. OpenAI Codex/Responses) that "
        "reject oneOf/anyOf/allOf/enum/not at the root of function parameters."
    ),
)


@tool_parameters(_CRON_PARAMETERS)
class CronTool(Tool):
    """Tool to schedule reminders and recurring tasks."""

    def __init__(
        self,
        cron_service: CronService,
        default_timezone: str = "UTC",
        to_validator: Callable[[str, str], bool] | None = None,
        current_actor_getter: Callable[[], str | None] | None = None,
        is_admin_getter: Callable[[str | None], bool] | None = None,
        reachable_tags_getter: Callable[[str | None], set[str]] | None = None,
    ):
        self._cron = cron_service
        self._default_timezone = default_timezone
        # Optional callback (channel, chat_id) -> bool. When set, the ``to``
        # override must point at a known principal identity on ``channel``.
        # Without it (nanobot-standalone), ``to`` is accepted verbatim. The
        # check exists to prevent a hallucinating/jailbroken LLM from
        # redirecting cron deliveries to arbitrary outsider chat ids.
        self._to_validator = to_validator
        # Identity of the caller for two purposes:
        #   1. stamp ``payload.created_by`` on jobs so ``cron list`` can
        #      filter by ownership (non-admin sees only their own jobs);
        #   2. tell admin from non-admin so admins still see everything.
        # Without these (nanobot-standalone), list returns all jobs as
        # before — backwards-compat for single-user installs.
        self._current_actor_getter = current_actor_getter
        self._is_admin_getter = is_admin_getter
        # Optional callback for tag-based visibility (familia integration).
        # Returns the set of tag-ids reachable by the actor (across both
        # graphs). Without it, tags are stored on jobs but ``cron list``
        # falls back to the legacy ownership-only filter.
        self._reachable_tags_getter = reachable_tags_getter
        self._channel: ContextVar[str] = ContextVar("cron_channel", default="")
        self._chat_id: ContextVar[str] = ContextVar("cron_chat_id", default="")
        self._in_cron_context: ContextVar[bool] = ContextVar("cron_in_context", default=False)

    def set_context(self, channel: str, chat_id: str) -> None:
        """Set the current session context for delivery."""
        self._channel.set(channel)
        self._chat_id.set(chat_id)

    def set_cron_context(self, active: bool):
        """Mark whether the tool is executing inside a cron job callback."""
        return self._in_cron_context.set(active)

    def reset_cron_context(self, token) -> None:
        """Restore previous cron context."""
        self._in_cron_context.reset(token)

    @staticmethod
    def _validate_timezone(tz: str) -> str | None:
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(tz)
        except (KeyError, Exception):
            return f"Error: unknown timezone '{tz}'"
        return None

    def _display_timezone(self, schedule: CronSchedule) -> str:
        """Pick the most human-meaningful timezone for display."""
        return schedule.tz or self._default_timezone

    @staticmethod
    def _format_timestamp(ms: int, tz_name: str) -> str:
        from zoneinfo import ZoneInfo

        dt = datetime.fromtimestamp(ms / 1000, tz=ZoneInfo(tz_name))
        return f"{dt.isoformat()} ({tz_name})"

    @property
    def name(self) -> str:
        return "cron"

    @property
    def description(self) -> str:
        return (
            "Schedule reminders and recurring tasks. Actions: add, list, remove. "
            f"If tz is omitted, cron expressions and naive ISO times default to {self._default_timezone}."
        )

    def validate_params(self, params: dict[str, Any]) -> list[str]:
        errors = super().validate_params(params)
        action = params.get("action")
        if action == "add" and not str(params.get("message") or "").strip():
            errors.append("message is required when action='add'")
        if action == "remove" and not str(params.get("job_id") or "").strip():
            errors.append("job_id is required when action='remove'")
        return errors

    async def execute(
        self,
        action: str,
        name: str | None = None,
        message: str = "",
        every_seconds: int | None = None,
        cron_expr: str | None = None,
        tz: str | None = None,
        at: str | None = None,
        job_id: str | None = None,
        deliver: bool = True,
        to: str | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> str:
        if action == "add":
            if self._in_cron_context.get():
                return "Error: cannot schedule new jobs from within a cron job execution"
            return self._add_job(name, message, every_seconds, cron_expr, tz, at, deliver, to, tags)
        elif action == "list":
            return self._list_jobs()
        elif action == "remove":
            return self._remove_job(job_id)
        return f"Unknown action: {action}"

    def _add_job(
        self,
        name: str | None,
        message: str,
        every_seconds: int | None,
        cron_expr: str | None,
        tz: str | None,
        at: str | None,
        deliver: bool = True,
        to: str | None = None,
        tags: list[str] | None = None,
    ) -> str:
        if not message:
            return (
                "Error: cron action='add' requires a non-empty 'message' parameter "
                "describing what to do when the job triggers "
                "(e.g. the reminder text). Retry including message=\"...\"."
            )
        channel = self._channel.get()
        chat_id = self._chat_id.get()
        if not channel or not chat_id:
            return "Error: no session context (channel/chat_id)"
        to_clean = (to or "").strip()
        target_chat_id = to_clean or chat_id
        if to_clean and to_clean != chat_id and self._to_validator is not None:
            if not self._to_validator(channel, to_clean):
                return (
                    f"Error: 'to={to_clean}' is not a known participant on channel "
                    f"'{channel}'. Use a chat_id from resolve_person → channels[<channel>], "
                    "or omit 'to' to deliver into the current chat."
                )
        if tz and not cron_expr:
            return "Error: tz can only be used with cron_expr"
        if tz:
            if err := self._validate_timezone(tz):
                return err

        # Build schedule
        delete_after = False
        if every_seconds:
            schedule = CronSchedule(kind="every", every_ms=every_seconds * 1000)
        elif cron_expr:
            effective_tz = tz or self._default_timezone
            if err := self._validate_timezone(effective_tz):
                return err
            schedule = CronSchedule(kind="cron", expr=cron_expr, tz=effective_tz)
        elif at:
            from zoneinfo import ZoneInfo

            try:
                dt = datetime.fromisoformat(at)
            except ValueError:
                return f"Error: invalid ISO datetime format '{at}'. Expected format: YYYY-MM-DDTHH:MM:SS"
            if dt.tzinfo is None:
                if err := self._validate_timezone(self._default_timezone):
                    return err
                dt = dt.replace(tzinfo=ZoneInfo(self._default_timezone))
            at_ms = int(dt.timestamp() * 1000)
            schedule = CronSchedule(kind="at", at_ms=at_ms)
            delete_after = True
        else:
            return "Error: either every_seconds, cron_expr, or at is required"

        creator = self._current_actor_getter() if self._current_actor_getter else None
        # Tag write-side ACL (mirror of memory_set SR-7): if a reachable
        # getter is wired, every tag must be in the actor's reachable set.
        # Admin bypass: if is_admin_getter says yes, skip the check.
        clean_tags: list[str] = []
        if tags:
            clean_tags = [t.strip() for t in tags if isinstance(t, str) and t.strip()]
        if clean_tags and self._reachable_tags_getter is not None:
            is_admin = (
                self._is_admin_getter(creator)
                if self._is_admin_getter is not None
                else False
            )
            if not is_admin:
                reachable = self._reachable_tags_getter(creator) or set()
                missing = sorted(set(clean_tags) - reachable)
                if missing:
                    return (
                        f"Error: cannot tag job with {missing} — outside your "
                        "reachable set. Use only ids of principals/topics you "
                        "have access to."
                    )
        job = self._cron.add_job(
            name=name or message[:30],
            schedule=schedule,
            message=message,
            deliver=deliver,
            channel=channel,
            to=target_chat_id,
            delete_after_run=delete_after,
            created_by=creator,
            tags=clean_tags,
        )
        if clean_tags:
            return f"Created job '{job.name}' (id: {job.id}, теги: {', '.join(clean_tags)})"
        return f"Created job '{job.name}' (id: {job.id})"

    def _format_timing(self, schedule: CronSchedule) -> str:
        """Format schedule as a human-readable timing string."""
        if schedule.kind == "cron":
            tz = f" ({schedule.tz})" if schedule.tz else ""
            return f"cron: {schedule.expr}{tz}"
        if schedule.kind == "every" and schedule.every_ms:
            ms = schedule.every_ms
            if ms % 3_600_000 == 0:
                return f"every {ms // 3_600_000}h"
            if ms % 60_000 == 0:
                return f"every {ms // 60_000}m"
            if ms % 1000 == 0:
                return f"every {ms // 1000}s"
            return f"every {ms}ms"
        if schedule.kind == "at" and schedule.at_ms:
            return f"at {self._format_timestamp(schedule.at_ms, self._display_timezone(schedule))}"
        return schedule.kind

    def _format_state(self, state: CronJobState, schedule: CronSchedule) -> list[str]:
        """Format job run state as display lines."""
        lines: list[str] = []
        display_tz = self._display_timezone(schedule)
        if state.last_run_at_ms:
            info = (
                f"  Last run: {self._format_timestamp(state.last_run_at_ms, display_tz)}"
                f" — {state.last_status or 'unknown'}"
            )
            if state.last_error:
                info += f" ({state.last_error})"
            lines.append(info)
        if state.next_run_at_ms:
            lines.append(f"  Next run: {self._format_timestamp(state.next_run_at_ms, display_tz)}")
        return lines

    @staticmethod
    def _system_job_purpose(job: CronJob) -> str:
        if job.name == "dream":
            return "Dream memory consolidation for long-term memory."
        return "System-managed internal job."

    def _is_visible_to(self, job: CronJob, viewer: str | None, viewer_chat_id: str) -> bool:
        """Visibility rule for ``cron list``.

        Admins see everything. System jobs (``system_event``) are admin-only —
        non-admins shouldn't even see internal scheduling. Regular jobs are
        visible to a non-admin viewer if any of:
          (a) they created the job (``payload.created_by``);
          (b) they are the recipient (``payload.to`` matches their chat_id);
          (c) tag-intersection: ``payload.tags`` intersects the viewer's
              reachable set (familia integration only).

        When neither getter callback is wired (nanobot-standalone), we
        preserve the original "show everything" behavior for backwards
        compatibility — single-user installs don't need ownership filtering.
        """
        if self._current_actor_getter is None and self._is_admin_getter is None:
            return True
        if self._is_admin_getter and self._is_admin_getter(viewer):
            return True
        if job.payload.kind == "system_event":
            return False
        creator = job.payload.created_by
        if creator and viewer and creator == viewer:
            return True
        if viewer_chat_id and job.payload.to == viewer_chat_id:
            return True
        # Tag-intersection visibility (familia integration).
        if (job.payload.tags
                and self._reachable_tags_getter is not None
                and viewer is not None):
            reachable = self._reachable_tags_getter(viewer) or set()
            if reachable & set(job.payload.tags):
                return True
        return False

    def _list_jobs(self) -> str:
        jobs = self._cron.list_jobs()
        viewer = self._current_actor_getter() if self._current_actor_getter else None
        viewer_chat_id = self._chat_id.get()
        visible = [j for j in jobs if self._is_visible_to(j, viewer, viewer_chat_id)]
        hidden_n = len(jobs) - len(visible)
        if not visible:
            if hidden_n:
                return (
                    f"No scheduled jobs visible to you "
                    f"({hidden_n} hidden — owned by other participants or system)."
                )
            return "No scheduled jobs."
        lines = []
        for j in visible:
            timing = self._format_timing(j.schedule)
            parts = [f"- {j.name} (id: {j.id}, {timing})"]
            if j.payload.kind == "system_event":
                parts.append(f"  Purpose: {self._system_job_purpose(j)}")
                parts.append("  Protected: visible for inspection, but cannot be removed.")
            parts.extend(self._format_state(j.state, j.schedule))
            lines.append("\n".join(parts))
        out = "Scheduled jobs:\n" + "\n".join(lines)
        if hidden_n:
            out += f"\n\n({hidden_n} more hidden — owned by other participants or system.)"
        return out

    def _remove_job(self, job_id: str | None) -> str:
        if not job_id:
            return "Error: job_id is required for remove"
        # Symmetric ownership check: non-admin can only remove jobs they own
        # or that are addressed to them. System jobs are protected separately
        # by the service layer (it returns "protected").
        if self._current_actor_getter is not None or self._is_admin_getter is not None:
            target = self._cron.get_job(job_id)
            if target is None:
                return f"Job {job_id} not found"
            viewer = self._current_actor_getter() if self._current_actor_getter else None
            viewer_chat_id = self._chat_id.get()
            if not self._is_visible_to(target, viewer, viewer_chat_id):
                return (
                    f"Job {job_id} not found"
                )  # 404 not 403: don't leak existence to non-owners
        result = self._cron.remove_job(job_id)
        if result == "removed":
            return f"Removed job {job_id}"
        if result == "protected":
            job = self._cron.get_job(job_id)
            if job and job.name == "dream":
                return (
                    "Cannot remove job `dream`.\n"
                    "This is a system-managed Dream memory consolidation job for long-term memory.\n"
                    "It remains visible so you can inspect it, but it cannot be removed."
                )
            return (
                f"Cannot remove job `{job_id}`.\n"
                "This is a protected system-managed cron job."
            )
        return f"Job {job_id} not found"
