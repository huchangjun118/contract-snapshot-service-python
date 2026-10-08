"""归一化：把真实请求/响应负载转换为可比较的规范形式。

保证：
- 字段顺序、无关空白不引起快照变化（字典按键排序、字符串去首尾空白）；
- 时间戳、请求标识、分页游标等易变字段被替换为固定占位符；
- 同一契约在不同时间、不同进程下采集得到同一份规范形式
  （不依赖哈希随机化、不依赖字典插入序、不依赖浮点表示差异）。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

from .config import NormalizeRules
from .paths import matches_any

# 易变字段归一化后的占位符
_VOLATILE_PLACEHOLDER = "<volatile>"


def normalize_payload(payload: Any, rules: NormalizeRules) -> Any:
    """将负载归一化为规范形式。

    纯函数：相同输入在任何进程、任何时间得到相同输出。
    """
    return _normalize(payload, rules, ())


def _normalize(value: Any, rules: NormalizeRules, path: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key in sorted(value.keys(), key=str):
            skey = str(key)
            child = path + (skey,)
            if matches_any(rules.ignore_paths, child):
                continue  # 配置的忽略路径：完全不进入快照
            if matches_any(rules.volatile_paths, child):
                out[skey] = _VOLATILE_PLACEHOLDER  # 易变字段：保留结构、抹掉取值
                continue
            out[skey] = _normalize(value[key], rules, child)
        return out
    if isinstance(value, (list, tuple)):
        # 数组不增加路径段：元素内字段的路径与父路径一致
        items = [_normalize(item, rules, path) for item in value]
        if rules.ignore_array_order:
            # 数组顺序不敏感：按各元素的规范序列化结果排序，
            # 保证不同顺序的同一集合得到同一规范形式。
            items.sort(key=canonical_dumps)
        return items
    if isinstance(value, str):
        # 无关空白不引起变化
        return value.strip()
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        # 消除浮点表示差异（1.0 与 1 视为同一取值）
        if value.is_integer():
            return int(value)
        return value
    return value


def canonical_dumps(value: Any) -> str:
    """确定性序列化：排序键、固定分隔符、无随机因素。

    用于计算快照哈希与数组排序键，进程间结果一致。
    """
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str
    )


def snapshot_hash(normalized: Any) -> str:
    """对规范形式计算内容哈希，作为快照身份。"""
    return hashlib.sha256(canonical_dumps(normalized).encode("utf-8")).hexdigest()
