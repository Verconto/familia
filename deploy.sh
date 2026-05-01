#!/usr/bin/env bash
# Deploy changed nanobot/familia files to the gateway VM.
#
# Usage:
#   ./deploy.sh <path> [<path> ...]         # sync files, restart container
#   ./deploy.sh --no-restart <path> ...     # sync only, skip restart
#   ./deploy.sh -h | --help                 # this help
#
# Paths are relative to this repo root and MUST start with "nanobot/" or
# "familia/" (e.g. nanobot/channels/vk.py, familia/src/familia/policy/engine.py).
# The script extracts at $REMOTE_ROOT on the VM; the compose file there
# mounts that path as the container's /app so local edits become live
# after a restart.
#
# IMPORTANT: this script uses `docker restart` (no compose). When the
# container needs to be RECREATED (image/cap/security_opt changes), do a
# manual round on the VM with the merged compose stack:
#   docker compose -f docker-compose.yml -f docker-compose.exec-sandbox.yml up -d
# The override file adds the SYS_ADMIN cap + unconfined seccomp/apparmor
# that bubblewrap needs for the exec tool. Without the override the gateway
# starts safe-by-default (cap_drop: ALL) — exec tool calls would fail at
# runtime.
#
# Environment:
#   FAMILIA_VM      — target VM SSH spec ("user@host"). REQUIRED — no default.
#   REMOTE_ROOT     — checkout path on the VM. Default: /opt/familia/upstream
#   CONTAINER       — container name to restart. Default: familia-gateway

set -euo pipefail

usage() {
    grep -E '^#( |$)' "$0" | sed -E 's/^# ?//'
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
fi

VM="${FAMILIA_VM:-}"
REMOTE_ROOT="${REMOTE_ROOT:-/opt/familia/upstream}"
CONTAINER="${CONTAINER:-familia-gateway}"
RESTART=1

if [[ -z "$VM" ]]; then
    echo "error: set FAMILIA_VM=user@host (or export it in your shell profile)" >&2
    exit 2
fi

if [[ "${1:-}" == "--no-restart" ]]; then
    RESTART=0
    shift
fi

if [[ $# -eq 0 ]]; then
    echo "usage: $0 [--no-restart] {nanobot|familia}/path/to/file [...]" >&2
    exit 2
fi

for p in "$@"; do
    if [[ "$p" != nanobot/* && "$p" != familia/* ]]; then
        echo "refusing to deploy '$p': path must start with nanobot/ or familia/" >&2
        exit 2
    fi
    if [[ ! -e "$p" ]]; then
        echo "refusing to deploy '$p': not found locally" >&2
        exit 2
    fi
done

echo "→ syncing $# path(s) to $VM:$REMOTE_ROOT"
tar -cf - "$@" | ssh "$VM" "cd $REMOTE_ROOT && tar -xvf -" 2>&1 | sed 's/^/  /'

if (( RESTART )); then
    echo "→ restarting $CONTAINER"
    ssh "$VM" "docker restart $CONTAINER >/dev/null && sleep 4 && docker logs --tail 15 $CONTAINER 2>&1 | tail -10"
else
    echo "→ skipped restart (--no-restart)"
fi
