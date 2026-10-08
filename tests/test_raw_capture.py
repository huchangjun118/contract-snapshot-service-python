"""原始报文采集：可解析则落盘；无法解析/不支持类型为可区分且稳定的失败，
失败采集不落盘、不污染基线、不被记成无变更。"""
import pytest

from contract_snapshot.errors import ContractError, FailureKind
from contract_snapshot.parser import parse_raw_message
from contract_snapshot.service import ContractService
from contract_snapshot.store import SnapshotStore


# ---- 解析器单元测试 ------------------------------------------------------

def test_parse_bare_json_object():
    assert parse_raw_message('{"id": 1}') == {"id": 1}


def test_parse_bare_json_array_bytes():
    assert parse_raw_message(b'[1, 2, 3]') == [1, 2, 3]


def test_parse_http_request_envelope():
    raw = (
        "POST /api/users HTTP/1.1\r\n"
        "Host: example.com\r\n"
        "Content-Type: application/json\r\n"
        "\r\n"
        '{"id": 1, "name": "a"}'
    )
    assert parse_raw_message(raw) == {"id": 1, "name": "a"}


def test_parse_http_response_envelope_with_charset():
    raw = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: application/json; charset=utf-8\r\n"
        "\r\n"
        '{"ok": true}'
    )
    assert parse_raw_message(raw) == {"ok": True}


def test_parse_lf_separated_envelope():
    raw = (
        "GET /x HTTP/1.1\n"
        "content-type: application/json\n"
        "\n"
        '{"v": 1}'
    )
    assert parse_raw_message(raw) == {"v": 1}


@pytest.mark.parametrize("bad", ["", "   ", "{not json", b"\xff\xfe{", "\x00\x01"])
def test_unparseable_kinds_are_stable(bad):
    with pytest.raises(ContractError) as ei:
        parse_raw_message(bad)
    assert ei.value.kind is FailureKind.UNPARSEABLE_BODY
    # 稳定：同一输入多次失败类别一致
    with pytest.raises(ContractError) as ei2:
        parse_raw_message(bad)
    assert ei2.value.kind is FailureKind.UNPARSEABLE_BODY


def test_unsupported_content_type_distinguishable():
    raw = (
        "POST /x HTTP/1.1\r\n"
        "Content-Type: text/plain\r\n"
        "\r\n"
        "plain text body"
    )
    with pytest.raises(ContractError) as ei:
        parse_raw_message(raw)
    assert ei.value.kind is FailureKind.UNSUPPORTED_MEDIA_TYPE


# ---- 服务层：失败不落盘、不污染基线 --------------------------------------

def test_failed_raw_capture_does_not_persist():
    svc = ContractService(SnapshotStore(":memory:"))
    v1 = svc.capture_payload("/ep", {"id": 1}).version_id
    svc.approve(v1)

    with pytest.raises(ContractError) as ei:
        svc.capture("/ep", "<<<not a message>>>")
    assert ei.value.kind is FailureKind.UNPARSEABLE_BODY

    assert svc.get_baseline("/ep").version_id == v1
    assert len(svc.list_versions("/ep")) == 1
    # 失败采集不产生判定记录（不被记成无变更）
    j = svc.get_last_judgment("/ep")
    assert j is None or j.verdict != "identical" or j.version_id == v1


def test_unsupported_media_capture_does_not_persist():
    svc = ContractService(SnapshotStore(":memory:"))
    v1 = svc.capture_payload("/ep", {"id": 1}).version_id
    svc.approve(v1)
    raw = "HTTP/1.1 200 OK\r\nContent-Type: text/xml\r\n\r\n<a>1</a>"
    with pytest.raises(ContractError) as ei:
        svc.capture("/ep", raw)
    assert ei.value.kind is FailureKind.UNSUPPORTED_MEDIA_TYPE
    assert len(svc.list_versions("/ep")) == 1
    assert svc.get_baseline("/ep").version_id == v1


def test_valid_raw_capture_persists_and_judges():
    svc = ContractService(SnapshotStore(":memory:"))
    raw = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: application/json\r\n"
        "\r\n"
        '{"id": 1}'
    )
    r = svc.capture("/ep", raw)
    assert r.sampled is True and r.verdict is not None
    assert len(svc.list_versions("/ep")) == 1


def test_raw_capture_skipped_when_disabled():
    """采集关闭时不读/解析报文，返回未采样且不写任何数据。"""
    from contract_snapshot.config import CaptureConfig

    svc = ContractService(
        SnapshotStore(":memory:"), CaptureConfig(sampling_rate=0.0)
    )
    # 即便报文完全无法解析，关闭采集时也直接返回未采样（不报错、不落盘）
    r = svc.capture("/ep", b"\xff\xff")
    assert r.sampled is False and r.in_scope is True
    assert svc.list_versions("/ep") == []


def test_raw_capture_skipped_when_out_of_scope():
    from contract_snapshot.config import CaptureConfig

    svc = ContractService(
        SnapshotStore(":memory:"),
        CaptureConfig(include_endpoints=frozenset({"/included/*"})),
    )
    r = svc.capture("/other/ep", "{not parseable")
    assert r.sampled is False and r.in_scope is False
    assert svc.list_versions("/other/ep") == []
