"""Admin role management tools (familia).

Four tools — ``admin_grant``, ``admin_revoke``, ``admin_list``,
``admin_set_tz`` — are admin-gated. The first three manage the admin
role grants in memX; the last flips the single global timezone.

Grants live in memX at ``shared:roles.admin_grants`` as a JSON list of
``{principal, granted_by, until_ts_utc, reason}``. Expired entries are
treated as absent by the roles layer; the grant tool also prunes them
on write to keep the list small.

The TZ tool writes a small state file (see :mod:`familia.tz`). All
four tools are gated by ``role.admin.*`` actions bound to ``@admin``
by policy.
"""

from __future__ import annotations

import json
import time
from typing import Any

from familia import audit
from familia.memx_client import memx_base_url
from familia.policy import Decision, PolicyContext, get_engine
from familia.principals import get_current_actor, get_registry
from familia.roles import (
    fetch_admin_grants,
    write_admin_grants,
)
from nanobot.agent.tools.base import Tool, tool_parameters
from nanobot.agent.tools.schema import NumberSchema, StringSchema, tool_parameters_schema


def _policy_check(action: str, actor: str | None, target: str) -> tuple[bool, str]:
    decision = get_engine().evaluate(
        PolicyContext(action=action, actor=actor, to_chat=target)
    )
    if decision.decision is Decision.ALLOW:
        return True, ""
    reason = decision.reason or "policy denied"
    return False, f"Policy denied {action}: {reason}"


def _current_actor_with_key() -> tuple[str | None, str | None, str | None]:
    actor_id = get_current_actor()
    if not actor_id:
        return None, None, "Error: no actor in context"
    p = get_registry().get(actor_id)
    if p is None or not p.memx_key:
        return None, None, f"Error: principal '{actor_id}' has no memx_key"
    return actor_id, p.memx_key, None


def _prune_active(grants: list[dict[str, Any]], now_ts: float) -> list[dict[str, Any]]:
    return [
        g for g in grants
        if isinstance(g, dict)
        and isinstance(g.get("until_ts_utc"), (int, float))
        and g["until_ts_utc"] > now_ts
    ]


@tool_parameters(
    tool_parameters_schema(
        principal=StringSchema(
            "Principal id to grant admin to (e.g. 'member_a'). Must be a "
            "known principal from principals.json."
        ),
        hours=NumberSchema(
            "Grant duration in hours from now (1–168). Defaults to 4.",
            nullable=True,
        ),
        reason=StringSchema(
            "Short reason logged to audit (e.g. 'нужно переключить policy на выходных').",
            nullable=True,
        ),
        required=["principal"],
    )
)
class AdminGrantTool(Tool):
    """Grant the admin role to another principal for a bounded window."""

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url_override = base_url

    @property
    def _base_url(self) -> str:
        return self._base_url_override or memx_base_url()

    @property
    def name(self) -> str:
        return "admin_grant"

    @property
    def description(self) -> str:
        return (
            "Grant the admin role to another principal for a bounded window "
            "(default 4h, max 168h). Only callable by current admins. "
            "Emits an audit event. Returns the resulting grant record."
        )

    async def execute(
        self,
        principal: str,
        hours: float | None = None,
        reason: str | None = None,
        **kwargs: Any,
    ) -> str:
        actor_id, api_key, err = _current_actor_with_key()
        if err:
            return err
        target = (principal or "").strip()
        if not target:
            return "Error: 'principal' is required"
        if get_registry().get(target) is None:
            return f"Error: unknown principal '{target}'"
        if target == actor_id:
            return "Error: cannot grant admin to yourself"

        hours_f = 4.0 if hours is None else float(hours)
        if not (0 < hours_f <= 168):
            return "Error: hours must be in (0, 168]"

        ok, msg = _policy_check("role.admin.grant", actor_id, target)
        if not ok:
            return msg

        now = time.time()
        grants = _prune_active(await fetch_admin_grants(api_key, self._base_url), now)
        grants = [g for g in grants if g.get("principal") != target]
        until = now + hours_f * 3600
        record = {
            "principal": target,
            "granted_by": actor_id,
            "until_ts_utc": until,
            "reason": (reason or "").strip() or None,
        }
        grants.append(record)
        ok, msg = await write_admin_grants(grants, api_key, self._base_url)
        if not ok:
            return f"Error: {msg}"
        audit.log_event(
            "role_admin",
            action="grant",
            actor=actor_id,
            to_chat=target,
            decision="allow",
            extra={"until_ts_utc": until, "hours": hours_f, "reason": record["reason"]},
        )
        return (
            f"Granted admin to '{target}' until "
            f"{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(until))} "
            f"({hours_f:g}h). Reason: {record['reason'] or '—'}"
        )


@tool_parameters(
    tool_parameters_schema(
        principal=StringSchema("Principal id whose admin grant should be revoked."),
        required=["principal"],
    )
)
class AdminRevokeTool(Tool):
    """Revoke an active admin grant before it expires."""

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url_override = base_url

    @property
    def _base_url(self) -> str:
        return self._base_url_override or memx_base_url()

    @property
    def name(self) -> str:
        return "admin_revoke"

    @property
    def description(self) -> str:
        return (
            "Revoke an active admin grant before its natural expiry. Only "
            "callable by current admins. Does not touch static admin roles "
            "from principals.json."
        )

    async def execute(self, principal: str, **kwargs: Any) -> str:
        actor_id, api_key, err = _current_actor_with_key()
        if err:
            return err
        target = (principal or "").strip()
        if not target:
            return "Error: 'principal' is required"
        # Symmetry with admin_grant: surface unknown ids loudly so a typo
        # doesn't silently no-op (which used to look like a successful revoke).
        if get_registry().get(target) is None:
            return f"Error: unknown principal '{target}'"

        ok, msg = _policy_check("role.admin.revoke", actor_id, target)
        if not ok:
            return msg

        now = time.time()
        grants = _prune_active(await fetch_admin_grants(api_key, self._base_url), now)
        before = len(grants)
        grants = [g for g in grants if g.get("principal") != target]
        if len(grants) == before:
            return f"No active admin grant for '{target}' to revoke"
        ok, msg = await write_admin_grants(grants, api_key, self._base_url)
        if not ok:
            return f"Error: {msg}"
        audit.log_event(
            "role_admin",
            action="revoke",
            actor=actor_id,
            to_chat=target,
            decision="allow",
        )
        return f"Revoked admin grant for '{target}'"


@tool_parameters(tool_parameters_schema())
class AdminListTool(Tool):
    """List active admin grants (static roles from principals.json are not included)."""

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url_override = base_url

    @property
    def _base_url(self) -> str:
        return self._base_url_override or memx_base_url()

    @property
    def name(self) -> str:
        return "admin_list"

    @property
    def description(self) -> str:
        return (
            "List active (unexpired) admin grants as a JSON array. "
            "Static admin roles from principals.json are shown separately "
            "under 'static'. Only callable by current admins."
        )

    @property
    def read_only(self) -> bool:
        return True

    async def execute(self, **kwargs: Any) -> str:
        actor_id, api_key, err = _current_actor_with_key()
        if err:
            return err

        ok, msg = _policy_check("role.admin.list", actor_id, "")
        if not ok:
            return msg

        now = time.time()
        grants = _prune_active(await fetch_admin_grants(api_key, self._base_url), now)
        reg = get_registry()
        static_admins = [
            pid for pid in reg.ids
            if (p := reg.get(pid)) is not None and "admin" in (p.roles or [])
        ]
        return json.dumps(
            {"static": static_admins, "grants": grants},
            ensure_ascii=False,
            default=str,
        )


@tool_parameters(
    tool_parameters_schema(
        tz=StringSchema(
            "IANA timezone name, e.g. 'Europe/Moscow' or 'America/New_York'. "
            "Takes effect immediately for all users (no restart). Stored "
            "datetimes remain in UTC; only presentation and parsing of "
            "user-provided times change."
        ),
        required=["tz"],
    )
)
class AdminSetTzTool(Tool):
    """Set the single global familia timezone. Admin-only."""

    @property
    def name(self) -> str:
        return "admin_set_tz"

    @property
    def description(self) -> str:
        return (
            "Set the single global timezone used by familia for all users. "
            "All stored datetimes remain in UTC; this only changes how "
            "times are displayed to users and how user-provided times "
            "(reminders, schedules) are interpreted. Admin-only."
        )

    async def execute(self, tz: str, **kwargs: Any) -> str:
        from familia import tz as tzmod

        actor_id = get_current_actor()
        if not actor_id:
            return "Error: no actor in context"

        target = (tz or "").strip()
        if not target:
            return "Error: 'tz' is required"

        ok, msg = _policy_check("role.admin.set_tz", actor_id, "")
        if not ok:
            return msg

        before = tzmod.current_tz_name()
        try:
            tzmod.set_current_tz(target)
        except ValueError as e:
            return f"Error: {e}"

        audit.log_event(
            "tz_changed",
            actor=actor_id,
            # Explicit empty string (not absent) so SIEM/grep consumers
            # that key on to_chat see the same schema as grant/revoke.
            to_chat="",
            decision="allow",
            extra={"before": before, "after": target},
        )
        return f"Global timezone: {before} → {target}"
