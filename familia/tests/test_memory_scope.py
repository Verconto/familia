"""Unit tests for ``familia.tools.memory._resolve_full_key``.

The fix (memory pair-scope idempotency) addresses a real bug seen in
production audit logs: the LLM would call
``memory_get(scope="pair:owner_member_a")`` (canonical pair form, copied
from a stored value) and the tool re-sorted
``[actor="member_a", "owner_member_a"]`` producing
``pair:member_a_owner_member_a:upcoming`` — which policy denied on every
heartbeat tick, breaking the upcoming-events check. The fix accepts both
``pair:<other_id>`` and ``pair:<a>_<b>`` shapes.
"""

from __future__ import annotations

import pytest

from familia import principals as principals_mod
from familia.principals import Identity, Principal, PrincipalRegistry
from familia.tools.memory import _resolve_full_key


@pytest.fixture
def registry(monkeypatch: pytest.MonkeyPatch) -> PrincipalRegistry:
    reg = PrincipalRegistry([
        Principal(id="owner", display_name="O", identities=[
            Identity(channel="vk", sender_id="1000001"),
        ], memx_key="k1", roles=["admin"]),
        Principal(id="member_a", display_name="A", identities=[
            Identity(channel="vk", sender_id="1000002"),
        ], memx_key="k2", roles=[]),
        Principal(id="member_b", display_name="B", identities=[],
                  memx_key="k3", roles=[]),
    ])
    monkeypatch.setattr(principals_mod, "_registry", reg)
    return reg


def test_shared(registry):
    full, err = _resolve_full_key("shared", "todo", "member_a")
    assert err is None
    assert full == "shared:todo"


def test_private(registry):
    full, err = _resolve_full_key("private", "feels", "member_a")
    assert err is None
    assert full == "private:member_a:feels"


def test_pair_other_id_form(registry):
    full, err = _resolve_full_key("pair:owner", "upcoming", "member_a")
    assert err is None
    assert full == "pair:member_a_owner:upcoming"


def test_pair_canonical_form_idempotent(registry):
    # The bug case: LLM passes already-sorted "pair:a_b".
    full, err = _resolve_full_key("pair:member_a_owner", "upcoming", "member_a")
    assert err is None
    assert full == "pair:member_a_owner:upcoming"


def test_pair_canonical_form_from_other_actor(registry):
    # Same canonical name resolves identically when called by the other peer.
    full, err = _resolve_full_key("pair:member_a_owner", "upcoming", "owner")
    assert err is None
    assert full == "pair:member_a_owner:upcoming"


def test_pair_argument_order_canonicalizes(registry):
    full, err = _resolve_full_key("pair:member_a", "x", "owner")
    assert err is None
    assert full == "pair:member_a_owner:x"


def test_pair_self_rejected(registry):
    _, err = _resolve_full_key("pair:member_a", "x", "member_a")
    assert err is not None
    assert "different principal" in err


def test_pair_unknown_principal_rejected(registry):
    _, err = _resolve_full_key("pair:nosuch", "x", "member_a")
    assert err is not None
    assert "unknown principal" in err.lower()


def test_pair_unrelated_canonical_rejected(registry):
    # "pair:owner_member_b" — actor member_a is not in the pair.
    _, err = _resolve_full_key("pair:owner_member_b", "x", "member_a")
    assert err is not None
    assert "unknown principal" in err.lower()


def test_pair_random_underscored_string_rejected(registry):
    # Stress the canonical-form scan: bogus "a_b_c" should fall through to
    # 'unknown principal', not silently succeed or hit the self-pair branch.
    _, err = _resolve_full_key("pair:a_b_c", "x", "member_a")
    assert err is not None
    assert "unknown principal" in err.lower()


def test_pair_empty_other_rejected(registry):
    _, err = _resolve_full_key("pair:", "x", "member_a")
    assert err is not None


def test_unknown_scope_rejected(registry):
    _, err = _resolve_full_key("weird", "x", "member_a")
    assert err is not None


def test_empty_key_rejected(registry):
    _, err = _resolve_full_key("shared", "", "member_a")
    assert err is not None
