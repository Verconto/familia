"""Tests for heartbeat target-actor wiring (Option B fix).

Background: ``_pick_heartbeat_target`` previously returned the most-recently
active session, so heartbeat ticks would ride whichever participant chatted
last. In a multi-user (familia) install, that meant heartbeat-triggered
``ask_principal`` and memory tools ran under the wrong actor — system-tick
semantics belong to the configured owner, not the most-recent talker.

Fix: ``HeartbeatConfig.target_actor`` (config) pins the principal; familia
defaults it from ``FAMILIA_OWNER_ACTOR`` when blank. Resolution to a
concrete (channel, chat_id) lives in ``cli/commands.py`` and uses
``familia.principals.get_registry``.
"""

from __future__ import annotations

import pytest

from familia import bootstrap as familia_bootstrap
from familia import principals as principals_mod
from familia.principals import Identity, Principal, PrincipalRegistry


class _HBStub:
    def __init__(self, target_actor: str = "") -> None:
        self.target_actor = target_actor


@pytest.fixture
def env_owner(monkeypatch: pytest.MonkeyPatch):
    def _set(value: str | None) -> None:
        if value is None:
            monkeypatch.delenv("FAMILIA_OWNER_ACTOR", raising=False)
        else:
            monkeypatch.setenv("FAMILIA_OWNER_ACTOR", value)
    return _set


def test_apply_defaults_fills_from_env(env_owner):
    env_owner("owner")
    hb = _HBStub(target_actor="")
    familia_bootstrap.apply_heartbeat_defaults(hb)
    assert hb.target_actor == "owner"


def test_apply_defaults_respects_explicit_config(env_owner):
    """User-pinned config wins — env never overrides explicit value."""
    env_owner("owner")
    hb = _HBStub(target_actor="member_a")
    familia_bootstrap.apply_heartbeat_defaults(hb)
    assert hb.target_actor == "member_a"


def test_apply_defaults_strips_whitespace_only(env_owner):
    env_owner("owner")
    hb = _HBStub(target_actor="   ")
    familia_bootstrap.apply_heartbeat_defaults(hb)
    assert hb.target_actor == "owner"


def test_apply_defaults_no_env_no_change(env_owner):
    env_owner(None)
    hb = _HBStub(target_actor="")
    familia_bootstrap.apply_heartbeat_defaults(hb)
    assert hb.target_actor == ""


def test_apply_defaults_strips_env_whitespace(env_owner):
    env_owner("  owner  ")
    hb = _HBStub(target_actor="")
    familia_bootstrap.apply_heartbeat_defaults(hb)
    assert hb.target_actor == "owner"


# ---- Resolution semantics (mirrors _pick_heartbeat_target's lookup) ----

@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> PrincipalRegistry:
    reg = PrincipalRegistry([
        Principal(id="owner", display_name="O", identities=[
            Identity(channel="vk", sender_id="1000001"),
            Identity(channel="tg", sender_id="2000001"),
        ], memx_key="k1", roles=["admin"]),
        Principal(id="member_a", display_name="A", identities=[
            Identity(channel="vk", sender_id="1000002"),
        ], memx_key="k2", roles=[]),
        Principal(id="ghost", display_name="G", identities=[],
                  memx_key="k3", roles=[]),
    ])
    monkeypatch.setattr(principals_mod, "_registry", reg)
    return reg


def _resolve(target_actor: str, enabled: set[str]) -> tuple[str, str] | None:
    """Mirror the resolution branch in cli.commands._pick_heartbeat_target."""
    from familia.principals import get_registry
    p = get_registry().get(target_actor)
    if p is None:
        return None
    for ident in p.identities:
        if ident.channel in enabled and ident.sender_id:
            return ident.channel, str(ident.sender_id)
    return None


def test_resolve_owner_to_vk(registry):
    assert _resolve("owner", {"vk"}) == ("vk", "1000001")


def test_resolve_picks_first_enabled_channel(registry):
    # Owner has both vk and tg; only tg enabled — should pick tg.
    assert _resolve("owner", {"tg"}) == ("tg", "2000001")


def test_resolve_unknown_actor(registry):
    assert _resolve("nosuch", {"vk"}) is None


def test_resolve_actor_with_no_identities(registry):
    assert _resolve("ghost", {"vk"}) is None


def test_resolve_actor_with_no_enabled_channel(registry):
    # member_a is only on vk; enabled = {tg} — no match.
    assert _resolve("member_a", {"tg"}) is None


def test_resolve_first_identity_wins_with_multiple_enabled_channels(registry):
    """When both channels are enabled, identity-list order decides.

    The principal in the fixture has identities=[vk, tg]. With both channels
    enabled, the first match (vk) wins. This pins the contract so a future
    refactor that reorders identities surfaces in CI rather than silently
    flipping the heartbeat target between restarts.
    """
    assert _resolve("owner", {"vk", "tg"}) == ("vk", "1000001")


# ---- make_principal_chat_validator (HIGH finding: cron 'to' injection) ----

def test_validator_accepts_known_identity(registry):
    validate = familia_bootstrap.make_principal_chat_validator()
    assert validate("vk", "1000001") is True
    assert validate("vk", "1000002") is True
    assert validate("tg", "2000001") is True


def test_validator_rejects_unknown_chat_id(registry):
    validate = familia_bootstrap.make_principal_chat_validator()
    assert validate("vk", "999999") is False


def test_validator_rejects_wrong_channel_for_known_chat_id(registry):
    """Same chat_id on a different channel must not match."""
    validate = familia_bootstrap.make_principal_chat_validator()
    # 1000001 is owner's vk id; owner has no slack identity.
    assert validate("slack", "1000001") is False


def test_validator_rejects_empty_inputs(registry):
    validate = familia_bootstrap.make_principal_chat_validator()
    assert validate("", "1000001") is False
    assert validate("vk", "") is False
