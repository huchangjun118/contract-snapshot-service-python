"""HTTP 接口层：把契约快照能力暴露为 REST 端点。

错误映射（可区分的失败状态）：
- UNPARSEABLE_BODY   -> 422
- STORE_UNAVAILABLE  -> 503
- VERSION_NOT_FOUND  -> 404
- NOT_APPROVED       -> 409
- CONFLICT           -> 409
- INVALID_CONFIG     -> 400
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
    FailureKind.STORE_UNAVAILABLE: 503,
    FailureKind.VERSION_NOT_FOUND: 404,
    FailureKind.NOT_APPROVED: 409,
    FailureKind.CONFLICT: 409,
    FailureKind.INVALID_CONFIG: 400,
}


class CaptureRequest(BaseModel):
    endpoint: str
    body: Any = None       # 已解析的请求/响应负载
    raw_body: str | None = None  # 原始报文（未解析的文本），与 body 二选一


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

    @app.post("/capture")
    def capture(req: CaptureRequest) -> dict:
        if req.raw_body is not None:
            # 原始报文：解析失败抛 UNPARSEABLE_BODY(422)，不落盘、不污染基线
            result = service.capture(req.endpoint, req.raw_body)
        elif req.body is not None:
            result = service.capture_payload(req.endpoint, req.body)
        else:
            raise ContractError(
                FailureKind.UNPARSEABLE_BODY,
                "必须提供 body（已解析负载）或 raw_body（原始报文）之一",
            )
        return {
            "endpoint": result.endpoint,
            "version_id": result.version_id,
            "verdict": result.verdict.value,
            "baseline_version_id": result.baseline_version_id,
            "sampled": result.sampled,
        }

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

    return app
