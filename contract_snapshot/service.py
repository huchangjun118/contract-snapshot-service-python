"""Contract service: orchestrates collect, judge, approve and rollback."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .collector import Collector
from .comparator import diff_shapes, verdict
from .config import Config, load_config
from .models import (
    BaselineHistoryEntry,
    CollectResult,
    CompatibilityReport,
    EndpointIndex,
    RawCapture,
    VersionRecord,
)
from .normalization import compute_shape, endpoint_key, snapshot_fingerprint
from .storage import StorageError, SnapshotStorage


class ContractService:
    def __init__(self, config: Config | None = None, storage: SnapshotStorage | None = None) -> None:
        self.config = config or load_config()
        self.storage = storage or SnapshotStorage(self.config.data_dir)
        self.collector = Collector(self.storage, self.config)

    # ---- helpers ---------------------------------------------------------
    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _endpoint_parts(self, ep_key: str) -> tuple[str, str]:
        method, path = ep_key.split(":", 1)
        return method, path

    def _register_version(self, ep_key: str, version_id: str, capture: RawCapture) -> None:
        with self.storage.endpoint_lock(ep_key, exclusive=True):
            index = self.storage.read_index(ep_key)
            if index is None:
                method, path = self._endpoint_parts(ep_key)
                index = EndpointIndex(endpoint_key=ep_key, method=method, path=path)
            if version_id not in index.versions:
                index.versions.append(version_id)
            index.updated_at = self._now()
            self.storage.write_index(index)

    # ---- collection ------------------------------------------------------
    def collect(self, capture: RawCapture) -> CollectResult:
        result = self.collector.collect(capture)
        if result.status == "collected" and result.version_id:
            try:
                self._register_version(result.endpoint_key, result.version_id, capture)
            except StorageError as exc:
                return CollectResult(
                    status="storage_error",
                    endpoint_key=result.endpoint_key,
                    version_id=result.version_id,
                    error=str(exc),
                )
        return result

    # ---- judgment --------------------------------------------------------
    def judge_capture(self, capture: RawCapture) -> CompatibilityReport:
        """Normalize a capture and judge it against the approved baseline (no persistence)."""
        normalized, error = self.collector.normalize_only(capture)
        if error:
            return CompatibilityReport(status="error", error=error)
        shape = compute_shape(normalized)
        version_id = snapshot_fingerprint(shape)
        ep_key = endpoint_key(
            capture.method, capture.path, self.config.normalization.normalize_path_ids
        )
        return self._judge(ep_key, version_id, shape)

    def judge_version(self, endpoint_key: str, version_id: str) -> CompatibilityReport:
        """Judge an already-collected version against the current baseline."""
        try:
            candidate = self.storage.read_version(version_id)
        except KeyError:
            return CompatibilityReport(
                status="error", error=f"version {version_id} not found"
            )
        except StorageError as exc:
            return CompatibilityReport(status="error", error=str(exc))
        return self._judge(endpoint_key, version_id, candidate.shape)

    def _judge(
        self, ep_key: str, candidate_version_id: str, candidate_shape: dict[str, Any]
    ) -> CompatibilityReport:
        try:
            with self.storage.endpoint_lock(ep_key, exclusive=False):
                index = self.storage.read_index(ep_key)
                if index is None or index.baseline_version_id is None:
                    return CompatibilityReport(
                        status="no_baseline", candidate_version_id=candidate_version_id
                    )
                baseline = self.storage.read_version(index.baseline_version_id)
        except KeyError:
            return CompatibilityReport(
                status="error",
                error=f"baseline version {index.baseline_version_id} is missing",
                candidate_version_id=candidate_version_id,
            )
        except StorageError as exc:
            return CompatibilityReport(status="error", error=str(exc))

        changes = diff_shapes(baseline.shape, candidate_shape)
        return CompatibilityReport(
            status=verdict(changes),
            changes=changes,
            baseline_version_id=baseline.version_id,
            candidate_version_id=candidate_version_id,
        )

    # ---- baseline promotion ----------------------------------------------
    def approve(self, endpoint_key: str, version_id: str, actor: str = "approve") -> EndpointIndex:
        with self.storage.endpoint_lock(endpoint_key, exclusive=True):
            try:
                if not self.storage.version_exists(version_id):
                    raise KeyError(version_id)
                index = self.storage.read_index(endpoint_key)
                if index is None:
                    method, path = self._endpoint_parts(endpoint_key)
                    index = EndpointIndex(endpoint_key=endpoint_key, method=method, path=path)
                if version_id not in index.versions:
                    index.versions.append(version_id)
                previous_sequence = index.baseline_history[-1].sequence if index.baseline_history else 0
                index.baseline_version_id = version_id
                index.baseline_history.append(
                    BaselineHistoryEntry(
                        sequence=previous_sequence + 1,
                        version_id=version_id,
                        action="approve" if actor == "approve" else actor,
                        committed_at=self._now(),
                    )
                )
                index.updated_at = self._now()
                self.storage.write_index(index)
            except KeyError:
                raise
            except StorageError:
                raise
        return index

    def rollback(self, endpoint_key: str, version_id: str) -> EndpointIndex:
        with self.storage.endpoint_lock(endpoint_key, exclusive=True):
            try:
                index = self.storage.read_index(endpoint_key)
                if index is None or version_id not in index.versions:
                    raise KeyError(version_id)
                if not self.storage.version_exists(version_id):
                    raise KeyError(version_id)
                previous_sequence = index.baseline_history[-1].sequence if index.baseline_history else 0
                index.baseline_version_id = version_id
                index.baseline_history.append(
                    BaselineHistoryEntry(
                        sequence=previous_sequence + 1,
                        version_id=version_id,
                        action="rollback",
                        committed_at=self._now(),
                    )
                )
                index.updated_at = self._now()
                self.storage.write_index(index)
            except KeyError:
                raise
            except StorageError:
                raise
        return index

    # ---- reads -----------------------------------------------------------
    def list_endpoints(self) -> list[EndpointIndex]:
        result = []
        for ep_key in self.storage.list_endpoints():
            with self.storage.endpoint_lock(ep_key, exclusive=False):
                index = self.storage.read_index(ep_key)
            if index is not None:
                result.append(index)
        return result

    def get_baseline(self, endpoint_key: str) -> VersionRecord | None:
        with self.storage.endpoint_lock(endpoint_key, exclusive=False):
            index = self.storage.read_index(endpoint_key)
            if index is None or index.baseline_version_id is None:
                return None
            try:
                return self.storage.read_version(index.baseline_version_id)
            except KeyError:
                return None

    def list_versions(self, endpoint_key: str) -> list[VersionRecord]:
        with self.storage.endpoint_lock(endpoint_key, exclusive=False):
            index = self.storage.read_index(endpoint_key)
            if index is None:
                return []
            versions = []
            for version_id in index.versions:
                try:
                    versions.append(self.storage.read_version(version_id))
                except KeyError:
                    continue
            return versions
