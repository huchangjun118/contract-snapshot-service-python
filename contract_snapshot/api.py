"""HTTP API for the contract snapshot service."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .models import RawCapture
from .service import ContractService

router = APIRouter(prefix="/api/contracts", tags=["contracts"])

_service: ContractService | None = None


def get_service() -> ContractService:
    global _service
    if _service is None:
        _service = ContractService()
    return _service


def set_service(service: ContractService) -> None:
    global _service
    _service = service


class CapturePayload(BaseModel):
    method: str
    path: str
    status_code: int
    request_headers: dict[str, str] = Field(default_factory=dict)
    request_body: Any = None
    response_headers: dict[str, str] = Field(default_factory=dict)
    response_body: Any = None


class ApprovePayload(BaseModel):
    version_id: str
    actor: str = "api"


class RollbackPayload(BaseModel):
    version_id: str


def _capture_from_payload(payload: CapturePayload) -> RawCapture:
    return RawCapture(
        method=payload.method,
        path=payload.path,
        status_code=payload.status_code,
        request_headers=payload.request_headers,
        request_body=payload.request_body,
        response_headers=payload.response_headers,
        response_body=payload.response_body,
    )


@router.post("/collect")
def collect_capture(payload: CapturePayload):
    result = get_service().collect(_capture_from_payload(payload))
    status_codes = {
        "collected": 200,
        "duplicate": 200,
        "ignored": 200,
        "parse_error": 400,
        "storage_error": 503,
    }
    body: dict[str, Any] = {"status": result.status}
    if result.endpoint_key:
        body["endpoint_key"] = result.endpoint_key
    if result.version_id:
        body["version_id"] = result.version_id
    if result.is_new_version:
        body["is_new_version"] = True
    if result.reason:
        body["reason"] = result.reason
    if result.error:
        body["error"] = result.error
    if result.status not in status_codes:
        raise HTTPException(status_code=500, detail=result.error or "unknown error")
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=status_codes[result.status], content=body)


@router.post("/judge")
def judge_capture(payload: CapturePayload):
    report = get_service().judge_capture(_capture_from_payload(payload))
    return _report_response(report)


@router.get("/endpoints")
def endpoints():
    indexes = get_service().list_endpoints()
    return {
        "endpoints": [
            {
                "endpoint_key": index.endpoint_key,
                "method": index.method,
                "path": index.path,
                "baseline_version_id": index.baseline_version_id,
                "version_count": len(index.versions),
                "updated_at": index.updated_at,
            }
            for index in indexes
        ]
    }


@router.get("/endpoints/detail")
def endpoint_detail(key: str = Query(...)):
    service = get_service()
    baseline = service.get_baseline(key)
    versions = service.list_versions(key)
    if not versions and baseline is None:
        raise HTTPException(status_code=404, detail=f"endpoint {key} not found")
    return {
        "endpoint_key": key,
        "baseline_version_id": baseline.version_id if baseline else None,
        "version_count": len(versions),
        "versions": [
            {
                "version_id": version.version_id,
                "collected_at": version.collected_at,
                "config_hash": version.config_hash,
                "sample_count": version.sample_count,
            }
            for version in versions
        ],
    }


@router.get("/endpoints/baseline")
def get_baseline(key: str = Query(...)):
    baseline = get_service().get_baseline(key)
    if baseline is None:
        raise HTTPException(status_code=404, detail=f"no baseline for {key}")
    return {
        "endpoint_key": baseline.endpoint_key,
        "version_id": baseline.version_id,
        "collected_at": baseline.collected_at,
        "config_hash": baseline.config_hash,
        "normalized": baseline.normalized,
    }


@router.get("/endpoints/versions")
def list_versions(key: str = Query(...)):
    versions = get_service().list_versions(key)
    return {
        "endpoint_key": key,
        "versions": [
            {
                "version_id": version.version_id,
                "collected_at": version.collected_at,
                "config_hash": version.config_hash,
                "sample_count": version.sample_count,
            }
            for version in versions
        ],
    }


@router.get("/endpoints/judge")
def judge_version(key: str = Query(...), version_id: str = Query(...)):
    report = get_service().judge_version(key, version_id)
    return _report_response(report)


@router.post("/endpoints/approve")
def approve(key: str = Query(...), payload: ApprovePayload | None = None):
    version_id = payload.version_id if payload else None
    actor = payload.actor if payload else "api"
    if not version_id:
        raise HTTPException(status_code=400, detail="version_id is required")
    try:
        index = get_service().approve(key, version_id, actor=actor)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"version {version_id} not found for {key}")
    except Exception as exc:
        from .storage import StorageError

        if isinstance(exc, StorageError):
            raise HTTPException(status_code=503, detail=str(exc))
        raise
    return {
        "endpoint_key": index.endpoint_key,
        "baseline_version_id": index.baseline_version_id,
        "sequence": index.baseline_history[-1].sequence if index.baseline_history else None,
        "action": index.baseline_history[-1].action if index.baseline_history else None,
    }


@router.post("/endpoints/rollback")
def rollback(key: str = Query(...), payload: RollbackPayload | None = None):
    version_id = payload.version_id if payload else None
    if not version_id:
        raise HTTPException(status_code=400, detail="version_id is required")
    try:
        index = get_service().rollback(key, version_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"version {version_id} not found in history for {key}")
    except Exception as exc:
        from .storage import StorageError

        if isinstance(exc, StorageError):
            raise HTTPException(status_code=503, detail=str(exc))
        raise
    return {
        "endpoint_key": index.endpoint_key,
        "baseline_version_id": index.baseline_version_id,
        "sequence": index.baseline_history[-1].sequence if index.baseline_history else None,
        "action": index.baseline_history[-1].action if index.baseline_history else None,
    }


@router.get("/config")
def get_config():
    from .config import config_hash

    cfg = get_service().config
    return {
        "source": cfg.source,
        "data_dir": cfg.data_dir,
        "config_hash": config_hash(cfg),
        "collection": {
            "enabled": cfg.collection.enabled,
            "sample_rate": cfg.collection.sample_rate,
            "include_endpoints": cfg.collection.include_endpoints,
            "exclude_endpoints": cfg.collection.exclude_endpoints,
            "methods": cfg.collection.methods,
        },
        "normalization": {
            "volatile_fields": cfg.normalization.volatile_fields,
            "volatile_query_params": cfg.normalization.volatile_query_params,
            "strip_headers": cfg.normalization.strip_headers,
            "normalize_path_ids": cfg.normalization.normalize_path_ids,
        },
    }


def _report_response(report):
    from fastapi.responses import JSONResponse

    status_codes = {
        "compatible": 200,
        "breaking": 200,
        "no_baseline": 404,
        "error": 400,
    }
    body: dict[str, Any] = {"status": report.status}
    if report.baseline_version_id:
        body["baseline_version_id"] = report.baseline_version_id
    if report.candidate_version_id:
        body["candidate_version_id"] = report.candidate_version_id
    if report.changes:
        body["changes"] = [
            {
                "path": change.path,
                "kind": change.kind,
                "old_type": change.old_type,
                "new_type": change.new_type,
                "breaking": change.breaking,
            }
            for change in report.changes
        ]
    if report.error:
        body["error"] = report.error
    code = status_codes.get(report.status, 500)
    return JSONResponse(status_code=code, content=body)
