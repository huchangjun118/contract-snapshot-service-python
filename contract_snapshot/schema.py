"""从归一化后的负载中提取结构 schema，用于兼容性判定。

schema 只描述结构，不携带具体取值：
- 对象：字段名 -> 子 schema，并记录字段是否在所有样本中出现（可选性）；
- 数组：元素 schema 的并集；
- 标量：类型标签（string/number/integer/boolean/null）。
"""
from __future__ import annotations

from typing import Any

TYPE_RANK = {"null": 0, "boolean": 1, "integer": 2, "number": 3, "string": 4}


def _type_tag(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    raise TypeError(f"无法提取类型的值: {value!r}")


def extract_schema(normalized: Any) -> dict:
    """从归一化负载提取结构描述。纯函数，确定性输出。"""
    if isinstance(normalized, dict):
        return {
            "kind": "object",
            # required=True 表示该字段在本次快照中确定存在；
            # 与历史版本比较时用于区分「新增可选字段」与「删除字段」。
            "fields": {k: extract_schema(v) for k, v in normalized.items()},
        }
    if isinstance(normalized, list):
        elem: dict = {"kind": "any"}
        for item in normalized:
            elem = merge_schemas(elem, extract_schema(item))
        return {"kind": "array", "items": elem}
    return {"kind": "scalar", "type": _type_tag(normalized)}


def _types_of(s: dict) -> set:
    if "types" in s:
        return set(s["types"])
    return {s["type"]}


def merge_schemas(a: dict, b: dict) -> dict:
    """求两份 schema 的并集（用于聚合数组元素的多种形态）。

    确定性的：merge(a, b) == merge(b, a)。
    """
    if a.get("kind") == "any":
        return b
    if b.get("kind") == "any":
        return a
    ka, kb = a["kind"], b["kind"]
    if ka != kb:
        # 不同种类并存：标量类型并集用类型集合表达
        if ka == "scalar" and kb == "scalar":
            types = _types_of(a) | _types_of(b)
            if len(types) == 1:
                return {"kind": "scalar", "type": types.pop()}
            return {"kind": "scalar", "types": sorted(types, key=TYPE_RANK.get)}
        return {"kind": "any"}
    if ka == "object":
        keys = set(a["fields"]) | set(b["fields"])
        return {
            "kind": "object",
            "fields": {
                k: merge_schemas(a["fields"][k], b["fields"][k])
                if k in a["fields"] and k in b["fields"]
                else a["fields"].get(k, b["fields"].get(k))
                for k in sorted(keys)
            },
        }
    if ka == "array":
        return {"kind": "array", "items": merge_schemas(a["items"], b["items"])}
    # scalar
    if a.get("type") == b.get("type"):
        return a
    types = _types_of(a) | _types_of(b)
    return {"kind": "scalar", "types": sorted(types, key=TYPE_RANK.get)}
