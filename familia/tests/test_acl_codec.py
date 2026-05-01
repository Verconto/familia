"""Adversarial round-trip tests for the wrapped-value codec (SR-4, SR-10).

The decoder must accept ONLY values it produced, and refuse anything
else — including JSON-shaped legacy strings that *look* tagged. The
positive paths are easy; the negative paths are where SR-4 lives, so
they get the bulk of the test surface.
"""

from __future__ import annotations

import json

from familia.acl.codec import decode, encode


# ---- positive: round-trip ---------------------------------------------------

def test_round_trip_preserves_value():
    raw = encode("hello world", ["varya", "school"])
    rec = decode(raw)
    assert rec is not None
    assert rec.value == "hello world"
    assert rec.tags == ("varya", "school")


def test_round_trip_preserves_unicode():
    raw = encode("Купить тетради", ["varya"])
    rec = decode(raw)
    assert rec is not None
    assert rec.value == "Купить тетради"


def test_round_trip_empty_tags():
    raw = encode("text", [])
    rec = decode(raw)
    assert rec is not None
    assert rec.tags == ()


def test_round_trip_dedupes_tags_preserve_order():
    raw = encode("v", ["a", "b", "a", "  b  ", "c"])
    rec = decode(raw)
    assert rec is not None
    assert rec.tags == ("a", "b", "c")


def test_round_trip_drops_non_string_tags_at_encode():
    raw = encode("v", ["a", None, 42, "b"])  # type: ignore[list-item]
    rec = decode(raw)
    assert rec is not None
    assert rec.tags == ("a", "b")


def test_round_trip_preserves_value_that_is_itself_json():
    """Inner ``value`` may be JSON; decoder must not double-parse."""
    inner = '{"foo": 1, "bar": [2, 3]}'
    raw = encode(inner, ["t"])
    rec = decode(raw)
    assert rec is not None
    assert rec.value == inner


# ---- negative: legacy detection -- adversarial inputs (SR-4) ---------------

def test_decode_legacy_plain_string_returns_none():
    assert decode("plain string, not JSON") is None


def test_decode_legacy_number_returns_none():
    assert decode("42") is None


def test_decode_legacy_array_returns_none():
    assert decode("[1, 2, 3]") is None


def test_decode_legacy_dict_without_sentinel_returns_none():
    """The CRITICAL adversarial case: pre-feature value that happens to
    have ``tags`` and ``value`` keys but no sentinel.  Must be legacy."""
    body = json.dumps({"tags": ["admin"], "value": "secret"})
    assert decode(body) is None


def test_decode_sentinel_with_wrong_value_is_legacy():
    """Sentinel must be literal Python ``True`` (JSON ``true``)."""
    bad_sentinels = [
        json.dumps({"__familia_acl_v1": False, "tags": [], "value": "v"}),
        json.dumps({"__familia_acl_v1": "true", "tags": [], "value": "v"}),
        json.dumps({"__familia_acl_v1": 1, "tags": [], "value": "v"}),
        json.dumps({"__familia_acl_v1": None, "tags": [], "value": "v"}),
    ]
    for raw in bad_sentinels:
        assert decode(raw) is None, raw


def test_decode_missing_tags_is_legacy():
    body = json.dumps({"__familia_acl_v1": True, "value": "v"})
    assert decode(body) is None


def test_decode_tags_not_list_is_legacy():
    body = json.dumps({"__familia_acl_v1": True, "tags": "scalar", "value": "v"})
    assert decode(body) is None


def test_decode_non_string_tag_in_array_rejects_whole():
    """Even one non-string tag → entire wrapper rejected (fail-closed)."""
    body = json.dumps({"__familia_acl_v1": True, "tags": ["ok", 42], "value": "v"})
    assert decode(body) is None


def test_decode_missing_value_is_legacy():
    body = json.dumps({"__familia_acl_v1": True, "tags": []})
    assert decode(body) is None


def test_decode_value_not_string_is_legacy():
    body = json.dumps({"__familia_acl_v1": True, "tags": [], "value": 42})
    assert decode(body) is None


def test_decode_invalid_json_returns_none():
    assert decode("{this is not valid json}") is None


def test_decode_non_string_input_returns_none():
    assert decode(None) is None  # type: ignore[arg-type]
    assert decode(b"bytes") is None  # type: ignore[arg-type]


def test_encoded_form_is_stable_json_object():
    """Sanity: the on-disk shape is the documented one — JSON object,
    sentinel + tags + value keys, nothing more."""
    raw = encode("v", ["x"])
    parsed = json.loads(raw)
    assert isinstance(parsed, dict)
    assert set(parsed.keys()) == {"__familia_acl_v1", "tags", "value"}
    assert parsed["__familia_acl_v1"] is True
