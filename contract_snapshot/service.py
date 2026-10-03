"""编排层：采集、比较、批准、回滚。

失败语义：
- 响应体无法解析 -> FailureKind.UNPARSEABLE_BODY，不落盘；
- 存储不可用 -> FailureKind.STORE_UNAVAILABLE，由存储层抛出；
- 任何失败都不会写入半成品，也不会被当成「无变更」。
"""
from __future__ import annotations

import json
import random
import threading
from dataclasses import dataclass
from typing import Any

from .compat import Verdict, compare_schemas
from .config import CaptureConfig
from .errors import ContractError, FailureKind
from .normalize import canonical_dumps, normalize_payload
from .schema import extract_schema
from .store import SnapshotRecord, SnapshotStore


@dataclass(frozen=True)
class CaptureResult:
    """一次采集的结果。"""

    endpoint: str
    version_id: str
    verdict: Verdict                 # 与当前基线比较的结论
    baseline_version_id: str | None  # 比较时使用的基线（可能为空）
    sampled: bool                    # 是否被采样命中（未命中则不落盘）


class ContractService:
    """契约快照服务。线程安全，可并发采集与判定。"""

    def __init__(self, store: SnapshotStore, config: CaptureConfig | None = None) -> None:
        self._store = store
        self._config = config or CaptureConfig()
        # 批准与回滚互斥，保证并发时结果确定
        self._promote_lock = threading.Lock()

    # ---- 采集 -------------------------------------------------

    def capture(self, endpoint: str, body: str | bytes) -> CaptureResult:
        """从真实响应体采集契约快照并与当前基线比较。

        体无法解析时抛 UNPARSEABLE_BODY，不落盘、不影响基线。
        """
        payload = self._parse_body(body)
        return self.capture_payload(endpoint, payload)

    def capture_payload(self, endpoint: str, payload: Any) -> CaptureResult:
        """从已解析的负载采集（供测试与内部复用）。"""
        if not self._sampled():
            baseline = self._store.get_baseline(endpoint)
            return CaptureResult(endpoint, "", Verdict.IDENTICAL,
                                 baseline.version_id if baseline else None, False)

        normalized = normalize_payload(payload, self._config.rules)
        schema = extract_schema(normalized)
        fp = self._config.rules.fingerprint()
        # 版本身份取自结构 schema 的哈希而非具体取值：
        # 同一契约在不同时间、不同进程采集得到同一版本。
        import hashlib

        schema_hash = hashlib.sha256(
            canonical_dumps(schema).encode("utf-8")
        ).hexdigest()[:16]
        version_id = f"{endpoint}:{schema_hash}:{fp}"

        record = self._store.save_version(version_id, endpoint, schema, fp)
        return CaptureResult(
            endpoint=endpoint,
            version_id=record.version_id,
            verdict=self._judge_against_baseline(endpoint, schema, fp),
            baseline_version_id=(b.version_id if (b := self._store.get_baseline(endpoint)) else None),
            sampled=True,
        )

    # ---- 批准与回滚 ---------------------------------------------

    def approve(self, version_id: str) -> SnapshotRecord:
        """显式批准一个版本并提升为基线。未批准版本永不成基线。"""
        with self._promote_lock:
            return self._store.approve_and_promote(version_id)

    def rollback(self, endpoint: str, version_id: str) -> SnapshotRecord:
        """回滚基线到任意历史版本（该版本必须已存在且已批准）。

        与并发批准互斥；任一时刻读到的基线都是完整已提交版本。
        """
        with self._promote_lock:
            record = self._store.get_version(version_id)
            if record is None:
                raise ContractError(
                    FailureKind.VERSION_NOT_FOUND, f"版本不存在: {version_id}"
                )
            if record.endpoint != endpoint:
                raise ContractError(
                    FailureKind.VERSION_NOT_FOUND,
                    f"版本 {version_id} 不属于接口 {endpoint}",
                )
            if not record.approved:
                raise ContractError(
                    FailureKind.NOT_APPROVED,
                    f"版本 {version_id} 尚未批准，不能作为回滚目标",
                )
            return self._store.approve_and_promote(version_id)

    # ---- 查询 -------------------------------------------------

    def get_baseline(self, endpoint: str) -> SnapshotRecord | None:
        return self._store.get_baseline(endpoint)

    def list_versions(self, endpoint: str, limit: int = 100) -> list[SnapshotRecord]:
        return self._store.list_versions(endpoint, limit)

    # ---- 内部 -------------------------------------------------

    def _judge_against_baseline(
        self, endpoint: str, schema: dict, rules_fingerprint: str
    ) -> Verdict:
        """与当前基线比较。规则指纹不一致时按当前配置重新提取基线侧不可比，
        视为兼容演进（不重新解释历史判定，只影响新判定）。"""
        baseline = self._store.get_baseline(endpoint)
        if baseline is None:
            return Verdict.IDENTICAL  # 首个版本：无基线可比，记为无变更
        if baseline.rules_fingerprint != rules_fingerprint:
            # 配置已变更：历史基线不与新快照直接比较，避免重新解释历史
            return Verdict.COMPATIBLE
        return compare_schemas(baseline.schema, schema)

    def _sampled(self) -> bool:
        rate = self._config.sampling_rate
        if rate <= 0.0:
            return False
        if rate >= 1.0:
            return True
        return random.random() < rate

    @staticmethod
    def _parse_body(body: str | bytes) -> Any:
        try:
            if isinstance(body, bytes):
                body = body.decode("utf-8")
            return json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ContractError(
                FailureKind.UNPARSEABLE_BODY, f"响应体无法解析: {exc}"
            ) from exc
