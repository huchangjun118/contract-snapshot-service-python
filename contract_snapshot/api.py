"""HTTP 接口层：把契约快照能力暴露为 REST 端点。

错误映射（可区分的失败状态）：
- UNPARSEABLE_BODY        -> 422
- UNSUPPORTED_MEDIA_TYPE  -> 415
- STORE_UNAVAILABLE       -> 503
- VERSION_NOT_FOUND       -> 404
- NOT_APPROVED            -> 409
- CONFLICT                -> 409
- INVALID_CONFIG          -> 400

采集入口：
- POST /capture           传入已解析负载 {"endpoint", "body"}；
- POST /capture/raw/{endpoint} 传入原始请求/响应报文（文本/字节，
  支持完整 HTTP 报文或裸 JSON），无法解析返回 422/415，不落盘。
"""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .errors import ContractError, FailureKind
from .service import ContractService

_STATUS = {
    FailureKind.UNPARSEABLE_BODY: 422,
    FailureKind.UNSUPPORTED_MEDIA_TYPE: 415,
    FailureKind.STORE_UNAVAILABLE: 503,
    FailureKind.VERSION_NOT_FOUND: 404,
    FailureKind.NOT_APPROVED: 409,
    FailureKind.CONFLICT: 409,
    FailureKind.INVALID_CONFIG: 400,
}


class CaptureRequest(BaseModel):
    endpoint: str
    body: Any  # 已解析的请求/响应负载


class VersionRequest(BaseModel):
    version_id: str


class RollbackRequest(BaseModel):
    endpoint: str
    version_id: str


def create_app(service: ContractService) -> FastAPI:
    app = FastAPI(title="contract-snapshot-service")

    @app.exception_handler(ContractError)
    async def _contract_error(_: Request, exc: ContractError) -> JSONResponse:
        return JSONResponse(
            status_code=_STATUS.get(exc.kind, 500),
            content={"error": {"kind": exc.kind.value, "message": exc.message}},
        )

    def _capture_payload(result) -> dict:
        return {
            "endpoint": result.endpoint,
            "version_id": result.version_id,
            "verdict": result.verdict_value,
            "baseline_version_id": result.baseline_version_id,
            "sampled": result.sampled,
            "in_scope": result.in_scope,
            "rules_match": result.rules_match,
        }

    @app.post("/capture")
    def capture(req: CaptureRequest) -> dict:
        result = service.capture_payload(req.endpoint, req.body)
        return _capture_payload(result)

    @app.post("/capture/raw/{endpoint:path}")
    async def capture_raw(endpoint: str, request: Request) -> dict:
        # 读取原始字节交由解析器处理；解析失败由异常处理器稳定映射。
        raw = await request.body()
        result = service.capture("/" + endpoint.lstrip("/"), raw)
        return _capture_payload(result)

    @app.post("/approve")
    def approve(req: VersionRequest) -> dict:
        record = service.approve(req.version_id)
        return {"version_id": record.version_id, "approved": record.approved}

    @app.post("/rollback")
    def rollback(req: RollbackRequest) -> dict:
        record = service.rollback(req.endpoint, req.version_id)
        return {"endpoint": record.endpoint, "baseline_version_id": record.version_id}

    @app.get("/baseline/{endpoint:path}")
    def baseline(endpoint: str) -> dict:
        record = service.get_baseline("/" + endpoint.lstrip("/"))
        if record is None:
            return {"endpoint": endpoint, "baseline_version_id": None}
        return {"endpoint": record.endpoint, "baseline_version_id": record.version_id}

    @app.get("/versions/{endpoint:path}")
    def versions(endpoint: str, limit: int = 100) -> dict:
        records = service.list_versions("/" + endpoint.lstrip("/"), limit)
        return {
            "endpoint": endpoint,
            "versions": [
                {
                    "version_id": r.version_id,
                    "approved": r.approved,
                    "created_seq": r.created_seq,
                }
                for r in records
            ],
        }

    @app.get("/judgment/{endpoint:path}")
    def judgment(endpoint: str) -> dict:
        rec = service.get_last_judgment("/" + endpoint.lstrip("/"))
        if rec is None:
            return {"endpoint": endpoint, "judgment": None}
        return {
            "endpoint": endpoint,
            "judgment": {
                "version_id": rec.version_id,
                "baseline_version_id": rec.baseline_version_id,
                "verdict": rec.verdict,
                "rules_match": rec.rules_match,
                "created_seq": rec.created_seq,
            },
        }

    return app
