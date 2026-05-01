"""Familia role layer: admin role with temporary delegation.

Roles live in two places:

* **Static** — ``Principal.roles`` loaded from ``principals.json``.  These
  are permanent role assignments (e.g. ``owner`` is always admin).
* **Dynamic** — ``shared:roles.admin_grants`` in memX.  A JSON list of
  temporary grants ``[{principal, granted_by, until_ts_utc, reason}]``.
  Expired grants are ignored on read.

Effective roles for an actor = static ∪ (active grants where
``principal == actor``).  The function :func:`load_effective_roles`
fetches both and stores the per-actor role map in a ContextVar so
policy evaluation can read it synchronously.  Call this once at the
start of each agent turn, right after ``set_current_actor``.
"""

from __future__ import annotations

import json
import time
from contextvars import ContextVar
from types import MappingProxyType
from typing import Any, Mapping

import httpx
from loguru import logger

from familia.memx_client import memx_base_url
from familia.principals import get_registry


ADMIN_GRANTS_KEY = "shared:roles.admin_grants"

_EMPTY_ROLES: Mapping[str, frozenset[str]] = MappingProxyType({})
_current_roles: ContextVar[Mapping[str, frozenset[str]]] = ContextVar(
    "familia_current_roles", default=_EMPTY_ROLES
)


def _static_roles() -> dict[str, set[str]]:
    reg = get_registry()
    out: dict[str, set[str]] = {}
    for pid in reg.ids:
        p = reg.get(pid)
        if p and p.roles:
            out[pid] = set(p.roles)
    return out


def _active_grants(raw: Any, now_ts: float) -> list[dict[str, Any]]:
    """Filter a grants list to only entries whose ``until_ts_utc`` is in the future."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for g in raw:
        if not isinstance(g, dict):
            continue
        until = g.get("until_ts_utc")
        if isinstance(until, (int, float)) and until > now_ts:
            out.append(g)
    return out


def compute_effective_roles(
    grants_raw: Any, now_ts: float | None = None
) -> dict[str, frozenset[str]]:
    """Pure: merge static principal roles with active grants."""
    now_ts = now_ts if now_ts is not None else time.time()
    out = _static_roles()
    for g in _active_grants(grants_raw, now_ts):
        pid = g.get("principal")
        if pid:
            out.setdefault(pid, set()).add("admin")
    return {k: frozenset(v) for k, v in out.items()}


async def fetch_admin_grants(
    api_key: str, base_url: str | None = None
) -> list[dict[str, Any]]:
    """Fetch the raw admin grants list from memX.  Empty list on error/missing."""
    url = (base_url or memx_base_url()) + "/get"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(
                url,
                headers={"x-api-key": api_key},
                params={"key": ADMIN_GRANTS_KEY},
            )
    except httpx.HTTPError as exc:
        logger.warning("familia roles: memX unreachable ({}): {}", type(exc).__name__, exc)
        return []
    if r.status_code == 404:
        return []
    if r.status_code >= 400:
        logger.warning("familia roles: memX {} — {}", r.status_code, r.text[:200])
        return []
    try:
        payload = r.json()
    except ValueError:
        return []
    if payload is None:
        return []
    value = payload.get("value", payload) if isinstance(payload, dict) else payload
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return value if isinstance(value, list) else []


async def write_admin_grants(
    grants: list[dict[str, Any]], api_key: str, base_url: str | None = None
) -> tuple[bool, str]:
    """Persist the grants list to memX.  Returns (ok, error_string)."""
    url = (base_url or memx_base_url()) + "/set"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post(
                url,
                headers={"x-api-key": api_key},
                json={"key": ADMIN_GRANTS_KEY, "value": json.dumps(grants, ensure_ascii=False)},
            )
    except httpx.HTTPError as exc:
        return False, f"memX unreachable ({type(exc).__name__}: {exc})"
    if r.status_code >= 400:
        return False, f"memX {r.status_code}: {r.text[:200]}"
    return True, ""


async def load_effective_roles(actor_id: str | None, base_url: str | None = None) -> None:
    """Load static + dynamic roles and store them in the turn-scoped ContextVar.

    Call at the start of each agent turn.  Reads memX under ``actor_id``'s
    key (admin_grants is in ``shared:*``, readable by all principals).
    If the actor has no memx_key or isn't set, only static roles are used.
    """
    if not actor_id:
        _current_roles.set({k: frozenset(v) for k, v in _static_roles().items()})
        return
    p = get_registry().get(actor_id)
    if p is None or not p.memx_key:
        _current_roles.set({k: frozenset(v) for k, v in _static_roles().items()})
        return
    grants = await fetch_admin_grants(p.memx_key, base_url)
    _current_roles.set(compute_effective_roles(grants))


def get_effective_roles(actor_id: str | None) -> frozenset[str]:
    """Return the effective role set for ``actor_id`` (empty if unknown)."""
    if not actor_id:
        return frozenset()
    return _current_roles.get().get(actor_id, frozenset())


def set_effective_roles_for_tests(roles_map: dict[str, set[str] | frozenset[str]]) -> None:
    """Test helper — directly populate the ContextVar."""
    _current_roles.set({k: frozenset(v) for k, v in roles_map.items()})
