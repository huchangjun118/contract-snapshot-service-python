"""采集范围与归一化规则的配置。

配置只影响「之后」的采集与判定；已固化的快照在写入时记录了
生成它所用的归一化规则指纹，历史基线与历史判定结果不会因
配置变更而被重新解释。

配置来源与优先级（高 → 低）：
1. 进程环境变量（CONTRACT_SNAPSHOT_*）；
2. 配置文件（JSON，路径由环境变量 CONTRACT_SNAPSHOT_CONFIG
   或 load_config(config_path=...) 指定）；
3. 代码默认值。

环境变量只覆盖对应键，未设置的键沿用文件或默认值；
列表型变量（逗号分隔）一旦设置即为完整覆盖，不与文件合并。
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Mapping

# 默认的易变字段路径：时间戳、请求标识、分页游标等。
# 这些字段在不同时间/进程下取值不同，不参与快照比较。
# 不含 "." 与通配符的裸字段名按「任意深度下同名字段」匹配。
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


# ---- 路径通配匹配 -------------------------------------------------------

class PathMatcher:
    """嵌套路径通配匹配器，匹配「根 → 目标字段」的段序列。

    语法：
    - 段以 "." 分隔，如 ``response.data.debug``；
    - ``*`` 匹配恰好一个字段段（不能跨 "."）；
    - ``**`` 匹配零个或多个字段段（可跨层）；
    - 不含 "." 与通配符的裸名（如 ``timestamp``）匹配任意深度下
      的同名字段，等价于 ``**.timestamp``。
    """

    __slots__ = ("_compiled",)

    def __init__(self, patterns: frozenset[str] | set[str] | tuple[str, ...]) -> None:
        self._compiled = tuple(self._compile(p) for p in sorted(set(patterns)))

    @staticmethod
    def _compile(pattern: str) -> tuple[str, tuple[str, ...] | None, str | None]:
        if "." not in pattern and "*" not in pattern:
            return ("bare", None, pattern)
        return ("glob", tuple(pattern.split(".")), None)

    def matches(self, path: tuple[str, ...]) -> bool:
        if not path:
            return False
        for kind, segs, name in self._compiled:
            if kind == "bare":
                if path[-1] == name:
                    return True
            else:
                if _match_segments(segs, path):  # type: ignore[arg-type]
                    return True
        return False


def _match_segments(pattern: tuple[str, ...], path: tuple[str, ...]) -> bool:
    """段级 glob 匹配：``*`` 一个段，``**`` 零或多个段。确定性。"""
    m, n = len(pattern), len(path)
    # dp[i][j] = pattern[i:] 是否能匹配 path[j:]
    dp = [[False] * (n + 1) for _ in range(m + 1)]
    dp[m][n] = True
    for i in range(m - 1, -1, -1):
        if pattern[i] == "**":
            dp[i][n] = dp[i + 1][n]
        for j in range(n - 1, -1, -1):
            if pattern[i] == "**":
                dp[i][j] = dp[i + 1][j] or dp[i][j + 1]
            elif pattern[i] == "*" or pattern[i] == path[j]:
                dp[i][j] = dp[i + 1][j + 1]
    return dp[0][0]


# ---- 归一化规则 ---------------------------------------------------------

@dataclass(frozen=True)
class NormalizeRules:
    """归一化规则。

    - ignore_paths: 完全忽略（不进入快照）的字段路径集合，支持嵌套与通配；
    - volatile_paths: 易变字段路径集合，归一化时替换为占位符，
      结构保留但取值不参与比较；
    - ignore_array_order: 数组元素是否按规范序排序（默认是）。
    """

    ignore_paths: frozenset[str] = frozenset()
    volatile_paths: frozenset[str] = DEFAULT_VOLATILE_PATHS
    ignore_array_order: bool = True

    def fingerprint(self) -> str:
        """规则指纹：同一套规则在任何进程下得到同一指纹。"""
        material = {
            "ignore_paths": sorted(self.ignore_paths),
            "volatile_paths": sorted(self.volatile_paths),
            "ignore_array_order": self.ignore_array_order,
        }
        blob = json.dumps(material, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


# ---- 采集配置 -----------------------------------------------------------

@dataclass(frozen=True)
class CaptureConfig:
    """采集配置。

    - sampling_rate: 采样率 [0.0, 1.0]，0 表示关闭采集；
    - include_endpoints: 采集范围白名单（fnmatch 通配，* 可跨 "/"），
      空集合表示不限制；
    - exclude_endpoints: 采集范围黑名单，命中则不采集；
    - rules: 归一化规则。
    """

    sampling_rate: float = 1.0
    include_endpoints: frozenset[str] = frozenset()
    exclude_endpoints: frozenset[str] = frozenset()
    rules: NormalizeRules = field(default_factory=NormalizeRules)

    def __post_init__(self) -> None:
        if not 0.0 <= self.sampling_rate <= 1.0:
            from .errors import ContractError, FailureKind

            raise ContractError(
                FailureKind.INVALID_CONFIG,
                f"sampling_rate 必须在 [0.0, 1.0] 内，得到 {self.sampling_rate}",
            )

    def endpoint_in_scope(self, endpoint: str) -> bool:
        """接口是否在采集范围内：白名单为空表示全部放行，黑名单优先拒绝。"""
        if self.exclude_endpoints and any(
            fnmatchcase(endpoint, p) for p in self.exclude_endpoints
        ):
            return False
        if self.include_endpoints:
            return any(fnmatchcase(endpoint, p) for p in self.include_endpoints)
        return True


# ---- 配置文件 / 环境变量读取 --------------------------------------------

ENV_CONFIG_FILE = "CONTRACT_SNAPSHOT_CONFIG"
ENV_SAMPLING_RATE = "CONTRACT_SNAPSHOT_SAMPLING_RATE"
ENV_INCLUDE_ENDPOINTS = "CONTRACT_SNAPSHOT_INCLUDE_ENDPOINTS"
ENV_EXCLUDE_ENDPOINTS = "CONTRACT_SNAPSHOT_EXCLUDE_ENDPOINTS"
ENV_IGNORE_PATHS = "CONTRACT_SNAPSHOT_IGNORE_PATHS"
ENV_VOLATILE_PATHS = "CONTRACT_SNAPSHOT_VOLATILE_PATHS"
ENV_IGNORE_ARRAY_ORDER = "CONTRACT_SNAPSHOT_IGNORE_ARRAY_ORDER"


def _csv(value: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in value.split(",") if v.strip())


def _rules_from(data: Mapping) -> NormalizeRules:
    return NormalizeRules(
        ignore_paths=frozenset(data.get("ignore_paths", ())),
        volatile_paths=frozenset(
            data.get("volatile_paths", DEFAULT_VOLATILE_PATHS)
        ),
        ignore_array_order=bool(data.get("ignore_array_order", True)),
    )


def load_config(
    config_path: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> CaptureConfig:
    """按 文件 < 环境变量 的优先级加载配置；缺省来源用代码默认值。

    config_path 为 None 时，若环境变量 CONTRACT_SNAPSHOT_CONFIG 指向文件
    则读取该文件；都没有则仅使用默认值与环境变量。
    显式/环境指定的文件不存在或无法解析时抛 INVALID_CONFIG。
    """
    from .errors import ContractError, FailureKind

    environ = os.environ if env is None else env

    # 1) 配置文件（优先级低于环境变量）
    file_path = config_path or environ.get(ENV_CONFIG_FILE)
    file_data: dict = {}
    if file_path is not None:
        try:
            with open(file_path, "r", encoding="utf-8") as fh:
                loaded = json.load(fh)
            if not isinstance(loaded, dict):
                raise ValueError("配置文件根节点必须是 JSON 对象")
            file_data = loaded
        except (OSError, ValueError) as exc:
            raise ContractError(
                FailureKind.INVALID_CONFIG,
                f"配置文件无法加载 ({file_path}): {exc}",
            ) from exc

    rules_data = dict(file_data.get("rules", {}))

    sampling_rate = float(file_data.get("sampling_rate", 1.0))
    include = frozenset(file_data.get("include_endpoints", ()))
    exclude = frozenset(file_data.get("exclude_endpoints", ()))

    # 2) 环境变量覆盖（仅覆盖被显式设置的键）
    if ENV_SAMPLING_RATE in environ:
        try:
            sampling_rate = float(environ[ENV_SAMPLING_RATE])
        except ValueError as exc:
            raise ContractError(
                FailureKind.INVALID_CONFIG,
                f"环境变量 {ENV_SAMPLING_RATE} 不是数值: "
                f"{environ[ENV_SAMPLING_RATE]!r}",
            ) from exc
    if ENV_INCLUDE_ENDPOINTS in environ:
        include = frozenset(_csv(environ[ENV_INCLUDE_ENDPOINTS]))
    if ENV_EXCLUDE_ENDPOINTS in environ:
        exclude = frozenset(_csv(environ[ENV_EXCLUDE_ENDPOINTS]))
    if ENV_IGNORE_PATHS in environ:
        rules_data["ignore_paths"] = _csv(environ[ENV_IGNORE_PATHS])
    if ENV_VOLATILE_PATHS in environ:
        rules_data["volatile_paths"] = _csv(environ[ENV_VOLATILE_PATHS])
    if ENV_IGNORE_ARRAY_ORDER in environ:
        raw = environ[ENV_IGNORE_ARRAY_ORDER].strip().lower()
        if raw not in ("1", "0", "true", "false", "yes", "no"):
            raise ContractError(
                FailureKind.INVALID_CONFIG,
                f"环境变量 {ENV_IGNORE_ARRAY_ORDER} 必须是布尔值，得到 {raw!r}",
            )
        rules_data["ignore_array_order"] = raw in ("1", "true", "yes")

    try:
        return CaptureConfig(
            sampling_rate=sampling_rate,
            include_endpoints=include,
            exclude_endpoints=exclude,
            rules=_rules_from(rules_data),
        )
    except ContractError:
        raise
