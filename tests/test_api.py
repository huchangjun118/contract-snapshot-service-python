"""HTTP 层：原始报文采集、可区分失败、采集范围。"""
import pytest
from fastapi.testclient import TestClient

from contract_snapshot.api import create_app
from contract_snapshot.config import CaptureConfig
from contract_snapshot.service import ContractService
from contract_snapshot.store import SnapshotStore


@pytest.fixture()
def client():
    return TestClient(create_app(ContractService(SnapshotStore())))


def test_capture_raw_body_ok(client):
    r = client.post("/capture", json={"endpoint": "/ep", "raw_body": '{"id": 1}'})
    assert r.status_code == 200
    body = r.json()
    assert body["sampled"] is True and body["version_id"]


def test_capture_raw_body_unparseable_distinguishable(client):
    ok = client.post("/capture", json={"endpoint": "/ep", "raw_body": '{"id": 1}'})
    vid = ok.json()["version_id"]
    client.post("/approve", json={"version_id": vid})

    bad = client.post("/capture", json={"endpoint": "/ep", "raw_body": "{not json"})
    assert bad.status_code == 422
    assert bad.json()["error"]["kind"] == "unparseable_body"
    # 失败采集不落盘、不覆盖基线
    assert client.get("/baseline/ep").json()["baseline_version_id"] == vid
    assert len(client.get("/versions/ep").json()["versions"]) == 1


def test_capture_missing_body_rejected(client):
    r = client.post("/capture", json={"endpoint": "/ep"})
    assert r.status_code == 422
    assert r.json()["error"]["kind"] == "unparseable_body"


def test_capture_out_of_scope_not_sampled():
    cfg = CaptureConfig(include_endpoints=("/api/*",))
    client = TestClient(create_app(ContractService(SnapshotStore(), cfg)))
    r = client.post("/capture", json={"endpoint": "/other", "body": {"id": 1}})
    assert r.status_code == 200
    assert r.json()["sampled"] is False
    assert client.get("/versions/other").json()["versions"] == []
