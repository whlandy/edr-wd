"""回放入口窗口受控恢复 —— 租约路径(增量 2,R0 第一阶段)。

entry_recovery=True 且目标具备 product-run 能力时,入口窗口改为在租约下通过
目标端 focus_entry_window 受控获取:只读枚举 -> 唯一匹配才 精确连接+严格锁+
激活,缺失/歧义/失败都是结构化处置,全程 RECOVERY_REQUESTED/RECOVERY_RESULT
审计事件落盘。默认关闭时走增量 1 前的历史顺序,逐字节不变。

本回归锁定:
1. 默认关闭:lease 目标 + legacy golden 仍先 acquire 后 begin,顺序不变;
2. 开启 + 目标 absent:focus_entry_window 被调,entry_window_absent 透传,
   且 trace 里有 RECOVERY_REQUESTED + RECOVERY_RESULT 审计事件;
3. 开启 + 歧义:拒绝且不误操作;
4. 工具返回非 dict:拒绝;
5. 非 lease 目标开启恢复:仍走增量 1 的 no-lease 路径,不调 focus_entry_window。
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


def _selector():
    return ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^logo1$"},
        control={"automationId": "btnApply"},
    )


def _golden(profile=None):
    environment = {"backend": "windows_pywinauto", "target": "win-dev"}
    if profile:
        environment["profile"] = profile
    return GoldenTrace(
        name="policy flow",
        source_recording={"schema": "edr.desktop-recording/v1", "sha256": "sha256:raw"},
        catalog={"version": "1.0.0", "digest": "sha256:catalog"},
        environment=environment,
        steps=(RecordedStep("step-0001", "gui.click", {}, _selector()),),
        status="ready",
    )


class _LeaseAgent:
    """product-run 全绿;focus_entry_window 按脚本应答并记录调用。"""

    def __init__(self, focus_result=None, tools=PRODUCT_TOOLS):
        self.focus_result = focus_result
        self.tools = tools
        self.calls: list[tuple[str, dict | None]] = []
        self.state = SimpleNamespace(backend_kind="windows_pywinauto")

    def tools_list(self):
        return {"tools": [{"name": name} for name in self.tools]}

    def call_tool(self, name, args=None, timeout=None):
        self.calls.append((name, args))
        if name == "focus_entry_window":
            if getattr(self, "focus_exception", None) is not None:
                raise self.focus_exception
            return self.focus_result
        return {"ok": True, "run_token": "tok-1"}


def _trace_events(tmp_path):
    trace_dirs = [d for d in tmp_path.iterdir() if d.is_dir()]
    assert trace_dirs, "lease 路径先开 trace,目录应存在"
    lines = [line for line in (trace_dirs[0] / "events.jsonl").read_text().splitlines() if line]
    return [json.loads(line) for line in lines]


def _build(agent, tmp_path, **kwargs):
    from agent.recording import runtime as runtime_mod
    original = runtime_mod.MCPObservationProvider

    class _Provider:
        def __init__(self, *args, **kwargs):
            pass

    runtime_mod.MCPObservationProvider = _Provider
    try:
        return build_replay_runtime(agent, _golden(profile="windows_hisec"),
                                    profile="windows_hisec",
                                    trace_root=tmp_path, **kwargs)
    finally:
        runtime_mod.MCPObservationProvider = original


def test_default_off_keeps_legacy_order(tmp_path):
    # 默认关闭:lease 目标 + legacy golden 仍是 acquire -> begin,无 focus_entry_window。
    agent = _LeaseAgent()
    _build(agent, tmp_path)
    names = [name for name, _ in agent.calls]
    assert "focus_entry_window" not in names
    assert "connect" in names, "默认路径仍走 legacy acquire"
    assert names.index("connect") < names.index("begin_product_run")


def test_entry_recovery_absent_audited_and_raises(tmp_path):
    agent = _LeaseAgent(focus_result={
        "ok": False, "code": "entry_window_absent",
        "disposition": "entry_window_absent",
        "error": "no window matches the recorded entry identity"})
    with pytest.raises(RecordingModelError) as excinfo:
        _build(agent, tmp_path, entry_recovery=True)
    assert excinfo.value.code == "entry_window_absent"
    names = [name for name, _ in agent.calls]
    assert "connect" not in names, "lease 路径不得裸调 legacy connect(会被策略拒)"
    assert names[0] == "begin_product_run", "先租约后入口获取"
    assert names[1] == "focus_entry_window"
    events = [e["event_type"] for e in _trace_events(tmp_path)]
    assert "recovery_requested" in events and "recovery_result" in events
    assert events.index("recovery_requested") < events.index("recovery_result")


def test_entry_recovery_ambiguous_refuses(tmp_path):
    agent = _LeaseAgent(focus_result={
        "ok": False, "code": "entry_window_ambiguous",
        "disposition": "entry_window_ambiguous",
        "error": "several windows match the recorded entry identity"})
    with pytest.raises(RecordingModelError) as excinfo:
        _build(agent, tmp_path, entry_recovery=True)
    assert excinfo.value.code == "entry_window_ambiguous"


def test_entry_recovery_invalid_response_refuses(tmp_path):
    agent = _LeaseAgent(focus_result=None)
    with pytest.raises(RecordingModelError) as excinfo:
        _build(agent, tmp_path, entry_recovery=True)
    assert excinfo.value.code == "replay_target_unavailable"


def test_entry_recovery_no_lease_target_uses_increment1_path(tmp_path):
    # 无 product-run 能力的目标:开启恢复也绝不调 focus_entry_window(没有租约授权)。
    agent = _LeaseAgent(tools=())
    _build(agent, tmp_path, entry_recovery=True)
    names = [name for name, _ in agent.calls]
    assert "focus_entry_window" not in names
    assert "connect" in names


def test_entry_recovery_success_acquires_and_continues(tmp_path):
    agent = _LeaseAgent(focus_result={
        "ok": True, "disposition": "acquired",
        "window": {"pid": 1984, "title": "logo1", "handle": 590820},
        "run_id": "run-1"})
    runtime = _build(agent, tmp_path, entry_recovery=True)
    assert runtime is not None
    names = [name for name, _ in agent.calls]
    assert names[0] == "begin_product_run"
    assert "focus_entry_window" in names
    assert "connect" not in names
    events = [e["event_type"] for e in _trace_events(tmp_path)]
    assert events[-1] != "trace_aborted", "成功路径不得把 trace 终结为 aborted"


# ---- 异常与审计(R0 验收:异常与审计) ----

def test_transport_exception_audits_unknown_and_propagates_untyped(tmp_path):
    # 传输异常 = 结果未知(目标可能已动作),不是"未执行":
    # 审计 RECOVERY_RESULT=result=unknown,原异常不带型上抛。
    agent = _LeaseAgent()
    agent.focus_exception = ConnectionError("tunnel down mid-recovery")
    with pytest.raises(ConnectionError, match="tunnel down mid-recovery"):
        _build(agent, tmp_path, entry_recovery=True)
    results = [json.loads(e["payload"]) if isinstance(e.get("payload"), str)
               else e.get("payload") or {} for e in _trace_events(tmp_path)
               if e["event_type"] == "recovery_result"]
    assert results and results[-1].get("result") == "unknown", (
        "传输异常必须审计为 unknown;结果未知绝不写成未执行/absent")


def test_result_audit_failure_does_not_mask_acquisition_error(tmp_path):
    # 处置审计写失败不得掩盖获取本身的真实错误码(单独报告,不覆盖)。
    agent = _LeaseAgent(focus_result={
        "ok": False, "code": "entry_window_absent",
        "disposition": "entry_window_absent", "error": "no window"})
    from agent.recording import runtime as runtime_mod

    original_audit = runtime_mod._recovery_audit
    calls = {"n": 0}

    def flaky_audit(store, event_type, payload):
        calls["n"] += 1
        if event_type == runtime_mod.EventType.RECOVERY_RESULT:
            raise OSError("disk full")
        return original_audit(store, event_type, payload)

    runtime_mod._recovery_audit = flaky_audit
    try:
        with pytest.raises(RecordingModelError) as excinfo:
            _build(agent, tmp_path, entry_recovery=True)
    finally:
        runtime_mod._recovery_audit = original_audit
    assert excinfo.value.code == "entry_window_absent", (
        "审计失败必须单独报告;真实获取错误码不得被 audit 错误覆盖")


def test_request_audit_failure_stops_before_side_effect(tmp_path):
    # 请求审计写失败 = 尚无任何副作用:stop here,不调 focus_entry_window。
    agent = _LeaseAgent(focus_result={"ok": True, "disposition": "acquired"})
    from agent.recording import runtime as runtime_mod

    def exploding_audit(store, event_type, payload):
        raise OSError("disk full")

    original = runtime_mod._recovery_audit
    runtime_mod._recovery_audit = exploding_audit
    try:
        with pytest.raises(RecordingModelError) as excinfo:
            _build(agent, tmp_path, entry_recovery=True)
    finally:
        runtime_mod._recovery_audit = original
    assert excinfo.value.code == "recovery_audit_write_failed"
    assert not [c for c in agent.calls if c[0] == "focus_entry_window"], (
        "请求审计失败时必须已停止;不得发生任何目标副作用")
