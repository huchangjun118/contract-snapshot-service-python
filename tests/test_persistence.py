"""跨进程重启持久化：默认落盘位置 + 重启后版本与基线仍在。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from contract_snapshot.service import ContractService
from contract_snapshot.store import SnapshotStore, default_db_path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_default_db_path_is_on_disk(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert default_db_path() == os.path.join(
        ".contract_snapshot", "contracts.db"
    )
    store = SnapshotStore()  # 无参：使用默认落盘位置，自动建目录
    assert Path(store.db_path).exists()
    store.close()


def test_reopen_same_file_keeps_baseline(tmp_path):
    db = str(tmp_path / "data" / "contracts.db")
    svc = ContractService(SnapshotStore(db))
    v1 = svc.capture_payload("/users", {"id": 1, "name": "a"}).version_id
    svc.approve(v1)
    svc.capture_payload("/users", {"id": 1, "name": "a", "extra": 1})
    svc._store.close()

    # 同一路径重新打开（模拟重启）：版本、批准状态、基线全部还在
    svc2 = ContractService(SnapshotStore(db))
    baseline = svc2.get_baseline("/users")
    assert baseline is not None
    assert baseline.version_id == v1
    assert baseline.approved is True
    versions = svc2.list_versions("/users")
    assert len(versions) == 2
    assert {r.version_id for r in versions} >= {v1}


def test_default_location_restart_via_new_store(tmp_path, monkeypatch):
    """未显式配置位置时，默认落盘库重启后仍能读到。"""
    monkeypatch.chdir(tmp_path)
    svc = ContractService(SnapshotStore())
    v1 = svc.capture_payload("/orders", {"id": 9}).version_id
    svc.approve(v1)
    svc._store.close()

    svc2 = ContractService(SnapshotStore())
    assert svc2.get_baseline("/orders").version_id == v1


def test_restart_in_subprocess(tmp_path):
    """用两个独立子进程验证真正的跨进程持久化。"""
    db = str(tmp_path / "contracts.db")
    script = """
import sys
sys.path.insert(0, %r)
from contract_snapshot.service import ContractService
from contract_snapshot.store import SnapshotStore
mode, db = sys.argv[1], sys.argv[2]
svc = ContractService(SnapshotStore(db))
if mode == "write":
    v = svc.capture_payload("/users", {"id": 1, "name": "a"}).version_id
    svc.approve(v)
    print(v)
elif mode == "read":
    b = svc.get_baseline("/users")
    assert b is not None, "baseline missing after restart"
    assert b.approved is True
    print(b.version_id)
""" % str(PROJECT_ROOT)

    env = dict(os.environ)
    w = subprocess.run([sys.executable, "-c", script, "write", db],
                       capture_output=True, text=True, env=env, timeout=30)
    assert w.returncode == 0, w.stderr
    written_version = w.stdout.strip()
    assert written_version

    r = subprocess.run([sys.executable, "-c", script, "read", db],
                       capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == written_version


def test_judgments_persist_across_restart(tmp_path):
    db = str(tmp_path / "contracts.db")
    svc = ContractService(SnapshotStore(db))
    v1 = svc.capture_payload("/ep", {"id": 1}).version_id
    svc.approve(v1)
    svc.capture_payload("/ep", {"id": 1, "new": "x"})  # compatible 判定
    svc._store.close()

    svc2 = ContractService(SnapshotStore(db))
    j = svc2.get_last_judgment("/ep")
    assert j is not None
    assert j.verdict == "compatible"
    assert j.baseline_version_id == v1
