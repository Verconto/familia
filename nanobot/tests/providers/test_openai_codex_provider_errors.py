"""Codex provider: structured error metadata for retry classification.

The provider used to return ``LLMResponse(content="Error calling Codex: …",
finish_reason="error")`` and let ``base._is_transient_response`` guess
retry-eligibility from text markers in the message. That worked for
HTTP-coded responses (the ``HTTP 503: …`` substring matched the marker
list) but failed for low-level ``httpx`` errors and for our own
``asyncio.TimeoutError`` hard-cap path. The new behaviour sets
``error_status_code``/``error_should_retry``/``error_kind`` directly so
``_is_transient_response`` can decide deterministically.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from nanobot.providers.openai_codex_provider import (
    OpenAICodexProvider,
    _CodexHTTPError,
)


def _provider() -> OpenAICodexProvider:
    return OpenAICodexProvider()


@pytest.mark.asyncio
async def test_codex_http_503_sets_status_code(monkeypatch):
    monkeypatch.setattr(
        "nanobot.providers.openai_codex_provider.get_codex_token",
        lambda: type("T", (), {"account_id": "a", "access": "t"})(),
    )
    err = _CodexHTTPError("HTTP 503: gateway down", retry_after=None)
    with patch(
        "nanobot.providers.openai_codex_provider._request_codex",
        AsyncMock(side_effect=err),
    ):
        resp = await _provider().chat(messages=[{"role": "user", "content": "hi"}])

    assert resp.finish_reason == "error"
    assert resp.error_status_code == 503
    # Status>=500 → ``base._is_transient_response`` returns True via
    # status check, no need for explicit should_retry.
    assert "Error calling Codex" in (resp.content or "")


@pytest.mark.asyncio
async def test_codex_http_429_quota_message_marks_retryable(monkeypatch):
    monkeypatch.setattr(
        "nanobot.providers.openai_codex_provider.get_codex_token",
        lambda: type("T", (), {"account_id": "a", "access": "t"})(),
    )
    # _friendly_error returns "ChatGPT usage quota exceeded or rate limit
    # triggered. Please try again later." for 429 — no "HTTP 429" substring.
    err = _CodexHTTPError(
        "ChatGPT usage quota exceeded or rate limit triggered. Please try again later.",
        retry_after=None,
    )
    with patch(
        "nanobot.providers.openai_codex_provider._request_codex",
        AsyncMock(side_effect=err),
    ):
        resp = await _provider().chat(messages=[{"role": "user", "content": "hi"}])

    assert resp.finish_reason == "error"
    assert resp.error_status_code == 429


@pytest.mark.asyncio
async def test_codex_asyncio_timeout_marks_retryable(monkeypatch):
    monkeypatch.setattr(
        "nanobot.providers.openai_codex_provider.get_codex_token",
        lambda: type("T", (), {"account_id": "a", "access": "t"})(),
    )
    with patch(
        "nanobot.providers.openai_codex_provider._request_codex",
        AsyncMock(side_effect=asyncio.TimeoutError()),
    ):
        resp = await _provider().chat(messages=[{"role": "user", "content": "hi"}])

    assert resp.finish_reason == "error"
    assert resp.error_kind == "timeout"
    assert resp.error_should_retry is True


@pytest.mark.asyncio
async def test_codex_httpx_connect_error_marks_retryable(monkeypatch):
    monkeypatch.setattr(
        "nanobot.providers.openai_codex_provider.get_codex_token",
        lambda: type("T", (), {"account_id": "a", "access": "t"})(),
    )
    with patch(
        "nanobot.providers.openai_codex_provider._request_codex",
        AsyncMock(side_effect=httpx.ConnectError("network unreachable")),
    ):
        resp = await _provider().chat(messages=[{"role": "user", "content": "hi"}])

    assert resp.finish_reason == "error"
    assert resp.error_kind == "connection"
    assert resp.error_should_retry is True


@pytest.mark.asyncio
async def test_codex_unknown_exception_no_retry_hint(monkeypatch):
    """An unrecognised exception still produces an error response but
    leaves ``error_should_retry`` unset so the standard 6-attempt
    budget runs and gives up cleanly."""
    monkeypatch.setattr(
        "nanobot.providers.openai_codex_provider.get_codex_token",
        lambda: type("T", (), {"account_id": "a", "access": "t"})(),
    )
    with patch(
        "nanobot.providers.openai_codex_provider._request_codex",
        AsyncMock(side_effect=RuntimeError("unexpected")),
    ):
        resp = await _provider().chat(messages=[{"role": "user", "content": "hi"}])

    assert resp.finish_reason == "error"
    assert resp.error_status_code is None
    assert resp.error_should_retry is None
    assert resp.error_kind is None
