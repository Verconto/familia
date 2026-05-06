# Agent Instructions

## Scheduled Reminders

Before scheduling reminders, check available skills and follow skill guidance first.
Use the built-in `cron` tool to create/list/remove jobs (do not call `nanobot cron` via `exec`).
Get USER_ID and CHANNEL from the current session (e.g., `8281248569` and `telegram` from `telegram:8281248569`).

**Do NOT just write reminders to MEMORY.md** — that won't trigger actual notifications.

## Heartbeat Tasks

`HEARTBEAT.md` is checked on the configured heartbeat interval. Use file tools to manage periodic tasks:

- **Add**: `edit_file` to append new tasks
- **Remove**: `edit_file` to delete completed tasks
- **Rewrite**: `write_file` to replace all tasks

When the user asks for a **time-of-day reminder** (e.g. "remind me daily at 12:00", "every Monday at 8 AM", "tomorrow at 18:00") — use the **`cron` tool**, NOT `HEARTBEAT.md`. Cron schedules deliver one message per fired tick; HEARTBEAT.md content is read by the heartbeat agent every interval and may be re-interpreted as a fresh request, so a reminder placed there will be surfaced repeatedly between the time-of-day instants.

`HEARTBEAT.md` is for **non-scheduled persistent context** that the heartbeat agent should consider on every tick — open todos, ongoing watches ("notify when X happens"), background reminders without a fixed clock. If a task already has a `cron` job representing it, do NOT also write it into `HEARTBEAT.md`.
