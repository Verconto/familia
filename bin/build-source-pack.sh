#!/usr/bin/env bash
# Build the source-pack for the admin install wizard's "build-on-VM"
# flow.
#
# Usage (from repo root):
#   ./bin/build-source-pack.sh 0.4.0 [/output/dir]
#
# Produces:
#   <output>/familia-source-v<VERSION>.tar.gz       — sources for VM build
#   <output>/familia-source-v<VERSION>.tar.gz.sha256
#
# The admin install wizard SFTP-uploads this archive to the target VM,
# untars it under $INSTALL_DIR, and runs `docker compose build` there.
# That replaces the legacy "ship 700 MB pre-built image archive" flow:
# the operator now uploads ~10 MB instead of ~700 MB, the VM pulls
# Python/apt/pip from public registries on first install, and updates
# of just our code re-ship a 10 MB tarball instead of a 700 MB image
# bundle.
#
# What goes inside the archive (relative paths preserved so docker
# compose's COPY directives Just Work):
#
#   familia/                  — our package source
#   nanobot/{nanobot,bridge,pyproject.toml,LICENSE,README.md}
#                              — forked nanobot subtree (patched)
#   memx/                     — vendored memX subtree
#   memx-config/acl.example.json
#                              — copied to acl.json by bootstrap
#   patches/                  — diffs for verification (audit trail)
#   Dockerfile                — familia-assistant build recipe
#   docker-compose.yml        — main stack
#   docker-compose.memx.yml   — memX stack
#   docker-compose.exec-sandbox.yml
#   bin/build-source-pack.sh  — for the operator to inspect what we ship
#
# What we deliberately DO NOT pack:
#   admin/                    — gitignored, not part of public release
#   dist/                     — release artefacts go here, no recursion
#   notes/                    — local planning docs (gitignored)
#   .git/                     — repo history is on the orphan branch only
#   __pycache__/, .pytest_cache/, .ruff_cache/, *.pyc
#                              — build droppings

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: $0 <version> [output-dir]" >&2
    echo "example: $0 0.4.0 dist/admin/" >&2
    exit 2
fi
VER="$1"
OUT_DIR="${2:-dist/admin}"

cd "$(dirname "$0")/.."

mkdir -p "$OUT_DIR"
ARCHIVE="$OUT_DIR/familia-source-v${VER}.tar.gz"

# Stage everything in a clean temp dir so we can call ``tar`` once and
# emit deterministic output (no half-staged files, no wandering over
# whatever happens to be in the working copy). ``--mtime`` + sort + a
# fixed numeric uid/gid normalize across machines.
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

# Per-component allow-list. Dockerfile only references a tight set of
# files; demos / GIF'ed README assets / upstream tests are 30+ MB and
# would balloon the pack with no build-time benefit.
ROOT_FILES=(
    Dockerfile
    docker-compose.yml
    docker-compose.memx.yml
    docker-compose.exec-sandbox.yml
    docker-compose.local.yml
    docker-compose.memx.local.yml
    .env.example
    principals.example.json
    LICENSE
    README.md
    THIRD_PARTY_NOTICES.md
)
# nanobot is a forked subtree — we keep upstream attribution
# (LICENSE, COMMUNICATION.md, SECURITY.md, CONTRIBUTING.md, README) but
# drop case/ (31 MB of demo gifs), tests/, webui/, docs/, images/. The
# Dockerfile only COPYs nanobot/{pyproject.toml,README.md,LICENSE,nanobot/,bridge/,entrypoint.sh}.
NANOBOT_INCLUDES=(
    nanobot/pyproject.toml
    nanobot/README.md
    nanobot/LICENSE
    nanobot/COMMUNICATION.md
    nanobot/SECURITY.md
    nanobot/CONTRIBUTING.md
    nanobot/THIRD_PARTY_NOTICES.md
    nanobot/entrypoint.sh
    nanobot/nanobot
    nanobot/bridge
)
# familia: pyproject + README + LICENSE + source. Drops tests/, audit
# logs, .claude/, workspace/.
FAMILIA_INCLUDES=(
    familia/pyproject.toml
    familia/README.md
    familia/policy.example.yaml
    familia/src
    # Optional: shipped only if regenerated. Dockerfile detects presence
    # and switches to hash-locked install when it's there.
    familia/requirements.lock
)
# memx: tracked subtree (vendored upstream) plus the compose-mounted
# acl.example. All small — fine to take whole, except runtime
# droppings.
MEMX_INCLUDES=(
    memx
)
# patches/ holds nanobot patches as audit trail.
PATCHES_INCLUDES=(
    patches
)
# The bin/ scripts ship for transparency — operator can re-pack
# themselves from sources if they want to verify.
EXTRA_FILES=(
    bin/build-source-pack.sh
    bin/release-admin.sh
)
# memx-config: we ship only the example. acl.json is operator-side
# and gitignored.
MEMX_CONFIG_FILES=(
    memx-config/acl.example.json
)

# Files to exclude from any directory copy regardless of inclusion
# rule — runtime / IDE / cache droppings.
EXCLUDES=(
    --exclude='__pycache__'
    --exclude='.pytest_cache'
    --exclude='.ruff_cache'
    --exclude='.mypy_cache'
    --exclude='*.pyc'
    --exclude='*.pyo'
    --exclude='node_modules'
    --exclude='.git'
    --exclude='.gitignore'
    --exclude='.claude'
    --exclude='.idea'
    --exclude='.vscode'
    --exclude='dist'
    --exclude='build'
    --exclude='*.egg-info'
    --exclude='audit.jsonl'
    --exclude='audit.jsonl.*'
    --exclude='workspace'
    --exclude='*.bak'
    --exclude='*.bak.*'
    --exclude='*.tmp'
    --exclude='*.local.*'
    --exclude='_tmp_*'
    --exclude='principals.json'
    --exclude='policy.yaml'
    --exclude='STATE.md'
    --exclude='PLAN.md'
    --exclude='CLAUDE.md'
)

echo "→ staging files"
for f in "${ROOT_FILES[@]}"; do
    if [[ -f "$f" ]]; then
        install -D "$f" "$STAGE/$f"
    fi
done

stage_paths() {
    # Helper: copy each path (file or dir) preserving structure.
    local entries=("$@")
    for p in "${entries[@]}"; do
        if [[ -f "$p" ]]; then
            install -D "$p" "$STAGE/$p"
        elif [[ -d "$p" ]]; then
            mkdir -p "$STAGE/$(dirname "$p")"
            tar -cf - "${EXCLUDES[@]}" "$p" | tar -xf - -C "$STAGE"
        else
            echo "warn: skipping missing $p" >&2
        fi
    done
}

stage_paths "${NANOBOT_INCLUDES[@]}"
stage_paths "${FAMILIA_INCLUDES[@]}"
stage_paths "${MEMX_INCLUDES[@]}"
stage_paths "${PATCHES_INCLUDES[@]}"

for f in "${EXTRA_FILES[@]}" "${MEMX_CONFIG_FILES[@]}"; do
    if [[ -f "$f" ]]; then
        install -D "$f" "$STAGE/$f"
    fi
done

# Drop in a small VERSION file so an operator who untars this somewhere
# weird can still tell what build it was.
echo "$VER" > "$STAGE/SOURCE_VERSION"

# memx-config/acl.example.json is the only memx-config file we ship;
# acl.json is operator-generated and gitignored. Sanity-check that we
# didn't accidentally pull a real acl.json into the pack.
if [[ -f "$STAGE/memx-config/acl.json" ]]; then
    echo "error: pack contains real memx-config/acl.json — refusing to ship" >&2
    exit 1
fi

# Reproducible-tar: stable mtime, deterministic ordering, fixed uid/gid.
# Tarballs built on different machines this way produce identical
# bytes (modulo gzip metadata, which we mute via --no-name).
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-1700000000}"
echo "→ writing $ARCHIVE"
tar --sort=name \
    --mtime="@${SOURCE_DATE_EPOCH}" \
    --numeric-owner --owner=0 --group=0 \
    -C "$STAGE" \
    -cf - . \
  | gzip -9 --no-name > "$ARCHIVE"

SIZE=$(du -h "$ARCHIVE" | cut -f1)
SHA=$(sha256sum "$ARCHIVE" | awk '{print $1}')
echo "$SHA  $(basename "$ARCHIVE")" > "${ARCHIVE}.sha256"

echo
echo "✓ ${ARCHIVE} (${SIZE})"
echo "  sha256: ${SHA}"
echo
echo "Next: place next to FamiliaAdmin-v${VER}.exe (default location:"
echo "dist/admin/). The wizard's source-pack pill auto-discovers it."
echo
echo "Verify pack contents:"
echo "  tar -tzf ${ARCHIVE} | head -40"
