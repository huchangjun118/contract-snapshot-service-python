"""Tests for normalization semantics."""
from __future__ import annotations

import pytest

from contract_snapshot.config import Config, NormalizationConfig
from contract_snapshot.models import RawCapture
from contract_snapshot.normalization import (
    NormalizationError,
    compute_shape,
    endpoint_key,
    normalize_capture,
    normalize_path,
    normalize_query,
    parse_body,
    snapshot_fingerprint,
    strip_volatile,
)


def _config(**overrides) -> NormalizationConfig:
    cfg = NormalizationConfig()
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


def _capture(**overrides) -> RawCapture:
    defaults = dict(
        method="POST",
        path="/api/items",
        status_code=200,
        request_headers={"content-type": "application/json"},
        request_body={"name": "widget"},
        response_headers={"content-type": "application/json"},
        response_body={"id": 1, "name": "widget"},
    )
    defaults.update(overrides)
    return RawCapture(**defaults)


class TestCanonicalForm:
    def test_field_order_irrelevant(self):
        a = _capture(request_body={"b": 1, "a": 2}, response_body={"z": 1, "y": 2})
        b = _capture(request_body={"a": 2, "b": 1}, response_body={"y": 2, "z": 1})
        na = normalize_capture(a, _config())
        nb = normalize_capture(b, _config())
        assert snapshot_fingerprint(compute_shape(na)) == snapshot_fingerprint(compute_shape(nb))

    def test_whitespace_irrelevant(self):
        a = _capture(request_body='{\n  "name": "widget",\n  "qty": 1\n}',
                     response_body='{\n  "id": 1,\n  "name": "widget"\n}')
        b = _capture(request_body='{"name":"widget","qty":1}',
                     response_body='{"id":1,"name":"widget"}')
        na = normalize_capture(a, _config())
        nb = normalize_capture(b, _config())
        assert snapshot_fingerprint(compute_shape(na)) == snapshot_fingerprint(compute_shape(nb))

    def test_volatile_fields_stripped(self):
        a = _capture(request_body={"name": "widget", "timestamp": "2024-01-01T00:00:00Z",
                                   "request_id": "req-1", "trace_id": "tr-1"},
                     response_body={"id": 1, "name": "widget", "timestamp": "2024-01-01T00:00:00Z"})
        b = _capture(request_body={"name": "widget", "timestamp": "2024-02-02T00:00:00Z",
                                   "request_id": "req-2", "trace_id": "tr-2"},
                     response_body={"id": 2, "name": "widget", "timestamp": "2024-02-02T00:00:00Z"})
        na = normalize_capture(a, _config())
        nb = normalize_capture(b, _config())
        assert snapshot_fingerprint(compute_shape(na)) == snapshot_fingerprint(compute_shape(nb))
        # volatile fields are absent from the normalized form
        assert "timestamp" not in na["request"]["body"]
        assert "request_id" not in na["request"]["body"]
        assert "timestamp" not in na["responses"]["200"]["body"]

    def test_volatile_nested_fields_stripped(self):
        value = {"outer": {"inner": {"timestamp": "x", "keep": 1}}, "ts": "y"}
        result = strip_volatile(value, ["timestamp", "ts"])
        assert result == {"outer": {"inner": {"keep": 1}}}

    def test_volatile_glob_pattern(self):
        value = {"meta": {"trace_id": "x", "span_id": "y", "keep": 1}, "trace_id": "z"}
        result = strip_volatile(value, ["*.trace_id", "span_id", "trace_id"])
        assert result == {"meta": {"keep": 1}}


class TestPathNormalization:
    def test_numeric_segments(self):
        assert normalize_path("/api/items/123") == "/api/items/{id}"
        assert normalize_path("/api/items/123/reviews/456") == "/api/items/{id}/reviews/{id}"

    def test_uuid_segments(self):
        assert normalize_path("/items/550e8400-e29b-41d4-a716-446655440000") == "/items/{id}"

    def test_hex_id_segments(self):
        assert normalize_path("/items/507f1f77bcf86cd799439011") == "/items/{id}"

    def test_non_id_segments_kept(self):
        assert normalize_path("/api/items") == "/api/items"
        assert normalize_path("/api/v1/items") == "/api/v1/items"

    def test_disabled(self):
        assert normalize_path("/api/items/123", normalize_ids=False) == "/api/items/123"

    def test_endpoint_key(self):
        assert endpoint_key("POST", "/api/items/123?cursor=x") == "POST:/api/items/{id}"
        assert endpoint_key("GET", "/api/items") == "GET:/api/items"


class TestQueryNormalization:
    def test_volatile_query_params_stripped(self):
        q = normalize_query("/api/items?cursor=abc&limit=10&timestamp=1", ["cursor", "timestamp"])
        assert q == {"limit": "10"}

    def test_no_query(self):
        assert normalize_query("/api/items", []) == {}

    def test_params_sorted(self):
        q = normalize_query("/api/items?b=2&a=1", [])
        assert list(q.keys()) == ["a", "b"]


class TestBodyParsing:
    def test_dict_body_passthrough(self):
        data, err = parse_body({"a": 1}, "application/json")
        assert data == {"a": 1}
        assert err is None

    def test_string_body_parsed_for_json(self):
        data, err = parse_body('{"a": 1}', "application/json")
        assert data == {"a": 1}
        assert err is None

    def test_invalid_json_body_error(self):
        data, err = parse_body("{invalid", "application/json")
        assert data is None
        assert err is not None
        assert "invalid JSON" in err

    def test_text_body_kept_as_string(self):
        data, err = parse_body("hello", "text/plain")
        assert data == "hello"
        assert err is None

    def test_empty_body(self):
        data, err = parse_body("", "application/json")
        assert data is None
        assert err is None

    def test_none_body(self):
        data, err = parse_body(None, "application/json")
        assert data is None
        assert err is None

    def test_bytes_body(self):
        data, err = parse_body(b'{"a": 1}', "application/json")
        assert data == {"a": 1}
        assert err is None


class TestNormalizeCapture:
    def test_invalid_request_body_raises(self):
        cap = _capture(request_body="{invalid")
        with pytest.raises(NormalizationError):
            normalize_capture(cap, _config())

    def test_invalid_response_body_raises(self):
        cap = _capture(response_body="{invalid")
        with pytest.raises(NormalizationError):
            normalize_capture(cap, _config())

    def test_deterministic_fingerprint(self):
        cap = _capture()
        cfg = _config()
        na = normalize_capture(cap, cfg)
        nb = normalize_capture(cap, cfg)
        assert snapshot_fingerprint(compute_shape(na)) == snapshot_fingerprint(compute_shape(nb))

    def test_different_structure_different_fingerprint(self):
        a = _capture(response_body={"id": 1, "name": "widget"})
        b = _capture(response_body={"id": 1, "name": "widget", "extra": True})
        na = normalize_capture(a, _config())
        nb = normalize_capture(b, _config())
        assert snapshot_fingerprint(compute_shape(na)) != snapshot_fingerprint(compute_shape(nb))


class TestArrayShape:
    def test_array_element_shape_merged(self):
        cap = _capture(response_body={"items": [{"a": 1}, {"a": 2, "b": "x"}]})
        normalized = normalize_capture(cap, _config())
        shape = compute_shape(normalized)
        items_shape = shape["responses"]["200"]["body"]["properties"]["items"]
        assert items_shape["type"] == "array"
        props = items_shape["items"]["properties"]
        assert props["a"]["type"] == "number"
        assert props["b"]["type"] == "string"

    def test_empty_array(self):
        cap = _capture(response_body={"items": []})
        normalized = normalize_capture(cap, _config())
        shape = compute_shape(normalized)
        items_shape = shape["responses"]["200"]["body"]["properties"]["items"]
        assert items_shape["type"] == "array"
