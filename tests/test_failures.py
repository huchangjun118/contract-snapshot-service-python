"""异常降级：失败状态可区分，不污染基线，不把失败当无变更。"""
import pytest

from contract_snapshot.errors import ContractError, FailureKind
from contract_snapshot.service import ContractService
from contract_snapshot.store import SnapshotStore


def test_unparseable_body_does_not_pollute_baseline():
    svc = ContractService(SnapshotStore())
    v1 = svc.capture_payload("/ep", {"id": 1}).version_id
    svc.approve(v1)
    with pytest.raises(ContractError) as ei:
        svc.capture("/ep", "{not valid json")
    assert ei.value.kind is FailureKind.UNPARSEABLE_BODY
    assert svc.get_baseline("/ep").version_id == v1
    assert len(svc.list_versions("/ep")) == 1  # 失败采集未落盘


def test_store_unavailable_distinguishable():
    svc = ContractService(SnapshotStore())
    svc._store.close()  # 模拟存储不可用
    with pytest.raises(ContractError) as ei:
        svc.capture_payload("/ep", {"id": 1})
    assert ei.value.kind is FailureKind.STORE_UNAVAILABLE


def test_approve_unknown_version():
    svc = ContractService(SnapshotStore())
    with pytest.raises(ContractError) as ei:
        svc.approve("no-such-version")
    assert ei.value.kind is FailureKind.VERSION_NOT_FOUND


def test_rollback_to_unapproved_rejected():
    svc = ContractService(SnapshotStore())
    v = svc.capture_payload("/ep", {"id": 1}).version_id
    with pytest.raises(ContractError) as ei:
        svc.rollback("/ep", v)
    assert ei.value.kind is FailureKind.NOT_APPROVED


def test_config_change_does_not_reinterpret_history():
    from contract_snapshot.config import CaptureConfig, NormalizeRules

    svc = ContractService(SnapshotStore())
    v1 = svc.capture_payload("/ep", {"id": 1, "debug": True}).version_id
    svc.approve(v1)
    # 配置变更：忽略 debug 字段
    svc2 = ContractService(svc._store, CaptureConfig(
        rules=NormalizeRules(ignore_paths=frozenset({"debug"}))
    ))
    # 历史基线仍是原版本，规则指纹不同 -> 不重新解释历史，按兼容演进处理
    r = svc2.capture_payload("/ep", {"id": 1})
    assert svc2.get_baseline("/ep").version_id == v1
    assert r.verdict.value in ("compatible", "identical")


def test_sampling_disabled_captures_nothing():
    from contract_snapshot.config import CaptureConfig

    svc = ContractService(SnapshotStore(), CaptureConfig(sampling_rate=0.0))
    r = svc.capture_payload("/ep", {"id": 1})
    assert r.sampled is False
    assert svc.list_versions("/ep") == []
