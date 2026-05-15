# Security model

Familia stores **highly sensitive personal data**: family members'
health, finances, schedules, school plans, marital matters. Readers
include kids. A broken access control here doesn't leak GitHub stars
— it leaks domestic life.

This page is a public summary. The full threat model lives privately
in maintainer notes (it deliberately doesn't enumerate every
attack-vector verbatim in case the doc itself becomes a target).

For how to **report** a vulnerability, see
[`../SECURITY.md`](../SECURITY.md).

## Audience model

- **Owner / parents** — full admin, message anyone, see everything.
- **Other adult principals** (spouse, in-laws if added) — own
  `private:` + `pair:` scopes plus topics they have edges to.
- **Children-as-principals** (when a kid grows up and gets a phone)
  — own `private:` only; cannot read parents' data by default.
  Asymmetric by design.
- **Caregivers / nanny / non-family helpers** (`role: member`,
  added later) — only the topics they have explicit edges to.

## Guarantees

1. **Default-deny** at every layer (policy, memX ACL, graph
   reachability).
2. **Reserved structural keys** — `shared:roles.*`,
   `shared:family.graph*`, `shared:topics.graph*` — are
   write-denied for **all chat actors**, including admins. Edits
   go through the `familia` CLI on the VM.
3. **Tag-write reachable check**: an actor cannot tag a record
   with an id they themselves don't have access to. Prevents
   adversarial planting.
4. **Vocabulary leak prevention**: the LLM's per-turn prompt only
   sees topic names the actor has reach to.
5. **Children-as-principals safe default**: `role: child` blocks
   reverse `parent_of` traversal. Hard-wired in code, not
   graph-configurable.
6. **Confirmation routing**: write confirmations reply only to the
   inbound chat, never broadcast.
7. **Sentinel-based codec**: peer profile stitches carry
   `__familia_acl_v1: true`. Parsers refuse missing/wrong
   sentinel.
8. **Fail-closed parsing**: corrupt graphs → empty reachable set,
   write refused, read returns "no value". Never silently allow.
9. **Audit trail completeness**: every privileged ACL decision
   logs an entry; every CLI graph edit logs `graph_edit`.
10. **Audit log permissions**: `audit.jsonl` is `chmod 0600`.
11. **Admin key hygiene**: CLI reads admin memX key from
    `/etc/familia/admin.key` (mode 0400, owner=root). Env-var
    fallback exists for dev with a loud warning.

## Non-guarantees

We do **not** defend against:

- **Physical / root access to the VM**. The host owner can read
  everything. Use encrypted volumes if this matters.
- **Past data after edge revocation**. Forward-only revocation —
  what was already read cannot be unread.
- **LLM misjudging tags within an actor's reach**. The LLM may
  pick a broader tag set than the user intended; mitigated by the
  confirmation message and audit, not enforced in code.
- **Trusted-but-curious admin**. The audit log records their
  actions for review; nothing prevents misuse beyond that.
- **Children's voiceless consent**. Decisions about a minor's
  data are made by guardians until the minor becomes a principal
  themselves.

## In scope (please report)

- ACL bypass: any way to read another principal's `private:` /
  `pair:` scope without an authorizing edge.
- Peer-edge approval bypass: any way to gain peer access without
  admin click-through.
- Prompt-injection escalation: a chat message that causes the
  bot to write to a scope it shouldn't, or read one it shouldn't,
  by manipulating the LLM.
- Audit-log tampering: any way for a chat actor to suppress or
  alter an entry.
- Credential leak: any way to extract a memX key or `.env` value
  through the bot.
- Sandbox escape: any way for a tool call to break out of the
  bubblewrap sandbox.

## Out of scope

- Feature requests, performance issues, UI bugs.
- Self-DoS through legitimate but expensive prompts.
- Anything requiring a malicious admin (the admin is in the trust
  boundary).
- Vulnerabilities in upstream `nanobot` or `memX` that aren't
  reachable through familia's code path — please report those
  upstream.

## Reporting

Email **goleff74@gmail.com**. Best-effort response window — this
is a hobby project with no SLA. Please don't open public issues
for security problems.

If you want to encrypt: a GPG key will be linked here once
published to github.com.
