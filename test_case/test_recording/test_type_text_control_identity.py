# gui.type_text 输入控件定位契约（0637 实机失败复现）与 _probe_backend 吞码。
#
# 0637 实证：同一 target_ref（automationId=FixtureInput/Edit）下 step1 gui.click
# 以 automation_id 定位成功，step2 gui.type_text 却派发 class_name/control_id
# （物化只取这两个字段），目标端 _find_control 找不到 → backend_error
# "Control not found"。
#
# 修复契约：type_text 与 click 使用同一控件身份来源——优先
# automation_id+control_type（观测投影的稳定身份），仅当 automation_id 缺失时
# 才回退 control_id/class_name（旧 Win32 身份），避免组合过滤被陈旧字段破坏。
# 另：run_case 的 _probe_backend 曾把 refresh 的 BackendUnavailable 吞为 bool，
# 探测失败必须保留原始 code。
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.execution.executor import AtomicExecutor, BackendUnavailable
from agent.recording.replay import GoldenStepMaterializer, load_golden_trace

pytestmark = pytest.mark.unit

GOLDEN = Path(
    "result-report/20261025-0520-replay-profile-binding"
    "/recompile-qa-profile/independent_unique_0426/golden-trace.json")


def _observation():
    """贴近 0637 真实观测投影：WinForms Edit 的稳定身份是 automation_id，
    class_name 为动态后缀串、control_id 观测缺失。"""
    return {
        "snapshot_id": "OBS-0001",
        "active_window": {"process_name": "powershell.exe"},
        "window_identity": {"native_id": "w-1", "generation": 1,
                            "process_name": "powershell.exe", "title": TITLE
                            if (TITLE := "EDR-WD Native Recording QA [qa-dsh-fixed-1025]") else ""},
        "controls": [{
            "target_id": "T0002",
            "process_name": "powershell.exe",
            "window_title": "EDR-WD Native Recording QA [qa-dsh-fixed-1025]",
            "control_type": "Edit",
            "automation_id": "FixtureInput",
            "name": "QA input",
            "text": "baseline",
            "fingerprint": "sha256:ffe580a61b04b931b67ea715e8c38af0e7b276f3bbe4e51177c4acb7dc8f4f96",
            "class_name": "WindowsForms10.EDIT.app.0.2b53634e1",
            "control_id": None,
            "rect": [20, 20, 200, 40],
            "visible": True,
        }],
    }


def _type_text_step():
    golden = load_golden_trace(GOLDEN)
    return next(s for s in golden.steps if s.step_id == "step-0002")


def test_type_text_materializes_automation_identity():
    from agent.recording.replay import _recorded_to_atomic
    materializer = GoldenStepMaterializer(load_golden_trace(GOLDEN))
    observation = _observation()
    recorded = _type_text_step()
    materialized = materializer(_recorded_to_atomic(recorded, 2), observation)
    assert materialized.args.get("automation_id") == "FixtureInput"
    assert materialized.args.get("control_type") == "Edit"
    # automation_id 身份存在时不得携带陈旧 class_name/control_id，
    # 否则目标端组合过滤（AND）会被动态类名/缺省 control_id 破坏。
    assert "class_name" not in materialized.args
    assert "control_id" not in materialized.args
    assert materialized.args["text"] == "baselineaindependent-0426unique0427"


def test_type_text_falls_back_to_legacy_identity_without_automation_id():
    # 无 automation 身份的旧控件（Win32 control_id/class_name 观测）才回退：
    # selector 与观测投影都没有 automationId。
    from agent.recording.models import GoldenTrace, RecordedStep, ReplaySelector
    golden = GoldenTrace(
        name="legacy identity",
        source_recording={"schema": "edr.desktop-recording/v1",
                          "sha256": "sha256:raw"},
        catalog={"version": "1.0.0", "digest": "sha256:catalog"},
        environment={"backend": "windows_pywinauto", "target": "win-dev"},
        steps=(RecordedStep(
            "step-0002", "gui.type_text", {"text": "hello"},
            ReplaySelector(
                window={"processName": "powershell.exe",
                        "titleRegex": "^EDR\\-WD Native Recording QA \\[qa\\-dsh\\-fixed\\-1025\\]$"},
                control={"controlType": "Edit", "name": "QA input"},
            ),
        ),),
        status="ready", cleanup=(),
    )
    materializer = GoldenStepMaterializer(golden)
    observation = _observation()
    observation["controls"][0]["automation_id"] = None
    observation["controls"][0]["control_type"] = "Edit"
    del observation["controls"][0]["text"]  # 无 automationId 的旧控件按 name 定位
    from agent.recording.replay import _recorded_to_atomic
    recorded = next(s for s in golden.steps if s.step_id == "step-0002")
    materialized = materializer(_recorded_to_atomic(recorded, 2), observation)
    assert materialized.args.get("class_name") == "WindowsForms10.EDIT.app.0.2b53634e1"
    assert "automation_id" not in materialized.args


def test_dispatch_routes_automation_identity_to_backend_type_text(monkeypatch):
    # 生产路由：execute_action → target.action_dispatcher.dispatch →
    # backend.type_text(**args)。args 里的 automation_id/control_type 必须原样
    # 作为 backend 方法 kwargs 到达（Step 10 method(**args)）。
    from target.action_dispatcher import dispatch as dispatch_fn
    import importlib

    captured = {}

    # fake backend 用具名签名（非 **kwargs），签名漂移即 TypeError——
    # 102 轮验收教训：**kwargs 会掩盖真实适配层签名。
    class _FakeBackend:
        def type_text(self, text=None, control_id=None, class_name=None,
                      string=None, automation_id=None, control_type=None):
            captured.update(text=text, control_id=control_id,
                            class_name=class_name, string=string,
                            automation_id=automation_id,
                            control_type=control_type)
            return {"ok": True, "method": "set_edit_text"}

        def get_window_lock(self):
            return {"locked": True, "process_name": "powershell.exe",
                    "window_title": "EDR-WD Native Recording QA [qa-dsh-fixed-1025]"}

    # 包属性 `target.action_dispatcher.dispatch` 被 __init__ 导出的同名函数
    # 遮蔽；用 import_module 取 sys.modules 里的真实模块再打补丁。
    dispatch_module = importlib.import_module("target.action_dispatcher.dispatch")
    monkeypatch.setattr(dispatch_module, "get_backend", lambda: _FakeBackend())
    receipt = dispatch_fn(
        action_id="gui.type_text", action_code="A030",
        args={"automation_id": "FixtureInput", "control_type": "Edit",
              "text": "hello"},
        target_ref={"snapshot_id": "OBS-0001", "target_id": "T0002",
                    "expected_process_name": "powershell.exe",
                    "fingerprint": "sha256:ffe580a61b04b931b67ea715e8c38af0e7b276f3bbe4e51177c4acb7dc8f4f96",
                    "selector_hint": {"automationId": "FixtureInput",
                                      "controlType": "Edit"}},
        request_id="R-test",
    )
    assert receipt.ok, receipt
    assert captured.get("automation_id") == "FixtureInput"
    assert captured.get("control_type") == "Edit"


def test_server_type_text_tool_passes_automation_identity(monkeypatch):
    import target.server as server

    captured = {}

    class _Backend:
        def type_text(self, control_id=None, text=None, class_name=None,
                      string="", automation_id=None, control_type=None):
            captured.update(control_id=control_id, text=text,
                            class_name=class_name, string=string,
                            automation_id=automation_id,
                            control_type=control_type)
            return {"ok": True}

    monkeypatch.setattr(server, "_backend", _Backend())
    server.type_text(automation_id="FixtureInput", control_type="Edit",
                     string="hello")
    assert captured["automation_id"] == "FixtureInput"
    assert captured["control_type"] == "Edit"


def test_probe_failure_keeps_original_code(tmp_path):
    # _probe_backend 内 refresh 的 BackendUnavailable 不得吞为 bool：
    # blocked 步骤必须携带 refresh 抛出的原始 code。
    from target.protocol_models import AtomicTestStep, TestCase

    class _Provider:
        def refresh(self):
            raise BackendUnavailable("window lock lost", code="window_lock_lost")

        def get_snapshot(self, snapshot_id):
            return None

    steps = tuple(
        AtomicTestStep(step_id=f"step-{i:04d}", action_id="gui.click",
                       args={}, required=True)
        for i in range(1, 4)
    )
    executor = AtomicExecutor(
        dispatch=lambda **kwargs: {"ok": True},
        observation_provider=_Provider(),
        confirmation_gate=None,
        risk_lookup=lambda action_id: ("low", ""),
    )
    result = executor.run_case(TestCase(case_id="case_probe", title="t",
                                        description="", steps=steps))
    assert len(result.step_results) == 3
    for sr in result.step_results:
        assert sr.status.value == "blocked"
        assert sr.error["code"] == "window_lock_lost"
        assert sr.error.get("message") == "window lock lost"


def test_windows_adapter_type_text_separates_locator_and_input():
    # 103 轮验收教训：真实适配层是 WindowsPywinautoBackend（target/automation/），
    # dispatch fake 的 **kwargs 掩盖了它的签名。本测试用真实适配类 +
    # fake 底层 _gui（具名签名），验证定位条件与输入值分离：
    # `_gui.type_text` 收到定位条件，`set_edit_text` 收到完整输入值，
    # 待写入值不兼作控件名称筛选。
    from target.automation.windows_pywinauto import WindowsPywinautoBackend

    backend = WindowsPywinautoBackend.__new__(WindowsPywinautoBackend)
    captured = {}

    class _FakeGUI:
        def type_text(self, control_id=None, text=None, class_name=None,
                      string="", automation_id=None, control_type=None):
            captured.update(control_id=control_id, text=text,
                            class_name=class_name, string=string,
                            automation_id=automation_id,
                            control_type=control_type)
            # 模拟底层契约：定位后 set_edit_text(string) 写入完整输入值
            return {"ok": True, "method": "set_edit_text", "text": string}

    backend._gui = _FakeGUI()

    # dispatch 统一语义：text=输入值 + automation 身份定位 → 定位 text 必须为空
    result = backend.type_text(text="hello", automation_id="FixtureInput",
                               control_type="Edit")
    assert result["ok"] is True, result
    assert captured["automation_id"] == "FixtureInput"
    assert captured["control_type"] == "Edit"
    assert captured["string"] == "hello"
    assert captured["text"] in (None, ""), "待写入值不得兼作控件名称筛选"

    # MCP legacy（server 以 kwargs 转 string/text）：text=定位、string=输入
    captured.clear()
    result = backend.type_text(text="QA input", string="hello")
    assert result["ok"] is True
    assert captured["text"] == "QA input"
    assert captured["string"] == "hello"

    # 旧 golden 无 automation 身份：保留既有 text 兜底定位行为
    captured.clear()
    result = backend.type_text(text="legacy-only")
    assert result["ok"] is True
    assert captured["text"] == "legacy-only"
    assert captured["string"] == "legacy-only"

    # 位置兼容（旧调用序）：text, control_id, class_name——有定位条件
    # （control_id/class_name）时，输入值不进定位（103 轮验收明令）
    captured.clear()
    backend.type_text("legacy", 3, "EDIT")
    assert captured["control_id"] == 3
    assert captured["class_name"] == "EDIT"
    assert captured["string"] == "legacy"
    assert captured["text"] is None


def test_macos_adapter_type_text_accepts_new_kwargs():
    # macOS 路径不被新增 kwargs 破坏：签名兼容、行为保持 v1 不支持错误。
    from target.automation.macos_accessibility import MacOSAccessibilityBackend

    backend = MacOSAccessibilityBackend.__new__(MacOSAccessibilityBackend)
    result = backend.type_text(text="hello", string="hello",
                               automation_id="FixtureInput",
                               control_type="Edit")
    assert result["ok"] is False
    assert "not supported" in result["error"]
