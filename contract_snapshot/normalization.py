"""Normalization: turn raw captures into canonical, comparable contracts.

See docs/normalization.md for the full semantics.
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from typing import Any
from urllib.parse import parse_qsl

from .config import NormalizationConfig

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_HEX_ID_RE = re.compile(r"^[0-9a-fA-F]{16,}$")


class NormalizationError(Exception):
    """Raised when a capture cannot be normalized (e.g. unparseable JSON body)."""


def canonical_json(value: Any) -> str:
    """Compact JSON with recursively sorted keys; the canonical form of a contract."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _is_id_segment(segment: str) -> bool:
    if segment.isdigit():
        return True
    if _UUID_RE.match(segment):
        return True
    if _HEX_ID_RE.match(segment):
        return True
    return False


def normalize_path(path: str, normalize_ids: bool = True) -> str:
    """Normalize the path portion: collapse numeric/uuid segments to {id}."""
    segments = path.split("/")
    out = []
    for segment in segments:
        if normalize_ids and segment and _is_id_segment(segment):
            out.append("{id}")
        else:
            out.append(segment)
    return "/".join(out)


def endpoint_key(method: str, path: str, normalize_ids: bool = True) -> str:
    """Logical endpoint identity: METHOD + normalized path."""
    path_only = path.split("?", 1)[0]
    return f"{method.upper()}:{normalize_path(path_only, normalize_ids)}"


def _matches_volatile(key_path: str, patterns: list[str]) -> bool:
    key = key_path.split(".")[-1]
    for pattern in patterns:
        if pattern == key:
            return True
        if fnmatch.fnmatchcase(key_path, pattern) or fnmatch.fnmatchcase(key, pattern):
            return True
    return False


def strip_volatile(value: Any, patterns: list[str], prefix: str = "") -> Any:
    """Remove volatile fields from a JSON value, recursively.

    Field order is irrelevant because dicts are re-serialized with sorted keys
    by canonical_json; whitespace is irrelevant because the canonical form is
    compact. Volatile fields (timestamps, request ids, cursors, ...) are dropped
    so that two captures of the same contract produce the same snapshot.
    """
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            if _matches_volatile(path, patterns):
                continue
            out[key] = strip_volatile(item, patterns, path)
        return out
    if isinstance(value, list):
        return [strip_volatile(item, patterns, f"{prefix}[]") for item in value]
    return value


def _lower_headers(headers: dict[str, str]) -> dict[str, str]:
    return {str(key).lower(): value for key, value in headers.items()}


def content_type(headers: dict[str, str]) -> str | None:
    """Extract the media type (without parameters) from a header map."""
    lowered = _lower_headers(headers)
    raw = lowered.get("content-type")
    if raw is None:
        return None
    media = raw.split(";", 1)[0].strip().lower()
    return media or None


def parse_body(body: Any, media_type: str | None) -> tuple[Any, str | None]:
    """Parse a body into JSON-compatible data. Returns (data, error_message).

    Bodies that already are JSON-native (dict/list) are returned as-is.
    Strings/bytes are parsed only when the media type indicates JSON; an
    unparseable JSON body yields an error (callers must treat it as a failure,
    never as "no change").
    """
    if body is None:
        return None, None
    if isinstance(body, (dict, list)):
        return body, None
    if isinstance(body, (bytes, bytearray)):
        try:
            body = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            return None, f"body is not valid UTF-8: {exc}"
    if isinstance(body, str):
        if body == "":
            return None, None
        if media_type and "json" in media_type:
            try:
                return json.loads(body), None
            except json.JSONDecodeError as exc:
                return None, (
                    f"invalid JSON body: {exc.msg} at line {exc.lineno} column {exc.colno}"
                )
        return body, None
    return None, f"unsupported body type: {type(body).__name__}"


def normalize_query(path: str, volatile_params: list[str]) -> dict[str, str]:
    """Parse the query string, sort parameters, and drop volatile ones."""
    if "?" not in path:
        return {}
    query_string = path.split("?", 1)[1]
    out: dict[str, str] = {}
    for key, value in sorted(parse_qsl(query_string, keep_blank_values=True)):
        if _matches_volatile(key, volatile_params):
            continue
        out[key] = value
    return out


def normalize_capture(capture, config: NormalizationConfig) -> dict[str, Any]:
    """Build the canonical snapshot structure for a raw capture.

    Raises NormalizationError when the capture cannot be normalized.
    """
    req_media = content_type(capture.request_headers)
    resp_media = content_type(capture.response_headers)

    req_body, req_err = parse_body(capture.request_body, req_media)
    resp_body, resp_err = parse_body(capture.response_body, resp_media)
    if req_err:
        raise NormalizationError(f"request body: {req_err}")
    if resp_err:
        raise NormalizationError(f"response body: {resp_err}")

    path_only = capture.path.split("?", 1)[0]
    normalized_path = normalize_path(path_only, config.normalize_path_ids)
    query = normalize_query(capture.path, config.volatile_query_params)

    req_body = strip_volatile(req_body, config.volatile_fields)
    resp_body = strip_volatile(resp_body, config.volatile_fields)

    return {
        "method": capture.method.upper(),
        "path": normalized_path,
        "request": {
            "content_type": req_media,
            "query": query,
            "body": req_body,
        },
        "responses": {
            str(capture.status_code): {
                "content_type": resp_media,
                "body": resp_body,
            }
        },
    }


def _merge_two_shapes(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    if a == b:
        return a
    type_a, type_b = a.get("type"), b.get("type")
    if type_a == "object" and type_b == "object":
        props: dict[str, Any] = dict(a.get("properties", {}))
        for key, value in b.get("properties", {}).items():
            props[key] = _merge_two_shapes(props[key], value) if key in props else value
        return {"type": "object", "properties": props}
    if type_a == "array" and type_b == "array":
        return {"type": "array", "items": _merge_two_shapes(a.get("items", {}), b.get("items", {}))}
    types: set[str] = set()
    for shape in (a, b):
        if shape.get("type") == "mixed":
            types.update(shape.get("types", []))
        else:
            types.add(shape.get("type"))
    return {"type": "mixed", "types": sorted(types)}


def shape_of(value: Any) -> dict[str, Any]:
    """Derive the structural shape of a JSON value (no actual values)."""
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, (int, float)):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if isinstance(value, list):
        if not value:
            return {"type": "array", "items": {"type": "unknown"}}
        items = shape_of(value[0])
        for item in value[1:]:
            items = _merge_two_shapes(items, shape_of(item))
        return {"type": "array", "items": items}
    if isinstance(value, dict):
        return {
            "type": "object",
            "properties": {key: shape_of(value[key]) for key in sorted(value.keys())},
        }
    return {"type": "unknown"}


def compute_shape(normalized: dict[str, Any]) -> dict[str, Any]:
    """Derive the structural shape of a normalized snapshot."""
    request = normalized.get("request", {}) or {}
    response_shapes: dict[str, Any] = {}
    for status, response in (normalized.get("responses", {}) or {}).items():
        response_shapes[status] = {
            "content_type": shape_of((response or {}).get("content_type")),
            "body": shape_of((response or {}).get("body")),
        }
    return {
        "request": {
            "content_type": shape_of(request.get("content_type")),
            "query": shape_of(request.get("query")),
            "body": shape_of(request.get("body")),
        },
        "responses": response_shapes,
    }


def snapshot_fingerprint(shape: dict[str, Any]) -> str:
    """Content-addressed id: identical contract structures share the same id.

    The fingerprint is based on the *shape* (structure), not on concrete
    values, so two captures of the same contract with different field values
    (e.g. different resource ids) are deduplicated to one version.
    """
    encoded = canonical_json(shape)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
