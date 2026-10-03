"""Persistent storage for versions and endpoint indexes.

Design guarantees:
- Version files are immutable and content-addressed; writes are atomic
  (temp file + fsync + rename), so a reader never observes a half-written file.
- The endpoint index (baseline pointer + history) is updated under an exclusive
  cross-process lock plus atomic rename, so approve/rollback are serialized and
  every read returns a complete, committed index.
- Hot paths (collect / judge) never scan history: versions are addressed by id.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
import threading
from typing import Any, Iterator

from .models import BaselineHistoryEntry, EndpointIndex, VersionRecord


class StorageError(Exception):
    """Raised when persistence is unavailable (disk, permissions, ...)."""


def _slug(endpoint_key: str) -> str:
    safe = re.sub(r"[^a-zA-Z0-9]", "_", endpoint_key)
    digest = hashlib.sha256(endpoint_key.encode("utf-8")).hexdigest()[:8]
    return f"{safe}_{digest}"


class SnapshotStorage:
    def __init__(self, data_dir: str) -> None:
        self.data_dir = data_dir
        self.versions_dir = os.path.join(data_dir, "versions")
        self.endpoints_dir = os.path.join(data_dir, "endpoints")
        self._locks_guard = threading.Lock()
        self._in_process_locks: dict[str, threading.RLock] = {}
        try:
            os.makedirs(self.versions_dir, exist_ok=True)
            os.makedirs(self.endpoints_dir, exist_ok=True)
        except OSError as exc:
            raise StorageError(f"cannot initialize storage at {data_dir}: {exc}") from exc

    # ---- paths -----------------------------------------------------------
    def version_path(self, version_id: str) -> str:
        return os.path.join(self.versions_dir, f"{version_id}.json")

    def index_path(self, endpoint_key: str) -> str:
        return os.path.join(self.endpoints_dir, f"{_slug(endpoint_key)}.json")

    def lock_path(self, endpoint_key: str) -> str:
        return os.path.join(self.endpoints_dir, f"{_slug(endpoint_key)}.lock")

    # ---- locking ---------------------------------------------------------
    @contextlib.contextmanager
    def endpoint_lock(self, endpoint_key: str, exclusive: bool = True) -> Iterator[None]:
        """Cross-process (flock) + in-process (threading) lock for an endpoint."""
        with self._locks_guard:
            lock = self._in_process_locks.setdefault(endpoint_key, threading.RLock())
        with lock:
            fd: int | None = None
            try:
                fd = os.open(self.lock_path(endpoint_key), os.O_RDWR | os.O_CREAT, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                yield
            except OSError as exc:
                raise StorageError(f"lock failed for {endpoint_key}: {exc}") from exc
            finally:
                if fd is not None:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    finally:
                        os.close(fd)

    # ---- atomic writes ---------------------------------------------------
    def _atomic_write_json(self, path: str, payload: dict[str, Any]) -> None:
        directory = os.path.dirname(path)
        fd: int | None = None
        tmp_path: str | None = None
        try:
            fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fd = None
                json.dump(payload, fh, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, path)
            tmp_path = None
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError as exc:
            raise StorageError(f"atomic write failed for {path}: {exc}") from exc
        finally:
            if fd is not None:
                os.close(fd)
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    # ---- versions --------------------------------------------------------
    def version_exists(self, version_id: str) -> bool:
        return os.path.isfile(self.version_path(version_id))

    def write_version(self, record: VersionRecord) -> None:
        payload = {
            "version_id": record.version_id,
            "endpoint_key": record.endpoint_key,
            "method": record.method,
            "path": record.path,
            "normalized": record.normalized,
            "shape": record.shape,
            "collected_at": record.collected_at,
            "config_hash": record.config_hash,
            "sample_count": record.sample_count,
        }
        self._atomic_write_json(self.version_path(record.version_id), payload)

    def read_version(self, version_id: str) -> VersionRecord:
        path = self.version_path(version_id)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except FileNotFoundError:
            raise KeyError(version_id)
        except OSError as exc:
            raise StorageError(f"cannot read version {version_id}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise StorageError(f"corrupt version file {version_id}: {exc}") from exc
        return VersionRecord(
            version_id=payload["version_id"],
            endpoint_key=payload["endpoint_key"],
            method=payload["method"],
            path=payload["path"],
            normalized=payload["normalized"],
            shape=payload["shape"],
            collected_at=payload["collected_at"],
            config_hash=payload["config_hash"],
            sample_count=payload.get("sample_count", 1),
        )

    # ---- indexes ---------------------------------------------------------
    def read_index(self, endpoint_key: str) -> EndpointIndex | None:
        path = self.index_path(endpoint_key)
        try:
            with open(path, "r", encoding="utf-8") as fh:
                payload = json.load(fh)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise StorageError(f"cannot read index for {endpoint_key}: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise StorageError(f"corrupt index for {endpoint_key}: {exc}") from exc
        history = [
            BaselineHistoryEntry(
                sequence=entry["sequence"],
                version_id=entry["version_id"],
                action=entry["action"],
                committed_at=entry["committed_at"],
            )
            for entry in payload.get("baseline_history", [])
        ]
        return EndpointIndex(
            endpoint_key=payload["endpoint_key"],
            method=payload["method"],
            path=payload["path"],
            baseline_version_id=payload.get("baseline_version_id"),
            versions=list(payload.get("versions", [])),
            baseline_history=history,
            updated_at=payload.get("updated_at", ""),
        )

    def write_index(self, index: EndpointIndex) -> None:
        payload = {
            "endpoint_key": index.endpoint_key,
            "method": index.method,
            "path": index.path,
            "baseline_version_id": index.baseline_version_id,
            "versions": index.versions,
            "baseline_history": [
                {
                    "sequence": entry.sequence,
                    "version_id": entry.version_id,
                    "action": entry.action,
                    "committed_at": entry.committed_at,
                }
                for entry in index.baseline_history
            ],
            "updated_at": index.updated_at,
        }
        self._atomic_write_json(self.index_path(index.endpoint_key), payload)

    def list_endpoints(self) -> list[str]:
        try:
            names = os.listdir(self.endpoints_dir)
        except OSError as exc:
            raise StorageError(f"cannot list endpoints: {exc}") from exc
        keys: list[str] = []
        for name in names:
            if not name.endswith(".json"):
                continue
            path = os.path.join(self.endpoints_dir, name)
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    payload = json.load(fh)
                keys.append(payload["endpoint_key"])
            except (OSError, json.JSONDecodeError, KeyError):
                continue
        return sorted(keys)
