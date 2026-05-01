# familia

A family AI assistant that lives in Telegram and VK. One bot for the whole
family: it remembers who's talking to it, replies to each person in their own
context, and doesn't mix up other people's business.

<!-- TODO: screenshot of admin app -->

> **Hobby project, no warranty.** Maintained by one person on weekends. No SLA,
> no PR review. Fork freely. To report a vulnerability, see
> [`SECURITY.md`](SECURITY.md).

## What it does

- **Telegram and VK at the same time.** One assistant persona, two channels.
  Wife writes in Telegram, husband writes in VK — same bot.
- **Knows each family member separately.** What you discussed with dad doesn't
  leak to mum without your permission. Every person has their own memory and
  context.
- **Transcribes voice messages.** Dictate on the go, the assistant gets it.
- **Remembers what matters.** Plans, habits, agreements, birthdays — and uses
  them in later conversations instead of asking you over and over.
- **You can set its personality.** From a strict secretary to a warm grandma —
  configured in the admin app, no coding.
- **Runs on YOUR server.** No cloud subscription, your chats and notes don't
  leak to the developer.

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

## What to prepare ahead of time

- **SSH access to your VPS** — login (usually `root`) and password OR an SSH
  key.
- **An LLM provider, your choice:**
  - ChatGPT Plus or Claude Pro subscription — sign in via OAuth, no API key
    needed, no extra cost;
  - or an OpenAI / Anthropic / Groq API key — pay-as-you-go, typically a few
    dollars a month for a family.
- **A channel** — a Telegram bot token (create one in
  [@BotFather](https://t.me/BotFather) in 30 seconds) OR a VK group access
  token (in the community settings).

## FAQ

**"Why do I need a server? Can't I just install an app?"**
Because the bot needs to run 24/7 without your laptop being on, and because
your conversations shouldn't sit with the developer. Your server, your data.

**"How much does it cost per month?"**
VPS around $5. LLM: if you already pay for ChatGPT Plus or Claude Pro, nothing
extra. If you use an API key, usually a few dollars a month for a 2–4 person
family.

**"Is it secure?"**
All data lives on your VPS. The backend doesn't call out to anyone except
your chosen LLM provider and Telegram/VK. More in
[`docs/security.md`](docs/security.md).

**"What if I change my mind?"**
The admin app has a "Remove" button that wipes the stack from your VPS in 30
seconds — containers, data, config. After that you just stop renewing the VPS.

**"Will there be WhatsApp support?"**
No, and not soon. WhatsApp has no public Bot API; the only option would be
hijacking a personal account, which is both fragile and against the rules.
More in the FAQ tab inside the admin app.

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
