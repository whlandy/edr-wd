"""入口窗口身份记录(第二阶段收敛范围,设计 08 R2)。

收敛后的契约:从首个实际入口 selector 及其可靠关联的录制证据生成**一条可选
身份记录**;证据不足/矛盾则显式不可恢复(不编造、不降级);目标端在租约下
对声明字段做实时唯一匹配(root 声明必须可验证,绝不退回仅标题匹配)。

锁定:
1. v1 双读:v1 golden 读入 = entryWindow=None,行为与 v1 逐语义一致;
   v1 读取方(只认 v1 schema)遇 v2 文件 -> unsupported_schema 显式拒绝;
   v2 无 entryWindow 字段 = 无清单语义回退。
2. 编译期:manifest 空(v1 旧录制)-> 无记录无诊断(兼容矩阵);
   manifest 有条目但无 already_open -> entry_identity_no_manifest_match;
   selector 根与 manifest 根矛盾 -> entry_identity_conflict;
   一致 -> v2 + 完整身份记录。
3. 目标端:声明 root 但后端无 root 枚举能力 -> entry_root_unverifiable
   (绝不降级);root 过滤后唯一 -> acquired;枚举与获取之间窗口被替换 ->
   entry_window_stale 且不落锁。
4. agent 端:v2 身份经 focus_entry_window 传递;处置审计含 declared 标记。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from agent.recording.models import (GOLDEN_TRACE_SCHEMA, GOLDEN_TRACE_SCHEMA_V2,
                                    GoldenTrace, EntryWindowIdentity,
                                    RecordedStep, ReplaySelector)
from target.recording.models import RecordingModelError

pytestmark = pytest.mark.unit


def _golden_dict(schema=GOLDEN_TRACE_SCHEMA, **extra):
    data = {
        "schema": schema, "status": "ready", "name": "policy flow",
        "sourceRecording": {"schema": "edr.desktop-recording/v1", "sha256": "sha256:raw"},
        "catalog": {"version": "1.0.0", "digest": "sha256:catalog"},
        "environment": {"backend": "windows_pywinauto", "target": "win-dev"},
        "entry": "step-0001",
        "steps": {"step-0001": {
            "stepId": "step-0001", "actionId": "gui.click", "args": {},
            "selector": {"window": {"processName": "EDRClient.exe",
                                    "titleRegex": "^logo1$"},
                         "control": {"automationId": "btnApply"}, "visual": None},
            "endSelector": None, "verifiers": [], "required": True,
            "status": "ready", "issues": [],
        }},
        "cleanup": [],
    }
    data.update(extra)
    return data


# ---- 双读 ----

def test_v1_golden_reads_without_entry_window():
    g = GoldenTrace.from_dict(_golden_dict())
    assert g.entry_window is None
    assert g.schema == GOLDEN_TRACE_SCHEMA


def test_v2_golden_without_entry_window_is_valid_fallback():
    g = GoldenTrace.from_dict(_golden_dict(schema=GOLDEN_TRACE_SCHEMA_V2))
    assert g.entry_window is None, "v2 无清单 = 回退语义,不是错误"


def test_v1_reader_rejects_v2_file_explicitly():
    # 现有严格解析先拒未知字段(_strict),再查 schema;
    # 错误码是现有解析器的 unsupported_schema —— 锁定显式拒绝,不承诺错误码路径。
    # v1 schema + entryWindow field: the explicit guard refuses it rather
    # than silently ignoring the identity record.
    with pytest.raises(RecordingModelError) as excinfo:
        GoldenTrace.from_dict(_golden_dict(
            schema=GOLDEN_TRACE_SCHEMA,
            entryWindow={"schema": "edr.entry-window-identity/v1",
                         "processName": "EDRClient.exe", "origin": "already_open",
                         "titleRegex": "^logo1$"}))
    assert excinfo.value.code == "type_error"


def test_entry_window_requires_title_or_root():
    with pytest.raises(RecordingModelError) as excinfo:
        EntryWindowIdentity.from_dict({
            "schema": "edr.entry-window-identity/v1",
            "processName": "EDRClient.exe", "origin": "already_open"})
    assert excinfo.value.code == "entry_identity_unidentifiable"


def test_entry_window_rejects_handle_leakage():
    # 禁令:HWND 不进 golden —— 未在允许字段集,严格解析拒绝。
    with pytest.raises(RecordingModelError):
        EntryWindowIdentity.from_dict({
            "schema": "edr.entry-window-identity/v1",
            "processName": "EDRClient.exe", "origin": "already_open",
            "titleRegex": "^logo1$", "handle": 590820})


# ---- 编译期身份生成 ----

def _recording(windows):
    from target.recording.models import RawRecording
    return RawRecording.from_dict({
        "schema": "edr.desktop-recording/v1", "name": "flow", "sessionId": "REC-1",
        "events": [], "controlSnapshots": [],
        "captureDiagnostics": {"droppedPackets": 0, "correlationErrorCount": 0,
                                "recorderUiEvents": 0, "unidentifiedTargetEvents": 0,
                                "outOfScopeEvents": 0},
        "rejections": [], "windows": windows,
    })


def _source_event(handle=None):
    """RawCaptureEvent whose evidence names a recorded window handle."""
    from target.recording.models import RawCaptureEvent, CaptureScope
    evidence = {}
    if handle is not None:
        evidence["window"] = {"handle": handle, "title": "logo1"}
    return RawCaptureEvent(
        sequence=1, wall_time="2026-11-02T00:00:00Z", monotonic_ms=1,
        type="pointer_click",
        scope=CaptureScope("target", "windows_pywinauto", "EDRClient.exe", "^logo1$"),
        input={"button": "left"},
        observed_target=None, evidence=evidence,
    )


def _first_step(process="EDRClient.exe", root=None, title="^logo1$"):
    window = {"processName": process}
    if root:
        window["rootAutomationId"] = root
    if title:
        window["titleRegex"] = title
    return RecordedStep(
        "step-0001", "gui.click", {},
        ReplaySelector(window=window, control={"automationId": "btnApply"}),
    )


def test_v1_era_recording_yields_no_record_and_no_issue():
    from agent.recording.compiler import _entry_window_identity
    identity, issues = _entry_window_identity(_first_step(), [], _recording([]))
    assert identity is None and issues == [], "v1 旧录制:兼容矩阵,无诊断"


def test_manifest_without_already_open_refuses_with_issue():
    from agent.recording.compiler import _entry_window_identity
    windows = [{"processName": "EDRClient.exe", "title": "logo1", "handle": 1,
                "pid": 9, "origin": "opened", "closed": False}]
    identity, issues = _entry_window_identity(
        _first_step(), _source_event(handle=1), _recording(windows))
    assert identity is None
    assert [i.code for i in issues] == ["entry_identity_no_manifest_match"]


def test_contradictory_root_refuses_with_issue():
    from agent.recording.compiler import _entry_window_identity
    windows = [{"processName": "EDRClient.exe", "title": "logo1", "handle": 1,
                "pid": 9, "origin": "already_open", "closed": False}]
    recording = _recording(windows)
    recording = recording.__class__.from_dict({**recording.to_dict(), "controlSnapshots": [
        {"snapshotId": "S1", "capturedAtMs": 0, "reason": "pre_action",
         "window": {"handle": 1, "processName": "EDRClient.exe",
                    "rootAutomationId": "SafraRoot", "title": "logo1"},
         "contentHash": "sha256:x", "stateHash": "sha256:y", "truncated": False,
         "complete": True, "controlCount": 1, "controls": []}]})
    identity, issues = _entry_window_identity(
        _first_step(root="EdrMainWindow"), _source_event(handle=1), recording)
    assert identity is None
    assert [i.code for i in issues] == ["entry_identity_conflict"], (
        "selector 根与 manifest 根矛盾:不编造任何一边")


def test_consistent_identity_emitted():
    from agent.recording.compiler import _entry_window_identity
    windows = [{"processName": "EDRClient.exe", "title": "logo1", "handle": 1,
                "pid": 9, "origin": "already_open", "closed": False}]
    recording = _recording(windows)
    recording = recording.__class__.from_dict({**recording.to_dict(), "controlSnapshots": [
        {"snapshotId": "S1", "capturedAtMs": 0, "reason": "pre_action",
         "window": {"handle": 1, "processName": "EDRClient.exe",
                    "rootAutomationId": "EdrMainWindow", "title": "logo1"},
         "contentHash": "sha256:x", "stateHash": "sha256:y", "truncated": False,
         "complete": True, "controlCount": 1, "controls": []}]})
    identity, issues = _entry_window_identity(
        _first_step(root="EdrMainWindow"), _source_event(handle=1), recording)
    assert issues == []
    assert identity is not None
    assert identity.process_name == "EDRClient.exe"
    assert identity.root_automation_id == "EdrMainWindow"
    assert identity.origin == "already_open"


# ---- 目标端 root 验证 ----

class _RootBackend:
    def __init__(self, windows, roots):
        self.windows = windows
        self.roots = roots
        self.locked = False
        self.calls = []

    def is_window_open(self, title_re=None, process_name=None, class_name=None):
        self.calls.append("is_window_open")
        return {"ok": True, "windows": self.windows}

    def _enumerate_top_windows_windows(self):
        self.calls.append("_enumerate_top_windows_windows")
        return [{"handle": h, "root_control_id": r} for h, r in self.roots.items()]

    def connect_window_identity(self, handle, pid, timeout=10.0):
        self.calls.append("connect_window_identity")
        return {"ok": True}

    def lock_window(self, **kwargs):
        self.calls.append("lock_window")
        self.locked = True
        return {"ok": True, "lock": {"handle": 5, "pid": 9, "snapshot": {}}}

    def unlock_window(self):
        self.calls.append("unlock_window")
        self.locked = False
        return {"ok": True}

    def get_window_lock(self):
        return {"ok": True, "locked": self.locked, "lock": {}}


@pytest.fixture()
def lease():
    from target import product_policy
    from target.product_profiles import get_capability
    product_policy.reset_for_tests()
    capability = get_capability("windows_hisec", "windows_pywinauto")
    run = product_policy.begin_product_run(capability.profile_id, capability.digest)
    yield run
    product_policy.reset_for_tests()


def _run_focus(monkeypatch, backend, token, root=None):
    from target import server
    monkeypatch.setattr(server, "_backend_kind", "windows_pywinauto")
    monkeypatch.setattr(server, "_backend", backend)
    monkeypatch.setattr(server, "_window_session", None)
    args = {"process_name": "EDRClient.exe", "title_regex": "^logo1$",
            "run_token": token}
    if root is not None:
        args["root_automation_id"] = root
    return json.loads(server.focus_entry_window(**args))


def test_declared_root_without_capability_refuses_not_downgrades(monkeypatch, lease):
    class NoEnumerateBackend:
        def is_window_open(self, *a, **k):
            return {"ok": True, "windows": [{"handle": 5, "process_id": 9, "title": "logo1"}]}

        def connect_window_identity(self, *a, **k):
            return {"ok": True}

        def lock_window(self, **k):
            raise AssertionError("root 不可验证时绝不能走到锁定")

    monkeypatch.setattr(__import__("target.server", fromlist=["server"]),
                        "_backend_kind", "windows_pywinauto")
    monkeypatch.setattr(__import__("target.server", fromlist=["server"]),
                        "_backend", NoEnumerateBackend())
    monkeypatch.setattr(__import__("target.server", fromlist=["server"]),
                        "_window_session", None)
    result = json.loads(__import__("target.server", fromlist=["server"])
                        .focus_entry_window(
                            process_name="EDRClient.exe", title_regex="^logo1$",
                            run_token=lease.run_token,
                            root_automation_id="EdrMainWindow"))
    assert result["code"] == "entry_root_unverifiable", "声明 root 不可验证 -> 拒绝,不降级为标题匹配"


def test_root_filter_unique_acquires(monkeypatch, lease):
    from target import server
    backend = _RootBackend(
        windows=[{"handle": 5, "process_id": 9, "title": "logo1"},
                 {"handle": 6, "process_id": 9, "title": "logo1"}],
        roots={5: "EdrMainWindow", 6: "OtherRoot"},
    )
    result = _run_focus(monkeypatch, backend, lease.run_token, root="EdrMainWindow")
    assert result["ok"] is True and result["disposition"] == "acquired"
    assert result["window"]["handle"] == 5, "root 过滤把两个同标题窗口唯一化"


def test_root_only_identity_acquires_without_title(monkeypatch, lease):
    """Compiler-emitted rooted identities normally have no titleRegex."""
    from target import server
    backend = _RootBackend(
        windows=[{"handle": 5, "process_id": 9, "title": "renamed-live"}],
        roots={5: "EdrMainWindow"},
    )
    monkeypatch.setattr(server, "_backend_kind", "windows_pywinauto")
    monkeypatch.setattr(server, "_backend", backend)
    monkeypatch.setattr(server, "_window_session", None)
    result = json.loads(server.focus_entry_window(
        process_name="EDRClient.exe", run_token=lease.run_token,
        root_automation_id="EdrMainWindow"))
    assert result["ok"] is True and result["window"]["handle"] == 5


def test_replacement_between_enumerate_and_acquire_refuses_stale(monkeypatch, lease):
    from target import server
    backend = _RootBackend(
        windows=[{"handle": 5, "process_id": 9, "title": "logo1"}],
        roots={5: "EdrMainWindow"},
    )
    state = {"enumerations": 0}

    def flipping_enumerate():
        state["enumerations"] += 1
        # 第一次枚举(过滤)root 匹配;第二次(获取前重验)root 已变 = 窗口被替换
        root = "EdrMainWindow" if state["enumerations"] == 1 else "ReplacedRoot"
        return [{"handle": 5, "root_control_id": root}]

    backend._enumerate_top_windows_windows = flipping_enumerate
    result = _run_focus(monkeypatch, backend, lease.run_token, root="EdrMainWindow")
    assert result["code"] == "entry_window_stale"
    assert "lock_window" not in backend.calls, "stale 处置绝不落锁"


# ---- agent 端传递与审计 ----

PRODUCT_TOOLS = ("begin_product_run", "end_product_run", "observe_window_bundle",
                 "focus_window_role", "focus_window_selector", "focus_entry_window")


def test_agent_passes_declared_root_and_audits(tmp_path):
    """v2 golden(title-only 身份,首步无 root)-> lease 路径携带 root=None 声明。

    lease_entry_path 只服务 legacy_lock golden(首步无 rootAutomationId);
    v2 的 title-only 身份记录同样适用(EntryWindowIdentity 允许 root=None)。
    """
    from agent.recording.runtime import build_replay_runtime

    golden = GoldenTrace.from_dict(_golden_dict(
        schema=GOLDEN_TRACE_SCHEMA_V2,
        entryWindow={"schema": "edr.entry-window-identity/v1",
                     "processName": "EDRClient.exe", "origin": "already_open",
                     "titleRegex": "^logo1$"}))
    # golden_entry_scope 取首个 selector:首步窗口需无 root 才走 legacy 路径。
    step = golden.steps[0]
    window = {"processName": "EDRClient.exe", "titleRegex": "^logo1$"}
    object.__setattr__(golden.steps[0], "selector",
                       ReplaySelector(window=window, control={"automationId": "btnApply"}, visual=None))
    del step

    class _Agent:
        def __init__(self):
            self.calls = []
            self.state = SimpleNamespace(backend_kind="windows_pywinauto")

        def tools_list(self):
            return {"tools": [{"name": n} for n in PRODUCT_TOOLS]}

        def call_tool(self, name, args=None, timeout=None):
            self.calls.append((name, args))
            if name == "focus_entry_window":
                return {"ok": True, "disposition": "acquired",
                        "window": {"pid": 9, "title": "logo1", "handle": 1},
                        "run_id": "run-1"}
            return {"ok": True, "run_token": "tok-1"}

    agent = _Agent()

    class _Provider:
        def __init__(self, *args, **kwargs):
            pass

    from agent.recording import runtime as runtime_mod
    original = runtime_mod.MCPObservationProvider
    runtime_mod.MCPObservationProvider = _Provider
    try:
        build_replay_runtime(agent, golden, profile="windows_hisec",
                             trace_root=tmp_path, entry_recovery=True)
    finally:
        runtime_mod.MCPObservationProvider = original

    focus_calls = [args for name, args in agent.calls if name == "focus_entry_window"]
    assert focus_calls, "lease 路径必须调用 focus_entry_window"
    assert "root_automation_id" not in focus_calls[0] or focus_calls[0]["root_automation_id"] is None, (
        "title-only 身份:root 参数缺省,目标端按进程+标题唯一匹配")
    events = [json.loads(line)
              for d in tmp_path.iterdir() if d.is_dir()
              for line in (d / "events.jsonl").read_text().splitlines() if line]
    requested = [e.get("payload") for e in events if e["event_type"] == "recovery_requested"]
    assert requested and requested[0]["scope"]["declared_identity"] is True, (
        "审计记录声明身份参与获取")


# ---- 评审反例(P1-1 / P1-2 / P1-3 / P2) ----

def test_sibling_window_cannot_vouch_for_entry(monkeypatch):
    """P1-1 反例:同进程已开窗口 B + 流程中新开窗口 A。

    A 的 handle 在 manifest 里 origin=opened;B 虽 already_open 但 handle 不同。
    handle 关联必须让 A 拿不到 B 的背书。"""
    from agent.recording.compiler import _entry_window_identity
    windows = [
        {"processName": "EDRClient.exe", "title": "B", "handle": 77,
         "pid": 9, "origin": "already_open", "closed": False},
        {"processName": "EDRClient.exe", "title": "A", "handle": 42,
         "pid": 9, "origin": "opened", "closed": False},
    ]
    identity, issues = _entry_window_identity(
        _first_step(title="^A$"), _source_event(handle=42), _recording(windows))
    assert identity is None, "新开窗口 A 绝不能借 B 的 already_open 背书"
    assert [i.code for i in issues] == ["entry_identity_no_manifest_match"]


def test_title_only_entry_cannot_borrow_sibling_manifest(monkeypatch):
    """P1-1 反例:title-only 的 A 借同进程 B 的 manifest 记录通过。"""
    from agent.recording.compiler import _entry_window_identity
    windows = [
        {"processName": "EDRClient.exe", "title": "B", "handle": 77,
         "pid": 9, "origin": "already_open", "closed": False},
    ]
    identity, issues = _entry_window_identity(
        _first_step(title="^A$"), _source_event(handle=99), _recording(windows))
    assert identity is None, "handle 99 无 manifest 条目:title-only 不得借进程背书"
    assert [i.code for i in issues] == ["entry_identity_no_manifest_match"]


def test_entry_evidence_conflict_closes_ready_gate():
    """P1-2:entry 证据矛盾必须让 golden incomplete —— 回放门槛才真正拒绝。"""
    from agent.recording.compiler import compile_recording
    from target.recording.models import RawRecording
    payload = _recording([{"processName": "EDRClient.exe", "title": "logo1",
                           "handle": 1, "pid": 9, "origin": "opened",
                           "closed": False}]).to_dict()
    payload["events"] = [{
        "sequence": 1, "type": "pointer_click",
        "wallTime": "2026-11-02T00:00:00Z", "monotonicMs": 1,
        "causalId": "CAUSE-1",
        "scope": {"backend": "windows_pywinauto",
                  "processName": "EDRClient.exe", "target": "target",
                  "windowTitle": "^logo1$"},
        "input": {"button": "left"},
        "observedTarget": {"automationId": "btnApply", "controlType": "Button",
                           "identifier": "id-x", "fingerprint": "sha256:abc",
                           "text": "Apply", "rect": [0, 0, 10, 10],
                           "ancestry": [], "snapshotId": "S1",
                           "targetId": "T0001", "protected": None,
                           "sameNameSiblings": None},
        "evidence": {"window": {"handle": 1, "title": "logo1"}},
        "assertion": None,
    }]
    result = compile_recording(RawRecording.from_dict(payload),
                               profile="windows_hisec")
    assert result.golden.status == "incomplete", (
        "身份矛盾后 golden 不得 ready —— 否则 CLI ok=true、回放放行")
    assert result.case.status == "incomplete"
    assert any(i.code.startswith("entry_identity_") for i in result.issues)


def test_rooted_v2_golden_uses_lease_entry_path(tmp_path):
    """P1-3 反例:带 root 的正常 v2 golden 必须执行租约入口获取。"""
    from agent.recording.runtime import build_replay_runtime
    golden = GoldenTrace.from_dict(_golden_dict(
        schema=GOLDEN_TRACE_SCHEMA_V2,
        entryWindow={"schema": "edr.entry-window-identity/v1",
                     "processName": "EDRClient.exe", "origin": "already_open",
                     "titleRegex": "^logo1$",
                     "rootAutomationId": "EdrMainWindow"}))
    # 首步 selector 带 root(rooted golden 的正常形态)。
    step = golden.steps[0]
    object.__setattr__(step, "selector", ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^logo1$",
                "rootAutomationId": "EdrMainWindow"},
        control={"automationId": "btnApply"}, visual=None))

    class _Agent:
        def __init__(self):
            self.calls = []
            self.state = SimpleNamespace(backend_kind="windows_pywinauto")

        def tools_list(self):
            return {"tools": [{"name": n} for n in PRODUCT_TOOLS]}

        def call_tool(self, name, args=None, timeout=None):
            self.calls.append((name, args))
            if name == "focus_entry_window":
                return {"ok": True, "disposition": "acquired",
                        "window": {"pid": 9, "title": "logo1", "handle": 1},
                        "run_id": "run-1"}
            return {"ok": True, "run_token": "tok-1"}

    agent = _Agent()

    class _Provider:
        def __init__(self, *args, **kwargs):
            pass

    from agent.recording import runtime as runtime_mod
    original = runtime_mod.MCPObservationProvider
    runtime_mod.MCPObservationProvider = _Provider
    try:
        build_replay_runtime(agent, golden, profile="windows_hisec",
                             trace_root=tmp_path, entry_recovery=True)
    finally:
        runtime_mod.MCPObservationProvider = original

    focus_calls = [args for name, args in agent.calls
                   if name == "focus_entry_window"]
    assert focus_calls, "rooted golden 必须走租约入口获取(死路径反例)"
    assert focus_calls[0]["root_automation_id"] == "EdrMainWindow"


def test_declared_identity_is_authoritative_not_mixed(tmp_path):
    """P2:声明身份存在时,process/title/root 全部来自 entryWindow,
    不得与首步 scope 混拼。"""
    from agent.recording.runtime import _acquire_entry_window_under_lease
    captured = {"audits": []}

    class _Agent:
        def call_tool(self, name, args=None, timeout=None):
            captured["args"] = dict(args)
            return {"ok": True, "disposition": "acquired"}

    identity = EntryWindowIdentity(
        process_name="Other.exe", title_regex="^Other$",
        root_automation_id="OtherRoot", origin="already_open")
    scope = SimpleNamespace(process_name="EDRClient.exe",
                            title_regex="^logo1$", root_automation_id="EdrRoot")
    store = SimpleNamespace(
        append_dict=lambda event_type, **kwargs:
        captured["audits"].append((event_type, kwargs)))
    _acquire_entry_window_under_lease(
        _Agent(), scope, "tok", timeout=None, trace_store=store,
        declared_identity=identity)
    args = captured["args"]
    assert args["process_name"] == "Other.exe"
    assert args["title_regex"] == "^Other$"
    assert args["root_automation_id"] == "OtherRoot", (
        "声明身份必须整体作为权威输入;混拼会让审计与实际验证不一致")
    requested = captured["audits"][0][1]["payload"]["scope"]
    assert requested == {
        "process_name": "Other.exe", "title_regex": "^Other$",
        "root_automation_id": "OtherRoot", "declared_identity": True,
    }, "审计必须记录实际发送给目标端的权威身份"
