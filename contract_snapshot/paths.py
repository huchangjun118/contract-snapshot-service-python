"""字段路径通配匹配。

忽略字段与易变字段按「路径模式」匹配，而不是只按字段名整体匹配：

- 模式以 ``.`` 分段，从根开始锚定匹配，如 ``data.user.token``
  只匹配根下 data 里 user 的 token 字段；
- ``*`` 匹配恰好一层路径段，如 ``data.*.token`` 匹配 data 下任意
  一层对象里的 token；
- ``**`` 匹配任意层数（含零层）路径段，如 ``**.token`` 匹配任意
  深度的 token；
- 不含 ``.`` 的裸字段名（如 ``debug``）匹配任意深度下同名字段，
  与早期「按字段名整体匹配」的行为兼容；
- 数组不增加路径段：``{"items": [{"id": 1}]}`` 中 id 的路径是
  ``items.id``，因此 ``items.id`` 即可匹配数组元素内的字段。

匹配是纯函数：同一组模式在任何进程、任何时间对同一路径结论一致。
"""
from __future__ import annotations


def _match_segments(pattern: list[str], path: tuple[str, ...]) -> bool:
    """递归匹配已分段的模式与路径。``**`` 可消耗任意数量的路径段。"""
    if not pattern:
        return not path
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        # 零层：跳过 **；或消耗一层路径继续匹配
        return _match_segments(rest, path) or (
            bool(path) and _match_segments(pattern, path[1:])
        )
    if not path:
        return False
    if head != "*" and head != path[0]:
        return False
    return _match_segments(rest, path[1:])


def match_path(pattern: str, path: tuple[str, ...]) -> bool:
    """判断路径是否命中模式。path 为从根到字段的键序列（至少一段）。"""
    if not path:
        return False
    if "." not in pattern and "*" not in pattern:
        # 裸字段名：兼容按字段名整体匹配，命中任意深度同名字段
        return path[-1] == pattern
    return _match_segments(pattern.split("."), path)


def matches_any(patterns: frozenset[str] | set[str], path: tuple[str, ...]) -> bool:
    """路径是否命中模式集合中的任意一个。"""
    return any(match_path(p, path) for p in patterns)
