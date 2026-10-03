"""Collector: turn raw captures into persisted, content-addressed contract versions."""
from __future__ import annotations

import fnmatch
import hashlib
from datetime import datetime, timezone
from typing import Any

from .config import Config, config_hash
from .models import CollectResult, RawCapture, VersionRecord
from .normalization import (
    NormalizationError,
    compute_shape,
    endpoint_key,
    normalize_capture,
    snapshot_fingerprint,
)
from .storage import StorageError, SnapshotStorage


class Collector:
    def __init__(self, storage: SnapshotStorage, config: Config) -> None:
        self.storage = storage
        self.config = config

    def in_scope(self, capture: RawCapture) -> tuple[bool, str | None]:
        """Return (True, None) if the capture should be collected, else (False, reason)."""
        collection = self.config.collection
        if not collection.enabled:
            return False, "collection_disabled"
        allowed_methods = {method.upper() for method in collection.methods}
        if capture.method.upper() not in allowed_methods:
            return False, "method_not_allowed"
        key = endpoint_key(capture.method, capture.path, self.config.normalization.normalize_path_ids)
        if not any(fnmatch.fnmatchcase(key, pattern) for pattern in collection.include_endpoints):
            return False, "not_in_include_scope"
        if any(fnmatch.fnmatchcase(key, pattern) for pattern in collection.exclude_endpoints):
            return False, "excluded"
        return True, None

    def sample(self, endpoint_key: str, version_id: str) -> bool:
        """Deterministic per-contract sampling: same contract is always sampled in or out."""
        rate = self.config.collection.sample_rate
        if rate >= 1.0:
            return True
        if rate <= 0.0:
            return False
        digest = hashlib.sha256(f"{endpoint_key}:{version_id}".encode("utf-8")).hexdigest()
        value = int(digest[:8], 16) / 0xFFFFFFFF
        return value < rate

    def collect(self, capture: RawCapture) -> CollectResult:
        """Normalize and persist a capture. Never pollutes the baseline on failure."""
        in_scope, reason = self.in_scope(capture)
        if not in_scope:
            return CollectResult(status="ignored", reason=reason)

        try:
            normalized = normalize_capture(capture, self.config.normalization)
        except NormalizationError as exc:
            return CollectResult(status="parse_error", error=str(exc))
        except (TypeError, ValueError) as exc:
            return CollectResult(status="parse_error", error=f"cannot normalize: {exc}")

        shape = compute_shape(normalized)
        version_id = snapshot_fingerprint(shape)
        ep_key = endpoint_key(capture.method, capture.path, self.config.normalization.normalize_path_ids)

        if not self.sample(ep_key, version_id):
            return CollectResult(
                status="ignored", reason="sampled_out", endpoint_key=ep_key, version_id=version_id
            )

        try:
            if self.storage.version_exists(version_id):
                return CollectResult(
                    status="duplicate",
                    endpoint_key=ep_key,
                    version_id=version_id,
                    is_new_version=False,
                )
            record = VersionRecord(
                version_id=version_id,
                endpoint_key=ep_key,
                method=capture.method.upper(),
                path=normalized["path"],
                normalized=normalized,
                shape=shape,
                collected_at=datetime.now(timezone.utc).isoformat(),
                config_hash=config_hash(self.config),
                sample_count=1,
            )
            self.storage.write_version(record)
        except StorageError as exc:
            return CollectResult(
                status="storage_error",
                endpoint_key=ep_key,
                version_id=version_id,
                error=str(exc),
            )

        return CollectResult(
            status="collected",
            endpoint_key=ep_key,
            version_id=version_id,
            is_new_version=True,
        )

    def normalize_only(self, capture: RawCapture) -> tuple[dict[str, Any] | None, str | None]:
        """Normalize without persisting. Returns (normalized, error)."""
        try:
            normalized = normalize_capture(capture, self.config.normalization)
        except NormalizationError as exc:
            return None, str(exc)
        return normalized, None
