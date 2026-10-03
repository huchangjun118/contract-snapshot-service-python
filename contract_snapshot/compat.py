"""兼容性判定：比较两份结构 schema，区分破坏性与非破坏性变更。

判定规则（确定性，相同输入必然得到相同结论，详见 docs/contract-snapshots.md）：

1. 完全一致 -> IDENTICAL（无变更）。
2. 新增字段（新 schema 有、旧 schema 无）-> 兼容（视为新增可选字段）。
3. 删除字段（旧 schema 有、新 schema 无）-> 破坏。
4. 标量类型演进：
   - 新类型集合是旧类型集合的超集（放宽，如 integer -> number）-> 兼容；
   - 是子集或交集不完整（收紧，如 number -> integer）-> 破坏；
   - 完全不相交（如 string -> boolean）-> 破坏。
5. 结构种类变化（object <-> array <-> scalar）-> 破坏。
6. 对象字段、数组元素递归适用以上规则；任一子节点破坏则整体破坏。
"""
from __future__ import annotations

from enum import Enum


class Verdict(str, Enum):
    COMPATIBLE = "compatible"          # 非破坏性变更
    BREAKING = "breaking"              # 破坏性变更
    IDENTICAL = "identical"            # 无变更


def _scalar_types(s: dict) -> frozenset[str]:
    if "types" in s:
        return frozenset(s["types"])
    return frozenset([s["type"]])


def _compare(old: dict, new: dict) -> Verdict:
    if old == new:
        return Verdict.IDENTICAL
    ko, kn = old.get("kind"), new.get("kind")
    if ko == "any" or kn == "any":
        # any 表示样本不足，无法判定收紧，按兼容处理（不谎报破坏）
        return Verdict.COMPATIBLE
    if ko != kn:
        return Verdict.BREAKING
    if ko == "scalar":
        to, tn = _scalar_types(old), _scalar_types(new)
        if to == tn:
            return Verdict.IDENTICAL
        # 放宽判定：旧类型集合中每个类型都必须仍被新集合接受；
        # integer 是 number 的子类型，故 integer -> number 视为放宽。
        def _accepted(t: str) -> bool:
            return t in tn or (t == "integer" and "number" in tn)

        if all(_accepted(t) for t in to):
            return Verdict.COMPATIBLE
        return Verdict.BREAKING  # 子集、部分重叠或不相交：收紧/变更，破坏
    if ko == "object":
        verdict = Verdict.IDENTICAL
        of, nf = old["fields"], new["fields"]
        for key in of:
            if key not in nf:
                return Verdict.BREAKING  # 删除字段：破坏
        for key in nf:
            if key not in of:
                verdict = Verdict.COMPATIBLE  # 新增可选字段：兼容
                continue
            sub = _compare(of[key], nf[key])
            if sub is Verdict.BREAKING:
                return Verdict.BREAKING
            if sub is Verdict.COMPATIBLE:
                verdict = Verdict.COMPATIBLE
        return verdict
    if ko == "array":
        return _compare(old["items"], new["items"])
    return Verdict.BREAKING  # 未知种类：保守判破坏


def compare_schemas(old: dict, new: dict) -> Verdict:
    """判定从 old 到 new 的演进是否兼容。确定性纯函数。"""
    return _compare(old, new)
