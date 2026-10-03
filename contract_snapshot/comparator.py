"""Compatibility judgment between two contract shapes.

See docs/compatibility.md for the rule set.
"""
from __future__ import annotations

from typing import Any

from .models import FieldChange

KIND_ADDED = "added"
KIND_REMOVED = "removed"
KIND_TYPE_CHANGED = "type_changed"

BREAKING_KINDS = {KIND_REMOVED, KIND_TYPE_CHANGED}

_ROOT_FIELDS = ("request", "responses")


def _type_label(shape: dict[str, Any]) -> str:
    kind = shape.get("type")
    if kind == "mixed":
        return "|".join(shape.get("types", [])) or "mixed"
    return kind or "unknown"


def _diff_shape_nodes(old: dict[str, Any], new: dict[str, Any], path: str) -> list[FieldChange]:
    """Diff two shape_of() nodes (objects, arrays, scalars)."""
    changes: list[FieldChange] = []
    old_type = old.get("type")
    new_type = new.get("type")

    if old_type == "object" and new_type == "object":
        old_props = old.get("properties", {}) or {}
        new_props = new.get("properties", {}) or {}
        for key in sorted(set(old_props) | set(new_props)):
            child_path = f"{path}.{key}" if path else key
            if key not in old_props:
                changes.append(
                    FieldChange(child_path, KIND_ADDED, None, _type_label(new_props[key]), False)
                )
            elif key not in new_props:
                changes.append(
                    FieldChange(child_path, KIND_REMOVED, _type_label(old_props[key]), None, True)
                )
            else:
                changes.extend(_diff_shape_nodes(old_props[key], new_props[key], child_path))
        return changes

    if old_type == "array" and new_type == "array":
        return _diff_shape_nodes(
            old.get("items", {}) or {"type": "unknown"},
            new.get("items", {}) or {"type": "unknown"},
            path,
        )

    old_label = _type_label(old)
    new_label = _type_label(new)
    if old_label != new_label and "unknown" not in (old_label, new_label):
        changes.append(FieldChange(path or "$", KIND_TYPE_CHANGED, old_label, new_label, True))
    return changes


def _diff_request(old_request: dict[str, Any], new_request: dict[str, Any]) -> list[FieldChange]:
    changes: list[FieldChange] = []
    for field in ("content_type", "query", "body"):
        old_shape = old_request.get(field) or {"type": "null"}
        new_shape = new_request.get(field) or {"type": "null"}
        changes.extend(_diff_shape_nodes(old_shape, new_shape, f"request.{field}"))
    return changes


def _diff_responses(old_responses: dict[str, Any], new_responses: dict[str, Any]) -> list[FieldChange]:
    changes: list[FieldChange] = []
    for status in sorted(set(old_responses) | set(new_responses)):
        if status not in old_responses:
            changes.append(FieldChange(f"responses.{status}", KIND_ADDED, None, "response", False))
        elif status not in new_responses:
            changes.append(FieldChange(f"responses.{status}", KIND_REMOVED, "response", None, True))
        else:
            old_response = old_responses[status] or {}
            new_response = new_responses[status] or {}
            for field in ("content_type", "body"):
                old_shape = old_response.get(field) or {"type": "null"}
                new_shape = new_response.get(field) or {"type": "null"}
                changes.extend(
                    _diff_shape_nodes(old_shape, new_shape, f"responses.{status}.{field}")
                )
    return changes


def diff_shapes(old_shape: dict[str, Any], new_shape: dict[str, Any], path: str = "") -> list[FieldChange]:
    """Recursively diff two shapes. Deterministic: same inputs always yield the same result."""
    changes: list[FieldChange] = []
    if any(field in old_shape or field in new_shape for field in _ROOT_FIELDS):
        changes.extend(_diff_request(old_shape.get("request") or {}, new_shape.get("request") or {}))
        changes.extend(
            _diff_responses(old_shape.get("responses") or {}, new_shape.get("responses") or {})
        )
    else:
        changes.extend(_diff_shape_nodes(old_shape, new_shape, path))
    changes.sort(key=lambda change: change.path)
    return changes


def is_breaking(change: FieldChange) -> bool:
    return change.kind in BREAKING_KINDS


def verdict(changes: list[FieldChange]) -> str:
    """Return 'breaking' if any breaking change exists, else 'compatible'."""
    return "breaking" if any(is_breaking(change) for change in changes) else "compatible"
