"""入口恢复的租约并发语义(R0 验收:并发)。

租约是 GUI 协作并发栅栏:其他 product-run 持租约时,本 agent 的恢复必须
整体不发生 —— begin_product_run 被拒后,不得再调 focus_entry_window,也不得
越权 unlock 别人的窗口锁;目标端 focus_entry_window 对无效/过期 token 拒绝
且零方法调用(副作用面文件已锁定零调用,这里锁 agent 侧编排顺序)。
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


def _golden():
    return GoldenTrace(
        name="policy flow",
        source_recording={"schema": "edr.desktop-recording/v1", "sha256": "sha256:raw"},
        catalog={"version": "1.0.0", "digest": "sha256:catalog"},
        environment={"backend": "windows_pywinauto", "target": "win-dev",
                     "profile": "windows_hisec"},
        steps=(RecordedStep(
            "step-0001", "gui.click", {},
            ReplaySelector(window={"processName": "EDRClient.exe",
                                   "titleRegex": "^logo1$"},
                           control={"automationId": "btnApply"}),
        ),),
        status="ready",
    )


class _Agent:
    def __init__(self, begin_result=None):
        self.begin_result = begin_result
        self.calls: list[tuple[str, dict | None]] = []
        self.state = SimpleNamespace(backend_kind="windows_pywinauto")

    def tools_list(self):
        return {"tools": [{"name": n} for n in PRODUCT_TOOLS]}

    def call_tool(self, name, args=None, timeout=None):
        self.calls.append((name, args))
        if name == "begin_product_run":
            return self.begin_result
        return {"ok": True}


def _build(agent, tmp_path):
    from agent.recording import runtime as runtime_mod

    class _Provider:
        def __init__(self, *args, **kwargs):
            pass

    original = runtime_mod.MCPObservationProvider
    runtime_mod.MCPObservationProvider = _Provider
    try:
        return build_replay_runtime(agent, _golden(), profile="windows_hisec",
                                    trace_root=tmp_path, entry_recovery=True)
    finally:
        runtime_mod.MCPObservationProvider = original


def test_lease_conflict_stops_recovery_before_focus(tmp_path):
    # 其他 product-run 持租约:begin 被拒 -> 恢复整体不发生,无 focus_entry_window。
    agent = _Agent(begin_result={"ok": False, "code": "product_run_conflict"})
    with pytest.raises(RecordingModelError) as excinfo:
        _build(agent, tmp_path)
    assert excinfo.value.code == "product_run_conflict"
    names = [name for name, _ in agent.calls]
    assert "focus_entry_window" not in names, "租约被拒后不得尝试恢复"
    assert "unlock_window" not in names, "不得越权 unlock 别人的窗口锁"


def test_begin_transport_exception_propagates_and_skips_recovery(tmp_path):
    # begin 阶段隧道断开:原样上抛,恢复不发生,trace 以 aborted 终结不悬挂。
    agent = _Agent()
    agent.begin_exception = ConnectionError("tunnel down before lease")

    def call(name, args=None, timeout=None):
        agent.calls.append((name, args))
        if name == "begin_product_run":
            raise agent.begin_exception
        return {"ok": True}

    agent.call_tool = call
    with pytest.raises(ConnectionError, match="tunnel down before lease"):
        _build(agent, tmp_path)
    assert "focus_entry_window" not in [n for n, _ in agent.calls]
    lines = [line for d in tmp_path.iterdir() if d.is_dir()
             for line in (d / "events.jsonl").read_text().splitlines() if line]
    assert lines and json.loads(lines[-1])["event_type"] == "trace_aborted", (
        "失败路径的 trace 不得悬挂为 in progress")


def test_expired_token_at_focus_is_audited_and_refused(tmp_path):
    # 租约在 begin 与 focus 之间过期(目标端 product_run_expired):
    # 错误码透传,处置审计为 expired 而非 acquired。
    agent = _Agent(begin_result={"ok": True, "run_token": "tok-expired"})
    agent.focus_result = {"ok": False, "code": "product_run_expired"}

    def call(name, args=None, timeout=None):
        agent.calls.append((name, args))
        if name == "focus_entry_window":
            return agent.focus_result
        return {"ok": True, "run_token": "tok-expired"}

    agent.call_tool = call
    with pytest.raises(RecordingModelError) as excinfo:
        _build(agent, tmp_path)
    assert excinfo.value.code == "product_run_expired"
    events = [json.loads(line)
              for d in tmp_path.iterdir() if d.is_dir()
              for line in (d / "events.jsonl").read_text().splitlines() if line]
    results = [e.get("payload") for e in events if e["event_type"] == "recovery_result"]
    assert results and results[-1].get("result") == "product_run_expired", (
        "过期处置必须如实审计,不得写成 acquired")
