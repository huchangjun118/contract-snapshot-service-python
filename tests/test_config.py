"""配置来源/优先级、采集范围、路径通配匹配测试。"""
import json

import pytest

from contract_snapshot.config import (
    CaptureConfig,
    NormalizeRules,
    PathMatcher,
    load_config,
)
from contract_snapshot.errors import ContractError, FailureKind
from contract_snapshot.normalize import normalize_payload


# ---- 路径通配 -----------------------------------------------------------

def test_path_matcher_exact_nested_path():
    m = PathMatcher({"response.data.debug"})
    assert m.matches(("response", "data", "debug"))
    assert not m.matches(("response", "data", "info"))
    assert not m.matches(("response", "debug"))


def test_path_matcher_single_star_one_segment():
    m = PathMatcher({"data.*.secret"})
    assert m.matches(("data", "user", "secret"))
    assert not m.matches(("data", "user", "nested", "secret"))


def test_path_matcher_double_star_crosses_layers():
    m = PathMatcher({"**.token"})
    assert m.matches(("token",))
    assert m.matches(("meta", "token"))
    assert m.matches(("a", "b", "c", "token"))
    assert not m.matches(("a", "tokens"))


def test_path_matcher_bare_name_any_depth():
    m = PathMatcher({"timestamp"})
    assert m.matches(("timestamp",))
    assert m.matches(("data", "timestamp"))
    assert m.matches(("data", "page", "timestamp"))
    assert not m.matches(("data", "timestamps"))


# ---- 归一化与通配结合 ----------------------------------------------------

def test_ignore_nested_path_only():
    rules = NormalizeRules(ignore_paths=frozenset({"response.data.debug"}))
    payload = {
        "response": {
            "data": {"id": 1, "debug": {"x": 1}},
            "debug": "keep-me",
        }
    }
    out = normalize_payload(payload, rules)
    assert out == {"response": {"data": {"id": 1}, "debug": "keep-me"}}


def test_volatile_wildcard_deep_field():
    rules = NormalizeRules(volatile_paths=frozenset({"**.token"}))
    a = {"auth": {"user": {"token": "abc"}}, "token": "xyz"}
    b = {"auth": {"user": {"token": "---"}}, "token": "???"}
    assert normalize_payload(a, rules) == normalize_payload(b, rules)
    out = normalize_payload(a, rules)
    assert out["auth"]["user"]["token"] == "<volatile>"
    assert out["token"] == "<volatile>"


def test_same_contract_comparable_across_rules_and_time():
    """同一份契约 + 同一套规则，在不同时间/进程得到同一份快照。"""
    rules = NormalizeRules(
        ignore_paths=frozenset({"meta.trace.*"}),
        volatile_paths=frozenset({"**.timestamp"}),
    )
    p1 = {"meta": {"trace": {"id": 1}, "timestamp": "t1"}, "data": [3, 1, 2]}
    p2 = {"data": [2, 3, 1], "meta": {"timestamp": "t2", "trace": {"id": 9}}}
    assert normalize_payload(p1, rules) == normalize_payload(p2, rules)


# ---- 采集范围 -----------------------------------------------------------

def _svc(store, **kw):
    from contract_snapshot.service import ContractService

    return ContractService(store, CaptureConfig(**kw))


def test_include_scope_filters_endpoints():
    from contract_snapshot.store import SnapshotStore
    from contract_snapshot.service import ContractService

    store = SnapshotStore(":memory:")
    svc = ContractService(
        store, CaptureConfig(include_endpoints=frozenset({"/v1/*"}))
    )
    hit = svc.capture_payload("/v1/users", {"id": 1})
    miss = svc.capture_payload("/v2/users", {"id": 1})
    assert hit.sampled is True and hit.in_scope is True
    assert miss.sampled is False and miss.in_scope is False
    assert miss.verdict is None
    assert svc.list_versions("/v1/users") and not svc.list_versions("/v2/users")


def test_exclude_scope_overrides():
    from contract_snapshot.store import SnapshotStore
    from contract_snapshot.service import ContractService

    svc = ContractService(
        SnapshotStore(":memory:"),
        CaptureConfig(
            include_endpoints=frozenset({"/v1/*"}),
            exclude_endpoints=frozenset({"/v1/health"}),
        ),
    )
    r = svc.capture_payload("/v1/health", {"ok": 1})
    assert r.in_scope is False and r.sampled is False
    assert svc.list_versions("/v1/health") == []


def test_sampling_disabled_returns_unsampled_and_writes_nothing():
    from contract_snapshot.store import SnapshotStore
    from contract_snapshot.service import ContractService

    svc = ContractService(
        SnapshotStore(":memory:"), CaptureConfig(sampling_rate=0.0)
    )
    r = svc.capture_payload("/ep", {"id": 1})
    assert r.sampled is False and r.verdict is None and r.version_id == ""
    assert svc.list_versions("/ep") == []
    assert svc.get_last_judgment("/ep") is None  # 未采样不记判定


# ---- 配置文件 / 环境变量 ------------------------------------------------

def test_load_config_defaults():
    cfg = load_config(env={})
    assert cfg.sampling_rate == 1.0
    assert cfg.include_endpoints == frozenset()
    assert cfg.rules.ignore_array_order is True


def test_load_config_from_file(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({
        "sampling_rate": 0.5,
        "include_endpoints": ["/a/*", "/b"],
        "rules": {
            "ignore_paths": ["debug"],
            "volatile_paths": ["nonce"],
            "ignore_array_order": False,
        },
    }), encoding="utf-8")
    cfg = load_config(config_path=p, env={})
    assert cfg.sampling_rate == 0.5
    assert cfg.include_endpoints == frozenset({"/a/*", "/b"})
    assert cfg.rules.ignore_paths == frozenset({"debug"})
    assert cfg.rules.volatile_paths == frozenset({"nonce"})
    assert cfg.rules.ignore_array_order is False


def test_env_overrides_file(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"sampling_rate": 0.5}), encoding="utf-8")
    env = {
        "CONTRACT_SNAPSHOT_SAMPLING_RATE": "0.25",
        "CONTRACT_SNAPSHOT_INCLUDE_ENDPOINTS": " /x , /y ",
        "CONTRACT_SNAPSHOT_IGNORE_PATHS": "a.b, c",
        "CONTRACT_SNAPSHOT_IGNORE_ARRAY_ORDER": "false",
    }
    cfg = load_config(config_path=p, env=env)
    assert cfg.sampling_rate == 0.25
    assert cfg.include_endpoints == frozenset({"/x", "/y"})
    assert cfg.rules.ignore_paths == frozenset({"a.b", "c"})
    assert cfg.rules.ignore_array_order is False


def test_env_config_file_pointer(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"sampling_rate": 0.1}), encoding="utf-8")
    cfg = load_config(env={"CONTRACT_SNAPSHOT_CONFIG": str(p)})
    assert cfg.sampling_rate == 0.1


def test_missing_explicit_config_file_is_error(tmp_path):
    with pytest.raises(ContractError) as ei:
        load_config(config_path=tmp_path / "nope.json", env={})
    assert ei.value.kind is FailureKind.INVALID_CONFIG


def test_malformed_config_file_is_error(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(ContractError) as ei:
        load_config(config_path=p, env={})
    assert ei.value.kind is FailureKind.INVALID_CONFIG


def test_invalid_sampling_rate_rejected():
    with pytest.raises(ContractError) as ei:
        CaptureConfig(sampling_rate=1.5)
    assert ei.value.kind is FailureKind.INVALID_CONFIG


def test_env_sampling_rate_must_be_number():
    with pytest.raises(ContractError) as ei:
        load_config(env={"CONTRACT_SNAPSHOT_SAMPLING_RATE": "soon"})
    assert ei.value.kind is FailureKind.INVALID_CONFIG
