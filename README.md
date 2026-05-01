# familia

Family AI assistant with separate memory for every member. Lives in Telegram
and VK, remembers each person on their own, and doesn't mix up husband, wife,
or kids.

<!-- TODO: screenshot of admin app -->

Repo: <https://github.com/Verconto/familia>. Latest release — `v0.5.60`,
backend `0.2.3`.

## Status

> **Status:** hobby project, maintained by one person. No SLA, no PR review.
> To report a vulnerability, see [`SECURITY.md`](SECURITY.md).
>
> **What works reliably** (1+ month in production with the author):
> - Telegram and VK channels, voice messages
> - Separate memory per family member, asymmetric visibility, audit log
> - Backup and restore
> - Install through the admin app, updates through the admin app
>
> **What's in beta:**
> - WhatsApp (code is there, marked "coming soon" in the admin UI)
> - macOS / Linux admin app
>
> **What's planned** (no timeline promises):
> - Native mobile client
> - Web admin
> - Discord / Slack / Matrix as first-class channels
>
> **Upgrade path:** the new admin app detects the version running on your VM
> and offers to upgrade in one click. If something goes wrong — backup →
> uninstall → install.

## Day-in-life scenarios

- **Husband dictated a shopping list.** Wednesday morning he voice-noted a
  list into his VK chat with the bot. Two days later the wife asks in her own
  chat: "what did my husband ask for from the shop on Wednesday?" — the bot
  answers, because the husband has explicitly given her visibility into his
  plans.
- **A decision from a week ago.** On Sunday the wife asks: "what did we
  decide about the cottage this weekend?" — the bot recalls the discussion
  from last Thursday: who's bringing what, when you leave.
- **Kid wrote about a school conflict.** The daughter messaged the bot in
  Telegram about a fight with her friend. That conversation is visible only
  to mum (registered as guardian) — not to dad, not to her sister. By design:
  kids are visible to their guardians, guardians are not visible to kids.

## What it looks like in real life

> **[Wednesday, 18:42 — dad in Telegram]**
>
> Dad: "buy flour cheese bread in two days"
> Familia: "Got it. Want me to remind you the day after tomorrow at 18:00?"
> Dad: "yes, and ping my wife too"
> Familia: "Done. Reminder for you Friday 18:00, and I'll let your wife
> know too."
>
> **[Friday, 18:00 — dad]**
>
> Familia: "Hi! From Wednesday: flour, cheese, bread. Have a good run."
>
> **[Friday, 18:00 — mum on VK]**
>
> Familia: "Dad asked me to remind you: flour / cheese / bread. He's heading
> to the shop now."

Dad explicitly said "ping my wife too" — without that Familia would not write
into the wife's chat. By default, conversations don't leak.

## What it removes

- **Doesn't mix people up.** Dad talks to the bot about work, mum talks
  about the kids. They don't bleed into each other.
- **Doesn't lose what matters.** You agreed something on Wednesday — Sunday
  it reminds you. You don't have to hold the whole family schedule in your
  head.
- **Takes load off the family dispatcher.** In a lot of families one person
  remembers everything. This assistant unloads them.
- **Voice is easier.** Dictate during a run — by lunch your spouse sees it
  in their reminders.
- **Matches your family's tone.** From strict secretary to warm grandma —
  30 seconds in the admin app.
- **On your server.** No "assistant subscription". The bot lives on your
  VPS; conversations don't go to the developer.

## Privacy

- **Where data lives:** on your VM, in Docker containers. No cloud of mine,
  no dashboards "on my side".
- **Who can see it:** only you and people you've given SSH access to the VM.
  The backend does NOT call any external service except (a) the LLM provider
  you chose, (b) the messenger APIs.
- **How memory is shared between people:** by default — it isn't. To make
  the husband's data visible to the wife you have to explicitly set a
  "spouse" link (`spouse_of`) in the family graph through the admin app.
  Children are visible to their guardians, guardians are NOT visible to
  children — asymmetric, can't be bypassed.
- **On removal:** the "Remove" button in the admin app wipes the whole stack
  off the VM in 30 seconds (containers + data + keys). You can download an
  archive to your laptop before removal.

## Who sees what

| Who | Sees own chats | Sees spouse's chats | Sees child's chats |
|---|---|---|---|
| Husband | ✓ | ✓ (if "spouse" link with wife) | ✓ (if guardian) |
| Wife | ✓ | ✓ (if "spouse" link with husband) | ✓ (if guardian) |
| Daughter | ✓ | — | ✓ (own only) |
| Nanny | ✓ (her slice) | — | ✓ (only her charge, if caregiver) |

## Don't worry if you're not a sysadmin

- You don't need to know Linux. Seriously — the wizard does the install
  itself.
- From you — only your VPS login/password (or SSH key) and the bot tokens.
  You don't type anything in a terminal.
- If something goes wrong — the "Remove" button wipes everything in 30
  seconds, you can start over.

## How to set it up (≈15 minutes)

### 1. Get a VPS

Almost any provider works: Hetzner CX22, DigitalOcean, Linode, OVH, your
local cloud. Minimum:

- 2 GB RAM, 10 GB disk
- Ubuntu 22.04+ or Debian 12+
- ~$5/month

When you order it, write down the IP address, login (usually `root`), and
either the password or your SSH key — you'll need them in the next step.

### 2. Download the admin app

Grab the latest `FamiliaAdmin-vX.Y.Z.exe` from
[Releases](../../releases/latest). Put `WebView2Loader.dll` from the same
release next to it — without it the app won't start.

Windows only for now. macOS and Linux are coming later. There is no installer;
the `.exe` is portable. SmartScreen will warn you the first time — click "More
info" → "Run anyway" (the app isn't code-signed; that's not in the hobby-project
budget).

### 3. Launch and click "Connect"

Enter your VPS IP and SSH credentials — password is fine, the app will create
its own SSH key on first connect. From there the wizard does everything:

- installs Docker and dependencies
- deploys the backend
- asks for your Telegram bot and/or VK group token
- asks for your LLM provider key

About 5 minutes later the bot is replying in your family chat.

### What to prepare ahead of time

- **SSH access to your VPS** — login (usually `root`) and password OR an SSH
  key.
- **An LLM provider, your choice:**
  - **ChatGPT Plus / Pro subscription** — sign in via OAuth from the admin
    (through OpenAI Codex). No API key needed, no extra cost. This is
    OpenAI's official agentic-use path via Codex / CLI — not grey area.
  - **API key** for OpenAI / Anthropic / Groq — pay-as-you-go, typically
    a few dollars a month for a family. For Claude this is the only
    path: Anthropic doesn't allow Claude Pro subscriptions to back
    bots, so the admin doesn't expose an OAuth button for them.
- **A channel** — a Telegram bot token (create one in
  [@BotFather](https://t.me/BotFather) in 30 seconds) OR a VK group access
  token (in the community settings).

## How is this different from…

| Alternative | What's missing |
|---|---|
| A regular Telegram bot (Cleo and similar) | Doesn't separate memory between family members. Dad and son see the same answers. |
| ChatGPT / Claude in a chat | Not tied to the family messenger. Cloud-hosted, conversations sit with OpenAI. |
| Notes apps (Notion, Apple Notes, Google Keep) | Doesn't answer questions. Doesn't remember context. Doesn't reach out with reminders. |
| The family group chat | Doesn't remember, doesn't structure, doesn't answer on someone's behalf. |

## Who it's for / who it's not for

**Familia is for you if:**

- There are 2–4 people in your family or close circle.
- Things are scattered across chats, notes, and people's heads — no one
  remembers anything.
- One person is carrying the whole family schedule.
- It matters to you that conversations don't end up with a corporation.
- Everyone already lives in Telegram / VK; nobody will install another app.

**Familia is not for you if:**

- You want a SaaS "click and it works", no VPS — that's a different product.
- You want a bot that TALKS on your behalf in WhatsApp / iMessage.
- The budget for a VPS + LLM (~$5–10/month) is a blocker.
- The family is large (>5–6 active users) — that's not been tested.

## I'd rather build from source

That's over here: [`docs/build-from-source.md`](docs/build-from-source.md).

## Documentation

- [`docs/architecture.md`](docs/architecture.md)
- [`docs/quickstart.md`](docs/quickstart.md)
- [`docs/operations.md`](docs/operations.md)
- [`docs/policy.md`](docs/policy.md)
- [`docs/security.md`](docs/security.md)
- [`docs/release.md`](docs/release.md)

## License

MIT — see [`LICENSE`](LICENSE). Vendored components keep their upstream
licences:

- `nanobot/LICENSE` (MIT)
- `memx/LICENSE` (MIT)
- See [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md).

---

Russian version: [`README.ru.md`](README.ru.md).
