"""配置：采集范围生效、文件与环境加载、优先级。"""
from contract_snapshot.config import CaptureConfig, NormalizeRules, load_config
from contract_snapshot.service import ContractService
from contract_snapshot.store import SnapshotStore


def test_include_endpoints_limits_capture():
    cfg = CaptureConfig(include_endpoints=("/api/*",))
    svc = ContractService(SnapshotStore(), cfg)
    in_scope = svc.capture_payload("/api/users", {"id": 1})
    out_scope = svc.capture_payload("/internal/health", {"id": 1})
    assert in_scope.sampled is True
    assert out_scope.sampled is False
    assert out_scope.version_id == ""
    # 范围外的接口不产生任何版本
    assert svc.list_versions("/internal/health") == []
    assert len(svc.list_versions("/api/users")) == 1


def test_sampling_disabled_writes_nothing():
    svc = ContractService(SnapshotStore(), CaptureConfig(sampling_rate=0.0))
    r = svc.capture_payload("/ep", {"id": 1})
    assert r.sampled is False
    assert svc.list_versions("/ep") == []
    assert svc.get_baseline("/ep") is None


def test_load_config_from_file(tmp_path):
    cfg_file = tmp_path / "cfg.toml"
    cfg_file.write_text(
        'sampling_rate = 0.5\n'
        'include_endpoints = ["/api/*"]\n'
        'db_path = "/tmp/cfg-test.db"\n'
        '[rules]\n'
        'ignore_paths = ["data.*.debug"]\n'
        'volatile_paths = ["**.token"]\n'
        'ignore_array_order = false\n'
    )
    cfg = load_config(str(cfg_file), env={})
    assert cfg.sampling_rate == 0.5
    assert cfg.include_endpoints == ("/api/*",)
    assert cfg.db_path == "/tmp/cfg-test.db"
    assert cfg.rules.ignore_paths == frozenset({"data.*.debug"})
    assert cfg.rules.volatile_paths == frozenset({"**.token"})
    assert cfg.rules.ignore_array_order is False


def test_env_overrides_file(tmp_path):
    cfg_file = tmp_path / "cfg.toml"
    cfg_file.write_text('sampling_rate = 0.5\ninclude_endpoints = ["/api/*"]\n')
    env = {
        "CONTRACT_SNAPSHOT_SAMPLING_RATE": "0.0",
        "CONTRACT_SNAPSHOT_INCLUDE_ENDPOINTS": "/v1/a,/v1/b",
        "CONTRACT_SNAPSHOT_DB": "/tmp/env-test.db",
    }
    cfg = load_config(str(cfg_file), env=env)
    assert cfg.sampling_rate == 0.0          # 环境变量覆盖配置文件
    assert cfg.include_endpoints == ("/v1/a", "/v1/b")
    assert cfg.db_path == "/tmp/env-test.db"


def test_env_config_path_used(tmp_path):
    cfg_file = tmp_path / "from-env.toml"
    cfg_file.write_text('sampling_rate = 0.25\n')
    cfg = load_config(env={"CONTRACT_SNAPSHOT_CONFIG": str(cfg_file)})
    assert cfg.sampling_rate == 0.25


def test_configured_rules_apply_to_capture():
    # 从配置加载的通配规则在采集中真实生效
    cfg = CaptureConfig(rules=NormalizeRules(
        volatile_paths=frozenset({"**.token"}),
        ignore_paths=frozenset({"meta.debug"}),
    ))
    svc = ContractService(SnapshotStore(), cfg)
    a = svc.capture_payload("/ep", {"user": {"token": "t1"}, "meta": {"debug": 1, "v": 1}})
    svc.approve(a.version_id)
    b = svc.capture_payload("/ep", {"user": {"token": "t9"}, "meta": {"v": 1}})
    assert b.verdict.value == "identical"
