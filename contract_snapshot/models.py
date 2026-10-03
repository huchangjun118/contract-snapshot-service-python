"""Data models shared across the contract snapshot pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RawCapture:
    """A single observed request/response pair."""

    method: str
    path: str
    status_code: int
    request_headers: dict[str, str] = field(default_factory=dict)
    request_body: Any = None
    response_headers: dict[str, str] = field(default_factory=dict)
    response_body: Any = None


@dataclass
class FieldChange:
    """A single structural difference between two contract shapes."""

    path: str
    kind: str  # "added" | "removed" | "type_changed"
    old_type: str | None
    new_type: str | None
    breaking: bool


@dataclass
class CompatibilityReport:
    """Result of comparing a candidate contract against the approved baseline."""

    status: str  # "compatible" | "breaking" | "no_baseline" | "error"
    changes: list[FieldChange] = field(default_factory=list)
    baseline_version_id: str | None = None
    candidate_version_id: str | None = None
    error: str | None = None

    @property
    def breaking_changes(self) -> list[FieldChange]:
        ...


@dataclass
class VersionRecord:
    """An immutable, content-addressed contract snapshot."""

    version_id: str
    endpoint_key: str
    method: str
    path: str
    normalized: dict[str, Any]
    shape: dict[str, Any]
    collected_at: str
    config_hash: str
    sample_count: int = 1


@dataclass
class BaselineHistoryEntry:
    """A committed baseline promotion (approve or rollback)."""

    sequence: int
    version_id: str
    action: str  # "approve" | "rollback"
    committed_at: str


@dataclass
class EndpointIndex:
    """Per-endpoint state. Written atomically; readers always see a complete file."""

    endpoint_key: str
    method: str
    path: str
    baseline_version_id: str | None = None
    versions: list[str] = field(default_factory=list)
    baseline_history: list[BaselineHistoryEntry] = field(default_factory=list)
    updated_at: str = ""


@dataclass
class CollectResult:
    status: str  # "collected" | "duplicate" | "ignored" | "parse_error" | "storage_error"
    endpoint_key: str | None = None
    version_id: str | None = None
    is_new_version: bool = False
    reason: str | None = None
    error: str | None = None
