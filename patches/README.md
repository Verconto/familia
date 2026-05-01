# Upstream patches

Diffs of every nanobot upstream file that familia touches, against the
subtree merge commit `328a386` (nanobot@0806ac02c).

Regenerate after any in-place edit to an upstream file:

```bash
UPSTREAM=328a386
for f in nanobot/nanobot/agent/loop.py \
         nanobot/nanobot/agent/context.py \
         nanobot/nanobot/agent/memory.py \
         nanobot/nanobot/agent/tools/message.py \
         nanobot/nanobot/channels/base.py \
         nanobot/nanobot/channels/vk.py \
         nanobot/nanobot/cli/commands.py \
         nanobot/nanobot/command/builtin.py; do
    name=$(echo "$f" | sed 's|nanobot/nanobot/||;s|/|_|g;s|\.py$|.patch|')
    git diff $UPSTREAM -- "$f" > patches/"$name"
done
git diff $UPSTREAM -- nanobot/pyproject.toml > patches/pyproject.patch
```

## Why they exist

Familia is a subtree merge of upstream nanobot; the long-term plan is to
keep patches minimal so subtree pulls stay painless. Patches split into
two groups:

**Thin (import rewrites only)** — cheap to regenerate after any upstream
move:

| file | LOC changed | nature |
|------|------------:|--------|
| `agent_context.patch`       |  2 | import |
| `channels_base.patch`       |  2 | import |
| `cli_commands.patch`        |  4 | imports |
| `command_builtin.patch`     |  2 | import |
| `agent_tools_message.patch` |  4 | imports |
| `pyproject.patch`           |  1 | pypdf pin bump (CVE fixes) |

**Thick (real familia logic integrated inline)** — when upstream conflicts
land here, re-apply by hand:

| file | LOC changed | nature |
|------|------------:|--------|
| `agent_loop.patch`    |  ~30 | tool registration → `familia.bootstrap.install_tools`; per-turn setup → `familia.bootstrap.on_inbound` |
| `agent_memory.patch`  | ~170 | Dream consolidation integrated with `dream_memory_set` + per-scope routing |
| `channels_vk.patch`   | ~590 | VK polling loop — verbose error logging; keyboard + callback metadata |

`channels/vk.py` was kept in nanobot (per project decision — VK is a
nanobot channel, not a familia concept), so its patch covers VK-side
edits only.
