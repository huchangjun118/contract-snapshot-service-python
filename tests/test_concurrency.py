"""并发采集、并发批准与回滚：结果确定，无半成品，基线始终是完整版本。"""
import threading

from contract_snapshot.service import ContractService
from contract_snapshot.store import SnapshotStore


def _make_service():
    return ContractService(SnapshotStore())


def test_concurrent_capture_consistent():
    svc = _make_service()
    errors = []

    def worker(i):
        try:
            svc.capture_payload("/ep", {"id": i, "name": f"n{i}"})
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    # 同一契约的并发采集只产生一个版本
    assert len(svc.list_versions("/ep")) == 1


def test_concurrent_approve_and_rollback_deterministic():
    svc = _make_service()
    v1 = svc.capture_payload("/ep", {"id": 1}).version_id
    svc.approve(v1)
    v2 = svc.capture_payload("/ep", {"id": 1, "extra": True}).version_id
    svc.approve(v2)

    outcomes = []
    lock = threading.Lock()

    def approver():
        svc.approve(v2)
        with lock:
            outcomes.append(svc.get_baseline("/ep").version_id)

    def rollbacker():
        svc.rollback("/ep", v1)
        with lock:
            outcomes.append(svc.get_baseline("/ep").version_id)

    threads = [threading.Thread(target=f) for _ in range(10) for f in (approver, rollbacker)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    final = svc.get_baseline("/ep")
    # 基线永远是某个已提交的完整版本，绝不出现混合态
    assert final.version_id in {v1, v2}
    assert all(o in {v1, v2} for o in outcomes)
    # 最终基线的 schema 与该版本完整一致
    record = svc.list_versions("/ep")
    by_id = {r.version_id: r for r in record}
    assert by_id[final.version_id].schema == final.schema


def test_unapproved_capture_never_becomes_baseline():
    svc = _make_service()
    v1 = svc.capture_payload("/ep", {"id": 1}).version_id
    svc.approve(v1)
    svc.capture_payload("/ep", {"id": 1, "extra": True})  # 未批准
    assert svc.get_baseline("/ep").version_id == v1
