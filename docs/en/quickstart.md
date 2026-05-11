# Quickstart

Familia is a family assistant that runs on your VPS and talks to your family through Telegram/VK. This document is for a **normal user**: a parent who downloaded the admin app and wants to bring the stack up in 5 minutes.

> If you are a developer and want to build the `.exe` from source or run the service manually with `docker compose`, see [`build-from-source.md`](build-from-source.md).

## Short version

Three steps: rent a VPS, download `FamiliaAdmin-vX.Y.Z.exe`, run the install wizard. The admin app installs Docker, builds the image from source (~3.6 MB tarball embedded in the `.exe`), and starts the stack.

Expect about 5 minutes of active work plus 3-5 minutes of build time on the VM.

## What to prepare ahead of time

### 1. VPS

Any Linux hosting with Ubuntu 22.04+ or Debian 12+ works. Minimum:

- **2 GB RAM**
- **10 GB disk**

Known options:

- **Hetzner Cloud** (CX22, about €4/month).
- **Selectel** / **Timeweb** / **Beget** for Russian card payments.
- **DigitalOcean** / **Vultr** / **Linode** with standard Ubuntu images.

Budget: roughly **250-500 RUB/month**, depending on provider and plan.

You do **not** need to install Docker or anything else in advance. The install wizard does it.

### 2. VPS access

Any of these work:

- `root@<ip>` + password. The wizard creates an SSH key on first connect, installs the public key into `~/.ssh/authorized_keys`, and stops needing the password. The password is not stored.
- Normal user + sudo, with a password or NOPASSWD.
- Existing SSH key. If the key has a passphrase, the admin app asks for it.

When you enter `host` in the wizard, the admin app tries to read `~/.ssh/config` automatically (`ssh -G user@host`) and suggest the right key. If it is not in the config, it tries the standard chain: `id_ed25519` -> `id_rsa` -> `id_ecdsa` -> `id_dsa`.

### 3. LLM provider

Choose one:

- **ChatGPT Plus / Pro subscription** — sign in via OAuth directly from the admin app through OpenAI Codex. No API key, no extra spend beyond your subscription. This is OpenAI's officially supported path for agentic use through Codex / CLI.
- **API key** for OpenAI / Anthropic / Groq — if you already have a paid API account. Enter it in the admin app during setup. For Claude this is the only supported path: Anthropic does not allow Claude Pro subscriptions to back bot scenarios, so the admin app does not expose an OAuth button for them.

### 4. Chat channel (optional)

At least one of:

- **Telegram bot token** — created in 30 seconds through [@BotFather](https://t.me/BotFather): `/newbot` -> name -> token like `1234567890:AAFxxx...`.
- **VK group access token** — in community settings, **API usage -> Create key**, permissions: `messages`, `photos`.

You can skip channels during installation and add them later in the admin app in a couple of clicks. **WhatsApp** code already exists, but the UI marks it as "coming soon": we are waiting for WhatsApp to provide an open Bot API. Until then, enabling it honestly is not possible.

### 5. Windows laptop

Any Windows 10 / 11 machine with **WebView2** installed. It is present by default on modern Windows systems.

## Install through the admin app

1. Download **two** files from [Releases](../../releases/latest):
   - `FamiliaAdmin-vX.Y.Z.exe`
   - `WebView2Loader.dll`

   Put them **in the same folder**. The `.exe` will not start without the `.dll` next to it.

2. Run the `.exe`. Windows SmartScreen shows a red "publisher could not be verified" screen. Click **More info -> Run anyway**. The binary is unsigned: a code-signing certificate costs about $300/year, too much for a hobby project. You can verify the SHA-256 hash published in Releases to make sure the file was not replaced.

3. On the first screen, enter your VM **IP/host** and login. The admin app:
   - checks SSH config and suggests a key automatically;
   - supports password-only mode if there is no key, then creates one and replaces password auth with it;
   - asks for the passphrase if the key is encrypted.

4. After connect, the **preflight panel** opens: a list of VM checks with color status.

   - **Green** — OK.
   - **Yellow** — non-critical, you can continue.
   - **Red** — blocks install, for example less than 3 GB of free disk or missing `curl`. The **Install** button stays disabled until red items are fixed.

5. Enter your name. It becomes the first **admin principal**: the owner of the stack.

6. Click **Install**. In about 5 minutes the wizard:
   - installs Docker and the compose plugin if missing;
   - unpacks the source pack on the VM (~3.6 MB tarball embedded in the `.exe`);
   - builds images on the VM with `docker compose build`;
   - starts the stack: gateway + memX + redis;
   - initializes the family graph.

7. After installation the dashboard opens. Add channels (Telegram / VK), invite the rest of the family, and follow the built-in steps in the **FAQ** tab.

## If hosting blocks international egress

Some VPS providers, especially Russian providers in 2026, filter outbound traffic to `pypi.org`, `get.docker.com`, `registry-1.docker.io`, and similar hosts. Bootstrap detects this automatically: it probes the known hosts with a 5-second timeout and switches to built-in mirrors when they are unreachable (Tsinghua, Yandex, aliyun, mirror.gcr.io, dockerhub.timeweb.cloud). When needed, it also reconfigures Docker daemon for `registry-mirror`.

To force your own mirror manually, set `APT_MIRROR=`, `PIP_INDEX_URL=`, or `NPM_REGISTRY=` before running bootstrap. These override the automatic choice. Details: [`operations.md`](operations.md), *Mirror fallbacks*.

## Updates

Previously you had to download things manually. Not anymore.

When a new version is released, download the new `FamiliaAdmin-vX.Y.Z.exe` and run it. On the next connection to the VM, the admin app compares its embedded backend version with the version deployed on the server and shows one of three states:

- **admin newer than VM** — "Update VM?" modal. The button builds the new image from the embedded source pack on the VM (~3-5 minutes).
- **versions match** — dashboard opens immediately.
- **admin older than VM** — connection is blocked with "admin too old". Download a newer `.exe`.

Admin version (`0.5.60`) and backend version (`0.2.3`) are separate. Admin can ship without a backend update and the backend can update without admin UI changes.

## Something went wrong?

- Open the **Diagnostics** tab in the admin app. It runs the standard checks: identity binding, memX reachability, audit tail, and displays the result in human language.
- Typical problems and symptoms are listed in [`operations.md`](operations.md), *Common problems*.
- If nothing helps, collect logs (`docker compose logs --tail=200 familia-gateway` and `docker compose logs --tail=100 memx-backend`) and open an issue.
