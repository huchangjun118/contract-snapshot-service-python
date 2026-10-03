"""采集范围与归一化规则的配置。

配置只影响「之后」的采集与判定；已固化的快照在写入时记录了
生成它所用的归一化规则指纹，历史基线与历史判定结果不会因
配置变更而被重新解释。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

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

        快照写入时记录该指纹；比较两份快照前校验指纹一致，
        从而保证配置变更不会让历史结果被重新解释。
        """
        material = {
            "ignore_paths": sorted(self.ignore_paths),
            "volatile_paths": sorted(self.volatile_paths),
            "ignore_array_order": self.ignore_array_order,
        }
        blob = json.dumps(material, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class CaptureConfig:
    """采集配置。

    - sampling_rate: 采样率 [0.0, 1.0]，0 表示关闭采集。
    - rules: 归一化规则。
    """

    sampling_rate: float = 1.0
    rules: NormalizeRules = field(default_factory=NormalizeRules)

    def __post_init__(self) -> None:
        if not 0.0 <= self.sampling_rate <= 1.0:
            from .errors import ContractError, FailureKind

            raise ContractError(
                FailureKind.INVALID_CONFIG,
                f"sampling_rate 必须在 [0.0, 1.0] 内，得到 {self.sampling_rate}",
            )
