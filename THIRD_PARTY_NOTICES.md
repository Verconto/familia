# Third-party notices

This project incorporates code from the following open-source projects.
Each retains its original licence in its own subdirectory.

## nanobot

- **Source**: https://github.com/HKUDS/nanobot
- **Vendoring strategy**: forked subtree at `nanobot/`, with patches
  applied inline (see `patches/` for diffs to upstream).
- **Licence**: MIT — see `nanobot/LICENSE`.
- **Why**: provides the agent loop, channel adapters (Telegram, VK,
  Discord, Slack, Matrix), tool-dispatch infrastructure, and the
  bubblewrap-sandboxed exec runtime.

## memX

- **Source**: https://github.com/MehulG/memX
- **Vendoring strategy**: vendored subtree at `memx/`, kept close
  to upstream (config in `memx-config/` is bind-mounted, so we
  never mutate the subtree).
- **Licence**: MIT — see `memx/LICENSE`.
- **Why**: provides the FastAPI + Redis scope-based memory backend
  with per-actor ACL.

## Other dependencies

Runtime Python and JavaScript dependencies are pulled from PyPI and
npm respectively at build time. They retain their respective licences
as declared in their packages. Notable ones:

- `httpx`, `loguru`, `pyyaml` — MIT / BSD / MIT.
- `react`, `react-i18next`, `i18next` — MIT.
- `tauri` — MIT / Apache-2.0.

A full SBOM is not generated for this project; if you need one for
compliance, run `pip-audit --format json` and `npm sbom` against a
freshly built image / installed admin app.
