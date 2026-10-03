"""Configuration for collection scope and normalization rules.

See docs/configuration.md for the full list of items.
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class CollectionConfig:
    enabled: bool = True
    sample_rate: float = 1.0
    include_endpoints: list[str] = field(default_factory=lambda: ["*"])
    exclude_endpoints: list[str] = field(default_factory=list)
    methods: list[str] = field(default_factory=lambda: ["GET", "POST", "PUT", "PATCH", "DELETE"])


@dataclass
class NormalizationConfig:
    volatile_fields: list[str] = field(
        default_factory=lambda: [
            # generic volatile field names
            "timestamp",
            "request_id",
            "requestId",
            "trace_id",
            "traceId",
            "span_id",
            "spanId",
            "correlation_id",
            "correlationId",
            "nonce",
            # pagination cursors
            "cursor",
            "next_cursor",
            "nextCursor",
            "previous_cursor",
            "previousCursor",
            "page_token",
            "pageToken",
            "next_page_token",
            "nextPageToken",
            "offset",
        ]
    )
    volatile_query_params: list[str] = field(
        default_factory=lambda: [
            "cursor",
            "next_cursor",
            "nextCursor",
            "page_token",
            "pageToken",
            "timestamp",
            "request_id",
            "trace_id",
            "nonce",
        ]
    )
    strip_headers: list[str] = field(
        default_factory=lambda: [
            "x-request-id",
            "x-trace-id",
            "x-span-id",
            "x-correlation-id",
            "date",
            "etag",
            "last-modified",
            "if-modified-since",
            "if-none-match",
        ]
    )
    normalize_path_ids: bool = True


@dataclass
class Config:
    collection: CollectionConfig = field(default_factory=CollectionConfig)
    normalization: NormalizationConfig = field(default_factory=NormalizationConfig)
    data_dir: str = "./contracts_data"
    source: str = "default"


def default_config() -> Config:
    return Config()


def _merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in overrides.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: str | None = None) -> Config:
    """Load configuration from a JSON file.

    Resolution order: explicit path > CONTRACT_SNAPSHOT_CONFIG env var >
    ./contracts.config.json (if present) > built-in defaults.
    """
    config_path = path or os.environ.get("CONTRACT_SNAPSHOT_CONFIG")
    if config_path is None and os.path.exists("contracts.config.json"):
        config_path = "contracts.config.json"

    cfg = default_config()
    cfg.source = "default"
    if config_path:
        with open(config_path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        data_dir = raw.pop("data_dir", None)
        merged = _merge(asdict(cfg), raw)
        cfg = Config(
            collection=CollectionConfig(**merged["collection"]),
            normalization=NormalizationConfig(**merged["normalization"]),
            data_dir=data_dir or merged.get("data_dir", "./contracts_data"),
            source=os.path.abspath(config_path),
        )
    env_data_dir = os.environ.get("CONTRACT_SNAPSHOT_DATA_DIR")
    if env_data_dir:
        cfg.data_dir = env_data_dir
    return cfg


def config_hash(config: Config) -> str:
    """Stable hash of the effective configuration.

    Recorded with every collected version so that the rules in force at
    collection time are detectable; stored versions are never re-interpreted.
    """
    payload = {
        "collection": asdict(config.collection),
        "normalization": asdict(config.normalization),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
