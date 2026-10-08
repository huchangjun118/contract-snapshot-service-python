"""接口契约快照与兼容性回归能力。"""
from .config import CaptureConfig, NormalizeRules, PathMatcher, load_config
from .errors import ContractError, FailureKind
from .parser import parse_raw_message
from .service import CaptureResult, ContractService
from .store import SnapshotStore, default_db_path

__all__ = [
    "ContractError",
    "FailureKind",
    "CaptureConfig",
    "NormalizeRules",
    "PathMatcher",
    "load_config",
    "parse_raw_message",
    "CaptureResult",
    "ContractService",
    "SnapshotStore",
    "default_db_path",
]
