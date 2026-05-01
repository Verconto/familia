"""Regression tests for cron tool ``to`` override.

Original bug: in multi-user setups, the LLM scheduling a reminder for
*another* participant had no way to redirect delivery; cron jobs always
defaulted to the chat_id of the scheduling session. This caused reminders
addressed to user A to be delivered into user B's chat (audit trace showed
a reminder addressed to the owner being routed to member_a's chat instead).

Fix: optional ``to`` parameter overrides the delivery chat_id at job
creation time. Channel stays the same as the current session. An optional
``to_validator`` callback (injected by familia) restricts ``to`` to known
participant identities so a hallucinating/jailbroken LLM can't redirect
deliveries to outsider chat ids.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from nanobot.agent.tools.cron import CronTool


# Generic test ids — match the example principals.json shape (member_a, owner)
# and the placeholder chat ids documented in policy.example.yaml (1000001/1000002).
SESSION_CHAT = "1000002"   # the scheduling participant's chat
OTHER_CHAT = "1000001"     # the *other* participant the reminder is for


class _CapturingSvc:
    """Captures the kwargs passed to ``add_job`` for assertion."""

    def __init__(self) -> None:
        self.last: dict[str, Any] = {}

    def list_jobs(self):  # pragma: no cover
        return []

    def get_job(self, _job_id):  # pragma: no cover
        return None

    def remove_job(self, _job_id):  # pragma: no cover
        return "not-found"

    def add_job(self, **kwargs):
        self.last = kwargs

        class _J:
            pass

        j = _J()
        j.id = "j1"
        j.name = kwargs.get("name", "x")
        return j


@pytest.fixture
def tool_and_svc():
    svc = _CapturingSvc()
    tool = CronTool(svc, default_timezone="UTC")
    tool.set_context("vk", SESSION_CHAT)
    return tool, svc


def _run(tool: CronTool, **kwargs: Any) -> str:
    return asyncio.run(tool.execute(**kwargs))


def test_default_routes_to_session_chat(tool_and_svc):
    tool, svc = tool_and_svc
    out = _run(tool, action="add", message="ping me", at="2030-01-01T00:00:00")
    assert "Created job" in out
    assert svc.last["channel"] == "vk"
    assert svc.last["to"] == SESSION_CHAT


def test_to_override_redirects_delivery(tool_and_svc):
    """The bug case: scheduling for another participant."""
    tool, svc = tool_and_svc
    out = _run(
        tool,
        action="add",
        message="reminder for the other participant",
        at="2030-01-01T00:00:00",
        to=OTHER_CHAT,
    )
    assert "Created job" in out
    assert svc.last["channel"] == "vk"
    assert svc.last["to"] == OTHER_CHAT


def test_empty_to_falls_back_to_session_chat(tool_and_svc):
    tool, svc = tool_and_svc
    _run(tool, action="add", message="x", at="2030-01-01T00:00:00", to="")
    assert svc.last["to"] == SESSION_CHAT


def test_whitespace_only_to_falls_back(tool_and_svc):
    tool, svc = tool_and_svc
    _run(tool, action="add", message="x", at="2030-01-01T00:00:00", to="   ")
    assert svc.last["to"] == SESSION_CHAT


def test_to_override_works_with_cron_expr(tool_and_svc):
    tool, svc = tool_and_svc
    _run(
        tool,
        action="add",
        message="cross-recipient cron reminder",
        cron_expr="0 16 28 4 *",
        tz="Europe/Moscow",
        to=OTHER_CHAT,
    )
    assert svc.last["to"] == OTHER_CHAT


def test_to_override_works_with_every_seconds(tool_and_svc):
    tool, svc = tool_and_svc
    _run(tool, action="add", message="x", every_seconds=3600, to=OTHER_CHAT)
    assert svc.last["to"] == OTHER_CHAT


def test_schema_advertises_to_field():
    """LLMs only learn the param exists by reading the schema description."""
    tool = CronTool(_CapturingSvc())
    props = tool.parameters["properties"]
    assert "to" in props
    desc = props["to"]["description"]
    assert "chat_id" in desc
    # The whole point: description must signal the cross-recipient use case
    # so LLMs reach for it instead of accepting the session default.
    assert "another" in desc.lower() or "other" in desc.lower()


def test_to_not_required():
    tool = CronTool(_CapturingSvc())
    assert "to" not in tool.parameters.get("required", [])


# ---- to_validator (HIGH-severity finding from review) ---------------------

def _make_tool_with_validator(known: dict[str, set[str]]):
    """known: {channel: {chat_id, ...}} — what counts as a known principal."""
    svc = _CapturingSvc()

    def validate(channel: str, chat_id: str) -> bool:
        return chat_id in known.get(channel, set())

    tool = CronTool(svc, default_timezone="UTC", to_validator=validate)
    tool.set_context("vk", SESSION_CHAT)
    return tool, svc


def test_validator_accepts_known_chat_id():
    tool, svc = _make_tool_with_validator({"vk": {OTHER_CHAT, SESSION_CHAT}})
    out = _run(tool, action="add", message="x", at="2030-01-01T00:00:00", to=OTHER_CHAT)
    assert "Created job" in out
    assert svc.last["to"] == OTHER_CHAT


def test_validator_rejects_unknown_chat_id():
    """Prompt-injection guard: arbitrary outsider chat_id must be refused."""
    tool, svc = _make_tool_with_validator({"vk": {OTHER_CHAT, SESSION_CHAT}})
    out = _run(tool, action="add", message="x", at="2030-01-01T00:00:00", to="999999")
    assert "Error" in out
    assert "999999" in out
    # Job must NOT have been created.
    assert svc.last == {}


def test_validator_skipped_when_to_omitted():
    """No 'to' = current session = no validation needed."""
    tool, svc = _make_tool_with_validator({"vk": set()})  # validator would reject everything
    out = _run(tool, action="add", message="x", at="2030-01-01T00:00:00")
    assert "Created job" in out
    assert svc.last["to"] == SESSION_CHAT


def test_validator_skipped_when_to_equals_session():
    """Self-redirect bypasses validation (no-op)."""
    tool, svc = _make_tool_with_validator({"vk": set()})
    out = _run(tool, action="add", message="x", at="2030-01-01T00:00:00", to=SESSION_CHAT)
    assert "Created job" in out
    assert svc.last["to"] == SESSION_CHAT


def test_no_validator_means_unvalidated_passthrough():
    """Backwards-compat: standalone nanobot accepts any 'to' verbatim."""
    svc = _CapturingSvc()
    tool = CronTool(svc, default_timezone="UTC")  # no validator
    tool.set_context("vk", SESSION_CHAT)
    out = _run(tool, action="add", message="x", at="2030-01-01T00:00:00", to="999999")
    assert "Created job" in out
    assert svc.last["to"] == "999999"
