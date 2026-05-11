# Architecture

Familia is a fork of [nanobot](https://github.com/HKUDS/nanobot) plus a
vendored copy of [memX](https://github.com/MehulG/memX), wrapped with a
small `familia` package that wires identity, policy and ACL into the
nanobot agent loop.

## Components

### `familia/` (this repo)

The thin layer that turns nanobot from a single-user agent into a
multi-principal one.

```
familia/src/familia/
  identity_resolver.py   binds inbound messages to principal_id
  principals.py          loads/validates principals.json
  acl.py                 family.graph + topics.graph reachability
  policy/                policy.yaml engine (audit + decisions)
  tools/                 memory_*, send_buttons, family_graph,
                         dream_memory_* (consolidator only),
                         and admin-grant tooling
  bootstrap.py           single insertion point into nanobot.agent.loop
  audit.py               append-only JSONL log + 50 MB × 5 rotation
  cli/                   familia CLI (graph_admin, audit_view)
```

### `nanobot/` (forked subtree)

The upstream agent loop. We patch a handful of files to hand control to
the `familia` layer at well-known points (channel ingress, prompt
build, tool dispatch, memory write). Patches live in `patches/`.

### `memx/` (vendored subtree)

FastAPI + Redis store with per-actor ACL. Each principal has a unique
API key (`memx_key`); requests are authenticated by `X-API-Key`. The
ACL file (`memx-config/acl.json`) maps each key to allowed scopes.

We use three scope shapes:

- `shared:<key>` — visible to every actor (e.g. `shared:family.graph`).
- `private:<P>:<key>` — only principal `P` can read/write
  (e.g. `private:owner:value:user_profile`).
- `pair:<A>:<B>:<key>` — both `A` and `B` (e.g. spouses).

There's also a special **dream consolidator** actor with full
write access across all scopes; it runs at night to digest history
into per-scope memory. Its key is the single most sensitive secret
in the system — see [`security.md`](security.md).

## Data flow (inbound message)

```text
1. Telegram / VK delivers a message to the channel adapter.
2. nanobot calls familia.identity_resolver.resolve(channel, sender_id).
3. We look up principals.json → principal_id, role.
4. set_current_actor(principal_id) is pinned for the rest of the turn.
5. The agent loop builds a prompt:
     - shared SOUL/AGENTS/TOOLS files (read-only, same for everyone)
     - that principal's USER profile from memX
     - their MEMORY and HEARTBEAT entries
     - a sanitized stitch of peer profiles they have ACL reach to
       (4 KiB cap, sentinel-wrapped, see acl.py)
6. LLM produces a response and tool calls. Each tool call is
   re-checked: e.g. memory_write to a foreign principal goes through
   policy.yaml + family.graph reachability.
7. policy.yaml decisions are appended to audit.jsonl with the
   actor, target scope, decision and reason.
8. Reply goes back through the same channel, same chat. No broadcast.
```

## Storage layout

Two storage planes that intentionally don't mix:

| Plane | Purpose | Keyed by |
|-------|---------|----------|
| **Hybrid memX** (per principal) | identity-bound facts: USER profile, MEMORY entries, HEARTBEAT | `principal_id` |
| **Shared files** (workspace) | bot's persona / tool docs / agent docs | none — single shared truth |

This split is the security invariant: a principal can never write to
shared files through the chat path (only through the `familia` CLI
on the VM), and a principal can never read another's `private:*:*`
unless an explicit peer-edge grants it.

## Trust boundaries

```text
                    ┌─────────────────┐
                    │ Operator laptop │  (admin .exe + WebView2Loader.dll)
                    └────────┬────────┘
                             │ SSH (port 22, key auth)
   ┌─────────────────────────┴───────────────────────────────────┐
   │  VM (Linux, root SSH only)                                  │
   │  ┌───────────────────────────────────────────────────────┐  │
   │  │ docker network: familia_default                       │  │
   │  │   familia-gateway   ──▶  memx-backend ──▶ memx-redis │  │
   │  └───────────────────────────────────────────────────────┘  │
   │  /opt/familia/{principals.json, policy.yaml, acl.json,      │
   │                .env, audit.jsonl}                           │
   └─────────────────────────────────────────────────────────────┘
                ▲                                ▲
                │                                │
   ┌────────────┴──────────┐         ┌───────────┴────────────┐
   │ Telegram channel API  │         │ LLM provider           │
   │ VK long-poll API      │         │ (OpenAI/Claude/Groq/…) │
   └───────────────────────┘         └────────────────────────┘
```

- The operator's laptop is **trusted**: it holds the SSH key.
- The VM is **trusted**: root on the host can read everything anyway.
- Telegram/VK channels are **outside the trust boundary** — they see
  inbound and outbound messages plain.
- The LLM provider is **outside the trust boundary** — it sees the
  per-turn prompt (including the actor's stitched peer context).

For the full threat model see [`security.md`](security.md).

## Why three compose stacks (and why they live together)

`docker-compose.yml`, `docker-compose.memx.yml` and
`docker-compose.cli.yml` are separate so you can stop, restart,
backup or rebuild each plane independently. In practice all three
run on the same VM and share the same docker network — the admin
app drives them as one unit.

There's also `docker-compose.exec-sandbox.yml` for nanobot's
bubblewrap sandbox dependencies (kept separate because changing
sandbox config shouldn't restart the gateway).
