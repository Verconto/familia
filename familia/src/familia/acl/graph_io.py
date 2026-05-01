"""Direct memX I/O for graph mutations (SR-5, SR-9, SR-14, SR-17).

Used by the CLI (which is the only sanctioned write path for
``shared:family.graph`` and ``shared:topics.graph``). The chat-flow
``MemorySetTool`` is policy-denied for these keys; this module is the
companion path for admins on the VM.

API key resolution (SR-5):

* First choice: ``/etc/familia/admin.key``, mode 0400, owner=root. Read
  with explicit mode-check; refuse to use a world-readable file.
* Dev fallback: env ``FAMILIA_ADMIN_MEMX_KEY``. Emits a warning. The
  caller (CLI) decides whether to continue (default) or hard-fail in
  production deploys.
* Tertiary fallback: the ``memx_key`` of any principal with role
  ``admin`` from ``principals.json``. Useful in single-host setups
  where the admin runs CLI as the same UNIX user that owns
  ``~/.nanobot/principals.json``.

The choice is logged once at process start so audits can correlate
graph_edit events with the path used to authenticate.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

import httpx
from loguru import logger


ADMIN_KEY_PATH = Path("/etc/familia/admin.key")
ENV_FALLBACK = "FAMILIA_ADMIN_MEMX_KEY"
HTTP_TIMEOUT_SECS = 10.0


class GraphIOError(RuntimeError):
    """Anything that should make a CLI command exit non-zero."""


# ---------------------------------------------------------------------------
# Admin-key resolution
# ---------------------------------------------------------------------------

def _read_admin_key_from_file(path: Path = ADMIN_KEY_PATH) -> str | None:
    """Return the key string if file is present and securely permissioned.

    SR-5: refuse files that are group/world-readable. POSIX only — on
    Windows we skip the mode check (dev path).
    """
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning("admin-key file unreadable at {}: {}", path, exc)
        return None
    if os.name == "posix":
        mode = stat.S_IMODE(st.st_mode)
        if mode & 0o077:
            raise GraphIOError(
                f"admin-key file {path} has insecure mode {oct(mode)}; "
                "must be 0400 (root-only readable)"
            )
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise GraphIOError(f"failed to read {path}: {exc}") from exc
    return text or None


# SR-5 fallback warnings dedupe. Without this the env / principals
# fallbacks emit a WARN on every CLI invocation — and since seed_graph
# runs ~10 graph mutations in a row, each calling resolve_admin_key,
# the install log fills with the same line ~30 times. We still want
# the warning loud, but exactly once per Python process; the audit
# trail shows the source either way (each graph_edit event is logged
# in audit.jsonl with the resolved-source attribute).
_FALLBACK_WARNED: set[str] = set()


def _warn_once(source: str, message: str) -> None:
    if source in _FALLBACK_WARNED:
        return
    _FALLBACK_WARNED.add(source)
    logger.warning(message)


def _read_admin_key_from_env() -> str | None:
    val = os.environ.get(ENV_FALLBACK, "").strip()
    if val:
        _warn_once(
            "env",
            f"admin key from env {ENV_FALLBACK} (dev fallback) — production "
            f"deployments should provision /etc/familia/admin.key with mode 0400",
        )
        return val
    return None


def _read_admin_key_from_principals() -> str | None:
    """Last resort: any ``role: admin`` principal's memx_key from registry.

    Emits a loud WARN that mirrors the env-fallback wording (SR-5 demands
    operators see when admin auth comes from anything weaker than the
    0400 root-only file). Without the warn the registry path would
    silently widen the attack surface — principals.json is typically
    0644 group-readable, vs the file fallback's mandatory 0400.
    """
    try:
        from familia.principals import get_registry
    except ImportError:
        return None
    reg = get_registry()
    for pid in reg.ids:
        p = reg.get(pid)
        if p is None or not p.memx_key:
            continue
        if "admin" in (p.roles or []):
            _warn_once(
                "principals",
                "admin key from principals registry (last-resort fallback) "
                "— production deployments should provision /etc/familia/admin.key "
                "with mode 0400 instead of trusting the registry file's mode",
            )
            return p.memx_key
    return None


def resolve_admin_key() -> str:
    """Return the admin memX key, raising :class:`GraphIOError` on failure."""
    for source, fn in (
        ("file", _read_admin_key_from_file),
        ("env", _read_admin_key_from_env),
        ("principals", _read_admin_key_from_principals),
    ):
        try:
            key = fn()
        except GraphIOError:
            raise
        if key:
            # Same dedupe pattern as the WARN — log the resolved source
            # exactly once per process. ``"info:<source>"`` keys it
            # separately from the warn dedupe so a future env→file
            # transition would still log once.
            _warn_once(
                f"info:{source}",
                f"admin-key loaded from {source}",
            )
            return key
    raise GraphIOError(
        f"no admin memX key available; expected file {ADMIN_KEY_PATH}, "
        f"env {ENV_FALLBACK}, or a principal with role: admin in registry"
    )


# ---------------------------------------------------------------------------
# memX read/write helpers
# ---------------------------------------------------------------------------

def _memx_url() -> str:
    try:
        from familia.memx_client import memx_base_url
    except ImportError:
        return os.environ.get("MEMX_BASE_URL", "http://172.17.0.1:8100")
    return memx_base_url()


def get_raw(key: str, *, api_key: str | None = None) -> Any:
    """Fetch a raw memX value (already JSON-decoded, or None on 404)."""
    api_key = api_key or resolve_admin_key()
    url = f"{_memx_url()}/get"
    try:
        r = httpx.get(url, headers={"x-api-key": api_key},
                      params={"key": key}, timeout=HTTP_TIMEOUT_SECS)
    except httpx.HTTPError as exc:
        raise GraphIOError(f"memX unreachable: {exc}") from exc
    if r.status_code == 404:
        return None
    if r.status_code == 403:
        raise GraphIOError(f"memX denied access to {key} (403)")
    if r.status_code >= 400:
        raise GraphIOError(f"memX {r.status_code} on get {key}: {r.text[:200]}")
    try:
        payload = r.json()
    except ValueError as exc:
        raise GraphIOError(f"memX returned non-JSON for {key}: {exc}") from exc
    if payload is None:
        return None
    if isinstance(payload, dict) and "value" in payload:
        return payload["value"]
    return payload


def set_raw(key: str, value: Any, *, api_key: str | None = None) -> None:
    """Persist a value back to memX. Stores as JSON string of the dict."""
    api_key = api_key or resolve_admin_key()
    url = f"{_memx_url()}/set"
    body = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    try:
        r = httpx.post(url, headers={"x-api-key": api_key},
                       json={"key": key, "value": body},
                       timeout=HTTP_TIMEOUT_SECS)
    except httpx.HTTPError as exc:
        raise GraphIOError(f"memX unreachable: {exc}") from exc
    if r.status_code >= 400:
        raise GraphIOError(f"memX {r.status_code} on set {key}: {r.text[:200]}")


def load_graph_value(key: str, *, api_key: str | None = None) -> dict[str, Any]:
    """Fetch a graph and return a plain dict (empty on missing/null/corrupt).

    Callers convert via :class:`Graph.from_dict` for typed access.
    """
    raw = get_raw(key, api_key=api_key)
    if raw is None:
        return {"nodes": [], "edges": [], "updated_at_ms": 0}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            # SR-10: fail-closed → empty graph rather than silent passthrough.
            logger.error("graph at {} is corrupt; treating as empty", key)
            return {"nodes": [], "edges": [], "updated_at_ms": 0}
    if not isinstance(raw, dict):
        logger.error("graph at {} is not an object; treating as empty", key)
        return {"nodes": [], "edges": [], "updated_at_ms": 0}
    raw.setdefault("nodes", [])
    raw.setdefault("edges", [])
    raw.setdefault("updated_at_ms", 0)
    return raw
