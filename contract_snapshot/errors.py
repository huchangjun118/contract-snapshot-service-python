"""可区分的失败状态定义。

采集、归一化、存储、判定各环节的异常都映射到这里的错误类型，
调用方可以据此区分失败原因，且任何失败都不会污染已有基线。
"""
from __future__ import annotations

from enum import Enum


class FailureKind(str, Enum):
    """失败类别，用于对外暴露可区分的状态。"""

    UNPARSEABLE_BODY = "unparseable_body"      # 请求/响应体无法解析
    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"  # 报文 Content-Type 不支持
    STORE_UNAVAILABLE = "store_unavailable"    # 存储暂时不可用
    VERSION_NOT_FOUND = "version_not_found"    # 目标版本不存在
    NOT_APPROVED = "not_approved"              # 版本尚未被批准
    CONFLICT = "conflict"                      # 并发冲突（可重试）
    INVALID_CONFIG = "invalid_config"          # 配置非法


class ContractError(Exception):
    """所有契约快照相关异常的基类，携带可区分的失败类别。"""

    def __init__(self, kind: FailureKind, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
