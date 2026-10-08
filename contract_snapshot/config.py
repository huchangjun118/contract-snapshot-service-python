"""采集范围与归一化规则的配置。

配置来源与优先级（高到低）：
1. 环境变量（CONTRACT_SNAPSHOT_*）；
2. 配置文件（TOML，路径由 CONTRACT_SNAPSHOT_CONFIG 指定，
   缺省查找 ./contract-snapshot.toml）；
3. 代码默认值。

配置只影响「之后」的采集与判定；已固化的快照在写入时记录了
生成它所用的归一化规则本体与指纹，历史基线与历史判定结果不会因
配置变更而被重新解释。
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# 默认的易变字段路径：时间戳、请求标识、分页游标等。
# 这些字段在不同时间/进程下取值不同，不参与快照比较。
DEFAULT_VOLATILE_PATHS: frozenset[str] = frozenset(
    {
        "timestamp",
        "request_id",
        "requestId",
        "trace_id",
        "traceId",
        "cursor",
        "next_cursor",
        "nextCursor",
        "page_cursor",
    }
)


@dataclass(frozen=True)
class NormalizeRules:
    """归一化规则。

    - ignore_paths: 完全忽略（不进入快照）的字段名集合，按字段名匹配。
    - volatile_paths: 易变字段名集合，归一化时替换为占位符，
      结构保留但取值不参与比较。
    - ignore_array_order: 数组元素是否按规范序排序（默认是，
      分页结果等顺序不敏感的列表不会因顺序变化产生差异）。
    """

    ignore_paths: frozenset[str] = frozenset()
    volatile_paths: frozenset[str] = DEFAULT_VOLATILE_PATHS
    ignore_array_order: bool = True

    def fingerprint(self) -> str:
        """规则指纹：同一套规则在任何进程下得到同一指纹。

        快照写入时记录该指纹与规则本体；比较两份快照前校验指纹一致，
        指纹不一致时用基线侧落库的规则重新归一化候选再比较，
        从而保证配置变更不会让历史结果被重新解释。
        """
        blob = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict:
        """序列化为可落库的确定性字典。"""
        return {
            "ignore_paths": sorted(self.ignore_paths),
            "volatile_paths": sorted(self.volatile_paths),
            "ignore_array_order": self.ignore_array_order,
        }

    @staticmethod
    def from_dict(data: dict) -> "NormalizeRules":
        """从落库字典还原规则；缺失键取默认值（兼容旧记录）。"""
        return NormalizeRules(
            ignore_paths=frozenset(data.get("ignore_paths", ())),
            volatile_paths=frozenset(
                data.get("volatile_paths", DEFAULT_VOLATILE_PATHS)
            ),
            ignore_array_order=bool(data.get("ignore_array_order", True)),
        )


@dataclass(frozen=True)
class CaptureConfig:
    """采集配置。

    - sampling_rate: 采样率 [0.0, 1.0]，0 表示关闭采集。
    - include_endpoints: 采集范围，fnmatch 风格模式（如 ``/api/*``）。
      空元组表示采集全部接口；非空时不在范围内的接口不产生任何版本。
    - db_path: 持久化位置；None 表示使用默认落盘位置（见 default_db_path）。
    - rules: 归一化规则。
    """

    sampling_rate: float = 1.0
    include_endpoints: tuple[str, ...] = ()
    db_path: str | None = None
    rules: NormalizeRules = field(default_factory=NormalizeRules)

    def __post_init__(self) -> None:
        if not 0.0 <= self.sampling_rate <= 1.0:
            from .errors import ContractError, FailureKind

            raise ContractError(
                FailureKind.INVALID_CONFIG,
                f"sampling_rate 必须在 [0.0, 1.0] 内，得到 {self.sampling_rate}",
            )

    def endpoint_in_scope(self, endpoint: str) -> bool:
        """接口是否落在采集范围内。范围为空表示全部采集。"""
        if not self.include_endpoints:
            return True
        return any(
            fnmatch.fnmatchcase(endpoint, pat) for pat in self.include_endpoints
        )

    def resolved_db_path(self) -> str:
        """实际落盘位置：显式配置优先，否则用默认位置。"""
        return self.db_path or default_db_path()


def default_db_path() -> str:
    """默认落盘位置：用户目录下的固定文件，未显式配置时也可跨重启保留数据。"""
    return str(Path.home() / ".contract-snapshot" / "snapshots.db")


# ---- 配置文件与环境变量 -----------------------------------------------

_ENV_PREFIX = "CONTRACT_SNAPSHOT_"
_DEFAULT_CONFIG_FILE = "contract-snapshot.toml"


def _split_list(raw: str) -> list[str]:
    return [item.strip() for item in raw.split(",") if item.strip()]


def load_config(
    path: str | None = None,
    env: dict[str, str] | None = None,
) -> CaptureConfig:
    """从配置文件与运行环境加载配置。环境变量优先于配置文件。

    环境变量：
    - CONTRACT_SNAPSHOT_CONFIG           配置文件路径
    - CONTRACT_SNAPSHOT_DB               持久化位置
    - CONTRACT_SNAPSHOT_SAMPLING_RATE    采样率（0 表示关闭采集）
    - CONTRACT_SNAPSHOT_INCLUDE_ENDPOINTS  采集范围，逗号分隔的模式
    - CONTRACT_SNAPSHOT_IGNORE_PATHS     忽略路径，逗号分隔
    - CONTRACT_SNAPSHOT_VOLATILE_PATHS   易变路径，逗号分隔
    - CONTRACT_SNAPSHOT_IGNORE_ARRAY_ORDER  true/false
    """
    env = os.environ if env is None else env
    data: dict = {}
    cfg_path = (
        path
        or env.get(_ENV_PREFIX + "CONFIG")
        or ( _DEFAULT_CONFIG_FILE if Path(_DEFAULT_CONFIG_FILE).is_file() else None )
    )
    if cfg_path:
        with open(cfg_path, "rb") as fh:
            data = tomllib.load(fh)

    rules_data = data.get("rules", {})

    def pick(env_key: str, file_value, default=None):
        """环境变量优先，其次配置文件，最后默认值。"""
        v = env.get(_ENV_PREFIX + env_key)
        return v if v is not None else (file_value if file_value is not None else default)

    sampling_raw = pick("SAMPLING_RATE", data.get("sampling_rate"), 1.0)
    include_raw = pick("INCLUDE_ENDPOINTS", data.get("include_endpoints"), [])
    ignore_raw = pick("IGNORE_PATHS", rules_data.get("ignore_paths"), [])
    volatile_raw = pick("VOLATILE_PATHS", rules_data.get("volatile_paths"), None)
    order_raw = pick("IGNORE_ARRAY_ORDER", rules_data.get("ignore_array_order"), True)
    db_raw = pick("DB", data.get("db_path"), None)

    def as_list(v):
        if isinstance(v, str):
            return _split_list(v)
        return list(v)

    def as_bool(v):
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on")
        return bool(v)

    return CaptureConfig(
        sampling_rate=float(sampling_raw),
        include_endpoints=tuple(as_list(include_raw)),
        db_path=str(db_raw) if db_raw else None,
        rules=NormalizeRules(
            ignore_paths=frozenset(as_list(ignore_raw)),
            volatile_paths=(
                frozenset(as_list(volatile_raw))
                if volatile_raw is not None
                else DEFAULT_VOLATILE_PATHS
            ),
            ignore_array_order=as_bool(order_raw),
        ),
    )
