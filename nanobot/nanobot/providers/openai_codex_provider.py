"""OpenAI Codex Responses Provider."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from loguru import logger
from oauth_cli_kit import get_token as get_codex_token

from nanobot.providers.base import LLMProvider, LLMResponse, ToolCallRequest
from nanobot.providers.openai_responses import (
    consume_sse,
    convert_messages,
    convert_tools,
)

DEFAULT_CODEX_URL = "https://chatgpt.com/backend-api/codex/responses"
DEFAULT_ORIGINATOR = "nanobot"

# Hard upper bound on a single Codex request lifetime. The streaming
# endpoint can stall mid-response (server-side timeout, dead TCP keep-alive
# in CLOSE_WAIT, etc.) and httpx's per-chunk read timeout alone has been
# observed to miss those cases — so we cap the entire call here.
_REQUEST_HARD_TIMEOUT = 600.0  # seconds; aborts a stuck request and returns an error
_HTTP_TIMEOUTS = httpx.Timeout(connect=10.0, read=120.0, write=30.0, pool=10.0)


class OpenAICodexProvider(LLMProvider):
    """Use Codex OAuth to call the Responses API."""

    def __init__(self, default_model: str = "openai-codex/gpt-5.1-codex"):
        super().__init__(api_key=None, api_base=None)
        self.default_model = default_model

    async def _call_codex(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str | None,
        reasoning_effort: str | None,
        tool_choice: str | dict[str, Any] | None,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        """Shared request logic for both chat() and chat_stream()."""
        model = model or self.default_model
        system_prompt, input_items = convert_messages(messages)

        token = await asyncio.to_thread(get_codex_token)
        headers = _build_headers(token.account_id, token.access)

        body: dict[str, Any] = {
            "model": _strip_model_prefix(model),
            "store": False,
            "stream": True,
            "instructions": system_prompt,
            "input": input_items,
            "text": {"verbosity": "medium"},
            "include": ["reasoning.encrypted_content"],
            "prompt_cache_key": _prompt_cache_key(messages),
            "tool_choice": tool_choice or "auto",
            "parallel_tool_calls": True,
        }
        if reasoning_effort:
            body["reasoning"] = {"effort": reasoning_effort}
        if tools:
            body["tools"] = convert_tools(tools)

        try:
            try:
                content, tool_calls, finish_reason = await _request_codex(
                    DEFAULT_CODEX_URL, headers, body, verify=True,
                    on_content_delta=on_content_delta,
                )
            except Exception as e:
                if "CERTIFICATE_VERIFY_FAILED" not in str(e):
                    raise
                logger.warning("SSL verification failed for Codex API; retrying with verify=False")
                content, tool_calls, finish_reason = await _request_codex(
                    DEFAULT_CODEX_URL, headers, body, verify=False,
                    on_content_delta=on_content_delta,
                )
            return LLMResponse(content=content, tool_calls=tool_calls, finish_reason=finish_reason)
        except Exception as e:
            # Build a structured error response so ``_run_with_retry``
            # in base.py can decide retry/no-retry deterministically
            # (without resorting to fuzzy text-marker scans on the
            # final ``content``). Three classes:
            #
            #  * ``_CodexHTTPError`` — server response with a status
            #    code; status drives retry policy in base._is_transient.
            #  * ``asyncio.TimeoutError`` — our hard-cap fired, treat
            #    as transient (kind=timeout).
            #  * ``httpx`` connection / read errors — network blip,
            #    treat as transient (kind=connection).
            #  * everything else — surface to the user as final, but
            #    still attempt the ``standard`` retry budget (one or
            #    two passes won't hurt for a flaky transient).
            msg = f"Error calling Codex: {e}"
            retry_after = getattr(e, "retry_after", None) or self._extract_retry_after(msg)
            status_code: int | None = None
            should_retry: bool | None = None
            kind: str | None = None
            if isinstance(e, _CodexHTTPError):
                # ``_CodexHTTPError`` carries the friendly message and,
                # for HTTP failures, the source status code. We re-extract
                # the status from the message because ``_friendly_error``
                # already formatted it as ``HTTP <code>: …`` for non-429
                # paths; for 429 the message is plain English so we set
                # the code explicitly via the marker check.
                if "HTTP " in msg:
                    head = msg.split("HTTP ", 1)[1]
                    digits = ""
                    for ch in head:
                        if ch.isdigit():
                            digits += ch
                        else:
                            break
                    if digits:
                        try:
                            status_code = int(digits)
                        except ValueError:
                            status_code = None
                if status_code is None and "rate limit" in msg.lower():
                    status_code = 429
                if status_code is None and "timed out" in msg.lower():
                    kind = "timeout"
                    should_retry = True
            elif isinstance(e, asyncio.TimeoutError):
                kind = "timeout"
                should_retry = True
            elif isinstance(e, httpx.HTTPError):
                # Catch-all for httpx connection / read / pool errors
                # below the response level (no status code available).
                kind = "connection"
                should_retry = True
            return LLMResponse(
                content=msg,
                finish_reason="error",
                retry_after=retry_after,
                error_status_code=status_code,
                error_should_retry=should_retry,
                error_kind=kind,
                error_retry_after_s=retry_after,
            )

    async def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
        model: str | None = None, max_tokens: int = 4096, temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        return await self._call_codex(messages, tools, model, reasoning_effort, tool_choice)

    async def chat_stream(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None,
        model: str | None = None, max_tokens: int = 4096, temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
        on_content_delta: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        return await self._call_codex(messages, tools, model, reasoning_effort, tool_choice, on_content_delta)

    def get_default_model(self) -> str:
        return self.default_model


def _strip_model_prefix(model: str) -> str:
    if model.startswith("openai-codex/") or model.startswith("openai_codex/"):
        return model.split("/", 1)[1]
    return model


def _build_headers(account_id: str, token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "chatgpt-account-id": account_id,
        "OpenAI-Beta": "responses=experimental",
        "originator": DEFAULT_ORIGINATOR,
        "User-Agent": "nanobot (python)",
        "accept": "text/event-stream",
        "content-type": "application/json",
    }


class _CodexHTTPError(RuntimeError):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


async def _request_codex(
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    verify: bool,
    on_content_delta: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[str, list[ToolCallRequest], str]:
    async def _do() -> tuple[str, list[ToolCallRequest], str]:
        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUTS, verify=verify) as client:
            async with client.stream("POST", url, headers=headers, json=body) as response:
                if response.status_code != 200:
                    text = await response.aread()
                    retry_after = LLMProvider._extract_retry_after_from_headers(response.headers)
                    raise _CodexHTTPError(
                        _friendly_error(response.status_code, text.decode("utf-8", "ignore")),
                        retry_after=retry_after,
                    )
                return await consume_sse(response, on_content_delta)

    try:
        return await asyncio.wait_for(_do(), timeout=_REQUEST_HARD_TIMEOUT)
    except asyncio.TimeoutError as e:
        logger.warning(
            "Codex request exceeded hard timeout {}s; aborting", _REQUEST_HARD_TIMEOUT
        )
        raise _CodexHTTPError(
            f"Codex request timed out after {_REQUEST_HARD_TIMEOUT:.0f}s"
        ) from e


def _prompt_cache_key(messages: list[dict[str, Any]]) -> str:
    raw = json.dumps(messages, ensure_ascii=True, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _friendly_error(status_code: int, raw: str) -> str:
    if status_code == 429:
        return "ChatGPT usage quota exceeded or rate limit triggered. Please try again later."
    return f"HTTP {status_code}: {raw}"
