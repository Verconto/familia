# Release flow

Two independent SemVer tracks, coupled by a compose-template constant.

## Tracks

| Track | What changes trigger a bump | Tag prefix | Where it ships |
|-------|------------------------------|-----------|-----------------|
| **Image** | `familia/`, `nanobot/`, `memx/`, `Dockerfile` | `image-vX.Y.Z` | `ghcr.io/<owner>/familia-assistant:X.Y.Z`, `ghcr.io/<owner>/memx:X.Y.Z` |
| **Admin** | UI / IPC / install wizard | `admin-vX.Y.Z` | GitHub Release: `FamiliaAdmin-vX.Y.Z.exe` + `WebView2Loader.dll` (+ both `.sha256`) |

Each admin release embeds a build-time constant `IMAGE_TAG = X.Y.Z`
which becomes the default in the compose template the admin writes
to the VM during install. Operators can override per-VM, but the
default is whatever this admin build was tested against.

## Why two tracks

Most weeks the UI changes and the backend doesn't. Decoupling avoids
1 GB image rebuilds for a button-padding fix and lets us ship admin
patches in minutes.

When the backend changes in a way the admin needs to know about
(new tool, new IPC, new compose stanza), both bump in the same
push: `image-vX.Y.Z` *and* `admin-vA.B.C` together; the new admin
embeds the new image tag.

## Tag → CI flow

```
git commit ...
git tag image-v0.5.0           # only when backend changed
git tag admin-v0.5.35          # for the corresponding admin
git push --tags
```

CI (when configured):

- `image-v*` → builds and pushes `familia-assistant:0.5.0` and
  `memx:0.5.0`. Also tags `:latest`.
- `admin-v*` → builds the `.exe` (Windows runner) and uploads it
  to a draft GitHub Release.

Until the GitHub-side automation lands, both can be done manually:
`docker buildx build --push` for images and
`bin/release-admin.sh` for the admin.

## Update path on a running VM

`Maintenance` → **Pull image** runs:

```bash
cd /opt/familia
# admin rewrites docker-compose.yml with the new pinned tag
docker compose pull
docker compose up -d
```

This is delta-only (~30 MB typically), takes 1–3 minutes. The
operator can stay on an older image if they like — admin pins
SemVer, never `latest`, so their stack won't drift unexpectedly.

## Compatibility

Each release of admin declares a minimum image version
(`min_image_version` constant, currently `0.4.0`). The gateway's
`/health` returns `image_version`. On connect the admin compares;
if mismatch, the **Compatibility** banner offers an inline
"upgrade image" button.

We support the rolling window "admin v1.x ↔ image ≥ 0.5.0".
Bumping the lower bound only happens on admin major bumps.

## Security of the supply chain

- Images are pulled from `ghcr.io` over HTTPS.
- Admin `.exe` is unsigned (no code-signing certificate). The plan
  is to keep it unsigned indefinitely — it's a hobby project and
  the EV cert overhead isn't justifiable. SmartScreen warns; users
  click through.
- Each `.exe` is published with a `.sha256` next to it. We don't
  use sigstore yet; if you mirror the artifact, mirror the hash too.
- The release commit is signed with the maintainer's GPG key
  (will be published in `SECURITY.md` once the public key is
  registered on github.com).

Forks should swap in their own keys / certs as appropriate.
