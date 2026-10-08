"""原始请求/响应报文解析：从「线上抓到的原始报文」得到负载对象。

采集入口接受完整 HTTP 报文（含起始行与头部）或裸消息体：

1. 形如 ``GET /x HTTP/1.1`` / ``HTTP/1.1 200 OK`` 的文本按 HTTP 报文解析，
   依据 Content-Type 选择解析器（当前支持 application/json）；
2. 裸消息体按 JSON 解析；
3. 无法解析 -> UNPARSEABLE_BODY（稳定、可区分）；
   Content-Type 明确但不支持 -> UNSUPPORTED_MEDIA_TYPE。

任何解析失败都由调用方放弃本次采集：不落盘、不污染基线、不记无变更。
"""
from __future__ import annotations

import json
import re
from typing import Any

from .errors import ContractError, FailureKind

# 请求起始行：METHOD SP TARGET SP HTTP/x.y
_REQUEST_LINE = re.compile(rb"^[A-Z!#$%&'*+.^_`|~-]+\s+\S+\s+HTTP/\d(?:\.\d)?\s*$")
# 响应起始行：HTTP/x.y SP STATUS [REASON]
_STATUS_LINE = re.compile(rb"^HTTP/\d(?:\.\d)?\s+\d{3}(?:\s.*)?$")

_JSON_TYPES = ("application/json",)
_TEXT_JSON_SUFFIX = "+json"


def parse_raw_message(raw: str | bytes) -> Any:
    """从原始报文或裸消息体解析出 JSON 负载。失败抛稳定的 ContractError。"""
    data = _to_bytes(raw)
    head, sep, body = data.partition(b"\r\n\r\n")
    if not sep:
        # 兼容仅用 \n 分隔的报文
        head2, sep2, body2 = data.partition(b"\n\n")
        if sep2:
            head, body = head2, body2
        else:
            return _parse_json(data)  # 没有报文分隔：当作裸消息体

    lines = re.split(rb"\r?\n", head)
    first = lines[0].strip() if lines else b""
    if not (_REQUEST_LINE.match(first) or _STATUS_LINE.match(first)):
        # 不是合法 HTTP 起始行：整段按裸 JSON 处理
        return _parse_json(data)

    content_type = _header(lines[1:], "content-type")
    return _parse_body_by_type(content_type, body)


def _to_bytes(raw: str | bytes) -> bytes:
    if isinstance(raw, bytes):
        return raw
    if isinstance(raw, str):
        try:
            return raw.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise ContractError(
                FailureKind.UNPARSEABLE_BODY, f"报文不是合法 UTF-8: {exc}"
            ) from exc
    raise ContractError(
        FailureKind.UNPARSEABLE_BODY, f"不支持的报文类型: {type(raw).__name__}"
    )


def _header(lines: list[bytes], name: str) -> str | None:
    prefix = name.lower() + ":"
    for line in lines:
        if line[: len(prefix)].lower() == prefix.encode("ascii"):
            return line[len(prefix):].strip().decode("ascii", "ignore").lower()
    return None


def _is_json_content_type(content_type: str | None) -> bool:
    if not content_type:
        return False
    media = content_type.split(";", 1)[0].strip().lower()
    return media in _JSON_TYPES or media.endswith(_TEXT_JSON_SUFFIX)


def _parse_body_by_type(content_type: str | None, body: bytes) -> Any:
    if content_type is not None and not _is_json_content_type(content_type):
        raise ContractError(
            FailureKind.UNSUPPORTED_MEDIA_TYPE,
            f"不支持的 Content-Type: {content_type!r}，当前仅支持 application/json",
        )
    # content_type 为空或为 JSON：按 JSON 解析
    return _parse_json(body)


def _parse_json(data: bytes) -> Any:
    try:
        text = data.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise ContractError(
            FailureKind.UNPARSEABLE_BODY, f"报文不是合法 UTF-8: {exc}"
        ) from exc
    if not text:
        raise ContractError(
            FailureKind.UNPARSEABLE_BODY, "报文为空，无法解析为负载"
        )
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ContractError(
            FailureKind.UNPARSEABLE_BODY,
            f"报文无法解析为 JSON: 第 {exc.lineno} 行第 {exc.colno} 列: {exc.msg}",
        ) from exc
