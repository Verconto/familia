#!/usr/bin/env bash
#
# Run the familia pytest suite.
#
# Modes:
#   --local         Run locally against ./familia/tests (default if no
#                   FAMILIA_VM is set). Requires familia + nanobot installed
#                   locally (pip install -e nanobot -e familia pytest pytest-asyncio).
#   --vm            Run on $FAMILIA_VM inside a disposable cli container
#                   (docker-compose.cli.yml). Test files are scp'd into a
#                   staging dir, bind-mounted, `python -m pytest` runs there.
#                   Production familia-gateway is NOT touched — no mutating
#                   installs in a long-running container.
#   --help, -h      Print this usage.
#
# Extra arguments after the mode flag are forwarded to pytest.
#
# Defaults: if FAMILIA_VM is set → --vm, otherwise → --local.

set -euo pipefail

usage() {
    grep -E '^#( |$)' "$0" | sed -E 's/^# ?//'
}

REPO="$(cd "$(dirname "$0")" && pwd)"

MODE=""
case "${1:-}" in
    --local|--vm) MODE="${1#--}"; shift ;;
    -h|--help)    usage; exit 0 ;;
esac

if [[ -z "$MODE" ]]; then
    if [[ -n "${FAMILIA_VM:-}" ]]; then MODE="vm"; else MODE="local"; fi
fi

if [[ "$MODE" == "local" ]]; then
    echo "→ running familia tests locally"
    cd "$REPO/familia"
    exec python -m pytest tests -v "$@"
fi

# --- VM mode ---

VM="${FAMILIA_VM:-}"
REMOTE_ROOT="${REMOTE_ROOT:-/opt/familia}"
STAGE_HOST="${STAGE_HOST:-/tmp/familia-test}"

if [[ -z "$VM" ]]; then
    echo "error: --vm mode requires FAMILIA_VM=user@host" >&2
    exit 2
fi

# Properly quote forwarded pytest args for remote shell.
PYTEST_ARGS=""
for a in "$@"; do
    PYTEST_ARGS+=" $(printf '%q' "$a")"
done

echo "→ syncing test files to $VM:$STAGE_HOST"
ssh "$VM" "mkdir -p $STAGE_HOST/tests"
scp -qr "$REPO/familia/tests/." "$VM:$STAGE_HOST/tests/"

# Run via disposable cli container. pytest install lands in the shared
# `familia-devtools` named volume (mounted at /home/nanobot/.local), which
# is persistent across runs — so subsequent invocations skip the install.
# Crucially, this does NOT touch the running familia-gateway.
echo "→ running pytest via familia-cli container"
ssh "$VM" \
    REMOTE_ROOT="$REMOTE_ROOT" STAGE_HOST="$STAGE_HOST" \
    PYTEST_ARGS="$PYTEST_ARGS" \
    bash <<'EOF'
set -e
cd "$REMOTE_ROOT"
docker compose -f docker-compose.cli.yml run --rm \
    -v "$STAGE_HOST/tests:/tmp/familia-tests:ro" \
    --entrypoint sh \
    cli -c "python -c 'import pytest' 2>/dev/null || pip install --user --quiet pytest pytest-asyncio; cd /tmp/familia-tests && python -m pytest . -v $PYTEST_ARGS"
EOF
