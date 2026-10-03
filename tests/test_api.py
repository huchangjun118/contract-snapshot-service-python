"""Tests for the HTTP API."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from contract_snapshot.api import set_service
from contract_snapshot.config import Config
from contract_snapshot.service import ContractService
from main import app


@pytest.fixture()
def client(tmp_path):
    svc = ContractService(config=Config(data_dir=str(tmp_path / "data")))
    set_service(svc)
    return TestClient(app)


def _payload(**overrides):
    defaults = dict(
        method="POST",
        path="/api/items",
        status_code=200,
        request_headers={"content-type": "application/json"},
        request_body={"name": "widget"},
        response_headers={"content-type": "application/json"},
        response_body={"id": 1, "name": "widget"},
    )
    defaults.update(overrides)
    return defaults


class TestCollect:
    def test_collect_success(self, client):
        resp = client.post("/api/contracts/collect", json=_payload())
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "collected"
        assert body["is_new_version"] is True
        assert "version_id" in body

    def test_collect_duplicate(self, client):
        client.post("/api/contracts/collect", json=_payload())
        resp = client.post("/api/contracts/collect", json=_payload())
        assert resp.status_code == 200
        assert resp.json()["status"] == "duplicate"

    def test_collect_parse_error(self, client):
        resp = client.post("/api/contracts/collect", json=_payload(request_body="{invalid"))
        assert resp.status_code == 400
        assert resp.json()["status"] == "parse_error"

    def test_collect_storage_error(self, client, tmp_path):
        from contract_snapshot.storage import StorageError

        svc = ContractService(config=Config(data_dir=str(tmp_path / "data2")))

        def fail_write(record):
            raise StorageError("disk full")

        svc.storage.write_version = fail_write
        set_service(svc)
        resp = client.post("/api/contracts/collect", json=_payload())
        assert resp.status_code == 503
        assert resp.json()["status"] == "storage_error"


class TestJudge:
    def test_judge_compatible(self, client):
        r = client.post("/api/contracts/collect", json=_payload())
        ep = r.json()["endpoint_key"]
        vid = r.json()["version_id"]
        client.post(f"/api/contracts/endpoints/approve?key={ep}", json={"version_id": vid})
        resp = client.post("/api/contracts/judge", json=_payload(response_body={"id": 1, "name": "widget", "extra": True}))
        assert resp.status_code == 200
        assert resp.json()["status"] == "compatible"

    def test_judge_breaking(self, client):
        r = client.post("/api/contracts/collect", json=_payload())
        ep = r.json()["endpoint_key"]
        vid = r.json()["version_id"]
        client.post(f"/api/contracts/endpoints/approve?key={ep}", json={"version_id": vid})
        resp = client.post("/api/contracts/judge", json=_payload(response_body={"id": 1}))
        assert resp.status_code == 200
        assert resp.json()["status"] == "breaking"

    def test_judge_no_baseline(self, client):
        client.post("/api/contracts/collect", json=_payload())
        resp = client.post("/api/contracts/judge", json=_payload())
        assert resp.status_code == 404
        assert resp.json()["status"] == "no_baseline"

    def test_judge_parse_error(self, client):
        r = client.post("/api/contracts/collect", json=_payload())
        ep = r.json()["endpoint_key"]
        vid = r.json()["version_id"]
        client.post(f"/api/contracts/endpoints/approve?key={ep}", json={"version_id": vid})
        resp = client.post("/api/contracts/judge", json=_payload(request_body="{invalid"))
        assert resp.status_code == 400
        assert resp.json()["status"] == "error"


class TestApprove:
    def test_approve_success(self, client):
        r = client.post("/api/contracts/collect", json=_payload())
        ep = r.json()["endpoint_key"]
        vid = r.json()["version_id"]
        resp = client.post(f"/api/contracts/endpoints/approve?key={ep}", json={"version_id": vid})
        assert resp.status_code == 200
        assert resp.json()["baseline_version_id"] == vid

    def test_approve_nonexistent(self, client):
        resp = client.post("/api/contracts/endpoints/approve?key=POST:/api/items", json={"version_id": "nope"})
        assert resp.status_code == 404


class TestRollback:
    def test_rollback_success(self, client):
        r1 = client.post("/api/contracts/collect", json=_payload())
        ep = r1.json()["endpoint_key"]
        v1 = r1.json()["version_id"]
        client.post(f"/api/contracts/endpoints/approve?key={ep}", json={"version_id": v1})
        r2 = client.post("/api/contracts/collect", json=_payload(response_body={"id": 1, "name": "widget", "extra": True}))
        v2 = r2.json()["version_id"]
        client.post(f"/api/contracts/endpoints/approve?key={ep}", json={"version_id": v2})
        resp = client.post(f"/api/contracts/endpoints/rollback?key={ep}", json={"version_id": v1})
        assert resp.status_code == 200
        assert resp.json()["baseline_version_id"] == v1
        assert resp.json()["action"] == "rollback"

    def test_rollback_nonexistent(self, client):
        r = client.post("/api/contracts/collect", json=_payload())
        ep = r.json()["endpoint_key"]
        vid = r.json()["version_id"]
        client.post(f"/api/contracts/endpoints/approve?key={ep}", json={"version_id": vid})
        resp = client.post(f"/api/contracts/endpoints/rollback?key={ep}", json={"version_id": "nope"})
        assert resp.status_code == 404


class TestReads:
    def test_list_endpoints(self, client):
        client.post("/api/contracts/collect", json=_payload())
        resp = client.get("/api/contracts/endpoints")
        assert resp.status_code == 200
        assert len(resp.json()["endpoints"]) == 1

    def test_get_baseline(self, client):
        r = client.post("/api/contracts/collect", json=_payload())
        ep = r.json()["endpoint_key"]
        vid = r.json()["version_id"]
        client.post(f"/api/contracts/endpoints/approve?key={ep}", json={"version_id": vid})
        resp = client.get(f"/api/contracts/endpoints/baseline?key={ep}")
        assert resp.status_code == 200
        assert resp.json()["version_id"] == vid

    def test_get_baseline_not_found(self, client):
        resp = client.get("/api/contracts/endpoints/baseline?key=POST:/nope")
        assert resp.status_code == 404

    def test_list_versions(self, client):
        r = client.post("/api/contracts/collect", json=_payload())
        ep = r.json()["endpoint_key"]
        resp = client.get(f"/api/contracts/endpoints/versions?key={ep}")
        assert resp.status_code == 200
        assert len(resp.json()["versions"]) == 1

    def test_judge_version_by_id(self, client):
        r1 = client.post("/api/contracts/collect", json=_payload())
        ep = r1.json()["endpoint_key"]
        v1 = r1.json()["version_id"]
        client.post(f"/api/contracts/endpoints/approve?key={ep}", json={"version_id": v1})
        r2 = client.post("/api/contracts/collect", json=_payload(response_body={"id": 1, "name": "widget", "extra": True}))
        v2 = r2.json()["version_id"]
        resp = client.get(f"/api/contracts/endpoints/judge?key={ep}&version_id={v2}")
        assert resp.status_code == 200
        assert resp.json()["status"] == "compatible"

    def test_config_endpoint(self, client):
        resp = client.get("/api/contracts/config")
        assert resp.status_code == 200
        body = resp.json()
        assert "collection" in body
        assert "normalization" in body
        assert "config_hash" in body


class TestExistingRoute:
    def test_root_unchanged(self, client):
        resp = client.get("/")
        assert resp.status_code == 200
        assert resp.json() == {"Hello": "World"}
