"""编排层：采集、比较、批准、回滚。

失败与跳过语义：
- 接口不在采集范围或采样未命中 -> 返回 sampled=False 的结果，
  不解析、不落盘、不产生版本、不记判定（不被当成「无变更」）；
- 原始报文无法解析 -> FailureKind.UNPARSEABLE_BODY /
  UNSUPPORTED_MEDIA_TYPE，不落盘；
- 存储不可用 -> FailureKind.STORE_UNAVAILABLE，由存储层抛出；
- 任何失败都不会写入半成品，也不会覆盖已有基线。

配置变更与判定（关键语义）：
- 兼容性判定永远基于「当前基线 schema」与「候选 schema」的结构比较，
  与双方生成时使用的归一化规则是否一致无关；
- 因而删除字段、收紧类型这类破坏不会因为换了配置而被降级为
  兼容/不变；rules_match 仅作为标注暴露，不参与判定；
- 每次判定在采集时固化到 judgments 表，之后改配置不会改写
  已落库版本的判定依据。
"""
from __future__ import annotations

import hashlib
import random
import threading
from dataclasses import dataclass
from typing import Any

from .compat import Verdict, compare_schemas
from .config import CaptureConfig
from .errors import ContractError, FailureKind
from .normalize import canonical_dumps, normalize_payload
from .parser import parse_raw_message
from .schema import extract_schema
from .store import JudgmentRecord, SnapshotRecord, SnapshotStore


@dataclass(frozen=True)
class CaptureResult:
    """一次采集的结果。未采样/不在范围内时 sampled=False 且无版本。"""

    endpoint: str
    version_id: str
    verdict: Verdict | None        # 与当前基线比较的结论；未采样为 None
    baseline_version_id: str | None  # 比较时使用的基线（可能为空）
    sampled: bool                  # 是否在范围内且被采样命中
    rules_match: bool = True       # 候选与基线的归一化规则是否同套（仅标注）
    in_scope: bool = True          # 接口是否在采集范围内

    @property
    def verdict_value(self) -> str | None:
        return self.verdict.value if self.verdict is not None else None


def _unsampled(endpoint: str, baseline_version_id: str | None, in_scope: bool) -> CaptureResult:
    return CaptureResult(
        endpoint=endpoint,
        version_id="",
        verdict=None,
        baseline_version_id=baseline_version_id,
        sampled=False,
        rules_match=True,
        in_scope=in_scope,
    )


class ContractService:
    """契约快照服务。线程安全，可并发采集与判定。"""

    def __init__(self, store: SnapshotStore, config: CaptureConfig | None = None) -> None:
        self._store = store
        self._config = config or CaptureConfig()
        # 批准与回滚互斥，保证并发时结果确定
        self._promote_lock = threading.Lock()

    # ---- 采集 -------------------------------------------------

    def capture(self, endpoint: str, raw: str | bytes) -> CaptureResult:
        """从线上抓到的原始请求/响应报文采集契约快照。

        先做范围/采样判定（未命中不读报文、不落盘）；
        报文无法解析或类型不支持时抛稳定、可区分的 ContractError，
        该次采集不落盘、不覆盖基线、不被记成无变更。
        """
        if not self._config.endpoint_in_scope(endpoint) or not self._sampled():
            baseline = self._store.get_baseline(endpoint)
            return _unsampled(
                endpoint,
                baseline.version_id if baseline else None,
                self._config.endpoint_in_scope(endpoint),
            )
        payload = parse_raw_message(raw)
        return self.capture_payload(endpoint, payload)

    def capture_payload(self, endpoint: str, payload: Any) -> CaptureResult:
        """从已解析的负载采集（供测试与内部复用）。"""
        in_scope = self._config.endpoint_in_scope(endpoint)
        if not in_scope or not self._sampled():
            baseline = self._store.get_baseline(endpoint)
            return _unsampled(
                endpoint, baseline.version_id if baseline else None, in_scope
            )

        normalized = normalize_payload(payload, self._config.rules)
        schema = extract_schema(normalized)
        fp = self._config.rules.fingerprint()
        # 版本身份取自结构 schema 的哈希与规则指纹：
        # 同一契约 + 同一套规则在不同时间、不同进程采集得到同一版本。
        schema_hash = hashlib.sha256(
            canonical_dumps(schema).encode("utf-8")
        ).hexdigest()[:16]
        version_id = f"{endpoint}:{schema_hash}:{fp}"

        record = self._store.save_version(version_id, endpoint, schema, fp)

        baseline = self._store.get_baseline(endpoint)
        baseline_id = baseline.version_id if baseline else None
        rules_match = baseline is None or baseline.rules_fingerprint == fp
        if baseline is None:
            verdict = Verdict.IDENTICAL  # 首个版本：无基线可比
        else:
            # 始终按结构比较：删除字段/收紧类型无论规则是否同套都判破坏。
            verdict = compare_schemas(baseline.schema, schema)

        # 判定依据在采集时固化；之后配置变更不影响该记录。
        self._store.save_judgment(
            endpoint, record.version_id, baseline_id,
            verdict.value, rules_match, record.created_seq,
        )
        return CaptureResult(
            endpoint=endpoint,
            version_id=record.version_id,
            verdict=verdict,
            baseline_version_id=baseline_id,
            sampled=True,
            rules_match=rules_match,
            in_scope=True,
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

    def get_last_judgment(self, endpoint: str) -> JudgmentRecord | None:
        """读取该接口最近一次固化的采集判定（历史结论，不随配置重算）。"""
        return self._store.get_last_judgment(endpoint)

    # ---- 内部 -------------------------------------------------

    def _sampled(self) -> bool:
        rate = self._config.sampling_rate
        if rate <= 0.0:
            return False
        if rate >= 1.0:
            return True
        return random.random() < rate
