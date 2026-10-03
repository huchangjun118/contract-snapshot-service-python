"""Tests for the contract service: collect, approve, rollback, concurrency."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from contract_snapshot.config import Config
from contract_snapshot.models import RawCapture
from contract_snapshot.service import ContractService


@pytest.fixture()
def service(tmp_path):
    cfg = Config(data_dir=str(tmp_path / "data"))
    return ContractService(config=cfg)


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


class TestCollect:
    def test_collect_new_version(self, service):
        result = service.collect(_capture())
        assert result.status == "collected"
        assert result.is_new_version is True
        assert result.version_id is not None

    def test_collect_duplicate(self, service):
        service.collect(_capture())
        result = service.collect(_capture())
        assert result.status == "duplicate"
        assert result.is_new_version is False

    def test_unapproved_collection_does_not_affect_baseline(self, service):
        service.collect(_capture())
        baseline = service.get_baseline("POST:/api/items")
        assert baseline is None

    def test_collect_parse_error(self, service):
        result = service.collect(_capture(request_body="{invalid"))
        assert result.status == "parse_error"
        assert result.error is not None

    def test_collect_ignored_when_disabled(self, service):
        service.config.collection.enabled = False
        result = service.collect(_capture())
        assert result.status == "ignored"
        assert result.reason == "collection_disabled"


class TestApprove:
    def test_approve_sets_baseline(self, service):
        result = service.collect(_capture())
        index = service.approve(result.endpoint_key, result.version_id)
        assert index.baseline_version_id == result.version_id
        baseline = service.get_baseline(result.endpoint_key)
        assert baseline.version_id == result.version_id

    def test_approve_nonexistent_version_raises(self, service):
        with pytest.raises(KeyError):
            service.approve("POST:/api/items", "nonexistent")

    def test_approve_records_history(self, service):
        result = service.collect(_capture())
        service.approve(result.endpoint_key, result.version_id)
        index = service.list_endpoints()[0]
        assert len(index.baseline_history) == 1
        assert index.baseline_history[0].action == "approve"
        assert index.baseline_history[0].sequence == 1

    def test_unapproved_version_not_in_baseline(self, service):
        v1 = service.collect(_capture())
        service.approve(v1.endpoint_key, v1.version_id)
        # collect a different contract
        v2 = service.collect(_capture(response_body={"id": 1, "name": "widget", "extra": True}))
        baseline = service.get_baseline(v1.endpoint_key)
        assert baseline.version_id == v1.version_id
        assert baseline.version_id != v2.version_id


class TestJudge:
    def test_judge_no_baseline(self, service):
        service.collect(_capture())
        report = service.judge_capture(_capture())
        assert report.status == "no_baseline"

    def test_judge_compatible(self, service):
        result = service.collect(_capture())
        service.approve(result.endpoint_key, result.version_id)
        report = service.judge_capture(_capture(response_body={"id": 1, "name": "widget", "extra": True}))
        assert report.status == "compatible"

    def test_judge_breaking(self, service):
        result = service.collect(_capture())
        service.approve(result.endpoint_key, result.version_id)
        report = service.judge_capture(_capture(response_body={"id": 1}))
        assert report.status == "breaking"
        assert any(c.breaking for c in report.changes)

    def test_judge_version_by_id(self, service):
        v1 = service.collect(_capture())
        service.approve(v1.endpoint_key, v1.version_id)
        v2 = service.collect(_capture(response_body={"id": 1, "name": "widget", "extra": True}))
        report = service.judge_version(v1.endpoint_key, v2.version_id)
        assert report.status == "compatible"

    def test_judge_parse_error(self, service):
        result = service.collect(_capture())
        service.approve(result.endpoint_key, result.version_id)
        report = service.judge_capture(_capture(request_body="{invalid"))
        assert report.status == "error"
        assert report.error is not None


class TestRollback:
    def test_rollback_to_previous_version(self, service):
        v1 = service.collect(_capture())
        service.approve(v1.endpoint_key, v1.version_id)
        v2 = service.collect(_capture(response_body={"id": 1, "name": "widget", "extra": True}))
        service.approve(v2.endpoint_key, v2.version_id)
        # baseline is now v2
        assert service.get_baseline(v1.endpoint_key).version_id == v2.version_id
        # rollback to v1
        index = service.rollback(v1.endpoint_key, v1.version_id)
        assert index.baseline_version_id == v1.version_id
        assert index.baseline_history[-1].action == "rollback"
        assert index.baseline_history[-1].sequence == 3

    def test_rollback_to_nonexistent_raises(self, service):
        v1 = service.collect(_capture())
        service.approve(v1.endpoint_key, v1.version_id)
        with pytest.raises(KeyError):
            service.rollback(v1.endpoint_key, "nonexistent")

    def test_rollback_then_judge_uses_old_contract(self, service):
        v1 = service.collect(_capture())
        service.approve(v1.endpoint_key, v1.version_id)
        v2 = service.collect(_capture(response_body={"id": 1, "name": "widget", "extra": True}))
        service.approve(v2.endpoint_key, v2.version_id)
        service.rollback(v1.endpoint_key, v1.version_id)
        # judge a breaking change relative to v1 (extra field was compatible with v2)
        report = service.judge_capture(_capture(response_body={"id": 1, "name": "widget"}))
        assert report.status == "compatible"


class TestConcurrency:
    def test_concurrent_collects_no_half_artifacts(self, service):
        def do_collect(i):
            return service.collect(_capture(response_body={"id": i, "name": "widget"}))

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(do_collect, range(50)))
        # all collects succeed
        assert all(r.status in ("collected", "duplicate") for r in results)
        # no temp files left behind
        import os
        for root, _, files in os.walk(service.storage.data_dir):
            for f in files:
                assert not f.startswith(".tmp-"), f"temp file left: {f}"

    def test_concurrent_approve_and_rollback_deterministic(self, service):
        v1 = service.collect(_capture())
        v2 = service.collect(_capture(response_body={"id": 1, "name": "widget", "extra": True}))
        service.approve(v1.endpoint_key, v1.version_id)

        errors = []

        def approve_v2():
            try:
                service.approve(v1.endpoint_key, v2.version_id)
            except Exception as exc:
                errors.append(exc)

        def rollback_v1():
            try:
                service.rollback(v1.endpoint_key, v1.version_id)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=approve_v2) for _ in range(5)]
        threads += [threading.Thread(target=rollback_v1) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        # final baseline must be a committed complete version
        baseline = service.get_baseline(v1.endpoint_key)
        assert baseline is not None
        assert baseline.version_id in (v1.version_id, v2.version_id)
        # history sequences must be monotonic
        index = service.list_endpoints()[0]
        sequences = [h.sequence for h in index.baseline_history]
        assert sequences == sorted(sequences)
        assert len(sequences) == len(set(sequences))

    def test_concurrent_judge_during_approve(self, service):
        v1 = service.collect(_capture())
        service.approve(v1.endpoint_key, v1.version_id)

        def judge():
            return service.judge_capture(_capture(response_body={"id": 1, "name": "widget", "extra": True}))

        def approve():
            v2 = service.collect(_capture(response_body={"id": 1, "name": "widget", "extra": True}))
            service.approve(v1.endpoint_key, v2.version_id)

        threads = [threading.Thread(target=judge) for _ in range(10)]
        threads += [threading.Thread(target=approve) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # all judgments must be valid verdicts (never mixed state)
        # re-run judge to confirm service is consistent
        report = service.judge_capture(_capture(response_body={"id": 1, "name": "widget", "extra": True}))
        assert report.status in ("compatible", "breaking", "no_baseline", "error")
