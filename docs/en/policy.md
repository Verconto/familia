# Policy & access control

Two layers cooperate to decide what one principal can do to another's
data:

1. **Family graph (`shared:family.graph`)** — declares peer-edges
   like `spouse_of`, `guardian_of`, `parent_of`. Reachability through
   these edges defines who can ask the bot about whom.
2. **`policy.yaml`** — an explicit allow/deny matrix on top of the
   graph. Defaults are deny-all; rules whitelist specific
   (actor-role → target-role → tool/scope) tuples.

Every privileged decision is appended to `audit.jsonl`.

## The family graph

Stored as JSON in memX scope `shared:family.graph`, edited only via
`familia` CLI on the VM (chat actors cannot write it — see
[`security.md`](security.md), guarantee #2).

Example (sanitized):

```json
{
  "principals": {
    "p_a": { "role": "admin" },
    "p_b": { "role": "member" }
  },
  "edges": [
    { "from": "p_a", "to": "p_b", "type": "spouse_of" }
  ]
}
```

Edge types currently honored:

- `spouse_of` — bidirectional, full peer access.
- `guardian_of` — directional, guardian → ward only. Used for parents
  of an adult dependent who needs help managing their data.
- `parent_of` — directional, parent → child only **and** explicitly
  not the reverse, even if both edges exist (asymmetric by design;
  child principals can't auto-introspect their parents).

`role: child` is hard-wired to refuse reverse `parent_of` traversal
regardless of graph state. This prevents misconfiguration from
exposing parents' data to a teen account.

## `policy.yaml`

Lives at `familia/policy.yaml` (template at
`familia/policy.example.yaml`). Read at gateway start; restart to
apply changes (or use the admin app's **Personality** screen which
includes a policy editor + restart).

Skeleton:

```yaml
version: 1
defaults:
  decision: deny

rules:
  - id: spouses-share-memory-read
    when:
      actor.role: member
      target.role: member
      edge: spouse_of
      tool: memory_read
    decision: allow
    log: true

  - id: admin-overrides
    when:
      actor.role: admin
    decision: allow
    log: true

  - id: peer-write-via-buttons-only
    when:
      tool: memory_write
      target.kind: peer
    decision: deny
    reason: "writes to peer require explicit interactive consent (see send_buttons)"
```

Every rule that fires writes one entry to `audit.jsonl` if `log: true`.
The `id` field is required and shows up in the audit log so you can
trace which rule allowed or denied an action.

## Audit log

`audit.jsonl` is one JSON object per line. Always read with `jq` — never
trust line order to be temporally consistent (it's appended at the
moment of decision, but multi-thread writes can interleave). Common
event types:

| Event | When |
|-------|------|
| `policy_decision` | every `allow` or `deny` from `policy.yaml` |
| `tag_acl_decision` | a tag-write or tag-read goes through reachability |
| `graph_edit` | `familia` CLI mutates a graph |
| `peer_edge_proposal` | a principal proposes a peer-edge (admin must approve) |
| `peer_edge_approved` | admin approves it via the admin app |
| `memory_write` | every write to memX (regardless of allow/deny) |
| `dream_consolidate` | nightly consolidator pass |

The admin app's **Audit** screen tails the file with filters; the
**familia-audit** CLI on the VM does the same offline.

Sample entry:

```json
{
  "ts": "2026-04-30T10:12:34Z",
  "event": "policy_decision",
  "actor": "p_a",
  "target": "p_b",
  "tool": "memory_read",
  "rule": "spouses-share-memory-read",
  "decision": "allow"
}
```

## Peer-edge approval flow

When a principal in chat asks "tell me about my partner's schedule"
and there's no `spouse_of` edge yet, the bot:

1. Refuses the read (deny logged with reason `no_peer_edge`).
2. Sends a button-message to that principal: "Propose
   `spouse_of` with `<other>`?"
3. If they tap **yes**, a `peer_edge_proposal` is logged.
4. The admin sees it on the **Pending** screen of the admin app.
5. Admin approves → `peer_edge_approved` logged + edge added to
   `shared:family.graph`.
6. Future reads succeed.

This intentionally keeps the human in the loop — no chat-only path
can grant peer access.

## Children-as-principals

When a child becomes a principal (gets a phone, you add them to
`principals.json`):

1. Set `role: child`.
2. Migrate the topic representing them with
   `familia migrate topic-to-principal <name>` (see
   [`security.md`](security.md), *Migration paths*).
3. Their parents keep `parent_of` edges pointing **to** them. Reverse
   traversal stays blocked. The child can ask about themselves; they
   cannot ask about their parents.

This asymmetry is deliberate: it lets parents see school topics they
already managed, while preventing a teen account from introspecting
its parents' marital topics, work, finances, etc.
