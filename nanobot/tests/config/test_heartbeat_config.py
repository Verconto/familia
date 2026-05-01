"""HeartbeatConfig schema regression tests for ``target_actor`` field."""

from __future__ import annotations

from nanobot.config.schema import GatewayConfig, HeartbeatConfig


def test_target_actor_defaults_to_empty_string():
    cfg = HeartbeatConfig()
    assert cfg.target_actor == ""


def test_target_actor_accepts_string():
    cfg = HeartbeatConfig(target_actor="owner")
    assert cfg.target_actor == "owner"


def test_gateway_default_carries_empty_target_actor():
    cfg = GatewayConfig()
    assert cfg.heartbeat.target_actor == ""


def test_target_actor_serializes():
    cfg = HeartbeatConfig(target_actor="owner")
    dumped = cfg.model_dump()
    assert dumped["target_actor"] == "owner"
