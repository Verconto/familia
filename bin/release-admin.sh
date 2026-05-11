#!/usr/bin/env bash
# Local release script for the admin desktop app.
#
# Usage (from repo root):
#   ./bin/release-admin.sh 0.1.0
#
# Steps:
#   1. Verify version arg matches admin/package.json + admin/src-tauri/Cargo.toml.
#   2. Run pnpm test + cargo test.
#   3. pnpm tauri build (release).
#   4. Copy portable exe to dist/admin/FamiliaAdmin-vX.Y.Z.exe.
#   5. Compute sha256, append to dist/admin/CHANGELOG.md.
#
# Code-signing — TODO once Azure Trusted Signing or OV cert acquired.
# Until then: users get a SmartScreen warning on first run.

set -euo pipefail

if [[ $# -ne 1 ]]; then
    echo "usage: $0 <version>  (e.g., 0.1.0)" >&2
    exit 2
fi
VER="$1"

cd "$(dirname "$0")/.."

if [[ ! -d admin ]]; then
    echo "error: admin/ missing — sources are gitignored, restore from your local backup" >&2
    exit 1
fi

# Verify version sync.
pkg_ver=$(node -p "require('./admin/package.json').version")
cargo_ver=$(grep -E '^version = "' admin/src-tauri/Cargo.toml | head -1 | cut -d'"' -f2)
tauri_ver=$(node -p "require('./admin/src-tauri/tauri.conf.json').version")
if [[ "$pkg_ver" != "$VER" || "$cargo_ver" != "$VER" || "$tauri_ver" != "$VER" ]]; then
    echo "error: version mismatch — package.json=$pkg_ver, Cargo.toml=$cargo_ver, tauri.conf.json=$tauri_ver, requested=$VER" >&2
    echo "bump them all to $VER and re-run" >&2
    exit 1
fi

# Backend-version bump check. Rule (per feedback_backend_version_bump
# in auto-memory): familia/pyproject.toml's version moves only on
# real backend changes (nanobot/, familia/src/, memx/, Dockerfile,
# bootstrap.sh — the source-pack contents). Admin-only releases
# (admin/, docs/, tests, tooling) keep the same backend version.
#
# Heuristic CI guard: if any backend-relevant path changed since the
# last admin-release tag AND familia/pyproject.toml's version is the
# same as on that tag, refuse to release. The operator either bumps
# pyproject (real backend change) or moves the offending edits out
# of the backend paths (mistakenly classified).
#
# Bypass: ``ALLOW_STALE_BACKEND_VERSION=1`` env var, for the rare
# case where the change is genuinely no-op for the running gateway
# (e.g. comment-only edits in nanobot/).
if [[ -z "${ALLOW_STALE_BACKEND_VERSION:-}" ]]; then
    BACKEND_PATHS=(nanobot familia/src familia/pyproject.toml memx \
                   Dockerfile docker-compose.yml admin/src-tauri/resources/bootstrap.sh)
    backend_ver=$(grep -m1 -E '^version[[:space:]]*=' familia/pyproject.toml \
                  | sed -E 's/^version[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/')
    # Most recent admin-release tag (vX.Y.Z) — skip the check on a
    # repo with no tags yet (first release).
    last_tag=$(git tag -l 'v*' --sort=-v:refname | head -1 || true)
    if [[ -n "$last_tag" ]]; then
        # Backend pyproject version on the previous tag.
        prev_backend_ver=$(git show "$last_tag:familia/pyproject.toml" 2>/dev/null \
                           | grep -m1 -E '^version[[:space:]]*=' \
                           | sed -E 's/^version[[:space:]]*=[[:space:]]*"([^"]+)".*/\1/' || true)
        # Did any backend path change since then?
        backend_diff=$(git diff --name-only "$last_tag"..HEAD -- "${BACKEND_PATHS[@]}" 2>/dev/null || true)
        if [[ -n "$backend_diff" && "$backend_ver" == "$prev_backend_ver" ]]; then
            echo "error: backend files changed since $last_tag but familia/pyproject.toml version stayed at $backend_ver" >&2
            echo "       changed paths:" >&2
            echo "$backend_diff" | sed 's/^/         /' >&2
            echo "       bump familia/pyproject.toml or set ALLOW_STALE_BACKEND_VERSION=1 to bypass" >&2
            exit 1
        fi
    fi
fi

echo "→ tests (vitest only — cargo test fails on Win with STATUS_ENTRYPOINT_NOT_FOUND because WebView2Loader.dll isn't on the test exe's search path; the dll is loaded at runtime by bootstrap_webview2_loader, but the test harness exe doesn't run that path)"
(cd admin && pnpm test --run)

# Build source-pack and place it where ``include_bytes!`` will pick it
# up at compile time. The .exe ships with this archive embedded; at
# runtime ``bootstrap_source_pack`` extracts it to %LOCALAPPDATA%, and
# ``install_run`` SFTPs it to the target VM. Filename is fixed
# (``familia-source.tar.gz``) — the version-stamped name is created
# only at runtime extraction, so cargo doesn't need to know the version.
echo "→ source-pack (embedded in .exe)"
SRC_PACK_DIR="admin/src-tauri/resources"
SRC_PACK="$SRC_PACK_DIR/familia-source.tar.gz"
mkdir -p "$SRC_PACK_DIR"
# build-source-pack writes ``familia-source-v<VER>.tar.gz`` to the
# given output dir; we then rename to the fixed name include_bytes!
# expects. tmp dir keeps the build-source-pack output predictable.
SRC_PACK_TMP=$(mktemp -d)
bash bin/build-source-pack.sh "$VER" "$SRC_PACK_TMP"
mv -f "$SRC_PACK_TMP/familia-source-v${VER}.tar.gz" "$SRC_PACK"
rm -rf "$SRC_PACK_TMP"
echo "  embedded: $SRC_PACK ($(du -h "$SRC_PACK" | cut -f1))"

# SHA256 of the source-pack — build.rs reads this and bakes it into
# ``FAMILIA_SOURCE_PACK_SHA256`` so the orchestrator on the VM can
# verify either the GitHub-fetched copy or the SSH-fallback upload
# matches the bytes the .exe was built with.
SRC_PACK_SHA=$(sha256sum "$SRC_PACK" | awk '{print $1}')
echo "$SRC_PACK_SHA  $(basename "$SRC_PACK")" > "$SRC_PACK.sha256"
echo "  source-pack sha256: $SRC_PACK_SHA"

echo "→ tauri build"
(cd admin && pnpm tauri build)

OUT="dist/admin/FamiliaAdmin-v${VER}.exe"
SRC="admin/src-tauri/target/release/familia-admin.exe"
if [[ ! -f "$SRC" ]]; then
    echo "error: build artefact not found at $SRC" >&2
    exit 1
fi
mkdir -p dist/admin
cp -f "$SRC" "$OUT"

# Ship WebView2Loader.dll alongside the exe. The .exe imports the DLL as
# a non-delay-load symbol, so Windows resolves it BEFORE main() runs —
# our runtime bootstrap_webview2_loader() in lib.rs only kicks in after
# imports are already pinned. Without the DLL next to the .exe (or
# installed system-wide), the user gets a loader error and the window
# never appears. The NSIS installer Tauri builds at
# admin/src-tauri/target/release/bundle/nsis/ already handles this; the
# portable single-exe path doesn't, so we copy the DLL ourselves.
DLL_SRC="admin/src-tauri/WebView2Loader.dll"
DLL_OUT="dist/admin/WebView2Loader.dll"
if [[ ! -f "$DLL_SRC" ]]; then
    echo "error: WebView2Loader.dll not found at $DLL_SRC" >&2
    exit 1
fi
cp -f "$DLL_SRC" "$DLL_OUT"

echo "→ sha256"
SHA=$(sha256sum "$OUT" | awk '{print $1}')
echo "$SHA  FamiliaAdmin-v${VER}.exe" > "$OUT.sha256"
DLL_SHA=$(sha256sum "$DLL_OUT" | awk '{print $1}')
echo "$DLL_SHA  WebView2Loader.dll" > "$DLL_OUT.sha256"

# Versioned source-pack copy for ``gh release upload``. The orchestrator
# on the VM tries this exact URL first (constructed from the admin's
# CARGO_PKG_VERSION) and falls back to an SSH upload of the embedded
# copy on failure. SHA must match what's baked into the .exe.
SRC_PACK_VERSIONED="dist/admin/familia-source-v${VER}.tar.gz"
cp -f "$SRC_PACK" "$SRC_PACK_VERSIONED"
cp -f "$SRC_PACK.sha256" "$SRC_PACK_VERSIONED.sha256"

echo "→ changelog"
cat >> admin/CHANGELOG.md <<EOF

## v${VER} — $(date -u +%Y-%m-%d)

- (manually edit this stanza; auto-generated by release-admin.sh)
- SHA256: \`${SHA}\`
- Built from commit: \`$(git rev-parse --short HEAD)\`
EOF

# Per-version release-notes file. The admin's UpdateAvailableDialog
# fetches GitHub Releases API and renders ``body`` to help the user
# decide whether to upgrade — so the body has to actually exist on
# the release. Seed it with a placeholder; the human edits before
# pushing the release. Skipped if a hand-edited file is already there.
NOTES="admin/release-notes/release-notes-v${VER}.md"
mkdir -p admin/release-notes
if [[ ! -f "$NOTES" ]]; then
    cat > "$NOTES" <<EOF
## Что нового в v${VER}

- (опиши изменения, которые увидит пользователь — это покажется в
  «Доступна новая версия» в админке)

---

SHA256 \`FamiliaAdmin-v${VER}.exe\`: \`${SHA}\`
Built from commit: \`$(git rev-parse --short HEAD)\`
EOF
    echo "→ release notes stub: $NOTES (edit before publishing)"
fi

echo "✓ release v${VER} ready in dist/admin/"
echo
echo "Next:"
echo "  1. Edit  $NOTES  — this becomes the GitHub release body and"
echo "     what UpdateAvailableDialog shows to the user."
echo "  2. Publish:"
echo "       gh release create v${VER} \\"
echo "         dist/admin/FamiliaAdmin-v${VER}.exe \\"
echo "         dist/admin/WebView2Loader.dll \\"
echo "         dist/admin/familia-source-v${VER}.tar.gz \\"
echo "         dist/admin/familia-source-v${VER}.tar.gz.sha256 \\"
echo "         --title v${VER} \\"
echo "         --notes-file $NOTES"
