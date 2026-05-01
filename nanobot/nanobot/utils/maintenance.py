"""Background maintenance utilities for the nanobot runtime.

Three system jobs are registered at gateway start (see
``register_maintenance_system_jobs``); each is dispatched through the
existing cron service:

* ``media_cleanup`` — hourly. Drops media files in ``~/.nanobot/media/``
  whose mtime is older than ``MEDIA_TTL_SECONDS`` (default 24 h). Media
  is only used at the moment of LLM call (base64-encoded into the user
  message), so anything older than the next turn is dead weight.

* ``sessions_cleanup`` — daily. Removes ``workspace/sessions/*.jsonl``
  files unmodified for ``SESSIONS_TTL_SECONDS`` (default 90 days).
  Long-idle sessions accumulate even after autocompact (file remains
  on disk with the metadata stub).

* ``workspace_git_gc`` — monthly. Runs ``git gc --auto`` against
  ``workspace/.git`` so per-edit objects of MEMORY/USER/SOUL get packed.

All functions are no-op-on-error: failure is logged, the gateway turn
is not affected.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from loguru import logger

from nanobot.config.paths import (
    get_data_dir,
    get_media_dir,
    get_workspace_path,
)


# ---------------------------------------------------------------------------
# Tunables. Left as module-level constants for now; can be lifted into
# config.json schema if/when the user wants per-deploy overrides.
# ---------------------------------------------------------------------------

MEDIA_TTL_SECONDS = 24 * 60 * 60          # 1 day
SESSIONS_TTL_SECONDS = 90 * 24 * 60 * 60  # 90 days


# ---------------------------------------------------------------------------
# Media cleanup
# ---------------------------------------------------------------------------

def cleanup_media(ttl_seconds: int = MEDIA_TTL_SECONDS) -> tuple[int, int]:
    """Delete media files older than ``ttl_seconds``.

    Returns ``(files_deleted, bytes_freed)``. Errors per-file are
    logged at ``debug`` (so a single un-deletable file doesn't spam
    on every tick).
    """
    root = get_media_dir()
    if not root.exists():
        return 0, 0
    cutoff = time.time() - ttl_seconds
    deleted = 0
    freed = 0
    # ``rglob`` pulls everything below media/<channel>/<sender>/<file>.
    for f in root.rglob("*"):
        if not f.is_file():
            continue
        try:
            st = f.stat()
        except OSError:
            continue
        if st.st_mtime > cutoff:
            continue
        size = st.st_size
        try:
            f.unlink()
        except OSError as exc:
            logger.debug("media_cleanup: cannot unlink {}: {}", f, exc)
            continue
        deleted += 1
        freed += size
    # Best-effort prune of empty channel/sender directories (cosmetic;
    # leaves the root ``media/`` itself alone).
    for d in sorted(root.rglob("*"), reverse=True):
        if d == root or not d.is_dir():
            continue
        try:
            if not any(d.iterdir()):
                d.rmdir()
        except OSError:
            pass
    if deleted:
        logger.info(
            "media_cleanup: deleted {} files, freed {} bytes",
            deleted, freed,
        )
    return deleted, freed


# ---------------------------------------------------------------------------
# Sessions cleanup
# ---------------------------------------------------------------------------

def cleanup_sessions(ttl_seconds: int = SESSIONS_TTL_SECONDS) -> tuple[int, int]:
    """Delete ``workspace/sessions/*.jsonl`` files unmodified for *ttl*.

    A 90-day-quiet sender is exceedingly likely to have left the
    family or moved to a different channel; their old session file is
    no longer informative. If they do come back, a fresh session is
    created automatically — same effect as a brand-new chat.

    Returns ``(files_deleted, bytes_freed)``.
    """
    workspace = get_workspace_path()
    sessions_dir = workspace / "sessions"
    if not sessions_dir.exists():
        return 0, 0
    cutoff = time.time() - ttl_seconds
    deleted = 0
    freed = 0
    for f in sessions_dir.glob("*.jsonl"):
        try:
            st = f.stat()
        except OSError:
            continue
        if st.st_mtime > cutoff:
            continue
        size = st.st_size
        try:
            f.unlink()
        except OSError as exc:
            logger.debug("sessions_cleanup: cannot unlink {}: {}", f, exc)
            continue
        deleted += 1
        freed += size
    if deleted:
        logger.info(
            "sessions_cleanup: deleted {} files, freed {} bytes",
            deleted, freed,
        )
    return deleted, freed


# ---------------------------------------------------------------------------
# Workspace git gc
# ---------------------------------------------------------------------------

def workspace_git_gc() -> bool:
    """Run ``git gc --auto`` against ``workspace/.git``.

    Packs the per-edit objects produced by ``GitStore`` for MEMORY/
    USER/SOUL into a packfile. ``--auto`` is conservative — if there
    are few loose objects it's a noop. Returns True on success.
    """
    workspace = get_workspace_path()
    git_dir = workspace / ".git"
    if not git_dir.is_dir():
        return False
    try:
        # ``git gc --auto`` exits 0 even when it decides to skip;
        # we just propagate non-zero as failure.
        subprocess.run(
            ["git", "-C", str(workspace), "gc", "--auto"],
            check=True, capture_output=True, text=True, timeout=120,
        )
        logger.info("workspace_git_gc: ok")
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError) as exc:
        logger.warning("workspace_git_gc: failed: {}", exc)
        return False


# ---------------------------------------------------------------------------
# Disk-usage report (consumed by the admin app's Maintenance page)
# ---------------------------------------------------------------------------

def disk_usage_report() -> dict:
    """Snapshot disk usage by category + free space on the VM.

    Returned schema (JSON-friendly)::

        {
          "categories": [
            {"name": str, "path": str, "bytes": int, "files": int}
          ],
          "vm": {"path": str, "free_bytes": int, "total_bytes": int}
        }
    """
    data_dir = get_data_dir()
    workspace = get_workspace_path()

    categories = [
        ("media", get_media_dir()),
        ("sessions", workspace / "sessions"),
        ("memory", workspace / "memory"),
        ("workspace_git", workspace / ".git"),
        ("audit", Path(os.environ.get("FAMILIA_AUDIT_FILE", str(data_dir / "audit.jsonl")))),
        ("cron", workspace / "cron"),
        ("logs", data_dir / "logs"),
    ]
    out: list[dict] = []
    for name, path in categories:
        size, count = _path_size(path)
        out.append({
            "name": name,
            "path": str(path),
            "bytes": size,
            "files": count,
        })

    # ``/`` because that's what the user cares about — the disk that
    # actually runs out and crashes everything. The container's
    # filesystem inherits the host's root partition through Docker's
    # storage driver; statvfs reports the host-visible numbers.
    try:
        st = shutil.disk_usage("/")
        vm = {"path": "/", "free_bytes": st.free, "total_bytes": st.total}
    except OSError as exc:
        vm = {"path": "/", "free_bytes": 0, "total_bytes": 0,
              "error": str(exc)}

    return {"schema_version": 1, "categories": out, "vm": vm}


def _path_size(path: Path) -> tuple[int, int]:
    """Return ``(bytes, files)`` recursive summary for *path*.

    For a single file: size + 1. For a directory: sum of all files.
    Missing path → ``(0, 0)``. Errors per-file silently skipped.
    """
    if not path.exists():
        return 0, 0
    if path.is_file():
        try:
            return path.stat().st_size, 1
        except OSError:
            return 0, 0
    total = 0
    count = 0
    for f in path.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
                count += 1
        except OSError:
            continue
    return total, count
