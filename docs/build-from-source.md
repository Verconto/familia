# Build from source

This document is for a **developer or SRE** who wants to:

- build `FamiliaAdmin-vX.Y.Z.exe` locally from source;
- run the stack manually on a VM through `docker compose`, without the admin app;
- understand the source-pack architecture and update flow for debugging.

If you are an end user and just want to bring familia up in 5 minutes, see [`quickstart.md`](quickstart.md).

## Repository layout

```text
family-assistant/
├── nanobot/        — vendored fork of nanobot (agent runtime, channels, MCP)
├── familia/        — our layer: principals, policy, identity_resolver, tools
├── memx/           — vendored memX (memory backend, Redis-backed)
├── bin/            — release tooling (regen-lock, build-source-pack, release-admin)
├── docs/           — this document and neighbors
├── docker-compose.yml          — gateway + redis + ingress
├── docker-compose.memx.yml     — memX backend (separate compose project)
├── Dockerfile                  — multi-stage build for the gateway image
├── admin/src-tauri/resources/bootstrap.sh
│                               — install/update on the VM (run by the admin app)
└── principals.example.json     — starting template for a new deployment
```

> **Note about `admin/`.** Tauri/React admin app sources are **not published in this repository**. Only built artifacts are published: `.exe` + `WebView2Loader.dll` in [Releases][rel]. This is intentional. Code signing for unsigned hobby-project binaries is not available, and maintaining both a public frontend codebase and the backend is outside the author's budget. If you need this, fork the backend and write your own client. The IPC contract is described in `nanobot/nanobot/cli/commands.py` (`familia rpc-server`) and `bin/build-source-pack.sh`.
>
> [rel]: https://github.com/Verconto/familia/releases/latest

## What this repository builds

It builds what runs **inside the `gateway` container**: Python packages `nanobot`, `familia`, and `memx`. The build happens on the target VM through `docker compose build`. It is either launched by the admin app as part of the update flow, or manually from `git clone` on the VM; see [Manual install on a VM](#manual-install-on-a-vm-no-admin-exe).

`bin/build-source-pack.sh` creates the source-pack tarball (`nanobot/`, `familia/src/`, `memx/`, `Dockerfile`, compose YAML files, `bootstrap.sh`). The admin app embeds this pack into its `.exe` with `include_bytes!` at build time. If you fork the project and write your own client, this is what your client must deliver to the VM.

### CI guard: `ALLOW_STALE_BACKEND_VERSION`

`bin/release-admin.sh` runs on the author's release machine. In the public repository its visible effect is the backend-version bump. It refuses to cut a release if anything changed in `nanobot/`, `familia/src/`, `memx/`, `Dockerfile`, or `bootstrap.sh` since the previous tag, but `familia/pyproject.toml::version` stayed the same. This prevents the failure mode "shipped admin with a new backend, forgot to bump backend semver, update flow thinks there is nothing to update".

Bypass: `ALLOW_STALE_BACKEND_VERSION=1 bin/release-admin.sh ...`.

## Manual install on a VM (no admin .exe)

If you do not want to use the admin app (CI, headless, audit), install manually on the VM:

```bash
git clone https://github.com/<owner>/family-assistant.git
cd family-assistant

# 1. Configs:
cp principals.example.json ~/.nanobot/principals.json
cp memx-config/acl.example.json memx-config/acl.json
cp .env.example .env
cp familia/policy.example.yaml familia/policy.yaml

# 2. Generate a unique 64-hex memx_key for each principal.
#    Replace <replace_with_unique_key> in both files
#    (principals.json and acl.json); they must match.
openssl rand -hex 32

# 3. Bring memX up first; gateway depends on it:
docker compose -f docker-compose.memx.yml up -d --build

# 4. Then gateway:
docker compose up -d --build

# 5. Smoke test:
docker compose logs -f familia-gateway
```

Sanity checks:

```bash
# memX answers from inside the gateway container:
docker exec familia-gateway curl -s \
  -H "X-API-Key: <owner_memx_key>" \
  http://memx-backend:8100/get?key=shared:test
# Expected: 404 (key is absent), not connection refused.

# audit log is written:
tail -f /opt/familia/audit.jsonl
```

Send `/start` to the bot in Telegram/VK. It should return a greeting with your principal id.

## Backend bump rule

`familia/pyproject.toml::version` is the **backend** semver, separate from the admin `.exe` release version. The update flow reads it on connect and compares it with the version embedded in the `.exe`.

**Bump it for every change** in:

- `nanobot/`
- `familia/src/`
- `memx/`
- `Dockerfile`
- `bootstrap.sh`

**Do not bump it** for admin-only changes: frontend, locales, Tauri, admin tests, docs.

`bin/release-admin.sh` enforces this. If the backend changed but pyproject did not move, release fails. Bypass: `ALLOW_STALE_BACKEND_VERSION=1`; see above.

## Regenerating `requirements.lock`

Direct dependencies in `familia/pyproject.toml` and `nanobot/pyproject.toml` are pinned by ranges (`httpx>=0.27,<1.0`). Transitive dependencies are frozen through `familia/requirements.lock`, regenerated with:

```bash
bin/regen-lock.sh
```

The script mounts the current pyproject files into **the same digest-pinned base image** used by the production Dockerfile, runs `uv pip compile --generate-hashes`, and rewrites `familia/requirements.lock`. This guarantees the lock matches the wheels installed at build time.

Regenerate after:

- raising any direct dependency range;
- periodically, every month or two, to pick up security fixes in transitive dependencies.

When the lock file is absent, Dockerfile falls back to range resolution from pyprojects. Direct dependencies remain inside major-version bounds, but transitive drift becomes possible. Production builds should ship with a current lock.

## Source-pack architecture

Previously the admin app pulled `ghcr.io/<owner>/familia-assistant:X.Y.Z` to the VM. That path is gone: the image is now built directly on the VM from the tarball embedded in the `.exe`. This gives:

- full control over release contents, with no external registry;
- reproducible builds: everything required for the build sits next to the `.exe`;
- simple recovery: tarball + `bootstrap.sh` are enough to bring the stack up from scratch.

### Build time (`bin/build-source-pack.sh`)

Packs a deterministic `tar.gz`: sorted entries, fixed mtime.

Included:

- `nanobot/{nanobot,bridge,pyproject.toml,...}`
- `familia/{src,pyproject.toml,policy.example.yaml,requirements.lock}`
- `memx/{src,Dockerfile,...}`
- `Dockerfile`, `docker-compose.yml`, `docker-compose.memx.yml`, `principals.example.json`, scripts from `bin/`.

The result is a ~3.6 MB tarball placed into Tauri project resources outside this public repository. Tauri embeds it into the final `.exe` through `include_bytes!` during `cargo build`. If you fork and write your own client, either embed the pack the same way or download it as a separate file during install and pass the path.

### Runtime extraction

On first `.exe` launch, `bootstrap_source_pack()` writes the tarball to `%LOCALAPPDATA%\FamiliaAdmin\source\familia-source.tar.gz`. It writes once; later launches verify SHA and skip writing when unchanged.

### Install / update flow

When the user clicks **Install** or **Update VM**, the admin app:

1. uploads `familia-source.tar.gz` over SFTP to `/opt/familia/source.tar.gz`;
2. uploads embedded `bootstrap.sh` over SFTP to `/tmp/bootstrap.sh`;
3. runs `bash /tmp/bootstrap.sh MODE=install` or `MODE=update` over SSH;
4. bootstrap extracts the tarball into `/opt/familia/source/`, then `docker compose build` builds the image directly on the VM.

## Update flow in detail

`bootstrap.sh MODE=update` differs from `MODE=install`:

- skips `dirs` (directories already exist) and `seed_graph` (family graph already exists);
- keeps `prereqs`, `docker`, and `probe_mirrors` in case the VM changed since the previous deploy or new mirror requirements appeared;
- `compose up -d --force-recreate` guarantees containers are recreated for the new image.

### Atomic `SOURCE_VERSION`

`/opt/familia/SOURCE_VERSION` contains backend semver and is written **only after** `wait_healthy`, meaning after the gateway container passes healthcheck. If update fails halfway, the file keeps the old value, and on next connect admin truthfully shows that the VM still runs the old version and update must be repeated.

### Where the VM version is read from

Backend version for comparison with admin is read **from the live container**:

```bash
docker exec familia-gateway python3 -c \
  'import importlib.metadata; print(importlib.metadata.version("familia"))'
```

Not from disk. This is critical: if update partly succeeded (new files on disk, container not rebuilt), the container version remains old and admin sees it.

## SIGHUP hot-reload contract

Most config mutations (add/remove channel, approve pending principal, set STT provider) **do not restart the container**. Instead:

1. `nanobot.cli.commands::_run_gateway` registers `loop.add_signal_handler(signal.SIGHUP, _on_reload)` at startup.
2. `_on_reload` calls:
   - `familia.principals.reload_registry()` to reread `principals.json` from disk and update the in-memory registry;
   - `ChannelManager.reload_from_disk(new_config)` to diff `config.json` against current channel instances, add new ones, remove deleted ones, and reinitialize changed ones.
3. The whole path is serialized through `asyncio.Lock` with **one-deep coalescing**. If SIGHUP arrives during an active reload, it becomes exactly one follow-up run: an idempotent reread from disk. This dampens signal storms.

### Admin app side

`signal_gateway_reload` in Rust runs:

```bash
docker kill --signal=HUP familia-gateway
```

If the signal path fails (container dead, Docker unavailable), fallback is `restart_gateway_quiet`: full `docker restart familia-gateway`. Both paths log through `tracing::info!` / `tracing::warn!`, visible in **Diagnostics** in the admin app.

**Time budget**: SIGHUP reload ~120 ms; full restart ~30 s.

## Mirror fallbacks: quick reference

Detailed description: [`operations.md`](operations.md), *Mirror fallbacks*. Short list of environment variables read by `bootstrap.sh`:

| Env var | Overrides |
|---|---|
| `APT_MIRROR` | `deb.debian.org` (apt inside the image) |
| `PIP_INDEX_URL` | PyPI for `pip` / `uv` inside the image |
| `NPM_REGISTRY` | npm for building the WhatsApp bridge |
| `DOCKER_INSTALL_METHOD` | how Docker is installed: `auto` (default), `apt`, `get.docker.com` |

If none is set and upstream is unreachable (5-second probe), `bootstrap.sh` picks a mirror from the baked-in list (Tsinghua / Yandex / aliyun / mirror.gcr.io / dockerhub.timeweb.cloud) and logs `+ APT_MIRROR (auto): <url>`.

## Where to dig further

- [`quickstart.md`](quickstart.md) — user installation path.
- [`operations.md`](operations.md) — backup/restore, diagnostics, key rotation, mirror fallbacks.
- [`architecture.md`](architecture.md) — why gateway/memX/policy are arranged this way.
- [`policy.md`](policy.md) — privilege/ACL model and peer-edge.
- [`security.md`](security.md) — threat model and what counts as a privileged operation.
- [`release.md`](release.md) — release pipeline for admin and backend.
