"""Regression tests for the strict exec-sandbox guard (C1 strict mode).

Ensures AgentLoop refuses to register ExecTool when ``tools.exec.sandbox``
is empty unless the operator explicitly opts in via
``NANOBOT_ALLOW_UNSANDBOXED_EXEC=1``. This prevents accidental deploys
where a prompt-injection from an untrusted input source becomes RCE in the
container.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from nanobot.agent.loop import AgentLoop
from nanobot.bus.queue import MessageBus
from nanobot.config.schema import ExecToolConfig


def _make_loop(tmp_path: Path, exec_config: ExecToolConfig) -> AgentLoop:
    bus = MessageBus()
    provider = MagicMock()
    provider.get_default_model.return_value = "test-model"
    return AgentLoop(
        bus=bus,
        provider=provider,
        workspace=tmp_path,
        model="test-model",
        exec_config=exec_config,
    )


def test_default_config_has_bwrap_sandbox(tmp_path: Path) -> None:
    """The shipped default config should be safe-by-default."""
    cfg = ExecToolConfig()
    assert cfg.sandbox == "bwrap"
    # And the loop accepts it without complaint.
    loop = _make_loop(tmp_path, cfg)
    assert loop.tools.get("exec") is not None


def test_empty_sandbox_refuses_to_register(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("NANOBOT_ALLOW_UNSANDBOXED_EXEC", raising=False)
    cfg = ExecToolConfig(sandbox="")
    with pytest.raises(RuntimeError, match="sandbox is empty"):
        _make_loop(tmp_path, cfg)


def test_whitespace_only_sandbox_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Trim before checking: '  ' is not a valid sandbox name."""
    monkeypatch.delenv("NANOBOT_ALLOW_UNSANDBOXED_EXEC", raising=False)
    cfg = ExecToolConfig(sandbox="   ")
    with pytest.raises(RuntimeError, match="sandbox is empty"):
        _make_loop(tmp_path, cfg)


def test_explicit_dev_opt_in_allows_empty_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Dev override env var bypasses the guard but logs a warning."""
    monkeypatch.setenv("NANOBOT_ALLOW_UNSANDBOXED_EXEC", "1")
    cfg = ExecToolConfig(sandbox="")
    loop = _make_loop(tmp_path, cfg)
    assert loop.tools.get("exec") is not None


def test_disabled_exec_skips_guard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the operator disabled exec entirely, the sandbox guard is moot."""
    monkeypatch.delenv("NANOBOT_ALLOW_UNSANDBOXED_EXEC", raising=False)
    cfg = ExecToolConfig(enable=False, sandbox="")
    loop = _make_loop(tmp_path, cfg)
    assert loop.tools.get("exec") is None


def test_truthy_string_other_than_1_does_not_bypass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Strict env-var matching: only literal '1' opts in."""
    monkeypatch.setenv("NANOBOT_ALLOW_UNSANDBOXED_EXEC", "true")
    cfg = ExecToolConfig(sandbox="")
    with pytest.raises(RuntimeError, match="sandbox is empty"):
        _make_loop(tmp_path, cfg)
