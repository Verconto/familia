"""Lazy accessors for the memX base URL.

Every tool that talks to memX used to snapshot ``os.environ["MEMX_BASE_URL"]``
at import time, which made it impossible for tests (or hot-reloads) to
point the client at a different backend after start. Centralize the lookup
here and read per-call.
"""

from __future__ import annotations

import os

_FALLBACK = "http://memx-backend:8000"


def memx_base_url() -> str:
    """Return the current memX base URL from the environment.

    Read on every call — do NOT cache. Cheap (dict lookup) and keeps
    tests/reconfiguration honest.
    """
    return os.environ.get("MEMX_BASE_URL", _FALLBACK)
