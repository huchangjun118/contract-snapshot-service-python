"""配置变更不得让破坏降级：
无论采集侧归一化规则怎么改，候选相对当前基线删除字段或收紧类型，
判定必须仍是 breaking；历史判定也不被新配置重新解释。
"""
from contract_snapshot.compat import Verdict
from contract_snapshot.config import CaptureConfig, NormalizeRules
from contract_snapshot.service import ContractService
from contract_snapshot.store import SnapshotStore


def _service(config=None):
    return ContractService(SnapshotStore(":memory:"), config)


def test_removed_field_still_breaking_after_ignore_rule_change():
    svc = _service()
    v1 = svc.capture_payload("/ep", {"id": 1, "debug": True}).version_id
    svc.approve(v1)

    # 新规则把 debug 加入忽略：候选快照里 debug 消失 -> 相对基线是删除字段
    new_cfg = CaptureConfig(
        rules=NormalizeRules(ignore_paths=frozenset({"debug"}))
    )
    svc2 = ContractService(svc._store, new_cfg)
    r = svc2.capture_payload("/ep", {"id": 1, "debug": True})
    assert r.rules_match is False           # 规则确实换了
    assert r.verdict is Verdict.BREAKING    # 但删除字段仍是破坏
    assert svc2.get_baseline("/ep").version_id == v1  # 未批准不动基线


def test_nested_removed_field_breaking_across_rules():
    svc = _service()
    v1 = svc.capture_payload(
        "/ep", {"user": {"id": 1, "ssn": "x"}, "ok": True}
    ).version_id
    svc.approve(v1)

    # 新规则忽略深层路径 user.ssn
    svc2 = ContractService(
        svc._store,
        CaptureConfig(rules=NormalizeRules(ignore_paths=frozenset({"user.ssn"}))),
    )
    r = svc2.capture_payload("/ep", {"user": {"id": 1, "ssn": "x"}, "ok": True})
    assert r.rules_match is False
    assert r.verdict is Verdict.BREAKING


def test_type_narrowing_breaking_across_rules():
    svc = _service()
    v1 = svc.capture_payload("/ep", {"v": 1.5}).version_id
    svc.approve(v1)

    # 换一套完全不同的易变规则后，候选收紧 number -> integer
    svc2 = ContractService(
        svc._store,
        CaptureConfig(rules=NormalizeRules(volatile_paths=frozenset({"nonce"}))),
    )
    r = svc2.capture_payload("/ep", {"v": 1})
    assert r.rules_match is False
    assert r.verdict is Verdict.BREAKING


def test_type_change_string_to_bool_breaking_across_rules():
    svc = _service()
    v1 = svc.capture_payload("/ep", {"v": "s"}).version_id
    svc.approve(v1)
    svc2 = ContractService(
        svc._store,
        CaptureConfig(rules=NormalizeRules(ignore_paths=frozenset({"extra"}))),
    )
    r = svc2.capture_payload("/ep", {"v": True})
    assert r.verdict is Verdict.BREAKING


def test_adding_field_remains_compatible_across_rules():
    svc = _service()
    v1 = svc.capture_payload("/ep", {"id": 1}).version_id
    svc.approve(v1)
    svc2 = ContractService(
        svc._store,
        CaptureConfig(rules=NormalizeRules(ignore_paths=frozenset({"zzz"}))),
    )
    r = svc2.capture_payload("/ep", {"id": 1, "newf": "x"})
    assert r.rules_match is False
    assert r.verdict is Verdict.COMPATIBLE


def test_identical_structure_identical_even_with_rule_fingerprint_difference():
    """规则变了但被忽略/易变路径在负载中不存在：结构仍一致 -> identical。"""
    svc = _service()
    v1 = svc.capture_payload("/ep", {"id": 1}).version_id
    svc.approve(v1)
    svc2 = ContractService(
        svc._store,
        CaptureConfig(rules=NormalizeRules(ignore_paths=frozenset({"never.present"}))),
    )
    r = svc2.capture_payload("/ep", {"id": 1})
    assert r.rules_match is False
    assert r.verdict is Verdict.IDENTICAL


def test_history_judgment_not_reinterpreted_after_config_change():
    """已落库版本的判定依据不被配置变更改写：判定记录只追加。"""
    svc = _service()
    # 基线含 id 与 b 两个字段
    v1 = svc.capture_payload("/ep", {"id": 1, "b": 2}).version_id
    svc.approve(v1)
    # compatible 判定（再新增字段 c）
    svc.capture_payload("/ep", {"id": 1, "b": 2, "c": 3})
    assert svc.get_last_judgment("/ep").verdict == Verdict.COMPATIBLE.value

    # 换配置后再做一次采集：候选仍带 b，但新规则把 b 忽略掉 -> 相对基线删除字段
    svc2 = ContractService(
        svc._store,
        CaptureConfig(rules=NormalizeRules(ignore_paths=frozenset({"b"}))),
    )
    svc2.capture_payload("/ep", {"id": 1, "b": 2})
    latest = svc2.get_last_judgment("/ep")
    assert latest.verdict == Verdict.BREAKING.value
    # 历史版本记录本身没被改写：原始两个版本 + 新规则下的一个版本
    assert len(svc.list_versions("/ep")) == 3
    # 已批准基线仍是 v1，schema 内容与 v1 完整一致（含 b）
    baseline = svc2.get_baseline("/ep")
    assert baseline.version_id == v1
    assert set(baseline.schema["fields"]) == {"id", "b"}


def test_rules_fingerprint_participates_in_version_identity():
    """同结构不同规则产生不同版本；同规则跨时间/进程产生同一版本。"""
    svc = _service()
    r1 = svc.capture_payload("/ep", {"id": 1, "debug": True})
    svc2 = ContractService(
        svc._store,
        CaptureConfig(rules=NormalizeRules(ignore_paths=frozenset({"debug"}))),
    )
    r2 = svc2.capture_payload("/ep", {"id": 1, "debug": True})
    r3 = svc2.capture_payload("/ep", {"id": 1, "debug": True})  # 同规则再来一次
    assert r1.version_id != r2.version_id
    assert r2.version_id == r3.version_id  # 同一规则确定性版本
