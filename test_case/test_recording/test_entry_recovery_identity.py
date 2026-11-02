"""入口恢复的触发与身份边界(R0 验收:触发与身份)。

恢复只服务 legacy golden(无 rootAutomationId)且默认关闭。本回归锁定:
1. rooted golden(带 rootAutomationId)永远不走恢复路径 —— 它有自己的
   focus_window_selector 机制,恢复不得劫持;
2. 标题不匹配的窗口绝不入选(focus_entry_window 枚举按完整身份过滤,
   模拟"标题已变"场景:唯一存在的窗口标题不符 -> entry_window_absent,
   绝不锁到错误的窗口上);
3. entry_recovery=True 但 golden 无 profile:恢复路径仍按工具能力判定,
   不因 profile 缺失误触发或误拒绝(身份判定以 scope 为准,不是自报)。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent.recording.models import GoldenTrace, RecordedStep, ReplaySelector
from agent.recording.runtime import build_replay_runtime
from target.recording.models import RecordingModelError

pytestmark = pytest.mark.unit

PRODUCT_TOOLS = ("begin_product_run", "end_product_run", "observe_window_bundle",
                 "focus_window_role", "focus_window_selector", "focus_entry_window")


def _rooted_golden():
    return GoldenTrace(
        name="rooted flow",
        source_recording={"schema": "edr.desktop-recording/v1", "sha256": "sha256:raw"},
        catalog={"version": "1.0.0", "digest": "sha256:catalog"},
        environment={"backend": "windows_pywinauto", "target": "win-dev",
                     "profile": "windows_hisec"},
        steps=(RecordedStep(
            "step-0001", "gui.click", {},
            ReplaySelector(
                window={"processName": "EDRClient.exe", "titleRegex": "^logo1$",
                        "rootAutomationId": "EdrMainWindow"},
                control={"automationId": "btnApply"}),
        ),),
        status="ready",
    )


class _Agent:
    def __init__(self, focus_result=None):
        self.focus_result = focus_result
        self.calls: list[tuple[str, dict | None]] = []
        self.state = SimpleNamespace(backend_kind="windows_pywinauto")

    def tools_list(self):
        return {"tools": [{"name": n} for n in PRODUCT_TOOLS]}

    def call_tool(self, name, args=None, timeout=None):
        self.calls.append((name, args))
        if name == "focus_entry_window":
            return self.focus_result
        return {"ok": True, "run_token": "tok-1"}


def _build(agent, golden, tmp_path):
    from agent.recording import runtime as runtime_mod

    class _Provider:
        def __init__(self, *args, **kwargs):
            pass

    original = runtime_mod.MCPObservationProvider
    runtime_mod.MCPObservationProvider = _Provider
    try:
        return build_replay_runtime(agent, golden, profile="windows_hisec",
                                    trace_root=tmp_path, entry_recovery=True)
    finally:
        runtime_mod.MCPObservationProvider = original


def test_rooted_golden_never_enters_recovery_path(tmp_path):
    # rootAutomationId 在手:legacy_lock=False -> 恢复路径整体不参与;
    # rooted 的 focus_window_selector 由回放执行期的 window_focus 机制逐步调用
    # (test_window_session.py 覆盖),装配期只需证明恢复没被触发。
    agent = _Agent(focus_result={"ok": True, "disposition": "acquired"})
    _build(agent, _rooted_golden(), tmp_path)
    names = [name for name, _ in agent.calls]
    assert "focus_entry_window" not in names, "rooted golden 不得被恢复路径劫持"
    assert "connect" not in names, "rooted 路径装配期不裸调 legacy connect"
    assert names[0] == "begin_product_run", "仍先拿租约(rooted 回放需要它)"


def test_title_drift_window_is_not_selected(monkeypatch, tmp_path):
    # 目标端身份过滤:标题已漂移的唯一窗口 -> absent,绝不锁错窗口。
    from target import product_policy, server
    from target.product_profiles import get_capability

    product_policy.reset_for_tests()
    capability = get_capability("windows_hisec", "windows_pywinauto")
    run = product_policy.begin_product_run(capability.profile_id, capability.digest)
    try:
        enumerated = []

        class Backend:
            def is_window_open(self, title_re=None, process_name=None, class_name=None):
                enumerated.append({"title_re": title_re, "process_name": process_name})
                import re
                # 目标现场:唯一窗口标题已从 logo1 漂移为 logo2
                windows = [{"handle": 7, "process_id": 9, "title": "logo2"}]
                pattern = re.compile(title_re) if title_re else None
                matched = [w for w in windows
                           if pattern is None or pattern.search(w["title"])]
                return {"ok": True, "windows": matched}

            def activate_edr(self, *args, **kwargs):
                raise AssertionError("absent 处置绝不触碰 activate_edr")

        monkeypatch.setattr(server, "_backend_kind", "windows_pywinauto")
        monkeypatch.setattr(server, "_backend", Backend())
        result = json.loads(server.focus_entry_window(
            "EDRClient.exe", "^logo1$", run.run_token))
        assert result["code"] == "entry_window_absent", (
            "标题漂移的窗口必须按身份过滤排除,绝不误锁")
        assert enumerated[0]["process_name"] == "EDRClient.exe"
        assert enumerated[0]["title_re"] == "^logo1$"
    finally:
        product_policy.reset_for_tests()


def test_recovery_identity_uses_scope_not_profile_self_report(tmp_path):
    # golden 自报 profile 与执行 profile 一致性由 golden_profile_mismatch 门把守;
    # 恢复触发的身份依据是 scope(进程+标题),不是 profile 自报。
    golden = GoldenTrace(
        name="policy flow",
        source_recording={"schema": "edr.desktop-recording/v1", "sha256": "sha256:raw"},
        catalog={"version": "1.0.0", "digest": "sha256:catalog"},
        environment={"backend": "windows_pywinauto", "target": "win-dev",
                     "profile": "windows_hisec"},
        steps=(RecordedStep(
            "step-0001", "gui.click", {},
            ReplaySelector(window={"processName": "Notepad.exe",
                                   "titleRegex": "^Untitled$"},
                           control={"automationId": "btnApply"}),
        ),),
        status="ready",
    )
    agent = _Agent(focus_result={"ok": True, "disposition": "acquired"})
    _build(agent, golden, tmp_path)
    focus_calls = [args for name, args in agent.calls if name == "focus_entry_window"]
    assert focus_calls, "非 EDR 进程的 legacy golden 同样可用恢复(身份按 scope)"
    assert focus_calls[0]["process_name"] == "Notepad.exe"
    assert focus_calls[0]["title_regex"] == "^Untitled$", (
        "恢复请求的身份必须来自 golden scope 原文,不得被 profile 改写")
