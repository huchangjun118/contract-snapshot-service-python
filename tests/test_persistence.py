"""持久化：快照与基线跨进程重启后仍然存在。"""
from contract_snapshot.config import CaptureConfig, default_db_path
from contract_snapshot.service import ContractService
from contract_snapshot.store import SnapshotStore


def test_baseline_survives_restart(tmp_path):
    db = str(tmp_path / "snapshots.db")
    svc = ContractService(SnapshotStore(db))
    v1 = svc.capture_payload("/ep", {"id": 1, "name": "x"}).version_id
    svc.approve(v1)
    svc._store.close()

    # 模拟进程重启：同一落盘位置重新建库与服务
    svc2 = ContractService(SnapshotStore(db))
    baseline = svc2.get_baseline("/ep")
    assert baseline is not None
    assert baseline.version_id == v1
    assert baseline.approved is True
    versions = svc2.list_versions("/ep")
    assert [r.version_id for r in versions] == [v1]

    # 重启后采集相同契约：与重启前同一版本、判定无变更
    r = svc2.capture_payload("/ep", {"name": "x", "id": 1})
    assert r.version_id == v1
    assert r.verdict.value == "identical"


def test_versions_accumulate_across_restarts(tmp_path):
    db = str(tmp_path / "sub" / "dir" / "snapshots.db")  # 目录不存在也能落盘
    svc = ContractService(SnapshotStore(db))
    v1 = svc.capture_payload("/ep", {"id": 1}).version_id
    svc.approve(v1)
    svc._store.close()

    svc2 = ContractService(SnapshotStore(db))
    v2 = svc2.capture_payload("/ep", {"id": 1, "extra": True}).version_id
    assert svc2.get_baseline("/ep").version_id == v1  # 未批准不提升
    svc2.approve(v2)
    svc2._store.close()

    svc3 = ContractService(SnapshotStore(db))
    assert svc3.get_baseline("/ep").version_id == v2
    assert {r.version_id for r in svc3.list_versions("/ep")} == {v1, v2}


def test_default_db_path_is_on_disk():
    # 未显式配置时也有可用的默认落盘位置（非内存）
    path = CaptureConfig().resolved_db_path()
    assert path == default_db_path()
    assert path != ":memory:" and path.endswith(".db")
