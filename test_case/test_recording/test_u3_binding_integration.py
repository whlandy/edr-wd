"""U3 回归：结果验证绑定与集成验收（录制易用性批次）。

对应设计 §3.4 与 U3 验收门：绑定准确 causalId/窗口；缺 verifier 的旧门槛
保持（不绑定的 scroll/drag 继续 incomplete）；面板 pendingVerifiers 与
编译缺口同源；面板绑定流（begin_pick(binding) → freeze → commit）端到端
产出可编译的 ready golden。
"""

import pytest

from agent.recording.compiler import compile_recording
from target.recording.models import CaptureScope
from target.recording.session import RecordingSession

from test_phase0_recording import (
    _DRAG_INPUT, _DRAG_TARGET, _bound_assertion, _event, _recording, _scroll,
)


def _drag(sequence, ms=None, causal_id=None):
    return _event(sequence, "drag_commit", target=_DRAG_TARGET,
                  input_=_DRAG_INPUT, ms=ms, causal_id=causal_id)


# ── 编译门槛：绑定 → ready；不绑定 → incomplete（保持） ────────────


def test_bound_scroll_drag_compile_ready():
    events = [
        _event(1, "pointer_click", ms=100, causal_id="cause-open"),
        _scroll(2, ms=500, causal_id="cause-scroll"),
        _bound_assertion(3, "cause-scroll", ms=600),     # 显式绑定 scroll 的结果验证
        _drag(4, ms=1500, causal_id="cause-drag"),
        _bound_assertion(5, "cause-drag", ms=1600),      # 显式绑定 drag 的结果验证
    ]
    result = compile_recording(_recording(*events))
    assert result.golden.status == "ready", [i.code for i in result.issues]


def test_unbound_scroll_drag_stays_incomplete():
    events = [
        _event(1, "pointer_click", ms=100, causal_id="cause-open"),
        _scroll(2, ms=500, causal_id="cause-scroll"),
        _drag(3, ms=1500, causal_id="cause-drag"),
    ]
    result = compile_recording(_recording(*events))
    assert result.golden.status == "incomplete"
    codes = {i.code for i in result.issues}
    assert {"compile_scroll_verifier_required", "compile_drag_verifier_required"} <= codes


# ── 面板提示数据与编译缺口同源 ─────────────────────────────────────


def test_panel_pending_verifiers_align_with_compiler_issues():
    session = RecordingSession("REC-" + "7" * 32, "flow",
                               CaptureScope("win", "windows_pywinauto", "EDRClient.exe", "EDRClient"))
    session.start()
    events = _recording(
        _event(1, "scroll_commit", ms=100, causal_id="C-s1"),
        _event(2, "drag_commit", target=_DRAG_TARGET, input_=_DRAG_INPUT,
               ms=200, causal_id="C-d1"),
    ).events
    for event in events:
        assert session.ingest(event) is True
    pending = session.pending_verifiers()
    assert [(p["sequence"], p["type"]) for p in pending] == \
        [(1, "scroll_commit"), (2, "drag_commit")]
    # 同一事件序列编译出的缺口与 pending 提示指向同两条
    result = compile_recording(session.recording())
    assert result.golden.status == "incomplete"


# ── 面板绑定流端到端：begin_pick(binding) → freeze → commit → ready ─


class _PickSource:
    def start(self): ...
    def stop(self): ...
    def pause(self): ...
    def resume(self): ...
    def status(self): return {}
    def heartbeat(self): return {}
    def begin_pick(self): ...
    def end_pick(self): ...

    def current_observed_target(self):
        from target.recording.models import ObservedTarget
        return ObservedTarget(snapshot_id="OBS-res", target_id="T9",
                              fingerprint="sha256:" + "a" * 64,
                              automation_id="rowCount", control_type="Text",
                              text="行数", value="3")


def test_panel_binding_flow_end_to_end_compiles_ready():
    session = RecordingSession("REC-" + "8" * 32, "flow",
                               CaptureScope("win", "windows_pywinauto", "EDRClient.exe", "EDRClient"))
    session.attach_source(_PickSource())
    session.start()
    # 面板提示的数据源：一个 click + 一个 scroll（无绑定）
    events = _recording(
        _event(1, "pointer_click", ms=100, causal_id="cause-open"),
        _scroll(2, ms=500, causal_id="cause-scroll"),
    ).events
    for event in events:
        assert session.ingest(event) is True
    scroll = events[-1]
    assert [p["causalId"] for p in session.pending_verifiers()] == ["cause-scroll"]

    # 面板“为该动作添加验证”：显式携带该动作的 sequence/causalId 进入拾取
    binding = (scroll.sequence, scroll.causal_id)
    session.begin_pick(binding=binding)
    session.update_pick_pointer((30, 40))
    frozen = session.freeze_pick()
    assert frozen["draft"]["binding"] == [scroll.sequence, scroll.causal_id]
    saved = session.commit_assertion_draft(frozen["draft"])
    assert saved["ok"] is True
    assert session.pending_verifiers() == []      # 绑定已收敛

    # 集成：带绑定的录制编译为 ready（缺 verifier 的旧门槛未被削弱——
    # unbound 场景在上一条测试保持 incomplete）
    recording = session.recording()
    result = compile_recording(recording)
    assert result.golden.status == "ready", [i.code for i in result.issues]
    bound = [e for e in recording.events if e.type == "assertion"]
    assert bound and bound[0].causal_id == "cause-scroll"   # 绑定准确 causalId
