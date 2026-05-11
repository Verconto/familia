"""Per-principal memory client used by ContextBuilder and CLI.

Wraps :mod:`familia.acl.graph_io` ``get_raw``/``set_raw`` with a thin
namespacing layer. The client is **per-actor**: it knows the
principal's id and api_key, and prepends ``private:<id>:`` to every
relative suffix (``value:user_profile``, ``value:memory`` etc).

Two flavours of access:

* :py:meth:`get` / :py:meth:`set` — own data, namespace is fixed to
  ``private:<self.principal_id>:``.

* :py:meth:`get_other` — read a peer's namespace (e.g. spouse's
  USER profile) **after** a synthetic policy-check. Never used to
  write — cross-principal writes from chat are policy-denied through
  the regular memory tools.

This is the single point that ContextBuilder uses to assemble per-turn
prompt content. Standalone nanobot (no ``principals.json``) never
constructs this client; the legacy file-based USER/MEMORY remains as
a fallback path in ContextBuilder.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from familia.acl.graph_io import GraphIOError, get_raw, set_raw
from familia.policy import Decision, PolicyContext, get_engine


class PrincipalMemoryClient:
    """memX gateway scoped to one principal's namespace."""

    def __init__(self, principal_id: str, api_key: str) -> None:
        if not principal_id:
            raise ValueError("principal_id is required")
        if not api_key:
            raise ValueError("api_key is required")
        self.principal_id = principal_id
        self._api_key = api_key

    def _own_key(self, suffix: str) -> str:
        return f"private:{self.principal_id}:{suffix}"

    def _other_key(self, other_id: str, suffix: str) -> str:
        return f"private:{other_id}:{suffix}"

    def get(self, suffix: str) -> str | None:
        """Read own ``private:<self.principal_id>:<suffix>`` value.

        Returns the raw string body, or ``None`` if the key is missing
        or memX is unreachable. Never raises — failure equals ``None``,
        ContextBuilder degrades gracefully.
        """
        try:
            raw = get_raw(self._own_key(suffix), api_key=self._api_key)
        except GraphIOError as exc:
            logger.warning("principal_memory.get({}): {}", suffix, exc)
            return None
        return _coerce_to_str(raw)

    def set(self, suffix: str, value: str) -> None:
        """Write own ``private:<self.principal_id>:<suffix>`` value.

        Raises :class:`GraphIOError` on memX failure — callers
        (CLI/admin) want to know.
        """
        set_raw(self._own_key(suffix), value, api_key=self._api_key)

    def get_other(self, other_id: str, suffix: str) -> str | None:
        """Read a peer's namespace under the family-by-default model.

        Path (0.3.0):
          1. self read → fast path through :meth:`get`.
          2. Synthetic policy check (legacy gate). Deny → None.
          3. If self and ``other_id`` are connected by a peer-edge in
             family.graph (``acl.peers.is_peer``), read via the admin
             proxy key. Records tagged ``secret`` are filtered out and
             yield ``None`` (fail-closed).
          4. Otherwise fall back to caller's own api_key. This still
             works for the admin/owner who holds ``private:*`` in their
             acl.json scope list, and silently 403s for narrow
             per-principal keys.

        On any exception, return ``None`` — never raise into the
        prompt-building path.
        """
        if other_id == self.principal_id:
            return self.get(suffix)
        full_key = self._other_key(other_id, suffix)
        try:
            decision = get_engine().evaluate(
                PolicyContext(
                    action="memory.read",
                    actor=self.principal_id,
                    to_chat=full_key,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "principal_memory.get_other({}): policy eval failed: {}",
                other_id, exc,
            )
            return None
        if decision.decision is Decision.DENY:
            return None

        # Peer-edge fast path. Only ``private:`` keys flow through here;
        # peers of one actor can read each other's private namespace by
        # default, with ``secret``-tagged records filtered.
        peer_path_ok = False
        if full_key.startswith("private:"):
            try:
                from familia.acl.peers import is_peer  # noqa: PLC0415
                peer_path_ok = is_peer(self.principal_id, other_id)
            except Exception:  # noqa: BLE001 — never break prompt assembly
                peer_path_ok = False
        if peer_path_ok:
            try:
                from familia.acl.graph_io import resolve_admin_key  # noqa: PLC0415
                proxy_key = resolve_admin_key()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "principal_memory.get_other({}): admin key unavailable: {}",
                    other_id, exc,
                )
                proxy_key = None
            if proxy_key:
                try:
                    raw = get_raw(full_key, api_key=proxy_key)
                except GraphIOError as exc:
                    logger.warning(
                        "principal_memory.get_other({}) proxy: {}", other_id, exc,
                    )
                    return None
                text = _coerce_to_str(raw)
                if text is None:
                    return None
                # Filter ``secret``-tagged records — same fail-closed
                # semantics as MemoryGetTool._read_peer_private.
                try:
                    from familia.acl import codec  # noqa: PLC0415
                    wrapped = codec.decode(text)
                except Exception:  # noqa: BLE001
                    wrapped = None
                if wrapped is not None:
                    if "secret" in (wrapped.tags or []):
                        return None
                    return wrapped.value
                return text

        # Fallback: caller's narrow key. Admin/owner with private:* in
        # their acl scope succeeds; narrow per-principal keys 403 →
        # None.
        try:
            raw = get_raw(full_key, api_key=self._api_key)
        except GraphIOError as exc:
            logger.warning("principal_memory.get_other({}): {}", other_id, exc)
            return None
        return _coerce_to_str(raw)


def _coerce_to_str(raw: Any) -> str | None:
    """memX may return None / str / dict (legacy). Normalise to str.

    None → None. dict/list → JSON re-encoded (legacy structured
    values; we store text these days). Everything else → str().
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    import json as _json
    try:
        return _json.dumps(raw, ensure_ascii=False)
    except Exception:  # noqa: BLE001
        return str(raw)
