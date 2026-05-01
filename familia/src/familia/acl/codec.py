"""Sentinel-based codec for wrapped (tagged) memory values (SR-4, SR-10).

Encoding is unambiguous: every wrapped value is a JSON object with the
sentinel + tags + value keys, and *only* those keys. The encoder never
emits a legacy form.

Decoding is deliberately strict. Anything that doesn't match the exact
shape is reported as legacy (returns ``None`` for the wrapper, and the
original raw string as the inner value). This is the SR-4 invariant:
adversarial JSON that *looks* tagged (e.g. ``{"tags": ["admin"]}``) must
be treated as opaque legacy, not as a tagged record.

Why the sentinel matters: before this feature shipped, callers could
write any JSON-shaped value into memX. If the decoder trusted ``tags``
alone, an attacker writing ``{"tags": ["admin"], "value": "x"}`` as a
plain value (legacy path, before this feature) could later be misread by
a tag-aware reader as having admin-only ACL — and bypass the legacy
scope-based check. The sentinel + literal-True check forecloses that.
"""

from __future__ import annotations

import json
from typing import Any

from familia.acl.schema import WRAP_SENTINEL, WRAP_SENTINEL_KEY, WrappedRecord


def encode(value: str, tags: list[str]) -> str:
    """Serialize a value+tags pair into the wrapper format.

    ``tags`` is normalized: each entry is stripped, empty/non-string ones
    are dropped, duplicates are removed (preserving first-seen order so
    callers can reason about audit log readability).
    """
    norm: list[str] = []
    seen: set[str] = set()
    for t in tags or []:
        if not isinstance(t, str):
            continue
        s = t.strip()
        if not s or s in seen:
            continue
        seen.add(s)
        norm.append(s)
    payload = {
        WRAP_SENTINEL_KEY: WRAP_SENTINEL,
        "tags": norm,
        "value": value if isinstance(value, str) else str(value),
    }
    return json.dumps(payload, ensure_ascii=False)


def decode(raw: str) -> WrappedRecord | None:
    """Return ``WrappedRecord`` if ``raw`` is a valid wrapper, else ``None``.

    A return of ``None`` means: treat ``raw`` as legacy untagged content
    (use scope-based ACL, return the bytes as-is to the reader). The
    function never raises on parse errors — fail-closed semantics live in
    the caller, who interprets ``None`` as "no tags to enforce".
    """
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    sentinel = parsed.get(WRAP_SENTINEL_KEY)
    # Strict identity, not truthiness: ``"true"`` (string) or ``1`` (int)
    # must not pass.
    if sentinel is not WRAP_SENTINEL:
        return None
    raw_tags = parsed.get("tags")
    if not isinstance(raw_tags, list):
        return None
    tags: list[str] = []
    for t in raw_tags:
        if not isinstance(t, str):
            return None  # any non-string in tags array → reject the whole thing
        tags.append(t)
    inner = parsed.get("value")
    if not isinstance(inner, str):
        return None
    return WrappedRecord(tags=tuple(tags), value=inner)
