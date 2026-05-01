#!/usr/bin/env bash
# Regenerate familia/requirements.lock from the pyproject specs of
# nanobot + familia. Run on a host with docker; uses the same digest-
# pinned base image as the production Dockerfile so the lock matches
# what will actually install at build time.
#
# When to re-run:
#   * After bumping any direct dep range in nanobot/pyproject.toml or
#     familia/pyproject.toml.
#   * On a periodic schedule to absorb security fixes in transitive deps
#     (no functional change required, just regen + commit).
#
# Output is committed to the repo so build-on-VM stays reproducible
# without needing the operator to have uv on their host.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# Same digest as Dockerfile FROM. Update both together.
BASE_IMAGE="ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58"

OUT="familia/requirements.lock"

echo "+ resolving deps via $BASE_IMAGE → $OUT"
docker run --rm \
  -v "$REPO_ROOT":/work -w /work \
  --entrypoint /bin/sh \
  "$BASE_IMAGE" \
  -c '
    set -e
    # Compile the union of nanobot + familia direct deps. uv pip
    # compile reads pyproject.toml [project] tables natively.
    uv pip compile \
        --generate-hashes \
        --output-file '"$OUT"' \
        nanobot/pyproject.toml familia/pyproject.toml
  '

echo "+ wrote $OUT ($(wc -l < "$OUT") lines)"
echo "  commit alongside the pyproject change that triggered the regen."
