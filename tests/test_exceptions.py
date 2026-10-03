"""Tests for exception handling and degradation."""
from __future__ import annotations

import os

import pytest

from contract_snapshot.config import Config
from contract_snapshot.models import RawCapture
from contract_snapshot.service import ContractService
from contract_snapshot.storage import SnapshotStorage, StorageError


def _capture(**overrides) -> RawCapture:
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
    return RawCapture(**defaults)


class TestParseErrors:
    def test_unparseable_request_body(self, tmp_path):
        svc = ContractService(config=Config(data_dir=str(tmp_path / "data")))
        result = svc.collect(_capture(request_body="{invalid json"))
        assert result.status == "parse_error"
        assert result.error is not None

    def test_unparseable_response_body(self, tmp_path):
        svc = ContractService(config=Config(data_dir=str(tmp_path / "data")))
        result = svc.collect(_capture(response_body="{invalid json"))
        assert result.status == "parse_error"

    def test_parse_error_does_not_pollute_baseline(self, tmp_path):
        svc = ContractService(config=Config(data_dir=str(tmp_path / "data")))
        good = svc.collect(_capture())
        svc.approve(good.endpoint_key, good.version_id)
        # bad capture must not change the baseline
        svc.collect(_capture(request_body="{invalid"))
        baseline = svc.get_baseline(good.endpoint_key)
        assert baseline.version_id == good.version_id

    def test_parse_error_not_treated_as_no_change(self, tmp_path):
        svc = ContractService(config=Config(data_dir=str(tmp_path / "data")))
        good = svc.collect(_capture())
        svc.approve(good.endpoint_key, good.version_id)
        # judge with a parse error must return error, not compatible
        report = svc.judge_capture(_capture(request_body="{invalid"))
        assert report.status == "error"
        assert report.status != "compatible"

    def test_parse_error_creates_no_version_file(self, tmp_path):
        svc = ContractService(config=Config(data_dir=str(tmp_path / "data")))
        svc.collect(_capture(request_body="{invalid"))
        versions_dir = tmp_path / "data" / "versions"
        assert not list(versions_dir.glob("*.json"))


class TestStorageErrors:
    def test_storage_unavailable_on_collect(self, tmp_path):
        # point storage at a path that cannot be created (a file, not a dir)
        bad_path = tmp_path / "blocker"
        bad_path.write_text("I am a file")
        with pytest.raises(StorageError):
            SnapshotStorage(str(bad_path / "data"))

    def test_collect_returns_storage_error(self, tmp_path, monkeypatch):
        svc = ContractService(config=Config(data_dir=str(tmp_path / "data")))
        # force the version write to fail
        def fail_write(record):
            raise StorageError("simulated disk failure")
        monkeypatch.setattr(svc.storage, "write_version", fail_write)
        result = svc.collect(_capture())
        assert result.status == "storage_error"
        assert result.error is not None

    def test_storage_error_does_not_pollute_baseline(self, tmp_path, monkeypatch):
        svc = ContractService(config=Config(data_dir=str(tmp_path / "data")))
        good = svc.collect(_capture())
        svc.approve(good.endpoint_key, good.version_id)
        # break storage, then try to collect a new contract
        def fail_write(record):
            raise StorageError("simulated disk failure")
        monkeypatch.setattr(svc.storage, "write_version", fail_write)
        svc.collect(_capture(response_body={"id": 999, "name": "widget", "extra": True}))
        # baseline must be unchanged
        baseline = svc.get_baseline(good.endpoint_key)
        assert baseline.version_id == good.version_id

    def test_approve_with_missing_version_raises(self, tmp_path):
        svc = ContractService(config=Config(data_dir=str(tmp_path / "data")))
        with pytest.raises(KeyError):
            svc.approve("POST:/api/items", "nonexistent")


class TestNoBaseline:
    def test_judge_without_baseline_returns_no_baseline(self, tmp_path):
        svc = ContractService(config=Config(data_dir=str(tmp_path / "data")))
        svc.collect(_capture())
        report = svc.judge_capture(_capture())
        assert report.status == "no_baseline"

    def test_judge_version_without_baseline(self, tmp_path):
        svc = ContractService(config=Config(data_dir=str(tmp_path / "data")))
        result = svc.collect(_capture())
        report = svc.judge_version(result.endpoint_key, result.version_id)
        assert report.status == "no_baseline"


class TestDegradedReads:
    def test_list_endpoints_with_corrupt_index(self, tmp_path):
        svc = ContractService(config=Config(data_dir=str(tmp_path / "data")))
        result = svc.collect(_capture())
        svc.approve(result.endpoint_key, result.version_id)
        # corrupt the index file
        index_path = svc.storage.index_path(result.endpoint_key)
        with open(index_path, "w") as fh:
            fh.write("{corrupt")
        # list_endpoints should not raise; it skips corrupt entries
        endpoints = svc.list_endpoints()
        assert isinstance(endpoints, list)

    def test_read_version_with_corrupt_file(self, tmp_path):
        svc = ContractService(config=Config(data_dir=str(tmp_path / "data")))
        result = svc.collect(_capture())
        # corrupt the version file
        version_path = svc.storage.version_path(result.version_id)
        with open(version_path, "w") as fh:
            fh.write("{corrupt")
        with pytest.raises(StorageError):
            svc.storage.read_version(result.version_id)
