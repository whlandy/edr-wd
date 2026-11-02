"""build_replay_runtime 装配失败后的目标/trace 残留（批次 12）。

装配顺序是 acquire_target_window（可能已锁窗）→ trace_store.open()（落盘
trace_started）→ begin_product_run（租约）→ 装配观测/执行器。旧代码中这条
链没有整体保护：

- legacy 路径（无 product-run 能力）：acquire 已锁窗后，若 begin_product_run
  缺失/失败或装配抛错，除租约 hook 什么都不释放——窗口锁滞留在后端
  （lock_window 内部 verify 失败时锁也已写入）；
- product-run 路径：begin_product_run 成功后装配失败有租约释放，但
  trace_store.open() 已写 trace_started，失败路径无人 finalize——孤儿 trace
  永远读作 "in progress"；
- trace_store.open() 自身在旧代码位于任何保护之外。

本回归锁定（对 runtime.py，编辑前 SHA 55ccb613…，Edit-only + SHA 纪律）：
1. legacy 装配失败必须尝试 unlock_window（否则锁滞留）；
2. legacy 装配失败必须把已打开的 trace finalize 为 trace_aborted；
3. product-run 路径 begin 失败仍拒绝且不调用 unlock_window（租约 hook 负责
   窗口锁，legacy unlock 对 product-run 会话是越权操作）；
4. product-run 租约获取失败时，已打开的 trace 同样要 finalize。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent.recording.models import GoldenTrace, RecordedStep, ReplaySelector
from agent.recording.runtime import build_replay_runtime
from target.recording.models import RecordingModelError

pytestmark = pytest.mark.unit


def _selector():
    return ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"},
        control={"automationId": "btnApply"},
    )


_ROOTED_STEP = RecordedStep(
    "step-0001", "gui.click", {},
    ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^logo1$",
                "rootAutomationId": "EdrMainWindow"},
        control={"automationId": "btnApply"},
    ),
)


def _golden():
    return GoldenTrace(
        name="policy flow",
        source_recording={"schema": "edr.desktop-recording/v1", "sha256": "sha256:raw"},
        catalog={"version": "1.0.0", "digest": "sha256:catalog"},
        environment={"backend": "windows_pywinauto", "target": "win-dev"},
        steps=(RecordedStep("step-0001", "gui.click", {}, _selector()),),
        status="ready",
    )


class _AssemblyFailureAgent:
    """connect/lock/verify 全绿；按脚本应答 begin_product_run/unlock_window。

    装配期炸点用 monkeypatch MCPObservationProvider 构造抛错注入（见各测试）。
    """

    def __init__(self, tools=(), begin_result=None):
        self.tools = tools
        self.begin_result = begin_result
        self.calls: list[tuple[str, dict | None]] = []

    def tools_list(self):
        return {"tools": [{"name": name} for name in self.tools]}

    def call_tool(self, name, args=None, timeout=None):
        self.calls.append((name, args))
        if name == "begin_product_run":
            return self.begin_result
        if name == "unlock_window":
            return {"ok": True}
        return {"ok": True}


def _last_trace_event(trace_dir):
    lines = [line for line in (trace_dir / "events.jsonl").read_text().splitlines() if line]
    assert lines, "trace 目录必须已有事件"
    event = json.loads(lines[-1])
    return event["event_type"]


def test_legacy_assembly_failure_unlocks_window(tmp_path):
    """begin_product_run 缺失的 legacy 目标：装配失败必须尝试 unlock_window。"""
    agent = _AssemblyFailureAgent(tools=())
    from agent.recording import runtime as runtime_mod

    original = runtime_mod.MCPObservationProvider

    def exploding_provider(*args, **kwargs):
        raise RuntimeError("provider assembly exploded")

    runtime_mod.MCPObservationProvider = exploding_provider
    try:
        with pytest.raises(RuntimeError, match="provider assembly exploded"):
            build_replay_runtime(agent, _golden(), trace_root=tmp_path)
    finally:
        runtime_mod.MCPObservationProvider = original

    unlock_calls = [c for c in agent.calls if c[0] == "unlock_window"]
    assert unlock_calls, "装配失败后必须尝试 unlock_window，否则 legacy 锁滞留"
    lock_calls = [c for c in agent.calls if c[0] == "lock_window"]
    assert lock_calls, "前置条件：acquire 确实锁了窗"


def test_legacy_assembly_failure_finalizes_trace(tmp_path):
    """legacy 装配失败后，已打开的 trace 必须以 trace_aborted 终结。"""
    agent = _AssemblyFailureAgent(tools=())
    from agent.recording import runtime as runtime_mod

    original = runtime_mod.MCPObservationProvider

    def exploding_provider(*args, **kwargs):
        raise RuntimeError("provider assembly exploded")

    runtime_mod.MCPObservationProvider = exploding_provider
    try:
        with pytest.raises(RuntimeError):
            build_replay_runtime(agent, _golden(), trace_root=tmp_path)
    finally:
        runtime_mod.MCPObservationProvider = original

    trace_dirs = [d for d in tmp_path.iterdir() if d.is_dir()]
    assert trace_dirs, "trace 已 open，目录应存在"
    assert _last_trace_event(trace_dirs[0]) == "trace_aborted", (
        "装配失败的孤儿 trace 不得永远读作 in progress"
    )


def test_product_run_begin_failure_refuses_and_keeps_lease_authority(tmp_path):
    """begin_product_run 拒绝时：显式拒绝，且不得越权调 legacy unlock。"""
    agent = _AssemblyFailureAgent(
        tools=("begin_product_run", "end_product_run", "observe_window_bundle",
               "focus_window_role", "focus_window_selector"),
        begin_result={"ok": False, "code": "product_run_conflict"},
    )
    agent.state = SimpleNamespace(backend_kind="windows_pywinauto")
    # 加 rootAutomationId 才走 product-run 入口（不 legacy acquire）
    golden = GoldenTrace(
        name="policy flow",
        source_recording={"schema": "edr.desktop-recording/v1", "sha256": "sha256:raw"},
        catalog={"version": "1.0.0", "digest": "sha256:catalog"},
        environment={"backend": "windows_pywinauto", "target": "win-dev",
                     "profile": "windows_hisec"},
        steps=(_ROOTED_STEP,),
        status="ready",
    )
    with pytest.raises(RecordingModelError) as excinfo:
        build_replay_runtime(agent, golden, profile="windows_hisec",
                             trace_root=tmp_path)
    assert excinfo.value.code == "product_run_conflict"
    assert not [c for c in agent.calls if c[0] == "unlock_window"], (
        "product-run 会话的窗口锁归租约 hook 管，legacy unlock 是越权操作"
    )


def test_product_run_begin_failure_finalizes_trace(tmp_path):
    """begin_product_run 拒绝时，先于它打开的 trace 必须被 finalize。"""
    agent = _AssemblyFailureAgent(
        tools=("begin_product_run", "end_product_run", "observe_window_bundle",
               "focus_window_role", "focus_window_selector"),
        begin_result={"ok": False, "code": "product_run_conflict"},
    )
    agent.state = SimpleNamespace(backend_kind="windows_pywinauto")
    golden = GoldenTrace(
        name="policy flow",
        source_recording={"schema": "edr.desktop-recording/v1", "sha256": "sha256:raw"},
        catalog={"version": "1.0.0", "digest": "sha256:catalog"},
        environment={"backend": "windows_pywinauto", "target": "win-dev",
                     "profile": "windows_hisec"},
        steps=(_ROOTED_STEP,),
        status="ready",
    )
    with pytest.raises(RecordingModelError):
        build_replay_runtime(agent, golden, profile="windows_hisec",
                             trace_root=tmp_path)

    trace_dirs = [d for d in tmp_path.iterdir() if d.is_dir()]
    assert trace_dirs
    assert _last_trace_event(trace_dirs[0]) == "trace_aborted"
