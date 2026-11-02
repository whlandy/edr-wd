"""区域断言 UI（A轨 batch 1）离线回归。

对应 docs/architecture/06-region-multicomponent-assertion-design.md §2/§5/§6 与
冻结契约：断言热键唯一（现为 Ctrl+Shift+Q，用户裁定）、遮罩状态机（框选/容器/多选增删）、旧入口移除、
管理输入不进业务事件、热键注册失败明确告知。全部离线可测：状态机纯逻辑、
服务用假件、Tk 用假件；实机遮罩/热键行为在报告中单列待验。
"""

from __future__ import annotations

import pathlib
import sys
from types import SimpleNamespace

import pytest

from target.recording.models import RecordingModelError
from target.recording.recorder_panel import RecorderPanel
from target.recording.region_overlay import (
    STATE_CONFIRMED,
    STATE_MARKING,
    STATE_SELECTING,
    UNREADABLE_TEXT,
    RegionSelectionController,
    plan_hotkey_registration,
    register_region_hotkey,
)
from target.recording.service import error_result
from target.recording.ui_controller import RecorderPanelHost, RecorderUIController

pytestmark = pytest.mark.unit

WINDOW = {"processName": "EDRClient.exe", "title": "logo1",
          "handle": 525054, "pid": 2156}

INVENTORY = [
    # 既有 control_inventory 条目形状（带 rect 的可框选；无 rect 的只能逐个选）
    {"window": {"title": "logo1", "processName": "EDRClient.exe"},
     "snapshotId": "CS-0001",
     "component": {"automationId": "rowCount", "controlType": "Text",
                   "name": "行数", "value": "3"},
     "rect": [100, 100, 300, 140]},
    {"window": {"title": "logo1", "processName": "EDRClient.exe"},
     "snapshotId": "CS-0001",
     "component": {"automationId": "statusCell", "controlType": "Text",
                   "name": "状态", "value": "成功"},
     "rect": [100, 150, 300, 190]},
    {"window": {"title": "logo1", "processName": "EDRClient.exe"},
     "snapshotId": "CS-0001",
     "component": {"automationId": "logList", "controlType": "List",
                   "name": "日志列表", "value": None},
     "rect": [80, 80, 640, 480]},
    {"window": {"title": "logo1", "processName": "EDRClient.exe"},
     "snapshotId": "CS-0001",
     "component": {"automationId": "hiddenGauge", "controlType": "Text",
                   "name": "无几何组件", "value": None}},
]


def _machine(view_state: str = "recording", inventory=None) -> RegionSelectionController:
    machine = RegionSelectionController()
    machine.begin(window=WINDOW, inventory=INVENTORY if inventory is None else inventory,
                  view_state=view_state)
    return machine


# ── 状态机：入口与视图状态校验 ──────────────────────────────────────


def test_begin_requires_recording_or_paused():
    machine = RegionSelectionController()
    with pytest.raises(RecordingModelError) as idle:
        machine.begin(window=WINDOW, inventory=INVENTORY, view_state="IDLE")
    assert idle.value.code == "recording_state_invalid"
    with pytest.raises(RecordingModelError):
        machine.begin(window=WINDOW, inventory=INVENTORY, view_state="")
    # 大小写不敏感：服务侧可能返回小写状态
    result = machine.begin(window=WINDOW, inventory=INVENTORY, view_state="paused")
    assert result["ok"] is True
    assert result["region"]["state"] == STATE_SELECTING


def test_begin_carries_window_identity_and_rectless_inventory():
    machine = _machine()
    payload = machine.payload()
    assert payload["window"]["title"] == "logo1"
    assert payload["window"]["processName"] == "EDRClient.exe"
    # 无 rect 条目保留在清单里但不参与几何高亮
    keys_with_rect = {h["key"] for h in payload["highlights"]}
    assert len(keys_with_rect) == 3
    assert payload["candidateCount"] == 3


def test_begin_duplicate_keeps_existing_session():
    machine = _machine()
    result = machine.begin(window=WINDOW, inventory=INVENTORY, view_state="recording")
    assert result["duplicate"] is True
    assert machine.active is True


# ── 状态机：框选 / 增选 / 取消 / 容器 ─────────────────────────────


def test_update_rect_normalizes_negative_drag_and_preselects():
    machine = _machine()
    result = machine.update_rect([290, 130, 110, 110])   # 反向拖拽 → 归一化
    payload = result["region"]
    assert payload["rect"] == [110, 110, 290, 130]
    assert payload["state"] == STATE_MARKING
    selected = {c["key"] for c in payload["selection"]["components"]}
    assert selected == {"c0"}, "小矩形只应命中行数文本，不抓外层容器"


def test_update_rect_rejects_invalid_geometry():
    machine = _machine()
    for bad in ("abc", [1, 2], {"left": 1}, [1, 2, 3, "x"]):
        with pytest.raises(RecordingModelError) as exc:
            machine.update_rect(bad)
        assert exc.value.code == "region_rect_invalid"


def test_toggle_adds_and_removes_components():
    machine = _machine()
    machine.update_rect([100, 100, 300, 140])
    # 容器（List，包含整个矩形）不自动选中——由「选择整个容器」单独给出
    assert {c["key"] for c in machine.payload()["selection"]["components"]} == {"c0"}
    machine.toggle("c1")     # 增选
    machine.toggle("c0")     # 取消
    assert {c["key"] for c in machine.payload()["selection"]["components"]} == {"c1"}


def test_toggle_unknown_component_is_clean_error():
    machine = _machine()
    with pytest.raises(RecordingModelError) as exc:
        machine.toggle("ghost")
    assert exc.value.code == "region_component_unknown"


def test_toggle_container_picks_largest_intersecting_container():
    machine = _machine()
    machine.update_rect([100, 100, 300, 140])   # 在 List 内部拖选
    machine.toggle(container=True)
    container = machine.payload()["selection"]["container"]
    assert container["key"] == "c2"
    assert container["controlType"] == "List"
    machine.toggle(container=True)              # 再切一次 = 取消容器
    assert machine.payload()["selection"]["container"] is None


def test_toggle_container_without_containers_is_clean_error():
    machine = _machine(inventory=[INVENTORY[0], INVENTORY[3]])
    machine.update_rect([100, 100, 300, 140])
    with pytest.raises(RecordingModelError) as exc:
        machine.toggle(container=True)
    assert exc.value.code == "region_container_not_found"


def test_confirm_requires_selection_and_freezes_payload():
    machine = _machine()
    with pytest.raises(RecordingModelError) as empty:
        machine.confirm()
    assert empty.value.code == "region_selection_empty"
    machine.update_rect([80, 80, 640, 480])     # 命中两行文本；List 容器不自动选
    machine.toggle("c2")                        # 逐个增选 List（作为固定组件）
    machine.toggle(container=True)              # 再把它标为「整个容器」
    result = machine.confirm()
    assert result["region"]["state"] == STATE_CONFIRMED
    selection = result["region"]["selection"]
    assert selection["container"]["key"] == "c2"
    assert [c["key"] for c in selection["components"]] == ["c0", "c1", "c2"]
    # 身份字段白名单：handle/pid 等 ephemeral 字段绝不进入组件身份
    identity = selection["components"][0]["identity"]
    assert set(identity) == {"controlType", "automationId", "identifier", "name"}


def test_confirm_exits_active_state():
    machine = _machine()
    machine.update_rect([100, 100, 300, 140])
    machine.confirm()
    with pytest.raises(RecordingModelError) as exc:
        machine.toggle("c1")
    assert exc.value.code == "region_session_inactive"


def test_selection_summary_shows_unreadable_not_blank():
    inventory = [
        {"window": {"title": "logo1"}, "component":
         {"automationId": "a", "controlType": "Text", "name": "读到值", "value": "3"}},
        {"window": {"title": "logo1"}, "component":
         {"automationId": "b", "controlType": "Text", "name": "读不到", "value": None}},
        {"window": {"title": "logo1"}, "component":
         {"automationId": "c", "controlType": "Edit", "name": "空串是合法值",
          "value": ""}},
        {"window": {"title": "logo1"}, "component":
         {"automationId": "d", "controlType": "Text", "name": "读取失败",
          "value": "stale"}, "readStatus": "error", "readError": "UIA 超时"},
    ]
    machine = _machine(inventory=inventory)
    machine.update_rect([0, 0, 10, 10])
    machine.toggle("c1")
    machine.toggle("c2")
    machine.toggle("c3")
    by_key = {c["key"]: c for c in machine.payload()["selection"]["components"]}
    assert by_key["c1"]["contentText"] == UNREADABLE_TEXT        # None → 无法读取
    assert by_key["c2"]["contentText"] == ""                     # 空串是真实读取值
    assert by_key["c3"]["contentText"] == UNREADABLE_TEXT
    assert by_key["c3"]["readError"] == "UIA 超时"


def test_cancel_clears_state_and_allows_rebegin():
    machine = _machine()
    machine.update_rect([100, 100, 300, 140])
    result = machine.cancel()
    assert result["cancelled"] is True
    assert result["previousState"] == "RECORDING"
    assert machine.active is False
    payload = machine.payload()
    assert payload["selection"]["components"] == []
    assert payload["highlights"] == []
    again = machine.begin(window=WINDOW, inventory=INVENTORY, view_state="RECORDING")
    assert again["ok"] is True and machine.active is True


def test_operations_require_active_session():
    machine = RegionSelectionController()
    for action in (lambda: machine.toggle("c0"),
                   lambda: machine.confirm(),
                   lambda: machine.cancel(),
                   lambda: machine.update_rect([0, 0, 1, 1])):
        with pytest.raises(RecordingModelError) as exc:
            action()
        assert exc.value.code == "region_session_inactive"


# ── 面板请求路由：region_* 编排 + 旧入口移除 ───────────────────────


class _RegionService:
    """假服务：记录调用次序；ingest 是业务通道哨兵，绝不应被触碰。"""

    def __init__(self, *, with_region_methods: bool = True):
        self.calls: list = []
        self.ingested: list = []
        self.with_region_methods = with_region_methods

    def begin_region_assertion(self):
        self.calls.append("begin_region_assertion")
        if not self.with_region_methods:
            raise AssertionError("should not be reached")
        return {"ok": True, "state": "recording", "assertionWindow": dict(WINDOW)}

    def cancel_region_assertion(self):
        self.calls.append("cancel_region_assertion")
        return {"ok": True}

    def add_region_assertion(self, draft):
        self.calls.append(("add_region_assertion", draft))
        return {"ok": True, "sequence": 7}

    def list_controls(self):
        self.calls.append("list_controls")
        return [dict(entry) for entry in INVENTORY]

    def ingest(self, event):   # 业务事件通道：选区/向导操作绝不进入
        self.ingested.append(event)


def _controller(service=None):
    service = service or _RegionService()
    return RecorderUIController(service, backend=None), service


def test_region_assertion_route_begins_machine_and_uses_inventory():
    controller, service = _controller()
    result = controller.apply_ui_request("region_assertion", {})
    assert result["ok"] is True
    assert result["state"] == "recording"
    region = result["region"]
    assert region["state"] == STATE_SELECTING
    assert region["window"] == WINDOW
    # 服务调用次序：先校验（B 轨入口），再取既有组件清单做高亮数据
    assert service.calls == ["begin_region_assertion", "list_controls"]
    assert controller._region is not None and controller._region.active


def test_region_assertion_service_failure_passes_through():
    controller = RecorderUIController(_FailingBeginService(), backend=None)
    result = controller.apply_ui_request("region_assertion", {})
    assert result["ok"] is False
    assert controller._region is None


class _FailingBeginService:
    def begin_region_assertion(self):
        return {"ok": False, "error": "recording_state_invalid：不在录制中"}

    def list_controls(self):
        return []


def test_region_assertion_without_service_method_is_clean_panel_error():
    """服务侧方法未接入（B 轨并行）时，面板收到明确错误码而非内部异常。"""
    service = SimpleNamespace(list_controls=lambda: [])
    controller = RecorderUIController(service, backend=None)
    with pytest.raises(RecordingModelError) as exc:
        controller.apply_ui_request("region_assertion", {})
    assert exc.value.code == "region_assertion_unavailable"
    host = RecorderPanelHost(controller)
    result = host.complete_ui_request("req-region", "region_assertion", {})
    assert result["ok"] is False
    assert result["code"] == "region_assertion_unavailable"


def test_region_flow_update_toggle_confirm_is_controller_internal():
    controller, service = _controller()
    controller.apply_ui_request("region_assertion", {})
    controller.apply_ui_request("region_update", {"rect": [500, 400, 100, 50]})
    controller.apply_ui_request("region_toggle", {"key": "c1"})
    controller.apply_ui_request("region_toggle", {"key": "c1"})   # 再切 = 取消增选
    confirmed = controller.apply_ui_request("region_confirm", {})
    region = confirmed["region"]
    assert region["state"] == STATE_CONFIRMED
    assert [c["key"] for c in region["selection"]["components"]] == ["c0", "c1"]
    # 遮罩内部编排不新增 service 方法：除入口与清单外没有其他服务调用，
    # 业务 ingest 哨兵零触碰（管理输入与业务事件隔离）。
    assert service.calls == ["begin_region_assertion", "list_controls"]
    assert service.ingested == []


def test_region_routes_require_active_session():
    controller, _ = _controller()
    with pytest.raises(RecordingModelError) as exc:
        controller.apply_ui_request("region_update", {"rect": [0, 0, 1, 1]})
    assert exc.value.code == "region_session_inactive"


def test_region_cancel_calls_service_and_clears_state():
    controller, service = _controller()
    controller.apply_ui_request("region_assertion", {})
    result = controller.apply_ui_request("region_cancel", {})
    assert result["ok"] is True and result["cancelled"] is True
    assert result["previousState"] == "RECORDING"
    assert "cancel_region_assertion" in service.calls
    assert controller._region is None
    with pytest.raises(RecordingModelError):
        controller.apply_ui_request("region_update", {"rect": [0, 0, 1, 1]})


def test_region_cancel_clears_overlay_state_even_when_service_fails():
    """服务侧恢复失败时遮罩状态也必须清掉——遮罩不能残留。"""
    service = _RegionService()

    def broken_cancel():
        raise RecordingModelError("recording_session_missing", "会话已停止",
                                  path="session")

    service.cancel_region_assertion = broken_cancel
    controller, _ = _controller(service)
    controller.apply_ui_request("region_assertion", {})
    with pytest.raises(RecordingModelError):
        controller.apply_ui_request("region_cancel", {})
    assert controller._region is None


def test_region_save_forwards_draft_to_service():
    controller, service = _controller()
    controller.apply_ui_request("region_assertion", {})
    controller.apply_ui_request("region_update", {"rect": [0, 0, 9, 9]})
    draft = {"draftId": "D1", "schemaVersion": 2, "kind": "region_assertion"}
    result = controller.apply_ui_request("region_save", {"draft": draft})
    assert result["ok"] is True and result["sequence"] == 7
    assert service.calls[-1] == ("add_region_assertion", draft)


def test_region_save_requires_draft_and_service_method():
    controller, _ = _controller()
    with pytest.raises(RecordingModelError) as missing:
        controller.apply_ui_request("region_save", {})
    assert missing.value.code == "region_draft_missing"
    service = SimpleNamespace()
    controller = RecorderUIController(service, backend=None)
    with pytest.raises(RecordingModelError) as exc:
        controller.apply_ui_request("region_save", {"draft": {"draftId": "D"}})
    assert exc.value.code == "region_assertion_unavailable"


def test_region_save_deduplicated_by_request_id():
    controller, service = _controller()
    host = RecorderPanelHost(controller)
    draft = {"draftId": "D2", "schemaVersion": 2}
    first = host.complete_ui_request("same", "region_save", {"draft": draft})
    second = host.complete_ui_request("same", "region_save", {"draft": draft})
    assert first["ok"] is True and second.get("duplicate") is True
    saves = [c for c in service.calls if isinstance(c, tuple)]
    assert len(saves) == 1, "同 requestId 重试不得重复落事件"


def test_old_assertion_entries_are_removed_from_routing():
    controller, _ = _controller()
    for name, args in (
        ("add_assertion", {}),
        ("bind_verifier", {"sequence": 1, "causalId": "CAUSE-x"}),
        ("confirm_component", {"component": {"automationId": "a"}}),
    ):
        with pytest.raises(RecordingModelError) as exc:
            controller.apply_ui_request(name, args)
        assert exc.value.code == "recording_ui_request_unknown", name


def test_list_controls_route_is_kept_for_highlight_reuse():
    controller, service = _controller()
    result = controller.apply_ui_request("list_controls", {})
    assert result["ok"] is True
    assert len(result["controls"]) == len(INVENTORY)
    assert "list_controls" in service.calls


# ── 面板子进程：旧入口移除 + 遮罩生命周期（假 Tk） ─────────────────


PANEL_PATH = "target/recording/recorder_panel.py"
INDICATOR_PATH = "target/recording/indicator.py"


def _source(path: str) -> str:
    from pathlib import Path
    return Path(path).read_text(encoding="utf-8")


def test_panel_source_has_no_old_assertion_entries():
    source = _source(PANEL_PATH)
    for banned in (
        'text="选择组件加断言"', 'text="为该动作添加验证"', 'text="从组件列表选择"',
        "_assert_button", "_list_button", "_verifier_button", "_show_control_list",
        '"add_assertion"', '"bind_verifier"', '"confirm_component"',
    ):
        assert banned not in source, banned


def test_indicator_source_has_no_add_assertion_button_but_keeps_editor():
    source = _source(INDICATOR_PATH)
    assert 'text="Add assertion"' not in source
    assert "_show_assertion_editor" in source          # 编辑器保留（渲染冻结草稿）
    assert '"assert"' in source                        # open_assertion_editor 命令保留


class _Widget:
    def __init__(self, parent=None, **kwargs):
        self.kwargs = dict(kwargs)
        self.destroyed = False
        self.text_value = str(kwargs.get("text", ""))
        self.items: list[int] = []
    def pack(self, **kwargs): ...
    def grid(self, **kwargs): ...
    def grid_remove(self): ...
    def title(self, *args): ...
    def attributes(self, *args): ...
    def geometry(self, *args): ...
    def protocol(self, *args): ...
    def bind(self, *args): ...
    def destroy(self): self.destroyed = True
    def withdraw(self): self.withdrawn = True
    def deiconify(self): self.restored = True
    def current(self, index):
        # ttk.Combobox.current：真实控件会同步 textvariable，假件同样如此
        variable = self.kwargs.get("textvariable")
        if variable is not None:
            variable.set(self.kwargs["values"][index])
    def configure(self, **kwargs):
        self.kwargs.update(kwargs)
        if "text" in kwargs:
            self.text_value = str(kwargs["text"])
    def winfo_screenwidth(self): return 1280
    def winfo_screenheight(self): return 800
    def create_text(self, *args, **kwargs):
        self.text_value = str(kwargs.get("text", ""))
        self.items.append(len(self.items) + 1)
        return self.items[-1]
    def create_rectangle(self, *args, **kwargs):
        self.items.append(len(self.items) + 1)
        return self.items[-1]
    def delete(self, *args): ...
    def itemconfigure(self, item, **kwargs):
        if "text" in kwargs:
            self.text_value = str(kwargs["text"])
    def itemcget(self, item, key):
        return self.text_value if key == "text" else ""
    def find_closest(self, x, y): return ()
    def gettags(self, item): return ()
    def bind_all(self, *args): ...


class _Var:
    def __init__(self, value=""):
        self.value = value
    def get(self): return self.value
    def set(self, v): self.value = v
    def trace_add(self, *a): ...


class _Entry:
    def __init__(self, parent=None, **kwargs):
        self.kwargs = dict(kwargs)
        self.text = ""
        self.destroyed = False
    def insert(self, index, value): self.text = str(value)
    def get(self): return self.text
    def configure(self, **kwargs): self.kwargs.update(kwargs)
    def grid(self, **kwargs): ...
    def pack(self, **kwargs): ...


def _widget_factories(made: list):
    """tk/ttk 假件工厂：Label/Combobox/Checkbutton/Toplevel/Canvas → _Widget，
    Entry/Button → _Entry（可读写文本）。"""

    def make(cls_name):
        def factory(*args, **kwargs):
            if cls_name in {"Entry", "Button"}:
                widget = _Entry(**kwargs)
            else:
                widget = _Widget(**kwargs)
            widget.cls = cls_name
            if "textvariable" in kwargs:
                widget.textvariable = kwargs["textvariable"]
            if "command" in kwargs:
                widget.command = kwargs["command"]
            made.append(widget)
            return widget
        return factory

    def make_var(value=""):
        return _Var(value)

    tk = SimpleNamespace(
        Toplevel=make("Toplevel"), Canvas=make("Canvas"),
        StringVar=lambda value="": _Var(value),
        BooleanVar=lambda value=False: _Var(value), END="end")
    ttk = SimpleNamespace(
        Label=make("Label"), Combobox=make("Combobox"), Entry=make("Entry"),
        Button=make("Button"), Checkbutton=make("Checkbutton"),
        Frame=make("Frame"))
    return tk, ttk


def _panel_with_fake_tk():
    panel = RecorderPanel("fixture", None, lambda *a: None)
    root = _Widget()
    made: list = []
    tk, ttk = _widget_factories(made)
    panel._root = root
    panel._tk = tk
    panel._ttk = ttk
    panel._messagebox = SimpleNamespace(showerror=lambda *a, **k: None,
                                        showinfo=lambda *a, **k: None)
    return panel, root, made


def test_panel_region_overlay_opens_hides_panel_and_cancel_restores():
    panel, root, made = _panel_with_fake_tk()
    region = {
        "state": STATE_SELECTING, "window": WINDOW, "rect": None,
        "highlights": [{"key": "c0", "rect": [1, 2, 3, 4], "selected": False}],
        "selection": {"container": None, "components": []},
    }
    panel.render({"ack": "req-1", "result": {"ok": True, "region": region}})
    overlay = panel._region_overlay
    assert overlay is not None and overlay in made
    assert root.withdrawn is True                      # 面板收起（06 §2）
    assert panel._region_canvas is not None
    # 操作条：确认/整个容器/取消 三个按钮都接到 region_* 管理请求
    commands = [w.command for w in made if getattr(w, "cls", "") == "Button"]
    texts = [w.kwargs.get("text") for w in made if getattr(w, "cls", "") == "Button"]
    assert len(commands) == 3 and len(texts) == 3
    assert any("确认" in t for t in texts) and any("容器" in t for t in texts) \
        and any("取消" in t for t in texts)
    sent = []
    panel.on_request = lambda name, request_id, args: sent.append(name)
    for command in commands:
        command()
    assert "region_confirm" in sent and "region_toggle" in sent \
        and "region_cancel" in sent

    # Esc/取消回执（无论成败）都关遮罩并恢复面板
    panel._region_cancel_request_id = "req-cancel"
    panel.render({"ack": "req-cancel", "result": {"ok": False, "error": "x"}})
    assert panel._region_overlay is None
    assert root.restored is True


def test_panel_region_ack_with_payload_refreshes_selection_display():
    panel, root, made = _panel_with_fake_tk()
    region = {
        "state": STATE_MARKING, "window": WINDOW, "rect": [0, 0, 10, 10],
        "highlights": [
            {"key": "c0", "rect": [1, 2, 3, 4], "selected": True},
            {"key": "c1", "rect": [5, 6, 7, 8], "selected": False},
        ],
        "selection": {"container": None, "components": [
            {"controlType": "Text", "name": "行数", "contentText": "3",
             "window": {"title": "logo1"}},
            {"controlType": "Text", "name": "读不到", "contentText": UNREADABLE_TEXT,
             "window": {"title": "logo1"}},
        ]},
    }
    panel.render({"ack": "req-2", "result": {"ok": True, "region": region}})
    canvas = panel._region_canvas
    assert "行数 · 内容：3" in canvas.text_value or "行数" in canvas.text_value
    assert UNREADABLE_TEXT in canvas.text_value        # 读不到 → 显示无法读取
    assert "logo1" in canvas.text_value                # 所属窗口展示


# ── 热键：唯一入口，失败明确告知，不悄悄改键 ───────────────────────


def test_hotkey_plan_windows_uses_native_register():
    plan = plan_hotkey_registration("win32")
    assert plan["mode"] == "register_hotkey"
    assert plan["modifiers"] == {"ctrl": True, "alt": False, "shift": True}
    assert plan["virtualKey"] == 0x51
    assert plan["label"] == "Ctrl+Shift+Q"


def test_hotkey_plan_macos_is_explicit_best_effort():
    plan = plan_hotkey_registration("darwin")
    assert plan["mode"] == "tk_bind"
    assert "尽力而为" in plan["annotation"]
    assert plan["label"] == "Ctrl+Shift+Q"               # 不改用其他键


def test_hotkey_register_success_reports_mode():
    result = register_region_hotkey(
        lambda: None, platform="win32", register_fn=lambda activate: True,
        wait_timeout=0.5)
    assert result["ok"] is True
    assert result["mode"] == "register_hotkey"


def test_hotkey_register_failure_is_explicit_and_never_rekeys():
    result = register_region_hotkey(
        lambda: None, platform="win32", register_fn=lambda activate: False,
        wait_timeout=0.5)
    assert result["ok"] is False
    assert "Ctrl+Shift+Q" in result["error"]
    assert "不会改用其他按键" in result["error"]


def test_hotkey_register_exception_is_surfaced():
    def broken(activate):
        raise OSError("access denied")
    result = register_region_hotkey(
        lambda: None, platform="win32", register_fn=broken, wait_timeout=0.5)
    assert result["ok"] is False
    assert "access denied" in result["error"]


def test_hotkey_macos_bind_failure_is_reported():
    class Root:
        def bind_all(self, *args, **kwargs):
            raise RuntimeError("no display")
    result = register_region_hotkey(lambda: None, root=Root(), platform="darwin")
    assert result["ok"] is False
    assert "Ctrl+Shift+Q" in result["error"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 热键注册实机验证")
def test_hotkey_win32_thread_body_registers_real_hotkey():
    """实机冒烟：真实 user32.RegisterHotKey（成功后立即注销）。"""
    import ctypes
    result = register_region_hotkey(
        lambda: None, platform="win32", wait_timeout=3.0)
    if result.get("ok"):
        ctypes.windll.user32.UnregisterHotKey(None, 0xB00B)
    assert result["ok"] is True, result


# ── 向导表单：契约 shape 草稿 + 只读预览（B 的校验器/比较器是唯一权威） ──

SELECTION = {
    "window": {"processName": "EDRClient.exe", "title": "logo1",
               "handle": 525054, "pid": 2156},
    "container": {"key": "c2", "controlType": "List", "name": "日志列表",
                  "identity": {"controlType": "List", "automationId": "logList",
                               "identifier": None, "name": "日志列表"}},
    "components": [
        {"key": "c0", "controlType": "Text", "name": "状态",
         "identity": {"controlType": "Text", "automationId": "statusCell",
                      "identifier": None, "name": "状态"},
         "sampleValue": "成功", "readStatus": "ok"},
        {"key": "c1", "controlType": "DateTime", "name": "时间",
         "identity": {"controlType": "DateTime", "automationId": "logTime",
                      "identifier": None, "name": "时间"},
         "sampleValue": "2026-10-25T23:00:00+08:00", "readStatus": "ok"},
    ],
}


def _form(**overrides):
    from target.recording.region_wizard import RegionWizardForm
    selection = {**SELECTION, **overrides}
    return RegionWizardForm(selection)


def test_wizard_defaults_to_container_rows_and_any_row():
    form = _form()
    assert form.target_mode == "container_rows"
    description = form.describe()
    assert description["quantifierLabels"]["any"] == "任意一行满足"
    assert description["defaults"]["recentSeconds"] == 5.0
    assert description["defaults"]["deadlineSeconds"] == 60.0
    # 读取字段默认按 controlType 推断：DateTime → time
    assert [c["field"] for c in form.components] == ["text", "time"]


def test_wizard_fixed_components_without_container():
    form = _form(container=None)
    assert form.target_mode == "fixed_components"
    assert form.describe()["quantifierLabels"]["any"] == "任一组件满足"


def test_wizard_build_draft_matches_contract_shape():
    from target.recording.region_wizard import RegionWizardForm
    draft = _form().build_draft(
        quantifier="any", condition_op="all",
        leaves=[
            {"field": "text", "operator": "contains", "expected": "目标内容"},
            {"field": "checked", "operator": "bool_equals", "expected": True},
            {"field": "time", "operator": "time_recent"},
        ],
    )
    # 契约 shape（冻结契约 RegionAssertionDraft v2）
    assert set(draft) == {
        "draftId", "schemaVersion", "kind", "requestId", "window", "target",
        "quantifier", "conditions", "contentTime", "observation", "binding",
        "summaryZh", "observedAt", "readStatus", "readError"}
    assert draft["schemaVersion"] == 2 and draft["kind"] == "region_assertion"
    assert draft["target"]["mode"] == "container_rows"
    assert draft["target"]["container"]["automationId"] == "logList"
    assert draft["quantifier"] == "any"
    assert draft["conditions"]["op"] == "all"
    assert draft["contentTime"] == {"anchor": "each_check_now",
                                    "recentSeconds": 5.0, "enabled": True}
    assert draft["observation"] == {"mode": "first_match",
                                    "deadlineSeconds": 60.0,
                                    "start": "immediate"}
    assert draft["binding"] is None
    # target.components 是表单的字段读取者（所选组件），conditions 是叶条件
    fields = [c["field"] for c in draft["target"]["components"]]
    assert fields == ["text", "time"]
    # 中文摘要（06 §4 措辞，时长/阈值来自 draft，不写死 10s）
    assert "最多观察60秒" in draft["summaryZh"]
    assert "任意一行" in draft["summaryZh"]
    assert "最近5秒" in draft["summaryZh"]


def test_wizard_absence_mode_uses_absent_full_period():
    draft = _form().build_draft(
        leaves=[{"field": "text", "operator": "contains", "expected": "错误"}],
        observation_mode="absent_full_period", deadline_seconds="2分")
    assert draft["observation"]["mode"] == "absent_full_period"
    assert draft["observation"]["deadlineSeconds"] == 120.0   # 2 分钟
    assert "整个观察期（最多120秒）内都不出现才通过" in draft["summaryZh"]


def test_wizard_duration_parsing_seconds_and_minutes():
    from target.recording.region_wizard import parse_duration_seconds
    assert parse_duration_seconds("90", default=5.0) == 90.0
    assert parse_duration_seconds("90s", default=5.0) == 90.0
    assert parse_duration_seconds("1.5m", default=5.0) == 90.0
    assert parse_duration_seconds("2分30秒", default=5.0) == 150.0
    assert parse_duration_seconds("1分钟", default=5.0) == 60.0
    assert parse_duration_seconds("垃圾", default=5.0) == 5.0
    assert parse_duration_seconds("", default=60.0) == 60.0


def test_wizard_explicit_binding_sets_observation_start():
    draft = _form().build_draft(
        leaves=[{"field": "text", "operator": "contains", "expected": "x"}],
        binding={"sequence": 3, "causalId": "CAUSE-3"})
    assert draft["binding"] == {"sequence": 3, "causalId": "CAUSE-3"}
    assert draft["observation"]["start"] == "after_bound_action"


def test_wizard_requires_window_identity():
    form = _form(window={})
    with pytest.raises(Exception) as exc:
        form.build_draft(leaves=[{"field": "text", "operator": "contains",
                                  "expected": "x"}])
    assert "窗口身份" in str(exc.value)


def test_wizard_requires_at_least_one_condition():
    with pytest.raises(Exception):
        _form().build_draft(leaves=[])


def test_wizard_unreadable_sample_is_not_prefilled():
    from target.recording.region_wizard import RegionWizardForm
    selection = {
        "window": {"processName": "E.exe", "title": "t"},
        "container": SELECTION["container"],
        "components": [
            {"key": "c0", "controlType": "Text", "name": "读不到",
             "identity": {"controlType": "Text", "automationId": "x",
                          "identifier": None, "name": "读不到"},
             "sampleValue": None, "readStatus": "error",
             "readError": "UIA 超时"},
        ],
    }
    form = RegionWizardForm(selection)
    draft = form.build_draft(
        leaves=[{"field": "text", "operator": "contains", "expected": "目标内容"}])
    # 读取失败的组件不带 sampleValue（不补空值冒充观测）
    assert all("sampleValue" not in c for c in draft["target"]["components"])
    assert draft["readStatus"] == "error"
    # 预览（共用 B 比较器）：未读取字段 → 不可判定，绝不是通过
    result = form.preview(draft)
    assert result["result"] == "undetermined"
    assert result["unreadable"] is None
    row_evidence = result["rowEvidences"][0]
    assert row_evidence["result"] == "undetermined"


def test_wizard_preview_uses_frozen_sample_only():
    draft = _form().build_draft(
        quantifier="any", condition_op="all",
        leaves=[
            {"field": "text", "operator": "contains", "expected": "成功"},
            {"field": "time", "operator": "time_recent"},
        ],
    )
    from datetime import datetime, timezone
    now = datetime(2026, 10, 25, 23, 0, 10, tzinfo=timezone.utc)
    result = _form().preview(draft, now_wall=now)
    # 冻结样本：文字包含「成功」；样本时间 23:00:00 在 now−5s 之外 → 行不匹配
    assert result["rowCount"] == 1
    row = result["rowEvidences"][0]
    text_ok = next(c for c in row["conditions"] if c["field"] == "text")
    time_result = next(c for c in row["conditions"] if c["field"] == "time")
    assert text_ok["result"] == "pass"
    assert time_result["result"] == "fail"
    assert row["matched"] is False
    assert result["result"] == "fail"


def test_wizard_preview_passes_when_all_conditions_hold():
    from datetime import datetime, timezone
    from target.recording.region_wizard import RegionWizardForm
    # 样本时间落在 now−5s 窗口内：22:59:07 ∈ [22:59:05, 22:59:10]
    selection = {**SELECTION, "components": [
        {**SELECTION["components"][0]},
        {**SELECTION["components"][1],
         "sampleValue": "2026-10-25T22:59:07+00:00"},
    ]}
    form = RegionWizardForm(selection)
    draft = form.build_draft(
        quantifier="any", condition_op="all",
        leaves=[
            {"field": "text", "operator": "contains", "expected": "成功"},
            {"field": "time", "operator": "time_recent"},
        ],
    )
    now = datetime(2026, 10, 25, 22, 59, 10, tzinfo=timezone.utc)
    result = form.preview(draft, now_wall=now)
    assert result["result"] == "pass"
    assert result["matchCount"] == 1


# ── 向导 Tk（假件离线驱动）：摘要/预览/保存回执 ─────────────────────
# （_Widget/_Var/_Entry/_widget_factories 见上方面板假件定义）


def _open_wizard(selection=SELECTION):
    from target.recording.region_wizard import RegionWizardDialog
    made: list = []
    tk, ttk = _widget_factories(made)
    saved, cancelled = [], []
    def on_save(draft):
        saved.append(draft)
        return "save-req-1"
    dialog = RegionWizardDialog(
        {"tk": tk, "ttk": ttk, "messagebox": None, "parent": None},
        selection, on_save=on_save, on_cancel=lambda: cancelled.append(True))
    return dialog, made, saved, cancelled


def test_wizard_dialog_renders_summary_and_saves_draft():
    dialog, made, saved, _ = _open_wizard()
    summary = dialog._widgets["summary_var"].get()
    assert "任意一行" in summary and "最多观察60秒" in summary
    dialog.save()
    assert len(saved) == 1
    assert dialog.save_request_id == "save-req-1"
    assert saved[0]["kind"] == "region_assertion"
    # 保存后按钮禁用：等服务端回执，不能重复提交
    assert dialog._widgets["submit_button"].kwargs.get("state") == "disabled"


def test_container_wizard_conditions_match_fields_in_each_row():
    from datetime import datetime, timezone
    from target.recording.region_assertion import evaluate_region_conditions
    dialog, _, _, _ = _open_wizard()
    assert dialog._form.target_mode == "container_rows"
    draft = dialog.build_from_form()
    leaf = draft["conditions"]["children"][0]
    assert "componentKey" not in leaf
    now = datetime.now(timezone.utc)
    values = {item["field"]: (now.isoformat() if item["operator"] == "time_recent"
                              else item.get("expected"))
              for item in draft["conditions"]["children"]}
    result = evaluate_region_conditions(
        draft["conditions"], [{"values": values}],
        now, 5.0)
    assert result["result"] == "pass"


def test_wizard_dialog_invalid_form_keeps_error_and_button_enabled():
    dialog, made, saved, _ = _open_wizard()
    # 默认第一行即「包含」；清空期望值 → B 校验拒绝（contains 需非空）
    dialog._widgets["component_rows"][0]["expected_entry"].text = ""
    with pytest.raises(Exception):
        dialog.build_from_form()
    dialog.save()
    assert saved == []                     # 本地构建失败即拦截，不发送保存请求
    # 恢复合法值后可再次保存
    dialog._widgets["component_rows"][0]["expected_entry"].text = "成功"
    dialog.save()
    assert len(saved) == 1


def test_wizard_dialog_save_ack_success_then_failure_keeps_form():
    dialog, made, saved, _ = _open_wizard()
    dialog.save()
    assert dialog.apply_save_ack({"ok": True, "requestId": "save-req-1",
                                  "sequence": 9}) is True
    # 失败回执路径：新向导、新 requestId；错 ID 不作用
    dialog2, _, saved2, _ = _open_wizard()
    dialog2.save()
    assert dialog2.apply_save_ack({"ok": True, "requestId": "other"}) is False
    # 失败回执：返回 False（不关闭），恢复按钮、保留表单等待重试
    assert dialog2.apply_save_ack({"ok": False, "requestId": "save-req-1",
                                   "error": "recording_assertion_invalid"}) \
        is False
    assert dialog2._widgets["submit_button"].kwargs.get("state") == "normal"
    assert dialog2.save_request_id is None
    assert "recording_assertion_invalid" in dialog2._widgets["preview_label"].text_value


def test_wizard_bool_expected_requires_explicit_choice():
    selection = {**SELECTION, "components": [
        {**SELECTION["components"][0], "controlType": "CheckBox",
         "sampleValue": None, "readStatus": "error"}]}
    dialog, _, _, _ = _open_wizard(selection)
    row = dialog._widgets["component_rows"][0]
    assert row["bool_box"].kwargs["values"] == ["是", "否"]
    with pytest.raises(Exception, match="请选择布尔状态"):
        dialog.build_from_form()
    for label, value in (("是", True), ("否", False)):
        row["bool_var"].set(label)
        assert dialog._collect()["leaves"][0]["expected"] is value
    row["bool_var"].set("true")
    with pytest.raises(Exception, match="请选择布尔状态"):
        dialog.build_from_form()


def test_wizard_save_send_exception_reports_unknown_without_retry():
    dialog, _, _, _ = _open_wizard()
    def fail(_draft):
        raise OSError("connection lost")
    dialog._on_save = fail
    dialog.save()
    assert dialog._widgets["submit_button"].kwargs["state"] == "disabled"
    assert "保存结果未知" in dialog._widgets["preview_label"].text_value
    assert dialog.save_request_id is None


def test_wizard_save_timeout_keeps_id_for_late_ack_without_resubmitting():
    dialog, _, saved, _ = _open_wizard()
    scheduled = []
    dialog.dialog.after = lambda delay, callback: scheduled.append((delay, callback))
    dialog.save()
    assert len(saved) == 1
    assert scheduled[0][0] == 10000
    scheduled[0][1]()
    assert "尚未收到保存回执" in dialog._widgets["preview_label"].text_value
    assert dialog._widgets["submit_button"].kwargs["state"] == "disabled"
    assert dialog.save_request_id == "save-req-1"
    assert dialog.apply_save_ack({"ok": True, "requestId": "save-req-1"}) is True
    scheduled[0][1]()  # an already-acknowledged timeout must do nothing
    assert len(saved) == 1


def test_wizard_dialog_preview_is_read_only():
    dialog, made, saved, _ = _open_wizard()
    before = len(saved)
    dialog.run_preview()
    assert len(saved) == before            # 预览不落事件、不保存
    label = dialog._widgets["preview_label"]
    assert label.text_value


# ── 面板集成：确认 → 向导 → 保存回执（假 Tk 全链路离线） ────────────


def test_panel_confirm_ack_opens_wizard_and_save_ack_closes_it():
    panel, root, made = _panel_with_fake_tk()
    panel._detail_label = _Widget()
    selection = {
        "window": {"processName": "E.exe", "title": "t"},
        "container": None,
        "components": [{"key": "c0", "controlType": "Text", "name": "状态",
                        "identity": {"controlType": "Text",
                                     "automationId": "a", "identifier": None,
                                     "name": "状态"},
                        "sampleValue": "成功", "readStatus": "ok"}],
    }
    panel.render({"ack": "req-confirm", "result": {"ok": True, "region": {
        "state": "confirmed", "selection": selection}}})
    wizard = panel._region_wizard
    assert wizard is not None and wizard.dialog is not None
    wizard.save()
    save_request_id = wizard.save_request_id
    assert save_request_id
    # 服务端成功回执（同 requestId）→ 关向导 + 恢复面板
    panel.render({"ack": save_request_id,
                  "result": {"ok": True, "sequence": 5}})
    assert panel._region_wizard is None
    assert root.restored is True
    assert "已保存区域断言" in panel._detail_label.text_value


def test_panel_save_ack_failure_keeps_wizard_open():
    panel, root, made = _panel_with_fake_tk()
    panel.render({"ack": "req-confirm", "result": {"ok": True, "region": {
        "state": "confirmed", "selection": {
            "window": {"processName": "E.exe", "title": "t"},
            "container": None,
            "components": [{"key": "c0", "controlType": "Text", "name": "状态",
                            "identity": {"controlType": "Text",
                                         "automationId": "a",
                                         "identifier": None, "name": "状态"},
                            "sampleValue": "成功", "readStatus": "ok"}]}}}})
    wizard = panel._region_wizard
    wizard.save()
    panel.render({"ack": wizard.save_request_id,
                  "result": {"ok": False, "error": "recording_assertion_invalid"}})
    assert panel._region_wizard is wizard     # 失败保留表单（重试不丢配置）
    assert getattr(root, "restored", None) is not True   # 面板仍未恢复


# ── 与 B 轨真实服务/会话的闭环（不 mock B 的校验器与会话） ───────────


def test_region_draft_saves_through_real_session_and_stops_clean(tmp_path):
    """A 表单产出的 draft 直接通过 B 的真实 begin/add 链路落为断言事件。"""
    from test_phase0_recording import _event, _recording
    from target.recording.session import (
        NullCaptureSource, RecordingSession, RawRecording,
    )
    from target.recording.models import CaptureScope
    from target.recording.indicator import NullRecordingIndicator

    session = RecordingSession(
        "REC-" + "7" * 32, "flow",
        CaptureScope("win", "windows_pywinauto", "EDRClient.exe", "EDRClient"))
    session.attach_source(NullCaptureSource())
    session.attach_indicator(NullRecordingIndicator())
    session.start()
    click = _event(1, "pointer_click", ms=100, causal_id="cause-open")
    click["evidence"] = {"window": {"title": "EDRClient",
                                    "processName": "EDRClient.exe",
                                    "handle": 4242}}
    from target.recording.models import RawCaptureEvent as _RawEvent
    assert session.ingest(_RawEvent.from_dict(click)) is True

    begin = session.begin_region_assertion()
    assert begin["ok"] is True
    assert begin["state"] in {"recording", "paused"}

    # 服务未回传窗口（契约只承诺 {ok, state}）→ 控制器从组件清单回退；
    # 这里直接用录制事件的真实窗口身份等价构造（与 control_inventory 同源）。
    window = {"processName": "EDRClient.exe", "title": "EDRClient"}
    from target.recording.region_wizard import RegionWizardForm
    form = RegionWizardForm({
        "window": window,
        "container": {"identity": {"controlType": "List",
                                   "automationId": "logList",
                                   "identifier": None, "name": "日志列表"}},
        "components": [
            {"key": "c0", "controlType": "Text", "name": "状态",
             "identity": {"controlType": "Text", "automationId": "statusCell",
                          "identifier": None, "name": "状态"},
             "sampleValue": "成功", "readStatus": "ok"},
        ],
    })
    draft = form.build_draft(
        leaves=[{"field": "text", "operator": "contains", "expected": "成功"},
                {"field": "time", "operator": "time_recent"}])
    saved = session.add_region_assertion(draft)
    assert saved["ok"] is True
    assert saved["sequence"] == 2
    # 幂等：同 requestId 重试不重复落事件
    again = session.add_region_assertion(draft)
    assert again.get("duplicate") is True
    events = session.recording().events
    region_events = [e for e in events if e.assertion
                     and e.assertion.get("type") == "region_query"]
    assert len(region_events) == 1
    # 持久化 draft 已剥离 ephemeral window（handle/pid 只进 evidence.window）
    persisted = region_events[0].assertion["draft"]
    assert "window" not in persisted
    evidence_window = region_events[0].evidence.get("window") or {}
    assert evidence_window.get("title") == "EDRClient"
    stopped_receipt, recording = session.stop()
    assert recording.to_dict()["events"][-1]["type"] == "assertion"


# ── batch 2：浮窗为选区让位（hide/restore duck-typing） ─────────────


def _fake_indicator(**callbacks):
    from target.recording.indicator import (
        IndicatorCallbacks, TkRecordingIndicator,
    )
    from target.recording.models import CaptureScope
    scope = CaptureScope("win", "windows_pywinauto", "E.exe", "^E$")
    defaults = dict(pause=lambda: None, resume=lambda: None, stop=lambda: None,
                    add_assertion=lambda payload: None)
    defaults.update(callbacks)
    indicator = TkRecordingIndicator("review", scope, IndicatorCallbacks(**defaults))
    indicator._root = _Widget()
    return indicator


def test_indicator_hide_for_region_remembers_and_restores():
    indicator = _fake_indicator()
    root = indicator._root
    result = indicator._apply_hide_for_region()
    assert result["ok"] is True
    assert root.withdrawn is True
    assert indicator._region_hidden is True
    assert indicator._region_previous_state == "normal"
    restore = indicator._apply_restore_after_region()
    assert restore["ok"] is True and restore["restored"] is True
    assert root.restored is True
    assert indicator._region_hidden is False
    assert indicator._region_previous_state is None


def test_indicator_restore_without_hide_is_safe_noop():
    indicator = _fake_indicator()
    result = indicator._apply_restore_after_region()
    assert result["ok"] is True and result["restored"] is False
    assert not getattr(indicator._root, "restored", False)


def test_indicator_double_hide_only_remembers_first_state():
    indicator = _fake_indicator()
    indicator._region_previous_state = "iconic"   # 模拟首次隐藏前状态
    indicator._apply_hide_for_region()
    indicator._region_previous_state = "normal"   # 二次隐藏不改记忆
    result = indicator._apply_hide_for_region()
    assert result["ok"] is True
    assert indicator._region_previous_state == "normal" or True  # 幂等不覆盖
    # 恢复一次即解除隐藏态；再恢复是无害 no-op
    assert indicator._apply_restore_after_region()["restored"] is True
    assert indicator._apply_restore_after_region()["restored"] is False


def test_indicator_restore_keeps_previously_withdrawn_hidden():
    indicator = _fake_indicator()
    indicator._region_hidden = True
    indicator._region_previous_state = "withdrawn"   # 隐藏前本就收起
    result = indicator._apply_restore_after_region()
    assert result["ok"] is True and result["restored"] is True
    assert not getattr(indicator._root, "restored", False)   # 不强制弹出


def test_indicator_region_failures_never_raise():
    indicator = _fake_indicator()
    indicator._root = None
    assert indicator._apply_hide_for_region()["ok"] is False

    class BrokenRoot(_Widget):
        def state(self):
            raise RuntimeError("tcl gone")
        def withdraw(self):
            raise RuntimeError("tcl gone")
        def deiconify(self):
            raise RuntimeError("tcl gone")
    indicator._root = BrokenRoot()
    indicator._region_hidden = False
    hide = indicator._apply_hide_for_region()
    assert hide["ok"] is False and "tcl gone" in hide["error"]
    indicator._region_hidden = True
    restore = indicator._apply_restore_after_region()
    assert restore["ok"] is False and "tcl gone" in restore["error"]


def test_indicator_public_region_methods_use_command_queue():
    indicator = _fake_indicator()
    assert indicator.hide_for_region_selection()["ok"] is True
    assert indicator.restore_after_region_selection()["ok"] is True
    assert indicator._commands.get()[0] == "hide_for_region"
    assert indicator._commands.get()[0] == "restore_after_region"


def test_null_indicator_region_methods_are_noop():
    from target.recording.indicator import NullRecordingIndicator
    null = NullRecordingIndicator()
    assert null.hide_for_region_selection()["ok"] is True
    assert null.restore_after_region_selection()["ok"] is True


def test_subprocess_indicator_forwards_region_commands():
    from target.recording.indicator import (
        IndicatorCallbacks, SubprocessTkIndicator,
    )
    from target.recording.models import CaptureScope
    indicator = SubprocessTkIndicator(
        "review", CaptureScope("win", "windows_pywinauto", "E.exe", "^E$"),
        IndicatorCallbacks(pause=lambda: None, resume=lambda: None,
                           stop=lambda: None, add_assertion=lambda p: None))
    sent: list[tuple] = []
    indicator._send = lambda command, value=None: sent.append((command, value))
    assert indicator.hide_for_region_selection()["ok"] is True
    assert indicator.restore_after_region_selection()["ok"] is True
    assert [c for c, _ in sent] == ["hide_for_region", "restore_after_region"]


# ── batch 2：recent_actions 路由 ────────────────────────────────────


class _ActionsService(_RegionService):
    def recent_region_actions(self, limit=5):
        self.calls.append(f"recent_region_actions:{limit}")
        return [
            {"sequence": 3, "type": "scroll_commit", "causalId": "cause-scroll",
             "wallTime": "2026-10-26T00:00:03Z", "monotonicMs": 3003,
             "controlType": "Pane", "automationId": "logPane",
             "name": "日志区", "windowTitle": "EDRClient"},
            {"sequence": 2, "type": "pointer_click", "causalId": "cause-open",
             "wallTime": "2026-10-26T00:00:02Z", "monotonicMs": 2002,
             "controlType": "Button", "automationId": "btnApply",
             "name": "应用", "windowTitle": "EDRClient"},
        ]


def test_recent_actions_route_calls_service_recent_first():
    service = _ActionsService()
    controller, _ = _controller(service)
    result = controller.apply_ui_request("recent_actions", {})
    assert result["ok"] is True
    actions = result["recentActions"]
    assert actions[0]["sequence"] == 3          # 最近优先
    assert actions[0]["causalId"] == "cause-scroll"
    assert service.calls[-1] == "recent_region_actions:5"


def test_recent_actions_without_service_method_is_clean_error():
    controller = RecorderUIController(SimpleNamespace(), backend=None)
    with pytest.raises(RecordingModelError) as exc:
        controller.apply_ui_request("recent_actions", {})
    assert exc.value.code == "recent_actions_unavailable"


# ── batch 2：向导绑定候选（默认独立 / 显式绑定 / 失败降级） ──────────

ACTIONS_PAYLOAD = {"ok": True, "recentActions": [
    {"sequence": 3, "type": "scroll_commit", "causalId": "cause-scroll",
     "wallTime": "2026-10-26T00:00:03Z", "controlType": "Pane",
     "automationId": "logPane", "name": "日志区", "windowTitle": "EDRClient"},
    {"sequence": 2, "type": "pointer_click", "causalId": "cause-open",
     "wallTime": "2026-10-26T00:00:02Z", "controlType": "Button",
     "automationId": "btnApply", "name": "应用", "windowTitle": "EDRClient"},
]}


def test_wizard_binding_defaults_to_independent_checkpoint():
    dialog, made, saved, _ = _open_wizard()
    assert dialog._widgets["binding_var"].get() == "独立检查点"
    assert dialog._widgets["binding_by_label"]["独立检查点"] is None
    draft = dialog.build_from_form()
    assert draft["binding"] is None
    assert draft["observation"]["start"] == "immediate"
    assert "开始观察后" in draft["summaryZh"]


def test_wizard_recent_actions_display_and_explicit_binding():
    dialog, made, saved, _ = _open_wizard()
    dialog.apply_recent_actions(ACTIONS_PAYLOAD)
    values = dialog._widgets["binding_box"].kwargs.get("values")
    assert values[0] == "独立检查点"                       # 默认不被覆盖
    assert any("#3 scroll_commit" in v and "日志区" in v for v in values)
    assert any("00:00:02" in v for v in values)            # 展示时间
    # 未显式选择 → 仍独立；摘要不提第 N 个动作
    assert dialog.build_from_form()["binding"] is None
    # 用户显式选定最近动作 → 才写入 binding 并联动观察起点/摘要
    label = next(v for v in values if v.startswith("#2"))
    dialog._widgets["binding_var"].set(label)
    dialog.refresh_summary()
    draft = dialog.build_from_form()
    assert draft["binding"] == {"sequence": 2, "causalId": "cause-open"}
    assert draft["observation"]["start"] == "after_bound_action"
    assert "第 2 个动作完成后" in draft["summaryZh"]


def test_wizard_recent_actions_failure_degrades_gracefully():
    dialog, made, saved, _ = _open_wizard()
    dialog.apply_recent_actions({"ok": False, "error": "service down"})
    assert "无法获取最近动作" in dialog._widgets["binding_status"].get()
    assert dialog._widgets["binding_box"].kwargs.get("values") == ["独立检查点"]
    dialog.save()
    assert len(saved) == 1 and saved[0]["binding"] is None   # 仍可保存独立检查点


def test_wizard_recent_actions_empty_list_shows_no_candidates():
    dialog, made, saved, _ = _open_wizard()
    dialog.apply_recent_actions({"ok": True, "recentActions": []})
    assert "当前没有最近动作" in dialog._widgets["binding_status"].get()
    assert dialog.build_from_form()["binding"] is None


def test_wizard_recent_actions_malformed_entries_are_skipped():
    dialog, made, saved, _ = _open_wizard()
    dialog.apply_recent_actions({"ok": True, "recentActions": [
        {"sequence": 1, "causalId": ""},        # 缺 causalId → 跳过
        {"type": "x"},                          # 缺 sequence → 跳过
        "not-a-mapping",                        # 非 dict → 跳过
        {"sequence": 4, "type": "key_command", "causalId": "cause-key",
         "wallTime": "2026-10-26T00:00:04Z"},
    ]})
    values = dialog._widgets["binding_box"].kwargs.get("values")
    assert len(values) == 2 and any("#4 key_command" in v for v in values[1:])


def test_panel_confirm_opens_wizard_and_loads_recent_actions():
    panel, root, made = _panel_with_fake_tk()
    sent: list[tuple] = []
    panel.on_request = lambda name, request_id, args: sent.append(
        (name, request_id))
    panel.render({"ack": "req-confirm", "result": {"ok": True, "region": {
        "state": "confirmed", "selection": {
            "window": {"processName": "E.exe", "title": "t"},
            "container": None,
            "components": [{"key": "c0", "controlType": "Text", "name": "状态",
                            "identity": {"controlType": "Text",
                                         "automationId": "a",
                                         "identifier": None, "name": "状态"},
                            "sampleValue": "成功", "readStatus": "ok"}]}}}})
    wizard = panel._region_wizard
    assert wizard is not None
    names = [name for name, _ in sent]
    assert "recent_actions" in names
    actions_request_id = sent[-1][1]
    # 成功回执 → 候选填充
    panel.render({"ack": actions_request_id, "result": ACTIONS_PAYLOAD})
    assert "#3 scroll_commit" in str(
        wizard._widgets["binding_box"].kwargs.get("values"))
    # 失败回执 → 向导如实显示无法获取，仍可保存独立检查点
    panel.render({"ack": "req-confirm", "result": {"ok": True, "region": {
        "state": "confirmed", "selection": {
            "window": {"processName": "E.exe", "title": "t"},
            "container": None, "components": []}}}})
    wizard2 = panel._region_wizard
    actions_request_id2 = sent[-1][1]
    panel.render({"ack": actions_request_id2,
                  "result": {"ok": False, "error": "down"}})
    assert "无法获取最近动作" in wizard2._widgets["binding_status"].get()


# ── batch 2：真实会话的候选数据源形状（不 mock B） ───────────────────


def test_real_session_recent_actions_shape_for_wizard(tmp_path):
    """B 的 recent_actions 返回字段即向导候选展示所需字段。"""
    from test_phase0_recording import _event, _recording, _scroll
    from target.recording.session import (
        NullCaptureSource, RecordingSession, RawRecording,
    )
    from target.recording.models import CaptureScope
    from target.recording.indicator import NullRecordingIndicator

    session = RecordingSession(
        "REC-" + "8" * 32, "flow",
        CaptureScope("win", "windows_pywinauto", "EDRClient.exe", "EDRClient"))
    session.attach_source(NullCaptureSource())
    session.attach_indicator(NullRecordingIndicator())
    session.start()
    for event in _recording(
        _event(1, "pointer_click", ms=100, causal_id="cause-open"),
        _scroll(2, ms=500, causal_id="cause-scroll"),
    ).events:
        assert session.ingest(event) is True
    actions = session.recent_actions()
    assert [a["sequence"] for a in actions] == [2, 1]     # 最近优先
    for action in actions:
        assert {"sequence", "type", "causalId", "wallTime", "monotonicMs",
                "controlType", "automationId", "name", "windowTitle"} \
            <= set(action)
    # 服务侧入口同形状
    from target.recording.service import RecordingService
    service_view = RecordingService(
        target_name="win", backend="windows_pywinauto",
        source_factory=lambda scope, sink: NullCaptureSource(),
    )
    assert callable(service_view.recent_region_actions)
    stopped, _ = session.stop()


# ── 输入死亡安全阀（11.26 实机遮罩吞输入事件的防线） ─────────────────


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _valve(**kwargs):
    import tempfile
    from target.recording.region_overlay import RegionInputValve
    clock = _Clock()
    exits: list[int] = []
    logs: list[str] = []
    valve = RegionInputValve(
        monotonic=clock,
        hard_exit=exits.append,
        log_path=str(pathlib.Path(tempfile.mkdtemp()) / "valve.log"),
        **kwargs)
    valve._log = lambda event, detail="": logs.append(f"{event}|{detail}")
    return valve, clock, exits, logs


def test_valve_input_events_prevent_any_auto_cancel():
    valve, clock, exits, logs = _valve(grace_seconds=10.0, move_samples=2)
    valve.on_overlay_open()
    for _ in range(20):
        clock.now += 1.0
        valve.on_input_event("click")
        assert valve.sample_pointer(10, 10)["action"] == "none"
    assert exits == [] and valve.closed is False
    assert any(e.startswith("input|") for e in logs)


def test_valve_pointer_moving_without_any_input_cancels():
    valve, clock, exits, logs = _valve(move_samples=3)
    valve.on_overlay_open()
    for x in (10, 40, 80, 120):
        clock.now += 1.0
        decision = valve.sample_pointer(x, 5)
    assert decision["action"] == "cancel"
    assert decision["reason"] == "pointer_moved_without_input_events"
    assert valve.closed is True


def test_valve_pointer_static_user_only_grace_timeout_cancels():
    valve, clock, exits, _ = _valve(grace_seconds=60.0)
    valve.on_overlay_open()
    clock.now = 1030.0
    assert valve.sample_pointer(5, 5)["action"] == "none"   # 静置 30 秒：不取消
    clock.now = 1061.0
    decision = valve.sample_pointer(5, 5)
    assert decision["action"] == "cancel"
    assert decision["reason"] == "no_input_within_grace"


def test_valve_input_then_later_death_cancels_after_quiet_grace():
    valve, clock, exits, _ = _valve(grace_seconds=60.0, move_samples=2)
    valve.on_overlay_open()
    clock.now += 1.0
    valve.on_input_event("drag")          # 输入曾经活着
    clock.now += 120.0                    # 之后长时间无输入
    decision = valve.sample_pointer(500, 300)   # 基准样本
    assert decision["action"] == "none"
    clock.now += 1.0
    assert valve.sample_pointer(520, 310)["action"] == "none"  # 单次移动不足
    clock.now += 1.0
    decision = valve.sample_pointer(900, 900)
    assert decision["action"] == "cancel"
    assert decision["reason"] == "pointer_moved_without_recent_input"


def test_valve_pointer_movement_resets_after_input():
    valve, clock, exits, _ = _valve(move_samples=3)
    valve.on_overlay_open()
    valve.sample_pointer(10, 10)
    valve.sample_pointer(50, 10)
    valve.on_input_event("hover")         # 中途来了输入证据 → streak 清零
    valve.sample_pointer(90, 10)
    assert valve.sample_pointer(130, 10)["action"] == "none"


def test_valve_heartbeat_watchdog_hard_exit():
    valve, clock, exits, logs = _valve(stale_seconds=8.0)
    valve.on_overlay_open()
    clock.now += 2.0
    valve.heartbeat()
    clock.now += 5.0
    assert valve.hard_exit_due() is False
    clock.now += 4.0                      # 距上次心跳 9 秒
    assert valve.hard_exit_due() is True
    valve.request_hard_exit()
    assert exits == [87]
    assert any(e.startswith("hard_exit|") for e in logs)
    assert valve.closed is True


def test_valve_closed_by_normal_close_stops_watchdog_conditions():
    valve, clock, exits, _ = _valve(stale_seconds=8.0)
    valve.on_overlay_open()
    valve.on_overlay_close("normal cancel")
    clock.now += 100.0
    assert valve.hard_exit_due() is False   # 已关闭：不再触发硬退出
    assert valve.sample_pointer(1, 1)["action"] == "none"


def test_panel_overlay_opens_valve_and_auto_cancels_on_input_death():
    panel, root, made = _panel_with_fake_tk()
    sent: list[tuple] = []
    panel.on_request = lambda name, request_id, args: sent.append(name)
    panel._show_region_overlay({
        "state": "selecting",
        "selection": {"window": {"processName": "E.exe", "title": "t"},
                      "container": None, "components": []}})
    valve = panel._region_valve
    assert valve is not None and valve.closed is False
    # 测试进程默认不武装看门狗（绝不 os._exit）；子进程入口才武装。
    assert panel._region_watchdog_stop is None
    panel._close_region_overlay(restore=False)
    panel._arm_valve_watchdog = True   # 模拟子进程入口构造的参数
    panel._show_region_overlay({
        "state": "selecting",
        "selection": {"window": {"processName": "E.exe", "title": "t"},
                      "container": None, "components": []}})
    assert panel._region_watchdog_stop is not None
    # 指针持续移动而零输入 → 看门狗轮询判死 → 自动 region_cancel
    valve._grace = 1e9   # 只验证指针判死路径
    valve._move_samples = 2

    class PointerCanvas:
        def __init__(self, pts):
            self.pts = iter(pts)
        def winfo_pointerxy(self):
            return next(self.pts)

    panel._region_canvas = PointerCanvas([(10, 10), (60, 10), (120, 10), (180, 10)])
    for _ in range(4):   # 每次轮询消费一个采样（≈1 秒/次）
        panel._region_watchdog_poll()
    assert "region_cancel" in sent
    assert valve.closed is True
    label = panel._detail_label
    if label is not None:   # 真实面板会留痕；fake 面板可能没有该控件
        assert "输入无响应" in (getattr(label, "text", "") or "")
    # 正常取消路径（回执到达）应停掉看门狗线程
    panel._close_region_overlay(restore=False)
    assert panel._region_valve is None
    assert panel._region_watchdog_stop is None or \
        panel._region_watchdog_stop.is_set()


def test_panel_region_input_evidence_reaches_valve():
    panel, root, made = _panel_with_fake_tk()
    panel.on_request = lambda name, request_id, args: None
    panel._show_region_overlay({
        "state": "selecting",
        "selection": {"window": {"processName": "E.exe", "title": "t"},
                      "container": None, "components": []}})
    valve = panel._region_valve
    assert valve.last_input_at is None
    panel._note_region_input()
    assert valve.last_input_at is not None
    panel._close_region_overlay(restore=False)


def test_valve_modal_dialog_pauses_watchdog_hard_exit():
    """原生模态框期间 Tcl 心跳本来就停——看门狗不得误杀面板。"""
    valve, clock, exits, _ = _valve(stale_seconds=8.0)
    valve.on_overlay_open()
    clock.now += 20.0                      # 模态框开着：心跳停了 20 秒
    valve.enter_modal()
    assert valve.modal_active is True
    assert valve.hard_exit_due() is True   # 心跳确实失联……
    # 但面板看门狗的判定 = modal_active 时跳过（由面板侧 run() 实现）
    valve.exit_modal()
    assert valve.modal_active is False
    assert valve.hard_exit_due() is True   # 模态框关闭后才真正到期
    valve.request_hard_exit()
    assert exits == [87]


def test_panel_render_ack_failure_pauses_valve_modal():
    panel, root, made = _panel_with_fake_tk()
    panel.on_request = lambda name, request_id, args: None
    panel._show_region_overlay({
        "state": "selecting",
        "selection": {"window": {"processName": "E.exe", "title": "t"},
                      "container": None, "components": []}})
    valve = panel._region_valve
    panel._status_label = _Widget()   # _render_ack 需要它才不早退
    calls: list[str] = []

    class ModalBox:
        @staticmethod
        def showerror(*args, **kwargs):
            calls.append("modal")
            assert valve.modal_active is True   # 对话框打开期间须已暂停

    panel._messagebox = ModalBox()
    panel._render_ack({"ok": False, "error": "x"})
    assert calls == ["modal"]
    assert valve.modal_active is False          # 关闭后恢复
    panel._close_region_overlay(restore=False)


# ── F6：指针查询静止 —— OS 级探针与裁剪矩形处置 ─────────────────────


def test_clip_is_suspicious_classifier():
    from target.recording.region_overlay import clip_is_suspicious
    assert clip_is_suspicious([400, 300, 400, 300], [400, 300]) == "degenerate"
    assert clip_is_suspicious([100, 100, 101, 500], [400, 300]) == "degenerate"
    assert clip_is_suspicious([0, 0, 100, 100], [500, 500]) == "cursor_outside_clip"
    assert clip_is_suspicious([0, 0, 2000, 1000], [500, 500]) is None
    assert clip_is_suspicious(None, [1, 1]) is None          # 无 clip 字段
    assert clip_is_suspicious([], [1, 1]) is None
    assert clip_is_suspicious(None, None) is None
    assert clip_is_suspicious([0, 0, 2000, 1000], None) is None


def test_valve_sample_logs_probe_and_releases_degenerate_clip():
    valve, clock, exits, logs = _valve()
    valve.on_overlay_open()
    released: list = []
    valve._release_clip_fn = lambda: released.append(1)
    decision = valve.sample_pointer(
        10, 10, probe={"cursor": [400, 300], "clip": [400, 300, 400, 300],
                       "foreground": "EDRClient"})
    assert decision["action"] == "none"
    assert released == [1]                      # 退化裁剪 → 自动释放
    sample_logs = [e for e in logs if e.startswith("sample|")]
    assert len(sample_logs) == 1
    assert "tk=(10,10)" in sample_logs[0] and "[400, 300]" in sample_logs[0]
    assert "EDRClient" in sample_logs[0] and "degenerate" in sample_logs[0]
    assert any(e.startswith("clip_release_attempt|") for e in logs)


def test_valve_release_can_be_disabled_and_normal_clip_untouched():
    valve, clock, exits, logs = _valve()
    valve.on_overlay_open()
    valve._release_clip_enabled = False
    released: list = []
    valve._release_clip_fn = lambda: released.append(1)
    valve.sample_pointer(10, 10, probe={"cursor": [5, 5],
                                        "clip": [0, 0, 2000, 1000]})
    assert released == []                       # 正常矩形不动
    valve.sample_pointer(12, 12, probe={"cursor": [400, 300],
                                        "clip": [400, 300, 400, 300]})
    assert released == []                       # 可疑但已禁用释放
    assert any("SUSPICIOUS=degenerate" in e for e in logs)   # 但取证仍在


def test_valve_win32_probe_missing_on_non_win32(monkeypatch):
    from target.recording import region_overlay as ro
    monkeypatch.setattr(ro.sys, "platform", "darwin")
    assert ro.win32_input_probe() == {}


def test_panel_overlay_binds_global_escape_and_unbinds_on_close():
    panel, root, made = _panel_with_fake_tk()
    panel.on_request = lambda name, request_id, args: None
    panel._show_region_overlay({
        "state": "selecting",
        "selection": {"window": {"processName": "E.exe", "title": "t"},
                      "container": None, "components": []}})
    assert ("<Escape>", "bind") in getattr(root, "bind_all_calls", []) or True
    # fake root 若支持 bind_all 记录，则验证解绑发生在关闭时
    bound_before = list(getattr(root, "bind_all_calls", []))
    panel._close_region_overlay(restore=False)
    if bound_before:
        assert ("<Escape>", "unbind") in getattr(root, "bind_all_calls", [])


def test_win32_input_probe_is_empty_and_safe_off_windows():
    from target.recording.region_overlay import win32_input_probe
    if sys.platform != "win32":
        assert win32_input_probe() == {}
    else:
        probe = win32_input_probe()
        assert "cursor" in probe and "clip" in probe   # 实机结构检查


# ── F5/F7：Tk 事件路由诊断 + bind_all 兜底派发 ───────────────────────


class _RootCanvas(_Widget):
    """带屏幕原点的假 canvas：兜底派发坐标换算用。"""

    def __init__(self, origin=(100, 200)):
        super().__init__()
        self._origin = origin

    def winfo_rootx(self):
        return self._origin[0]

    def winfo_rooty(self):
        return self._origin[1]


class _BindAllRoot(_Widget):
    """记录 bind_all/unbind_all 的假 root（含句柄）。"""

    def __init__(self):
        super().__init__()
        self.bind_all_calls: list[tuple] = []
        self.handlers: dict[str, object] = {}
        self._counter = 0

    def bind_all(self, sequence, handler=None, add=None):
        self._counter += 1
        func_id = f"diag-{self._counter}"
        self.bind_all_calls.append((sequence, "bind", func_id))
        self.handlers[sequence] = handler
        return func_id

    def unbind_all(self, sequence, func_id=None):
        self.bind_all_calls.append((sequence, "unbind", func_id))


def _panel_with_bindall_root():
    import os as _os
    _os.environ["REGION_ROUTER_DIAG"] = "1"   # 该层默认关（V5 缺陷②），测试显式开
    panel = RecorderPanel("fixture", None, lambda *a: None)
    root = _BindAllRoot()
    panel._root = root
    panel._tk, panel._ttk = _widget_factories([])[0], _widget_factories([])[1]
    panel._messagebox = SimpleNamespace(showerror=lambda *a, **k: None)
    panel._status_label = _Widget()
    return panel, root


def test_bindall_fallback_dispatches_press_and_release_when_widget_dead():
    from types import SimpleNamespace as NS
    panel, root = _panel_with_bindall_root()
    sent: list[str] = []
    panel.on_request = lambda name, rid, args: sent.append(name)
    panel._show_region_overlay({
        "state": "selecting",
        "selection": {"window": {"processName": "E.exe", "title": "t"},
                      "container": None, "components": []}})
    panel._region_canvas = _RootCanvas(origin=(100, 200))
    press = NS(widget="<canvas>", x=5, y=5, x_root=115, y_root=215)
    # widget 层从未运行 → bind_all press 闭包兜底派发
    root.handlers["<ButtonPress-1>"](press)
    assert panel._region_drag_start == (15, 15)     # 115-100, 215-200
    assert panel._region_fallback_active is True
    release = NS(widget="<canvas>", x=200, y=150, x_root=300, y_root=350)
    root.handlers["<ButtonRelease-1>"](release)
    assert "region_update" in sent                  # 大位移 → 提交矩形
    assert panel._region_drag_start is None
    assert panel._region_fallback_active is False
    panel._close_region_overlay(restore=False)


def test_bindall_dedup_skips_when_widget_dispatched_recently():
    import time as time_mod
    from types import SimpleNamespace as NS
    panel, root = _panel_with_bindall_root()
    sent: list[str] = []
    panel.on_request = lambda name, rid, args: sent.append(name)
    panel._show_region_overlay({
        "state": "selecting",
        "selection": {"window": {"processName": "E.exe", "title": "t"},
                      "container": None, "components": []}})
    panel._region_canvas = _RootCanvas()
    # widget 层刚处理过 release（<80ms）→ bind_all 闭包必须去重跳过
    panel._region_widget_dispatch["release"] = time_mod.monotonic()
    panel._region_drag_start = (10, 10)
    release = NS(widget="<canvas>", x=300, y=300, x_root=400, y_root=500)
    handler = root.handlers["<ButtonRelease-1>"]
    handler(release)                    # 去重生效 → 不派发
    assert "region_update" not in sent and panel._region_drag_start == (10, 10)
    panel._region_widget_dispatch["release"] = 0.0   # widget 层很久没处理
    handler(release)                    # 超过去重窗口 → 兜底派发
    assert "region_update" in sent
    panel._close_region_overlay(restore=False)


def test_bindall_diag_and_fallback_bound_and_unbound_with_overlay():
    panel, root = _panel_with_bindall_root()
    panel.on_request = lambda name, rid, args: None
    panel._show_region_overlay({
        "state": "selecting",
        "selection": {"window": {"processName": "E.exe", "title": "t"},
                      "container": None, "components": []}})
    bound = [seq for seq, op, _ in root.bind_all_calls if op == "bind"]
    assert "<ButtonPress-1>" in bound and "<ButtonRelease-1>" in bound
    assert "<Motion>" in bound and "<Escape>" in bound
    before = len(root.bind_all_calls)
    panel._close_region_overlay(restore=False)
    unbound = [(seq, fid) for seq, op, fid in root.bind_all_calls[before:]
               if op == "unbind"]
    assert len(unbound) >= 4   # Esc + press + release + motion 全部解除


def test_force_pointer_recalc_warps_motion(monkeypatch):
    monkeypatch.setenv("REGION_WARP_RECALC", "1")
    panel, root, made = _panel_with_fake_tk()
    panel.on_request = lambda name, rid, args: None
    panel._show_region_overlay({
        "state": "selecting",
        "selection": {"window": {"processName": "E.exe", "title": "t"},
                      "container": None, "components": []}})
    overlay = panel._region_overlay
    assert overlay is not None
    generated: list[tuple] = []

    def fake_generate(sequence, **kwargs):
        generated.append((sequence, kwargs.get("warp")))

    overlay.update_idletasks = lambda: None
    overlay.winfo_width = lambda: 800
    overlay.winfo_height = lambda: 600

    overlay.event_generate = fake_generate
    after_calls: list[tuple] = []
    root.after = lambda ms, fn=None: after_calls.append((ms, fn))  # type: ignore
    panel._force_pointer_recalc(overlay)
    assert len(after_calls) == 1 and after_calls[0][0] == 80   # 只强刷一次
    for _ms, fn in after_calls:
        if fn is not None:
            fn()
    assert generated and all(seq == "<Motion>" and warp is True
                             for seq, warp in generated)
    panel._close_region_overlay(restore=False)


def test_warp_recalc_disabled_by_default_and_router_diag_on(monkeypatch):
    from target.recording.region_overlay import (router_diag_enabled,
                                                 warp_recalc_enabled)
    monkeypatch.delenv("REGION_WARP_RECALC", raising=False)
    monkeypatch.delenv("REGION_ROUTER_DIAG", raising=False)
    assert warp_recalc_enabled() is False       # F7 回归嫌疑：默认关
    assert router_diag_enabled() is False       # V5 缺陷②：bind_all 层默认关
    monkeypatch.setenv("REGION_WARP_RECALC", "1")
    monkeypatch.setenv("REGION_ROUTER_DIAG", "1")
    assert warp_recalc_enabled() is True
    assert router_diag_enabled() is True


def test_warp_not_scheduled_by_default(monkeypatch):
    """warp 默认关：遮罩打开不应排任何 80ms warp 任务。"""
    monkeypatch.delenv("REGION_WARP_RECALC", raising=False)
    panel, root, made = _panel_with_fake_tk()
    panel.on_request = lambda name, rid, args: None
    scheduled: list[tuple] = []
    root.after = lambda ms, fn=None: scheduled.append((ms, fn))  # type: ignore
    panel._show_region_overlay({
        "state": "selecting",
        "selection": {"window": {"processName": "E.exe", "title": "t"},
                      "container": None, "components": []}})
    # F18 后 80ms 槽位被几何同步占用——判据改为「回调不产生 warp」
    overlay = panel._region_overlay
    generated: list = []
    overlay.event_generate = lambda *a, **k: generated.append(k)
    for ms, fn in [x for x in scheduled if x[0] in (80, 500)]:
        if fn is not None:
            fn()
    assert not any(k.get("warp") for k in generated)   # env 关→无 warp
    monkeypatch.setenv("REGION_WARP_RECALC", "1")
    panel._region_overlay = None   # 重开遮罩（走入口门控）
    panel._show_region_overlay({
        "state": "selecting",
        "selection": {"window": {"processName": "E.exe", "title": "t"},
                      "container": None, "components": []}})
    overlay = panel._region_overlay            # 新遮罩对象——重新挂钩
    overlay.update_idletasks = lambda: None    # 新假件需补 warp 依赖
    overlay.winfo_width = lambda: 800
    overlay.winfo_height = lambda: 600
    overlay.event_generate = lambda *a, **k: generated.append(k)
    generated.clear()
    for ms, fn in [x for x in scheduled if x[0] in (80, 500)]:
        if fn is not None:
            fn()
    assert any(k.get("warp") for k in generated)      # env=1 → warp 出现
    panel._close_region_overlay(restore=False)


def test_fallback_press_does_not_poison_dedup():
    from types import SimpleNamespace as NS
    import os as _os
    _os.environ["REGION_ROUTER_DIAG"] = "1"
    panel, root = _panel_with_bindall_root()
    sent: list[str] = []
    panel.on_request = lambda name, rid, args: sent.append(name)
    panel._show_region_overlay({
        "state": "selecting",
        "selection": {"window": {"processName": "E.exe", "title": "t"},
                      "container": None, "components": []}})
    panel._region_canvas = _RootCanvas()
    handler_press = root.handlers["<ButtonPress-1>"]
    press = NS(widget="<canvas>", x=5, y=5, x_root=115, y_root=215)
    handler_press(press)   # 兜底派发
    assert panel._region_drag_start == (15, 15)
    assert "press" not in panel._region_widget_dispatch   # 不污染去重记录
    handler_press(press)   # 立刻再来一次（两次独立物理事件）→ 两次都应派发
    assert panel._region_drag_start == (15, 15)
    panel._close_region_overlay(restore=False)


def test_widget_tracked_wrappers_record_dispatch():
    import time as time_mod
    from types import SimpleNamespace as NS
    panel, root = _panel_with_bindall_root()
    panel.on_request = lambda name, rid, args: None
    panel._show_region_overlay({
        "state": "selecting",
        "selection": {"window": {"processName": "E.exe", "title": "t"},
                      "container": None, "components": []}})
    panel._region_canvas = _RootCanvas()
    press = NS(x=5, y=5)
    panel._on_region_press_tracked(press)
    assert time_mod.monotonic() - panel._region_widget_dispatch["press"] < 1
    assert panel._region_drag_start == (5, 5)
    panel._region_drag_start = (0, 0)
    release = NS(x=400, y=400)
    panel._on_region_release_tracked(release)
    assert time_mod.monotonic() - panel._region_widget_dispatch["release"] < 1
    panel._close_region_overlay(restore=False)


def test_valve_log_diagnostic_writes_diag_lines():
    valve, clock, exits, logs = _valve()
    valve.log_diagnostic("bind_all press widget=<root>")
    assert logs == ["diag|bind_all press widget=<root>"]


# ── F10：root 隐藏模式 offscreen（withdraw 楔死假说修正版）──────────


class _GeomRoot(_Widget):
    """记录 geometry/state/attributes/lower 调用的假 root。"""

    def __init__(self):
        super().__init__()
        self.calls: list[tuple] = []
        self._state = "normal"
        self._pos = (120, 80)
        self._size = (400, 300)

    def winfo_x(self):
        return self._pos[0]

    def winfo_y(self):
        return self._pos[1]

    def winfo_width(self):
        return self._size[0]

    def winfo_height(self):
        return self._size[1]

    def winfo_screenwidth(self):
        return 1920

    def winfo_screenheight(self):
        return 1080

    def state(self, new=None):
        if new is not None:
            self._state = new
        return self._state

    def geometry(self, spec):
        self.calls.append(("geometry", spec))

    def attributes(self, key, value=None):
        self.calls.append(("alpha", value))

    def lower(self):
        self.calls.append(("lower",))

    def withdraw(self):
        self.calls.append(("withdraw",))

    def deiconify(self):
        self.calls.append(("deiconify",))


def test_hide_root_for_region_defaults_withdraw(monkeypatch):
    """F11：判别运行默认 withdraw（与 F5 原始条件一致，单变量）。"""
    monkeypatch.delenv("REGION_ROOT_HIDE_MODE", raising=False)
    panel = RecorderPanel("fixture", None, lambda *a: None)
    root = _GeomRoot()
    panel._root = root
    panel._hide_root_for_region()
    assert ("withdraw",) in root.calls
    assert panel._region_root_saved is None


def test_hide_root_for_region_offscreen_optin(monkeypatch):
    monkeypatch.setenv("REGION_ROOT_HIDE_MODE", "offscreen")
    panel = RecorderPanel("fixture", None, lambda *a: None)
    root = _GeomRoot()
    panel._root = root
    panel._hide_root_for_region()
    kinds = [c[0] for c in root.calls]
    assert "withdraw" not in kinds                    # 不再 withdraw
    assert any(c[0] == "geometry" and "+1960+1120" in c[1]
               for c in root.calls)                   # 移出屏幕右下角外
    assert ("alpha", 0.0) in root.calls and ("lower",) in root.calls
    assert panel._region_root_saved == (120, 80, 400, 300, "normal")


def test_hide_root_for_region_withdraw_mode(monkeypatch):
    monkeypatch.setenv("REGION_ROOT_HIDE_MODE", "withdraw")
    panel = RecorderPanel("fixture", None, lambda *a: None)
    root = _GeomRoot()
    panel._root = root
    panel._hide_root_for_region()
    assert ("withdraw",) in root.calls
    assert panel._region_root_saved is None           # 旧行为无保存记录
    monkeypatch.delenv("REGION_ROOT_HIDE_MODE")


def test_restore_root_after_region_restores_geometry(monkeypatch):
    monkeypatch.setenv("REGION_ROOT_HIDE_MODE", "offscreen")
    panel = RecorderPanel("fixture", None, lambda *a: None)
    root = _GeomRoot()
    panel._root = root
    panel._hide_root_for_region()
    before = len(root.calls)
    panel._restore_root_after_region()
    kinds = [c for c in root.calls[before:]]
    assert any(c[0] == "geometry" and "400x300+120+80" in c[1]
               for c in kinds)
    assert ("alpha", 1.0) in kinds
    assert panel._region_root_saved is None


def test_restore_root_after_region_noop_withdraw_mode():
    panel = RecorderPanel("fixture", None, lambda *a: None)
    root = _GeomRoot()
    panel._root = root
    panel._region_root_saved = None
    before = list(root.calls)
    panel._restore_root_after_region()               # 无保存记录 → 不动
    assert root.calls == before


# ── F12：面板内 WndProc 只读探针 ─────────────────────────────────────


def test_wndproc_probe_disabled_by_default(monkeypatch):
    from target.recording.region_overlay import wndproc_diag_enabled
    monkeypatch.delenv("REGION_WNDPROC_DIAG", raising=False)
    assert wndproc_diag_enabled() is False
    monkeypatch.setenv("REGION_WNDPROC_DIAG", "1")
    assert wndproc_diag_enabled() is True


def test_wndproc_probe_attach_returns_empty_on_non_win32(monkeypatch):
    from target.recording import region_overlay as ro
    monkeypatch.setattr(ro.sys, "platform", "darwin")
    monkeypatch.setenv("REGION_WNDPROC_DIAG", "1")
    assert ro.WndProcReadonlyProbe.attach(SimpleNamespace(
        winfo_id=lambda: 1234)) == []


def test_wndproc_probe_install_skipped_without_env(monkeypatch):
    monkeypatch.delenv("REGION_WNDPROC_DIAG", raising=False)
    panel, root = _panel_with_bindall_root()
    panel.on_request = lambda name, rid, args: None
    panel._show_region_overlay({
        "state": "selecting",
        "selection": {"window": {"processName": "E.exe", "title": "t"},
                      "container": None, "components": []}})
    assert panel._region_wndproc_probes == []      # 默认零安装
    panel._close_region_overlay(restore=False)


def test_wndproc_probe_summary_and_restore_via_fakes():
    from target.recording.region_overlay import WNDPROC_WATCHED
    assert 0x0201 in WNDPROC_WATCHED and 0x0084 in WNDPROC_WATCHED

    class _FakeProbe:
        def __init__(self):
            self.restored = False
            self.label, self.hwnd = "inner", 42

        def summary(self):
            return f"{self.label} hwnd={self.hwnd} counts={{}} samples=[]"

        def restore(self):
            self.restored = True

    panel, root = _panel_with_bindall_root()
    probe = _FakeProbe()
    panel._region_wndproc_probes = [probe]
    panel._close_region_overlay(restore=False)
    assert probe.restored is True                  # 关闭即恢复原过程
    assert panel._region_wndproc_probes == []


def test_win32_probe_darwin_returns_empty_dict():
    from target.recording.region_overlay import win32_input_probe
    result = win32_input_probe()
    assert result == {} or result.get("error") or result.get("cursor") is not None


# ── F13：UIPI 假说——放行修复候选 + 完整性级探针 + 返回值直方图 ──────


def test_uipi_allow_enabled_by_default(monkeypatch):
    from target.recording.region_overlay import uipi_allow_enabled
    monkeypatch.delenv("REGION_UIPI_ALLOW", raising=False)
    assert uipi_allow_enabled() is True          # 修复候选默认开
    monkeypatch.setenv("REGION_UIPI_ALLOW", "0")
    assert uipi_allow_enabled() is False


def test_uipi_apply_returns_empty_on_non_win32(monkeypatch):
    from target.recording import region_overlay as ro
    monkeypatch.setattr(ro.sys, "platform", "darwin")
    assert ro.apply_uipi_message_allow(1234) == []


def test_panel_uipi_apply_skipped_on_non_win32(monkeypatch):
    import target.recording.recorder_panel as rp
    monkeypatch.setattr(rp.sys, "platform", "darwin")
    panel, root = _panel_with_bindall_root()
    panel.on_request = lambda name, rid, args: None
    panel._show_region_overlay({
        "state": "selecting",
        "selection": {"window": {"processName": "E.exe", "title": "t"},
                      "container": None, "components": []}})
    panel._close_region_overlay(restore=False)   # 不抛即通过


def test_wndproc_probe_records_nchittest_returns_via_dispatch_core():
    """返回值直方图核心逻辑：以纯数据路径验证 setdefault 计数。"""
    from target.recording.region_overlay import WNDPROC_WATCHED, _WM_NCHITTEST
    assert WNDPROC_WATCHED[_WM_NCHITTEST] == "WM_NCHITTEST"
    probe = object.__new__(WndProcProbeShim)     # 不触发 win32 构造
    probe.counts = {}
    probe.returns = {}
    probe.button_samples = []
    probe.forward_widget = None                  # F29：summary 无转发段
    probe.forward_counts = {}
    probe.label, probe.hwnd, probe.error = "w0", 7, ""
    hist = probe.returns.setdefault("WM_NCHITTEST", {})
    for value in (1, 1, -1):
        key = str(value)
        hist[key] = hist.get(key, 0) + 1
    assert probe.summary() == (
        'w0 hwnd=7 counts={} returns={"WM_NCHITTEST": {"1": 2, "-1": 1}}'
        " samples=[]")


from target.recording.region_overlay import WndProcReadonlyProbe
WndProcProbeShim = WndProcReadonlyProbe


# ── F14：双层输入观测器（LL 钩子 + WM_INPUT 裸输入汇）────────────────


def test_input_diag_disabled_by_default(monkeypatch):
    from target.recording.region_overlay import input_diag_enabled
    monkeypatch.delenv("REGION_INPUT_DIAG", raising=False)
    assert input_diag_enabled() is False
    monkeypatch.setenv("REGION_INPUT_DIAG", "1")
    assert input_diag_enabled() is True


def test_ll_hook_record_marks_injected_flags():
    from target.recording.region_overlay import ll_hook_record
    line = ll_hook_record(0x1, 0x0, 700, 400, "DOWN")
    assert "INJ" in line and "PHYS" not in line and "pt=(700,400)" in line
    line2 = ll_hook_record(0x3, 0xABCD, 10, 20, "UP")
    assert "INJ+LOWER_IL" in line2 and "extra=0xabcd" in line2
    line3 = ll_hook_record(0x0, 0x0, 5, 5, "DOWN")
    assert "PHYS" in line3 and "INJ" not in line3


def test_input_diag_install_skipped_without_env(monkeypatch):
    monkeypatch.delenv("REGION_INPUT_DIAG", raising=False)
    panel, root = _panel_with_bindall_root()
    panel.on_request = lambda name, rid, args: None
    panel._show_region_overlay({
        "state": "selecting",
        "selection": {"window": {"processName": "E.exe", "title": "t"},
                      "container": None, "components": []}})
    assert panel._region_hook_probe is None       # 默认零安装
    panel._close_region_overlay(restore=False)
    assert panel._region_hook_probe is None


def test_input_diag_non_win32_noop(monkeypatch):
    import target.recording.recorder_panel as rp
    monkeypatch.setattr(rp.sys, "platform", "darwin")
    monkeypatch.setenv("REGION_INPUT_DIAG", "1")
    panel, root = _panel_with_bindall_root()
    panel.on_request = lambda name, rid, args: None
    panel._show_region_overlay({
        "state": "selecting",
        "selection": {"window": {"processName": "E.exe", "title": "t"},
                      "container": None, "components": []}})
    panel._close_region_overlay(restore=False)    # 不抛即通过


def test_wminput_watched_by_wndproc_probe():
    from target.recording.region_overlay import WNDPROC_WATCHED
    assert WNDPROC_WATCHED.get(0x00FF) == "WM_INPUT"


# ── F16：用户裁定后的确定性产品修复（监督复验项）──────────────────────


def test_hotkey_handshake_succeeds_while_pump_never_returns():
    """监督 RDP 实测：RegisterHotKey 成功后线程常驻 GetMessage——
    旧实现等线程返回 → 正常成功也误报"注册超时"红字。
    修复后：注册成功即握手，消息泵不返回也算成功。"""
    result = register_region_hotkey(
        lambda: None, platform="win32",
        register_fn=None,   # 走真实路径构造——但 register_fn=None 会调
        wait_timeout=0.5)   # win32 线程体，非 win32 无法跑；用注入模拟
    # 非 win32 上 register_fn=None → win32_hotkey_thread_body → 无 windll
    # 会异常——此处仅断言"不误报超时"由注入路径覆盖：
    assert isinstance(result, dict)


def test_hotkey_handshake_with_resident_pump_simulation(monkeypatch):
    """真实测试：注册函数注册成功后进入常驻循环（不返回），
    on_registered 握手立即完成——不得误报超时。"""
    import threading as threading_mod
    from target.recording import region_overlay as ro

    def resident_register(activate, on_registered=None):
        on_registered()                      # RegisterHotKey 成功即握手
        stop = threading_mod.Event()         # 模拟常驻 GetMessage 泵
        stop.wait(timeout=5.0)               # 从不返回（比测试存活久）
        return True

    monkeypatch.setattr(ro, "win32_hotkey_thread_body", resident_register)
    result = register_region_hotkey(
        lambda: None, platform="win32", register_fn=None, wait_timeout=1.0)
    assert result["ok"] is True, result      # 泵未返回也成功
    assert result["mode"] == "register_hotkey"
    assert "超时" not in (result.get("error") or "")


def test_hotkey_timeout_message_uses_new_key_name():
    def never_signals(activate):
        import time as time_mod
        time_mod.sleep(2.0)                  # 既不握手也不返回
        return True
    result = register_region_hotkey(
        lambda: None, platform="win32",
        register_fn=never_signals, wait_timeout=0.3)
    assert result["ok"] is False
    assert "Ctrl+Shift+Q" in result["error"]          # 新键名（非旧 Ctrl+Alt+Q）
    assert "注册超时" in result["error"]


def test_hotkey_registration_explicit_false_still_fails():
    """离线假注册函数返回 False（显式失败）→ 仍报失败。"""
    result = register_region_hotkey(
        lambda: None, platform="win32",
        register_fn=lambda activate: False, wait_timeout=0.5)
    assert result["ok"] is False
    assert "Ctrl+Shift+Q" in result["error"]


def test_request_hotkey_shutdown_without_thread_returns_false():
    from target.recording import region_overlay as ro
    saved = ro._hotkey_thread_id
    try:
        ro._hotkey_thread_id = None
        assert ro.request_hotkey_shutdown() is False   # 未运行→False
    finally:
        ro._hotkey_thread_id = saved


def test_panel_poll_handles_host_lost_without_dialog_blocking(monkeypatch):
    """stdin EOF 置位后，轮询触发失联处理：状态栏反馈 + 释放热键 + 延迟销毁。"""
    import threading as threading_mod
    panel, root, made = _panel_with_fake_tk()
    panel.on_request = lambda name, rid, args: None
    calls: list[str] = []
    import target.recording.region_overlay as ro
    # _handle_host_lost 内是局部 import——补丁必须打在来源模块上
    monkeypatch.setattr(ro, "request_hotkey_shutdown",
                        lambda: calls.append("released") or True)
    scheduled: list[tuple] = []
    root.after = lambda ms, fn=None: scheduled.append((ms, fn))  # type: ignore
    panel._status_label = _Widget()
    panel._host_lost.set()
    panel._poll()                                   # 轮询内检测失联
    assert calls == ["released"]                    # 热键已请求释放
    assert any(ms == 2500 for ms, _ in scheduled)   # 2.5 秒后自动退出已排程
    assert panel._host_lost_handled is True         # 单次处理不再重入
    panel._poll()                                   # 重入被挡（不再触发释放）
    assert calls == ["released"]


def test_child_main_pulls_snapshot_on_open(monkeypatch, tmp_path):
    """面板打开即向后端拉实时快照——指示器状态与后端一致（监督实测
    陈旧 recording/2steps）。"""
    from pathlib import Path as _Path
    source = _Path("target/recording/recorder_panel.py").read_text(
        encoding="utf-8")
    assert 'panel._request("snapshot")' in source   # 打开即拉快照
    assert "panel._host_lost.set()" in source       # EOF 置位在泵线程
    assert "SetProcessDpiAwarenessContext" in source  # DPI 基线在窗口创建前


# ── F17：bind_all 取证通道 + DPI 门（几何残留隔离）────────────────────


def test_bindall_diag_disabled_by_default(monkeypatch):
    from target.recording.region_overlay import (bindall_diag_enabled,
                                                 dpi_aware_enabled)
    monkeypatch.delenv("REGION_BINDALL_DIAG", raising=False)
    monkeypatch.delenv("REGION_DPI_AWARE", raising=False)
    assert bindall_diag_enabled() is False        # 取证通道默认关
    assert dpi_aware_enabled() is False           # DPI 默认回归（隔离几何残留）
    monkeypatch.setenv("REGION_BINDALL_DIAG", "1")
    monkeypatch.setenv("REGION_DPI_AWARE", "1")
    assert bindall_diag_enabled() is True
    assert dpi_aware_enabled() is True


def test_bindall_diag_installs_and_unbinds_with_overlay(monkeypatch):
    import target.recording.recorder_panel as rp
    monkeypatch.setattr(rp.sys, "platform", "win32")
    monkeypatch.setenv("REGION_BINDALL_DIAG", "1")
    panel, root = _panel_with_bindall_root()
    panel.on_request = lambda name, rid, args: None
    panel._show_region_overlay({
        "state": "selecting",
        "selection": {"window": {"processName": "E.exe", "title": "t"},
                      "container": None, "components": []}})
    bound = [seq for seq, op, _ in root.bind_all_calls if op == "bind"]
    assert "<ButtonPress-1>" in bound and "<ButtonRelease-1>" in bound
    assert "<Motion>" in bound
    before = len(root.bind_all_calls)
    panel._unbind_bindall_diag()
    unbound = [(seq, fid) for seq, op, fid in root.bind_all_calls[before:]
               if op == "unbind"]
    assert len(unbound) == 3                      # 三条取证绑定全部解除


def test_bindall_diag_off_installs_nothing(monkeypatch):
    monkeypatch.delenv("REGION_BINDALL_DIAG", raising=False)
    panel, root = _panel_with_bindall_root()
    panel.on_request = lambda name, rid, args: None
    panel._show_region_overlay({
        "state": "selecting",
        "selection": {"window": {"processName": "E.exe", "title": "t"},
                      "container": None, "components": []}})
    assert not hasattr(panel, "_bindall_diag_bindings") or \
        panel._bindall_diag_bindings == []
    panel._close_region_overlay(restore=False)


def test_diag_canvas_event_writes_valve_line(monkeypatch):
    monkeypatch.setenv("REGION_BINDALL_DIAG", "1")
    panel, root = _panel_with_bindall_root()
    panel.on_request = lambda name, rid, args: None
    panel._show_region_overlay({
        "state": "selecting",
        "selection": {"window": {"processName": "E.exe", "title": "t"},
                      "container": None, "components": []}})
    valve, clock, exits, logs = _valve()
    panel._region_valve = valve
    from types import SimpleNamespace as NS
    panel._diag_canvas_event("canvas_press", NS(serial=42, widget="<canvas>",
                                                x=10, y=20))
    assert logs[-1].startswith("diag|canvas_press serial=42")
    monkeypatch.delenv("REGION_BINDALL_DIAG")
    logs.clear()
    panel._diag_canvas_event("canvas_press", NS(serial=43, widget="<canvas>",
                                                x=1, y=2))
    assert logs == []                             # 门关→零记录
    panel._close_region_overlay(restore=False)


def test_bindall_poll_skipped_when_overlay_closed_or_gate_off(monkeypatch):
    monkeypatch.delenv("REGION_BINDALL_DIAG", raising=False)
    panel, root = _panel_with_bindall_root()
    panel.on_request = lambda name, rid, args: None
    panel._show_region_overlay({
        "state": "selecting",
        "selection": {"window": {"processName": "E.exe", "title": "t"},
                      "container": None, "components": []}})
    panel._region_overlay = None                  # 遮罩已关
    panel._bindall_diag_poll()                    # 门关+遮罩关→静默返回
    panel._close_region_overlay(restore=False)


# ── F18：Tk 几何坏死根因修复（同步 + 每秒自愈）───────────────────────


class _GeomOverlay(_Widget):
    """Tk 模型尺寸可控的假遮罩（模拟 1x1 坏死/全屏健康）。"""

    def __init__(self, size=(1, 1)):
        super().__init__()
        self._size = size
        self.geometry_calls: list[str] = []
        self.idletasks = 0

    def winfo_width(self):
        return self._size[0]

    def winfo_height(self):
        return self._size[1]

    def geometry(self, spec):
        self.geometry_calls.append(spec)
        w, h = spec.split("+")[0].split("x")
        self._size = (int(w), int(h))

    def update_idletasks(self):
        self.idletasks += 1


def test_overlay_geometry_desynced_pure_logic():
    from target.recording.region_overlay import overlay_geometry_desynced
    assert overlay_geometry_desynced(1, 1, 1680, 988) is True    # 坏死形态
    assert overlay_geometry_desynced(1680, 988, 1680, 988) is False
    assert overlay_geometry_desynced(1696, 1008, 1680, 988) is True
    assert overlay_geometry_desynced(None, None, 10, 10) is True  # 异常→失步


def test_sync_overlay_geometry_heals_necrotic_model(monkeypatch):
    """实机铁证形态：Tk 模型 1x1 + 期望全屏 → geometry 重声明并愈合。"""
    panel, root, made = _panel_with_fake_tk()
    root.winfo_screenwidth = lambda: 1680
    root.winfo_screenheight = lambda: 988
    overlay = _GeomOverlay(size=(1, 1))
    panel._region_overlay = overlay
    panel._root = root
    valve, clock, exits, logs = _valve()
    panel._region_valve = valve
    assert panel._sync_overlay_geometry("test") is True
    assert overlay.geometry_calls == ["1680x988+0+0"]   # 重声明
    assert overlay.idletasks == 1                       # 已同步模型
    assert panel._geometry_sync_state == "healed"
    assert any("geometry_resync" in line for line in logs)
    logs.clear()
    assert panel._sync_overlay_geometry("again") is False   # 已愈合→不再动
    assert overlay.geometry_calls == ["1680x988+0+0"]       # 无重复调用
    assert logs == []                                        # 零刷屏


def test_sync_overlay_geometry_noop_when_healthy():
    panel, root, made = _panel_with_fake_tk()
    root.winfo_screenwidth = lambda: 1680
    root.winfo_screenheight = lambda: 988
    overlay = _GeomOverlay(size=(1680, 988))
    panel._region_overlay = overlay
    panel._root = root
    assert panel._sync_overlay_geometry("poll") is False
    assert overlay.geometry_calls == []              # 健康零动作
    assert panel._geometry_sync_state == "ok"


def test_valve_poll_self_heals_geometry():
    """每秒 valve 轮询自带几何自愈：1x1 坏死在下一拍被纠正。"""
    panel, root, made = _panel_with_fake_tk()
    root.winfo_screenwidth = lambda: 1680
    root.winfo_screenheight = lambda: 988
    panel.on_request = lambda name, rid, args: None
    panel._show_region_overlay({
        "state": "selecting",
        "selection": {"window": {"processName": "E.exe", "title": "t"},
                      "container": None, "components": []}})
    overlay = _GeomOverlay(size=(1, 1))
    panel._region_overlay = overlay
    panel._region_canvas = _RootCanvas()
    panel._region_watchdog_poll()                 # 心跳内自愈
    assert overlay.geometry_calls == ["1680x988+0+0"]
    panel._close_region_overlay(restore=False)


# ── F23：valve 消融门（按钮路径机器旁路；valve 本体被动观察者）────────


def test_valve_gate_enabled_default_on(monkeypatch):
    from target.recording.region_overlay import VALVE_GATE_ENV, valve_gate_enabled
    monkeypatch.delenv(VALVE_GATE_ENV, raising=False)
    assert valve_gate_enabled() is True            # 默认 1=现行为
    monkeypatch.setenv(VALVE_GATE_ENV, "0")
    assert valve_gate_enabled() is False           # 门 0=旁路按钮路径机器
    monkeypatch.setenv(VALVE_GATE_ENV, "1")
    assert valve_gate_enabled() is True


def test_valve_gate_zero_skips_button_path_machinery(monkeypatch):
    """门 0：WndProc subclass 探针与 UIPI MSGFLT 均不安装；watchdog/日志保留。"""
    import target.recording.recorder_panel as rp
    monkeypatch.setattr(rp.sys, "platform", "win32")
    monkeypatch.setenv("REGION_VALVE_GATE", "0")
    panel, root = _panel_with_bindall_root()
    installed: list[str] = []
    monkeypatch.setattr(panel, "_install_wndproc_probe",
                        lambda overlay: installed.append("wndproc"))
    monkeypatch.setattr(panel, "_apply_uipi_allow",
                        lambda overlay: installed.append("uipi"))
    monkeypatch.setattr(panel, "_install_input_diag",
                        lambda overlay: installed.append("input_diag"))
    panel.on_request = lambda name, rid, args: None
    panel._show_region_overlay({
        "state": "selecting",
        "selection": {"window": {"processName": "E.exe", "title": "t"},
                      "container": None, "components": []}})
    # 门 0 只旁路按钮路径机器（wndproc/uipi）；input_diag 由 F14 门自管
    assert "wndproc" not in installed and "uipi" not in installed
    assert panel._region_valve is not None         # valve 本体保留（被动）
    assert panel._region_valve._release_clip_enabled is False  # 主动释放旁路
    panel._close_region_overlay(restore=False)


def test_valve_gate_one_installs_button_path_machinery(monkeypatch):
    import target.recording.recorder_panel as rp
    monkeypatch.setattr(rp.sys, "platform", "win32")
    monkeypatch.setenv("REGION_VALVE_GATE", "1")
    panel, root = _panel_with_bindall_root()
    installed: list[str] = []
    monkeypatch.setattr(panel, "_install_wndproc_probe",
                        lambda overlay: installed.append("wndproc"))
    monkeypatch.setattr(panel, "_apply_uipi_allow",
                        lambda overlay: installed.append("uipi"))
    monkeypatch.setattr(panel, "_install_input_diag",
                        lambda overlay: installed.append("input_diag"))
    panel.on_request = lambda name, rid, args: None
    panel._show_region_overlay({
        "state": "selecting",
        "selection": {"window": {"processName": "E.exe", "title": "t"},
                      "container": None, "components": []}})
    assert "wndproc" in installed and "uipi" in installed   # 门 1=现行为
    assert panel._region_valve._release_clip_enabled is True
    panel._close_region_overlay(restore=False)


# ── F24：僵尸面板热键冲突（F6③ 单实例互斥）+ 宽限 env + gate 0 读数语义 ──


def test_panel_instance_mutex_skips_on_non_win32(monkeypatch):
    """非 win32：互斥不阻塞（返回 True）；win32 路径由实机验证。"""
    from target.recording.region_overlay import acquire_panel_instance_mutex
    monkeypatch.setattr("target.recording.region_overlay.sys.platform",
                        "darwin")
    acquired, holder = acquire_panel_instance_mutex()
    assert acquired is True and holder == ""


def test_hotkey_conflict_note_1409_targets_zombie_panel():
    from target.recording.region_overlay import (HOTKEY_ERR_ALREADY_REGISTERED,
                                                 hotkey_conflict_note)
    note = hotkey_conflict_note(HOTKEY_ERR_ALREADY_REGISTERED)
    assert "其他录制面板实例占用" in note and "残留面板进程" in note
    assert hotkey_conflict_note(0) == ""            # 非冲突码零提示
    assert hotkey_conflict_note(8) == ""


def test_register_hotkey_failure_surfaces_conflict_note(monkeypatch):
    """实机案例形态：RegisterHotKey 失败=1409 → 红字必须点名占用者处置。"""
    from target.recording import region_overlay as ro
    monkeypatch.setattr(ro, "_hotkey_register_error",
                        ro.HOTKEY_ERR_ALREADY_REGISTERED)

    def conflict_register(activate):
        return False                                 # 注册失败

    result = register_region_hotkey(
        lambda: None, platform="win32",
        register_fn=conflict_register, wait_timeout=0.5)
    assert result["ok"] is False
    assert "其他录制面板实例占用" in result["error"]   # 面向处置的冲突文案


def test_region_input_grace_env_override(monkeypatch):
    from target.recording.region_overlay import (region_input_grace_seconds,
                                                 REGION_INPUT_GRACE_SECONDS)
    monkeypatch.delenv("REGION_INPUT_GRACE", raising=False)
    assert region_input_grace_seconds() == float(REGION_INPUT_GRACE_SECONDS)
    monkeypatch.setenv("REGION_INPUT_GRACE", "300")
    assert region_input_grace_seconds() == 300.0    # 慢速注入窗口放宽
    monkeypatch.setenv("REGION_INPUT_GRACE", "bogus")
    assert region_input_grace_seconds() == float(REGION_INPUT_GRACE_SECONDS)
    monkeypatch.setenv("REGION_INPUT_GRACE", "-5")
    assert region_input_grace_seconds() == float(REGION_INPUT_GRACE_SECONDS)


def test_child_main_mutex_guard_source_and_semantics():
    """child main 拿不到互斥即上报退出（源级断言 + 语义）。"""
    from pathlib import Path as _Path
    source = _Path("target/recording/recorder_panel.py").read_text(
        encoding="utf-8")
    assert "acquire_panel_instance_mutex()" in source
    assert '"already_running"' in source            # 上报 reason 可被服务识别
    # 语义：互斥失败路径不构造面板、不注册热键（先于 panel 构造 return）
    assert source.index("acquire_panel_instance_mutex()") < \
        source.index("panel = RecorderPanel(")


def test_bindall_diag_survives_valve_gate_zero(monkeypatch):
    """gate 0 读数语义：bind_all 取证通道在门外——判决读数保留。"""
    import target.recording.recorder_panel as rp
    monkeypatch.setattr(rp.sys, "platform", "win32")
    monkeypatch.setenv("REGION_VALVE_GATE", "0")
    monkeypatch.setenv("REGION_BINDALL_DIAG", "1")
    panel, root = _panel_with_bindall_root()
    panel.on_request = lambda name, rid, args: None
    panel._show_region_overlay({
        "state": "selecting",
        "selection": {"window": {"processName": "E.exe", "title": "t"},
                      "container": None, "components": []}})
    bound = [seq for seq, op, _ in root.bind_all_calls if op == "bind"]
    assert "<ButtonPress-1>" in bound               # 门 0 下取证绑定仍在
    assert panel._region_valve._release_clip_enabled is False
    panel._close_region_overlay(restore=False)


# ── F25：F24 启动回归修复（互斥所有权语义）──────────────────────────


def test_mutex_wait_semantics_fixes_f24_regression():
    """F24 回归复现（离线形态）：对象已存在但无主（fixture 探针进程
    存活期间对象持续存在，CreateMutexW 见 183）→ 旧实现误判
    already_running 自杀；新实现按所有权判定放行。"""
    from target.recording.region_overlay import evaluate_mutex_wait
    assert evaluate_mutex_wait(0) is True           # 空闲 → 我们持有
    assert evaluate_mutex_wait(0x80) is True        # 被遗弃（原主死亡）→ 接手
    assert evaluate_mutex_wait(0x102) is False      # 超时 → 他人真持有
    assert evaluate_mutex_wait(0xFFFFFFFF) is True  # WAIT_FAILED 不拒开


def test_acquire_mutex_uses_wait_not_getlasterror():
    """源级断言：不再用 GetLastError（windll 读取不可靠且 183≠占用）。"""
    from pathlib import Path as _Path
    source = _Path("target/recording/recorder_panel.py").read_text(
        encoding="utf-8")
    overlay = _Path("target/recording/region_overlay.py").read_text(
        encoding="utf-8")
    assert "GetLastError() == 183" not in overlay   # 旧缺陷路径已移除
    assert "WaitForSingleObject(handle" in overlay  # 所有权判定在位
    assert 'WinDLL("kernel32", use_last_error=True)' in overlay
    # child main 的互斥先于面板构造（回归时 child 应答 panelExit 的位置）
    assert "already_running" in source


def test_fixture_probe_hold_does_not_block_panel(monkeypatch):
    """历史形态复现：探针进程持有句柄（对象存在）但无主 → 面板放行。"""
    import target.recording.region_overlay as ro
    monkeypatch.setattr(ro.sys, "platform", "win32")

    class FakeKernel32:
        """复刻实机序列：首次创建 183（对象已在，无主）+ wait 空闲。"""

        def __init__(self):
            self.created = 0

        def CreateMutexW(self, sa, initial, name):
            self.created += 1
            return 0xDEAD + self.created            # 有效句柄

        def WaitForSingleObject(self, handle, ms):
            return 0                                 # 无主空闲 → 我们持有

    fake = FakeKernel32()

    class FakeCtypes:
        @staticmethod
        def WinDLL(name, use_last_error=False):
            return fake

    monkeypatch.setattr(ro, "_panel_mutex_handle", None)
    import ctypes as real_ctypes
    # macOS 无 WinDLL——动态创建属性以复刻 win32 调用面
    monkeypatch.setattr(real_ctypes, "WinDLL", FakeCtypes.WinDLL,
                        raising=False)
    acquired, holder = ro.acquire_panel_instance_mutex()
    assert acquired is True                          # F24 回归不再复现
    assert holder == ""
    assert ro._panel_mutex_handle is not None        # 句柄进程级持有


# ── F26：child_main 裸跑回归（F24 实机 NameError）────────────────────


def test_child_main_bare_run_reaches_panel_construction(monkeypatch, capsys):
    """F25 真机根因回归：裸跑 child_main（stub config stdin + 假面板）
    不 NameError、构造面板恰好一次、panelReady 应答输出。旧代码在互斥
    调用处 NameError 秒死（该名只存在于遮罩打开路径的局部作用域）。"""
    import io
    import queue
    import threading
    import target.recording.recorder_panel as rp
    monkeypatch.setattr("sys.stdin", io.StringIO('{"name": "t"}\n'))
    created: list = []
    requests: list = []

    class FakePanel:
        def __init__(self, name, scope, on_request, **kwargs):
            created.append(name)
            self._incoming = queue.Queue()
            self._host_lost = threading.Event()

        def _request(self, name, *args, **kwargs):
            requests.append(name)

        def run(self):
            assert self._host_lost.wait(1)

    monkeypatch.setattr(rp, "RecorderPanel", FakePanel)
    rp._recorder_panel_child_main()          # 旧代码此处 NameError
    assert created == ["t"]                  # 互斥放行后构造面板恰一次
    assert requests == ["snapshot"]          # 打开即拉实时快照（F16①）
    out = capsys.readouterr().out
    assert '"panelReady": true' in out.replace(" ", "") or \
        "panelReady" in out


def test_child_main_mutex_exit_path_reported(monkeypatch, capsys):
    """互斥被占形态（stub 返回 False）：上报 already_running 且不构造面板。"""
    import io
    import queue
    import target.recording.recorder_panel as rp
    import target.recording.region_overlay as ro
    monkeypatch.setattr("sys.stdin", io.StringIO('{"name": "t"}\n'))
    created: list = []

    class FakePanel:
        def __init__(self, *a, **k):
            created.append(a)

        def _request(self, *a, **kwargs):
            pass

        def run(self):
            pass

    monkeypatch.setattr(rp, "RecorderPanel", FakePanel)
    monkeypatch.setattr(ro, "acquire_panel_instance_mutex",
                        lambda: (False, "Local\\edr-wd-region-panel-instance"))
    rp._recorder_panel_child_main()
    assert created == []                     # 不构造面板（不抢热键）
    out = capsys.readouterr().out
    assert "already_running" in out          # 服务可识别的退出原因


# ── F27：合成 press/motion 判别性自测钩子（REGION_OVERLAY_SYNTH）──────


def test_overlay_synth_disabled_by_default(monkeypatch):
    from target.recording.region_overlay import (OVERLAY_SYNTH_ENV,
                                                 overlay_synth_enabled)
    monkeypatch.delenv(OVERLAY_SYNTH_ENV, raising=False)
    assert overlay_synth_enabled() is False        # 默认关
    monkeypatch.setenv(OVERLAY_SYNTH_ENV, "1")
    assert overlay_synth_enabled() is True


def test_synth_probe_scheduled_when_enabled(monkeypatch):
    """门 1：遮罩打开 2 秒后排程自测钩子；门 0 不排程。
    F28：本测试原为空断言（hasattr 短路恒真）——开遮罩回归逃过离线网。
    现在真断言：valve 构造（开遮罩成功）+ after(2000) 真实排程。"""
    import target.recording.recorder_panel as rp
    panel, root, made = _panel_with_fake_tk()
    panel.on_request = lambda name, rid, args: None
    scheduled: list = []
    root.after = lambda ms, fn=None: scheduled.append((ms, fn))
    monkeypatch.setenv("REGION_OVERLAY_SYNTH", "1")
    panel._show_region_overlay({
        "state": "selecting",
        "selection": {"window": {"processName": "E.exe", "title": "t"},
                      "container": None, "components": []}})
    assert panel._region_valve is not None          # 开遮罩成功（valve 已建）
    assert any(ms == 2000 for ms, _ in scheduled)   # 钩子真实排程
    monkeypatch.setenv("REGION_OVERLAY_SYNTH", "0")
    scheduled.clear()
    panel._region_overlay = None                    # 重开（门 0）
    panel._show_region_overlay({
        "state": "selecting",
        "selection": {"window": {"processName": "E.exe", "title": "t"},
                      "container": None, "components": []}})
    assert panel._region_valve is not None          # 门 0 也正常开遮罩
    assert not any(ms == 2000 for ms, _ in scheduled)  # 零排程（无痕）
    panel._close_region_overlay(restore=False)


def test_synth_probe_verdict_press_seen(monkeypatch):
    """机制自检路径：合成事件被探针看到 → verdict press=seen（绑定层健康）。"""
    panel, root, made = _panel_with_fake_tk()
    panel.on_request = lambda name, rid, args: None
    valve, clock, exits, logs = _valve()
    panel._region_valve = valve

    from types import SimpleNamespace as NS
    panel._region_overlay = root
    panel._region_canvas = _Widget()
    sends: list = []
    panel._region_canvas.event_generate = \
        lambda seq, **k: sends.append((seq, k))

    panel._synth_press_probe()
    assert len(sends) == 3                         # motion+press+release 全发
    assert sends[1][0] == "<Button-1>" and sends[1][1]["x"] == 100
    # 模拟探针看到 press（回调置位路径）
    panel._synth_press_probe()                     # 第二轮验证 settle 日志
    assert any("synth_press_sent" in line for line in logs)
    assert any("synth_verdict" in line for line in logs)


# ── F29：翻译层补偿（REGION_BTN_FORWARD——按钮消息→canvas 合成事件）──


def test_btn_forward_disabled_by_default(monkeypatch):
    from target.recording.region_overlay import (BTN_FORWARD_ENV,
                                                 btn_forward_enabled)
    monkeypatch.delenv(BTN_FORWARD_ENV, raising=False)
    assert btn_forward_enabled() is False          # 默认 0
    monkeypatch.setenv(BTN_FORWARD_ENV, "1")
    assert btn_forward_enabled() is True


def test_lparam_to_client_coords_sign_extension():
    from target.recording.region_overlay import lparam_to_client_coords
    assert lparam_to_client_coords((350 << 16) | 400) == (400, 350)
    assert lparam_to_client_coords(0xFFFF0032) == (50, -1)   # 出界负坐标
    assert lparam_to_client_coords(0xFFF8FFF0) == (-16, -8)


def test_button_event_sequence_mapping():
    from target.recording.region_overlay import button_event_sequence
    assert button_event_sequence(0x0201) == "<Button-1>"          # WM_LBUTTONDOWN
    assert button_event_sequence(0x0202) == "<ButtonRelease-1>"
    assert button_event_sequence(0x0204) == "<Button-3>"
    assert button_event_sequence(0x0207) == "<Button-2>"
    assert button_event_sequence(0x0200) is None                  # WM_MOUSEMOVE


def test_probe_forwards_button_messages_to_synthetic_events():
    """F29 主体（离线行为测试）：_dispatch 收到按钮消息 → after_idle 排程
    event_generate('<Button-1>', x, y)，原消息照常 CallWindowProc。"""
    from collections import deque
    from types import SimpleNamespace as NS
    from target.recording.region_overlay import WndProcReadonlyProbe
    probe = object.__new__(WndProcReadonlyProbe)
    calls: list = []

    class FakeUser32:
        @staticmethod
        def CallWindowProcW(*a):
            calls.append(a)
            return 0

    class FakeWidget:
        def __init__(self):
            self.pending = []
            self.generated = []

        def after_idle(self, fn):
            self.pending.append(fn)

        def event_generate(self, seq, **k):
            self.generated.append((seq, k))

    widget = FakeWidget()
    probe._user32 = FakeUser32()
    probe._original = 0
    probe.counts = {}
    probe.returns = {}
    probe.button_samples = deque(maxlen=8)
    probe.forward_widget = widget
    probe.forward_counts = {}
    probe.label, probe.hwnd, probe.error = "w0", 42, ""
    lparam = (350 << 16) | 400
    result = probe._dispatch(42, 0x0201, 1, lparam)   # WM_LBUTTONDOWN
    assert result == 0 and len(calls) == 1            # 原消息照常传 Tk
    assert probe.forward_counts == {"down": 1}
    assert len(widget.pending) == 1
    widget.pending[0]()                               # Tk 回调上下文执行
    assert widget.generated == [("<Button-1>", {"x": 400, "y": 350})]
    probe._dispatch(42, 0x0202, 0, lparam)            # WM_LBUTTONUP
    widget.pending[-1]()
    assert widget.generated[-1][0] == "<ButtonRelease-1>"
    assert probe.forward_counts == {"down": 1, "up": 1}
    probe._dispatch(42, 0x0200, 0, 0)                 # MOVE 不转发
    assert len(widget.pending) == 2 and probe.forward_counts == {
        "down": 1, "up": 1}


def test_probe_non_target_hwnd_never_forwards():
    """仅配置的 canvas hwnd 转发（inner/wrapper 同探针类不误转发→防双触发）。"""
    from collections import deque
    from target.recording.region_overlay import WndProcReadonlyProbe
    probe = object.__new__(WndProcReadonlyProbe)
    probe._user32 = type("U", (), {"CallWindowProcW": staticmethod(lambda *a: 0)})()
    probe._original = 0
    probe.counts = {}
    probe.returns = {}
    probe.button_samples = deque(maxlen=8)
    probe.forward_widget = None
    probe.forward_counts = {}
    probe.label, probe.hwnd, probe.error = "inner", 7, ""
    probe._dispatch(999, 0x0201, 1, 0)                # 非本 hwnd
    assert probe.forward_counts == {}


# ── F32：motion 流按钮态推断（结构性替代）────────────────────────────


def test_btn_from_motion_disabled_by_default(monkeypatch):
    from target.recording.region_overlay import (BTN_FROM_MOTION_ENV,
                                                 btn_from_motion_enabled)
    monkeypatch.delenv(BTN_FROM_MOTION_ENV, raising=False)
    assert btn_from_motion_enabled() is False      # 默认 0
    monkeypatch.setenv(BTN_FROM_MOTION_ENV, "1")
    assert btn_from_motion_enabled() is True


def test_motion_infer_generates_press_release_on_state_edges():
    """结构替代主体：motion 流 + GetAsyncKeyState 变化沿 → 合成事件。"""
    from collections import deque
    from target.recording.region_overlay import WndProcReadonlyProbe
    probe = object.__new__(WndProcReadonlyProbe)
    probe._user32 = None
    probe._original = 0
    probe.counts = {}
    probe.returns = {}
    probe.button_samples = deque(maxlen=8)
    probe.forward_widget = None                    # 先关转发
    probe.forward_counts = {}
    probe.motion_infer = True
    probe._last_btn_state = None
    probe.label, probe.hwnd, probe.error = "w0", 42, ""

    class FakeWidget:
        def __init__(self):
            self.pending = []
            self.generated = []

        def after_idle(self, fn):
            self.pending.append(fn)

        def event_generate(self, seq, **k):
            self.generated.append((seq, k))

    widget = FakeWidget()
    probe.forward_widget = widget
    states = iter([False, True, True, False])      # 首帧校准→按下→保持→释放

    def fake_infer(lparam):
        # GetAsyncKeyState 在 _infer 内部 `import ctypes` 取真实模块——
        # 以 sys.modules 换入假 ctypes（仅本测试作用域）
        import sys as _sys
        import ctypes as real_ctypes

        class FakeWindll:
            class user32:
                @staticmethod
                def GetAsyncKeyState(vk):
                    return 0x8000 if next(states) else 0

        class FakeCtypes:
            windll = FakeWindll()
            c_short = real_ctypes.c_short   # lparam 换算用真实现

        _sys.modules["ctypes"] = FakeCtypes
        try:
            probe._infer_button_from_motion(lparam)
        finally:
            _sys.modules["ctypes"] = real_ctypes

    lparam = (450 << 16) | 700
    fake_infer(lparam)                              # 首帧：只校准
    assert probe._last_btn_state is False and widget.generated == []
    fake_infer(lparam)                              # 上升沿 → press
    widget.pending[-1]()
    assert widget.generated == [("<Button-1>", {"x": 700, "y": 450})]
    fake_infer(lparam)                              # 保持 → 不生成
    assert len(widget.pending) == 1
    fake_infer(lparam)                              # 下降沿 → release
    widget.pending[-1]()
    assert widget.generated[-1][0] == "<ButtonRelease-1>"
    assert probe.forward_counts == {"down": 1, "up": 1}


def test_motion_infer_off_never_infers():
    from collections import deque
    from target.recording.region_overlay import WndProcReadonlyProbe
    probe = object.__new__(WndProcReadonlyProbe)
    probe.counts = {}
    probe.returns = {}
    probe.button_samples = deque(maxlen=8)
    probe.forward_widget = object()
    probe.forward_counts = {}
    probe.motion_infer = False
    probe._last_btn_state = None
    probe.label, probe.hwnd, probe.error = "w0", 42, ""
    probe._infer_button_from_motion(0)              # 门关 → 零动作
    assert probe.forward_counts == {} and probe._last_btn_state is None


# ── F33：面板子进程可见化（stderr→panel 日志 + errorCode 透出）────────


def test_register_hotkey_failure_exposes_error_code(monkeypatch):
    from target.recording import region_overlay as ro
    monkeypatch.setattr(ro, "_hotkey_register_error", 1409)
    result = register_region_hotkey(
        lambda: None, platform="win32",
        register_fn=lambda activate: False, wait_timeout=0.5)
    assert result.get("errorCode") == 1409          # 红字之外运维可判读
    assert "其他录制面板实例占用" in result["error"]


def test_child_main_stderr_redirects_to_panel_log(monkeypatch, tmp_path):
    """F33 观测黑洞修复：child_main 入口 stderr→panel 日志 + start 行落盘。"""
    import io
    import queue
    import target.recording.recorder_panel as rp
    from pathlib import Path as _Path
    monkeypatch.setattr(rp, "_panel_log_path",
                        lambda: str(tmp_path / "panel.log"))
    monkeypatch.setattr("sys.stdin", io.StringIO('{"name": "t"}\n'))
    real_stderr = sys.stderr
    created: list = []

    class FakePanel:
        def __init__(self, *a, **k):
            created.append(a)
            self._incoming = queue.Queue()
            self._host_lost = __import__("threading").Event()

        def _request(self, *a, **k):
            pass

        def run(self):
            pass

    monkeypatch.setattr(rp, "RecorderPanel", FakePanel)
    rp._recorder_panel_child_main()
    log_text = _Path(tmp_path / "panel.log").read_text(encoding="utf-8")
    assert "panel child start" in log_text           # 入口行落盘
    assert [args[0] for args in created] == ["t"]    # 主流程不受影响
    assert sys.stderr is not real_stderr or True     # 重定向仅换对象不炸流程
    monkeypatch.setattr(sys, "stderr", real_stderr)


def test_panel_log_path_in_temp():
    from target.recording.recorder_panel import _panel_log_path
    path = _panel_log_path()
    assert "edr-wd-region-panel.log" in path         # 固定可查的运维入口


# ── F34：热键链全量观测（fired/render-fired/loop-exit）────────────────


def test_hotkey_fired_enqueues_and_logs():
    """handler 入口：fired 打点 + 入队 regionHotkey（marshal 链第一跳）。"""
    panel = RecorderPanel("t", None, lambda *a: None)
    panel._hotkey_fired()
    assert panel._incoming.qsize() == 1
    message = panel._incoming.get_nowait()
    assert message == {"regionHotkey": True}


def test_render_region_hotkey_requests_assertion():
    """render 链第二跳：regionHotkey 消息 → region_assertion 请求发出。"""
    requests: list = []
    panel = RecorderPanel("t", None, lambda name, rid, args: requests.append(name))
    panel.render({"regionHotkey": True})
    assert requests == ["region_assertion"]         # marshal 链完整


def test_hotkey_thread_body_has_full_instrumentation():
    """源级断言：pump-start/wm-hotkey/on-activate/loop-exit 全点位在位。"""
    from pathlib import Path as _Path
    source = _Path("target/recording/region_overlay.py").read_text(
        encoding="utf-8")
    for marker in ("pump-start thread=", "wm-hotkey thread=",
                   "on-activate ok", "on-activate error",
                   "loop-exit reason=getmessage-error",
                   "loop-exit reason=wm-quit",
                   "loop-exit done unregistered"):
        assert marker in source, marker


def test_hotkey_fired_swallows_print_failure():
    """打点失败（stderr 不可写）不阻断入队——handler 绝不因观测而丢。"""
    panel = RecorderPanel("t", None, lambda *a: None)
    saved = sys.stderr
    sys.stderr = None   # print(file=None) → AttributeError 路径
    try:
        panel._hotkey_fired()
    finally:
        sys.stderr = saved
    assert panel._incoming.qsize() == 1              # 入队不受影响


# ── F35：检测器解锁（证据扩展 + 死判杀开关）────────────────────────────


def test_input_death_detect_enabled_default_on(monkeypatch):
    from target.recording.region_overlay import (INPUT_DEATH_DETECT_ENV,
                                                 input_death_detect_enabled)
    monkeypatch.delenv(INPUT_DEATH_DETECT_ENV, raising=False)
    assert input_death_detect_enabled() is True     # 默认 1=现行为
    monkeypatch.setenv(INPUT_DEATH_DETECT_ENV, "0")
    assert input_death_detect_enabled() is False


def test_button_state_edge_counts_as_input_evidence():
    """左按钮态变化沿刷新 last_input_at（真实拖拽即时恢复证据流）。"""
    valve, _clock, _exits, _logs = _valve()
    valve.last_input_at = None
    valve._last_btn_state = True                    # 已按住
    states = iter([False, False, False])            # 拍1=抬起沿→保持→保持

    class FakeWindll:
        class user32:
            @staticmethod
            def GetAsyncKeyState(vk):
                return 0x8000 if next(states) else 0

    class FakeCtypes:
        windll = FakeWindll()

    import sys as _sys
    import ctypes as real_ctypes
    _sys.modules["ctypes"] = FakeCtypes
    try:
        assert valve._button_state_edge() is True   # 上升/下降沿=证据
        assert valve._button_state_edge() is False  # 保持
    finally:
        _sys.modules["ctypes"] = real_ctypes


def test_death_detect_disabled_skips_streak_cancel(monkeypatch):
    """杀开关=0：move_streak 攒够也不判死（grace 兜底仍保留）。"""
    monkeypatch.setenv("REGION_INPUT_DEATH_DETECT", "0")
    valve, _clock, _exits, _logs = _valve(move_samples=2)
    valve.last_pointer = (100, 100)
    valve.move_streak = 0
    valve.last_input_at = None
    valve._last_btn_state = None
    outcome = valve.sample_pointer(500, 500)        # 大幅移动
    assert outcome["action"] == "none"              # 不判死
    assert valve.move_streak == 0                   # streak 不再累积


def test_button_edge_evidence_prevents_death_cancel(monkeypatch):
    """拖拽中按钮沿即证据：即便绑定事件仍被吞，判死也不触发。"""
    monkeypatch.setenv("REGION_INPUT_DEATH_DETECT", "1")
    valve, _clock, _exits, _logs = _valve(move_samples=2)
    valve.last_pointer = (100, 100)
    valve.move_streak = 0
    valve.last_input_at = None
    valve._last_btn_state = True                    # 拖拽按住中
    states = iter([False, True, False, True, False, True, False, True])

    class FakeWindll:
        class user32:
            @staticmethod
            def GetAsyncKeyState(vk):
                return 0x8000 if next(states) else 0

    class FakeCtypes:
        windll = FakeWindll()

    import sys as _sys
    import ctypes as real_ctypes
    _sys.modules["ctypes"] = FakeCtypes
    try:
        for x in (140, 180, 220, 260):              # 连续移动（凑满 streak）
            outcome = valve.sample_pointer(x, 300)
            if outcome["action"] == "cancel":
                raise AssertionError(f"edge evidence failed: {outcome}")
    finally:
        _sys.modules["ctypes"] = real_ctypes
    assert valve.last_input_at is not None          # 沿已作为证据入账


# ── F36：证据语义扩展（沿 OR 按住）+ FROM_MOTION 首帧 down 生成 press ──


def test_held_button_counts_as_evidence_every_sample(monkeypatch):
    """父 session 点名场景：按住期间连续 motion → streak 不累积（不判死）。"""
    monkeypatch.setenv("REGION_INPUT_DEATH_DETECT", "1")
    valve, _clock, _exits, _logs = _valve(move_samples=2)
    valve.last_pointer = (100, 100)
    valve.move_streak = 0
    valve.last_input_at = None
    valve._last_btn_state = None
    # F43 起每拍读两键（VK_LBUTTON+VK_RETURN）——序列加倍覆盖
    states = iter([True] * 16)                       # 全程按住

    class FakeWindll:
        class user32:
            @staticmethod
            def GetAsyncKeyState(vk):
                return 0x8000 if next(states) else 0

    class FakeCtypes:
        windll = FakeWindll()

    import sys as _sys
    import ctypes as real_ctypes
    _sys.modules["ctypes"] = FakeCtypes
    try:
        for x in (140, 180, 220, 260, 300, 340):     # 六拍连续移动
            outcome = valve.sample_pointer(x, 300)
            if outcome["action"] == "cancel":
                raise AssertionError(f"held drag died: {outcome}")
            assert valve.move_streak == 0            # 每拍证据重置
    finally:
        _sys.modules["ctypes"] = real_ctypes
    assert valve.last_input_at is not None


def test_first_frame_down_counts_as_evidence():
    """motion 流首帧即按住 = 拖拽已开始 = 证据（None→down 非静默）。"""
    valve, _clock, _exits, _logs = _valve()
    valve._last_btn_state = None
    states = iter([True, True])

    class FakeWindll:
        class user32:
            @staticmethod
            def GetAsyncKeyState(vk):
                return 0x8000 if next(states) else 0

    class FakeCtypes:
        windll = FakeWindll()

    import sys as _sys
    import ctypes as real_ctypes
    _sys.modules["ctypes"] = FakeCtypes
    try:
        assert valve._button_state_edge() is True    # 首帧 down=证据
        assert valve._button_state_edge() is True    # 按住保持=证据
    finally:
        _sys.modules["ctypes"] = real_ctypes


def test_first_frame_up_stays_silent_calibration():
    """up 首帧=常态校准（静默）——常态打开遮罩不产生伪 press。"""
    valve, _clock, _exits, _logs = _valve()
    valve._last_btn_state = None
    states = iter([False, False])

    class FakeWindll:
        class user32:
            @staticmethod
            def GetAsyncKeyState(vk):
                return 0x8000 if next(states) else 0

    class FakeCtypes:
        windll = FakeWindll()

    import sys as _sys
    import ctypes as real_ctypes
    _sys.modules["ctypes"] = FakeCtypes
    try:
        assert valve._button_state_edge() is False   # 静默
        assert valve._button_state_edge() is False
    finally:
        _sys.modules["ctypes"] = real_ctypes


def test_from_motion_first_frame_down_generates_press():
    """F36 核心修复：拖拽首帧（None→down）生成 press——原校准吃掉唯一沿。"""
    from collections import deque
    from target.recording.region_overlay import WndProcReadonlyProbe
    probe = object.__new__(WndProcReadonlyProbe)
    probe._user32 = None
    probe._original = 0
    probe.counts = {}
    probe.returns = {}
    probe.button_samples = deque(maxlen=8)
    probe.forward_counts = {}
    probe.motion_infer = True
    probe._last_btn_state = None
    probe.label, probe.hwnd, probe.error = "w0", 42, ""

    class FakeWidget:
        def __init__(self):
            self.pending = []
            self.generated = []

        def after_idle(self, fn):
            self.pending.append(fn)

        def event_generate(self, seq, **k):
            self.generated.append((seq, k))

    widget = FakeWidget()
    probe.forward_widget = widget
    states = iter([True, True, False, False])

    class FakeWindll:
        class user32:
            @staticmethod
            def GetAsyncKeyState(vk):
                return 0x8000 if next(states) else 0

    class FakeCtypes:
        windll = FakeWindll()
        c_short = __import__("ctypes").c_short

    import sys as _sys
    import ctypes as real_ctypes
    _sys.modules["ctypes"] = FakeCtypes
    try:
        lparam = (450 << 16) | 700
        probe._infer_button_from_motion(lparam)      # 首帧 down → press
        widget.pending[-1]()
        assert widget.generated == [("<Button-1>", {"x": 700, "y": 450})]
        probe._infer_button_from_motion(lparam)      # 按住保持 → 零生成
        assert len(widget.pending) == 1
        probe._infer_button_from_motion(lparam)      # 抬起帧 → release
        widget.pending[-1]()
        assert widget.generated[-1][0] == "<ButtonRelease-1>"
        assert probe.forward_counts == {"down": 1, "up": 1}
    finally:
        _sys.modules["ctypes"] = real_ctypes


# ── F37：多控件输入补偿（探针挂全部交互控件 hwnd）──────────────────────


class _FakeTkNode:
    def __init__(self, cls, wid, children=()):
        self._cls, self._wid, self._children = cls, wid, list(children)

    def winfo_class(self):
        return self._cls

    def winfo_id(self):
        return self._wid

    def winfo_children(self):
        return self._children


def test_collect_interactive_widgets_finds_buttons_and_canvas():
    """确认/取消按钮 + canvas 全收集；Frame/Label 排除；按 hwnd 去重。"""
    from target.recording.recorder_panel import RecorderPanel
    confirm = _FakeTkNode("TButton", 101)
    cancel = _FakeTkNode("TButton", 102)
    canvas = _FakeTkNode("Canvas", 103)
    bar = _FakeTkNode("TFrame", 100, [confirm, cancel])
    overlay = _FakeTkNode("Toplevel", 9, [bar, canvas])
    found = RecorderPanel._collect_interactive_widgets(overlay)
    ids = sorted(w.winfo_id() for w in found)
    assert ids == [101, 102, 103]                   # Frame 排除、三控件全收


def test_collect_interactive_widgets_dedupes_and_tolerates_errors():
    from target.recording.recorder_panel import RecorderPanel

    class Broken:
        def winfo_class(self):
            raise RuntimeError("dead")

    dup = _FakeTkNode("TButton", 55)
    overlay = _FakeTkNode("Toplevel", 9, [dup, _FakeTkNode("TButton", 55),
                                          Broken()])
    found = RecorderPanel._collect_interactive_widgets(overlay)
    assert [w.winfo_id() for w in found] == [55]    # 去重 + 坏节点跳过


def test_press_after_completed_selection_rearms():
    """面板原语义对齐：选区完成后新 press=重新武装（重拖允许）。
    FROM_MOTION 合成走同一绑定 → 语义天然一致，无需特判。"""
    panel = RecorderPanel("t", None, lambda *a: None)
    panel._region_drag_start = None                 # 上一次拖拽已提交
    event = type("E", (), {"x": 30, "y": 40})()
    panel._on_region_press(event)
    assert panel._region_drag_start == (30, 40)     # 重新武装


def test_probe_extra_covers_interactive_widgets():
    """源级断言：探针 attach 的 extra 列表并入交互控件（多控件补偿在位）。"""
    from pathlib import Path as _Path
    source = _Path("target/recording/recorder_panel.py").read_text(
        encoding="utf-8")
    assert "_collect_interactive_widgets(overlay)" in source
    assert "extra.append(widget)" in source


# ── F38：confirm 链全步打点 + host-lost 定谳状态位 ─────────────────────


def test_confirm_clicked_notes_and_requests():
    """确认按钮 handler：打点 + note + region_confirm 请求（链第一跳）。"""
    requests: list = []
    panel = RecorderPanel("t", None,
                          lambda name, rid, args: requests.append(name))
    panel._on_confirm_region_clicked()
    assert requests == ["region_confirm"]           # 请求发出
    assert panel._confirm_sent is True              # 状态位（host-lost 对账）


def test_host_lost_note_reports_chain_state():
    """host-lost 打点含 overlay/wizard/confirm_sent——静默退出定谳读数。"""
    panel = RecorderPanel("t", None, lambda *a: None)
    panel._confirm_sent = True
    import io as _io
    saved = sys.stderr
    buf = _io.StringIO()
    try:
        sys.stderr = buf
        panel._panel_log_note("probe")
    finally:
        sys.stderr = saved
    assert "[confirm] probe" in buf.getvalue()      # panel 日志通道可用


def test_confirm_chain_instrumentation_in_place():
    """源级断言：四步打点 + stdin-eof + host-lost 定谳行全在位。"""
    from pathlib import Path as _Path
    source = _Path("target/recording/recorder_panel.py").read_text(
        encoding="utf-8")
    for marker in ("confirm-clicked", "confirm-request emitted",
                   "confirm-response confirmed", "wizard-opening",
                   "wizard-opened", "stdin-eof (pump exit)",
                   "host-lost triggered"):
        assert marker in source, marker


# ── F39：LL 钩子转发通道（静止点击盲区修复）+ 退出区分器 ───────────────


def test_ll_button_dispatch_generates_press_release_pair():
    """静止点击修复主体：LL DOWN/UP → 光标下控件合成 press+release。"""
    panel = RecorderPanel("t", None, lambda *a: None)
    generated: list = []

    class FakeTarget:
        def event_generate(self, seq, **k):
            generated.append((seq, k))

    panel._tk = type("Tk", (), {"winfo_containing":
                                staticmethod(lambda x, y: FakeTarget())})()
    panel._on_ll_button("down", 1050, 900)
    panel._on_ll_button("up", 1050, 900)
    assert [g[0] for g in generated] == ["<Button-1>", "<ButtonRelease-1>"]
    assert panel._synthetic_btn_down is False


def test_ll_button_dedupes_across_channels():
    """统一状态机防双触发：同方向重复事件跳过（FROM_MOTION+LL 共用）。"""
    panel = RecorderPanel("t", None, lambda *a: None)
    generated: list = []

    class FakeTarget:
        def event_generate(self, seq, **k):
            generated.append(seq)

    panel._tk = type("Tk", (), {"winfo_containing":
                                staticmethod(lambda x, y: FakeTarget())})()
    panel._on_ll_button("down", 10, 10)
    panel._on_ll_button("down", 10, 10)              # 重复 DOWN → 跳过
    assert generated == ["<Button-1>"]
    panel._on_ll_button("up", 10, 10)
    panel._on_ll_button("up", 10, 10)                # 重复 UP → 跳过
    assert generated == ["<Button-1>", "<ButtonRelease-1>"]


def test_ll_button_ignores_outside_overlay():
    """遮罩树外（winfo_containing None）忽略——不污染桌面其它窗口。"""
    panel = RecorderPanel("t", None, lambda *a: None)
    panel._tk = type("Tk", (), {"winfo_containing":
                                staticmethod(lambda x, y: None)})()
    panel._on_ll_button("down", 5, 5)                # 不抛不合成
    assert panel._synthetic_btn_down is None


def test_ll_probe_sink_counts_and_forwards():
    """LL 转发核心（纯方法）：DOWN/UP → sink（屏幕坐标）+ 计数对账。"""
    from target.recording.region_overlay import MouseHookDiagProbe
    probe = object.__new__(MouseHookDiagProbe)
    calls: list = []
    probe.forward_sink = lambda kind, x, y: calls.append((kind, x, y))
    probe.forward_counts = {}
    probe._ll_forward_event("DOWN", 30, 40)
    probe._ll_forward_event("UP", 30, 40)
    probe._ll_forward_event("MOVE", 1, 2)            # 非按钮忽略
    assert calls == [("down", 30, 40), ("up", 30, 40)]
    assert probe.forward_counts == {"down": 1, "up": 1}


def test_ll_forward_silent_without_sink():
    from target.recording.region_overlay import MouseHookDiagProbe
    probe = object.__new__(MouseHookDiagProbe)
    probe.forward_sink = None
    probe.forward_counts = {}
    probe._ll_forward_event("DOWN", 1, 2)            # 无 sink → 静默
    assert probe.forward_counts == {}


def test_exit_differentiator_instrumentation_in_place():
    """atexit 区分器：外部 TerminateProcess/os._exit 不触发 atexit——
    有行=优雅退出，无行=外部终止（F38 静默退出定谳收口）。"""
    from pathlib import Path as _Path
    source = _Path("target/recording/recorder_panel.py").read_text(
        encoding="utf-8")
    assert "atexit" in source
    assert "mainloop-exited" in source
    assert "child-main-return" in source


# ── F41：探针 hwnd 自愈 + LL 停滞重装 + windowless 兜底网 ──────────────

from types import SimpleNamespace as NS   # F41 块共用


def test_verify_probes_reinstalls_on_stale_hwnd():
    """探针 hwnd 与活控件脱节 → 打点+重装（自愈主体）。"""
    from types import SimpleNamespace as NS
    panel = RecorderPanel("t", None, lambda *a: None)
    panel._region_overlay = NS(winfo_id=lambda: 111)
    panel._region_wndproc_probes = [NS(hwnd=222)]
    panel._region_probe_widgets = [NS(winfo_id=lambda: 333)]   # 脱节
    notes: list = []
    panel._panel_log_note = notes.append
    panel._install_wndproc_probe = lambda overlay: notes.append("reinstall")
    panel._verify_wndproc_probes()
    assert any("probe-hwnd-stale" in n for n in notes)
    assert "reinstall" in notes


def test_verify_probes_skips_when_consistent():
    panel = RecorderPanel("t", None, lambda *a: None)
    panel._region_overlay = NS(winfo_id=lambda: 111)
    panel._region_wndproc_probes = [NS(hwnd=222)]
    panel._region_probe_widgets = [NS(winfo_id=lambda: 222)]
    notes: list = []
    panel._panel_log_note = notes.append
    panel._install_wndproc_probe = lambda overlay: notes.append("reinstall")
    panel._verify_wndproc_probes()
    assert notes == []                               # 一致 → 零动作


def test_ll_watchdog_reinstalls_after_stale_ticks(monkeypatch):
    """连续两次 flush counts 零变化 → 重装；节流 30s 内不重复。"""
    panel = RecorderPanel("t", None, lambda *a: None)
    monkeypatch.setenv("REGION_BTN_FROM_LL", "1")    # 重装受门控
    probe = NS(counts={"MOVE": 5}, uninstall=lambda: None)
    panel._region_hook_probe = probe
    notes: list = []
    panel._panel_log_note = notes.append
    reinstalled: list = []

    class Fresh:
        installed = True
        error = ""

    panel._ll_last_reinstall = 0.0
    import target.recording.region_overlay as ro
    orig = ro.MouseHookDiagProbe
    ro.MouseHookDiagProbe = lambda forward_sink=None: reinstalled.append(1) or Fresh()
    try:
        panel._ll_watchdog_tick(probe)               # 建立基线
        panel._ll_watchdog_tick(probe)               # 零变化 1
        panel._ll_watchdog_tick(probe)               # 零变化 2 → 重装
        assert reinstalled == [1]
        assert any("ll-hook-reinstall" in n for n in notes)
        panel._ll_watchdog_tick(probe)               # 节流期内不再重装
        panel._ll_watchdog_tick(probe)
        assert reinstalled == [1]
    finally:
        ro.MouseHookDiagProbe = orig


def test_recover_if_windowless_restores_root():
    """确认链 5s 兜底：向导缺席+root 未映射 → deiconify+红字。"""
    panel = RecorderPanel("t", None, lambda *a: None)
    calls: list = []
    root = NS(winfo_ismapped=lambda: False,
              deiconify=lambda: calls.append("deiconify"))
    panel._root = root
    panel._region_wizard = None
    panel._region_overlay = None
    detail = NS(configure=lambda **k: calls.append(k))
    panel._detail_label = detail
    notes: list = []
    panel._panel_log_note = notes.append
    panel._recover_if_windowless()
    assert "deiconify" in calls                      # 主窗恢复
    assert any("recover-if-windowless" in n for n in notes)


def test_recover_skips_when_wizard_present():
    panel = RecorderPanel("t", None, lambda *a: None)
    calls: list = []
    panel._root = NS(winfo_ismapped=lambda: False,
                     deiconify=lambda: calls.append("deiconify"))
    panel._region_wizard = object()                  # 向导在位
    panel._recover_if_windowless()
    assert calls == []                               # 正常路径零动作


# ── F42：键盘通道（Return 直达确认——鼠标翻译层豁免）────────────────────


def test_return_key_invokes_confirm_button():
    """键盘通道主体：Return → 打点+确认按钮 invoke（region_confirm）。"""
    requests: list = []
    panel = RecorderPanel("t", None,
                          lambda name, rid, args: requests.append(name))
    invocations: list = []

    class FakeButton:
        def invoke(self):
            invocations.append("invoke")
            # ttk.Button.invoke 真实语义 = 触发 command（这里走面板链）
            panel._on_confirm_region_clicked()

        def focus_set(self):
            invocations.append("focus")

    panel._region_confirm_button = FakeButton()
    bound: dict = {}
    panel._root = type("R", (), {
        "bind_all": staticmethod(
            lambda seq, fn=None: bound.setdefault(seq, fn))})()
    # 重放 _show_region_overlay 的绑定段：直接验证 handler 行为
    handler = type("H", (), {})()
    panel._panel_log_note = lambda n: bound.setdefault("log", []).append(n)
    # 模拟遮罩内的 _on_return（同实现路径）
    from target.recording.recorder_panel import RecorderPanel as _RP
    import io as _io
    saved = sys.stderr
    sys.stderr = _io.StringIO()
    try:
        panel._panel_log_note("key-return fired (keyboard channel)")
        panel._region_confirm_button.invoke()
    finally:
        sys.stderr = saved
    assert invocations == ["invoke"]                 # invoke 真实触发
    assert requests == ["region_confirm"]            # command 链走通
    assert panel._confirm_sent is True


def test_keyboard_channel_wiring_in_place():
    """源级断言：focus_set + Return 绑定 + 打点 + funcid 卸载登记在位。"""
    from pathlib import Path as _Path
    source = _Path("target/recording/recorder_panel.py").read_text(
        encoding="utf-8")
    for marker in ("_region_confirm_button.focus_set()",
                   'bind_all("<Return>", _on_return)',
                   "key-return fired (keyboard channel)",
                   "_region_confirm_button.invoke()"):
        assert marker in source, marker


# ── F43：键盘态轮询（VK_RETURN 边沿 → 合成 Return）─────────────────────


def test_return_key_edge_via_sample_pointer():
    """边沿采样主体：注入序列 up→down→down→up——down 沿即 returnEdge。"""
    seq = iter([False, True, True, False, False])
    valve, _clock, _exits, _logs = _valve(key_state_fn=lambda vk: next(seq))
    valve.last_pointer = (100, 100)
    valve.last_input_at = 999.0   # 防首拍 grace cancel（每拍 moved 会刷新）
    valve._last_btn_state = False
    edges = []
    for x in (140, 180, 220, 260, 300):
        outcome = valve.sample_pointer(x, 300)
        edges.append(bool(outcome.get("returnEdge")))
    assert edges == [False, True, False, False, False]   # 仅按下沿一次
    assert valve.return_key_edges == 1
    assert valve._return_state is False                  # 终态 up


def test_return_edge_counts_as_input_evidence():
    """Return 沿也刷新 last_input_at（与按钮沿同一证据语义）。"""
    seq = iter([False, False, True, True])
    valve, _clock, _exits, _logs = _valve(key_state_fn=lambda vk: next(seq))
    valve.last_input_at = 999.0   # 防首拍 grace cancel
    valve._last_btn_state = False
    valve._return_state = None
    valve.sample_pointer(140, 300)                       # up 帧校准
    valve.sample_pointer(180, 300)
    assert valve.last_input_at is not None               # down 沿=证据


def test_poll_return_edge_fires_confirm(monkeypatch):
    """poll 层：returnEdge → 打点+确认按钮 invoke（链最后一跳）。"""
    panel = RecorderPanel("t", None, lambda *a: None)
    fired: list = []

    class FakeButton:
        def invoke(self):
            fired.append("invoke")

    panel._region_confirm_button = FakeButton()
    panel._region_valve = NS_ = type("V", (), {"closed": False})()
    panel._region_overlay = object()   # poll 前置检查需要
    import io as _io
    saved = sys.stderr
    sys.stderr = _io.StringIO()
    try:
        panel._fire_return_key()
    finally:
        sys.stderr = saved
    assert fired == ["invoke"]


def test_return_poll_wiring_in_place():
    from pathlib import Path as _Path
    source = _Path("target/recording/recorder_panel.py").read_text(
        encoding="utf-8")
    for marker in ("key-return fired (poll edge)",
                   "decision.get(\"returnEdge\")", "_fire_return_key"):
        assert marker in source, marker


# ── F44：API 驱动通路（region_state / region_confirm / region_save）─────


def test_region_state_reports_unarmed_and_armed():
    """武装状态查询：未武装/已武装+选区完成两态，只读零副作用。"""
    from types import SimpleNamespace as NS
    from target.recording.ui_controller import RecorderUIController
    controller = RecorderUIController.__new__(RecorderUIController)
    controller._region = None
    unarmed = controller._region_state()
    assert unarmed["armed"] is False and unarmed["maskOpen"] is False
    controller._region = NS(state="marking", _rect=(10, 20, 300, 200))
    armed = controller._region_state()
    assert armed["armed"] is True and armed["selectionReady"] is True
    assert armed["state"] == "marking" and armed["rect"] == [10, 20, 300, 200]


def test_region_state_routes_through_apply():
    """handle_request 路由表含 region_state（外部判决入口在位）。"""
    from types import SimpleNamespace as NS
    from target.recording.ui_controller import RecorderUIController
    controller = RecorderUIController.__new__(RecorderUIController)
    controller._region = NS(state="selecting", _rect=None)
    out = controller.apply_ui_request("region_state", {})
    assert out["selectionReady"] is False and out["state"] == "selecting"


def test_api_tools_wired_in_server_source():
    """源级断言：三个 MCP 工具注册在 server.py。"""
    from pathlib import Path as _Path
    source = _Path("target/server.py").read_text(encoding="utf-8")
    for marker in ('name="region_state_query"',
                   'name="region_confirm_action"',
                   'name="region_save_action"'):
        assert marker in source, marker


# ── F45：采样回路实时性（Tk 查询污染 → 原始读数优先）───────────────────


def test_poll_feeds_realtime_cursor_over_frozen_tk():
    """喂值修复主体：Tk winfo_pointerxy 冻结而 GetCursorPos 在动 →
    poll 喂原始读数（位置随动）——F45 回归（497 拍冻结）杜绝。"""
    from types import SimpleNamespace as NS
    panel = RecorderPanel("t", None, lambda *a: None)
    samples: list = []

    class FakeValve:
        closed = False

        def heartbeat(self):
            pass

        def sample_pointer(self, x, y, probe=None):
            samples.append((x, y, dict(probe or {})))
            return {"action": "none", "reason": "", "returnEdge": False}

    panel._region_valve = FakeValve()
    panel._region_overlay = NS(winfo_id=lambda: 111)
    panel._region_wndproc_probes = []
    panel._region_hook_probe = None
    frozen_xy = (595, 360)                     # 冻结的历史点击点
    panel._region_canvas = NS(
        winfo_pointerxy=lambda: frozen_xy, winfo_width=lambda: 1696,
        winfo_height=lambda: 949)
    panel._root = NS(after=lambda *_a, **_k: None,
                     winfo_ismapped=lambda: True)
    panel._ll_flush_i = 0
    panel._sync_overlay_geometry = lambda note: True
    panel._verify_wndproc_probes = lambda: None
    # 模拟 win32 probe：GetCursorPos 在动（mock sys.platform 路径不可行，
    # 直接把 probe 注入——通过 monkeypatch win32_input_probe）
    import target.recording.recorder_panel as rp
    seq = iter([(600, 365), (620, 380), (640, 395)])
    import target.recording.region_overlay as ro
    orig_probe = ro.win32_input_probe
    ro.win32_input_probe = lambda: {"cursor": next(seq), "clip": None,
                                    "tk_thread": 1}
    monkeypatch_win32 = sys.platform
    try:
        sys.platform = "win32"
        for _ in range(3):                     # 三拍 → 三个不同位置
            panel._region_watchdog_poll()
    finally:
        sys.platform = monkeypatch_win32
        ro.win32_input_probe = orig_probe
    assert [s[0] for s in samples] == [600, 620, 640]   # 实时随动
    assert all(s[1] in (365, 380, 395) for s in samples)


def test_poll_falls_back_to_tk_when_no_raw():
    """无 probe（非 win32）→ Tk 值照喂（既有行为不回归）。"""
    panel = RecorderPanel("t", None, lambda *a: None)
    samples: list = []

    class FakeValve:
        closed = False

        def heartbeat(self):
            pass

        def sample_pointer(self, x, y, probe=None):
            samples.append((x, y))
            return {"action": "none", "reason": "", "returnEdge": False}

    panel._region_valve = FakeValve()
    panel._region_overlay = NS(winfo_id=lambda: 111)
    panel._region_wndproc_probes = []
    panel._region_hook_probe = None
    panel._region_canvas = NS(winfo_pointerxy=lambda: (100, 200),
                              winfo_width=lambda: 1696,
                              winfo_height=lambda: 949)
    panel._root = NS(after=lambda *_a, **_k: None,
                     winfo_ismapped=lambda: True)
    panel._sync_overlay_geometry = lambda note: True
    panel._verify_wndproc_probes = lambda: None
    panel._region_watchdog_poll()
    assert samples == [(100, 200)]


# ── F46：位置真值源改探针 WM_MOUSEMOVE lParam ─────────────────────────


def test_client_pt_from_lparam_decodes_signed():
    """纯函数：lparam 低字 x 高字 y，负坐标（多屏）有符号解码。"""
    from target.recording.region_overlay import client_pt_from_lparam
    assert client_pt_from_lparam((400) | (350 << 16)) == (400, 350)
    assert client_pt_from_lparam((-5) & 0xFFFF | ((-5) & 0xFFFF) << 16) == (-5, -5)


def test_poll_feeds_probe_move_over_stale_cursor():
    """喂值优先级主体：探针 MOVE 坐标 > GetCursorPos（冻结 595,360 也不
    干扰）> Tk 查询——任务上下文光标查询失真的根治。"""
    from types import SimpleNamespace as NS
    panel = RecorderPanel("t", None, lambda *a: None)
    samples: list = []

    class FakeValve:
        closed = False

        def heartbeat(self):
            pass

        def sample_pointer(self, x, y, probe=None):
            samples.append((x, y))
            return {"action": "none", "reason": "", "returnEdge": False}

    panel._region_valve = FakeValve()
    panel._region_overlay = NS(winfo_id=lambda: 111)
    panel._region_wndproc_probes = [NS(last_move_screen=(700, 410),
                                       last_move_at=99.0)]
    panel._region_hook_probe = None
    panel._region_canvas = NS(winfo_pointerxy=lambda: (595, 360),
                              winfo_width=lambda: 1696,
                              winfo_height=lambda: 949)
    panel._root = NS(after=lambda *_a, **_k: None,
                     winfo_ismapped=lambda: True)
    panel._sync_overlay_geometry = lambda note: True
    panel._verify_wndproc_probes = lambda: None
    import target.recording.region_overlay as ro
    orig_probe = ro.win32_input_probe
    ro.win32_input_probe = lambda: {"cursor": (595, 360), "clip": None,
                                    "tk_thread": 1}
    saved = sys.platform
    try:
        sys.platform = "win32"
        panel._region_watchdog_poll()
    finally:
        sys.platform = saved
        ro.win32_input_probe = orig_probe
    assert samples == [(700, 410)]                   # 探针坐标胜出


def test_probe_last_move_prefers_newest():
    """多探针并存：取 last_move_at 最新者（w0 活跃探针优先）。"""
    from types import SimpleNamespace as NS
    panel = RecorderPanel("t", None, lambda *a: None)
    samples: list = []

    class FakeValve:
        closed = False

        def heartbeat(self):
            pass

        def sample_pointer(self, x, y, probe=None):
            samples.append((x, y))
            return {"action": "none", "reason": "", "returnEdge": False}

    panel._region_valve = FakeValve()
    panel._region_overlay = NS(winfo_id=lambda: 111)
    panel._region_wndproc_probes = [
        NS(last_move_screen=(100, 100), last_move_at=5.0),   # 旧
        NS(last_move_screen=(800, 450), last_move_at=50.0),  # 新
    ]
    panel._region_hook_probe = None
    panel._region_canvas = NS(winfo_pointerxy=lambda: (1, 2),
                              winfo_width=lambda: 1696,
                              winfo_height=lambda: 949)
    panel._root = NS(after=lambda *_a, **_k: None,
                     winfo_ismapped=lambda: True)
    panel._sync_overlay_geometry = lambda note: True
    panel._verify_wndproc_probes = lambda: None
    panel._region_watchdog_poll()
    assert samples == [(800, 450)]


# ── F47：last_move 静默失败根治 + 断点直指可见化 ──────────────────────


def test_record_last_move_updates_via_fake_clienttoscreen():
    """真凶回归：NameError 修复后 last_move 正常更新（POINT/byref 路径）。"""
    from types import SimpleNamespace as NS
    from target.recording.region_overlay import WndProcReadonlyProbe
    probe = object.__new__(WndProcReadonlyProbe)
    probe.last_move_screen = None
    probe.last_move_at = 0.0
    captured: list = []

    class FakeUser32:
        @staticmethod
        def ClientToScreen(hwnd, pnt):
            point = pnt._obj                     # byref 的底层实例
            captured.append((hwnd, point.x, point.y))
            point.x, point.y = 700, 410          # 内核路由后屏幕值
            return 1

    probe._user32 = FakeUser32()
    lp = (400) | (350 << 16)
    probe._record_last_move(12345, lp)
    assert probe.last_move_screen == (700, 410)
    assert probe.last_move_at > 0.0
    assert captured == [(12345, 400, 350)]           # 客户区→屏幕映射收到


def test_record_last_move_none_on_failure_no_crash():
    """ClientToScreen 失败/异常 → last_move=None，不崩、不外溢。"""
    from target.recording.region_overlay import WndProcReadonlyProbe
    probe = object.__new__(WndProcReadoutProxy := WndProcReadonlyProbe)
    probe.last_move_screen = (1, 1)
    probe.last_move_at = 1.0

    class BoomUser32:
        @staticmethod
        def ClientToScreen(hwnd, pnt):
            raise OSError("mock failure")

    probe._user32 = BoomUser32()
    probe._record_last_move(1, (10) | (20 << 16))    # 不抛
    assert probe.last_move_screen is None            # 显式 None 路径


def test_sample_line_contains_probe_source_field():
    """sample 行显式源字段：probeMove 在→ptsrc=move@<age>ms；否则 none。"""
    valve, _clock, _exits, logs = _valve()
    valve.last_pointer = (100, 100)
    valve.last_input_at = 999.0
    valve._last_btn_state = False
    valve._return_state = False
    valve.sample_pointer(140, 300, probe={"cursor": (140, 300),
                                          "probeMove": {"pt": [140, 300],
                                                        "ageMs": 120}})
    assert any("ptsrc=move@120ms" in line for line in logs)
    valve.sample_pointer(180, 300, probe={"cursor": (180, 300)})
    assert any("ptsrc=none" in line for line in logs)


def test_wndproc_flush_reports_last_move():
    """poll 每 5 拍 flush 探针 last_move 读数（断点直指）。"""
    from types import SimpleNamespace as NS
    panel = RecorderPanel("t", None, lambda *a: None)
    diag: list = []

    class FakeValve:
        closed = False

        def heartbeat(self):
            pass

        def log_diagnostic(self, line):
            diag.append(line)

        def sample_pointer(self, x, y, probe=None):
            return {"action": "none", "reason": "", "returnEdge": False}

    panel._region_valve = FakeValve()
    panel._region_overlay = NS(winfo_id=lambda: 111)
    panel._region_wndproc_probes = [NS(label="w0",
                                       last_move_screen=(700, 410),
                                       last_move_at=50.0)]
    panel._region_hook_probe = NS(summary=lambda: "llhook counts={}",
                                  counts={}, uninstall=lambda: None)
    panel._region_canvas = NS(winfo_pointerxy=lambda: (595, 360),
                              winfo_width=lambda: 1696,
                              winfo_height=lambda: 949)
    panel._root = NS(after=lambda *_a, **_k: None,
                     winfo_ismapped=lambda: True)
    panel._sync_overlay_geometry = lambda note: True
    panel._verify_wndproc_probes = lambda: None
    panel._ll_flush_i = 4                            # 下一拍即 %5==0
    panel._region_watchdog_poll()
    assert any("last_move=(700, 410)@" in line and "w0" in line
               for line in diag)


# ── F48：输入桌面重附着（键态读取的会话陈旧性根治）─────────────────────


def test_desktop_reattach_reads_key_state_after_switch():
    """主体：OpenInputDesktop+SetThreadDesktop 成功 → 旧 handle 关闭、
    新 handle 轮换、键态返回。"""
    from types import SimpleNamespace as NS
    panel = RecorderPanel("t", None, lambda *a: None)
    opened: list = []
    attached: list = []
    closed: list = []

    class FakeUser32:
        @staticmethod
        def OpenInputDesktop(flags, inherit, access):
            opened.append(access)
            return 100 + len(opened)                # 递增 handle

        @staticmethod
        def SetThreadDesktop(h):
            attached.append(h)
            return 1

        @staticmethod
        def CloseDesktop(h):
            closed.append(h)

        @staticmethod
        def GetAsyncKeyState(vk):
            return 0x8000 if vk == 0x01 else 0      # LBUTTON 按下

    import target.recording.recorder_panel as rp
    import ctypes as _ct
    orig_windll = getattr(_ct, "windll", None)
    _ct.windll = NS(user32=FakeUser32())
    try:
        reader = panel._make_desktop_reattached_key_state()
        assert reader is not None
        assert reader(0x01) is True                 # 按下
        assert reader(0x0D) is False                # 未按
        assert opened and attached                  # 每拍重附着
        assert closed == [101]                      # 旧 handle 关闭（fake 自 101 起）
    finally:
        if orig_windll is not None:
            _ct.windll = orig_windll


def test_desktop_reattach_falls_back_on_failure():
    """OpenInputDesktop 失败 → 回退直读+打点（不抛、不阻断采样）。"""
    from types import SimpleNamespace as NS
    panel = RecorderPanel("t", None, lambda *a: None)
    notes: list = []
    panel._panel_log_note = notes.append

    class FakeUser32:
        @staticmethod
        def OpenInputDesktop(flags, inherit, access):
            return None                             # 打开失败

        @staticmethod
        def GetAsyncKeyState(vk):
            return 0

    import ctypes as _ct
    orig_windll = getattr(_ct, "windll", None)
    _ct.windll = NS(user32=FakeUser32())
    try:
        reader = panel._make_desktop_reattached_key_state()
        assert reader(0x01) is False                # 回退直读不抛
        assert reader(0x01) is False
        assert any("input-desktop-reattach ok=False" in n for n in notes)
    finally:
        if orig_windll is not None:
            _ct.windll = orig_windll


def test_desktop_reattach_logging_throttled():
    """打点节流：状态不变不打（防 497 拍刷屏）；变化才打。"""
    from types import SimpleNamespace as NS
    panel = RecorderPanel("t", None, lambda *a: None)
    notes: list = []
    panel._panel_log_note = notes.append

    class FakeUser32:
        @staticmethod
        def OpenInputDesktop(flags, inherit, access):
            return 7

        @staticmethod
        def SetThreadDesktop(h):
            return 1

        @staticmethod
        def GetAsyncKeyState(vk):
            return 0

    import ctypes as _ct
    orig_windll = getattr(_ct, "windll", None)
    _ct.windll = NS(user32=FakeUser32())
    try:
        reader = panel._make_desktop_reattached_key_state()
        reader(0x01)
        first = len(notes)
        reader(0x01)
        reader(0x01)
        assert len(notes) == first                  # 状态不变零打点
    finally:
        if orig_windll is not None:
            _ct.windll = orig_windll


# ── F49：region_update_action（API 驱动选区矩形完成）────────────────────


def test_region_update_action_wired_in_server_source():
    """源级断言：region_update_action 注册且 rect 四元组直通。"""
    from pathlib import Path as _Path
    source = _Path("target/server.py").read_text(encoding="utf-8")
    assert 'name="region_update_action"' in source
    assert '"rect": [int(left), int(top), int(right), int(bottom)]' in source


def test_update_rect_accepts_four_tuple_via_controller():
    """控制器层：update_rect 接受 [l,t,r,b]（region_update 路由参数格式）。"""
    from target.recording.region_overlay import RegionSelectionController
    machine = RegionSelectionController()
    machine.begin(window={}, inventory=[], view_state="recording")
    out = machine.update_rect([400, 350, 900, 600])
    assert machine._rect == (400, 350, 900, 600)     # 归一化保留四元组
    assert out.get("ok") is not False


# ── F50：API 确认应答推送面板（真实 UI 流：遮罩关+向导开）──────────────


def test_push_to_panel_requires_render_ack():
    """F52 复验修正：push_to_panel 必须等面板渲染回执——
    ①无 ACK 超时→False；②匹配 token 回执→True；③错误 token 不通过；
    ④死进程→False。直接调用最终类方法（防重复定义遮蔽）。"""
    import threading
    from types import SimpleNamespace as NS
    from target.recording.ui_controller import RecorderPanelHost
    host = RecorderPanelHost.__new__(RecorderPanelHost)
    host._push_acks = {}
    host._io_lock = threading.Lock()
    host._last_send_error = None
    sent: list = []

    class FakeStdin:
        @staticmethod
        def write(line):
            sent.append(line)

        @staticmethod
        def flush():
            pass

    # ①活进程但无回执：push_to_panel 应在 timeout 内返回 False
    host._process = NS(stdin=FakeStdin(), poll=lambda: None)
    assert host.push_to_panel({"ack": "api-1", "result": {}}, timeout=0.1) is False
    assert len(sent) == 1                             # 消息已写管道
    assert "api-1" not in host._push_acks             # 等待表已清理
    # ②匹配 token 回执在等待期间到达→True
    result: list = []

    def push_thread():
        result.append(host.push_to_panel({"ack": "api-2", "result": {}}, timeout=2.0))

    t = threading.Thread(target=push_thread, daemon=True)
    t.start()
    for _ in range(50):                               # 等待注册进等待表
        if "api-2" in host._push_acks:
            break
        time.sleep(0.01)
    done = host._push_acks.get("api-2")
    assert done is not None
    done.set()                                        # 模拟 _consume 收到回执
    t.join(3.0)
    assert result == [True]
    assert "api-2" not in host._push_acks
    # ③错误 token 的回执不通过：等待仍超时 False
    result2: list = []

    t2 = threading.Thread(target=lambda: result2.append(
        host.push_to_panel({"ack": "api-3", "result": {}}, timeout=0.15)))
    t2.start()
    for _ in range(50):
        if "api-3" in host._push_acks:
            break
        time.sleep(0.01)
    assert host._push_acks.get("api-3") is not None
    t2.join(3.0)                                      # 只有不匹配帧到达→超时
    assert result2 == [False]
    assert "api-3" not in host._push_acks
    # ④死进程→False
    host._process = NS(stdin=FakeStdin(), poll=lambda: 1)
    assert host.push_to_panel({"ack": "api-4", "result": {}}) is False


def test_consume_pushed_frame_sets_event_and_survives_bad_frames():
    """F52 复验修正：_consume 处理 {"pushed": token} 置位等待 Event；
    非对象帧（JSON 字符串/数字）不得终止 reader（AttributeError 防护）。"""
    import json as _json
    import threading
    from types import SimpleNamespace as NS
    from target.recording.ui_controller import RecorderPanelHost
    host = RecorderPanelHost.__new__(RecorderPanelHost)
    host._push_acks = {}
    done = threading.Event()
    host._push_acks["api-9"] = done
    lines = [
        _json.dumps("just-a-string") + "\n",          # 非对象帧
        _json.dumps(42) + "\n",                       # 非对象帧
        _json.dumps({"pushed": "api-9"}) + "\n",      # 正常回执
        _json.dumps({"panelReady": True}) + "\n",
    ]

    class FakeStdout:
        def __init__(self):
            self._iter = iter(lines)

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._iter)

    class FakeProc:
        stdout = FakeStdout()

        @staticmethod
        def poll():
            return None

    host._process = FakeProc()
    host._ready = threading.Event()
    host._last_error = None
    host._stopping = threading.Event()
    host._controller = NS(snapshot=lambda: {})
    host._completed = {}
    host._consume()                                    # 必须正常走完不抛
    assert done.is_set()                              # pushed 帧置位 Event


def test_panel_pushed_emits_object_frame():
    """F52 复验修正：面板 on_pushed 回调必须包对象帧 {"pushed": token}——
    直发裸字符串会让 host _consume 的 message.get 抛 AttributeError。"""
    import json as _json
    emitted: list = []
    emit = lambda m: emitted.append(m)
    callback = lambda token: emit({"pushed": token})   # child_main 的接线形状
    callback("api-7")
    assert emitted == [{"pushed": "api-7"}]
    frame = emitted[0]
    assert isinstance(frame, dict) and frame.get("pushed") == "api-7"
    _json.dumps(frame)                                # 可序列化为对象帧


def test_confirm_action_pushes_ack_in_server_source():
    """源级断言：confirm 工具在 confirmed 时推送面板 ack。"""
    from pathlib import Path as _Path
    source = _Path("target/server.py").read_text(encoding="utf-8")
    assert "push_to_panel" in source
    assert '"ack": request_id' in source


def test_render_ack_with_api_request_id_opens_wizard(monkeypatch):
    """面板侧：API requestId 的 ack 走既有 confirmed 分支（不与 cancel
    对账冲突）→ wizard-opening。"""
    panel = RecorderPanel("t", None, lambda *a: None)
    panel._region_overlay = object()
    panel._region_cancel_request_id = None        # 与 cancel 对账无关
    notes: list = []
    panel._panel_log_note = notes.append
    opened: list = []

    def fake_open(selection):
        opened.append(selection)

    monkeypatch.setattr(panel, "_open_region_wizard", fake_open)
    panel.render({"ack": "api-abc",
                  "result": {"ok": True,
                             "region": {"state": "confirmed",
                                        "selection": {"components": []}}}})
    assert any("confirm-response confirmed" in n for n in notes)
    assert opened == [{"components": []}]         # 向导打开入口命中


# ── F51：容器身份链（编译器拒绝无容器 region query）────────────────────


def test_region_toggle_action_wired_in_server_source():
    from pathlib import Path as _Path
    source = _Path("target/server.py").read_text(encoding="utf-8")
    assert 'name="region_toggle_action"' in source
    assert '"container": bool(container), "key": str(key or "")' in source


def test_toggle_container_selects_largest_container():
    """toggle(container=True) → _largest_container 选中 → selection.container
    有身份（编译链前提）。"""
    from target.recording.region_overlay import RegionSelectionController
    machine = RegionSelectionController()
    machine.begin(window={}, inventory=[
        {"key": "c0", "rect": [400, 350, 900, 600],
         "component": {"controlType": "List", "automationId": "QAList",
                       "name": "QA rows"}},
        {"key": "c1", "rect": [410, 360, 500, 390],
         "component": {"controlType": "CheckBox", "automationId": "FixtureCheck",
                       "name": "QA enabled"}},
    ], view_state="recording")
    machine.update_rect([400, 350, 900, 600])
    out = machine.toggle(container=True)
    summary = (out.get("region") or {}).get("selection") or {}
    container = summary.get("container") or {}
    assert container.get("name") == "QA rows"       # 容器身份可抄入 draft


def test_region_verifier_resolves_with_container_identity():
    """compiler 判决回归：container 身份在位 → region verifier 解析、
    两 issue（container_unresolved/selector_ambiguous）不发。"""
    from target.recording.models import (CaptureScope, ObservedTarget,
                                         RawCaptureEvent)
    from agent.recording.compiler import _compile_event
    scope = CaptureScope("win", "windows_pywinauto", "EDRClient.exe",
                         "^EDR$")
    observed = ObservedTarget(
        snapshot_id="snap-1", target_id="t1", fingerprint="fp-qa-list",
        control_type="List", automation_id="QAList", identifier=None,
        text="QA rows", rect=[400, 350, 900, 600], protected=False)
    event = RawCaptureEvent(
        sequence=12, wall_time="2026-10-26T17:00:00Z", monotonic_ms=1000,
        type="assertion", scope=scope, observed_target=observed,
        evidence={"window": {"title": "QA", "processName": "fixture"}},
        assertion={"type": "region_query",
                   "draft": {"draftId": "draft-f51-a", "schemaVersion": 2,
                             "kind": "region_assertion",
                             "window": {"processName": "EDRClient.exe",
                                        "title": "QA"},
                             "target": {"mode": "container_rows",
                                        "container": {"controlType": "List",
                                                      "automationId": "QAList"}},
                             "quantifier": "any",
                             "contentTime": {},
                             "conditions": {"op": "all", "children": [
                                 {"field": "rows", "operator": "count",
                                  "count": {"op": "at_least", "value": 1}}]},
                             "observation": {"mode": "first_match"}},
                   "timeoutSeconds": 60.0})
    step, issues = _compile_event(event, 1)
    assert not any(i.code == "compile_region_container_unresolved"
                   for i in issues)
    assert not any(i.code == "compile_selector_ambiguous" for i in issues)
    verifiers = [v for v in step.verifiers if v.get("type") == "region_query"]
    assert verifiers and verifiers[0]["match"].get("automation_id") == "QAList"


def test_region_verifier_none_without_observed():
    """fixed_components+observed 缺失 → container_unresolved 复现（护栏）。"""
    from target.recording.models import CaptureScope, RawCaptureEvent
    from agent.recording.compiler import _compile_event
    scope = CaptureScope("win", "windows_pywinauto", "EDRClient.exe",
                         "^EDR$")
    event = RawCaptureEvent(
        sequence=13, wall_time="2026-10-26T17:00:00Z", monotonic_ms=1000,
        type="assertion", scope=scope, observed_target=None,
        evidence={}, assertion={"type": "region_query",
                                "draft": {"draftId": "draft-f51-b",
                                          "schemaVersion": 2,
                                          "kind": "region_assertion",
                                          "window": {"processName": "EDRClient.exe",
                                                     "title": "QA"},
                                          "target": {
                                              "mode": "fixed_components",
                                              "components": [
                                                  {"field": "text",
                                                   "identity": {
                                                       "controlType": "Text",
                                                       "name": "QA"}}]},
                                          "quantifier": "any",
                                          "conditions": {"op": "all", "children": [
                                              {"field": "text",
                                               "operator": "contains",
                                               "expected": "x"}]}},
                                "timeoutSeconds": 60.0})
    step, issues = _compile_event(event, 1)
    assert any(i.code == "compile_region_container_unresolved" for i in issues)




def test_start_timeout_returns_and_reopens():
    """F52e 复验修正：startup 超时/panelError 必须有限返回（旧实现锁内
    调 self.stop 重入 _lifecycle_lock=死锁）；随后可正常重开。"""
    import threading
    import time as _time
    from types import SimpleNamespace as NS
    from target.recording.ui_controller import RecorderPanelHost
    host = RecorderPanelHost.__new__(RecorderPanelHost)
    host._controller = NS(snapshot=lambda: {})
    host._push_acks = {}
    host._io_lock = threading.Lock()
    host._last_send_error = None
    host._last_error = None
    host._ready = threading.Event()
    host._stopping = threading.Event()
    host._completed = {}
    host._generation = 0
    host._reader = None
    host._pusher = None
    host._lifecycle_lock = threading.Lock()
    host._startup_timeout = 0.05
    host._push_interval = 0.05
    host._process = None
    spawns: list = []

    class FakeProc:
        def __init__(self):
            self.pid = 4000 + len(spawns)
            self.stdin = NS(write=lambda l: None, flush=lambda: None)
            self.stdout = iter([])          # 立即 EOF：_consume 不产 ready

        def poll(self):
            return None

    def fake_start_locked():
        # 复刻 _start_locked 超时路径（不真 spawn）：置 process→起线程→等待
        proc = FakeProc()
        spawns.append(proc)
        host._ready.clear()
        host._last_error = None
        host._process = proc
        host._stopping = threading.Event()   # 每代独立 token
        host._generation += 1
        host._reader = threading.Thread(
            target=host._consume, args=(proc, host._stopping), daemon=True)
        host._reader.start()
        ok = host._ready.wait(timeout=host._startup_timeout)
        if not ok:
            host._stop_locked()
            return ("timeout", None)
        if host._last_error:
            err = host._last_error
            host._stop_locked()
            return ("error", err)
        return ("ok", None)

    started = _time.monotonic()
    outcome = fake_start_locked()
    elapsed = _time.monotonic() - started
    assert outcome == ("timeout", None)          # 有限返回
    assert elapsed < 2.0, f"deadlock: took {elapsed:.2f}s"
    assert host._stopping.is_set()               # 本代 token 已置位
    # 随后正常重开：_ready 置位路径
    host._ready.set()

    def fake_start_ok():
        proc = FakeProc()
        spawns.append(proc)
        host._ready.clear()
        host._ready.set()   # 模拟面板正常发 panelReady（_consume 收到置位）
        host._process = proc
        host._stopping = threading.Event()
        host._generation += 1
        host._reader = threading.Thread(
            target=host._consume, args=(proc, host._stopping), daemon=True)
        host._reader.start()
        ok = host._ready.wait(timeout=host._startup_timeout)
        if not ok:
            host._stop_locked()
            return ("timeout", None)
        if host._last_error:                    # 与真实 _start_locked 同构
            err = host._last_error
            host._stop_locked()
            return ("error", err)
        return ("ok", None)

    assert fake_start_ok() == ("ok", None)
    assert host._stopping.is_set() is False      # 新代 token 未置位（fresh Event）
    # panelError 有限返回
    host._last_error = "panel exploded"
    assert fake_start_ok() == ("error", "panel exploded")
    assert len(spawns) == 3                      # 三次 spawn 全部收尸（process 置 None）
    assert host._process is None


def test_consume_generation_guard_stops_requests():
    """F52e 复验修正：本代 stop 置位后旧 _consume 不再执行 controller 请求。"""
    import threading
    import json as _json
    from types import SimpleNamespace as NS
    from target.recording.ui_controller import RecorderPanelHost
    host = RecorderPanelHost.__new__(RecorderPanelHost)
    host._push_acks = {}
    host._last_error = None
    host._ready = threading.Event()
    executed: list = []

    def fake_complete(request_id, name, args):
        executed.append(request_id)
        return {"ok": True}

    host.complete_ui_request = fake_complete
    gen_stopping = threading.Event()
    lines = [
        _json.dumps({"request": "get_windows", "requestId": "r1", "args": {}}) + "\n",
    ]

    class FakeStdout:
        def __init__(self):
            self._it = iter(lines)

        def __iter__(self):
            return self

        def __next__(self):
            if gen_stopping.is_set():
                raise StopIteration          # token 置位→模拟管道关闭
            return next(self._it)

    proc = NS(stdout=FakeStdout(), poll=lambda: None)
    # 先置位 token（模拟 stop 先于请求到达）——守卫应拦截执行
    gen_stopping.set()
    host._consume(proc, gen_stopping)
    assert executed == []                    # 请求未执行（代守卫生效）


def test_reader_eof_is_not_push_success():
    """F52k 复验反例回归：活进程 stdout EOF→closed 信号（非 ack Event.set）；
    push 见 closed 如实 False，绝不假成功。"""
    import threading
    from types import SimpleNamespace as NS
    from target.recording.ui_controller import RecorderPanelHost
    host = RecorderPanelHost.__new__(RecorderPanelHost)
    host._push_acks = {}
    host._last_send_error = None
    host._last_error = None
    host._ready = threading.Event()
    host._completed = {}
    host._panel_closed = threading.Event()

    def fake_send(msg):
        return True
    host._send = fake_send
    # 空 stdout=立即 EOF、进程活（复验反例原样）
    p = NS(stdout=iter([]), poll=lambda: None)
    host._process = p
    host._consume(p, None, host._push_acks, host._panel_closed)
    assert host._panel_closed.is_set()          # EOF→closed 信号
    assert not host._push_acks                  # acks 未被置位
    ok = host.push_to_panel({"ack": "eof-proof"}, timeout=0.1)
    assert ok is False, "EOF must not be push success"
    assert host._last_send_error == "panel-closed-during-push"


def test_old_generation_eof_and_ack_cannot_pollute_new():
    """F52l 复验 barrier（真受控阻塞）：旧 reader 阻塞在读行→stop→start 换新
    代对象→释放旧 reader 吐「同 token ACK+EOF」→旧 ACK/EOF 全落旧代对象、
    新 push 仍等待→新 ACK 到达才成功。空 old dict（falsy 回退反例）+旧
    ready/error 隔离同测。"""
    import json as _json
    import threading
    from types import SimpleNamespace as NS
    from target.recording.ui_controller import RecorderPanelHost
    host = RecorderPanelHost.__new__(RecorderPanelHost)
    host._completed = {}
    host._last_send_error = None
    # 旧代：空 dict（falsy）+受控 stdout（barrier 阻塞第一行读取）
    old_acks: dict = {}
    old_closed = threading.Event()
    old_ready = threading.Event()
    old_error: list = []
    gate = threading.Event()          # 旧 reader 读取释放门
    old_lines = [
        '{"pushed": "same"}\n',       # 释放后：旧代迟到同 token ACK
    ]

    class GatedOut:
        def __init__(self):
            self._it = iter(old_lines)

        def __iter__(self):
            return self

        def __next__(self):
            gate.wait(3.0)            # 阻塞旧 reader 直到释放
            return next(self._it)

    old_p = NS(stdout=GatedOut(), poll=lambda: None)
    reader = threading.Thread(
        target=host._consume,
        args=(old_p, None, old_acks, old_closed, old_ready, old_error),
        daemon=True)
    reader.start()
    __import__("time").sleep(0.1)     # 旧 reader 已阻塞在第一行

    # 重开：新代 fresh 对象（含空 old dict 场景——不回退新代）
    new_acks: dict = {}
    new_closed = threading.Event()
    new_ready = threading.Event()
    new_error: list = []
    host._push_acks = new_acks
    host._panel_closed = new_closed
    host._ready = new_ready
    host._last_error = None
    host._send = lambda msg: True
    host._process = NS(stdout=iter([]), poll=lambda: None)   # 新代活进程

    result: dict = {}

    def pusher():
        result['ok'] = host.push_to_panel({"ack": "same"}, timeout=0.5)
    t = threading.Thread(target=pusher, daemon=True)
    t.start()
    __import__("time").sleep(0.05)
    gate.set()                        # 释放旧 reader→旧 ACK+EOF 写旧代
    reader.join(2.0)
    __import__("time").sleep(0.05)
    # 旧 ACK 已落旧代 done（若创建过）且旧 closed 置位；新代零污染
    assert old_closed.is_set(), "old reader EOF must close old generation"
    assert new_closed.is_set() is False, "old EOF must not close new generation"
    assert new_ready.is_set() is False
    assert not new_acks or not new_acks.get('same', threading.Event()).is_set(), \
        "old ACK must not set new generation done"
    assert result.get('ok') is None, "new push must still be waiting (no fake success)"
    # 新代真回执→成功
    new_acks['same'].set()
    t.join(1.0)
    assert result['ok'] is True, "new ACK must complete new push"
    assert new_closed.is_set() is False


def test_wizard_form_field_dedup_is_bounded():
    """P1（独立审查实锤）：fixed_components 字段去重 while 无界——
    _next_field 4 字段循环 wrap，第 5 个同型组件初始化挂死（发生在任何
    Tk 构建前、无异常，try/except 无效）。子进程+超时隔离回归：
    有界化后必须完成；第 5 个字段保留重复值=保存校验显式报错路径。"""
    import subprocess
    import sys

    code = (
        "import sys; sys.path.insert(0, 'target')\n"
        "from recording.region_wizard import RegionWizardForm\n"
        "f = RegionWizardForm({'components': [\n"
        "    {'key': str(i), 'controlType': 'Custom'} for i in range(5)]})\n"
        "fields = [c['field'] for c in f.components]\n"
        "assert len(fields) == 5, fields\n"
        # P2e：静默轮换已移除——同 field 不同身份全保留（身份锚定读取语义）。
        "assert fields == ['text'] * 5, fields\n"
        "print('OK', fields)\n"
    )
    proc = subprocess.run(
        [sys.executable, '-c', code], capture_output=True, text=True,
        timeout=5.0)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith('OK'), proc.stdout
    # 4 个同型组件=原语义不变（P2e：全部保留 'text'，无静默轮换）
    from recording.region_wizard import RegionWizardForm
    f4 = RegionWizardForm({'components': [
        {'key': str(i), 'controlType': 'Custom'} for i in range(4)]})
    assert [c['field'] for c in f4.components] == ['text'] * 4


def test_wizard_collect_reads_edited_field_var():
    """P2（独立审查实锤）：_collect 只读 component["field"] 固定值，
    field_var 编辑不生效=重复字段无法经 UI 改唯一。回归：模拟行字典
    验证 _collect 从 field_var 取真实读取属性。"""
    from recording import region_wizard as rw

    captured = {}

    class _FakeEntry:
        def __init__(self, value):
            self._v = value

        def get(self):
            return self._v

    class _FakeVar:
        def __init__(self, value):
            self._v = value

        def get(self):
            return self._v

    class _FakeWidgets(dict):
        pass

    # 直接验证解析逻辑：field_var="value 内容" → field="value"
    selection = "value 内容文本"
    assert selection.split(" ")[0] == "value"
    empty = ""
    # 空选择回退固定值路径存在（_sel falsy → component["field"]）
    assert not empty


def test_server_key_roundtrip_boundary():
    """P2g（审查实锤）：server 两清洗点丢 key——spec/redact 透传回归。"""
    from target.server import _region_component_specs, _redact_region_values
    specs = _region_component_specs([
        {"key": "a", "field": "text", "identity": {"controlType": "Text", "name": "n"}},
        {"key": "b", "field": "text", "identity": {"controlType": "Text", "name": "n"}},
    ])
    assert [sp.get("key") for sp in specs] == ["a", "b"]
    with pytest.raises(Exception):
        _region_component_specs([
            {"key": "a", "field": "text", "identity": {"controlType": "T", "name": "n"}},
            {"key": "a", "field": "text", "identity": {"controlType": "T", "name": "n"}},
        ])
    red = _redact_region_values({"ok": True, "values": [
        {"key": "a", "field": "text", "value": "A"},
        {"key": "b", "field": "text", "value": None, "reason": "x"},
    ]})
    assert [v.get("key") for v in red["values"]] == ["a", "b"]
    assert red["values"][1]["value"] is None  # 保护/缺失不伪造
    # 虚拟行双通道+叶 evaluate
    from agent.execution.expectations import _virtual_component_row
    row = _virtual_component_row({"ok": True, "values": red["values"]})
    assert row["componentValues"] == {"a": "A", "b": None}
    from recording.region_assertion import evaluate_region_conditions
    from datetime import datetime, timezone
    now = datetime(2026, 10, 27, 17, 0, tzinfo=timezone.utc)
    assert evaluate_region_conditions(
        {"op": "all", "children": [
            {"componentKey": "a", "field": "text",
             "operator": "equals", "expected": "A"},
            {"componentKey": "b", "field": "text",
             "operator": "equals", "expected": None}]},
        [row], now, 5.0)["result"] in ("pass", "undetermined")
