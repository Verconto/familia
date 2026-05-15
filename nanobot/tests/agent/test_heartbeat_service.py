import asyncio

import pytest

from nanobot.heartbeat.service import HeartbeatService
from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class DummyProvider(LLMProvider):
    def __init__(self, responses: list[LLMResponse]):
        super().__init__()
        self._responses = list(responses)
        self.calls = 0

    async def chat(self, *args, **kwargs) -> LLMResponse:
        self.calls += 1
        if self._responses:
            return self._responses.pop(0)
        return LLMResponse(content="", tool_calls=[])

    def get_default_model(self) -> str:
        return "test-model"


@pytest.mark.asyncio
async def test_start_is_idempotent(tmp_path) -> None:
    provider = DummyProvider([])

    service = HeartbeatService(
        workspace=tmp_path,
        provider=provider,
        model="openai/gpt-4o-mini",
        interval_s=9999,
        enabled=True,
    )

    await service.start()
    first_task = service._task
    await service.start()

    assert service._task is first_task

    service.stop()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_decide_returns_skip_when_no_tool_call(tmp_path) -> None:
    provider = DummyProvider([LLMResponse(content="no tool call", tool_calls=[])])
    service = HeartbeatService(
        workspace=tmp_path,
        provider=provider,
        model="openai/gpt-4o-mini",
    )

    action, tasks = await service._decide("heartbeat content")
    assert action == "skip"
    assert tasks == ""


@pytest.mark.asyncio
async def test_trigger_now_executes_when_decision_is_run(tmp_path) -> None:
    (tmp_path / "HEARTBEAT.md").write_text("- [ ] do thing", encoding="utf-8")

    provider = DummyProvider([
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="hb_1",
                    name="heartbeat",
                    arguments={"action": "run", "tasks": "check open tasks"},
                )
            ],
        )
    ])

    called_with: list[str] = []

    async def _on_execute(tasks: str) -> str:
        called_with.append(tasks)
        return "done"

    service = HeartbeatService(
        workspace=tmp_path,
        provider=provider,
        model="openai/gpt-4o-mini",
        on_execute=_on_execute,
    )

    result = await service.trigger_now()
    assert result == "done"
    assert called_with == ["check open tasks"]


@pytest.mark.asyncio
async def test_trigger_now_returns_none_when_decision_is_skip(tmp_path) -> None:
    (tmp_path / "HEARTBEAT.md").write_text("- [ ] do thing", encoding="utf-8")

    provider = DummyProvider([
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="hb_1",
                    name="heartbeat",
                    arguments={"action": "skip"},
                )
            ],
        )
    ])

    async def _on_execute(tasks: str) -> str:
        return tasks

    service = HeartbeatService(
        workspace=tmp_path,
        provider=provider,
        model="openai/gpt-4o-mini",
        on_execute=_on_execute,
    )

    assert await service.trigger_now() is None


@pytest.mark.asyncio
async def test_tick_notifies_when_evaluator_says_yes(tmp_path, monkeypatch) -> None:
    """Phase 1 run -> Phase 2 execute -> Phase 3 evaluate=notify -> on_notify called."""
    (tmp_path / "HEARTBEAT.md").write_text("- [ ] check deployments", encoding="utf-8")

    provider = DummyProvider([
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="hb_1",
                    name="heartbeat",
                    arguments={"action": "run", "tasks": "check deployments"},
                )
            ],
        ),
    ])

    executed: list[str] = []
    notified: list[str] = []

    async def _on_execute(tasks: str) -> str:
        executed.append(tasks)
        return "deployment failed on staging"

    async def _on_notify(response: str) -> None:
        notified.append(response)

    service = HeartbeatService(
        workspace=tmp_path,
        provider=provider,
        model="openai/gpt-4o-mini",
        on_execute=_on_execute,
        on_notify=_on_notify,
    )

    async def _eval_notify(*a, **kw):
        return True

    monkeypatch.setattr("nanobot.utils.evaluator.evaluate_response", _eval_notify)

    await service._tick()
    assert executed == ["check deployments"]
    assert notified == ["deployment failed on staging"]


@pytest.mark.asyncio
async def test_tick_suppresses_when_evaluator_says_no(tmp_path, monkeypatch) -> None:
    """Phase 1 run -> Phase 2 execute -> Phase 3 evaluate=silent -> on_notify NOT called."""
    (tmp_path / "HEARTBEAT.md").write_text("- [ ] check status", encoding="utf-8")

    provider = DummyProvider([
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="hb_1",
                    name="heartbeat",
                    arguments={"action": "run", "tasks": "check status"},
                )
            ],
        ),
    ])

    executed: list[str] = []
    notified: list[str] = []

    async def _on_execute(tasks: str) -> str:
        executed.append(tasks)
        return "everything is fine, no issues"

    async def _on_notify(response: str) -> None:
        notified.append(response)

    service = HeartbeatService(
        workspace=tmp_path,
        provider=provider,
        model="openai/gpt-4o-mini",
        on_execute=_on_execute,
        on_notify=_on_notify,
    )

    async def _eval_silent(*a, **kw):
        return False

    monkeypatch.setattr("nanobot.utils.evaluator.evaluate_response", _eval_silent)

    await service._tick()
    assert executed == ["check status"]
    assert notified == []


@pytest.mark.asyncio
async def test_decide_retries_transient_error_then_succeeds(tmp_path, monkeypatch) -> None:
    provider = DummyProvider([
        LLMResponse(content="429 rate limit", finish_reason="error"),
        LLMResponse(
            content="",
            tool_calls=[
                ToolCallRequest(
                    id="hb_1",
                    name="heartbeat",
                    arguments={"action": "run", "tasks": "check open tasks"},
                )
            ],
        ),
    ])

    delays: list[int] = []

    async def _fake_sleep(delay: int) -> None:
        delays.append(delay)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)

    service = HeartbeatService(
        workspace=tmp_path,
        provider=provider,
        model="openai/gpt-4o-mini",
    )

    action, tasks = await service._decide("heartbeat content")

    assert action == "run"
    assert tasks == "check open tasks"
    assert provider.calls == 2
    assert delays == [1]


@pytest.mark.asyncio
async def test_decide_prompt_includes_current_time(tmp_path) -> None:
    """Phase 1 user prompt must contain current time so the LLM can judge task urgency."""

    captured_messages: list[dict] = []

    class CapturingProvider(LLMProvider):
        async def chat(self, *, messages=None, **kwargs) -> LLMResponse:
            if messages:
                captured_messages.extend(messages)
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="hb_1", name="heartbeat",
                        arguments={"action": "skip"},
                    )
                ],
            )

        def get_default_model(self) -> str:
            return "test-model"

    service = HeartbeatService(
        workspace=tmp_path,
        provider=CapturingProvider(),
        model="test-model",
    )

    await service._decide("- [ ] check servers at 10:00 UTC")

    user_msg = captured_messages[1]
    assert user_msg["role"] == "user"
    assert "Current Time:" in user_msg["content"]


def _install_familia_stubs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    memx_value: str | None,
    memx_key: str | None = "test-key",
) -> None:
    """Inject minimal fakes into sys.modules so the dynamic imports
    inside ``HeartbeatService._read_heartbeat_content`` succeed and
    return controlled values."""
    import sys
    import types

    class _Principal:
        def __init__(self) -> None:
            self.memx_key = memx_key

    class _Registry:
        def get(self, _pid: str) -> _Principal:
            return _Principal()

    class _Client:
        def __init__(self, _pid: str, _key: str) -> None:
            pass

        def get(self, _suffix: str) -> str | None:
            return memx_value

    principals_mod = types.ModuleType("familia.principals")
    principals_mod.get_registry = lambda: _Registry()  # type: ignore[attr-defined]

    pm_mod = types.ModuleType("familia.acl.principal_memory")
    pm_mod.PrincipalMemoryClient = _Client  # type: ignore[attr-defined]

    familia_mod = types.ModuleType("familia")
    acl_mod = types.ModuleType("familia.acl")

    monkeypatch.setitem(sys.modules, "familia", familia_mod)
    monkeypatch.setitem(sys.modules, "familia.acl", acl_mod)
    monkeypatch.setitem(sys.modules, "familia.principals", principals_mod)
    monkeypatch.setitem(sys.modules, "familia.acl.principal_memory", pm_mod)


def test_familia_owned_source_does_not_fall_back_to_file(
    tmp_path, monkeypatch
) -> None:
    """When familia is installed, an admin is resolved, and that admin has
    a memx_key, ``value:heartbeat`` is the *only* source. A stale
    HEARTBEAT.md must NOT be consulted even if memX returned no
    content — otherwise an empty slot + leftover file content causes
    spam every tick (real incident, 2026-05-15).
    """
    (tmp_path / "HEARTBEAT.md").write_text(
        "- stale task that already has a cron equivalent",
        encoding="utf-8",
    )
    _install_familia_stubs(monkeypatch, memx_value=None)

    service = HeartbeatService(
        workspace=tmp_path,
        provider=DummyProvider([]),
        model="test-model",
    )
    monkeypatch.setattr(service, "_admin_principal_id", lambda: "owner")

    content, source = service._read_heartbeat_content()
    assert content is None
    assert source is None


def test_familia_owned_source_returns_memx_when_present(
    tmp_path, monkeypatch
) -> None:
    """memX wins over the legacy file when both have content."""
    (tmp_path / "HEARTBEAT.md").write_text("file content", encoding="utf-8")
    _install_familia_stubs(monkeypatch, memx_value="memx content")

    service = HeartbeatService(
        workspace=tmp_path,
        provider=DummyProvider([]),
        model="test-model",
    )
    monkeypatch.setattr(service, "_admin_principal_id", lambda: "owner")

    content, source = service._read_heartbeat_content()
    assert content == "memx content"
    assert source == "memx"


def test_file_fallback_used_when_admin_has_no_memx_key(
    tmp_path, monkeypatch
) -> None:
    """If the resolved admin has no memx_key, familia cannot own the
    source — the file fallback remains the only path. Keeps
    standalone-nanobot-like setups working."""
    (tmp_path / "HEARTBEAT.md").write_text("file content", encoding="utf-8")
    _install_familia_stubs(monkeypatch, memx_value=None, memx_key=None)

    service = HeartbeatService(
        workspace=tmp_path,
        provider=DummyProvider([]),
        model="test-model",
    )
    monkeypatch.setattr(service, "_admin_principal_id", lambda: "owner")

    content, source = service._read_heartbeat_content()
    assert content == "file content"
    assert source == "file"


def test_file_fallback_used_when_no_admin_resolved(
    tmp_path, monkeypatch
) -> None:
    """Standalone nanobot path: no admin → file is the only source."""
    (tmp_path / "HEARTBEAT.md").write_text("file content", encoding="utf-8")

    service = HeartbeatService(
        workspace=tmp_path,
        provider=DummyProvider([]),
        model="test-model",
    )
    monkeypatch.setattr(service, "_admin_principal_id", lambda: None)

    content, source = service._read_heartbeat_content()
    assert content == "file content"
    assert source == "file"
