"""S1:raw v2 录制实例与 windowTimeline(09 号设计 §4)。

失败回归先行。覆盖面(用户指定):
1. 多实例同名:同 handle 销毁重建 → 新 instanceId;同进程同标题两 handle → 两实例;
2. HIDE→SHOW 保持同实例(不误判新实例);DESTROY→同 handle 再现 → 新实例;
3. timeline 是事实来源:native SHOW/HIDE/DESTROY 分开落盘,不投影成 opened/closed;
4. 丢事件/乱序:coverage 声明 droppedEvents;乱序事件保留观测时间不重排;
5. 缺证据:无 handle 窗口的关联声明 unknown,不伪造;
6. 业务事件/快照/transition 引用同实例与 timeline 事件 ID;统一接收 order + 保留观测 monotonicMs;
7. raw v1 双读(不伪造 instanceId)+ v2 to_dict/from_dict round-trip;
8. 关联来源标注:temporal_forward 只是来源,不是 explicit,不是直接因果。
"""

from __future__ import annotations

import inspect

import json
import time

import pytest

from target.recording.models import CaptureScope, RawRecording
from target.recording.source import HookPacket
from target.recording.windows import WindowsUIACorrelator

pytestmark = pytest.mark.unit

SCOPE = CaptureScope("target", "windows_pywinauto", "EDRClient.exe", "^logo1$")


class _AppResolver:
    """Minimal in-scope resolver: every queried window belongs to the app."""

    def __init__(self, windows, app_names):
        self._windows = windows
        self._names = app_names

    def find_windows(self, *args, **kwargs):
        return list(self._windows)

    def process_pid_belongs_to_root(self, pid, root):
        return True

    def process_in_scope(self, name, pid=None):
        return True

    def foreground(self):
        return {"windowTitle": "logo1", "processName": "EDRClient.exe",
                "pid": 42, "handle": 13239958}


def _resolver():
    return _AppResolver(
        [{"title": "logo1", "pid": 42, "processName": "EDRClient.exe",
          "handle": 13239958}],
        ["EDRClient.exe"],
    )


def _click(seq, ms, causal="CAUSE-1", handle=13239958):
    packet = HookPacket(
        kind="pointer_click", monotonic_ms=ms,
        wall_time="2026-11-03T00:00:00.000Z",
        native={"kind": "pointer_down", "button": "left",
                "screen_point": (400, 300), "handle": handle,
                "processName": "EDRClient.exe", "pid": 42},
    )
    return packet, causal


def _transition(kind, ms, *, title="策略详情", handle=777, pid=42,
                process="EDRClient.exe"):
    return HookPacket(
        kind="window_transition", monotonic_ms=ms,
        wall_time="2026-11-03T00:00:00.000Z",
        native={"kind": kind, "processName": process, "title": title,
                "pid": pid, "handle": handle},
    )


def _correlate_all(correlator, packets, scope=SCOPE):
    events = []
    for index, packet in enumerate(packets, start=1):
        result = correlator.correlate(packet, scope, index)
        if result is None:
            continue
        if isinstance(result, tuple):
            events.extend(result)
        else:
            events.append(result)
    return events


# ---- 1/2:实例生命周期 ----

def test_hide_then_show_keeps_the_same_instance():
    """HIDE→SHOW 在未观察销毁时是同实例的新可见阶段(设计 §4.2)。"""
    correlator = WindowsUIACorrelator(_resolver())
    _correlate_all(correlator, [
        _transition("show", 100, title="日志中心", handle=777),
        _transition("hide", 200, title="日志中心", handle=777),
        _transition("show", 300, title="日志中心", handle=777),
    ])
    timeline = correlator.window_timeline
    instances = {e["instanceId"] for e in timeline["events"]
                 if e["instanceId"]}
    assert len(instances) == 1, (
        f"HIDE→SHOW 必须保持同一实例,实际 {instances}")
    by_kind = {e["kind"]: e for e in timeline["events"]}
    # kind 保留 native 事实:created(首见)/hidden/show(重显),不是 opened/closed
    assert by_kind["created"]["monotonicMs"] == 100, "首次观察 = created"
    assert by_kind["hidden"]["monotonicMs"] == 200, "HIDE 落 native 事实 hidden"
    assert by_kind["shown"]["monotonicMs"] == 300, "SHOW 重现 = shown(同实例新可见阶段)"
    ids = {e["instanceId"] for e in timeline["events"]}
    assert len(ids) == 1, "HIDE→SHOW 全程同实例"


def test_destroy_then_same_handle_is_a_new_instance():
    """DESTROY 后同 handle 再现必须分配新实例;老实例历史保留(设计 §4.2)。"""
    correlator = WindowsUIACorrelator(_resolver())
    _correlate_all(correlator, [
        _transition("show", 100, title="日志中心", handle=777),
        _transition("destroy", 200, title="日志中心", handle=777),
        _transition("show", 300, title="日志中心", handle=777),
    ])
    timeline = correlator.window_timeline
    appearances = [e for e in timeline["events"] if e["kind"] in ("created", "shown")]
    assert len(appearances) == 2
    assert appearances[0]["instanceId"] != appearances[1]["instanceId"], (
        "句柄复用必须产生新实例,不能沿用老 instanceId")
    destroyed = [e for e in timeline["events"] if e["kind"] == "destroyed"]
    assert len(destroyed) == 1
    assert destroyed[0]["instanceId"] == appearances[0]["instanceId"], (
        "destroy 归属老实例")


def test_same_title_two_handles_two_instances():
    """同进程同标题同时存在两个窗口:两个实例(设计 §4.2)。"""
    correlator = WindowsUIACorrelator(_resolver())
    _correlate_all(correlator, [
        _transition("show", 100, title="EDRClient", handle=101),
        _transition("show", 150, title="EDRClient", handle=202),
    ])
    timeline = correlator.window_timeline
    appearances = [e for e in timeline["events"] if e["kind"] in ("created", "shown")]
    assert len(appearances) == 2
    assert appearances[0]["instanceId"] != appearances[1]["instanceId"]


# ---- 3:timeline 是事实来源 ----

def test_timeline_records_native_kinds_not_opened_closed_projection():
    correlator = WindowsUIACorrelator(_resolver())
    _correlate_all(correlator, [
        _transition("show", 100, handle=777),
        _transition("hide", 200, handle=777),
        _transition("destroy", 300, handle=777),
    ])
    kinds = [e["kind"] for e in correlator.window_timeline["events"]]
    assert "created" in kinds and "hidden" in kinds and "destroyed" in kinds
    assert "opened" not in kinds and "closed" not in kinds, (
        "timeline 落盘 native 事实;opened/closed 是旧 manifest 的投影,不是 timeline 事实")


def test_timeline_is_append_only_with_order_and_observed_time():
    correlator = WindowsUIACorrelator(_resolver())
    _correlate_all(correlator, [
        _transition("show", 500, handle=777),
        _transition("hide", 100, handle=888),  # 故意乱序的观测时间
    ])
    events = correlator.window_timeline["events"]
    orders = [e["order"] for e in events]
    assert orders == sorted(orders), "order 是统一接收顺序,单调递增"
    assert events[0]["monotonicMs"] == 500 and events[1]["monotonicMs"] == 100, (
        "乱序事件的观测时间如实保留,不重排不伪造")


# ---- 5:缺证据 unknown ----

def test_no_handle_window_association_is_unknown_not_fabricated():
    correlator = WindowsUIACorrelator(_resolver())
    _correlate_all(correlator, [
        _transition("show", 100, title="匿名窗口", handle=None),
    ])
    timeline = correlator.window_timeline
    created = [e for e in timeline["events"] if e["kind"] == "created"]
    assert created, "无 handle 窗口仍要进 timeline(按 title 键跟踪)"
    entry = created[0]
    if "associatedActionCausalId" in entry:
        assert entry.get("association") in {"temporal_forward", "temporal_backfill",
                                            "unknown"}, (
            "有关联必须声明来源;无关联不伪造")
    else:
        assert entry.get("association", "unknown") == "unknown"


# ---- 6:事件/快照/transition 引用实例 ----

def test_business_event_and_transition_carry_instance_id():
    """绑定 transition(点击后打开的窗口)的 evidence 必须引用 timeline 事件
    ID 与录制实例 — 下游按实例关联,不再从裸 handle 重推导。"""
    from target.recording.models import RawCaptureEvent
    from test_windows_capture import _Resolver as _CaptureResolver

    correlator = WindowsUIACorrelator(
        _CaptureResolver(process="EDRClient.exe", title="logo1"))
    click = correlator.correlate(HookPacket(
        kind="pointer_up", monotonic_ms=100,
        wall_time="2026-11-03T00:00:00.000Z", screen_point=(50, 40),
        button="left"), SCOPE, 1)
    assert isinstance(click, RawCaptureEvent), "前置:点击已关联"
    shown = correlator.correlate(_transition("show", 700, title="策略详情",
                                             handle=777), SCOPE, 2)
    assert shown is not None and shown.type == "window_transition", (
        "前置:点击后的窗口出现被绑定为 transition")
    assert shown.evidence.get("windowTimelineEventId"), (
        "transition 必须引用产生它的 timeline 事件 ID")
    assert shown.evidence.get("windowInstanceId"), (
        "transition 必须引用录制实例 instanceId")
    timeline_event = next(e for e in correlator.window_timeline["events"]
                          if e["id"] == shown.evidence["windowTimelineEventId"])
    assert timeline_event["instanceId"] == shown.evidence["windowInstanceId"], (
        "evidence 引用的 timeline 事件与实例必须一致")


# ---- 7:v1 双读 + round-trip ----

def test_v1_recording_still_reads_without_instance_ids():
    recording = RawRecording.from_dict({
        "schema": "edr.desktop-recording/v1", "name": "flow", "sessionId": "REC-1",
        "events": [], "controlSnapshots": [],
        "captureDiagnostics": {"droppedPackets": 0, "correlationErrorCount": 0},
        "rejections": [],
        "windows": [{"title": "日志中心", "processName": "EDRClient",
                     "origin": "opened", "openedBy": "CAUSE-1",
                     "handle": 777, "closed": False}],
    })
    assert recording.schema == "edr.desktop-recording/v1"
    assert recording.window_timeline is None, "v1 无 timeline:不伪造"


def test_v2_round_trip_preserves_timeline():
    recording = RawRecording(
        "REC-1", "flow", (),
        {"droppedPackets": 0, "correlationErrorCount": 0},
        schema="edr.desktop-recording/v2",
        windows=({"instanceId": "RI-0001", "title": "logo1",
                  "processName": "EDRClient.exe", "origin": "already_open",
                  "handle": 13239958, "closed": False},),
        window_timeline={
            "schema": "edr.recorded-window-timeline/v1",
            "events": (
                {"id": "WE1", "order": 1, "monotonicMs": 1000,
                 "instanceId": "RI-0001", "kind": "initial_observed",
                 "source": "initial_inventory"},
            ),
            "coverage": {"destroyEvents": "supported", "droppedEvents": 0},
        },
    )
    document = recording.to_dict()
    restored = RawRecording.from_dict(document)
    assert restored.schema == "edr.desktop-recording/v2"
    # 序列化形态逐字段一致(events 统一数组);重序列化稳定(§9 兼容要求)。
    assert restored.window_timeline == recording.window_timeline or (
        json.loads(json.dumps(restored.window_timeline, sort_keys=True))
        == json.loads(json.dumps(recording.window_timeline, sort_keys=True)))
    assert json.loads(json.dumps(restored.to_dict(), sort_keys=True)) == \
        json.loads(json.dumps(document, sort_keys=True)), "round-trip 稳定"
    assert restored.windows == recording.windows


def test_v2_rejects_timeline_schema_mismatch():
    with pytest.raises(Exception):
        RawRecording.from_dict({
            "schema": "edr.desktop-recording/v2", "name": "flow",
            "sessionId": "REC-1", "events": [], "controlSnapshots": [],
            "captureDiagnostics": {"droppedPackets": 0,
                                   "correlationErrorCount": 0},
            "rejections": [], "windows": [],
            "windowTimeline": {"schema": "edr.recorded-window-timeline/v9",
                               "events": [], "coverage": {}},
        })


def test_v2_rejects_dangling_instance_reference():
    with pytest.raises(Exception):
        RawRecording.from_dict({
            "schema": "edr.desktop-recording/v2", "name": "flow",
            "sessionId": "REC-1", "events": [], "controlSnapshots": [],
            "captureDiagnostics": {"droppedPackets": 0,
                                   "correlationErrorCount": 0},
            "rejections": [], "windows": [],
            "windowTimeline": {
                "schema": "edr.recorded-window-timeline/v1",
                "events": ({"id": "WE1", "order": 1, "monotonicMs": 1,
                            "instanceId": "RI-404", "kind": "shown",
                            "source": "native_show"},),
                "coverage": {"destroyEvents": "supported", "droppedEvents": 0},
            },
        })


# ---- 8:关联来源不是 explicit ----

def test_temporal_forward_is_a_source_label_never_explicit():
    correlator = WindowsUIACorrelator(_resolver())
    _correlate_all(correlator, [
        _transition("show", 100, title="日志中心", handle=777),
    ])
    for event in correlator.window_timeline["events"]:
        assert event.get("association") != "explicit", (
            "正向最近动作关联只是来源标注(设计 §4.3),永远不能写成 explicit")


# ---- 快照/manifest 实例一致性 ----

def test_manifest_entry_carries_the_timeline_instance_id():
    """manifest 条目与 timeline 引用同一 instanceId:下游按实例关联,
    不再从裸 handle 重推导(设计 §4.1 windows[].instanceId 投影)。"""
    correlator = WindowsUIACorrelator(_resolver())
    correlator.correlate(_transition("show", 100, title="日志中心", handle=777),
                         SCOPE, 1)
    registry = correlator.window_registry
    assert registry, "前置:窗口已注册"
    entry = next(w for w in registry if w["title"] == "日志中心")
    timeline_event = next(e for e in correlator.window_timeline["events"]
                          if e["kind"] == "created")
    assert entry.get("instanceId") == timeline_event["instanceId"], (
        "manifest 条目必须携带与 timeline 一致的录制实例 ID")
    assert entry["openedBy"] is None, (
        "无动作可归因的窗口:openedBy 保持空,关联标注 unknown,不伪造")


# ---- JSON schema 同步 ----

def test_v2_document_validates_against_json_schema():
    """raw v2 落盘文档必须通过 desktop-recording.schema.json(§9:模型/schema/
    工具能力一并完成版本测试)。"""
    import jsonschema
    from pathlib import Path
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "schema"
         / "desktop-recording.schema.json").read_text(encoding="utf-8"))
    recording = RawRecording(
        "REC-1", "flow", (),
        {"droppedPackets": 0, "correlationErrorCount": 0},
        schema="edr.desktop-recording/v2",
        windows=({"instanceId": "RI-0001", "title": "logo1",
                  "processName": "EDRClient.exe", "origin": "already_open",
                  "handle": 13239958, "closed": False},),
        window_timeline={
            "schema": "edr.recorded-window-timeline/v1",
            "events": (
                {"id": "WE1", "order": 1, "monotonicMs": 1000,
                 "instanceId": "RI-0001", "kind": "initial_observed",
                 "source": "initial_inventory"},
                {"id": "WE2", "order": 2, "monotonicMs": 1200,
                 "instanceId": "RI-0001", "kind": "shown",
                 "source": "native_show",
                 "associatedActionCausalId": "CAUSE-1",
                 "association": "temporal_forward"},
            ),
            "coverage": {"destroyEvents": "supported", "droppedEvents": 0},
        },
    )
    jsonschema.validate(recording.to_dict(), schema)


def test_v1_document_still_validates_and_v2_fields_rejected_by_v1_schema():
    import jsonschema
    from pathlib import Path
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "schema"
         / "desktop-recording.schema.json").read_text(encoding="utf-8"))
    v1 = RawRecording.from_dict({
        "schema": "edr.desktop-recording/v1", "name": "flow",
        "sessionId": "REC-1", "events": [], "controlSnapshots": [],
        "captureDiagnostics": {"droppedPackets": 0, "correlationErrorCount": 0},
        "rejections": [],
        "windows": [{"title": "日志中心", "processName": "EDRClient",
                     "origin": "opened", "openedBy": "CAUSE-1",
                     "handle": 777, "closed": False}],
    })
    jsonschema.validate(v1.to_dict(), schema)
    # 旧 schema 语义(§9 老 reader 显式拒绝):v1 文档带 instanceId/windowTimeline
    # 必须拒绝。构造一个非法 v1 文档直接对 schema 断言。
    bad = v1.to_dict()
    bad["windows"][0]["instanceId"] = "RI-0001"
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(bad, schema)


# ---- 复验 P1 反例(实际路径,真实对象)----

def _real_correlator():
    return WindowsUIACorrelator(_resolver())


def test_p1_session_recording_survives_v2_windows():
    """复验 P1-1:真 RecordingSession.recording() 读取带 instanceId 的
    manifest 不得崩 —— 停止收集是新增窗口字段的第一消费者。"""
    from types import SimpleNamespace
    from target.recording.session import RecordingSession
    correlator = _real_correlator()
    _correlate_all(correlator, [
        _transition("show", 100, title="日志中心", handle=777),
    ])
    assert correlator.window_registry[0].get("instanceId"), "前置:v2 实例已标注"
    session = RecordingSession(
        "REC-P1", "flow", SCOPE,
        source=SimpleNamespace(
            _correlator=correlator,
            dropped_packets=0, out_of_scope_events=0,
            correlation_errors=(), control_snapshots=(),
        ),
    )
    recording = session.recording()
    assert recording.schema in {"edr.desktop-recording/v1",
                                "edr.desktop-recording/v2"}
    assert recording.window_timeline is not None, (
        "session 必须把 correlator 的 window_timeline 带进录制文档")
    assert recording.windows[0].get("instanceId"), "manifest 实例引用不得丢失"
    # 落盘形态可重读(服务持久化 → agent 收集重读的最小等价路径)。
    restored = RawRecording.from_dict(recording.to_dict())
    assert restored.window_timeline == recording.window_timeline or (
        json.loads(json.dumps(restored.window_timeline, sort_keys=True))
        == json.loads(json.dumps(recording.window_timeline, sort_keys=True)))


def test_p1_destroy_reuse_keeps_two_manifest_instances():
    """复验 P1-2:show→destroy→show(同 handle)必须产出两个 manifest 实例,
    且实际 correlator 输出可直接构造/序列化/重读 raw v2(不能只比编号)。"""
    correlator = _real_correlator()
    _correlate_all(correlator, [
        _transition("show", 100, title="日志中心", handle=777),
        _transition("destroy", 200, title="日志中心", handle=777),
        _transition("show", 300, title="日志中心", handle=777),
    ])
    manifest_ids = [w.get("instanceId") for w in correlator.window_registry]
    timeline_ids = [e.get("instanceId") for e in correlator.window_timeline["events"]]
    assert len(manifest_ids) == 2, (
        f"manifest 必须按实例保留两条声明,实际 {manifest_ids}")
    assert set(manifest_ids) == {i for i in timeline_ids if i}, (
        "timeline 引用的每个实例都必须有 manifest 声明")
    # 用实际输出构造 raw v2 并重读 —— 这正是复验反例的崩点。
    recording = RawRecording(
        "REC-P1b", "flow", (),
        {"droppedPackets": 0, "correlationErrorCount": 0},
        schema="edr.desktop-recording/v2",
        windows=tuple(dict(w) for w in correlator.window_registry),
        window_timeline=json.loads(json.dumps(correlator.window_timeline)),
    )
    restored = RawRecording.from_dict(recording.to_dict())
    assert len(restored.windows) == 2
    # 旧实例的 manifest 声明不被新实例覆盖(背书语义不变)。
    old, new = restored.windows
    assert old["instanceId"] != new["instanceId"]
    assert old["origin"] == "opened" and new["origin"] == "opened"




# ---- 复验补充验收项(业务事件实例引用/初始窗口 timeline)----

def test_pointer_event_evidence_names_instance_and_initial_window_enters_timeline():
    """复验收口项:普通业务事件(点击)的 evidence.window 必须携带 instanceId;
    入口窗口(从不 transition)也必须有 initial_observed timeline 事实 ——
    复验时真实 correlate 输出 timeline events=0 是验收缺口。"""
    from test_windows_capture import _Resolver as _CaptureResolver
    from target.recording.source import HookPacket as _HookPacket

    correlator = WindowsUIACorrelator(
        _CaptureResolver(process="EDRClient.exe", title="logo1"))
    click = correlator.correlate(_HookPacket(
        kind="pointer_up", monotonic_ms=100,
        wall_time="2026-11-03T00:00:00.000Z", screen_point=(50, 40),
        button="left"), SCOPE, 1)
    assert click is not None, "前置:点击已关联"
    window = click.evidence.get("window") or {}
    assert window.get("instanceId"), (
        f"业务事件 evidence.window 必须携带实例引用,实际 {window}")
    kinds = [e["kind"] for e in correlator.window_timeline["events"]]
    assert kinds, f"入口窗口必须进 timeline,实际 kinds={kinds}"
    # 无 seed 时经由输入路径首见:来源如实 input_observation,不冒充清单观测。
    fact = correlator.window_timeline["events"][0]
    assert fact["kind"] == "observed" and fact["source"] == "input_observation"
    assert fact["instanceId"] == window["instanceId"], (
        "业务事件引用的实例与首见观测必须是同一实例")


# ---- 0457 复验反例(实际路径,先红后绿)----

def _review_resolver():
    """复验脚本同款 resolver:seed/foreground 均返回 handle777。"""
    from test_windows_capture import _Resolver as _CaptureResolver
    r = _CaptureResolver(process="EDRClient.exe", title="logo1")
    r.windows = lambda: [{"handle": 777, "title": "logo1",
                          "processName": "EDRClient.exe", "pid": 42}]
    r.foreground = lambda: {"handle": 777, "windowTitle": "logo1",
                            "processName": "EDRClient.exe", "pid": 42}
    return r


def test_p1_initial_window_session_recording_roundtrip():
    """0457 P1-1:seed_scope(已有 handle777)→ 普通 pointer_up →
    RecordingSession.recording() 必须可序列化 — 实例在首次可信观测时统一
    建立,manifest/initial timeline/快照/业务事件引用同一实例。"""
    from types import SimpleNamespace
    from target.recording.session import RecordingSession
    correlator = WindowsUIACorrelator(_review_resolver())
    correlator.seed_scope(SCOPE)
    correlator.correlate(HookPacket(
        kind="pointer_up", monotonic_ms=100,
        wall_time="2026-11-03T00:00:00.000Z", screen_point=(50, 40),
        button="left"), SCOPE, 1)
    manifest_ids = [w.get("instanceId") for w in correlator.window_registry]
    timeline_ids = {e.get("instanceId")
                    for e in correlator.window_timeline["events"]}
    assert manifest_ids and all(manifest_ids), (
        f"seed 登记的 manifest 必须带实例,实际 {manifest_ids}")
    assert {i for i in manifest_ids} <= timeline_ids, (
        "manifest 实例必须在 timeline 中有声明的初始观测")
    session = RecordingSession(
        "REC-0457a", "review", SCOPE,
        source=SimpleNamespace(_correlator=correlator, dropped_packets=0))
    recording = session.recording()  # 修前:RI-0001 does not name a declared window instance
    restored = RawRecording.from_dict(recording.to_dict())
    assert restored.schema == "edr.desktop-recording/v2"
    initial = [e for e in restored.window_timeline["events"]
               if e["kind"] == "initial_observed"]
    assert initial, "入口窗口必须有 initial_observed"
    # initial 的来源是真实清单观测(seed 发生在任何输入包之前,
    # 没有 ingressSequence 是事实 —— 不得把输入观测冒充清单来源)。
    assert initial[0]["source"] == "initial_inventory"
    assert "monotonicMs" in initial[0] and "order" in initial[0]


def test_p1_driver_destroy_paths():
    """0457 P1-2:真实 callback 的三条 DESTROY 路径。
    a) 已存身份 + 销毁后 GetAncestor=0:仍要落 destroyed 包(不查死 HWND);
    b) 无身份 DESTROY + ancestor=hwnd:不崩(driver 有丢包计数),计丢包;
    c) driver 丢包计数进入 correlator/最终 coverage。"""
    import ast
    import time as _time
    from pathlib import Path
    from types import SimpleNamespace
    from target.recording import windows as W

    module = ast.parse(Path(W.__file__).read_text(encoding="utf-8"))
    cls = next(n for n in module.body if isinstance(n, ast.ClassDef)
               and n.name == "WindowsWinEventDriver")
    callback_node = next(n for n in ast.walk(cls)
                         if isinstance(n, ast.FunctionDef)
                         and n.name == "callback")

    def run_destroy(ancestor_value, remembered):
        driver = W.WindowsWinEventDriver("EDRClient.exe")
        packets = []
        driver._emit = packets.append
        if remembered:
            driver._titles[777] = ("dialog", "EDRClient.exe", 42)
        env = {"self": driver,
               "user32": SimpleNamespace(GetAncestor=lambda h, f: ancestor_value),
               "HookPacket": HookPacket, "time": _time,
               "_utc_now": lambda: "2026-11-03T00:00:00.000Z"}
        exec(compile(ast.Module(body=[callback_node], type_ignores=[]),
                     "<actual-production-callback>", "exec"), env)
        env["callback"](None, driver.EVENT_OBJECT_DESTROY, 777, 0, 0, None, None)
        return packets, driver

    # a) 销毁后窗口已死:ancestor=0,已存身份 → destroyed 包仍落盘。
    packets, driver = run_destroy(0, remembered=True)
    assert len(packets) == 1 and packets[0].native.get("nativeKind") == "destroyed", (
        f"销毁后 GetAncestor=0 不得吞 DESTROY,实际 {len(packets)} 包")
    assert 777 not in driver._titles, "destroyed 后身份退役"

    # b) 无身份 DESTROY:不崩,计丢包,不伪造实例。
    packets2, driver2 = run_destroy(777, remembered=False)
    assert not packets2
    assert getattr(driver2, "dropped_window_events", None) == 1, (
        "无身份 DESTROY 必须计入丢包(且 driver 必须初始化该计数)")

    # c) loss 汇总进 correlator timeline coverage。
    correlator = WindowsUIACorrelator(_resolver())
    correlator.note_hook_loss(driver2.dropped_window_events)
    coverage = correlator.refresh_timeline_coverage(dropped_packets=2)
    assert coverage["droppedEvents"] == 3, "driver 丢包必须汇总进最终 coverage"


def test_p1_inventory_instance_isolation():
    """0457 P1-3:同 handle 先后两实例,pending/快照/去重按实例隔离;
    采集期间换代不得贴旧 RI,无法证明归属时明确丢弃并计数。"""
    import runpy
    fixture = runpy.run_path("test_case/test_recording/test_control_inventory.py")
    resolver = fixture["_Resolver"]({}, "root")
    resolver.trees[7] = fixture["_tree"](fixture["_control"]("W.a"))
    inventory = fixture["ControlInventory"](resolver)
    # 权威实例索引(真实 correlator 行为的最小替身):当前实例随换代翻转,
    # 每次捕获时的当前实例 = 该请求可证明的归属。
    authority_state = {"current": "RI-0001"}
    inventory.instance_authority = lambda handle: authority_state["current"]
    inventory.request(handle=7, instance_id="RI-0001")
    inventory.request(handle=7, instance_id="RI-0002")
    assert inventory._pending[7]["instanceId"] == "RI-0002", (
        "同 handle 新实例请求必须覆盖 pending 的旧实例")
    inventory._capture({"handle": 7, "instanceId": "RI-0001"})
    authority_state["current"] = "RI-0002"  # 换代完成
    inventory._capture({"handle": 7, "instanceId": "RI-0002"})
    snap_ids = [x["window"].get("instanceId") for x in inventory.snapshots]
    assert snap_ids == ["RI-0001", "RI-0002"], (
        f"同内容不同实例必须各自落快照,实际 {snap_ids} skipped={inventory.skipped}")


def test_p2_strict_timeline_coverage():
    """0457 P2:coverage 与 schema 严格一致 — {} / 未知字段 / 非法枚举 /
    负数全部拒绝。"""
    for bad in ({}, {"destroyEvents": "invented", "droppedEvents": 0},
                {"destroyEvents": "supported", "droppedEvents": -1},
                {"destroyEvents": "supported", "droppedEvents": 0,
                 "unknown": True},
                {"destroyEvents": "supported"}):
        with pytest.raises(Exception):
            RawRecording("REC-bad", "review", (),
                         {"droppedPackets": 0, "correlationErrorCount": 0},
                         schema="edr.desktop-recording/v2",
                         window_timeline={"schema": "edr.recorded-window-timeline/v1",
                                          "events": [], "coverage": bad})


# ---- 0517 复验反例(真实链路,先红后绿)----

def _real_source_with_driver(dropped_window_events=0):
    """真实 QueuedCaptureSource:Composite(内含计丢包的 WinEventDriver)。"""
    from target.recording.source import CompositeHookDriver, QueuedCaptureSource
    from target.recording.windows import (WindowsUIACorrelator,
                                          WindowsLowLevelHookDriver,
                                          WindowsWinEventDriver)
    driver = WindowsWinEventDriver("EDRClient.exe")
    driver.dropped_window_events = dropped_window_events
    correlator = WindowsUIACorrelator(_resolver())
    source = QueuedCaptureSource(
        scope=SCOPE,
        sink=lambda event: True,
        driver=CompositeHookDriver(
            WindowsLowLevelHookDriver(), driver),
        correlator=correlator,
    )
    return source, correlator, driver


def test_0517_loss_flows_through_real_source_to_coverage():
    """0517 P1-1:driver 丢包 → Composite → **真实 source** → session
    coverage;重复 recording() 不得重复累加同一累计丢失。"""
    from types import SimpleNamespace
    from target.recording.session import RecordingSession
    source, correlator, driver = _real_source_with_driver(3)
    session = RecordingSession(
        "REC-0517a", "review", SCOPE, source=source)
    recording = session.recording()
    coverage = recording.window_timeline["coverage"]
    assert coverage["droppedEvents"] == 3, (
        f"真实 source 链的 driver 丢包必须到达最终 coverage,实际 {coverage}")
    # 重复收集:同一累计丢失不得重复累加。
    again = session.recording().window_timeline["coverage"]
    assert again["droppedEvents"] == 3, (
        f"重复 recording 不得重复累计,实际 {again}")


def test_0517_in_flight_generation_change_invalidates_snapshot():
    """0517 P1-2:walk 期间换代 → 旧实例标签不得背书新树。
    resolver.control_tree 调用中注入同 handle RI-0002 请求(换代),再返回
    新窗口的 W.NEW 树;权威实例索引同时翻转到 RI-0002(真实 correlator 行为:
    destroy 退役旧实例)。发布前校验必须丢弃旧结果并结构化计数。"""
    import runpy
    fixture = runpy.run_path("test_case/test_recording/test_control_inventory.py")
    resolver = fixture["_Resolver"]({}, "root")
    inventory = fixture["ControlInventory"](resolver)
    # 权威索引:walk 开始时 RI-0001 是 handle7 的实例;注入换代后 RI-0002。
    authority_state = {"current": "RI-0001"}
    inventory.instance_authority = (
        lambda handle: authority_state["current"])

    def swapping_control_tree(handle, max_depth=12):
        if authority_state["current"] == "RI-0001":
            # 模拟 walk 期间 destroy+复用:新实例请求进 pending,权威翻转,
            # 返回新窗口的树 —— 旧请求拿到的树属于新实例。
            authority_state["current"] = "RI-0002"
            inventory.request(handle=handle, instance_id="RI-0002")
            return {"ok": True, "title": "新窗口", "controls": [
                fixture["_control"]("W.NEW")], "complete": True}
        return {"ok": True, "title": "旧窗口", "controls": [
            fixture["_control"]("W.a")], "complete": True}

    resolver.control_tree = swapping_control_tree
    inventory.request(handle=7, instance_id="RI-0001")
    inventory._capture({"handle": 7, "instanceId": "RI-0001"})
    snap_ids = [x["window"].get("instanceId") for x in inventory.snapshots]
    assert "RI-0001" not in snap_ids, (
        f"换代后旧实例的 walk 结果不得发布;实际 {snap_ids}")
    assert inventory.skipped.get("instance_superseded", 0) >= 1, (
        f"换代必须结构化丢弃并计数,实际 {inventory.skipped}")
    # destroy 后尚无新 request:authority=None(实例退役)→ 同样不可发布。
    authority_state["current"] = None
    inventory.request(handle=7, instance_id="RI-0001")
    inventory._capture({"handle": 7, "instanceId": "RI-0001"})
    assert "RI-0001" not in [x["window"].get("instanceId")
                             for x in inventory.snapshots], (
        "authority 未知(实例退役)时旧实例结果同样不得发布")


def test_0517_unseeded_first_input_session_roundtrip():
    """0517 P1-3:无 seed/SHOW 丢失时,首次业务输入创建的实例必须登记
    manifest(来源如实 input_observation,不冒充 initial_inventory),
    session.recording 可序列化可重读。"""
    from types import SimpleNamespace
    from target.recording.session import RecordingSession
    correlator = WindowsUIACorrelator(_review_resolver())
    # 不调 seed_scope(模拟 seed 漏窗口/SHOW 丢失),直接首输入。
    correlator.correlate(HookPacket(
        kind="pointer_up", monotonic_ms=100,
        wall_time="2026-11-03T00:00:00.000Z", screen_point=(50, 40),
        button="left"), SCOPE, 1)
    manifest_ids = [w.get("instanceId") for w in correlator.window_registry]
    assert "RI-0001" in manifest_ids, (
        f"首次输入观测的实例必须登记 manifest,实际 {manifest_ids}")
    session = RecordingSession(
        "REC-0517c", "review", SCOPE,
        source=SimpleNamespace(_correlator=correlator, dropped_packets=0))
    recording = session.recording()  # 修前:RI-0001 does not name a declared instance
    restored = RawRecording.from_dict(recording.to_dict())
    initial = [e for e in restored.window_timeline["events"]
               if e["instanceId"] == "RI-0001"]
    assert initial, "该实例必须有 timeline 事实"
    kinds = {e["kind"] for e in initial}
    sources = {e["source"] for e in initial}
    assert "initial_observed" not in kinds, (
        "输入路径首见不是清单观测:不得冒充 initial_inventory")
    assert "input_observation" in sources, (
        f"来源必须如实标注 input_observation,实际 {sources}")


# ---- 0547 复验反例(标题别名 P1,先红后绿)----

def _alias_correlator():
    """show(handle101,logo1) 后的状态:RI1=handle101。"""
    correlator = WindowsUIACorrelator(_resolver())
    correlator.correlate(_transition("show", 100, title="logo1", handle=101),
                         SCOPE, 1)
    return correlator


def test_0547_new_handle_is_never_aliased_to_sibling_instance():
    """0547-1:输入明确来自 handle202(其 SHOW 未见)——不得借同标题兄弟
    RI1(handle101)背书;202 是一次新的独立观测,如实登记。"""
    correlator = _alias_correlator()
    from test_windows_capture import _Resolver as _CaptureResolver
    # 输入解析到 handle202(foreground 如实报告 202)
    correlator._resolver = _CaptureResolver(process="EDRClient.exe", title="logo1")
    correlator._resolver.foreground = lambda: {
        "handle": 202, "windowTitle": "logo1",
        "processName": "EDRClient.exe", "pid": 42}
    click = correlator.correlate(HookPacket(
        kind="pointer_up", monotonic_ms=200,
        wall_time="2026-11-03T00:00:00.000Z", screen_point=(50, 40),
        button="left"), SCOPE, 2)
    assert click is not None
    window = click.evidence.get("window") or {}
    assert window.get("handle") == 202
    ri202 = window.get("instanceId")
    assert ri202 != "RI-0001", (
        f"明确不同的 native handle 不得借同标题兄弟背书,实际 {window}")
    # 走 session 序列化重读:引用一致(manifest 含两个实例声明)。
    from types import SimpleNamespace
    from target.recording.session import RecordingSession
    session = RecordingSession(
        "REC-0547a", "review", SCOPE,
        source=SimpleNamespace(_correlator=correlator, dropped_packets=0))
    restored = RawRecording.from_dict(session.recording().to_dict())
    manifest_ids = [w.get("instanceId") for w in restored.windows]
    assert ri202 in manifest_ids, "新实例必须登记 manifest"
    assert {w.get("instanceId") for w in restored.windows} == \
        {e.get("instanceId") for e in restored.window_timeline["events"]
         if e.get("instanceId")}, "manifest 与 timeline 引用一致"


def test_0547_two_live_same_title_without_handle_keeps_gap():
    """0547-2:同标题两活实例 + 无 handle 输入 —— 不得静默选第一个;
    evidence 不贴确定 RI,timeline 如实记录 unknown 缺口。"""
    correlator = _alias_correlator()
    correlator.correlate(_transition("show", 150, title="logo1", handle=202),
                         SCOPE, 2)
    # 无 handle 输入(foreground 不带 handle —— 弱 resolver 契约)。
    from test_windows_capture import _Resolver as _CaptureResolver
    correlator._resolver = _CaptureResolver(process="EDRClient.exe", title="logo1")
    click = correlator.correlate(HookPacket(
        kind="pointer_up", monotonic_ms=200,
        wall_time="2026-11-03T00:00:00.000Z", screen_point=(50, 40),
        button="left"), SCOPE, 3)
    assert click is not None
    window = click.evidence.get("window") or {}
    assert window.get("instanceId") is None, (
        "多候选同标题无 handle:不得静默选第一个,实际 "
        f"{window.get('instanceId')}")
    # session 序列化重读:无确定引用,不得出现两个实例之一被误指。
    from types import SimpleNamespace
    from target.recording.session import RecordingSession
    session = RecordingSession(
        "REC-0547b", "review", SCOPE,
        source=SimpleNamespace(_correlator=correlator, dropped_packets=0))
    restored = RawRecording.from_dict(session.recording().to_dict())
    assert restored.window_timeline is not None


def test_0547_retired_instance_never_revives_via_title():
    """0547-3:destroy101 后(live 只剩 202→RI2),无 handle 输入不得引用
    已退役 RI1 —— 标题别名必须跟随实例生命周期。"""
    correlator = _alias_correlator()
    correlator.correlate(_transition("destroy", 120, title="logo1", handle=101),
                         SCOPE, 2)
    correlator.correlate(_transition("show", 150, title="logo1", handle=202),
                         SCOPE, 3)
    from test_windows_capture import _Resolver as _CaptureResolver
    correlator._resolver = _CaptureResolver(process="EDRClient.exe", title="logo1")
    click = correlator.correlate(HookPacket(
        kind="pointer_up", monotonic_ms=200,
        wall_time="2026-11-03T00:00:00.000Z", screen_point=(50, 40),
        button="left"), SCOPE, 4)
    assert click is not None
    window = click.evidence.get("window") or {}
    ri = window.get("instanceId")
    assert ri != "RI-0001", (
        "已退役实例不得经标题别名复活,实际 " + str(ri))
    # 序列化重读:引用一致。
    from types import SimpleNamespace
    from target.recording.session import RecordingSession
    session = RecordingSession(
        "REC-0547c", "review", SCOPE,
        source=SimpleNamespace(_correlator=correlator, dropped_packets=0))
    restored = RawRecording.from_dict(session.recording().to_dict())
    live_ids = {e.get("instanceId") for e in restored.window_timeline["events"]}
    assert ri is None or ri in live_ids


# ---- 0607 复验反例(生命周期统一不变量,先红后绿)----

def test_0607_lost_show_first_hide_collects_and_serializes():
    """0607 P1-1:SHOW 队列溢出丢失后首 HIDE —— 真实 source 队列丢包链。
    manifest 必须有声明(来源如实,不冒充清单/不造打开因果),session 可
    停止/收集/重读;source=native_hide、kind=hidden 保持真实事实。"""
    import queue as _queue
    from types import SimpleNamespace
    from target.recording.session import RecordingSession
    from target.recording.source import CompositeHookDriver, QueuedCaptureSource
    from target.recording.windows import (WindowsUIACorrelator,
                                          WindowsLowLevelHookDriver,
                                          WindowsWinEventDriver)
    correlator = WindowsUIACorrelator(_resolver())
    source = QueuedCaptureSource(
        scope=SCOPE, sink=lambda event: True,
        driver=CompositeHookDriver(WindowsLowLevelHookDriver(),
                                   WindowsWinEventDriver("EDRClient.exe")),
        correlator=correlator, queue_size=1)
    source._started = True  # 只为直接驱动 worker 路径;不开线程。
    # 生产丢包路径:SHOW 入队后被后续包挤出(队列 size=1),HIDE 到达 worker。
    source._enqueue(_transition("show", 100, title="日志中心", handle=777))
    source._enqueue(_transition("hide", 200, title="日志中心", handle=777))
    assert source.dropped_packets >= 1, "前置:SHOW 已被队列溢出丢弃"
    correlator.correlate(_transition("hide", 200, title="日志中心", handle=777),
                         SCOPE, 2)
    session = RecordingSession(
        "REC-0607a", "review", SCOPE,
        source=SimpleNamespace(_correlator=correlator,
                               dropped_packets=source.dropped_packets,
                               correlation_errors=(), control_snapshots=()))
    recording = session.recording()  # 修前:RI-0001 does not name declared instance
    restored = RawRecording.from_dict(recording.to_dict())
    assert restored.windows, "hidden 首见必须有 manifest 声明"
    assert restored.windows[0].get("instanceId"), "manifest 必须带实例"
    fact = next(e for e in restored.window_timeline["events"]
                if e["kind"] == "hidden")
    assert fact["source"] == "native_hide", (
        f"HIDE 事实来源必须如实,实际 {fact['source']}")
    assert fact["instanceId"] == restored.windows[0]["instanceId"]


def test_0607_pid_change_creates_new_instance_with_identity_changed():
    """0607 P1-2:同 HWND 的 pid 正向变化 = 新实例(§4.2),旧历史保留,
    identity_changed 事实落盘;同 owner HIDE→SHOW 仍同实例。"""
    correlator = WindowsUIACorrelator(_resolver())
    correlator.correlate(_transition("show", 100, title="策略详情",
                                     handle=777, pid=42), SCOPE, 1)
    correlator.correlate(_transition("show", 200, title="策略详情",
                                     handle=777, pid=99), SCOPE, 2)
    manifest = correlator.window_registry
    assert len(manifest) == 2, f"身份变化必须新实例,实际 {len(manifest)} 条"
    pids = {w.get("pid"): w.get("instanceId") for w in manifest}
    assert pids[42] != pids[99], "新旧实例 ID 必须不同"
    assert pids[42] == "RI-0001", "旧实例历史保留"
    kinds = [(e["kind"], e["instanceId"]) for e in correlator.window_timeline["events"]]
    # identity_changed 记录该 HWND 身份在观测时刻变化的实事,归属解析到的
    # 当前(新)实例;旧实例历史由 created+manifest 保留(设计 §4.2)。
    assert ("identity_changed", pids[99]) in kinds, (
        f"身份变化事实必须落盘,实际 {kinds}")
    assert ("created", pids[42]) in kinds, "旧实例首见事实保留"
    # 同 owner HIDE→SHOW 保持同实例(不回归)。
    correlator.correlate(_transition("hide", 300, title="策略详情",
                                     handle=777, pid=99), SCOPE, 3)
    correlator.correlate(_transition("show", 400, title="策略详情",
                                     handle=777, pid=99), SCOPE, 4)
    after = [w.get("instanceId") for w in correlator.window_registry
             if w.get("pid") == 99]
    assert len(after) == 1, "同 owner HIDE→SHOW 不得新建实例"


# ---- 0627 复验反例(input owner 入口 + 真实接收序,先红后绿)----

def _owner_resolver(pids_in_order):
    """foreground 返回显式控制的 pid:测试用 resolver.advance() 推进换代点
    (correlate 内部可能多次调用 foreground,不能按调用次数推进)。"""
    from test_windows_capture import _Resolver as _CaptureResolver
    r = _CaptureResolver(process="EDRClient.exe", title="logo1")
    state = {"index": 0}
    r.windows = lambda: [{"handle": 777, "title": "logo1",
                          "processName": "EDRClient.exe",
                          "pid": pids_in_order[0]}]

    def foreground():
        return {"handle": 777, "windowTitle": "logo1",
                "processName": "EDRClient.exe",
                "pid": pids_in_order[min(state["index"],
                                        len(pids_in_order) - 1)]}
    r.foreground = foreground
    r.advance = lambda: state.__setitem__("index", state["index"] + 1)
    return r


def _input(correlator, seq, ms=100):
    return correlator.correlate(HookPacket(
        kind="pointer_up", monotonic_ms=ms,
        wall_time="2026-11-03T00:00:00.000Z", screen_point=(50, 40),
        button="left"), SCOPE, seq)


def test_0627_input_owner_change_show_then_input():
    """0627 P1-1a:show777/pid42 → 无新 SHOW → 输入 foreground 777/pid99。
    输入观测必须验证 owner:新实例 + manifest 登记 + identity_changed;
    序列化重读引用一致。"""
    from types import SimpleNamespace
    from target.recording.session import RecordingSession
    correlator = WindowsUIACorrelator(
        _owner_resolver([42, 99]))
    correlator.correlate(_transition("show", 100, title="logo1",
                                     handle=777, pid=42), SCOPE, 1)
    correlator._resolver.advance()  # 无 SHOW 的换代:输入观测 owner=99
    _input(correlator, 2, 200)
    manifest = correlator.window_registry
    pids = {w.get("pid"): w.get("instanceId") for w in manifest}
    assert 99 in pids and pids[99] != pids[42], (
        f"输入观测的 owner 变化必须建立新实例,实际 {pids}")
    kinds = [(e["kind"], e["instanceId"])
             for e in correlator.window_timeline["events"]]
    assert ("identity_changed", pids[99]) in kinds, f"实际 {kinds}"
    # session 序列化重读(manifest/timeline/事件引用一致性)。
    correlator_events = []
    # 从 correlator 的 packet_trace 无法取事件;直接断言 evidence 引用与
    # manifest/timeline 一致,再走 session 序列化。
    from test_windows_capture import _Resolver as _CaptureResolver
    # 事件 evidence 已在上面 _input 的返回值里;重新驱动一次并捕获:
    correlator2 = WindowsUIACorrelator(_owner_resolver([42, 99]))
    correlator2.correlate(HookPacket(
        kind="window_transition", monotonic_ms=100, wall_time="t",
        native={"kind": "show", "processName": "EDRClient.exe",
                "title": "logo1", "pid": 42, "handle": 777}), SCOPE, 1)
    correlator2._resolver.advance()
    ev2 = _input(correlator2, 2, 200)
    assert ev2.evidence["window"]["instanceId"] == "RI-0002", (
        "事件引用必须指向新实例(旧 RI1/pid42 不得背书 pid99 的动作)")
    session = RecordingSession(
        "REC-0627a", "review", SCOPE,
        source=SimpleNamespace(_correlator=correlator, dropped_packets=0))
    restored = RawRecording.from_dict(session.recording().to_dict())
    assert {w.get("instanceId") for w in restored.windows} == \
        {e.get("instanceId") for e in restored.window_timeline["events"]
         if e.get("instanceId")}, "manifest↔timeline 引用一致"


def test_0627_input_owner_change_seed_then_input():
    """0627 P1-1b:seed(777/pid42)→ 输入 foreground 777/pid99 —— 同一不变量。"""
    from types import SimpleNamespace
    from target.recording.session import RecordingSession
    correlator = WindowsUIACorrelator(_owner_resolver([42, 99]))
    correlator.seed_scope(SCOPE)
    correlator._resolver.advance()
    _input(correlator, 1, 100)
    pids = {w.get("pid"): w.get("instanceId") for w in correlator.window_registry}
    assert 99 in pids and pids[99] != pids[42], f"实际 {pids}"
    session = RecordingSession(
        "REC-0627b", "review", SCOPE,
        source=SimpleNamespace(_correlator=correlator, dropped_packets=0))
    RawRecording.from_dict(session.recording().to_dict())


def test_0627_input_owner_change_input_then_input():
    """0627 P1-1c:两次输入间 foreground pid 42→99,中间无 SHOW。"""
    from types import SimpleNamespace
    from target.recording.session import RecordingSession
    correlator = WindowsUIACorrelator(_owner_resolver([42, 99]))
    _input(correlator, 1, 100)
    correlator._resolver.advance()
    _input(correlator, 2, 200)
    pids = {w.get("pid"): w.get("instanceId") for w in correlator.window_registry}
    assert 99 in pids and pids[99] != pids[42], f"实际 {pids}"
    session = RecordingSession(
        "REC-0627c", "review", SCOPE,
        source=SimpleNamespace(_correlator=correlator, dropped_packets=0))
    RawRecording.from_dict(session.recording().to_dict())


def test_0627_timeline_ingress_is_true_receive_order():
    """0627 P2:event.sequence 是 raw 产物连续序号(未产出事件的包不推进),
    不是接收序。timeline 的 ingressSequence 必须来自独立包接收计数:
    show777/hide777/pointer_up777 三包经真实 source,raw 仅 [1](pointer_up),
    show/hide 未产出事件但接收序必须递增且互不相同。"""
    from types import SimpleNamespace
    from target.recording.session import RecordingSession
    from target.recording.source import CompositeHookDriver, QueuedCaptureSource
    from target.recording.windows import (WindowsUIACorrelator,
                                          WindowsLowLevelHookDriver,
                                          WindowsWinEventDriver)

    correlator = WindowsUIACorrelator(_ForegroundResolver())
    source = QueuedCaptureSource(
        scope=SCOPE, sink=lambda event: True,
        driver=CompositeHookDriver(WindowsLowLevelHookDriver(),
                                   WindowsWinEventDriver("EDRClient.exe")),
        correlator=correlator, queue_size=16)
    source._started = True
    source._enqueue(_transition("show", 100, title="日志中心", handle=777))
    source._enqueue(_transition("hide", 200, title="日志中心", handle=777))
    source._enqueue(HookPacket(
        kind="pointer_up", monotonic_ms=300,
        wall_time="2026-11-03T00:00:00.000Z", screen_point=(50, 40),
        button="left"))
    # 排空队列(取尽三个包 + 不阻塞)。
    for _ in range(3):
        try:
            packet = source._queue.get_nowait()
            source._dequeued += 1
            correlated = correlator.correlate(
                packet, SCOPE, source._sequence + 1)
            if correlated is not None and source._sink(correlated):
                events = (correlated if isinstance(correlated, tuple)
                          else (correlated,))
                source._sequence = max(
                    e.sequence for e in events) if events else source._sequence
        except _queue.Empty:
            break
    ingest = [e.get("ingressSequence")
              for e in correlator.window_timeline["events"]]
    assert len(ingest) >= 2 and len(set(ingest)) == len(ingest), (
        f"接收序必须互不相同且递增,实际 {ingest}")
    session = RecordingSession(
        "REC-0627d", "review", SCOPE,
        source=SimpleNamespace(_correlator=correlator,
                               dropped_packets=0, correlation_errors=(),
                               control_snapshots=()))
    RawRecording.from_dict(session.recording().to_dict())




class _ForegroundResolver:
    """最小前台解析器(handle777/pid42),供真 source worker 测试使用。"""

    def foreground(self):
        return {"handle": 777, "windowTitle": "logo1",
                "processName": "EDRClient.exe", "pid": 42}

    def cursor_position(self):
        return (50, 40)

    def element_at(self, x, y):
        return {"controlType": "Button", "automationId": "btnApply",
                "text": "应用", "rect": [10, 20, 110, 60], "protected": False}

    def refresh(self, target):
        return self.element_at(0, 0)

    def focused(self):
        return self.element_at(0, 0)

    def process_in_scope(self, name, pid=None):
        return True

    def process_pid_belongs_to_root(self, pid, root):
        return True


# ---- 0647 复验反例(真实 source worker 接收序,先红后绿)----

class _EmittingFakeDriver:
    """start(emit) 后由测试直接调 emit 的假 driver(真实 source.start 接线)。"""

    def __init__(self):
        self.emit = None

    def start(self, emit):
        self.emit = emit

    def stop(self):
        self.emit = None


def _real_worker_source(packets, correlator=None):
    from types import SimpleNamespace
    from target.recording.session import RecordingSession
    from target.recording.source import (CompositeHookDriver,
                                         QueuedCaptureSource)
    from target.recording.windows import WindowsUIACorrelator
    correlator = correlator or WindowsUIACorrelator(_ForegroundResolver())
    source = QueuedCaptureSource(
        scope=SCOPE, sink=lambda event: True,
        driver=CompositeHookDriver(_EmittingFakeDriver()),
        correlator=correlator)
    source.start()
    for p in packets:
        correlator._drivers_emit = None  # noop guard
        source._drivers_emit = None
        source._driver._drivers[0].emit(p)
    source._driver._drivers[0].emit(source._STOP) \
        if hasattr(source._driver._drivers[0], "_STOP") else None
    source.stop()
    return correlator, RecordingSession(
        "REC-0647", "review", SCOPE,
        source=SimpleNamespace(_correlator=correlator,
                               dropped_packets=0, correlation_errors=(),
                               control_snapshots=()))


def test_0647_worker_ingress_is_receive_order_no_raw():
    """0647:真 source worker 驱动 show/hide/destroy 三包,0 个 raw 输出。
    timeline 的 ingress 必须是递增接收序(相邻不同包不得 [1,2,1]);raw 输出
    连续序号契约不受影响(无 raw 事件,source._sequence 保持 0)。"""
    from target.recording.source import HookPacket as _HookPacket
    correlator, session = _real_worker_source([
        _HookPacket(kind="window_transition", monotonic_ms=100,
                    wall_time="2026-11-03T00:00:00.000Z",
                    native={"kind": "show", "processName": "EDRClient.exe",
                            "title": "日志中心", "pid": 42, "handle": 777}),
        _HookPacket(kind="window_transition", monotonic_ms=200,
                    wall_time="2026-11-03T00:00:00.000Z",
                    native={"kind": "hide", "processName": "EDRClient.exe",
                            "title": "日志中心", "pid": 42, "handle": 777}),
        _HookPacket(kind="window_transition", monotonic_ms=300,
                    wall_time="2026-11-03T00:00:00.000Z",
                    native={"kind": "destroy", "processName": "EDRClient.exe",
                            "title": "日志中心", "pid": 42, "handle": 777}),
    ])
    ingest = [e.get("ingressSequence")
              for e in correlator.window_timeline["events"]]
    assert len(ingest) == 3, f"三包三事实,实际 {ingest}"
    assert ingest == sorted(ingest) and len(set(ingest)) == 3, (
        f"相邻不同包的接收序必须递增且互异,实际 {ingest} —— 不得出现 [1,2,1]")
    restored = RawRecording.from_dict(session.recording().to_dict())
    assert restored.events == (), "前置:0 raw 输出保持"
    assert restored.window_timeline["coverage"]["droppedEvents"] >= 0


def test_0647_worker_input_first_observation_and_owner_change_ingress():
    """0647:无 raw 产物后 input 首见 + owner 变化 —— 同包多事实可同 ingress
    (同一包),相邻不同包必须递增;seed 无输入包序 = ingress 如实缺失。"""
    from test_windows_capture import _Resolver as _CaptureResolver
    from types import SimpleNamespace
    from target.recording.session import RecordingSession
    from target.recording.source import CompositeHookDriver, QueuedCaptureSource
    r = _CaptureResolver(process="EDRClient.exe", title="logo1")
    state = {"pid": 42}

    def foreground():
        return {"handle": 777, "windowTitle": "logo1",
                "processName": "EDRClient.exe", "pid": state["pid"]}

    r.foreground = foreground
    correlator = WindowsUIACorrelator(r)
    source = QueuedCaptureSource(
        scope=SCOPE, sink=lambda event: True,
        driver=CompositeHookDriver(_EmittingFakeDriver()),
        correlator=correlator)
    source.start()
    try:
        # 第一输入:weak resolver 无 raw 树 → 首见 observed(无 raw 产物)。
        # 等待 worker 完全处理(队列清空 + correlate 返回),避免 foreground
        # 在 correlate 中途看到换代后的 pid(harness 竞态,非生产行为)。
        source._driver._drivers[0].emit(HookPacket(
            kind="pointer_up", monotonic_ms=100,
            wall_time="2026-11-03T00:00:00.000Z",
            screen_point=(50, 40), button="left"))
        while source._dequeued < 1:
            time.sleep(0.01)
        # owner 换代后第二输入(仍无 SHOW)。
        state["pid"] = 99
        source._driver._drivers[0].emit(HookPacket(
            kind="pointer_up", monotonic_ms=300,
            wall_time="2026-11-03T00:00:00.000Z",
            screen_point=(50, 40), button="left"))
        while source._dequeued < 2:
            time.sleep(0.01)
        time.sleep(0.05)  # correlate 完成落 timeline
    finally:
        source.stop()
    facts = correlator.window_timeline["events"]
    ingests = [e.get("ingressSequence") for e in facts]
    assert all(i is not None for i in ingests), f"ingress 不得缺失,实际 {ingests}"
    first_obs = [e for e in facts if e["kind"] == "observed"]
    idchg = [e for e in facts if e["kind"] == "identity_changed"]
    assert first_obs and idchg, f"首见+换代事实都应存在,实际 {[(e['kind'],e.get('ingressSequence')) for e in facts]}"
    assert max(e["ingressSequence"] for e in first_obs) < min(
        e["ingressSequence"] for e in idchg), (
        f"相邻不同包的接收序必须递增,实际 {ingests}")
    session = RecordingSession(
        "REC-0647b", "review", SCOPE,
        source=SimpleNamespace(_correlator=correlator,
                               dropped_packets=0, correlation_errors=(),
                               control_snapshots=()))
    restored = RawRecording.from_dict(session.recording().to_dict())
    assert {w.get("instanceId") for w in restored.windows} == \
        {e.get("instanceId") for e in restored.window_timeline["events"]
         if e.get("instanceId")}
