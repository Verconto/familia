#!/usr/bin/env bash
# Regenerate patches/*.patch after an upstream subtree pull (or after an
# in-place edit to one of the tracked upstream files).
#
# Usage:
#   ./patches/regenerate.sh                       # use default UPSTREAM commit
#   UPSTREAM=<sha> ./patches/regenerate.sh        # pin to a specific commit
#
# The reference commit is the subtree-merge where familia diverges from
# vanilla nanobot. Bump the default when you pull a new subtree.

set -euo pipefail

UPSTREAM="${UPSTREAM:-328a386}"

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

if ! git rev-parse --verify "$UPSTREAM^{commit}" >/dev/null 2>&1; then
    echo "refusing to regenerate: UPSTREAM=$UPSTREAM not found in git history" >&2
    echo "(set UPSTREAM=<sha> to the subtree-merge commit you want to diff against)" >&2
    exit 2
fi

FILES=(
    nanobot/nanobot/agent/loop.py
    nanobot/nanobot/agent/context.py
    nanobot/nanobot/agent/memory.py
    nanobot/nanobot/agent/tools/message.py
    nanobot/nanobot/channels/base.py
    nanobot/nanobot/channels/vk.py
    nanobot/nanobot/cli/commands.py
    nanobot/nanobot/command/builtin.py
)

echo "→ regenerating patches against $UPSTREAM"

for f in "${FILES[@]}"; do
    name=$(echo "$f" | sed 's|nanobot/nanobot/||;s|/|_|g;s|\.py$|.patch|')
    git diff "$UPSTREAM" -- "$f" > "patches/$name"
    echo "  patches/$name"
done

git diff "$UPSTREAM" -- nanobot/pyproject.toml > patches/pyproject.patch
echo "  patches/pyproject.patch"

echo "→ done. Review with: git diff -- patches/"
