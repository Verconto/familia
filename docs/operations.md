# Operations

How to run familia day-to-day: backups, restores, log/disk hygiene,
key rotation, troubleshooting.

## Backup

Through the admin app:

- **Maintenance** → **Backup** → choose destination folder.
- Produces `familia-backup-<host>-<timestamp>.tar.gz` containing
  `principals.json`, `policy.yaml`, `memx-config/acl.json`, the
  full memX volume contents, and `audit.jsonl`.
- The backup includes secrets — store it as carefully as `.env`.

Manual:

```bash
# On the VM:
cd /opt/familia
docker compose down               # quiesce writers
tar czf familia-backup-$(date +%F).tar.gz \
    principals.json policy.yaml \
    memx-config/acl.json .env audit.jsonl \
    workspace/    # docker volume mount path; depends on your driver
docker compose up -d
```

Backups happen offline (containers down) intentionally. Live snapshots
of memX/Redis can race with active writes; this project doesn't try
to support consistent live backups.

## Restore (universal)

Restore is built to be **independent of the source VM** — UID/GID,
volume names and paths on the source don't matter.

Through the admin app:

- **Install** → **Restore from backup** → pick the tarball and a
  fresh target VM.
- The app uploads, untars into a temp dir, resolves real volume
  paths via `docker inspect`, and chowns content to the target
  container's UID. It never assumes the source VM was identical.

Manual:

```bash
# On a fresh VM with familia stack pulled but stopped:
cd /opt/familia
docker compose down
tar xzf familia-backup-*.tar.gz -C ./
# Adjust ownership inside docker volumes:
docker run --rm -v memx_data:/data alpine chown -R 1000:1000 /data
docker compose up -d
```

## Logs

Container logs are capped via docker `json-file` driver
(20 MB × 5 rotations) — see `docker-compose.yml`. To inspect:

```bash
docker compose logs -f familia-gateway
docker compose logs -f memx-backend
docker compose logs -f memx-redis
```

The audit log (`audit.jsonl`) is application-rotated at 50 MB × 5.

## Disk hygiene

Familia auto-cleans:

- `media/` (downloaded chat attachments) — TTL 24 h, runs hourly.
- `workspace/sessions/*.jsonl` (per-session conversation logs) —
  TTL 90 days, runs daily.
- `workspace/.git` (nanobot internal git store) — `git gc` monthly.
- `audit.jsonl` — rotated at 50 MB, keeps 5 generations.

Disk usage shows on **Maintenance** in the admin: free / total VM disk
plus per-category bytes.

## Key rotation

### memX per-actor key

When a principal's memX key needs rotation (suspected leak, device
change):

```bash
# 1. Generate new key:
openssl rand -hex 32 > /tmp/newkey

# 2. Edit both files atomically:
cd /opt/familia
NEW=$(cat /tmp/newkey)
# - update principals.json: principals.<id>.memx_key = $NEW
# - update memx-config/acl.json: replace OLD key with NEW key
# - keep all scope grants

# 3. Restart memX (re-reads acl.json) and gateway (re-reads principals.json):
docker compose -f docker-compose.memx.yml restart memx-backend
docker compose restart familia-gateway

# 4. Verify the principal can still read their data:
docker compose logs --tail=50 familia-gateway | grep -i actor
```

### Dream consolidator key

Rotation needs the same dance plus an extra step: the dream
consolidator key is named separately in `acl.json` (not bound to a
principal). Its leak is the most catastrophic — a holder can
overwrite any principal's private memory. Treat it as a root-equivalent
secret.

## Bring up / down

```bash
# Stop everything:
cd /opt/familia
docker compose down
docker compose -f docker-compose.memx.yml down

# Start in correct order (memX first):
docker compose -f docker-compose.memx.yml up -d
docker compose up -d
```

## Diagnostics

The admin app's **Diagnostics** page runs the equivalent of:

```bash
# Identity binding test:
docker exec familia-gateway python -c \
  "from familia.identity_resolver import resolve; \
   print(resolve('telegram', 12345))"

# memX reachability:
docker exec familia-gateway curl -s \
  -H "X-API-Key: $(grep '^FAMILIA_OWNER_ACTOR' .env | cut -d= -f2)" \
  http://memx-backend:8100/get?key=shared:family.graph

# Audit tail:
tail -n 50 /opt/familia/audit.jsonl
```

If audit is silent during a real chat — the principal binding is
broken. Check `principals.json` `channel_id`/`sender_id` against
the message metadata in container logs.

## Common problems

| Symptom | Likely cause |
|---------|--------------|
| `unknown principal: telegram/12345` | Telegram chat not bound to a principal in `principals.json` |
| Replies are generic, ignore your context | LLM key wrong / quota; `OPENAI_API_KEY` env not loaded into gateway |
| `acl deny: scope=private:X:value:Y` | Cross-principal read without peer-edge — by design, see [`policy.md`](policy.md) |
| memX returns 401 | `acl.json` and `principals.json` out of sync (different memX keys) |
| Telegram bot silent | webhook URL not set, or container can't reach `api.telegram.org` |
| VK media missing | VK CDN blocks non-RU IPs; set `VK_PROXY` in `.env` to a SOCKS5 you trust |

## Mirror fallbacks for restricted-egress hosts

Bootstrap auto-probes the standard upstreams on startup
(`pypi.org`, `deb.debian.org`, `registry.npmjs.org`,
`get.docker.com`, `registry-1.docker.io`). When any of them isn't
reachable from the target VM, it picks the first reachable mirror
from a baked-in fallback list — no operator action required. The
auto-selected mirror is logged as `+ APT_MIRROR (auto): <url>` so you
can see what was chosen.

The baked-in lists (intentionally short — one well-trusted mirror per
region):

| Resource | Auto-fallback chain |
|----------|---------------------|
| PyPI | `pypi.tuna.tsinghua.edu.cn`, `mirrors.aliyun.com/pypi` |
| Debian apt | `mirror.yandex.ru/debian`, `mirrors.tuna.tsinghua.edu.cn/debian` |
| npm | `registry.npmmirror.com`, `mirrors.huaweicloud.com/repository/npm` |
| Docker Hub | `mirror.gcr.io`, `dockerhub.timeweb.cloud` (written to `/etc/docker/daemon.json`) |
| Docker engine install | falls through to `apt install docker.io` when `get.docker.com` is blocked |

To **override** the auto-pick (e.g. you have a corporate-internal
mirror you'd rather use), set any of these env vars before bootstrap
runs and they win unconditionally:

| Env var | What it overrides | Example value |
|---------|-------------------|---------------|
| `APT_MIRROR` | `deb.debian.org` / `security.debian.org` inside the image | `https://mirror.yandex.ru/debian` |
| `PIP_INDEX_URL` | PyPI index used by `pip` and `uv` inside the image | `https://pypi.tuna.tsinghua.edu.cn/simple` (or a self-hosted devpi) |
| `NPM_REGISTRY` | npm registry used when building the WhatsApp bridge | `https://registry.npmmirror.com` |
| `DOCKER_INSTALL_METHOD` | How bootstrap installs docker on a host that doesn't have it | `auto` (default — try `get.docker.com`, fall back to `apt install docker.io`), `apt`, `get.docker.com` |

For the **base images themselves** (`ghcr.io/astral-sh/uv:...` and
`python:3.12-slim`), the Dockerfiles pin `FROM` by digest, so the
only knob is the daemon-level registry mirror. Bootstrap writes a
minimal `/etc/docker/daemon.json` with `registry-mirrors` automatically
when Docker Hub probe fails — but only if the file doesn't already
declare its own mirror. To pre-empt the auto-pick with a corporate
mirror, drop the daemon.json yourself before running install:

```jsonc
// /etc/docker/daemon.json
{
  "registry-mirrors": ["https://your.mirror.example.com"]
}
```

For headless installs (no admin app), bootstrap reads the same env
vars from its environment:

```bash
export APT_MIRROR=https://your-corp-mirror/debian
export PIP_INDEX_URL=https://your-corp-mirror/pypi/simple
sudo -E bash bootstrap.sh
```

## Reproducible Python builds (`requirements.lock`)

Direct deps in `familia/pyproject.toml` are version-capped at the next
major (`httpx>=0.27,<1.0` etc) so a surprise breaking release can't
land on a Friday `pip install`. Transitive deps are also frozen — but
via a separate lock file, `familia/requirements.lock`, regenerated on
demand:

```bash
bin/regen-lock.sh
```

The script runs `uv pip compile` inside the same digest-pinned base
image the production Dockerfile uses, so the lock matches what will
actually install at build time. Re-run after bumping any direct dep,
or periodically to absorb security fixes in transitive deps. When the
lock file is absent, the Dockerfile falls back to range-resolving from
the pyprojects (still major-version-capped, but transitive drift is
possible).
