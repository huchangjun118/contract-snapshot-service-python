"""Tests for compatibility judgment rules."""
from __future__ import annotations

import pytest

from contract_snapshot.comparator import (
    KIND_ADDED,
    KIND_REMOVED,
    KIND_TYPE_CHANGED,
    diff_shapes,
    is_breaking,
    verdict,
)
from contract_snapshot.normalization import compute_shape


def _shape(body, status="200", method="POST", path="/api/items", query=None,
           req_body=None, content_type="application/json"):
    normalized = {
        "method": method,
        "path": path,
        "request": {
            "content_type": content_type,
            "query": query or {},
            "body": req_body,
        },
        "responses": {
            status: {
                "content_type": content_type,
                "body": body,
            }
        },
    }
    return compute_shape(normalized)


def _verdict(old_body, new_body, **kwargs):
    old_shape = _shape(old_body, **kwargs)
    new_shape = _shape(new_body, **kwargs)
    changes = diff_shapes(old_shape, new_shape)
    return verdict(changes), changes


class TestFieldChanges:
    def test_added_field_is_compatible(self):
        v, changes = _verdict({"a": 1}, {"a": 1, "b": 2})
        assert v == "compatible"
        added = [c for c in changes if c.kind == KIND_ADDED]
        assert any(c.path == "responses.200.body.b" for c in added)
        assert all(not c.breaking for c in added)

    def test_removed_field_is_breaking(self):
        v, changes = _verdict({"a": 1, "b": 2}, {"a": 1})
        assert v == "breaking"
        removed = [c for c in changes if c.kind == KIND_REMOVED]
        assert any(c.path == "responses.200.body.b" for c in removed)
        assert all(c.breaking for c in removed)

    def test_type_change_is_breaking(self):
        v, changes = _verdict({"a": 1}, {"a": "string"})
        assert v == "breaking"
        changed = [c for c in changes if c.kind == KIND_TYPE_CHANGED]
        assert any(c.path == "responses.200.body.a" for c in changed)
        assert all(c.breaking for c in changed)

    def test_nested_changes(self):
        v, changes = _verdict(
            {"outer": {"inner": {"a": 1}}},
            {"outer": {"inner": {"a": 1, "b": 2}}},
        )
        assert v == "compatible"
        assert any(c.path == "responses.200.body.outer.inner.b" for c in changes)

    def test_array_element_changes(self):
        v, changes = _verdict(
            {"items": [{"a": 1}]},
            {"items": [{"a": 1, "b": 2}]},
        )
        assert v == "compatible"
        assert any("items" in c.path and c.kind == KIND_ADDED for c in changes)

    def test_array_element_type_change(self):
        v, changes = _verdict(
            {"items": [{"a": 1}]},
            {"items": [{"a": "x"}]},
        )
        assert v == "breaking"
        assert any("items" in c.path and c.kind == KIND_TYPE_CHANGED for c in changes)


class TestStatusCodeChanges:
    def test_added_status_compatible(self):
        old = _shape({"a": 1}, status="200")
        new = _shape({"a": 1}, status="200")
        new["responses"]["201"] = new["responses"]["200"]
        changes = diff_shapes(old, new)
        assert verdict(changes) == "compatible"
        assert any(c.path == "responses.201" and c.kind == KIND_ADDED for c in changes)

    def test_removed_status_breaking(self):
        old = _shape({"a": 1}, status="200")
        old["responses"]["201"] = old["responses"]["200"]
        new = _shape({"a": 1}, status="200")
        changes = diff_shapes(old, new)
        assert verdict(changes) == "breaking"
        assert any(c.path == "responses.201" and c.kind == KIND_REMOVED for c in changes)


class TestRequestChanges:
    def test_request_added_field_compatible(self):
        old = _shape(None, req_body={"a": 1})
        new = _shape(None, req_body={"a": 1, "b": 2})
        changes = diff_shapes(old, new)
        assert verdict(changes) == "compatible"
        assert any(c.path == "request.body.b" and c.kind == KIND_ADDED for c in changes)

    def test_request_removed_field_breaking(self):
        old = _shape(None, req_body={"a": 1, "b": 2})
        new = _shape(None, req_body={"a": 1})
        changes = diff_shapes(old, new)
        assert verdict(changes) == "breaking"
        assert any(c.path == "request.body.b" and c.kind == KIND_REMOVED for c in changes)

    def test_query_param_added_compatible(self):
        old = _shape(None, query={"a": "1"})
        new = _shape(None, query={"a": "1", "b": "2"})
        changes = diff_shapes(old, new)
        assert verdict(changes) == "compatible"

    def test_query_param_removed_breaking(self):
        old = _shape(None, query={"a": "1", "b": "2"})
        new = _shape(None, query={"a": "1"})
        changes = diff_shapes(old, new)
        assert verdict(changes) == "breaking"
        assert any(c.path == "request.query.b" and c.kind == KIND_REMOVED for c in changes)


class TestDeterminism:
    def test_same_input_same_verdict(self):
        old = _shape({"a": 1, "b": {"c": [1, 2]}})
        new = _shape({"a": 1, "b": {"c": [1, 2, 3], "d": "x"}})
        v1, _ = _verdict(old, new)
        v2, _ = _verdict(old, new)
        assert v1 == v2

    def test_no_changes_compatible(self):
        old = _shape({"a": 1})
        new = _shape({"a": 1})
        changes = diff_shapes(old, new)
        assert changes == []
        assert verdict([]) == "compatible"


class TestBreakingKinds:
    def test_is_breaking(self):
        from contract_snapshot.models import FieldChange

        assert is_breaking(FieldChange("x", KIND_REMOVED, "string", None, True))
        assert is_breaking(FieldChange("x", KIND_TYPE_CHANGED, "string", "number", True))
        assert not is_breaking(FieldChange("x", KIND_ADDED, None, "string", False))
