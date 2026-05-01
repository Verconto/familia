"""Unit tests for per-scope Dream (familia #44).

Scope:

* ``MemoryStore.append_history`` tags entries with ``actor``.
* ``Consolidator._group_by_actor`` splits a mixed chunk into per-actor runs.
* ``DreamMemorySetTool`` builds the right memX key for each scope and gates
  through policy as ``dream_consolidator``.

The heavy Dream.run path (LLM-driven Phase 1 + Phase 2) is not exercised
here — it's covered by live integration smoke.  These tests pin down
the mechanical guarantees the smoke depends on.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from nanobot.agent.memory import Consolidator, MemoryStore
from familia.policy import Decision, PolicyContext
from familia.policy.engine import load_engine
from familia.tools.dream_memory import DreamMemorySetTool


REPO_ROOT = Path(__file__).resolve().parents[1] / "src" / "familia" / "config"


# --- append_history --------------------------------------------------------

def test_append_history_records_actor(tmp_path: Path) -> None:
    store = MemoryStore(tmp_path)
    c1 = store.append_history("hi from member_a", actor="member_a")
    c2 = store.append_history("system tick")  # no actor
    entries = list(store._iter_valid_entries())
    by_cursor = {cursor: e for e, cursor in entries}
    assert by_cursor[c1]["actor"] == "member_a"
    assert "actor" not in by_cursor[c2]


# --- Consolidator._group_by_actor -----------------------------------------

def _msg(role: str, content: str, actor: str | None = None) -> dict[str, Any]:
    m: dict[str, Any] = {"role": role, "content": content}
    if actor:
        m["actor"] = actor
    return m


def test_group_by_actor_splits_on_new_user() -> None:
    msgs = [
        _msg("user", "zh1", "member_a"),
        _msg("assistant", "resp1"),
        _msg("user", "vya1", "owner"),
        _msg("assistant", "resp2"),
        _msg("user", "zh2", "member_a"),
    ]
    groups = Consolidator._group_by_actor(msgs)
    assert [a for a, _ in groups] == ["member_a", "owner", "member_a"]
    assert [len(g) for _, g in groups] == [2, 2, 1]


def test_group_by_actor_preserves_run_when_actor_repeats() -> None:
    msgs = [
        _msg("user", "zh1", "member_a"),
        _msg("assistant", "resp1"),
        _msg("user", "zh2", "member_a"),
        _msg("assistant", "resp2"),
    ]
    groups = Consolidator._group_by_actor(msgs)
    assert len(groups) == 1
    assert groups[0][0] == "member_a"
    assert len(groups[0][1]) == 4


def test_group_by_actor_leading_untagged_goes_to_none() -> None:
    msgs = [
        _msg("assistant", "system notice"),
        _msg("user", "zh1", "member_a"),
    ]
    groups = Consolidator._group_by_actor(msgs)
    assert groups[0][0] is None
    assert groups[1][0] == "member_a"


# --- DreamMemorySetTool: key construction ---------------------------------

@pytest.mark.parametrize(
    "scope,actor,other,key,expected",
    [
        ("shared",  None,         None,     "todo",  "shared:todo"),
        ("private", "member_a",     None,     "feels", "private:member_a:feels"),
        # pair: alphabetical order regardless of argument order
        ("pair",    "member_a",     "owner", "note", "pair:member_a_owner:note"),
        ("pair",    "owner", "member_a",     "note", "pair:member_a_owner:note"),
    ],
)
def test_dream_memory_key_resolution(scope, actor, other, key, expected):
    from familia.tools.dream_memory import _resolve_full_key
    full, err = _resolve_full_key(scope, key, actor, other)
    assert err is None, err
    assert full == expected


@pytest.mark.parametrize(
    "scope,actor,other,key",
    [
        ("private", None,         None,      "x"),   # scope=private requires actor
        ("pair",    "member_a",     None,      "x"),   # pair requires both
        ("pair",    "member_a",     "member_a",  "x"),   # pair requires distinct
        ("shared",  None,         None,      ""),    # empty key
        ("weird",   None,         None,      "x"),   # unknown scope
    ],
)
def test_dream_memory_key_invalid(scope, actor, other, key):
    from familia.tools.dream_memory import _resolve_full_key
    _, err = _resolve_full_key(scope, key, actor, other)
    assert err is not None


# --- DreamMemorySetTool: policy gate --------------------------------------

@pytest.fixture(scope="module")
def policy_engine():
    # Reuse the real policy.yaml so the dream_consolidator rule is tested
    # against the actual deployed rules, not a local fiction.
    from familia import policy as policy_mod
    eng = load_engine(REPO_ROOT / "policy.yaml")
    policy_mod.get_engine()  # warm singleton module
    policy_mod._engine = eng  # type: ignore[attr-defined]
    yield eng


def test_dream_consolidator_allowed_for_private_member_a(policy_engine) -> None:
    r = policy_engine.evaluate(
        PolicyContext(
            action="memory.write", actor="dream_consolidator",
            to_chat="private:member_a:secret",
        )
    )
    assert r.decision is Decision.ALLOW
    assert r.rule and "dream_consolidator" in r.rule.name


def test_dream_consolidator_denied_for_memory_read(policy_engine) -> None:
    r = policy_engine.evaluate(
        PolicyContext(
            action="memory.read", actor="dream_consolidator",
            to_chat="private:member_a:secret",
        )
    )
    assert r.decision is Decision.DENY


def test_dream_memory_set_tool_calls_memx_on_allow(policy_engine) -> None:
    import asyncio
    from familia.principals import set_current_actor
    from familia.tools.dream_memory import CONSOLIDATOR_ACTOR

    tool = DreamMemorySetTool(base_url="http://mock-memx:8000", api_key="dream_consolidator_key")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "ok"

    # Dream agent pins this actor for its turn; the tool now refuses to run
    # outside that context (defense-in-depth against accidental registration
    # on the main loop). Mirror prod here.
    set_current_actor(CONSOLIDATOR_ACTOR)

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client_cls.return_value.__aenter__.return_value = mock_client

        result = asyncio.run(tool.execute(
            scope="private", actor="member_a",
            key="feelings", value="worried about deadline",
        ))

    assert "Stored at 'private:member_a:feelings'" in result
    mock_client.post.assert_called_once()
    call_args = mock_client.post.call_args
    assert call_args.kwargs["headers"]["x-api-key"] == "dream_consolidator_key"
    assert call_args.kwargs["json"]["key"] == "private:member_a:feelings"
