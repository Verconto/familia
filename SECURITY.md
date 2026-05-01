# Reporting security issues

Familia handles a family's most private data — health, finances,
schedules, marital matters, kids' lives. Please report security
problems privately so we can fix them before disclosure.

## How to report

Email **goleff74@gmail.com** with:

- A clear description of the issue.
- Steps to reproduce, ideally against a fresh `docker compose up`
  install.
- The version (image tag and admin `.exe` version) you tested.
- Whether you've shared this with anyone else.

Please do **not** open a public GitHub issue for security problems.
PGP-encrypted email is welcome — a public key will be linked here
once published to github.com.

## Response

This is a hobby project with no SLA. I'll do my best to:

- Acknowledge within a week.
- Triage and confirm reproduction within two weeks.
- Patch and ship within a month for clear vulnerabilities.

Please be patient if a report takes longer — life happens.

## In scope

- ACL bypass: any way to read another principal's `private:` /
  `pair:` scope without an authorizing peer-edge.
- Peer-edge approval bypass: any way to gain peer access without
  admin click-through in the admin app.
- Prompt-injection escalation: a chat message that causes the bot
  to write to a scope it shouldn't or read one it shouldn't.
- Audit-log tampering: any way for a chat actor to suppress or
  alter an entry.
- Credential leak: any way to extract a memX key, the dream
  consolidator key, or `.env` values through the bot.
- Sandbox escape: any way for a tool call to break out of the
  bubblewrap sandbox.

## Out of scope

- Feature requests, performance issues, UI/UX bugs (use regular
  issues for those — though see "no PRs accepted" in the README).
- Self-DoS via legitimate but expensive prompts.
- Anything requiring a malicious admin — the admin is in the trust
  boundary.
- Vulnerabilities in upstream nanobot or memX that aren't reachable
  through familia's code path. Report those upstream:
  - https://github.com/HKUDS/nanobot
  - https://github.com/MehulG/memX

## Known limitations

The admin `.exe` embeds a `familia-source-vX.Y.Z.tar.gz` archive and
extracts it to `%LOCALAPPDATA%\FamiliaAdmin\source\` at startup so it
can be uploaded to the target VM over SFTP. That path inherits the
default per-user ACLs, which on a multi-account Windows machine let
another local account read the extracted tree — exposing the familia
version installed for that operator and the source/comments in the
pack. No secrets live there: credentials, principal data, and OAuth
tokens are kept under `%LOCALAPPDATA%\FamiliaAdmin\keys\` and inside
the admin's connection-profile storage. If this matters to you, run
the admin only on a single-user Windows machine.

## Disclosure

After a fix ships, I'll publish a short note in the release notes
crediting the reporter (with permission) and describing the issue
at a level that lets operators decide whether they need to upgrade
urgently. CVE assignment is best-effort — I'm happy to support an
external request via MITRE / GitHub Security Advisory.

For the full security model see [`docs/security.md`](docs/security.md).
