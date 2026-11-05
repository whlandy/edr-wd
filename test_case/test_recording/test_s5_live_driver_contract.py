"""2112 缺口1/2:live 驱动的阶段判定、退出码、负例锁码与源头脱敏。

本地独立探针口径(同监督者 reproduce_live_driver.py):真实
server/policy/GuardedBackend/WindowSession/dispatcher,fake OS 层;
MCP 传输适配为直接 tool 调用;无远端 I/O。覆盖:
- capability=unsupported → V4 blocked、V5/V6 not-run、exit≠0 语义
  (blocked 不是 positive pass);
- capability=strong 对照 → V4 成功、V5 必须使用合法 focus-only 输入
  (action_id=None,非空字符串;空串曾致 product_action_forbidden 假失败);
- 负例锁定精确 code(digest mismatch / not_declared / not_found);
- 全链凭据递归脱敏:run_token 不出现在任何落盘 debug。
"""

from __future__ import annotations

import importlib.util
import json
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

from target import product_policy
from target.action_dispatcher import set_backend_resolver


def _run_driver(monkeypatch, tmp_path, capability):
    import target.server as server
    from test_case.test_recording.test_window_binding_server_wiring import (
        _FakeOSBackend,
    )
    product_policy.reset_for_tests()
    server._binding_facade_holder["facade"] = None
    backend = _FakeOSBackend(
        {"100": {"process_id": 42, "title": "logo1",
                 "root_control_id": "MainRoot"}}, capability=capability)
    if not capability.get("supported"):
        # 真 unsupported 场景:无事件源工厂(协商权威缺失 → 拒绝)。
        class Sourceless:
            def __init__(self, delegate):
                self._delegate = delegate

            def __getattr__(self, name):
                if name == "lifecycle_event_stream":
                    raise AttributeError(name)
                return getattr(self._delegate, name)
        backend = Sourceless(backend)
    monkeypatch.setattr(server, "_backend",
                        product_policy.GuardedBackend(backend,
                                                      "windows_pywinauto"),
                        raising=False)
    monkeypatch.setattr(server, "_backend_kind", "windows_pywinauto")
    monkeypatch.setattr(server, "_recording_service",
                        SimpleNamespace(status=lambda **kw:
                                        {"state": "idle"}))
    set_backend_resolver(lambda: server._backend)
    spec = importlib.util.spec_from_file_location(
        "s5_live_s3_check", "scripts/s5_live_s3_check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.OUT = tmp_path
    module.rpc = lambda *a, **kw: {"result": {}}
    module.call_tool = lambda name, arguments: json.loads(
        getattr(server, name)(**arguments))
    try:
        exit_code = module.main()
        artifact = json.loads(
            (tmp_path / "s3-live-checks.json").read_text())
        return exit_code, artifact
    finally:
        product_policy.reset_for_tests()
        server._binding_facade_holder["facade"] = None


def test_unsupported_capability_is_blocked_not_pass(monkeypatch, tmp_path):
    """capability 不可用:V4=blocked、V5/V6=not-run,正向链**不是**
    positive-flow-pass;退出码不得为 0 成功语义(负例仍在)。"""
    exit_code, artifact = _run_driver(
        monkeypatch, tmp_path,
        {"supported": False, "coverage": "unknown", "generation": 0})
    checks = artifact["checks"]
    assert checks["V4_bind_golden_window"]["bucket"] == \
        "blocked_unsupported"
    assert checks["V5_focus_and_observe"]["bucket"] == "not_run"
    assert checks["V6_reverify_bound"]["bucket"] == "not_run"
    assert artifact["verdict"] != "positive-flow-pass"
    # 负例仍须精确通过(V1a digest mismatch 锁码)。
    assert checks["V1a_bad_digest_refused"]["bucket"] == \
        "negative_gate_pass"
    assert "negative_gate_pass" in artifact["stages"]


def test_strong_capability_positive_flow_and_focus_only_shape(
        monkeypatch, tmp_path):
    """capability=strong 对照:V4 正向成功;V5 focus-only 必须用合法
    协议输入(action_id=None),不得出现 product_action_forbidden 假失败;
    负例 V7 锁定 window_binding_not_declared 精确码。"""
    exit_code, artifact = _run_driver(
        monkeypatch, tmp_path,
        {"supported": True, "coverage": "strong", "generation": 2})
    checks = artifact["checks"]
    assert checks["V4_bind_golden_window"]["bucket"] == \
        "positive_flow_pass", checks["V4_bind_golden_window"]
    assert checks["V5_focus_and_observe"]["bucket"] == \
        "positive_flow_pass", (
        f"focus-only 合法输入下 V5 必须成功,实际 "
        f"{checks['V5_focus_and_observe']}")
    assert checks["V5_focus_and_observe"].get("code") != \
        "product_action_forbidden"
    assert checks["V6_reverify_bound"]["bucket"] == "positive_flow_pass"
    assert checks["V7_undeclared_window_refused"]["bucket"] == \
        "negative_gate_pass"
    assert checks["V7_undeclared_window_refused"]["code"] == \
        "window_binding_not_declared"
    assert artifact["verdict"] == "positive-flow-pass"


def test_never_reports_success_when_positive_items_fail(
        monkeypatch, tmp_path):
    """正向必要项失败 → fail 桶 + 非成功退出码(不再把失败/未运行当
    exit 0)。用非法 titleRegex 的声明触发 V3 fail 路径。"""
    import target.server as server
    from test_case.test_recording.test_window_binding_server_wiring import (
        _FakeOSBackend,
    )
    product_policy.reset_for_tests()
    server._binding_facade_holder["facade"] = None
    backend = _FakeOSBackend(
        {"100": {"process_id": 42, "title": "logo1",
                 "root_control_id": "MainRoot"}},
        capability={"supported": True, "coverage": "strong",
                    "generation": 2})
    monkeypatch.setattr(server, "_backend",
                        product_policy.GuardedBackend(backend,
                                                      "windows_pywinauto"),
                        raising=False)
    monkeypatch.setattr(server, "_backend_kind", "windows_pywinauto")
    monkeypatch.setattr(server, "_recording_service",
                        SimpleNamespace(status=lambda **kw:
                                        {"state": "idle"}))
    set_backend_resolver(lambda: server._backend)
    spec = importlib.util.spec_from_file_location(
        "s5_live_s3_check_bad", "scripts/s5_live_s3_check.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.OUT = tmp_path
    module.rpc = lambda *a, **kw: {"result": {}}
    real_register = server.register_window_declaration

    def refusing_register(declarations_json, run_token):
        return json.dumps({"ok": False,
                           "code": "window_declaration_invalid"})

    monkeypatch.setattr(server, "register_window_declaration",
                        refusing_register)
    module.call_tool = lambda name, arguments: json.loads(
        getattr(server, name)(**arguments))
    try:
        exit_code = module.main()
        artifact = json.loads((tmp_path / "s3-live-checks.json").read_text())
        assert artifact["checks"]["V3_register_golden_declaration"][
            "bucket"] == "fail"
        assert artifact["verdict"] == "fail"
        assert exit_code != 0, "正向必要项失败不得返回 0"
    finally:
        product_policy.reset_for_tests()
        server._binding_facade_holder["facade"] = None


def test_debug_capture_never_persists_credentials(monkeypatch, tmp_path):
    """2112 缺口2:begin 返回(含 run_token)进入 debug 前**源头**递归
    脱敏 — 落盘结构中凭据字段为 <redacted>(合成断言,无真实凭据)。"""
    exit_code, artifact = _run_driver(
        monkeypatch, tmp_path,
        {"supported": True, "coverage": "strong", "generation": 2})
    blob = json.dumps(artifact.get("debug", {}))
    assert "run_token" not in blob or "<redacted>" in blob
    # 任何 debug 值不得包含非 redacted 的 token 字符串形态。
    debug = artifact.get("debug", {})
    for name, value in debug.items():
        raw = json.dumps(value)
        if "run_token" in raw:
            parsed = value
            token_value = parsed.get("run_token")
            assert token_value == "<redacted>", (
                f"{name}: run_token 必须已脱敏,实际 {token_value!r}")
    # V1b-raw 特别核对(此前泄漏点)。
    v1b = debug.get("V1b-raw", {})
    if "run_token" in v1b:
        assert v1b["run_token"] == "<redacted>"
