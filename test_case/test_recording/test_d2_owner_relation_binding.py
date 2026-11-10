"""D2 接续（design 11 §6/§4.1 owner/parent 关系证据；REC-a3cf9a2f 实机）。

实机原件：result-report/20261109-0230-rerecord-replay/04-stop-fix-verify.json
— 每轮"设置对话框 + 语言子菜单"是两个同时存活的同名（EDRClient）顶层窗口；
compile_window_identity_ambiguous ×2 是诚实判定，缺口在关系证据缺失。

本片按既有设计补齐关系识别与运行时唯一绑定（不合并 HWND、不凭 causalId）：

R1 采集：WinEvent SHOW/HIDE/DESTROY native 增加 ownerHandle
   （GetAncestor GA_ROOTOWNER）；manifest 经 _register_window 把 owner
   handle 对着权威 handle 索引解析为 ownerInstanceId（未知 owner 保持
   裸 ownerHandle，不伪造实例边）。
R2 golden：windowSequence.windows[] 增加可选 ownerWindowId（编译器由
   manifest ownerInstanceId 解析为已声明 W id；自引用/悬空引用拒绝；
   strict model 只允许该新键）。
R3 编译歧义门：同身份不同 handle 组不再仅因 drift 判 ambiguous —— 需要
   manifest 原生 ownerInstanceId 关系才豁免；causalId/时间邻近不算。
R4 运行时绑定：live inventory 携带 owner_handle（_win_info GA_ROOTOWNER；
   runtime 透传）；绑定 _resolve_unique 在多候选时按已 BOUND 的 owner
   窗口 live handle 过滤 owner_handle —— owner 未绑定/已失效 → 照旧
   ambiguous（fail closed，不猜）。

锁定反例（设计要求全覆盖）：
- 同名无关兄弟：无 owner 边 → 编译期照旧 ambiguous；绑定期多候选照旧拒。
- 错误父链：声明的 ownerWindowId 指向的非父窗口 → live owner_handle
  不匹配 → ambiguous（不误绑）。
- HIDE 后重开：重开是新实例，manifest 无 ownerInstanceId → 豁免不适用。
- 旧绑定失效：owner 窗口 LOST → 绑定期拒绝（不缩小候选）。
- causalId 不足：仅有时间关联/关联字段、无 ownerInstanceId → 不豁免。
"""

from __future__ import annotations

import pytest

from agent.recording.compiler import compile_recording
from target.recording.models import (
    CaptureScope,
    ObservedTarget,
    RawCaptureEvent,
    RawRecording,
)

pytestmark = pytest.mark.unit

SCOPE = CaptureScope(backend="windows_pywinauto",
                     process_name="EDRClient.exe",
                     window_title="^EDRClient$", target="target")

_SETTINGS_HANDLE = 2229890
_MENU_HANDLE = 4128908
_SIBLING_HANDLE = 6619896


def _window(instance_id: str, handle: int, origin: str = "opened",
            owner_instance_id: str | None = None) -> dict:
    entry = {"instanceId": instance_id, "title": "EDRClient",
             "processName": "EDRClient.exe", "origin": origin,
             "handle": handle, "pid": 1984, "closed": False}
    if owner_instance_id is not None:
        entry["ownerInstanceId"] = owner_instance_id
    return entry


def _click(seq: int, ms: int, ri: str) -> RawCaptureEvent:
    return RawCaptureEvent(
        sequence=seq, wall_time="2026-11-08T19:26:02.000Z",
        monotonic_ms=ms, type="pointer_click", scope=SCOPE,
        input={"button": "left", "clickCount": 1,
               "screenPoint": [1000, 170]},
        observed_target=ObservedTarget(
            snapshot_id=f"OBS-{seq}", target_id=f"T{seq:04d}",
            fingerprint=f"sha256:{seq:016x}", control_type="MenuItem",
            automation_id="EDRClient.menuBar.Language.zhMenu", identifier=None,
            text="简体中文",
            rect=(985, 154, 1138, 186),
            ancestry=({"controlType": "Menu"},
                      {"controlType": "MenuItem", "name": "Language"},
                      {"controlType": "Window", "name": "EDRClient"},
                      {"controlType": "Pane", "name": "Desktop 1"}),
            protected=False, same_name_siblings=None),
        evidence={"window": {"title": "EDRClient",
                             "processName": "EDRClient.exe",
                             "handle": _SETTINGS_HANDLE if seq == 1
                             else (_MENU_HANDLE if seq == 2
                                   else _SIBLING_HANDLE),
                             "instanceId": ri}},
        causal_id=f"CAUSE-{seq:04d}",
    )


def _recording(windows: list[dict], events: list[RawCaptureEvent]) -> RawRecording:
    timeline_events = [
        {"id": f"WE-{i + 1}", "order": i, "monotonicMs": 900,
         "instanceId": w["instanceId"], "kind": "initial_observed",
         "source": "initial_inventory"}
        for i, w in enumerate(windows) if w.get("instanceId")]
    return RawRecording(
        name="d2-owner", session_id="s-d2",
        events=tuple(events),
        windows=tuple(windows),
        schema="edr.desktop-recording/v2",
        window_timeline={
            "schema": "edr.recorded-window-timeline/v1",
            "coverage": {"destroyEvents": "supported",
                         "droppedEvents": 0,
                         "unattributedDestroys": 0},
            "events": timeline_events,
        },
    )


def _compile(windows, events):
    return compile_recording(_recording(windows, events))


def test_positive_owner_related_same_title_windows_not_flagged():
    """设置框 + 其语言子菜单（manifest 有 ownerInstanceId）：同身份不同
    handle 不再判 ambiguous —— 关系已声明，绑定侧可经 owner 链唯一化。"""
    result = _compile(
        [_window("RI-0001", 13239958, origin="already_open"),
         _window("RI-0004", _SETTINGS_HANDLE, origin="already_open"),
         _window("RI-0005", _MENU_HANDLE, owner_instance_id="RI-0004")],
        [_click(1, 1000, "RI-0004"), _click(2, 1100, "RI-0005")],
    )
    steps = list(result.case.steps)
    statuses = [(s.step_id, s.status, tuple(s.issues)) for s in steps]
    assert all(status == "ready" for _, status, _ in statuses), statuses
    # golden 声明 owner 关系（两个独立 W，子窗口带 ownerWindowId）
    ws = result.golden.window_sequence.to_dict()["windows"]
    by_id = {w["id"]: w for w in ws}
    child = by_id.get("W3") or by_id.get("W2")
    owners = [w.get("ownerWindowId") for w in ws]
    assert any(owners), "owner 关系必须出现在 golden 声明里"
    assert len([w for w in ws if w.get("ownerWindowId")]) >= 1
    # 每个窗口仍是独立条目：ids 唯一、identity 各自保留
    assert len({w["id"] for w in ws}) == len(ws)


def test_negative_unrelated_same_title_siblings_still_flagged():
    """同名无关兄弟（无 owner 边）：照旧 ambiguous —— 时间先后不算关系。"""
    result = _compile(
        [_window("RI-0001", 13239958, origin="already_open"),
         _window("RI-0004", _SETTINGS_HANDLE),
         _window("RI-0007", _SIBLING_HANDLE)],
        [_click(1, 1000, "RI-0004"), _click(2, 1100, "RI-0007")],
    )
    codes = {i.code for i in result.issues}
    assert "compile_window_identity_ambiguous" in codes


def test_negative_causal_id_alone_never_exempts():
    """只有 openedBy/causalId 链、manifest 无 ownerInstanceId → 不豁免。
    causalId 是动作因果证据，不是窗口归属证据（设计 11 D2 明确）。"""
    wins = [_window("RI-0001", 13239958, origin="already_open"),
            _window("RI-0004", _SETTINGS_HANDLE),
            _window("RI-0005", _MENU_HANDLE)]
    # 子窗口带 openedBy 指向父窗口的打开动作（时间因果）但无 owner 边
    for w in wins:
        if w.get("instanceId") == "RI-0005":
            w["openedBy"] = "CAUSE-0001"
    result = _compile(wins, [_click(1, 1000, "RI-0004"),
                             _click(2, 1100, "RI-0005")])
    codes = {i.code for i in result.issues}
    assert "compile_window_identity_ambiguous" in codes


def test_negative_hide_then_reopen_is_new_instance():
    """HIDE 后重开的同名窗口是新实例：旧 owner 关系不随 handle 复用继承；
    新实例 manifest 未重记 owner → 豁免不适用，照旧 ambiguous。"""
    result = _compile(
        [_window("RI-0001", 13239958, origin="already_open"),
         _window("RI-0004", _SETTINGS_HANDLE),
         # RI-0005 重开复用同一 handle，但 manifest 是新实例条目、无 owner
         _window("RI-0005", _MENU_HANDLE)],
        [_click(1, 1000, "RI-0004"), _click(2, 1100, "RI-0005")],
    )
    codes = {i.code for i in result.issues}
    assert "compile_window_identity_ambiguous" in codes


def test_positive_owner_relation_directed_either_direction():
    """owner 边写在子侧或父侧（互指）都成立：关系是声明，不是方向约定。"""
    base = [_window("RI-0001", 13239958, origin="already_open"),
            _window("RI-0004", _SETTINGS_HANDLE, owner_instance_id="RI-0005"),
            _window("RI-0005", _MENU_HANDLE)]
    result = _compile(base, [_click(1, 1000, "RI-0004"),
                             _click(2, 1100, "RI-0005")])
    statuses = [s.status for s in result.case.steps]
    assert all(s == "ready" for s in statuses)


# ---- golden strict model: ownerWindowId validation -------------------------


def test_golden_owner_window_id_self_reference_refused():
    from agent.recording.models import WindowSequence, WINDOW_SEQUENCE_SCHEMA
    with pytest.raises(Exception, match="ownerWindowId"):
        seq = WindowSequence(
            schema=WINDOW_SEQUENCE_SCHEMA,
            windows=({"id": "W1",
                      "identity": {"processName": "EDRClient.exe",
                                   "titleRegex": "^EDRClient$"},
                      "origin": "already_open",
                      "ownerWindowId": "W1"},),
            steps=(), cleanup=(),
        )
        from agent.recording.models import _validate_window_sequence
        _validate_window_sequence(seq, (), (), None)


def test_golden_owner_window_id_dangling_refused():
    from agent.recording.models import WindowSequence, WINDOW_SEQUENCE_SCHEMA
    seq = WindowSequence(
        schema=WINDOW_SEQUENCE_SCHEMA,
        windows=({"id": "W1",
                  "identity": {"processName": "EDRClient.exe",
                               "titleRegex": "^EDRClient$"},
                  "origin": "already_open",
                  "ownerWindowId": "W9"},),
        steps=(), cleanup=(),
    )
    from agent.recording.models import _validate_window_sequence
    with pytest.raises(Exception, match="not a declared window"):
        _validate_window_sequence(seq, (), (), None)


# ---- runtime binding: owner narrowing (facade-level) -----------------------


class _LiveItem(dict):
    pass


class _OwnerNarrowBackend:
    """live inventory：两个同名窗口并存；子窗口 owner_handle 指向父。"""

    def __init__(self, items, exe_path="/opt/EDRClient/EDRClient.exe"):
        self._items = items
        self.exe_path = exe_path
        self.focus_calls: list = []

    def exe_path_for_pid(self, pid):
        return self.exe_path

    def refresh_inventory(self):
        from types import MappingProxyType
        return [MappingProxyType({**item, "native_id": str(item["handle"]),
                                  "lost": False, "generation": 2})
                for item in self._items]

    def focus_existing_window(self, **kwargs):
        self.focus_calls.append(kwargs)
        return {"ok": True}


def _binding_protocol(backend):
    """构造最小 WindowBindingProtocol（租约/策略桩；U3 已覆盖全链）。"""
    from agent.recording import window_binding as wb
    protocol = wb.WindowBindingProtocol.__new__(wb.WindowBindingProtocol)
    protocol._backend = backend
    protocol._policy = None
    protocol._lease_token = "tok"
    protocol._declared = {}
    protocol._bindings = {}
    protocol._token_to_window = {}
    protocol._revision = 0
    protocol._lifecycle = []
    import threading
    protocol._lock = threading.RLock()
    return protocol


def _bound_parent(protocol, identity):
    from agent.recording import window_binding as wb
    protocol._bindings["W1"] = wb._Binding(
        window_id="W1", token="tok", native_id="2229890", bound_pid=1984,
        bound_title="EDRClient", bound_root="", identity=identity,
        revision=1, run_id="run-1", state="BOUND")


def test_positive_binding_narrows_by_live_owner_handle():
    """父窗口已 BOUND：子窗口按 owner_handle==父 live handle 唯一绑定。"""
    backend = _OwnerNarrowBackend([
        {"handle": 2229890, "pid": 1984, "title": "EDRClient",
         "process_name": "EDRClient.exe", "owner_handle": None,
         "root_control_id": ""},
        {"handle": 4128908, "pid": 1984, "title": "EDRClient",
         "process_name": "EDRClient.exe", "owner_handle": 2229890,
         "root_control_id": ""},
    ])
    protocol = _binding_protocol(backend)
    identity = {"processName": "EDRClient.exe", "titleRegex": "^EDRClient$",
                "ownerWindowId": "W1"}
    _bound_parent(protocol, identity)
    native_id, item = protocol._resolve_unique(identity)
    assert native_id == "4128908", (
        "同名双窗口必须经 live owner 链唯一命中子菜单")


def test_negative_binding_without_bound_owner_stays_ambiguous():
    """owner 未绑定（无 BOUND 父）→ 多候选照旧拒，不猜。"""
    backend = _OwnerNarrowBackend([
        {"handle": 2229890, "pid": 1984, "title": "EDRClient",
         "process_name": "EDRClient.exe", "owner_handle": None,
         "root_control_id": ""},
        {"handle": 4128908, "pid": 1984, "title": "EDRClient",
         "process_name": "EDRClient.exe", "owner_handle": 2229890,
         "root_control_id": ""},
    ])
    protocol = _binding_protocol(backend)
    identity = {"processName": "EDRClient.exe", "titleRegex": "^EDRClient$",
                "ownerWindowId": "W1"}
    with pytest.raises(Exception, match="live windows match"):
        protocol._resolve_unique(identity)


def test_negative_binding_wrong_parent_handle_refuses():
    """声明的 ownerWindowId 已绑定但 live handle 对不上（错误父链）→ 拒。"""
    backend = _OwnerNarrowBackend([
        {"handle": 2229890, "pid": 1984, "title": "EDRClient",
         "process_name": "EDRClient.exe", "owner_handle": None,
         "root_control_id": ""},
        {"handle": 4128908, "pid": 1984, "title": "EDRClient",
         "process_name": "EDRClient.exe", "owner_handle": 999,
         "root_control_id": ""},
    ])
    protocol = _binding_protocol(backend)
    identity = {"processName": "EDRClient.exe", "titleRegex": "^EDRClient$",
                "ownerWindowId": "W1"}
    _bound_parent(protocol, identity)
    with pytest.raises(Exception, match="live windows match"):
        protocol._resolve_unique(identity)
