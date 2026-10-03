"""归一化语义测试：字段顺序、空白、易变字段不得引起快照变化。"""
from contract_snapshot.config import NormalizeRules
from contract_snapshot.normalize import normalize_payload, snapshot_hash


def test_field_order_and_whitespace_irrelevant():
    rules = NormalizeRules()
    a = {"b": 1, "a": "  hello  ", "c": {"y": 2, "x": 1}}
    b = {"c": {"x": 1, "y": 2}, "a": "hello", "b": 1}
    assert normalize_payload(a, rules) == normalize_payload(b, rules)
    assert snapshot_hash(normalize_payload(a, rules)) == snapshot_hash(
        normalize_payload(b, rules)
    )


def test_volatile_fields_masked():
    rules = NormalizeRules()
    a = {"id": 1, "timestamp": "2026-01-01T00:00:00Z", "request_id": "r-1"}
    b = {"id": 1, "timestamp": "2030-12-31T23:59:59Z", "request_id": "r-999"}
    assert snapshot_hash(normalize_payload(a, rules)) == snapshot_hash(
        normalize_payload(b, rules)
    )


def test_cursor_and_pagination_volatile():
    rules = NormalizeRules()
    a = {"items": [1, 2], "next_cursor": "abc"}
    b = {"items": [1, 2], "next_cursor": "xyz"}
    assert normalize_payload(a, rules) == normalize_payload(b, rules)


def test_array_order_insensitive_by_default():
    rules = NormalizeRules()
    a = {"items": [3, 1, 2]}
    b = {"items": [2, 3, 1]}
    assert normalize_payload(a, rules) == normalize_payload(b, rules)


def test_array_order_sensitive_when_configured():
    rules = NormalizeRules(ignore_array_order=False)
    a = {"items": [3, 1, 2]}
    b = {"items": [2, 3, 1]}
    assert normalize_payload(a, rules) != normalize_payload(b, rules)


def test_ignore_paths_removed():
    rules = NormalizeRules(ignore_paths=frozenset({"debug"}))
    a = {"id": 1, "debug": {"anything": True}}
    b = {"id": 1}
    assert normalize_payload(a, rules) == normalize_payload(b, rules)


def test_deterministic_across_calls():
    rules = NormalizeRules()
    payload = {"z": [1, {"k": "v"}], "a": {"n": None}}
    first = snapshot_hash(normalize_payload(payload, rules))
    for _ in range(20):
        assert snapshot_hash(normalize_payload(payload, rules)) == first


def test_float_integer_equivalence():
    rules = NormalizeRules()
    assert normalize_payload({"v": 1.0}, rules) == normalize_payload({"v": 1}, rules)
