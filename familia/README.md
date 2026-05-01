# familia

Familia-specific extensions layered on top of the upstream `nanobot` agent.

## Layout

```
familia/
├── src/familia/               # installable package (pip install -e .)
│   ├── bootstrap.py            # install_tools(loop) / on_inbound(msg)
│   ├── principals.py           # principal registry + current-actor ContextVar
│   ├── roles.py                # admin role layer (static + memX-backed grants)
│   ├── audit.py                # JSONL audit log
│   ├── pending_asks.py         # ask_principal correlation store (DEPRECATED 2026-04-27, dead path)
│   ├── policy/                 # YAML policy engine + pending-approval store
│   ├── tools/                  # memory, ask, buttons, admin, family_graph, dream_memory
│   ├── bus/                    # callback_dispatcher
│   ├── cli/                    # audit_view (entry point: familia-audit)
│   └── config/policy.yaml      # canonical deployed rules (packaged with the wheel)
└── tests/                      # pure familia + policy shape (`pytest -q`)
```

## Dependencies on nanobot

Familia imports `nanobot.agent.tools.base`, `nanobot.bus.events`, etc. — the
upstream package must be installed first (the repo-root Dockerfile does this
in order: `pip install nanobot` → `pip install familia`).

## Entry point into nanobot

One module on the upstream side — `familia.bootstrap` — owns all wiring:

```python
# nanobot/agent/loop.py
from familia import audit, bootstrap as familia_bootstrap

class AgentLoop:
    def _register_tools(self):
        ...
        familia_bootstrap.install_tools(self)     # registers 8 familia tools
        ...

    async def _process_message(self, msg):
        ...
        await familia_bootstrap.on_inbound(msg)   # sets current actor + roles
```

All other upstream touchpoints are single-line import rewrites captured
under `../patches/`.

## Upstream update workflow

```bash
cd nanobot/
git subtree pull --prefix=nanobot <upstream> main
cd ..
# If conflicts hit the 8 files in patches/, re-apply familia edits by
# consulting patches/*.patch. Regenerate the snapshot afterwards:
./patches/regenerate.sh   # see patches/README.md
pytest familia/tests -q
```
