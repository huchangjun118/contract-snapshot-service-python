"""兼容性判定测试：规则确定，相同输入结论一致。"""
from contract_snapshot.compat import Verdict, compare_schemas
from contract_snapshot.schema import extract_schema as ex


def test_identical():
    assert compare_schemas(ex({"a": 1}), ex({"a": 1})) is Verdict.IDENTICAL


def test_add_optional_field_compatible():
    assert compare_schemas(ex({"a": 1}), ex({"a": 1, "b": "x"})) is Verdict.COMPATIBLE


def test_remove_field_breaking():
    assert compare_schemas(ex({"a": 1, "b": 2}), ex({"a": 1})) is Verdict.BREAKING


def test_type_narrowing_breaking():
    assert compare_schemas(ex({"v": 1.5}), ex({"v": 1})) is Verdict.BREAKING


def test_type_widening_compatible():
    assert compare_schemas(ex({"v": 1}), ex({"v": 1.5})) is Verdict.COMPATIBLE


def test_type_change_breaking():
    assert compare_schemas(ex({"v": "s"}), ex({"v": True})) is Verdict.BREAKING


def test_nested_breaking_propagates():
    old = ex({"user": {"id": 1, "name": "x"}})
    new = ex({"user": {"id": 1}})
    assert compare_schemas(old, new) is Verdict.BREAKING


def test_array_element_union():
    assert compare_schemas(ex({"l": [1]}), ex({"l": [1, "s"]})) is Verdict.COMPATIBLE
    assert compare_schemas(ex({"l": [1, "s"]}), ex({"l": [1]})) is Verdict.BREAKING


def test_deterministic_verdict():
    old = ex({"a": 1, "b": {"c": "x"}})
    new = ex({"a": 1, "b": {"c": "x", "d": None}})
    results = {compare_schemas(old, new) for _ in range(50)}
    assert results == {Verdict.COMPATIBLE}
