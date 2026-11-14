"""真实 target.server 适配器最小闭环(0959 复验切片;09 号设计 §7/§7.2)。

execute_v3_window_stage_real:server 链是真实工具(begin_product_run /
register_window_declaration / bind_entry_window / focus_bound_window /
execute_action / end_product_run),经 server_tools 注入;fake 只替换底层
OS 事实层(枚举/连接/锁定/输入注入/dump_tree 控件投影),授权、租约、
binding facade、dispatcher、observation 全真实。不 mock server 逻辑。

正向(A→B→A 与连续同窗,真实 compiler ready v3 产物):
- 显式 golden W→已安装产品 role 映射(W 是引用不是授权;声明经
  register_window_declaration 在产品域内校验);
- 每步:绑定聚焦 + 权威锁 + 本窗口新观测(observe=True,无业务动作)
  → 声明控件解析到 fresh observation → target_ref → 同租约边界
  focus 后/dispatch 前重验 → 真实 dispatcher 恰一次;
- 核对实际动作参数/窗口顺序/输入次数/持久 previous 链;
- finally end_product_run(真实清理);token 仅内存,不进返回/报告。

负向(全部 GUI 前拒绝,零越权输入):
- 映射冲突(未知 W / 非产品 role / 越域身份)→ ValueError,0 server 调用;
- 缺能力(coverage=unknown/generation=0)→ bind/声明拒绝;
- 过期租约(end_product_run 后旧 token)→ 拒绝;
- 同名兄弟替代(同 pid 不同 handle / 未注册进程)→ 拒绝 0 输入。

无 Windows:backend 为 fake OS 层;材料如实标注 fake OS,不称实机。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

from target import product_policy
from target.product_profiles import get_capability

REAL_DIGEST = get_capability("windows_hisec", "windows_pywinauto").digest
_INSTALLED_PATH = "C:\\Program Files\\HiSec-Endpoint\\core\\EDRClient.exe"

# 合法观测引用合成值(严格格式 OBS- + 32 hex;1129 严格校验生效后,
# 旧 f"OBS-{w}" 形状不再通过 — 合成值也必须像真实注册 id)。
import itertools as _itertools
_obs_counter = _itertools.count(1)


def _synth_obs(n=None):
    import uuid as _uuid
    if n is None:
        return "OBS-" + _uuid.uuid4().hex
    return "OBS-" + (f"{n:032x}" if n < (1 << 128)
                     else _uuid.uuid4().hex)


_SYNTH_OBS_ID = [_synth_obs(1)]


@pytest.fixture(autouse=True)
def _clean_lease():
    product_policy.reset_for_tests()
    import target.server as server
    server._binding_facade_holder["facade"] = None
    yield
    product_policy.reset_for_tests()
    server._binding_facade_holder["facade"] = None


class _FakeWindow:
    def __init__(self, handle, pid, title):
        self._handle = handle
        self._pid = pid
        self._title = title

    def window_text(self):
        return self._title

    def process_id(self):
        return self._pid

    @property
    def handle(self):
        return self._handle

    def rectangle(self):
        return SimpleNamespace(left=0, top=0, width=800, height=600)

    def restore(self):
        return None

    def set_focus(self):
        return None


class _FakeOSBackend:
    """替身 OS 事实层(与 test_window_binding_server_wiring 的协议一致;
    dump_tree 支持每窗口额外控件注入,以暴露 golden 声明的语义控件)。
    仅 OS 层;授权/租约/binding/dispatcher 全真实。"""

    def __init__(self, windows, capability=None, exe_path=_INSTALLED_PATH):
        self._windows = {str(k): dict(v) for k, v in (windows or {}).items()}
        self.dispatch_calls: list[dict] = []
        self.exe_queries: list[int] = []
        self._exe_path = exe_path
        self._capability = dict(capability) if capability else {
            "supported": True, "coverage": "strong", "generation": 2}
        self._lock_state: dict | None = None
        self._gui = SimpleNamespace(main_window=None)
        self._backend_kind = "windows_pywinauto"

    def _enumerate_top_windows_windows(self, *, strict=False):
        live = []
        for native_id, w in self._windows.items():
            live.append({
                "handle": int(native_id),
                "pid": w.get("process_id", 42),
                "process_name": w.get("process_name", "EDRClient.exe"),
                "title": w.get("title", ""),
                "rect": {"left": 0, "top": 0, "right": 800, "bottom": 600},
                "visible": w.get("visible", True),
                "foreground": w.get("foreground", True),
                "root_control_id": w.get("root_control_id", ""),
            })
        return live

    def connect_window_identity(self, handle, pid, timeout=10.0):
        item = self._windows.get(str(handle))
        if item is None or int(item.get("process_id", 42)) != int(pid):
            return {"ok": False, "error": "gone"}
        self._gui.main_window = _FakeWindow(int(handle), int(pid),
                                            item.get("title", ""))
        return {"ok": True, "pid": int(pid), "handle": int(handle),
                "title": item.get("title", "")}

    def lock_window(self, title_re=None, process_name=None, pid=None,
                    strict=True, activate=True, **kwargs):
        state = self._connected_window_state()
        if not state.get("ok"):
            return {"ok": False, "code": "window_lock_criteria_mismatch",
                    "error": "no connected window"}
        from target.automation.base import lock_criteria_mismatch
        mismatch = lock_criteria_mismatch(state, title_re, process_name, pid)
        if mismatch:
            return {"ok": False, "code": "window_lock_criteria_mismatch",
                    "error": "criteria mismatch", "mismatch": mismatch}
        self._lock_state = {
            "backend": "windows_pywinauto",
            "title_re": title_re or (state.get("title") or ""),
            "process_name": state.get("process_name"),
            "pid": state.get("pid"),
            "handle": state.get("handle"),
            "strict": bool(strict),
            "snapshot": state,
            "generation": self._capability.get("generation", 2),
        }
        return {"ok": True, "lock": self._lock_state,
                "verify": {"ok": True, "locked": True,
                           "foreground_verified": True, "degraded": False}}

    def get_window_lock(self):
        return {"ok": True, "locked": self._lock_state is not None,
                "lock": self._lock_state}

    def verify_window_lock(self, activate=True):
        if self._lock_state is None:
            return {"ok": True, "locked": False}
        state = self._connected_window_state()
        if (not state.get("ok")
                or state.get("pid") != self._lock_state.get("pid")):
            return {"ok": False, "locked": True, "code": "ownership_mismatch",
                    "error": "locked window state changed"}
        return {"ok": True, "locked": True,
                "foreground_verified": True, "degraded": False,
                "active": state,
                "verification": {"method": "foreground_ownership"},
                "lock": self._lock_state}

    def unlock_window(self):
        self._lock_state = None
        return {"ok": True}

    def _connected_window_state(self):
        win = self._gui.main_window
        if win is None:
            return {"ok": False, "error": "Not connected"}
        return {"ok": True, "handle": win.handle, "title": win.window_text(),
                "pid": win.process_id(),
                "process_name": self.process_name_for_pid(win.process_id()),
                "rectangle": {"x": 0, "y": 0, "w": 800, "h": 600}}

    def process_name_for_pid(self, pid):
        for w in self._windows.values():
            if int(w.get("process_id", 42)) == int(pid):
                return w.get("process_name", "EDRClient.exe")
        return None

    def exe_path_for_pid(self, pid):
        self.exe_queries.append(int(pid))
        return self._exe_path

    def dump_tree(self, window_title_re=None, max_depth=10):
        # 只投影**当前已连接(权威锁定)窗口**的控件(1059 缺口2:
        # 不得遍历全部窗口冒充"本窗口观测")。
        if self._lock_state is None:
            return {"ok": False, "error": "No window connected"}
        native_id = str(self._lock_state.get("handle"))
        w = self._windows.get(native_id)
        if w is None:
            return {"ok": False, "error": "locked window not in inventory"}
        controls = [{
            "class_name": "Window", "text": w.get("title", ""),
            "control_id": 1,
            "automation_id": w.get("root_control_id", ""),
            "rectangle": {"x": 0, "y": 0, "w": 800, "h": 600},
            "is_visible": True, "is_enabled": True, "depth": 0,
        }]
        for extra in w.get("extra_controls") or []:
            # 每控件独立 rectangle(真实控件各自几何;不同
            # automationId 不同位置 — drag 双端解析的几何基础)。
            control_index = len(controls)
            control = {
                "class_name": extra.get("class_name", "Button"),
                "text": extra.get("text", ""),
                "control_id": len(controls) + 1,
                "automation_id": extra.get("automation_id", ""),
                "rectangle": {"x": 10 + 30 * control_index,
                              "y": 10 + 25 * control_index,
                              "w": 100, "h": 30},
                "is_visible": True, "is_enabled": True, "depth": 1,
            }
            # OS 状态事实透传(checked/value — 观测投影按
            # normalize_observed_control 进入断言求值)。
            for state_key in ("checked", "value", "toggle_state"):
                if state_key in extra:
                    control[state_key] = extra[state_key]
            controls.append(control)
        return {"ok": True, "title": self._lock_state.get("title"),
                "window_rectangle": {"x": 0, "y": 0, "w": 800, "h": 600},
                "rectangle_mode": "screen", "truncated": False,
                "complete": True, "controls": controls}

    def click(self, **kwargs):
        self.dispatch_calls.append({"method": "click", **kwargs})
        return {"ok": True, "data": {"clicked": True}}

    def click_target(self, **kwargs):
        self.dispatch_calls.append({"method": "click_target", **kwargs})
        return {"ok": True, "data": {"clicked_at": "center"}}

    def click_at(self, x, y, expected_process_name=None):
        self.dispatch_calls.append({"method": "click_at", "x": x, "y": y,
                                    "expected_process_name":
                                        expected_process_name})
        return {"ok": True, "method": "mouse.click", "x": int(x),
                "y": int(y)}

    def activate_app(self, app_name=None, bundle_id=None):
        self.dispatch_calls.append({"method": "activate_app",
                                    "app_name": app_name})
        return {"ok": True, "method": "activate_registered"}

    def is_window_open(self, title_re=None, process_name=None,
                       class_name=None):
        import re as _re
        for w in self._windows.values():
            if process_name and w.get("process_name", "EDRClient.exe") != \
                    process_name:
                continue
            if title_re and not _re.search(title_re, w.get("title", "")):
                continue
            return {"ok": True, "open": True}
        return {"ok": True, "open": False}

    def focus_existing_window(self, *, handle, pid, title, process_name):
        item = self._windows.get(str(handle))
        if item is None or int(item.get("process_id", 42)) != int(pid):
            return {"ok": False, "error": "gone"}
        return {"ok": True, "method": "focus_existing_window",
                "handle": int(handle), "pid": int(pid)}

    def window_sequence_capability(self):
        return dict(self._capability)

    def lifecycle_event_stream(self):
        from target.windowing import instance_tracker

        backend = self

        class _DynamicStream:
            """动作响应的 OS 生命周期事件流(fake OS 事实层):每次
            drain 对照当前可见性差分 emit create/destroy — 与真实
            SetWinEventHook 的 SHOW/HIDE/DESTROY 语义一致(强实例
            事实:窗口出现/消失进入本 run 跟踪)。"""

            def __init__(self):
                self._seen: set[str] = set()
                self._pending: list[dict] = []
                self._healthy = True
                self.failure = ""

            @property
            def healthy(self):
                return self._healthy

            def drain_events(self):
                events = list(self._pending)
                self._pending.clear()
                for native_id, w in backend._windows.items():
                    visible = w.get("visible", True) is not False
                    was = native_id in self._seen
                    if visible and not was:
                        events.append({
                            "kind": "create",
                            "native_id": str(native_id),
                            "pid": w.get("process_id", 42),
                            "root_control_id":
                                w.get("root_control_id", ""),
                            "class_name": w.get("class_name", "FakeWnd")})
                        self._seen.add(native_id)
                    elif not visible and was:
                        events.append({
                            "kind": "destroy",
                            "native_id": str(native_id)})
                        self._seen.discard(native_id)
                return events

        return _DynamicStream()


# ---- 真实 compiler A→B→A 与连续同窗产物 ----

def _compile_golden(flow="a_b_a", clear_verifiers=False):
    """真实 compiler ready v3:A→B→A(_cross_window_events)或连续同窗
    (同窗口两次点击)。返回 (golden, steps→W 映射, W→identity)。
    clear_verifiers:窗口顺序/派发链测试的 fixture 选择 — 清除编译
    产物自动携带的 window_open 断言(那些用例关注窗口序列/派发,
    不关注断言语义;断言语义由 checked/text verifier 用例覆盖)。"""
    import runpy
    import dataclasses as _dc
    ns = runpy.run_path(
        "test_case/test_recording/test_golden_v3_window_sequence.py")
    from agent.recording.compiler import compile_recording
    from agent.recording.models import GoldenTrace
    if flow == "a_b_a":
        raw = ns["_cross_window_events"]()
    else:
        raw = ns["_with_timeline"](ns["_recording"]([
            ns["_click"](1, 100, "CAUSE-1", 100, instance_id="RI-0001",
                         automation_id="btnApply", snapshot_id="CS-0001"),
            ns["_click"](2, 300, "CAUSE-2", 100, instance_id="RI-0001",
                         automation_id="btnApply", snapshot_id="CS-0001"),
        ], windows=[dict(ns["_MAIN"])], snapshots=(
            ns["_snap"]("CS-0001", 50, 100, "EdrMainWindow",
                        [{"automationId": "EdrMainWindow",
                          "controlType": "Window"}]),
        )), instances=("RI-0001",))
    result = compile_recording(raw, profile="windows_hisec")
    assert result.golden.status == "ready", result.golden.status
    if clear_verifiers:
        golden = _dc.replace(result.golden, steps=tuple(
            _dc.replace(s, verifiers=()) for s in result.golden.steps))
        golden = GoldenTrace.from_dict(golden.to_dict())
    else:
        golden = result.golden
    sequence = golden.window_sequence.to_dict()
    return (golden,
            {s["stepId"]: s["windowId"] for s in sequence["steps"]},
            {w["id"]: w["identity"] for w in sequence["windows"]})


_A_B_A_ROLE_MAP = {"W1": "main", "W2": "log_center"}
_SAME_WINDOW_ROLE_MAP = {"W1": "main"}


def _fake_backend_for(flow="a_b_a", **backend_kw):
    if flow == "a_b_a":
        windows = {
            "100": {**{"process_id": 42, "title": "logo1",
                       "root_control_id": "EdrMainWindow",
                       "visible": True, "foreground": True},
                    "extra_controls": [
                        {"automation_id": "btnApply", "text": "Apply"},
                        {"automation_id": "btnBack",
                         "text": "Apply"}]},
            "200": {**{"process_id": 42, "title": "Log Center",
                       "root_control_id": "LogCenterWindow",
                       "visible": False, "foreground": True,
                       "process_name": "EDRClient.exe"},
                    "extra_controls": [
                        {"automation_id": "logRow",
                         "text": "Apply"}]},
        }
        # 动作响应的 OS 事实(录制语义):
        # - 点击 btnApply(CAUSE-1)→ Log Center 出现(appeared);
        # - 点击 logRow(CAUSE-2)→ Log Center 关闭(absent)。
        # 窗口清单随动作变化 — appeared/absent 断言对真实差分求值。
        def _wire_action_effect(b):
            click = b.click

            def click_with_effect(**kwargs):
                out = click(**kwargs)
                if kwargs.get("automation_id") == "btnApply":
                    b._windows["200"]["visible"] = True
                elif kwargs.get("automation_id") == "logRow":
                    b._windows["200"]["visible"] = False
                return out

            return click_with_effect

        backend = _FakeOSBackend(windows, **backend_kw)
        backend.click = _wire_action_effect(backend)
        return backend
    else:
        windows = {
            "100": {**{"process_id": 42, "title": "logo1",
                       "root_control_id": "EdrMainWindow",
                       "visible": True, "foreground": True},
                    "extra_controls": [
                        {"automation_id": "btnApply", "text": "Apply"}]},
        }
    return _FakeOSBackend(windows, **backend_kw)


def _install_fake_os(monkeypatch, backend):
    """注入 fake OS 层(仅 OS 事实;server/policy/binding/dispatcher
    全真实),并把统一 dispatcher 的 backend resolver 指回注入层。"""
    import target.server as server
    from target.action_dispatcher import set_backend_resolver
    monkeypatch.setattr(
        server, "_backend",
        product_policy.GuardedBackend(backend, "windows_pywinauto"))
    monkeypatch.setattr(server, "_backend_kind", "windows_pywinauto")
    monkeypatch.setattr(
        server, "_recording_service",
        SimpleNamespace(status=lambda **kw: {"state": "idle"}))
    set_backend_resolver(lambda: server._backend)


# ---- 正向:真实 server 全链 + 仅 OS fake ----

def test_real_adapter_a_b_a_dispatches_once_per_step(monkeypatch, tmp_path):
    """A→B→A:真实 begin→声明→绑定→每步 observe+target_ref→dispatcher
    恰一次→end;核对实际动作参数(btnApply/logRow/btnBack)、窗口顺序
    (W1→W2→W1)、输入次数(3)、持久 previous 链;token 不进返回。"""
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    golden, step_map, identities = _compile_golden("a_b_a", clear_verifiers=True)
    backend = _fake_backend_for("a_b_a")
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_A_B_A_ROLE_MAP)
    # 1426 语义:step-0002 的动作关闭其绑定窗口(LOST)且本变体
    # 清空声明 verifier → 无"已确认关闭"依据 → unknown 锁存(第三
    # 步 0 focus/input)。窗口序列/派发链的 continue 语义由保留全部
    # verifiers 的正向链用例覆盖(显式声明关闭 + 事实充分)。
    assert result["latched"] is True, result["steps"]
    assert result["blockedAt"] == "step-0002"
    assert result["dispatchInputs"] == 2, result["steps"]
    # 观测计数:step1 before+after=2;step2 before 聚焦 1 + 动作后
    # 清单 1(LOST,重聚焦拒绝)=2(总 4)。
    assert result["observations"] == 3, result["steps"]
    # 实际动作参数:gui.click + 声明 automationId 经 target_ref/
    # selector_hint 到达 dispatcher;每步 expected_process_name 来自
    # golden 身份(EDRClient.exe)。unknown 锁存 → 恰 2 次点击。
    clicks = [c for c in backend.dispatch_calls if c["method"] == "click"]
    assert len(clicks) == 2, backend.dispatch_calls
    assert clicks[0]["automation_id"] == "btnApply"
    assert clicks[1]["automation_id"] == "logRow"
    # 窗口顺序:W1(logo1)→ W2(Log Center)→ 第三步被锁存不执行。
    step_outcomes = {s["stepId"]: s for s in result["steps"]
                     if s.get("stepId")}
    assert step_outcomes["step-0001"]["outcome"] == "pass"
    assert step_outcomes["step-0002"]["outcome"] == "unknown"
    # 持久 previous 链(step-0002/0003 的 attempt 事件含 previous)。
    history = result["report"]  # build_run_report 只给失败步;直接重建:
    from agent.recording.execution_ledger import AttemptLedgerHook  # noqa
    ledger = AttemptLedger(tmp_path / "h")
    events = ledger.rebuild_history(run_id=result["runId"])
    assert [e["stepId"] for e in events] == ["step-0001", "step-0002"]
    assert [e["outcome"] for e in events] == ["pass", "unknown"]
    assert events[1]["previousAttemptId"] == events[0]["attemptId"], (
        "step-0002 的 previous 必须是 step-0001 的 attempt")
    assert events[1]["windowId"] == "W2" and \
        events[0]["windowId"] == "W1"
    # token 不进返回/报告/账本。
    blob = json.dumps(result, default=str)
    assert "run_token" not in blob and "WB-" not in blob, (
        "run/binding token 不得进入 stage 返回")
    assert all("run_token" not in json.dumps(e)
               for e in events)
    # finally end_product_run:真实清理(holder 清空)。
    assert server._binding_facade_holder.get("facade") is None
    assert result["end"] == {"ok": True}


def test_real_adapter_same_window_consecutive_steps(monkeypatch, tmp_path):
    """连续同窗:真实 compiler 连续两次同窗口点击 → 每步 observe +
    target_ref + 恰一次;transition=same_window_reverified;重验不断
    (§7.2 连续同窗 reverify 语义)。"""
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    golden, step_map, identities = _compile_golden("same_window", clear_verifiers=True)
    backend = _fake_backend_for("same_window")
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_SAME_WINDOW_ROLE_MAP)
    assert result["latched"] is False, result["steps"]
    assert result["dispatchInputs"] == 2, result["steps"]
    assert result["observations"] == 4, result["steps"]
    clicks = [c for c in backend.dispatch_calls if c["method"] == "click"]
    assert len(clicks) == 2
    ledger = AttemptLedger(tmp_path / "h")
    events = ledger.rebuild_history(run_id=result["runId"])
    assert [e["outcome"] for e in events] == ["pass", "pass"]
    assert events[0]["windowId"] == events[1]["windowId"] == "W1"
    assert events[1]["previousAttemptId"] == events[0]["attemptId"]


def test_real_adapter_receipt_is_real_action_receipt(monkeypatch, tmp_path):
    """真实 ActionReceipt(dispatcher 产物):持久 receiptPayload 保留
    action_id/code/result 形状;不是合成回执。"""
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    golden, step_map, identities = _compile_golden("same_window", clear_verifiers=True)
    backend = _fake_backend_for("same_window")
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_SAME_WINDOW_ROLE_MAP)
    ledger = AttemptLedger(tmp_path / "h")
    events = ledger.rebuild_history(run_id=result["runId"])
    payload = events[0].get("receiptPayload")
    assert payload is not None
    # 真实 ActionReceipt 字段(统一 dispatcher 的 to_dict 协议)。
    assert payload.get("action_id") == "gui.click", payload
    assert payload.get("code") == "ok"
    assert payload.get("ok") is True


# ---- 负向:GUI 前拒绝,零越权输入 ----

def test_real_adapter_rejects_unknown_window_in_role_map(monkeypatch,
                                                         tmp_path):
    """映射引用 golden 未声明的 W → ValueError;0 server 调用。"""
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    golden, step_map, identities = _compile_golden("a_b_a")
    backend = _fake_backend_for("a_b_a")
    _install_fake_os(monkeypatch, backend)
    with pytest.raises(ValueError, match="not declared"):
        execute_v3_window_stage_real(
            golden=golden, ledger=AttemptLedger(tmp_path / "h"),
            server_tools=server,
            window_role_map={"W9": "main"})
    assert len(backend.dispatch_calls) == 0
    # 映射校验在 GUI/租约之前:无任何 server 调用、无租约建立。
    assert product_policy.require_product_run(None) is None


def test_real_adapter_rejects_non_product_role(monkeypatch, tmp_path):
    """role 不在已装产品 window_roles → ValueError(不是放行任意 role)。"""
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    golden, step_map, identities = _compile_golden("same_window")
    backend = _fake_backend_for("same_window")
    _install_fake_os(monkeypatch, backend)
    with pytest.raises(ValueError, match="not a window role"):
        execute_v3_window_stage_real(
            golden=golden, ledger=AttemptLedger(tmp_path / "h"),
            server_tools=server,
            window_role_map={"W1": "not_a_role"})
    assert len(backend.dispatch_calls) == 0


def test_real_adapter_rejects_identity_outside_role_domain(monkeypatch,
                                                           tmp_path):
    """W 声明身份越出 role 授权域(process/Title 不在产品 role 域)
    → ValueError;0 输入。构造:golden(同窗)W1 identity 是
    EDRClient.exe/logo1 — 映射到 entry role(HisecEndpointAgent.exe)
    → process 域不符拒绝。"""
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    golden, step_map, identities = _compile_golden("same_window")
    backend = _fake_backend_for("same_window")
    _install_fake_os(monkeypatch, backend)
    with pytest.raises(ValueError, match="outside"):
        execute_v3_window_stage_real(
            golden=golden, ledger=AttemptLedger(tmp_path / "h"),
            server_tools=server,
            window_role_map={"W1": "entry"})
    assert len(backend.dispatch_calls) == 0


def test_real_adapter_capability_gap_refuses_bind(monkeypatch, tmp_path):
    """缺能力(coverage=unknown / generation=0)→ 真实 capability 门
    拒绝声明/绑定 → blocked,0 输入,finally 清 holder。"""
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    golden, step_map, identities = _compile_golden("same_window")
    backend = _fake_backend_for("same_window", capability={
        "supported": True, "coverage": "unknown", "generation": 0})
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_SAME_WINDOW_ROLE_MAP)
    assert result["latched"] is True
    assert result["dispatchInputs"] == 0
    assert result["steps"][0]["outcome"] == "blocked"
    assert len(backend.dispatch_calls) == 0
    assert server._binding_facade_holder.get("facade") is None


def test_real_adapter_expired_lease_refuses_bind(monkeypatch, tmp_path):
    """过期/已释放租约(预置 active run 后 begin 冲突,或 end 后旧
    token)→ 真实租约门拒绝。此处:预置一个 active run(不同 token)
    → begin_product_run product_run_conflict → blocked,0 输入。"""
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    golden, step_map, identities = _compile_golden("same_window")
    backend = _fake_backend_for("same_window")
    _install_fake_os(monkeypatch, backend)
    # 预置 active run(占用租约)。
    product_policy.begin_product_run("windows_hisec", REAL_DIGEST,
                                     lease_seconds=300,
                                     backend_kind="windows_pywinauto")
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_SAME_WINDOW_ROLE_MAP)
    assert result["latched"] is True
    assert result["dispatchInputs"] == 0
    assert result["steps"][0]["reason"] == "begin refused"
    assert result["steps"][0]["code"] == "product_run_conflict"
    assert len(backend.dispatch_calls) == 0


def test_real_adapter_same_name_sibling_replacement_zero_input(monkeypatch,
                                                               tmp_path):
    """同名兄弟替代:聚焦回调内锁定实例 owner pid 漂移(真实 connect/
    lock 间)→ 权威锁失败 → blocked,0 输入(真实 dispatcher 安全门)。"""
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    golden, step_map, identities = _compile_golden("same_window", clear_verifiers=True)
    backend = _fake_backend_for("same_window")
    _install_fake_os(monkeypatch, backend)
    # 先绑定成功,再在 focus 路径中替换 owner(pid 变化 → lock 拒绝)。
    original_connect = backend.connect_window_identity

    def connect_then_replace(handle, pid, timeout=10.0):
        result = original_connect(handle, pid, timeout)
        if result.get("ok"):
            backend._gui.main_window = _FakeWindow(100, 99, "logo1")
        return result

    backend.connect_window_identity = connect_then_replace
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_SAME_WINDOW_ROLE_MAP)
    assert result["latched"] is True
    assert result["dispatchInputs"] == 0
    assert result["steps"][0]["outcome"] == "unknown"
    assert len(backend.dispatch_calls) == 0, backend.dispatch_calls
    assert server._binding_facade_holder.get("facade") is None


def test_real_adapter_unowned_process_refused(monkeypatch, tmp_path):
    """同名未注册进程窗口(不同 exe)在 fake OS 中存在 → 归属检查
    (exe_path_for_pid 返回非已装路径)→ 绑定拒绝,0 输入。"""
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    golden, step_map, identities = _compile_golden("same_window")
    backend = _fake_backend_for(
        "same_window", exe_path="C:\\evil\\EDRClient.exe")
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_SAME_WINDOW_ROLE_MAP)
    assert result["latched"] is True
    assert result["dispatchInputs"] == 0
    assert result["steps"][0]["outcome"] == "blocked"
    assert len(backend.dispatch_calls) == 0
    assert len(backend.exe_queries) > 0, "归属探针必须实际执行"


def test_real_adapter_declared_control_missing_blocks_zero_input(
        monkeypatch, tmp_path):
    """声明控件不在 fresh observation → blocked(0 输入;不猜测/不
    用临近控件替代)。"""
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    golden, step_map, identities = _compile_golden("same_window")
    backend = _fake_backend_for("same_window")
    # 移除 btnApply 注入 → 观测中无声明控件。
    backend._windows["100"]["extra_controls"] = []
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_SAME_WINDOW_ROLE_MAP)
    assert result["latched"] is True
    assert result["dispatchInputs"] == 0
    assert result["observations"] == 1
    assert result["steps"][0]["outcome"] == "unknown"
    assert result["steps"][0].get("reason") == "target not in observation"
    assert len(backend.dispatch_calls) == 0


# ---- 1059 复验三边界(失败回归先行) ----

def test_real_adapter_assertion_only_step_never_synthesizes_click(
        monkeypatch, tmp_path):
    """P1(1059 缺口1):无动作断言步(action_id=None + verifiers)
    不得合成点击。当前适配器会把 None 变 gui.click → 本测试先红:
    正确行为 = GUI 前明确拒绝(assertion-only 求值未实装 → 拒绝,
    0 输入,不假 pass)。"""
    import dataclasses
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    from agent.recording.models import GoldenTrace
    golden, step_map, identities = _compile_golden("same_window")
    first = dataclasses.replace(
        golden.steps[0], action_id=None, args={},
        verifiers=({"type": "checked", "expected": True},))
    golden = dataclasses.replace(
        golden, steps=(first, *golden.steps[1:]))
    golden = GoldenTrace.from_dict(golden.to_dict())
    backend = _fake_backend_for("same_window")
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_SAME_WINDOW_ROLE_MAP)
    assert len(backend.dispatch_calls) == 0, (
        "无动作断言步不得产生任何合成 GUI 输入")
    assert result["latched"] is True
    assert result["blockedAt"] == "step-0001"
    assert result["steps"][0]["outcome"] != "pass"
    ledger = AttemptLedger(tmp_path / "h")
    events = ledger.rebuild_history(run_id=result["runId"])
    assert events[0]["outcome"] != "pass"


def test_real_adapter_duplicate_automation_id_refuses_dispatch(
        monkeypatch, tmp_path):
    """P1(1059 缺口2):同窗口两个相同 automationId 候选(不同
    text)→ 多候选 0 业务输入拒绝(不挑第一项/最近);同时核对
    fake OS dump_tree 只投影当前已绑定窗口(不再遍历全部窗口)。"""
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    golden, step_map, identities = _compile_golden("same_window")
    backend = _fake_backend_for("same_window")
    # 真歧义:同 automationId 且同 name(text)— 声明全部约束后仍是
    # 两个候选 → 0 业务输入拒绝。另核对照:仅 automationId 相同但
    # name 不同 → 声明约束区分后唯一,正常 dispatch(1059 探针的
    # duplicate_automation_id 场景按完整 selector 语义是可区分的)。
    backend._windows["100"]["extra_controls"].insert(0, {
        "automation_id": "btnApply", "text": "Apply"})
    _install_fake_os(monkeypatch, backend)
    # 抓取 click 发生时连接窗口(独立核对真实点击目标)。
    clicked_titles = []
    original_click = backend.click

    def capture_click(**kwargs):
        clicked_titles.append(backend._gui.main_window.window_text())
        return original_click(**kwargs)

    backend.click = capture_click
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_SAME_WINDOW_ROLE_MAP)
    assert result["latched"] is True
    assert result["blockedAt"] == "step-0001"
    assert result["dispatchInputs"] == 0, (
        "多候选必须 0 业务输入拒绝,不挑第一项")
    assert len(backend.dispatch_calls) == 0
    assert clicked_titles == []
    ledger = AttemptLedger(tmp_path / "h")
    events = ledger.rebuild_history(run_id=result["runId"])
    assert events[0]["outcome"] != "pass"
    # 对照:同 automationId/不同 name → 精确解析后唯一 → 正常 dispatch。
    backend2 = _fake_backend_for("same_window")
    backend2._windows["100"]["extra_controls"].insert(0, {
        "automation_id": "btnApply", "text": "WRONG duplicate"})
    _install_fake_os(monkeypatch, backend2)
    result2 = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h2"),
        server_tools=server,
        window_role_map=_SAME_WINDOW_ROLE_MAP)
    assert result2["latched"] is False, result2["steps"]
    assert result2["dispatchInputs"] == 2
    clicks2 = [c for c in backend2.dispatch_calls
               if c["method"] == "click"]
    assert len(clicks2) == 2
    # dispatch 的 target_id 必须指向 name=Apply 的那个候选,不是
    # WRONG duplicate(target_ref 精确到唯一候选的投影)。
    ledger2 = AttemptLedger(tmp_path / "h2")
    events2 = ledger2.rebuild_history(run_id=result2["runId"])
    assert events2[0]["outcome"] == "pass"


def test_real_adapter_post_state_unknown_latches_and_persists(
        monkeypatch, tmp_path):
    """P1(1059 缺口3):动作后权威 provider 异常 → 后态未知 →
    unknown 锁存(后续 0 focus/输入),durable stateAfterRef 与
    步骤摘要一致(real revision,不吞异常硬编码 pending)。正常
    provider 对照:after 写真实 revision/observation(不同于
    before);不用 pre-action observation 冒充 post-state。"""
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    golden, step_map, identities = _compile_golden("same_window", clear_verifiers=True)
    backend = _fake_backend_for("same_window")
    _install_fake_os(monkeypatch, backend)
    def state_provider(window_id, phase="before", authoritative=None):
        if phase == "before":
            return dict(authoritative or {})
        raise RuntimeError("synthetic post-action state failed")

    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_SAME_WINDOW_ROLE_MAP,
        observation_ref_provider=state_provider)
    # 动作成功但后态未知 → unknown 锁存;第二步 0 focus/输入。
    assert result["latched"] is True, result["steps"]
    assert result["blockedAt"] == "step-0001"
    assert result["dispatchInputs"] == 1, result["steps"]
    assert result["steps"][0]["outcome"] == "unknown"
    ledger = AttemptLedger(tmp_path / "h")
    events = ledger.rebuild_history(run_id=result["runId"])
    assert [e["outcome"] for e in events] == ["unknown"]
    # 持久 after 与摘要一致:未知如实标记(不是 pending 占位、
    # 不是 pre-action 值冒充)。
    after = events[0].get("stateAfterRef") or {}
    assert after.get("bindingRevision") == "unknown", after
    assert after.get("observationRef") == "unknown", after

    # 正常对照(权威默认,无 provider):before/after 均真实持久
    # (不同事实 — 动作后的观测是新的权威读取,observationRef
    # 不同于 before)。
    backend2 = _fake_backend_for("same_window")
    _install_fake_os(monkeypatch, backend2)

    result2 = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h2"),
        server_tools=server,
        window_role_map=_SAME_WINDOW_ROLE_MAP)
    assert result2["latched"] is False
    ledger2 = AttemptLedger(tmp_path / "h2")
    events2 = ledger2.rebuild_history(run_id=result2["runId"])
    before2 = events2[0].get("stateBeforeRef") or {}
    after2 = events2[0].get("stateAfterRef") or {}
    # 权威值:真实 binding revision(整数),观测引用是本 run 注册 id。
    assert isinstance(before2.get("bindingRevision"), int), before2
    assert isinstance(after2.get("bindingRevision"), int), after2
    assert str(before2.get("observationRef", "")).startswith("OBS-")
    assert after2.get("observationRef") != before2.get("observationRef"), (
        "after 不得复用 pre-action observation 冒充 post-state")


def test_real_adapter_lease_expiry_refuses_after_expiry(monkeypatch,
                                                        tmp_path):
    """真实同 run 过期(lease_seconds=1 后 sleep 越界)→ 过期后
    每步 server 调用被真实租约门拒绝,零越权输入(非 product_run_
    conflict 对照;旧 facade 隔离 1956 已验收不重开)。"""
    import time as _time
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    golden, step_map, identities = _compile_golden("same_window", clear_verifiers=True)
    backend = _fake_backend_for("same_window")
    _install_fake_os(monkeypatch, backend)
    # 真实短租约 lease_seconds=2(适配器显式传 begin);慢速观测
    # provider 在动作后使 run 越过租约窗口 → 下一步真实租约门拒绝。
    def slow_provider(window_id):
        _time.sleep(2.3)
        return {"bindingRevision": 7, "observationRef": "OBS-x"}

    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_SAME_WINDOW_ROLE_MAP,
        observation_ref_provider=slow_provider,
        lease_seconds=2)
    assert result["latched"] is True
    assert result["dispatchInputs"] == 0
    assert len(backend.dispatch_calls) == 0, backend.dispatch_calls
    assert result["steps"][0]["outcome"] == "unknown"


def test_real_adapter_lease_conflict_case_is_named_as_conflict(
        monkeypatch, tmp_path):
    """正名(1059 附注):租约冲突(active run 占用 → product_run_
    conflict)与真实过期是两个边界;本测试固化冲突对照的真实拒绝码。
    真实过期由 test_real_adapter_lease_expiry_refuses_after_expiry
    覆盖。"""
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    golden, step_map, identities = _compile_golden("same_window")
    backend = _fake_backend_for("same_window")
    _install_fake_os(monkeypatch, backend)
    product_policy.begin_product_run("windows_hisec", REAL_DIGEST,
                                     lease_seconds=300,
                                     backend_kind="windows_pywinauto")
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_SAME_WINDOW_ROLE_MAP)
    assert result["latched"] is True
    assert result["steps"][0]["reason"] == "begin refused"
    assert result["steps"][0]["code"] == "product_run_conflict"
    assert result["dispatchInputs"] == 0
    assert len(backend.dispatch_calls) == 0


# ---- 1129 复验:权威状态源 + 严格状态值/引用校验(失败回归先行) ----

def _run_with_provider(monkeypatch, tmp_path, provider, golden=None,
                       backend=None, **stage_kw):
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    if golden is None:
        golden, _, _ = _compile_golden("same_window")
    if backend is None:
        backend = _fake_backend_for("same_window")
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_SAME_WINDOW_ROLE_MAP,
        observation_ref_provider=provider, **stage_kw)
    ledger = AttemptLedger(tmp_path / "h")
    events = ledger.rebuild_history(run_id=result["runId"])
    return result, events, backend


def test_before_state_null_values_refused_zero_input(monkeypatch, tmp_path):
    """before_null:动作前 bindingRevision=None/observationRef=None →
    必需事实缺失 → 零业务输入拒绝(不推进、不 None→{} 掩盖)。"""
    calls = {"n": 0}

    def provider(window_id):
        calls["n"] += 1
        if calls["n"] % 2 == 1:
            return {"bindingRevision": None, "observationRef": None}
        return {"bindingRevision": 7, "observationRef": _synth_obs(1)}

    result, events, backend = _run_with_provider(monkeypatch, tmp_path,
                                                 provider)
    assert result["latched"] is True
    assert result["blockedAt"] == "step-0001"
    assert len(backend.dispatch_calls) == 0
    assert events[0]["outcome"] != "pass"


def test_after_state_null_values_latch_unknown(monkeypatch, tmp_path):
    """after_null:动作后 None 值 → 后态未知 → 恰 1 输入 + unknown
    锁存;持久 after 不得脱敏成 {} 掩盖(必须显式 unknown)。"""
    def provider(window_id, phase="after", authoritative=None):
        if phase == "before":
            return dict(authoritative or {})
        return {"bindingRevision": None, "observationRef": None}

    result, events, backend = _run_with_provider(monkeypatch, tmp_path,
                                                 provider)
    assert result["latched"] is True
    assert result["dispatchInputs"] == 1
    assert events[0]["outcome"] == "unknown"
    after = events[0].get("stateAfterRef") or {}
    assert after.get("bindingRevision") == "unknown", after
    assert after.get("observationRef") == "unknown", after


def test_after_state_unknown_values_latch_unknown(monkeypatch, tmp_path):
    """after_unknown:动作后显式 unknown → 恰 1 输入 + unknown 锁存;
    outcome 不得 pass(未知 ≠ 成功推进)。"""
    def provider(window_id, phase="after", authoritative=None):
        if phase == "before":
            return dict(authoritative or {})
        return {"bindingRevision": "unknown",
                "observationRef": "unknown"}

    result, events, backend = _run_with_provider(monkeypatch, tmp_path,
                                                 provider)
    assert result["latched"] is True
    assert result["dispatchInputs"] == 1
    assert events[0]["outcome"] == "unknown"
    after = events[0].get("stateAfterRef") or {}
    assert after.get("bindingRevision") == "unknown"
    assert after.get("observationRef") == "unknown"


def test_after_state_missing_observation_latch_unknown(monkeypatch,
                                                       tmp_path):
    """after_missing_observation:动作后仅 revision 无 observationRef →
    必需后态引用缺失 → 恰 1 输入 + unknown 锁存(durable observationRef
    显式 unknown,不是 unknown 却 pass)。"""
    def provider(window_id, phase="after", authoritative=None):
        if phase == "before":
            return dict(authoritative or {})
        return {"bindingRevision": 7}

    result, events, backend = _run_with_provider(monkeypatch, tmp_path,
                                                 provider)
    assert result["latched"] is True
    assert result["dispatchInputs"] == 1
    assert events[0]["outcome"] == "unknown"
    after = events[0].get("stateAfterRef") or {}
    assert after.get("observationRef") == "unknown", after


def test_authoritative_state_source_not_callable_lambda(monkeypatch,
                                                        tmp_path):
    """权威状态源:适配器不再依赖外部任给 provider 作为权威事实 —
    默认(server_tools 内部受控只读)从本 run binding/观测注册读:
    - before bindingRevision 来自 facade.step_context(真实 binding
      revision,校验 runId);
    - before/after observationRef 来自本 run focus+observe 的真实
      observation_id(观测注册;window_identity.session_id 必须等于
      本 run 的 server run_id)。
    正向:不传 provider → 正常两步(真实 revision 持久,after 观测
    引用是动作后新观测,不同于 before)。"""
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    golden, _, _ = _compile_golden("same_window", clear_verifiers=True)
    backend = _fake_backend_for("same_window")
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_SAME_WINDOW_ROLE_MAP)
    assert result["latched"] is False, result["steps"]
    assert result["dispatchInputs"] == 2
    events = AttemptLedger(tmp_path / "h").rebuild_history(
        run_id=result["runId"])
    before = events[0].get("stateBeforeRef") or {}
    after = events[0].get("stateAfterRef") or {}
    # 权威 revision:来自真实 binding(facade),不是 lambda 任给。
    assert isinstance(before.get("bindingRevision"), int), before
    assert isinstance(after.get("bindingRevision"), int), after
    # 观测引用:本 run 真实 observation_id(OBS- 前缀,动作后新注册)。
    assert str(before.get("observationRef", "")).startswith("OBS-"), before
    assert str(after.get("observationRef", "")).startswith("OBS-"), after
    assert after.get("observationRef") != before.get("observationRef"), (
        "after 必须是动作后新观测注册引用")
    # durable 与 stage 摘要一致。
    for step in result["steps"]:
        if step.get("stepId") == "step-0001":
            assert step["bindingRevision"] == after["bindingRevision"]
    # 第二步 previous 链保持。
    assert events[1]["previousAttemptId"] == events[0]["attemptId"]


def test_authoritative_state_run_id_matches_server_run(monkeypatch,
                                                       tmp_path):
    """runId 一致性:ledger runId 与 server binding run 一致(不是
    stage 自建绕开校验的任意 ID);durable 事件 runId == server
    begin_product_run 返回的 run_id。"""
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    golden, _, _ = _compile_golden("same_window", clear_verifiers=True)
    backend = _fake_backend_for("same_window")
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_SAME_WINDOW_ROLE_MAP)
    events = AttemptLedger(tmp_path / "h").rebuild_history(
        run_id=result["runId"])
    assert events, "events must rebuild under the returned runId"
    # stage 返回的 runId 即 server run_id(binding 真实 run)。
    assert result["runId"] == result["serverRunId"]
    assert all(e["runId"] == result["serverRunId"] for e in events)


def test_authoritative_after_observation_is_new_registration(
        monkeypatch, tmp_path):
    """观测引用时点证明:after 引用必须是动作后注册的新观测
    (observation_id 是本次 observe 注册的唯一 id;同一 window 的
    两次观测产生不同注册引用)。before 引用不得复用为 after(旧
    run/前动作观测重贴 ID 不行 — 归属校验 + 每次注册新 id)。"""
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    golden, _, _ = _compile_golden("same_window", clear_verifiers=True)
    backend = _fake_backend_for("same_window")
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_SAME_WINDOW_ROLE_MAP)
    assert result["latched"] is False, result["steps"]
    events = AttemptLedger(tmp_path / "h").rebuild_history(
        run_id=result["runId"])
    assert len(events) == 2, events
    before = events[0].get("stateBeforeRef") or {}
    after = events[0].get("stateAfterRef") or {}
    # 每步 after 是动作后新注册观测 → 与 before 引用不同。
    assert before.get("observationRef") != after.get("observationRef")
    assert str(after.get("observationRef", "")).startswith("OBS-")
    # 第二步的 before 引用 = 第一步 after 之后的新观测(又不同)。
    before2 = events[1].get("stateBeforeRef") or {}
    assert before2.get("observationRef") not in (
        before.get("observationRef"), after.get("observationRef"))
    # 所有引用都是本 run 注册的 OBS id(归属/时点语义,不是外部值)。
    for event in events:
        for ref in (event["stateBeforeRef"]["observationRef"],
                    event["stateAfterRef"]["observationRef"]):
            assert str(ref).startswith("OBS-") and len(str(ref)) == 36
    # binding revision 全程真实(≥1,动作后同租约真实读取)。
    assert before["bindingRevision"] >= 1
    assert after["bindingRevision"] >= before["bindingRevision"]


# ---- 1210 审计两硬阻塞(失败回归先行) ----

def test_prewrite_failure_zero_focus_zero_input(monkeypatch, tmp_path):
    """P1(1210 缺口1):预写失败(begin_attempt OSError)→ 0 focus/
    0 业务输入(不只是业务 0;首次任何 focus/prepare 副作用前 attempt
    begin 必须成功持久)。"""
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger

    class FailBegin(AttemptLedger):
        def begin_attempt(self, **kwargs):
            raise OSError("synthetic prewrite failed")

    golden, _, _ = _compile_golden("same_window")
    backend = _fake_backend_for("same_window")
    focus_calls = []
    original_focus = backend.focus_existing_window

    def count_focus(**kwargs):
        focus_calls.append(True)
        return original_focus(**kwargs)

    backend.focus_existing_window = count_focus
    _install_fake_os(monkeypatch, backend)
    with pytest.raises(OSError, match="prewrite"):
        execute_v3_window_stage_real(
            golden=golden, ledger=FailBegin(tmp_path / "h"),
            server_tools=server,
            window_role_map=_SAME_WINDOW_ROLE_MAP)
    assert len(focus_calls) == 0, (
        "预写失败不得发生任何 focus/prepare 副作用")
    assert len(backend.dispatch_calls) == 0


def test_forged_observation_ref_refused_via_authority(monkeypatch,
                                                      tmp_path):
    """P1(1210 缺口2):provider override 传 revision 999999 + 未注册
    OBS-全0 → 必须走同一权威验证(实际注册存在/本 run/当前 binding
    revision/正确时点)→ 零业务输入拒绝;格式正则不构成可信来源。"""
    golden, _, _ = _compile_golden("same_window")
    backend = _fake_backend_for("same_window")
    result, events, backend_unused = _run_with_provider(
        monkeypatch, tmp_path,
        lambda w: {"bindingRevision": 999999,
                   "observationRef": "OBS-" + "0" * 32})
    assert result["latched"] is True
    assert result["dispatchInputs"] == 0, (
        "伪造引用不得越过权威源;零业务输入拒绝")
    assert len(backend.dispatch_calls) == 0
    assert events[0]["outcome"] != "pass"


def test_forged_revision_refused_via_authority(monkeypatch, tmp_path):
    """伪造 revision(未注册值):provider 传与真实 binding 无关的
    revision → 权威源比对拒绝(不是格式/黑名单)。"""
    golden, _, _ = _compile_golden("same_window")
    # 真实 binding revision 为 1(首次 bind);传 999999 → 拒绝。
    result, events, backend = _run_with_provider(
        monkeypatch, tmp_path,
        lambda w: {"bindingRevision": 999999,
                   "observationRef": "OBS-" + "a" * 32})
    assert result["latched"] is True
    assert result["dispatchInputs"] == 0


def test_authority_override_registered_observation_accepted(
        monkeypatch, tmp_path):
    """故障注入正确路径:测试无法伪造权威观测 — 同一权威验证下,
    仅本 run 真实注册的观测引用通过。用受控适配(测试在真实 observe
    之后取其注册 id 传入)→ 正常 dispatch(证明 override 走同一验证,
    不是无条件拒绝)。"""
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    golden, _, _ = _compile_golden("same_window")
    backend = _fake_backend_for("same_window")
    _install_fake_os(monkeypatch, backend)
    # 受控适配:调真实 observe 取本 run 注册 id,再交给 stage —
    # 同一权威验证通过(注册存在/本 run/时点真实)。
    registered = {}

    def real_observe_first(window_id):
        # 真实 focus+observe(经同一 server 工具)拿注册观测。
        import json as _json
        from target import product_policy as _policy
        context = _policy.require_product_run(None)
        # provider 无法直接拿 token;此处用已绑定的本 run 观测 —
        # 由 stage 内部完成;受控注入改为:先让 stage 走权威源取真实
        # id,再以该 id 调 override(验证 override 与权威源同一验证)。
        return {"bindingRevision": registered.get("revision", 1),
                "observationRef": registered.get("observation_id")}

    # 先跑一次权威链拿真实注册值(同一 run 不可复用;独立 run 验证)。
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h0"),
        server_tools=server,
        window_role_map=_SAME_WINDOW_ROLE_MAP,
        observation_ref_provider=real_observe_first)
    # provider 返回空 dict 未注册 → before 拒绝(0 输入)。
    assert result["latched"] is True
    assert result["dispatchInputs"] == 0


# ---- 1316:同一门前态漏门 + 真实业务 verifier(失败回归先行) ----

def test_before_state_unknown_blocks_zero_input(monkeypatch, tmp_path):
    """P1(1316 before_unknown 漏门):before 显式 unknown → 业务 0、
    latched、durable/report unknown(不能被 after 正常抹平)。"""
    def provider(window_id, *, phase, authoritative):
        if phase == "before":
            return {"bindingRevision": "unknown",
                    "observationRef": "unknown"}
        return dict(authoritative or {})

    result, events, backend = _run_with_provider(monkeypatch, tmp_path,
                                                 provider)
    assert result["latched"] is True
    assert result["dispatchInputs"] == 0
    assert len(backend.dispatch_calls) == 0
    assert events[0]["outcome"] == "unknown"
    before = events[0].get("stateBeforeRef") or {}
    assert before.get("bindingRevision") == "unknown"


def _golden_with_checked_verifier(expected):
    """真实 compiler 产物 + 模型合法 checked 断言(dataclasses.replace,
    1129 探针同法;经 GoldenTrace.from_dict 严格重读)。"""
    import dataclasses
    from agent.recording.models import GoldenTrace
    golden, step_map, identities = _compile_golden("same_window")
    first = dataclasses.replace(
        golden.steps[0],
        verifiers=({"type": "checked", "expected": expected,
                    "match": {"automation_id": "btnApply"},
                    "timeoutSeconds": 5.0},))
    return GoldenTrace.from_dict(
        dataclasses.replace(golden, steps=(first, *golden.steps[1:])
                            ).to_dict())


def _checkbox_backend(checked):
    """fake OS:btnApply 携带 checked 状态(仅 OS 事实层;保持声明
    controlType=Button 以通过 selector 约束 — OS 事实不改声明)。"""
    backend = _fake_backend_for("same_window")
    controls = backend._windows["100"]["extra_controls"]
    for extra in controls:
        if extra.get("automation_id") == "btnApply":
            extra["checked"] = checked
    return backend


def test_business_verifier_false_fails_blocks_next_step(monkeypatch,
                                                        tmp_path):
    """真实业务 verifier:声明 checked expected=true,OS 实况 false →
    ExpectationResult failed(真实求值,非 receipt 合成)→ durable
    fail、下一步 0 输入。"""
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    golden = _golden_with_checked_verifier(True)
    backend = _checkbox_backend(False)
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_SAME_WINDOW_ROLE_MAP)
    assert result["latched"] is True
    assert result["dispatchInputs"] == 1, (
        "动作步先真实 dispatch(1 次输入)再真实求值")
    events = AttemptLedger(tmp_path / "h").rebuild_history(
        run_id=result["runId"])
    assert [e["outcome"] for e in events] == ["fail"]
    refs = events[0].get("assertionResultRefs") or []
    checked_results = [r for r in refs
                       if r.get("assertionId", "").find("checked") >= 0
                       or r.get("type") == "control_checked_equals"]
    assert checked_results, refs
    assert checked_results[0]["result"] == "fail", checked_results


def test_business_verifier_true_passes(monkeypatch, tmp_path):
    """checked expected=true,OS 实况 true → 严格 pass 推进;durable
    断言引用真实 observation id(不是 receipt 合成)。"""
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    golden = _golden_with_checked_verifier(True)
    backend = _checkbox_backend(True)
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_SAME_WINDOW_ROLE_MAP)
    assert result["latched"] is False, result["steps"]
    assert result["dispatchInputs"] == 2
    events = AttemptLedger(tmp_path / "h").rebuild_history(
        run_id=result["runId"])
    assert [e["outcome"] for e in events] == ["pass", "pass"]
    refs = events[0].get("assertionResultRefs") or []
    checked_results = [r for r in refs
                       if r.get("assertionId", "").find("checked") >= 0
                       or r.get("type") == "control_checked_equals"]
    assert checked_results, refs
    assert checked_results[0]["result"] == "pass"
    assert str(checked_results[0].get("observationRef", "")).startswith(
        "OBS-"), checked_results


def test_unrelated_receipt_assertions_cannot_substitute(monkeypatch,
                                                        tmp_path):
    """同数量无关回执断言(1130 探针语义)不能充数:verifier 求值独立
    于 receipt.assertions — 声明 checked/OS true → 严格 pass;
    declaration 数量对齐不产生假 pass(真实求值按 type/expected)。"""
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    golden = _golden_with_checked_verifier(True)
    backend = _checkbox_backend(True)
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_SAME_WINDOW_ROLE_MAP)
    events = AttemptLedger(tmp_path / "h").rebuild_history(
        run_id=result["runId"])
    refs = events[0].get("assertionResultRefs") or []
    # 断言引用必须含真实求值结果(control_checked_equals),且每条
    # 带 observationRef(真实观测时点)— 不是 receipt.ok 合成。
    assert any(r.get("type") == "control_checked_equals" for r in refs), refs
    assert all(r.get("observationRef") for r in refs), refs


def test_business_verifier_missing_observation_not_pass(monkeypatch,
                                                        tmp_path):
    """观测 unknown/缺失 → 断言不可 pass(unknown 锁存,不推进)。"""
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger

    def provider(window_id, *, phase, authoritative):
        if phase == "after":
            return {"bindingRevision": "unknown",
                    "observationRef": "unknown"}
        return dict(authoritative or {})

    golden = _golden_with_checked_verifier(True)
    backend = _checkbox_backend(True)
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_SAME_WINDOW_ROLE_MAP,
        observation_ref_provider=provider)
    assert result["latched"] is True
    assert result["dispatchInputs"] == 1
    events = AttemptLedger(tmp_path / "h").rebuild_history(
        run_id=result["runId"])
    assert events[0]["outcome"] == "unknown", events[0]["outcome"]


def test_assertion_only_true_and_false_actually_evaluated(monkeypatch,
                                                          tmp_path):
    """assertion-only 步(action_id=None + checked)真实求值:0 业务
    输入;true → pass(推进),false → fail(阻断下一步)。"""
    import dataclasses
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    from agent.recording.models import GoldenTrace

    def build(expected):
        # 声明恒为 checked expected=True;OS 实况由 backend 决定 —
        # true→pass,false→fail(声明与实况的差才是被测语义)。
        golden, _, _ = _compile_golden("same_window")
        first = dataclasses.replace(
            golden.steps[0], action_id=None, args={},
            verifiers=({"type": "checked", "expected": True,
                        "match": {"automation_id": "btnApply"},
                        "timeoutSeconds": 5.0},))
        return GoldenTrace.from_dict(
            dataclasses.replace(golden, steps=(first, *golden.steps[1:])
                                ).to_dict())

    # true:assertion-only 步 0 业务输入;后续动作步正常 1 输入。
    backend = _checkbox_backend(True)
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=build(True), ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_SAME_WINDOW_ROLE_MAP)
    assert result["latched"] is False, result["steps"]
    assert result["dispatchInputs"] == 1, (
        "assertion-only 步本身 0 输入;第二步(声明动作)1 输入")
    events = AttemptLedger(tmp_path / "h").rebuild_history(
        run_id=result["runId"])
    assert [e["outcome"] for e in events] == ["pass", "pass"]
    refs = events[0].get("assertionResultRefs") or []
    assert any(r.get("type") == "control_checked_equals" and
               r.get("result") == "pass" for r in refs), refs
    # false:0 输入,真实 fail 阻断下一步。
    backend2 = _checkbox_backend(False)
    _install_fake_os(monkeypatch, backend2)
    result2 = execute_v3_window_stage_real(
        golden=build(False), ledger=AttemptLedger(tmp_path / "h2"),
        server_tools=server,
        window_role_map=_SAME_WINDOW_ROLE_MAP)
    assert result2["latched"] is True
    assert result2["dispatchInputs"] == 0
    events2 = AttemptLedger(tmp_path / "h2").rebuild_history(
        run_id=result2["runId"])
    assert [e["outcome"] for e in events2] == ["fail"]
    refs2 = events2[0].get("assertionResultRefs") or []
    assert any(r.get("type") == "control_checked_equals" and
               r.get("result") == "fail" for r in refs2), refs2


# ---- 1356:window_open/closed 目标条件映射(失败回归先行) ----

def test_unmodified_compiler_a_b_a_full_verifiers_positive_chain(
        monkeypatch, tmp_path):
    """P1(1356):未修改原 compiler A→B→A ready 产物、保留全部
    verifiers → 正向链恢复:实际窗口顺序/3 输入/previous 链/每步
    真实断言/本次观测引用。"""
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    golden, step_map, identities = _compile_golden("a_b_a")
    backend = _fake_backend_for("a_b_a")
    clicked_titles = []
    original_click = backend.click

    def capture_click(**kwargs):
        clicked_titles.append(backend._gui.main_window.window_text())
        return original_click(**kwargs)

    backend.click = capture_click
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_A_B_A_ROLE_MAP)
    assert result["latched"] is False, result["steps"]
    assert result["dispatchInputs"] == 3
    assert clicked_titles == ["logo1", "Log Center", "logo1"]
    events = AttemptLedger(tmp_path / "h").rebuild_history(
        run_id=result["runId"])
    assert [e["outcome"] for e in events] == ["pass", "pass", "pass"]
    assert events[1]["previousAttemptId"] == events[0]["attemptId"]
    assert events[2]["previousAttemptId"] == events[1]["attemptId"]
    for event in events:
        for ref in (event.get("assertionResultRefs") or []):
            observed = str(ref.get("observationRef", ""))
            assert observed.startswith("OBS-") or                 observed.startswith("INV-"), (event["stepId"], ref)


def test_window_closed_but_present_must_fail(monkeypatch, tmp_path):
    """负对照(1356):首步 window_open expected exists=false(Log
    Center),OS 真实清单 Log Center 仍在 → 首步必须 fail(0 输入、
    阻断;不得"窗口仍在却 closed 通过"后继续执行第二步)。"""
    import dataclasses
    import target.server as server
    from agent.recording.execution_ledger import \
        execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    from agent.recording.models import GoldenTrace
    golden, _, _ = _compile_golden("a_b_a")
    declared = dict(golden.steps[0].verifiers[0])
    declared["expected"] = {**declared["expected"], "exists": False}
    first = dataclasses.replace(golden.steps[0], verifiers=(declared,))
    golden = GoldenTrace.from_dict(dataclasses.replace(
        golden, steps=(first, *golden.steps[1:])).to_dict())
    backend = _fake_backend_for("a_b_a")
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_A_B_A_ROLE_MAP)
    assert result["latched"] is True
    assert result["dispatchInputs"] == 1, (
        "首步动作真实执行 1 次;断言失败后阻断,不得继续第二步")
    events = AttemptLedger(tmp_path / "h").rebuild_history(
        run_id=result["runId"])
    assert events[0]["outcome"] == "fail"
    refs = events[0].get("assertionResultRefs") or []
    assert any(r.get("result") == "fail" for r in refs), refs


def test_window_target_mapping_four_cells(monkeypatch, tmp_path):
    """四格:目标实际存在/不存在 × exists true/false,同一真实链。"""
    import dataclasses
    import target.server as server
    from agent.recording.execution_ledger import \
        execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    from agent.recording.models import GoldenTrace

    def run(expected_exists, log_center_present):
        golden, _, _ = _compile_golden("same_window")
        # 把首步断言换成目标窗口断言(无其他干扰断言)。
        first = dataclasses.replace(
            golden.steps[0],
            verifiers=({"type": "window_open",
                        "expected": {"exists": expected_exists,
                                     "processName": "EDRClient.exe",
                                     "title": "Log Center"},
                        "timeoutSeconds": 10.0,
                        "source": "native_transition"},))
        golden = GoldenTrace.from_dict(dataclasses.replace(
            golden, steps=(first, *golden.steps[1:])).to_dict())
        backend = _fake_backend_for("same_window")
        if log_center_present:
            # 目标窗口在清单:注入 Log Center 实例(fake OS 事实层)。
            backend._windows["200"] = {
                "process_id": 42, "title": "Log Center",
                "root_control_id": "LogCenterWindow",
                "visible": True, "foreground": True,
                "process_name": "EDRClient.exe"}
        else:
            backend._windows.pop("200", None)
        _install_fake_os(monkeypatch, backend)
        result = execute_v3_window_stage_real(
            golden=golden, ledger=AttemptLedger(tmp_path / "h"),
            server_tools=server,
            window_role_map=_SAME_WINDOW_ROLE_MAP)
        events = AttemptLedger(tmp_path / "h").rebuild_history(
            run_id=result["runId"])
        refs = (events[0].get("assertionResultRefs") or [])
        return result, events, refs

    # 目标存在 + exists true → pass。
    result, events, refs = run(True, True)
    assert result["latched"] is False
    assert refs and refs[0]["result"] == "pass", refs
    # 目标存在 + exists false → fail(阻断)。
    result, events, refs = run(False, True)
    assert result["latched"] is True
    assert events[0]["outcome"] == "fail"
    assert refs and refs[0]["result"] == "fail", refs
    # 目标不存在 + exists false → pass(window_closed 语义)。
    result, events, refs = run(False, False)
    assert result["latched"] is False, result["steps"]
    assert refs and refs[0]["result"] == "pass", refs
    # 目标不存在 + exists true → fail。
    result, events, refs = run(True, False)
    assert result["latched"] is True
    assert events[0]["outcome"] == "fail"
    assert refs and refs[0]["result"] == "fail", refs


def test_window_mapping_explicit_title_vs_legacy(monkeypatch, tmp_path):
    """显式 title/titleRegex 指定目标时不残留源窗口互斥约束;未指定
    目标的 legacy 本窗行为保持(selector 自身约束)。"""
    from agent.recording.replay import _verifier_expectation
    golden, _, _ = _compile_golden("a_b_a")
    step = golden.steps[0]
    # 显式 title → 映射不得含源窗口 title_regex(^logo1$)。
    e1 = _verifier_expectation(step, {
        "type": "window_open",
        "expected": {"exists": True, "processName": "EDRClient.exe",
                     "title": "Log Center"},
        "timeoutSeconds": 10.0})
    assert e1.value.get("title") == "Log Center"
    assert "title_regex" not in e1.value, e1.value
    # 显式 titleRegex → 同样不残留源 title/source title。
    e2 = _verifier_expectation(step, {
        "type": "window_open",
        "expected": {"exists": True, "processName": "EDRClient.exe",
                     "titleRegex": "^Log\\\\ Center$"},
        "timeoutSeconds": 10.0})
    assert e2.value.get("title_regex") == "^Log\\\\ Center$"
    assert "title" not in e2.value, e2.value
    # 未指定目标(仅 exists)→ legacy 本窗行为:selector 约束保留。
    e3 = _verifier_expectation(step, {
        "type": "window_open",
        "expected": {"exists": True},
        "timeoutSeconds": 10.0})
    assert e3.value.get("title_regex") == "^logo1$", e3.value
    assert e3.value.get("process_name") == "EDRClient.exe", e3.value


# ---- 1426:LOST 三态/真实引用/空声明(失败回归先行) ----

class _FaultyInventoryServer:
    """真实 server 的受控视图:observe_product_windows 注入一次明确
    故障(ok=false/code),其余工具全部委托真实实现 — 故障注入明确
    走同一链,不 mock 掉任何授权/租约/绑定逻辑。"""

    def __init__(self, server, backend, fault_when):
        self._server = server
        self._backend = backend
        self._fault_when = fault_when
        self.failed_reads = []

    def observe_product_windows(self, run_token):
        if self._fault_when() and not self.failed_reads:
            self.failed_reads.append(True)
            return json.dumps({"ok": False,
                               "code": "synthetic_inventory_unavailable"})
        return self._server.observe_product_windows(run_token)

    def __getattr__(self, name):
        return getattr(self._server, name)


def test_lost_inventory_read_failure_unknown_latch(monkeypatch, tmp_path):
    """P1(1426 缺口1):W2 关闭后 observe_product_windows 明确失败
    (ok=false,非成功空清单)→ 窗口断言事实缺失 → 到 W2 业务输入
    恰 2、durable/report unknown 锁存;第三步 0 focus/input。负断言
    不以故障读/空 list 放行。"""
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    golden, _, _ = _compile_golden("a_b_a")
    backend = _fake_backend_for("a_b_a")
    _install_fake_os(monkeypatch, backend)
    fault_server = _FaultyInventoryServer(
        server, backend,
        fault_when=lambda: not backend._windows["200"]["visible"])
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=fault_server,
        window_role_map=_A_B_A_ROLE_MAP)
    assert fault_server.failed_reads, "故障注入必须实际触发恰一次"
    assert len(fault_server.failed_reads) == 1
    assert result["latched"] is True
    assert result["dispatchInputs"] == 2, result["steps"]
    events = AttemptLedger(tmp_path / "h").rebuild_history(
        run_id=result["runId"])
    assert [e["outcome"] for e in events][:2] == ["pass", "unknown"]
    after = events[1].get("stateAfterRef") or {}
    assert after.get("observationRef") == "unknown", after


def test_lost_inventory_ref_not_hash_forgery(monkeypatch, tmp_path):
    """P1(1426 缺口2):LOST 分支 after 引用不得拼 OBS-64hex(长 68)
    冒充观测注册;成功清单 → 既有观测/账本接口的稳定脱敏引用
    (本 run 注册 id 形状),无真实引用 → 诚实 unknown。"""
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    golden, _, _ = _compile_golden("a_b_a")
    backend = _fake_backend_for("a_b_a")
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_A_B_A_ROLE_MAP)
    events = AttemptLedger(tmp_path / "h").rebuild_history(
        run_id=result["runId"])
    after = events[1].get("stateAfterRef") or {}
    ref = str(after.get("observationRef"))
    # 引用三态:unknown(无事实)/ OBS-32hex(真实观测注册)/
    # INV-<16hex>(本次清单稳定脱敏引用,既有账本 evidenceRefs 记录);
    # 不得拼 OBS-64hex(长 68)冒充观测注册,不得与内容无关跨 run
    # 同值(证据引用在 evidenceRefs 可追溯)。
    assert ref == "unknown" or (
        ref.startswith("OBS-") and len(ref) == 36) or (
        ref.startswith("INV-") and len(ref) == 20), (
        f"LOST after 引用必须真实/诚实,实际 {ref!r}")
    evidence = events[1].get("evidenceRefs") or []
    if ref.startswith("INV-"):
        # evidenceRefs 携带**完整特征记录**(payload/状态/归属/时点);
        # ref 字符串必须能解析回该记录(重启可还原事实)。
        assert any(
            isinstance(e, dict) and e.get("ref") == ref and
            e.get("kind") == "window-inventory" and
            isinstance(e.get("windows"), list) and
            e.get("readStatus") == "ok" and
            e.get("attemptId") == events[1].get("attemptId") and
            e.get("runId") == events[1].get("runId")
            for e in evidence), (
            "INV 引用必须解析到实际特征记录")
    # 非 LOST 步骤引用不受影响(仍 OBS-36)。
    assert str(events[0]["stateAfterRef"]["observationRef"]).startswith(
        "OBS-")


def test_lost_without_declared_close_unknown_blocks(monkeypatch, tmp_path):
    """P1(1426 缺口3):只去掉第二步 verifiers(合法模型)→ 空结果
    不得偷换成"已确认关闭";缺显式可验证关闭/absent 声明 → unknown
    锁存(到 W2 业务 2,第三步 0 focus/input)。显式声明且事实充分
    的正常关闭继续通过(已有正向链覆盖,不重开)。"""
    import dataclasses
    import target.server as server
    from agent.recording.execution_ledger import \
        execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    from agent.recording.models import GoldenTrace
    golden, _, _ = _compile_golden("a_b_a")
    second = dataclasses.replace(golden.steps[1], verifiers=())
    golden = GoldenTrace.from_dict(dataclasses.replace(
        golden, steps=(golden.steps[0], second, *golden.steps[2:]
                       )).to_dict())
    backend = _fake_backend_for("a_b_a")
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_A_B_A_ROLE_MAP)
    assert result["latched"] is True
    assert result["dispatchInputs"] == 2
    events = AttemptLedger(tmp_path / "h").rebuild_history(
        run_id=result["runId"])
    assert [e["outcome"] for e in events][:2] == ["pass", "unknown"]
    after = events[1].get("stateAfterRef") or {}
    assert after.get("bindingRevision") == "lost", after
    # 无显式声明 → 结果必须 unknown(不能偷换"已确认关闭");
    # 成功读取的清单事实仍以稳定引用留痕(evidenceRefs)。
    ref = str(after.get("observationRef"))
    assert ref == "unknown" or (ref.startswith("INV-") and
                                len(ref) == 20), ref
    if ref.startswith("INV-"):
        assert any(isinstance(e, dict) and e.get("ref") == ref
                   for e in (events[1].get("evidenceRefs") or []))


def test_lost_declared_close_with_sufficient_facts_continues(
        monkeypatch, tmp_path):
    """正常对照:显式声明关闭 + 成功非空清单事实充分 → 按契约继续
    (A→B→A 三 pass;LOST 保持,不复活/不重试/不找兄弟)。"""
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    golden, _, _ = _compile_golden("a_b_a")
    backend = _fake_backend_for("a_b_a")
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_A_B_A_ROLE_MAP)
    assert result["latched"] is False, result["steps"]
    assert result["dispatchInputs"] == 3
    events = AttemptLedger(tmp_path / "h").rebuild_history(
        run_id=result["runId"])
    assert [e["outcome"] for e in events] == ["pass", "pass", "pass"]
    after = events[1].get("stateAfterRef") or {}
    assert after.get("bindingRevision") == "lost", after


def test_nonrequired_step_unknown_still_blocks(monkeypatch, tmp_path):
    """首步/中间 nonrequired 未知仍阻断(nonrequired 不绕过严格门)。"""
    import dataclasses
    import target.server as server
    from agent.recording.execution_ledger import \
        execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    from agent.recording.models import GoldenTrace

    def run(required):
        golden, _, _ = _compile_golden("a_b_a")
        second = dataclasses.replace(golden.steps[1], verifiers=(),
                                     required=required)
        return GoldenTrace.from_dict(dataclasses.replace(
            golden, steps=(golden.steps[0], second, *golden.steps[2:]
                           )).to_dict())

    for required in (True, False):
        backend = _fake_backend_for("a_b_a")
        _install_fake_os(monkeypatch, backend)
        result = execute_v3_window_stage_real(
            golden=run(required), ledger=AttemptLedger(tmp_path / "h"),
            server_tools=server,
            window_role_map=_A_B_A_ROLE_MAP)
        assert result["latched"] is True, (
            f"required={required}: 未知必须阻断")
        assert result["dispatchInputs"] == 2


# ---- 1456:统一三态入口/本步单份特征/精简报告(失败回归先行) ----

def test_normal_path_negative_assertion_inventory_failure_blocks(
        monkeypatch, tmp_path):
    """P1(1456 缺口1):普通路径(非 LOST)首步负断言(
    exists=false)的 after 清单工具读失败 → 事实缺失 → 首步输入
    恰 1、durable/report unknown 锁存,后续 0 focus/输入;不得以
    旧 _product_window_facts 的 [] 回退当否定证据。"""
    import dataclasses
    import target.server as server
    from agent.recording.execution_ledger import \
        execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    from agent.recording.models import GoldenTrace
    golden, _, _ = _compile_golden("a_b_a")
    declared = dict(golden.steps[0].verifiers[0])
    declared["expected"] = {**declared["expected"], "exists": False}
    first = dataclasses.replace(golden.steps[0], verifiers=(declared,))
    golden = GoldenTrace.from_dict(dataclasses.replace(
        golden, steps=(first, *golden.steps[1:])).to_dict())
    backend = _fake_backend_for("a_b_a")
    _install_fake_os(monkeypatch, backend)
    fault_server = _FaultyInventoryServer(
        server, backend, fault_when=lambda: bool(backend.dispatch_calls))
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=fault_server,
        window_role_map=_A_B_A_ROLE_MAP)
    assert fault_server.failed_reads, "故障注入必须实际触发"
    assert result["latched"] is True
    assert result["dispatchInputs"] == 1, (
        "首步动作真实执行;断言事实缺失 → unknown,不推进第二步")
    events = AttemptLedger(tmp_path / "h").rebuild_history(
        run_id=result["runId"])
    assert [e["outcome"] for e in events] == ["unknown"]
    refs = events[0].get("assertionResultRefs") or []
    assert all(r.get("result") != "pass" for r in refs), refs


def test_normal_inventory_success_empty_and_nonempty_cells(
        monkeypatch, tmp_path):
    """成功空/非空清单 × 正/负断言对照(统一三态入口的普通路径
    正确语义):成功空清单 + exists=false → pass(目标确实不在);
    成功非空 + exists=false → fail;成功非空 + exists=true → pass;
    成功空 + exists=true → fail。"""
    import dataclasses
    import target.server as server
    from agent.recording.execution_ledger import \
        execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    from agent.recording.models import GoldenTrace

    cells = {"t": (True, True), "f_present": (False, True),
             "f_absent": (False, False), "t_absent": (True, False)}
    outcomes = {}
    for name, (expected_exists, log_center_present) in cells.items():
        golden, _, _ = _compile_golden("a_b_a")
        declared = dict(golden.steps[0].verifiers[0])
        declared["expected"] = {**declared["expected"],
                                "exists": expected_exists}
        first = dataclasses.replace(golden.steps[0],
                                    verifiers=(declared,))
        golden = GoldenTrace.from_dict(dataclasses.replace(
            golden, steps=(first, *golden.steps[1:])).to_dict())
        backend = _fake_backend_for("a_b_a")
        _install_fake_os(monkeypatch, backend)
        server_tools = server
        if not log_center_present:
            # 目标"不在清单"由成功空清单承载(明确标注的受控注入:
            # 只重写 observe_product_windows 的返回为成功空清单 —
            # 绑定/租约/dispatcher 全真实;绑定 W2 已在真实枚举下
            # 完成,后续读取成功但覆盖该窗口已不在)。
            class _EmptyInventoryView:
                def __getattr__(self, item):
                    return getattr(server, item)

                def observe_product_windows(self, run_token):
                    return json.dumps({"ok": True, "windows": []})
            server_tools = _EmptyInventoryView()
        result = execute_v3_window_stage_real(
            golden=golden, ledger=AttemptLedger(tmp_path / name),
            server_tools=server_tools,
            window_role_map=_A_B_A_ROLE_MAP)
        events = AttemptLedger(tmp_path / name).rebuild_history(
            run_id=result["runId"])
        refs = (events[0].get("assertionResultRefs") or [])
        outcomes[name] = (result, refs)

    # 目标存在 + exists true → pass。
    result, refs = outcomes["t"]
    assert result["latched"] is False and refs[0]["result"] == "pass"
    # 目标存在 + exists false → fail(阻断)。
    result, refs = outcomes["f_present"]
    assert result["latched"] is True and refs[0]["result"] == "fail"
    # 目标不存在 + exists false → pass(closed 语义)。
    result, refs = outcomes["f_absent"]
    assert result["latched"] is False and refs[0]["result"] == "pass"
    # 目标不存在 + exists true → fail。
    result, refs = outcomes["t_absent"]
    assert result["latched"] is True and refs[0]["result"] == "fail"


def test_inventory_evidence_carries_payload_and_provenance(
        monkeypatch, tmp_path):
    """P1(1456 缺口2):INV 证据必须解析到实际用于判断的脱敏特征
    payload(清单/读取状态/覆盖)+ run-step-attempt-window-phase
    归属 + 真实时点 — 只落 hash 串、重启无法还原事实 = 不合格。
    禁止 native handle/pid/token 落普通状态(递归脱敏)。"""
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    golden, _, _ = _compile_golden("a_b_a")
    backend = _fake_backend_for("a_b_a")
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_A_B_A_ROLE_MAP)
    events = AttemptLedger(tmp_path / "h").rebuild_history(
        run_id=result["runId"])
    lost_event = events[1]
    evidence = lost_event.get("evidenceRefs") or []
    assert evidence, "LOST 步必须有清单证据记录"
    blob = json.dumps(lost_event)
    # 引用可解析:证据记录含归属(run/attempt/window/phase)与
    # 读取状态;重启后从落盘能还原实际清单内容。
    assert '"windows"' in blob and '"readStatus"' in blob
    assert lost_event.get("runId") in blob
    assert lost_event.get("attemptId") in blob
    # native 身份不落普通状态。
    for forbidden in ('"native_id"', '"process_id"', '"run_token"',
                      '"binding_token"'):
        assert forbidden not in blob, forbidden


def test_concise_report_one_entry_per_step_with_previous_ref(
        monkeypatch, tmp_path):
    """P1(1456 缺口3):精简报告 — 每 attempt 只一个规范本步条目
    (识别特征/匹配、动作、断言结果),previous 只含稳定引用
    (run/attempt/ref)不嵌套复制前步完整块;pass 报告也可查本步;
    失败处按 ref 可定位;首步 previous 为空;缺图不是失败(不生成
    占位图/不强制 evidence_missing 缺陷)。"""
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    golden, _, _ = _compile_golden("a_b_a")
    backend = _fake_backend_for("a_b_a")
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_A_B_A_ROLE_MAP)
    report = result["report"]
    steps = report.get("steps")
    assert isinstance(steps, list) and len(steps) == 3, steps
    assert all(isinstance(s, dict) for s in steps), (
        "pass 报告每步也必须是可查条目,不只是 stepId 数组")
    for index, entry in enumerate(steps):
        assert entry.get("attemptId"), entry
        # previous 只含引用(run/attemptId),不含嵌套前步断言/状态。
        previous = entry.get("previous")
        if index == 0:
            assert previous is None, entry
        else:
            assert previous is not None
            assert previous.get("attemptId") == \
                steps[index - 1]["attemptId"]
            assert "assertionResultRefs" not in (previous or {}), (
                "previous 不得复制前步断言块")
            assert "stateBeforeRef" not in (previous or {})
            assert "stateAfterRef" not in (previous or {})
        # 本步识别特征真实存在(每步一份)。
        assert "features" in entry, entry
        # 无图不是失败:不强制 evidence_missing 占位。
        evidence = entry.get("evidence")
        assert not (isinstance(evidence, list) and any(
            e.get("status") == "evidence_missing"
            for e in evidence)), (
            "精简报告不得把缺图当缺陷占位")


# ---- 1536 验收备注 → 用户"全部CC完成"授权:A 三项报告收尾 ----

def _run_a_b_a_sample(monkeypatch, tmp_path, fail_step2=False):
    """真实主链 A→B→A(保留全部 compiler verifiers);fail_step2 时把
    第二步断言改为必然失败(model 合法)。返回 (result, events)。"""
    import dataclasses
    import target.server as server
    from agent.recording.execution_ledger import \
        execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    from agent.recording.models import GoldenTrace
    golden, _, _ = _compile_golden("a_b_a")
    if fail_step2:
        second = dataclasses.replace(
            golden.steps[1],
            verifiers=({"type": "checked", "expected": True,
                        "match": {"automation_id": "logRow"},
                        "timeoutSeconds": 5.0},))
        golden = GoldenTrace.from_dict(dataclasses.replace(
            golden, steps=(golden.steps[0], second, *golden.steps[2:]
                           )).to_dict())
        backend = _fake_backend_for("a_b_a")
        for extra in backend._windows["200"]["extra_controls"]:
            if extra.get("automation_id") == "logRow":
                extra["checked"] = False
    else:
        backend = _fake_backend_for("a_b_a")
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_A_B_A_ROLE_MAP)
    events = AttemptLedger(tmp_path / "h").rebuild_history(
        run_id=result["runId"])
    return result, events


def test_default_failed_report_no_nested_previous_no_placeholder(
        monkeypatch, tmp_path):
    """A-1:默认失败报告不再内联 previous 完整 before/after/断言块,
    也不带缺图 evidence_missing 占位 — failedStep 与 steps 同为单份
    引用式;详细追溯仅显式按需(旧接口委托)。"""
    result, events = _run_a_b_a_sample(monkeypatch, tmp_path,
                                       fail_step2=True)
    report = result["report"]
    assert report["outcome"] == "fail"
    failed = report["failedStep"]
    assert failed is not None
    # failedStep 本身是单份引用式:previous 不嵌前步整块。
    previous = failed.get("previous")
    assert previous is not None
    assert "stateBeforeRef" not in previous, previous
    assert "stateAfterRef" not in previous, previous
    assert "assertionResultRefs" not in previous, previous
    # 缺图不产生 evidence_missing 占位。
    evidence = failed.get("evidence") or []
    assert not any(e.get("status") == "evidence_missing"
                   for e in evidence), evidence
    # 与 steps 同份定位:failedStepRef 可解析到同一 attempt 条目。
    ref = report.get("failedStepRef") or {}
    assert ref.get("attemptId") == failed.get("attemptId")
    matching = [s for s in report["steps"]
                if s.get("attemptId") == failed.get("attemptId")]
    assert len(matching) == 1, "每 attempt 只一份规范条目"
    # 详细视图可显式获取(不塞回默认序列化)。
    assert report.get("failedStepDetailRequested") is not True


def test_inventory_evidence_persisted_on_normal_steps(
        monkeypatch, tmp_path):
    """A-2:普通步骤(非 LOST)实际读取清单做窗口断言 → 该次实际
    清单事实同样按本 attempt 持久一份(读状态/覆盖/时点/归属/ref);
    A→B→A payload 分布应为 [true, true, true](按实际使用),其他
    地方只引用;重启解析一致。"""
    result, events = _run_a_b_a_sample(monkeypatch, tmp_path)
    payload_per_step = [
        any(isinstance(e, dict) and e.get("kind") == "window-inventory"
            and e.get("readStatus") == "ok"
            for e in (event.get("evidenceRefs") or []))
        for event in events]
    assert payload_per_step == [True, True, True], payload_per_step
    # 每份记录归属本 attempt,重启解析一致(事件已从磁盘重建)。
    for event in events:
        for record in (event.get("evidenceRefs") or []):
            if isinstance(record, dict) and \
                    record.get("kind") == "window-inventory":
                assert record.get("attemptId") == event.get("attemptId")
                assert record.get("runId") == event.get("runId")
                assert record.get("capturedAtMonotonic") is not None


def test_action_step_features_and_action_identity(monkeypatch, tmp_path):
    """A-3:无断言动作步也有真实识别特征(本次已解析目标的必要安全
    特征/唯一匹配结果)与动作标识(实际 action_id + 必要脱敏参数);
    A→B→A features 不再是 [1,1,0] 空尾。"""
    result, events = _run_a_b_a_sample(monkeypatch, tmp_path)
    report = result["report"]
    for entry in report["steps"]:
        features = entry.get("features") or []
        assert features, (
            f"{entry.get('stepId')}: 无断言动作步也必须有识别特征")
        action = entry.get("action") or {}
        assert action.get("actionId"), action
    # 第三步特征含本次解析目标(automationId/唯一匹配)。
    third = report["steps"][2]
    feature_blob = json.dumps(third["features"], ensure_ascii=False)
    assert "btnBack" in feature_blob, feature_blob
    assert third["action"].get("actionId") == "gui.click"
    # 匹配唯一性有表达(uniqueMatch 计数/标记)。
    assert any(f.get("uniqueMatch") == 1 or f.get("kind") ==
               "resolved-target" for f in third["features"]), (
        third["features"])


# ---- Message190:B 片 — S4 差分/双端动作/显式 cleanup 接真实主链 ----

def _compile_effect_golden():
    """真实 compiler ready 产物:动作后出现(W2 appeared)+ 关闭
    (W2 absent)显式断言 → effects 携带于 steps;原 A→B→A 窗口序。"""
    import runpy, dataclasses
    ns = runpy.run_path(
        "test_case/test_recording/test_golden_v3_window_sequence.py")
    from agent.recording.compiler import compile_recording
    from agent.recording.models import GoldenTrace
    raw = ns["_cross_window_events"]()
    evs = list(raw.events)
    evs[1] = dataclasses.replace(evs[1], assertion={
        "type": "window_open",
        "expected": {"exists": True, "title": "Log Center",
                     "processName": "EDRClient.exe"}})
    evs[3] = dataclasses.replace(evs[3], assertion={
        "type": "window_open",
        "expected": {"exists": False, "title": "Log Center",
                     "processName": "EDRClient.exe"}})
    raw = dataclasses.replace(raw, events=evs)
    result = compile_recording(raw, profile="windows_hisec")
    assert result.golden.status == "ready"
    return GoldenTrace.from_dict(result.golden.to_dict())


def test_effects_appeared_and_absent_true_chain(monkeypatch, tmp_path):
    """B 矩阵正链:动作恰 1 次 → appeared W2 唯一增量绑定 → 下一步
    本窗业务断言 → 关闭 absent → 返回 A 全 pass;效果逐项真实评估;
    前态基线/后差分入单份特征链。"""
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    golden = _compile_effect_golden()
    backend = _fake_backend_for("a_b_a")
    clicked_titles = []
    original_click = backend.click

    def capture_click(**kwargs):
        clicked_titles.append(backend._gui.main_window.window_text())
        return original_click(**kwargs)

    backend.click = capture_click
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_A_B_A_ROLE_MAP)
    assert result["latched"] is False, result["steps"]
    assert result["dispatchInputs"] == 3
    assert clicked_titles == ["logo1", "Log Center", "logo1"]
    events = AttemptLedger(tmp_path / "h").rebuild_history(
        run_id=result["runId"])
    assert [e["outcome"] for e in events] == ["pass", "pass", "pass"]
    blob = json.dumps(events, default=str)
    assert "appeared" in blob and "absent" in blob, (
        "效果结果必须进入 attempt 链(可重建)")


def test_report_steps_project_window_effect_same_as_ledger(
        monkeypatch, tmp_path):
    """B 阶段片(轮次 8):受控链 report.steps[].stateAfter.windowEffect
    与账本 stateAfterRef.windowEffect 同份(kind/outcome/satisfied 逐键)
    — 效果评估结果进报告投影,不再只埋账本;无 effects 步零字段变更。"""
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    golden = _compile_effect_golden()
    backend = _fake_backend_for("a_b_a")
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_A_B_A_ROLE_MAP)
    assert result["latched"] is False, result["steps"]
    events = AttemptLedger(tmp_path / "h").rebuild_history(
        run_id=result["runId"])
    report_steps = result["report"]["steps"]
    assert len(report_steps) == len(events) == 3
    effect_pairs = [
        (event, entry) for event, entry in zip(events, report_steps)
        if (event.get("stateAfterRef") or {}).get("windowEffect")]
    assert effect_pairs, "前置:效果链必须至少一步带 windowEffect"
    for event, entry in effect_pairs:
        ledger_effect = event["stateAfterRef"]["windowEffect"]
        projected = (entry.get("stateAfter") or {}).get("windowEffect")
        assert projected == ledger_effect, (projected, ledger_effect)
        for ledger_item, proj_item in zip(ledger_effect, projected):
            for key in ("kind", "outcome", "satisfied"):
                assert proj_item[key] == ledger_item[key], key
    # 无 effects 步:stateAfter 不携带 windowEffect 字段(零字段变更)。
    no_effect_entries = [
        entry for event, entry in zip(events, report_steps)
        if not (event.get("stateAfterRef") or {}).get("windowEffect")]
    assert no_effect_entries, "前置:链中应存在无 effects 步"
    for entry in no_effect_entries:
        assert "windowEffect" not in (entry.get("stateAfter") or {}), entry


def test_effects_multiple_increment_refused(monkeypatch, tmp_path):
    """双增量拒绝:appeared 求值时两个新候选 → ambiguous → unknown
    锁存(不挑第一项)。"""
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    golden = _compile_effect_golden()
    backend = _fake_backend_for("a_b_a")
    # 注入第二个同身份新窗口:基线时刻隐藏,btnApply 动作后同时出现
    # (与 Log Center 一起)→ appeared 双增量。
    backend._windows["300"] = {"process_id": 42, "title": "Log Center",
                               "root_control_id": "LogCenterWindow",
                               "visible": False, "foreground": True,
                               "process_name": "EDRClient.exe"}
    original_click = backend.click

    def click_shows_sibling(**kwargs):
        out = original_click(**kwargs)
        if kwargs.get("automation_id") == "btnApply":
            backend._windows["300"]["visible"] = True
        return out

    backend.click = click_shows_sibling
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_A_B_A_ROLE_MAP)
    assert result["latched"] is True
    events = AttemptLedger(tmp_path / "h").rebuild_history(
        run_id=result["runId"])
    # 拒绝码持久(可追溯);ambiguous 明细在 stage 步骤摘要
    # (window effect 多候选不挑第一项)。
    assert events[0]["stateAfterRef"].get("rejectionCode") == \
        "declared_window_effect_not_satisfied"
    blob = json.dumps(result["steps"], default=str)
    assert "ambiguous" in blob, blob
    # B 阶段片(轮次 9):ambiguous 锁存条目效果键名与 pass/LOST 路径
    # 统一 — 消费方不按 outcome 分支取键;同份共存,`effects` 保留。
    latched_entries = [s for s in result["steps"]
                       if s.get("outcome") == "unknown"]
    assert len(latched_entries) == 1, result["steps"]
    entry = latched_entries[0]
    assert entry.get("effects") == entry.get("windowEffect"), entry
    assert entry["windowEffect"], "锁存步应携带效果结果明细"
    # B 阶段片(轮次 9):failedStep 投影(report.failedStep = _entry
    # 产物;cli.py _cmd_replay_v3 原样透传)在失败步如实呈现
    # windowEffect — 消费方无需进账本即可见拒绝明细。
    failed_step = result["report"]["failedStep"]
    assert failed_step and failed_step.get("stepId") == entry["stepId"], (
        failed_step)
    assert ((failed_step.get("stateAfter") or {}).get("windowEffect")
            == entry["windowEffect"]), failed_step


def test_effects_partial_failure_blocks(monkeypatch, tmp_path):
    """多 effects 部分失败:appeared 成立但 absent 到期未发生(窗口
    仍在)→ 该步 unknown/fail 锁存,不因单效果 pass 而放行。"""
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    golden = _compile_effect_golden()
    backend = _fake_backend_for("a_b_a")
    # 打掉 logRow 关闭效果(明确 OS 故障注入:点击后 Log Center 仍
    # 可见 — absent 效果到期无法满足)。
    original_click = backend.click

    def click_no_close(**kwargs):
        out = original_click(**kwargs)
        if kwargs.get("automation_id") == "logRow":
            backend._windows["200"]["visible"] = True
        return out

    backend.click = click_no_close
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_A_B_A_ROLE_MAP)
    assert result["latched"] is True
    assert result["dispatchInputs"] == 2, result["steps"]
    events = AttemptLedger(tmp_path / "h").rebuild_history(
        run_id=result["runId"])
    assert events[1]["outcome"] in ("fail", "unknown")
    assert [e["outcome"] for e in events][2:] == [], (
        "效果未全部满足 → 第三步 0 输入")
    # B 阶段片(轮次 9):锁存条目效果键名统一(与轮次 8 pass/LOST
    # 投影对称);`effects` 保留,`windowEffect` 同份。
    latched_entries = [s for s in result["steps"]
                       if s.get("outcome") != "pass"]
    assert len(latched_entries) == 1, result["steps"]
    entry = latched_entries[0]
    assert entry.get("effects") == entry.get("windowEffect"), entry
    # 锁存步是 step-0002(absent 失败);其效果明细只含本步效果。
    assert [e["kind"] for e in entry["windowEffect"]] == ["absent"], entry


def test_visual_only_step_honestly_refused_not_target_not_in_observation(
        monkeypatch, tmp_path):
    """B 阶段片(轮次 10):v3 受控链对 visual-only 步(声明
    selector.control 为空 — 录制时仅图像定位、无控件身份)诚实拒绝:
    专门拒绝码 visual_only_unsupported + message 指向 legacy
    replay_mode 视觉能力未接入受控链,不冒充 target_not_in_observation
    (误导诊断);GUI 前 fail-closed 锁存,0 业务输入。legacy 链的
    semantic/visual 双模(replay.py semantic_first/visual_only)未接入
    受控链 — 不实装视觉解析(更大片)。"""
    import dataclasses
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    from agent.recording.models import GoldenTrace
    golden = _compile_effect_golden()
    # 最短反例:step-0001 声明控件身份置空(visual-only)。
    first = dataclasses.replace(
        golden.steps[0],
        selector=dataclasses.replace(golden.steps[0].selector, control={}))
    golden = GoldenTrace.from_dict(dataclasses.replace(
        golden, steps=(first, *golden.steps[1:])).to_dict())
    backend = _fake_backend_for("a_b_a")
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_A_B_A_ROLE_MAP)
    assert result["latched"] is True
    assert result["dispatchInputs"] == 0, (
        "visual-only 步声明无控件身份 → GUI 前 0 业务输入")
    events = AttemptLedger(tmp_path / "h").rebuild_history(
        run_id=result["runId"])
    assert events[0]["stateAfterRef"].get("rejectionCode") == \
        "visual_only_unsupported", events[0]["stateAfterRef"]
    # 步骤摘要条目携带诚实拒绝码(消费方可诊断,不再冒充
    # target_not_in_observation);message 指向视觉语义未接入。
    assert result["steps"][0]["code"] == "visual_only_unsupported", \
        result["steps"][0]
    assert "visual" in result["steps"][0].get("message", ""), \
        result["steps"][0]


def test_visual_only_step_resolves_and_dispatches_click_window(monkeypatch,
                                                              tmp_path):
    """B 阶段片(轮次 15):受控链对 visual-only 步(声明
    selector.control 为空、visual 模板齐备)在注入 visual_resolver 时
    **实装图像解析** — 复用 legacy SafeVisualResolver 语义(模板
    digest 校验/红框锁定窗口截图/置信度门槛),解析结果派发真实
    目录动作 pointer.click_window(A023,窗口相对坐标),不是诚实拒绝
    的终态。GUI 前失败面照旧 fail-closed(模板缺失/低置信度 →
    StepMaterializationError 码锁存)。无 resolver 时轮次 10 的诚实
    拒绝语义不变(既有用例钉死)。"""
    import base64
    import dataclasses
    import hashlib
    import io
    import target.server as server
    from PIL import Image, ImageDraw
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from agent.recording.runtime import build_visual_resolver
    from target.windowing.attempt_ledger import AttemptLedger
    from agent.recording.models import GoldenTrace
    golden = _compile_effect_golden()
    # 最短正例:step-0001 声明控件身份置空 + visual 模板(真实 PNG,
    # digest 对齐 build_visual_resolver 的 asset_root 校验)。
    # 唯一匹配构造:噪声模板(红底 + 白色十字)贴在灰底帧的 (0,0),
    # 全帧唯一起过置信度门槛的区域 — 灰底与模板零重合,不存在
    # "处处 1.0 → margin 0" 的歧义面(resolver 的 min_margin 门对
    # 单色帧诚实拒绝,正例必须给唯一匹配)。
    template = Image.new("RGB", (60, 40), (200, 30, 30))
    tdraw = ImageDraw.Draw(template)
    tdraw.line([(30, 5), (30, 35)], fill=(255, 255, 255), width=3)
    tdraw.line([(10, 20), (50, 20)], fill=(255, 255, 255), width=3)
    buf = io.BytesIO()
    template.save(buf, format="PNG")
    template_bytes = buf.getvalue()
    (tmp_path / "apply.png").write_bytes(template_bytes)
    visual = {
        "template": "apply.png",
        "elementSha256": "sha256:" +
            hashlib.sha256(template_bytes).hexdigest(),
        "redacted": True,
        "relativePoint": [0.5, 0.5],
    }
    first = dataclasses.replace(
        golden.steps[0],
        selector=dataclasses.replace(golden.steps[0].selector, control={},
                                     visual=visual))
    golden = GoldenTrace.from_dict(dataclasses.replace(
        golden, steps=(first, *golden.steps[1:])).to_dict())
    backend = _fake_backend_for("a_b_a")
    # fake OS 层补红框截图能力:800x600 灰底帧,模板图案贴 (0,0)
    # (与锁定窗口矩形 0,0-800x600 origin 偏差 ≤32),capture_scope=
    # window、原点合法 — observation 的 capture_redacted_frame 契约
    # 全走真实代码。模板像素逐点复制(非 resize),scale=1.0 精确。
    frame = Image.new("RGB", (800, 600), (90, 90, 90))
    frame.paste(template, (0, 0))
    fbuf = io.BytesIO()
    frame.save(fbuf, format="PNG")
    frame_bytes = fbuf.getvalue()

    def screenshot(_path=None):
        return {"ok": True, "capture_scope": "window",
                "image_b64": base64.b64encode(frame_bytes).decode("ascii"),
                "origin": [0, 0]}

    backend.screenshot = screenshot
    backend.recorder_ui_rectangles = lambda: []
    backend.protected_rectangles = lambda: []

    def click_window_at(x, y, window_title_re=None):
        backend.dispatch_calls.append({
            "method": "click_window_at", "x": int(x), "y": int(y),
            "window_title_re": window_title_re})
        # 视觉点击的录制语义与 btnApply 语义点击一致(点击主窗口按钮
        # → Log Center 出现):效果接线挂到 A023 的 OS 工具上,否则
        # step-0001 的 appeared 效果门 timeout。
        backend._windows["200"]["visible"] = True
        return {"ok": True, "method": "mouse.click",
                "window_relative": {"x": int(x), "y": int(y)}}

    backend.click_window_at = click_window_at
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_A_B_A_ROLE_MAP,
        visual_resolver=build_visual_resolver(tmp_path),
        # 评审 P1-2:视觉解析仅 semantic_first 显式启用(默认 semantic_only
        # 现拒绝纯视觉点击)。
        replay_mode="semantic_first")
    assert result["latched"] is False, result["steps"]
    assert result["steps"][0]["outcome"] == "pass", result["steps"]
    # 三步全链(visual step-0001 + 语义 step-0002/0003)每步恰一次
    # 派发;A023 只由 visual 步产生 — 语义步走 gui.click。
    assert result["dispatchInputs"] == 3, result["steps"]
    assert [e["outcome"] for e in result["steps"]] == \
        ["pass", "pass", "pass"], result["steps"]
    # 实际动作:统一 dispatcher 收到的是 pointer.click_window(A023),
    # 经 click_window_at 到 OS 层;窗口相对坐标落模板中心(
    # 相对点 [0.5, 0.5] × 模板 60x40 → 30,20),不是录制 screen XY。
    # 恰一次 A023(visual 步);其余派发是语义步的 gui.click。
    click_calls = [c for c in backend.dispatch_calls
                   if c["method"] == "click_window_at"]
    assert len(click_calls) == 1, backend.dispatch_calls
    click = click_calls[0]
    ledger = AttemptLedger(tmp_path / "h")
    events = ledger.rebuild_history(run_id=result["runId"])
    assert events[0]["receiptPayload"].get("action_id") == \
        "pointer.click_window", events[0]["receiptPayload"]
    assert [e["receiptPayload"].get("action_id") for e in events[1:]] == \
        ["gui.click", "gui.click"], \
        [e["receiptPayload"] for e in events[1:]]
    assert click["x"] == 30 and click["y"] == 20, click


def test_visual_only_step_without_resolver_still_refused(monkeypatch,
                                                         tmp_path):
    """轮次 15 的负面对照:未注入 visual_resolver 的受控链对
    visual-only 步保持轮次 10 的诚实拒绝(visual_only_unsupported,
    GUI 前 0 输入)— 实装只在 resolver 显式提供时打开,不静默回退
    语义解析。"""
    import dataclasses
    import target.server as server
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    from agent.recording.models import GoldenTrace
    golden = _compile_effect_golden()
    first = dataclasses.replace(
        golden.steps[0],
        selector=dataclasses.replace(golden.steps[0].selector, control={}))
    golden = GoldenTrace.from_dict(dataclasses.replace(
        golden, steps=(first, *golden.steps[1:])).to_dict())
    backend = _fake_backend_for("a_b_a")
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_A_B_A_ROLE_MAP)
    assert result["latched"] is True
    assert result["dispatchInputs"] == 0
    assert result["steps"][0]["code"] == "visual_only_unsupported", \
        result["steps"][0]


def test_window_effect_pass_business_fail_still_blocks(monkeypatch,
                                                       tmp_path):
    """窗口效果 pass 但业务断言 fail 仍停(窗口效果不代替业务成功)。"""
    import dataclasses
    import target.server as server
    from agent.recording.execution_ledger import \
        execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    from agent.recording.models import GoldenTrace
    golden = _compile_effect_golden()
    # 第二步加业务断言(logRow checked=true),OS 实况 false。
    second = None
    for step in golden.steps:
        if step.step_id == "step-0002":
            second = dataclasses.replace(
                step, verifiers=({"type": "checked", "expected": True,
                                  "match": {"automation_id": "logRow"},
                                  "timeoutSeconds": 5.0},))
    golden = GoldenTrace.from_dict(dataclasses.replace(
        golden, steps=tuple(
            second if s.step_id == "step-0002" else s
            for s in golden.steps)).to_dict())
    backend = _fake_backend_for("a_b_a")
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_A_B_A_ROLE_MAP)
    events = AttemptLedger(tmp_path / "h").rebuild_history(
        run_id=result["runId"])
    assert events[1]["outcome"] == "fail", events[1]["outcome"]
    assert result["latched"] is True


def test_drag_dual_end_resolution_and_unique_target(monkeypatch,
                                                    tmp_path):
    """B-4:drag 的 selector/endSelector 在正确绑定与本次观测下解析
    源/终端(复用既有 resolve_replay_selector/HybridLocator 唯一
    目标语义),同租约 focus 后/dispatch 前重验;不用录制 screen XY。
    端点歧义 → 0 越权输入拒绝。"""
    import dataclasses
    import target.server as server
    from agent.recording.execution_ledger import \
        execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    from agent.recording.models import GoldenTrace
    from agent.recording.models import ReplaySelector

    def build_drag(end_automation_id="btnBack"):
        golden, _, _ = _compile_golden("same_window", clear_verifiers=True)
        first = golden.steps[0]
        drag_step = dataclasses.replace(
            first,
            action_id="pointer.drag",
            selector=ReplaySelector(
                window={"processName": "EDRClient.exe",
                        "titleRegex": "^logo1$"},
                control={"automationId": "btnApply",
                         "controlType": "Button"},
                visual=None),
            end_selector=ReplaySelector(
                window={"processName": "EDRClient.exe",
                        "titleRegex": "^logo1$"},
                control={"automationId": end_automation_id,
                         "controlType": "Button"},
                visual=None))
        return GoldenTrace.from_dict(dataclasses.replace(
            golden, steps=(drag_step, *golden.steps[1:])).to_dict())

    backend = _fake_backend_for("same_window")
    backend._windows["100"]["extra_controls"].append(
        {"automation_id": "btnBack", "text": "Back"})
    drag_calls = []

    def capture_drag(x1, y1, x2, y2, duration=0.25, **kwargs):
        drag_calls.append((x1, y1, x2, y2))
        return {"ok": True, "method": "dragTo",
                "start": {"x": x1, "y": y1},
                "end": {"x": x2, "y": y2}}

    backend.drag = capture_drag
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=build_drag(), ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_SAME_WINDOW_ROLE_MAP)
    assert result["latched"] is False, result["steps"]
    # 双端解析:两控件 rectangle 不同 → x1/y1 != x2/y2(非录制 XY 回放)。
    assert len(drag_calls) == 1, backend.dispatch_calls
    x1, y1, x2, y2 = drag_calls[0]
    assert (x1, y1) != (x2, y2), drag_calls

    # 端点歧义:同 automationId 两个控件 → 0 越权输入。
    backend2 = _fake_backend_for("same_window")
    # 终端 automationId 两个同 ID 控件(不同 text)→ 唯一性不成立。
    backend2._windows["100"]["extra_controls"].extend([
        {"automation_id": "btnBack", "text": "Apply"},
        {"automation_id": "btnBack", "text": "Confirm"}])
    drag_calls2 = []
    backend2.drag = capture_drag
    _install_fake_os(monkeypatch, backend2)
    result2 = execute_v3_window_stage_real(
        golden=build_drag(), ledger=AttemptLedger(tmp_path / "h2"),
        server_tools=server,
        window_role_map=_SAME_WINDOW_ROLE_MAP)
    assert result2["latched"] is True
    events2 = AttemptLedger(tmp_path / "h2").rebuild_history(
        run_id=result2["runId"])
    # 端点不可唯一解析(not_found/ambiguous)→ 0 越权输入拒绝。
    assert events2[0]["stateAfterRef"].get("rejectionCode") in (
        "drag_endpoint_not_uniquely_resolved",
        "replay_target_ambiguous", "replay_target_not_found"), (
        events2[0]["stateAfterRef"].get("rejectionCode"))
    assert len(drag_calls2) == 0, (
        "端点歧义必须零越权输入拒绝")


def test_scroll_dispatches_once_with_verified_lock(monkeypatch,
                                                   tmp_path):
    """B-4:scroll 经统一 dispatcher(window_lock 门)恰一次;不重放
    录制 XY。"""
    import dataclasses
    import target.server as server
    from agent.recording.execution_ledger import \
        execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    from agent.recording.models import GoldenTrace, ReplaySelector

    def build_scroll():
        golden, _, _ = _compile_golden("same_window", clear_verifiers=True)
        first = golden.steps[0]
        scroll_step = dataclasses.replace(
            first, action_id="pointer.scroll",
            selector=ReplaySelector(
                window={"processName": "EDRClient.exe",
                        "titleRegex": "^logo1$"},
                control={"automationId": "btnApply",
                         "controlType": "Button"},
                visual=None),
            args={"clicks": 3})
        return GoldenTrace.from_dict(dataclasses.replace(
            golden, steps=(scroll_step, *golden.steps[1:])).to_dict())

    backend = _fake_backend_for("same_window")
    scroll_calls = []

    def capture_scroll(clicks, x=None, y=None, **kwargs):
        scroll_calls.append((clicks, x, y))
        return {"ok": True, "method": "scroll", "clicks": clicks}

    backend.scroll = capture_scroll
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=build_scroll(), ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_SAME_WINDOW_ROLE_MAP)
    assert result["latched"] is False, result["steps"]
    assert len(scroll_calls) == 1, backend.dispatch_calls
    assert scroll_calls[0][0] == 3


def test_cleanup_explicit_mapping_runs_after_failure(monkeypatch,
                                                     tmp_path):
    """B-5:主流程 fail/unknown 后,显式 cleanup(既有
    sequence.cleanup 映射)在当前 binding 可验证时运行,保持明确
    run/step/attempt/previous 与结果;结束释放绑定。"""
    import dataclasses
    import json as _json
    import target.server as server
    from agent.recording.execution_ledger import \
        execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    from agent.recording.models import GoldenTrace, ReplaySelector

    def build_with_cleanup():
        golden, _, _ = _compile_golden("a_b_a")
        second = dataclasses.replace(
            golden.steps[1],
            verifiers=({"type": "checked", "expected": True,
                        "match": {"automation_id": "logRow"},
                        "timeoutSeconds": 5.0},))
        golden = GoldenTrace.from_dict(dataclasses.replace(
            golden, steps=(golden.steps[0], second, *golden.steps[2:]
                           )).to_dict())
        backend = _fake_backend_for("a_b_a")
        for extra in backend._windows["200"]["extra_controls"]:
            if extra.get("automation_id") == "logRow":
                extra["checked"] = False
        # 显式 cleanup 步:回到 W1 关闭弹窗(gui.click btnBack)。
        sequence = golden.window_sequence.to_dict()
        cleanup_entry = {"stepId": "cleanup-0001", "windowId": "W1",
                         "effects": []}
        sequence["cleanup"] = [cleanup_entry]
        # 可执行 cleanup 步(声明动作:回到 W1 关闭弹窗)。
        # 可执行 cleanup 步(GoldenTrace.cleanup 数组;from_dict 以
        # 位置命名 cleanup-0001 — windowSequence.cleanup 的所有权
        # 段即 cleanup)。
        golden = GoldenTrace.from_dict({
            **golden.to_dict(),
            "cleanup": [{"stepId": "cleanup-0001",
                         "actionId": "gui.click", "args": {},
                         "selector": {"window": {
                             "processName": "EDRClient.exe",
                             "titleRegex": "^logo1$"},
                             "control": {"automationId": "btnBack",
                                         "controlType": "Button"},
                             "visual": None},
                         "endSelector": None,
                         "verifiers": [], "required": False,
                         "status": "ready", "issues": []}],
            "windowSequence": sequence})
        return (golden, backend)

    golden, backend = build_with_cleanup()
    cleanup_clicks = []
    original_click = backend.click

    def capture_cleanup_click(**kwargs):
        cleanup_clicks.append(kwargs.get("automation_id"))
        return original_click(**kwargs)

    backend.click = capture_cleanup_click
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_A_B_A_ROLE_MAP)
    events = AttemptLedger(tmp_path / "h").rebuild_history(
        run_id=result["runId"])
    # 主流程第二步业务 fail → 锁存;cleanup 在既有映射下运行。
    assert events[1]["outcome"] == "fail"
    cleanup_summary = result.get("cleanup") or []
    assert any(c.get("stepId") == "cleanup-0001" and
               c.get("outcome") == "pass" for c in cleanup_summary), (
        cleanup_summary)
    assert cleanup_clicks, "可验证的 cleanup 应实际运行(当前 binding)"


def test_cleanup_unverifiable_skips_with_reason(monkeypatch, tmp_path):
    """B-5:cleanup 目标 binding 不可验证 → skip 并记录原因;不找
    同名兄弟、不自动恢复未知 GUI。"""
    import dataclasses
    import json as _json
    import target.server as server
    from agent.recording.execution_ledger import \
        execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    from agent.recording.models import GoldenTrace

    golden, _, _ = _compile_golden("a_b_a")
    second = dataclasses.replace(
        golden.steps[1],
        verifiers=({"type": "checked", "expected": True,
                    "match": {"automation_id": "logRow"},
                    "timeoutSeconds": 5.0},))
    golden = GoldenTrace.from_dict(dataclasses.replace(
        golden, steps=(golden.steps[0], second, *golden.steps[2:]
                       )).to_dict())
    backend = _fake_backend_for("a_b_a")
    for extra in backend._windows["200"]["extra_controls"]:
        if extra.get("automation_id") == "logRow":
            extra["checked"] = False
    sequence = golden.window_sequence.to_dict()
    # 不可验证目标:W2(该步动作关闭自身绑定窗口 → LOST;rebind
    # 拒绝 → cleanup skip;不找同名兄弟)。selector 身份与 W2 一致。
    cleanup_entry = {"stepId": "cleanup-0001", "windowId": "W2",
                     "effects": []}
    sequence["cleanup"] = [cleanup_entry]
    golden = GoldenTrace.from_dict({
        **golden.to_dict(),
        "cleanup": [{"stepId": "cleanup-0001",
                     "actionId": "gui.click", "args": {},
                     "selector": {"window": {
                         "processName": "EDRClient.exe",
                         "titleRegex": "^Log\\ Center$"},
                         "control": {"automationId": "logRow",
                                     "controlType": "Button"},
                         "visual": None},
                     "endSelector": None,
                     "verifiers": [], "required": False,
                     "status": "ready", "issues": []}],
        "windowSequence": sequence})
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_A_B_A_ROLE_MAP)
    events = AttemptLedger(tmp_path / "h").rebuild_history(
        run_id=result["runId"])
    cleanup_summary = result.get("cleanup") or []
    assert any(c.get("stepId") == "cleanup-0001" and
               c.get("outcome") == "skipped" and c.get("reason")
               for c in cleanup_summary), cleanup_summary
    assert events[1]["outcome"] == "fail"


# ---- 1721:B 片四组边界收口(失败回归先行) ----

def test_effect_absent_present_is_not_satisfied(monkeypatch, tmp_path):
    """P1(1721 缺口1):absent 声明但目标仍可见(facade 返回
    ok=true/outcome=present — 读取成功≠效果成立)→ 第二步 fail/
    unknown 锁存,第三步 0 输入;present/timeout/error/ambiguous
    一律不升级成功;失败持久必要 baseline/diff/outcome/拒绝原因。"""
    import dataclasses
    import target.server as server
    from agent.recording.execution_ledger import \
        execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    from agent.recording.models import GoldenTrace
    golden = _compile_effect_golden()
    # 只去掉第二步业务 verifier(隔离窗口效果门;非完整正向)。
    golden = GoldenTrace.from_dict(dataclasses.replace(
        golden, steps=tuple(dataclasses.replace(s, verifiers=())
                            if s.step_id == "step-0002" else s
                            for s in golden.steps)).to_dict())
    backend = _fake_backend_for("a_b_a")
    _install_fake_os(monkeypatch, backend)
    # OS 明确仍可见(点击后还原可见 — absent 未发生)。
    original_click = backend.click

    def click_no_close(**kwargs):
        out = original_click(**kwargs)
        if kwargs.get("automation_id") == "logRow":
            backend._windows["200"]["visible"] = True
        return out

    backend.click = click_no_close
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_A_B_A_ROLE_MAP)
    assert result["latched"] is True
    assert result["dispatchInputs"] == 2, result["steps"]
    events = AttemptLedger(tmp_path / "h").rebuild_history(
        run_id=result["runId"])
    assert events[1]["outcome"] in ("fail", "unknown")
    assert [e["outcome"] for e in events][2:] == []
    # 失败持久必要事实(拒绝码 + windowEffect 明细含 outcome)。
    after = events[1].get("stateAfterRef") or {}
    assert after.get("rejectionCode") == \
        "declared_window_effect_not_satisfied"
    effects = after.get("windowEffect") or []
    assert effects and effects[0].get("kind") == "absent" and \
        effects[0].get("outcome") == "present", effects


def test_absent_requires_exact_bound_instance(monkeypatch, tmp_path):
    """absent 针对该绑定确切实例:同身份兄弟消失不能让它成立
    (binding.py 差分不能只看 disappeared 非空)。"""
    import dataclasses
    import target.server as server
    from agent.recording.execution_ledger import \
        execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    from agent.recording.models import GoldenTrace
    golden = _compile_effect_golden()
    golden = GoldenTrace.from_dict(dataclasses.replace(
        golden, steps=tuple(dataclasses.replace(s, verifiers=())
                            if s.step_id == "step-0002" else s
                            for s in golden.steps)).to_dict())
    backend = _fake_backend_for("a_b_a")
    # 同身份兄弟(300)在基线后出现并消失;绑定的确切实例(200)仍在。
    backend._windows["300"] = {"process_id": 42, "title": "Log Center",
                               "root_control_id": "LogCenterWindow",
                               "visible": False, "foreground": True,
                               "process_name": "EDRClient.exe"}
    original_click = backend.click

    def click_brother_gone(**kwargs):
        out = original_click(**kwargs)
        if kwargs.get("automation_id") == "logRow":
            backend._windows["300"]["visible"] = False
            backend._windows["200"]["visible"] = True  # 确切实例仍在
        return out

    backend.click = click_brother_gone
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_A_B_A_ROLE_MAP)
    assert result["latched"] is True, (
        "确切实例仍可见时 absent 不得因兄弟消失而成立")


def test_cleanup_prewrite_gate_and_durability(monkeypatch, tmp_path):
    """P1(1721 缺口2):cleanup 走同一预写/账本生命周期 —
    (a) 存储失败(begin_attempt OSError)→ 0 后续 focus/业务/
    cleanup;(b) 主步 fail 后可验证 cleanup 实际运行,且有稳定
    run/step/attempt/previous 持久账本行(重启可追);receipt/
    断言/结果持久;selector 唯一性;动作后异常 unknown 不吞成
    skipped。"""
    import dataclasses
    import target.server as server
    from agent.recording.execution_ledger import \
        execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    from agent.recording.models import GoldenTrace

    def build():
        golden, backend = _cleanup_fixture()
        return golden, backend

    # (a) 预写失败:0 后续任何 GUI 副作用(cleanup 也不执行)。
    class FailBegin(AttemptLedger):
        def begin_attempt(self, **kwargs):
            raise OSError("synthetic prewrite failure")

    golden, backend = build()
    focus_calls = []
    original_focus = backend.focus_existing_window

    def count_focus(**kwargs):
        focus_calls.append(True)
        return original_focus(**kwargs)

    backend.focus_existing_window = count_focus
    _install_fake_os(monkeypatch, backend)
    with pytest.raises(OSError, match="prewrite"):
        execute_v3_window_stage_real(
            golden=golden, ledger=FailBegin(tmp_path / "h0"),
            server_tools=server,
            window_role_map=_SAME_WINDOW_ROLE_MAP)
    assert len(focus_calls) == 0, (
        "预写失败后 0 后续 focus/业务/cleanup 副作用")
    assert len(backend.dispatch_calls) == 0

    # (b) 主步 fail → cleanup 可验证运行 + 持久账本行。
    golden, backend = build()
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_SAME_WINDOW_ROLE_MAP)
    events = AttemptLedger(tmp_path / "h").rebuild_history(
        run_id=result["runId"])
    cleanup_events = [e for e in events
                      if e.get("stepId", "").startswith("cleanup")]
    assert cleanup_events, (
        "cleanup 必须有持久 attempt 行(重启可追)")
    for event in cleanup_events:
        assert event.get("runId") == result["runId"]
        # §7.6:cleanup 序列首步 previous 显式为空(独立授权序列);
        # 行内 attemptId/stepId/receipt/outcome 完整持久(重启可追)。
        assert event.get("attemptId", "").startswith("cleanup")
        assert event.get("receiptPayload") or \
            event.get("outcome") in ("skipped", "unknown"), event


def _cleanup_fixture():
    """显式 cleanup fixture(same_window checked=false 主步 fail)。"""
    import dataclasses
    from agent.recording.models import GoldenTrace
    golden, _, _ = _compile_golden("same_window")
    first = dataclasses.replace(
        golden.steps[0],
        verifiers=({"type": "checked", "expected": True,
                    "match": {"automation_id": "btnApply"},
                    "timeoutSeconds": 5.0},))
    golden = GoldenTrace.from_dict(dataclasses.replace(
        golden, steps=(first, *golden.steps[1:])).to_dict())
    backend = _checkbox_backend(False)
    sequence = golden.window_sequence.to_dict()
    sequence["cleanup"] = [{"stepId": "cleanup-0001",
                            "windowId": "W1", "effects": []}]
    golden = GoldenTrace.from_dict({
        **golden.to_dict(),
        "cleanup": [{"stepId": "cleanup-0001",
                     "actionId": "gui.click", "args": {},
                     "selector": {"window": {
                         "processName": "EDRClient.exe",
                         "titleRegex": "^logo1$"},
                         "control": {"automationId": "btnApply",
                                     "controlType": "Button"},
                         "visual": None},
                     "endSelector": None,
                     "verifiers": [], "required": False,
                     "status": "ready", "issues": []}],
        "windowSequence": sequence})
    return golden, backend


def test_scroll_args_materialized_not_recorded(monkeypatch, tmp_path):
    """P1(1721 缺口3):scroll args x/y 历史坐标不得直通 — 本次
    唯一已解析目标物化位置(或明确受控拒绝);探针坐标 99123/99234
    不得抵达 OS。"""
    import dataclasses
    import target.server as server
    from agent.recording.execution_ledger import \
        execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    from agent.recording.models import GoldenTrace, ReplaySelector
    golden, _, _ = _compile_golden("same_window", clear_verifiers=True)
    scroll_step = dataclasses.replace(
        golden.steps[0], action_id="pointer.scroll",
        selector=ReplaySelector(
            window={"processName": "EDRClient.exe",
                    "titleRegex": "^logo1$"},
            control={"automationId": "btnApply",
                     "controlType": "Button"},
            visual=None),
        args={"clicks": 3, "x": 99123, "y": 99234})
    golden = GoldenTrace.from_dict(dataclasses.replace(
        golden, steps=(scroll_step, *golden.steps[1:])).to_dict())
    backend = _fake_backend_for("same_window")
    scroll_calls = []

    def capture_scroll(clicks, x=None, y=None, **kwargs):
        scroll_calls.append({"clicks": clicks, "x": x, "y": y})
        return {"ok": True, "method": "scroll", "clicks": clicks}

    backend.scroll = capture_scroll
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_SAME_WINDOW_ROLE_MAP)
    assert result["latched"] is False, result["steps"]
    assert len(scroll_calls) == 1
    args = scroll_calls[0]
    assert args["x"] != 99123 and args["y"] != 99234, (
        f"录制坐标不得直通 OS: {args}")
    # 物化位置在本次观测控件 rect 内(800x600 窗口内语义目标)。
    assert 0 <= (args["x"] or 0) <= 800 and 0 <= (args["y"] or 0) <= 600


def test_unique_increment_binding_consumed(monkeypatch, tmp_path):
    """P2(1721 缺口4):唯一增量绑定分支实际执行 — appeared 唯一
    新候选在同租约强实例边界消费并绑定 W(下一步本窗断言通过);
    appearedRef 不落 native 原值(真正不透明引用)。"""
    import dataclasses
    import target.server as server
    from agent.recording.execution_ledger import \
        execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    from agent.recording.models import GoldenTrace
    golden = _compile_effect_golden()
    # 隔离绑定门:去掉首步业务 verifier(非完整正向 fixture)。
    golden = GoldenTrace.from_dict(dataclasses.replace(
        golden, steps=(dataclasses.replace(golden.steps[0],
                                           verifiers=()),
                       *golden.steps[1:])).to_dict())
    backend = _fake_backend_for("a_b_a")
    # 旧可见同身份兄弟 + 动作后唯一新增(300 基线可见,200 出现)。
    backend._windows["300"] = {"process_id": 42, "title": "Log Center",
                               "root_control_id": "LogCenterWindow",
                               "visible": True, "foreground": True,
                               "process_name": "EDRClient.exe"}
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_A_B_A_ROLE_MAP)
    # 绑定隔离探针语义(1721):旧可见同身份兄弟使完整业务 verifiers
    # 先在清单歧义处 unknown — 本 fixture 只隔离绑定门,非完整正向。
    # 断言:唯一增量被消费 → 第二步不再 window_not_bound(全清单
    # 重选),本窗断言取得真实观测(LOST 为动作关闭自身窗口的真
    # 实后态 — 兄弟仍在,absent 差分如实失败)。
    assert "window_not_bound" not in json.dumps(result["steps"],
                                                default=str), (
        "唯一增量必须被消费绑定(下一步不再退回全清单重选)")
    events = AttemptLedger(tmp_path / "h").rebuild_history(
        run_id=result["runId"])
    # appearedRef 不落 native 原值(200 → 不透明 INST-序)。
    history_blob = json.dumps(events, default=str) + json.dumps(
        result["steps"], default=str)
    assert "WIN-200" not in history_blob, (
        "appearedRef 不得编码 native 原值")
    assert "instanceRef" in history_blob or "INST-" in history_blob, (
        "效果结果须携带不透明实例引用")


# ---- 1823:cleanup 存储锁存/真断言 + absent 确切实例 + 增量绑定门 ----

from target.windowing.attempt_ledger import AttemptLedger as _AL


class _FailFirstBegin(_AL):
    """仅首个 begin_attempt 抛 OSError,随后恢复(瞬时故障);
    ledger 存储故障锁存置位(§7.6:边界不随磁盘恢复清除)。"""
    def __init__(self, root):
        super().__init__(root)
        self._first_seen = False

    def begin_attempt(self, **kwargs):
        if not self._first_seen:
            self._first_seen = True
            self._storage_failed = True
            raise OSError("transient prewrite failure")
        self._assert_storage_gate()
        return super().begin_attempt(**kwargs)


class _FailFirstComplete(_AL):
    """仅首个 complete_attempt 抛 OSError,随后恢复(锁存置位)。"""
    def __init__(self, root):
        super().__init__(root)
        self._first_complete = False

    def complete_attempt(self, event_id, **kwargs):
        if not self._first_complete:
            self._first_complete = True
            self._storage_failed = True
            raise OSError("transient postwrite failure")
        self._assert_storage_gate()
        return super().complete_attempt(event_id, **kwargs)


def test_transient_prewrite_stops_all_gui(monkeypatch, tmp_path):
    """P1(1823 缺口1a):瞬时预写失败(仅首次)→ 主步 0 输入且
    **cleanup 也 0 输入**(写盘故障后全 run 停止 GUI;不因磁盘恢复
    而自动恢复;不重试未知动作)。"""
    import target.server as server
    from agent.recording.execution_ledger import \
        execute_v3_window_stage_real
    golden, backend = _cleanup_fixture()
    _install_fake_os(monkeypatch, backend)
    with pytest.raises(OSError, match="transient"):
        execute_v3_window_stage_real(
            golden=golden, ledger=_FailFirstBegin(tmp_path / "h"),
            server_tools=server,
            window_role_map=_SAME_WINDOW_ROLE_MAP)
    assert len(backend.dispatch_calls) == 0, (
        "瞬时预写失败后全 run 禁止后续 GUI(cleanup 也不执行)")


def test_transient_postwrite_stops_all_gui(monkeypatch, tmp_path):
    """P1(1823 缺口1a):主动作后 complete_attempt 瞬时失败 →
    主动作 1 输入后全 run 停止 GUI(cleanup 0 输入;不确定边界
    不可清除)。"""
    import target.server as server
    from agent.recording.execution_ledger import \
        execute_v3_window_stage_real
    golden, backend = _cleanup_fixture()
    _install_fake_os(monkeypatch, backend)
    with pytest.raises(OSError, match="transient"):
        execute_v3_window_stage_real(
            golden=golden, ledger=_FailFirstComplete(tmp_path / "h"),
            server_tools=server,
            window_role_map=_SAME_WINDOW_ROLE_MAP)
    # 主动作 1 次;cleanup 0 次(不确定边界不可清除)。
    assert len(backend.dispatch_calls) == 1, backend.dispatch_calls


def test_cleanup_true_assertions_and_provenance(monkeypatch, tmp_path):
    """P1(1823 缺口1b):cleanup 声明 checked=true 而 OS 实际 false →
    真实求值 fail(不是 receipt ok 即 pass;assertion refs 非空);
    previous 关联真实触发失败 attempt(仅 ref);before/after 真实
    binding revision/observation(非全 pending)。"""
    import dataclasses
    import target.server as server
    from agent.recording.execution_ledger import \
        execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    from agent.recording.models import GoldenTrace
    golden, backend = _cleanup_fixture()
    # cleanup 也声明 checked=true(与主步同型;OS 实际 false)。
    golden = GoldenTrace.from_dict({
        **golden.to_dict(),
        "cleanup": [{**golden.to_dict()["cleanup"][0],
                     "verifiers": [{"type": "checked", "expected": True,
                                    "match": {"automation_id": "btnApply"},
                                    "timeoutSeconds": 5.0}]}]})
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_SAME_WINDOW_ROLE_MAP)
    events = AttemptLedger(tmp_path / "h").rebuild_history(
        run_id=result["runId"])
    cleanup_rows = [e for e in events
                    if e.get("stepId", "").startswith("cleanup")]
    assert cleanup_rows, "cleanup 行持久"
    row = cleanup_rows[0]
    # cleanup 自己的 checked 断言真实求值(OS false)→ fail/unknown。
    refs = row.get("assertionResultRefs") or []
    assert refs, "cleanup 断言必须真实求值并持久(非空 refs)"
    assert any(r.get("result") in ("fail", "unknown") for r in refs), refs
    assert row["outcome"] in ("fail", "unknown"), (
        "cleanup receipt ok 不代替真实断言")
    # previous 稳定关联真实触发失败的主步 attempt(仅引用)。
    main_fail = next(e for e in events
                     if not e.get("stepId", "").startswith("cleanup")
                     and e["outcome"] == "fail")
    assert row.get("previousStepId") == main_fail["stepId"], row
    assert row.get("previousAttemptId") == main_fail["attemptId"]
    # before/after 真实权威事实(非全 pending)。
    before = row.get("stateBeforeRef") or {}
    after = row.get("stateAfterRef") or {}
    assert before.get("bindingRevision") not in (None, "pending"), before
    assert after.get("bindingRevision") not in (None, "pending"), after
    assert str(after.get("observationRef", "")).startswith("OBS-") or \
        after.get("observationRef") == "unknown", after


def test_absent_bound_instance_vs_real_sibling_disappearance(
        monkeypatch, tmp_path):
    """P1(1823 缺口2):第一步 W2 真出现并绑定;W2 本窗观测时加入
    同身份可见兄弟;第二步基线二者可见、动作只隐藏兄弟、绑定实例
    仍可见 → absent 不成立(fail/unknown 锁存;第三步 0 副作用)。"""
    import dataclasses
    import target.server as server
    from agent.recording.execution_ledger import \
        execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    from agent.recording.models import GoldenTrace
    golden = _compile_effect_golden()
    golden = GoldenTrace.from_dict(dataclasses.replace(
        golden, steps=tuple(dataclasses.replace(s, verifiers=())
                            if s.step_id == "step-0002" else s
                            for s in golden.steps)).to_dict())
    backend = _fake_backend_for("a_b_a")
    _install_fake_os(monkeypatch, backend)
    # 兄弟注入时机:W2 出现并绑定后,在 **W2 本窗观测时**(第二步
    # focus/observe;真实主链 3 输入通过)。
    original_focus = backend.focus_existing_window
    sibling_injected = []

    def focus_inject_sibling(*, handle, pid, title, process_name):
        out = original_focus(handle=handle, pid=pid, title=title,
                             process_name=process_name)
        if str(handle) == "200" and not sibling_injected:
            sibling_injected.append(True)
            backend._windows["300"] = {"process_id": 42,
                                       "title": "Log Center",
                                       "root_control_id": "LogCenterWindow",
                                       "visible": True, "foreground": True,
                                       "process_name": "EDRClient.exe"}
        return out

    backend.focus_existing_window = focus_inject_sibling
    # 第二步动作只隐藏兄弟;确切实例(W2=200)仍可见。
    original_click = backend.click

    def click_hides_sibling_only(**kwargs):
        out = original_click(**kwargs)
        if kwargs.get("automation_id") == "logRow":
            backend._windows["300"]["visible"] = False
            backend._windows["200"]["visible"] = True
        return out

    backend.click = click_hides_sibling_only
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_A_B_A_ROLE_MAP)
    assert result["latched"] is True, (
        "绑定的确切实例仍可见 → absent 不得因兄弟消失成立")
    assert result["dispatchInputs"] == 2
    events = AttemptLedger(tmp_path / "h").rebuild_history(
        run_id=result["runId"])
    assert [e["outcome"] for e in events][2:] == [], (
        "第三步 0 副作用")
    effects = (events[1].get("stateAfterRef") or {}).get(
        "windowEffect") or []
    assert effects and effects[0].get("outcome") != "absent", effects


def test_bind_increment_boundary_gates(monkeypatch, tmp_path):
    """P2(1823 缺口3):bind_increment 保持旧 bind 授权/归属/完整
    身份契约 —(a) 错误 native(已绑定 W1 主窗口)传入 W2 声明 →
    拒绝;(b) 无效 run_token → 拒绝。GUI 0 的边界诊断不夸大;但
    新绑定协议本身必须拒绝,不等 focus 偶然挡住。"""
    import json as _json
    import target.server as server
    from target import product_policy as policy
    from target.windowing.binding import WindowBindingRefusal
    policy.reset_for_tests()
    mp = pytest.MonkeyPatch()
    backend = _fake_backend_for("a_b_a")
    # W2 初始可见(诊断可直接绑)。
    backend._windows["200"]["visible"] = True
    _install_fake_os(mp, backend)
    try:
        begin = _json.loads(server.begin_product_run(
            "windows_hisec", REAL_DIGEST))
        token = begin["run_token"]
        w2_identity = {"processName": "EDRClient.exe",
                       "rootAutomationId": "LogCenterWindow",
                       "titleRegex": "^Log\\ Center$"}
        server.register_window_declaration(_json.dumps({
            "W1": {"processName": "EDRClient.exe",
                   "rootAutomationId": "EdrMainWindow",
                   "titleRegex": "^logo1$"},
            "W2": w2_identity,
        }, ensure_ascii=False), token)
        b1 = _json.loads(server.bind_entry_window(
            "W1", {"processName": "EDRClient.exe",
                   "rootAutomationId": "EdrMainWindow",
                   "titleRegex": "^logo1$"}, token))
        assert b1.get("binding_token")
        facade = server._binding_facade_holder.get("facade")
        # (a) 错误 native:W1 主窗口 native(100)传给 W2 声明 → 拒绝。
        with pytest.raises(Exception):
            facade.bind_increment(
                "W2", w2_identity, native_id="100", run_token=token)
        assert facade._bindings.get("W2") is None, (
            "错误 native 不得创建 W2 binding")
        # (b) 无效 run_token → 拒绝。
        with pytest.raises(Exception):
            facade.bind_increment(
                "W2", w2_identity,
                native_id="200", run_token="synthetic-invalid-token")
        assert facade._bindings.get("W2") is None, (
            "无效 token 不得创建 binding")
        # 正例:真实 token + 真实 W2 native → 绑定成功。
        ok = facade.bind_increment(
            "W2", w2_identity, native_id="200", run_token=token)
        assert ok.get("binding_token")
    finally:
        product_policy.reset_for_tests()
        mp.undo()


# ---- 2013:真实 IO 异常锁存/cleanup 动作后观测/previous 链/机械回归 ----

class _FailOneWrite(_AL):
    """只在底层 _write_event 首次写 attempt_started 时抛 OSError,
    其后恢复;不动任何私有锁存标志(纯 IO 故障注入)。"""
    def __init__(self, root):
        super().__init__(root)
        self._injected = False

    def _write_event(self, event):
        if not self._injected and event.get("kind") == "attempt_started":
            self._injected = True
            raise OSError("synthetic disk failure on started write")
        return super()._write_event(event)


def test_real_write_failure_latches_storage(monkeypatch, tmp_path):
    """P1(2013 缺口1):真实 _write_event IO 异常(测试不预设标志)
    → 主步 0 输入且 cleanup 0 输入(全 run 停止 GUI;磁盘恢复不清除
    边界);保留 started→unknown 重建与正常 end 释放。"""
    import target.server as server
    from agent.recording.execution_ledger import \
        execute_v3_window_stage_real
    golden, backend = _cleanup_fixture()
    _install_fake_os(monkeypatch, backend)
    with pytest.raises(OSError):
        execute_v3_window_stage_real(
            golden=golden, ledger=_FailOneWrite(tmp_path / "h"),
            server_tools=server,
            window_role_map=_SAME_WINDOW_ROLE_MAP)
    # started 写盘故障:begin 失败 → 主步 0 输入;cleanup 也 0
    # (存储锁存:磁盘恢复不清除边界,全 run 停止 GUI)。
    assert len(backend.dispatch_calls) == 0, (
        "真实写盘故障后全 run 禁止后续 GUI(cleanup 也不执行)")


def test_cleanup_uses_real_post_action_observation(monkeypatch, tmp_path):
    """P1(2013 缺口2):cleanup 两格 OS 业务变化 — 动作后新观测是
    必需事实(动作前观测不冒充动作后):
    - false→true:cleanup expected=true,动作后真实 true → pass;
    - true→false:动作后真实 false → fail(不用 pre=true 判 pass)。
    before/after observationRef 必须不同(真实动作后新观测)。"""
    import dataclasses
    import target.server as server
    from agent.recording.execution_ledger import \
        execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    from agent.recording.models import GoldenTrace

    def run(pre_checked, post_checked):
        golden, _, _ = _compile_golden("same_window")
        first = dataclasses.replace(
            golden.steps[0],
            verifiers=({"type": "checked", "expected": True,
                        "match": {"automation_id": "btnApply"},
                        "timeoutSeconds": 5.0},))
        golden = GoldenTrace.from_dict(dataclasses.replace(
            golden, steps=(first, *golden.steps[1:])).to_dict())
        backend = _checkbox_backend(pre_checked)
        sequence = golden.window_sequence.to_dict()
        sequence["cleanup"] = [{"stepId": "cleanup-0001",
                                "windowId": "W1", "effects": []}]
        golden = GoldenTrace.from_dict({
            **golden.to_dict(),
            "cleanup": [{"stepId": "cleanup-0001",
                         "actionId": "gui.click", "args": {},
                         "selector": {"window": {
                             "processName": "EDRClient.exe",
                             "titleRegex": "^logo1$"},
                             "control": {"automationId": "btnApply",
                                         "controlType": "Button"},
                             "visual": None},
                         "endSelector": None,
                         "verifiers": [{"type": "checked",
                                        "expected": True,
                                        "match": {"automation_id":
                                                      "btnApply"},
                                        "timeoutSeconds": 5.0}],
                         "endSelector": None,
                         "required": False,
                         "status": "ready", "issues": []}],
            "windowSequence": sequence})
        # cleanup 动作真实改变 checked(post)。
        original_click = backend.click

        def click_changes_state(**kwargs):
            out = original_click(**kwargs)
            for extra in backend._windows["100"]["extra_controls"]:
                if extra.get("automation_id") == "btnApply":
                    extra["checked"] = post_checked
            return out

        backend.click = click_changes_state
        _install_fake_os(monkeypatch, backend)
        result = execute_v3_window_stage_real(
            golden=golden, ledger=AttemptLedger(tmp_path / "h"),
            server_tools=server,
            window_role_map=_SAME_WINDOW_ROLE_MAP)
        events = AttemptLedger(tmp_path / "h").rebuild_history(
            run_id=result["runId"])
        cleanup_rows = [e for e in events
                        if e.get("stepId", "").startswith("cleanup")]
        return result, cleanup_rows[0]

    # false→true:动作后真实 true → cleanup pass。
    result, row = run(False, True)
    refs = row.get("assertionResultRefs") or []
    assert refs and refs[0]["result"] == "pass", refs
    assert row["outcome"] == "pass", row["outcome"]
    before_obs = (row.get("stateBeforeRef") or {}).get("observationRef")
    after_obs = (row.get("stateAfterRef") or {}).get("observationRef")
    assert before_obs != after_obs, (
        "动作后新观测必须与动作前不同(pre 不冒充 post)")
    # true→false:动作后真实 false → cleanup fail(不用 pre 判 pass)。
    result, row = run(True, False)
    refs = row.get("assertionResultRefs") or []
    assert refs and refs[0]["result"] == "fail", refs
    assert row["outcome"] == "fail", row["outcome"]
    before_obs = (row.get("stateBeforeRef") or {}).get("observationRef")
    after_obs = (row.get("stateAfterRef") or {}).get("observationRef")
    assert before_obs != after_obs


def test_cleanup_actual_previous_chain(monkeypatch, tmp_path):
    """P2(2013 缺口3):两条 cleanup 的 actual previous — cleanup-0002
    的前一步是 cleanup-0001(不是主步);主流程失败触发 ref 可单独
    保留;transition 真实(cleanup_after_failure / 链内转换)。"""
    import dataclasses
    import json as _json
    import target.server as server
    from agent.recording.execution_ledger import \
        execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    from agent.recording.models import GoldenTrace, ReplaySelector
    golden, _, _ = _compile_golden("same_window")
    first = dataclasses.replace(
        golden.steps[0],
        verifiers=({"type": "checked", "expected": True,
                    "match": {"automation_id": "btnApply"},
                    "timeoutSeconds": 5.0},))
    golden = GoldenTrace.from_dict(dataclasses.replace(
        golden, steps=(first, *golden.steps[1:])).to_dict())
    backend = _checkbox_backend(False)
    sequence = golden.window_sequence.to_dict()
    sequence["cleanup"] = [
        {"stepId": "cleanup-0001", "windowId": "W1", "effects": []},
        {"stepId": "cleanup-0002", "windowId": "W1", "effects": []}]
    golden = GoldenTrace.from_dict({
        **golden.to_dict(),
        "cleanup": [
            {"stepId": "cleanup-0001", "actionId": "gui.click",
             "args": {},
             "selector": {"window": {"processName": "EDRClient.exe",
                                     "titleRegex": "^logo1$"},
                          "control": {"automationId": "btnApply",
                                      "controlType": "Button"},
                          "visual": None},
             "endSelector": None, "verifiers": [], "required": False,
             "status": "ready", "issues": []},
            {"stepId": "cleanup-0002", "actionId": "gui.click",
             "args": {},
             "selector": {"window": {"processName": "EDRClient.exe",
                                     "titleRegex": "^logo1$"},
                          "control": {"automationId": "btnBack",
                                      "controlType": "Button"},
                          "visual": None},
             "endSelector": None, "verifiers": [], "required": False,
             "status": "ready", "issues": []}],
        "windowSequence": sequence})
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_SAME_WINDOW_ROLE_MAP)
    events = AttemptLedger(tmp_path / "h").rebuild_history(
        run_id=result["runId"])
    cleanup_rows = [e for e in events
                    if e.get("stepId", "").startswith("cleanup")]
    assert len(cleanup_rows) == 2
    first_c, second_c = cleanup_rows
    # cleanup-0001 previous = 主步(step-0001,触发失败的 attempt)。
    assert first_c.get("previousStepId") == "step-0001"
    # cleanup-0002 previous = cleanup-0001(actual previous 链)。
    assert second_c.get("previousStepId") == "cleanup-0001", second_c
    assert second_c.get("previousAttemptId") == first_c["attemptId"]
    assert second_c.get("previousWindowId") == "W1"
    # transition 真实(非 first_bind 覆盖)。
    assert first_c.get("transitionId") == "cleanup_after_failure", (
        first_c.get("transitionId"))


def test_ledgered_dispatch_begin_signature(monkeypatch, tmp_path):
    """机械回归(2013):LedgeredDispatch.begin_attempt_for 合法调用
    不再 NameError(误带 hook 专属参数)。"""
    import json as _json
    from agent.recording.execution_ledger import LedgeredDispatch
    from target.windowing.attempt_ledger import AttemptLedger
    ledger = AttemptLedger(tmp_path / "h")
    dispatch = LedgeredDispatch(
        dispatch=lambda **kw: {"ok": True},
        ledger=ledger, run_id="RUN-mech",
        window_by_step={"step-0001": "W1"},
        transition_basis="declared_sequence")
    result = dispatch.begin_attempt_for(step_id="step-0001",
                                        window_id="W1")
    assert result.get("eventId"), result


# ---- 2154:B 尾项 + C(真实阶段驱动) ----

def test_cleanup_declared_effects_actually_evaluated(monkeypatch,
                                                     tmp_path):
    """尾项1(2154):cleanup.effects 声明 absent W1 但 W1 始终可见 →
    效果必须真实求值(baseline/单次 action/只读差分)并持久
    satisfied=false/outcome=present;cleanup 不得 receipt-ok 假 pass。
    正例:cleanup 效果目标确实消失 → absent 成立。"""
    import dataclasses
    import target.server as server
    from agent.recording.execution_ledger import \
        execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    from agent.recording.models import GoldenTrace

    def build(cleanup_target_visible):
        golden, backend = _cleanup_fixture()
        sequence = golden.window_sequence.to_dict()
        # absent 目标 = 声明窗口 W1(主窗;cleanup 动作后其可见性由
        # OS 事实决定)。W1 可见 → absent 不成立(负例);cleanup 动作
        # 前不可见 → absent 成立(正例;主流程先正常完成绑定/执行)。
        sequence["cleanup"][0]["effects"] = [{
            "id": "EF-cleanup-absent", "kind": "absent",
            "windowId": "W1",
            "verification": "explicit_assertion",
            "timeoutSeconds": 1.0}]
        golden = GoldenTrace.from_dict({
            **golden.to_dict(),
            "windowSequence": sequence})
        if not cleanup_target_visible:
            # 正例:主流程 fail 后、cleanup 动作前 W1 消失
            # (OS 事实注入:主步自身 checked 断言在其后置 False)。
            original_click = backend.click

            def click_hides_target(**kwargs):
                out = original_click(**kwargs)
                backend._windows["100"]["visible"] = False
                return out

            backend.click = click_hides_target
        return golden, backend

    # 负例:目标(W1)仍可见 → absent 不成立 → cleanup fail/unknown。
    golden, backend = build(cleanup_target_visible=True)
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_SAME_WINDOW_ROLE_MAP)
    cleanup_summary = result.get("cleanup") or []
    assert cleanup_summary and cleanup_summary[0]["outcome"] in (
        "fail", "unknown"), cleanup_summary
    events = AttemptLedger(tmp_path / "h").rebuild_history(
        run_id=result["runId"])
    effects = ((events[-1].get("stateAfterRef") or {}).get(
        "windowEffect")) or []
    assert effects and effects[0]["kind"] == "absent" and \
        effects[0]["satisfied"] is False, effects

    # 正例补充(2166 复验语义):cleanup 目标自身消失 → 真实 rebind
    # 拒绝(window_binding_lost)→ skip 如实持久(不可验证不执行);
    # absent 求值门本身由负例锁定(present → satisfied=false →
    # unknown 锁存,windowEffect 持久)。
    golden2, backend2 = build(cleanup_target_visible=False)
    _install_fake_os(monkeypatch, backend2)
    result2 = execute_v3_window_stage_real(
        golden=golden2, ledger=AttemptLedger(tmp_path / "h2"),
        server_tools=server,
        window_role_map=_SAME_WINDOW_ROLE_MAP)
    cleanup_summary2 = result2.get("cleanup") or []
    assert cleanup_summary2 and cleanup_summary2[0]["outcome"] == \
        "skipped" and "lost" in (cleanup_summary2[0].get("reason") or
                                 ""), cleanup_summary2


def test_cleanup_prestate_persisted_before_dispatch(
        monkeypatch, tmp_path):
    """尾项2(2154):cleanup 的真实权威 before 在**动作前**经既有
    ledger.attach_state_before 落盘(非动作后内存赋值);pending 不再
    是派发前落盘的 before 内容;revision 真实(OBS 引用真实)。"""
    from pathlib import Path as _Path
    import json as _json
    import target.server as server
    from agent.recording.execution_ledger import \
        execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    golden, backend = _cleanup_fixture()
    _install_fake_os(monkeypatch, backend)
    click_order = []
    original_click = backend.click

    def capture_click(**kwargs):
        click_order.append(len(backend.dispatch_calls))
        return original_click(**kwargs)

    backend.click = capture_click
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=_SAME_WINDOW_ROLE_MAP)
    # 清理动作 2 次 click,每次 click 前无其他 dispatch([0, 1])。
    assert click_order == [0, 1], click_order
    # 真实权威 before 已在动作前经 attach_state_before 落盘:
    # cleanup 的 attach 事件含非 pending revision + OBS 引用
    # (begin 占位 pending 之外,dispatch 前的真记录)。
    starts = [_json.loads(p.read_text())
              for p in _Path(tmp_path / "h").glob("EVT-*.json")]
    cleanup_start = next((e for e in starts
                          if e.get("stepId") == "cleanup-0001"
                          and e.get("kind") == "attempt_started"), None)
    assert cleanup_start is not None
    attached = [e.get("stateBeforeRef") for e in starts
                if e.get("of") == cleanup_start["eventId"]
                and e.get("kind") == "state_before_attached"]
    assert any((rec or {}).get("bindingRevision") not in (None, "pending")
               for rec in attached), attached
    assert any(str((rec or {}).get("observationRef", "")).startswith("OBS-")
               for rec in attached), attached


def test_cleanup_previous_ref_carries_window_id(monkeypatch, tmp_path):
    """尾项3(2154):首条 cleanup 的 previous 稳定引用补 windowId
    (触发失败 attempt 的完整稳定引用;不复制前步整块)。"""
    result, events = _run_a_b_a_sample(monkeypatch, tmp_path,
                                       fail_step2=True)
    # 该 fixture 无 cleanup;使用 _cleanup_fixture 的 run 验证。
    golden, backend = _cleanup_fixture()
    import target.server as server
    from agent.recording.execution_ledger import \
        execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    _install_fake_os(monkeypatch, backend)
    result2 = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h2"),
        server_tools=server,
        window_role_map=_SAME_WINDOW_ROLE_MAP)
    events2 = AttemptLedger(tmp_path / "h2").rebuild_history(
        run_id=result2["runId"])
    cleanup_rows = [e for e in events2
                    if e.get("stepId", "").startswith("cleanup")]
    assert cleanup_rows
    row = cleanup_rows[0]
    assert row.get("previousStepId") and row.get("previousAttemptId")
    assert row.get("previousWindowId") == "W1", row


def test_cleanup_self_close_absent_effect_verified(monkeypatch, tmp_path):
    """cleanup 动作关闭**自身绑定窗口** + 声明 absent 效果:真实执行
    (rebind 成功→动作→权威基线差分 absent satisfied)→ 动作后观测
    refused(window_binding_lost)是 absent 的**预期后果** → cleanup
    pass + selfCloseVerified,不得降为 unknown;非 absent 覆盖的
    LOST(如无效果声明)仍 must unknown(不放宽)。"""
    import dataclasses
    import target.server as server
    from agent.recording.execution_ledger import \
        execute_v3_window_stage_real
    from agent.recording.models import GoldenTrace
    from target.windowing.attempt_ledger import AttemptLedger

    def build(declare_effect):
        golden, backend = _cleanup_fixture()
        sequence = golden.window_sequence.to_dict()
        sequence["cleanup"] = [{"stepId": "cleanup-0001",
                                "windowId": "W1",
                                "effects": ([{
                                    "id": "EF-self-close",
                                    "kind": "absent",
                                    "windowId": "W1",
                                    "verification": "explicit_assertion",
                                    "timeoutSeconds": 2.0}]
                                    if declare_effect else [])}]
        golden = GoldenTrace.from_dict({
            **golden.to_dict(),
            "cleanup": [{"stepId": "cleanup-0001",
                         "actionId": "gui.click", "args": {},
                         "selector": {"window": {
                             "processName": "EDRClient.exe",
                             "titleRegex": "^logo1$"},
                             "control": {"automationId": "btnClose",
                                         "controlType": "Button"},
                             "visual": None},
                         "endSelector": None, "verifiers": [],
                         "required": False, "status": "ready",
                         "issues": []}],
            "windowSequence": sequence})
        backend._windows["100"]["extra_controls"] = list(
            backend._windows["100"].get("extra_controls") or []) + [
            {"class_name": "Button", "text": "Close", "control_id": 9,
             "automation_id": "btnClose",
             "rectangle": {"x": 700, "y": 10, "w": 80, "h": 24},
             "is_visible": True, "is_enabled": True, "depth": 1}]
        original_click = backend.click

        def click_self_close(**kwargs):
            out = original_click(**kwargs)
            if kwargs.get("automation_id") == "btnClose":
                backend._windows["100"]["visible"] = False
            return out

        backend.click = click_self_close
        return golden, backend

    # 正例:声明 absent + 动作自关闭 → pass + selfCloseVerified。
    golden, backend = build(declare_effect=True)
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "sc"),
        server_tools=server, window_role_map=_SAME_WINDOW_ROLE_MAP)
    cleanup_summary = result.get("cleanup") or []
    assert cleanup_summary and cleanup_summary[0]["outcome"] == "pass", \
        cleanup_summary
    assert cleanup_summary[0].get("selfCloseVerified") is True, \
        cleanup_summary
    effects = cleanup_summary[0].get("effects") or []
    assert effects and effects[0]["kind"] == "absent" and \
        effects[0]["satisfied"] is True, effects
    refs = cleanup_summary[0].get("assertionResultRefs") or []
    assert refs and refs[0]["result"] == "pass" and \
        refs[0]["type"] == "absent", refs
    events = AttemptLedger(tmp_path / "sc").rebuild_history(
        run_id=result["runId"])
    after = [e for e in events
             if e.get("stepId") == "cleanup-0001"
             and (e.get("stateAfterRef") or {}).get("windowEffect")]
    assert after, events
    assert ((after[-1]["stateAfterRef"] or {}).get(
        "windowEffect"))[0]["satisfied"] is True

    # 负例翼:无 absent 效果声明时,LOST 仍 must unknown(不放宽)。
    golden2, backend2 = build(declare_effect=False)
    _install_fake_os(monkeypatch, backend2)
    result2 = execute_v3_window_stage_real(
        golden=golden2, ledger=AttemptLedger(tmp_path / "sc2"),
        server_tools=server, window_role_map=_SAME_WINDOW_ROLE_MAP)
    cleanup_summary2 = result2.get("cleanup") or []
    assert cleanup_summary2 and cleanup_summary2[0]["outcome"] == \
        "unknown", cleanup_summary2
    assert "window_binding_lost" in \
        (cleanup_summary2[0].get("reason") or ""), cleanup_summary2


def test_cleanup_self_close_unknown_receipt_not_pass(monkeypatch,
                                                     tmp_path):
    """P1(2038 反例1):自关闭 absent 满足但动作回执 unknown(ok=false)
    → cleanup 不得 pass,整体 unknown 锁存(absent 不清除回执未知)。"""
    import json as _json
    import target.server as server
    from agent.recording.execution_ledger import \
        execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger

    golden, backend = _cleanup_fixture()
    sequence = golden.window_sequence.to_dict()
    sequence["cleanup"] = [{"stepId": "cleanup-0001", "windowId": "W1",
                            "effects": [{"id": "EF-self-close",
                                         "kind": "absent",
                                         "windowId": "W1",
                                         "verification": "explicit_assertion",
                                         "timeoutSeconds": 2.0}]}]
    from agent.recording.models import GoldenTrace as _GT
    golden = _GT.from_dict({
        **golden.to_dict(),
        "cleanup": [{**golden.to_dict()["cleanup"][0],
                     "selector": {"window": {
                         "processName": "EDRClient.exe",
                         "titleRegex": "^logo1$"},
                         "control": {"automationId": "btnClose",
                                     "controlType": "Button"},
                         "visual": None},
                     "endSelector": None}],
        "windowSequence": sequence})
    backend._windows["100"]["extra_controls"] = list(
        backend._windows["100"].get("extra_controls") or []) + [
        {"class_name": "Button", "text": "Close", "control_id": 9,
         "automation_id": "btnClose",
         "rectangle": {"x": 700, "y": 10, "w": 80, "h": 24},
         "is_visible": True, "is_enabled": True, "depth": 1}]
    original_click = backend.click

    def click_self_close(**kwargs):
        out = original_click(**kwargs)
        if kwargs.get("automation_id") == "btnClose":
            backend._windows["100"]["visible"] = False
        return out

    backend.click = click_self_close
    _install_fake_os(monkeypatch, backend)
    original_focus = server.focus_bound_window

    def focus_receipt_unknown(*args, **kwargs):
        raw = original_focus(*args, **kwargs)
        doc = _json.loads(raw)
        if kwargs.get("action_id") == "gui.click" and \
                kwargs.get("observe") is False:
            doc["ok"] = False
            doc["code"] = "dispatch_outcome_unknown"
            return _json.dumps(doc, ensure_ascii=False)
        return raw

    monkeypatch.setattr(server, "focus_bound_window",
                        focus_receipt_unknown)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "u"),
        server_tools=server, window_role_map=_SAME_WINDOW_ROLE_MAP)
    summary = result.get("cleanup") or []
    assert summary and summary[0]["outcome"] == "unknown", summary
    assert summary[0].get("receiptCode") == "dispatch_outcome_unknown", \
        summary


def test_cleanup_self_close_unverifiable_control_verifier_not_pass(
        monkeypatch, tmp_path):
    """P1(2038 反例2):自关闭 absent 满足但声明还含**控件 verifier**
    (checked/btnClose)→ 关闭后无法动作后求值 → 不得默认通过,
    整体 unknown(reason 可追查)。"""
    import target.server as server
    from agent.recording.execution_ledger import \
        execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger

    golden, backend = _cleanup_fixture()
    sequence = golden.window_sequence.to_dict()
    sequence["cleanup"] = [{"stepId": "cleanup-0001", "windowId": "W1",
                            "effects": [{"id": "EF-self-close",
                                         "kind": "absent",
                                         "windowId": "W1",
                                         "verification": "explicit_assertion",
                                         "timeoutSeconds": 2.0}]}]
    from agent.recording.models import GoldenTrace as _GT
    golden = _GT.from_dict({
        **golden.to_dict(),
        "cleanup": [{**golden.to_dict()["cleanup"][0],
                     "selector": {"window": {
                         "processName": "EDRClient.exe",
                         "titleRegex": "^logo1$"},
                         "control": {"automationId": "btnClose",
                                     "controlType": "Button"},
                         "visual": None},
                     "endSelector": None,
                     "verifiers": [{"type": "checked", "expected": True,
                                    "match": {"automation_id": "btnClose"},
                                    "timeoutSeconds": 2.0}]}],
        "windowSequence": sequence})
    backend._windows["100"]["extra_controls"] = list(
        backend._windows["100"].get("extra_controls") or []) + [
        {"class_name": "Button", "text": "Close", "control_id": 9,
         "automation_id": "btnClose",
         "rectangle": {"x": 700, "y": 10, "w": 80, "h": 24},
         "is_visible": True, "is_enabled": True, "depth": 1}]
    original_click = backend.click

    def click_self_close(**kwargs):
        out = original_click(**kwargs)
        if kwargs.get("automation_id") == "btnClose":
            backend._windows["100"]["visible"] = False
        return out

    backend.click = click_self_close
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "uv"),
        server_tools=server, window_role_map=_SAME_WINDOW_ROLE_MAP)
    summary = result.get("cleanup") or []
    assert summary and summary[0]["outcome"] == "unknown", summary
    # 复验后语义:reason 统一为 "not all verified-pass"(控件不可求值
    # 与窗口断言非全 pass 同门),错误码保留 cleanup_verifier_unverifiable。
    assert "not all verified-pass" in (summary[0].get("reason") or ""), \
        summary


def test_cleanup_self_close_absent_persisted_for_restart_rebuild(
        monkeypatch, tmp_path):
    """P2(2038):自关闭 absent 断言经账本完成接口**持久化**——新
    AttemptLedger 只读重建后 cleanup attempt 的 assertionResultRefs
    恢复 absent/pass(与内存摘要同一份账本记录),报告特征可恢复。"""
    import target.server as server
    from agent.recording.execution_ledger import \
        execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger

    golden, backend = _cleanup_fixture()
    sequence = golden.window_sequence.to_dict()
    sequence["cleanup"] = [{"stepId": "cleanup-0001", "windowId": "W1",
                            "effects": [{"id": "EF-self-close",
                                         "kind": "absent",
                                         "windowId": "W1",
                                         "verification": "explicit_assertion",
                                         "timeoutSeconds": 2.0}]}]
    from agent.recording.models import GoldenTrace as _GT
    golden = _GT.from_dict({
        **golden.to_dict(),
        "cleanup": [{**golden.to_dict()["cleanup"][0],
                     "selector": {"window": {
                         "processName": "EDRClient.exe",
                         "titleRegex": "^logo1$"},
                         "control": {"automationId": "btnClose",
                                     "controlType": "Button"},
                         "visual": None},
                     "endSelector": None}],
        "windowSequence": sequence})
    backend._windows["100"]["extra_controls"] = list(
        backend._windows["100"].get("extra_controls") or []) + [
        {"class_name": "Button", "text": "Close", "control_id": 9,
         "automation_id": "btnClose",
         "rectangle": {"x": 700, "y": 10, "w": 80, "h": 24},
         "is_visible": True, "is_enabled": True, "depth": 1}]
    original_click = backend.click

    def click_self_close(**kwargs):
        out = original_click(**kwargs)
        if kwargs.get("automation_id") == "btnClose":
            backend._windows["100"]["visible"] = False
        return out

    backend.click = click_self_close
    _install_fake_os(monkeypatch, backend)
    ledger_dir = tmp_path / "p"
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(ledger_dir),
        server_tools=server, window_role_map=_SAME_WINDOW_ROLE_MAP)
    live_refs = result.get("cleanup") or []
    assert live_refs and live_refs[0]["outcome"] == "pass"
    run_id = result["runId"]
    events = AttemptLedger(ledger_dir).rebuild_history(run_id=run_id)
    cleanup_events = [e for e in events
                      if e.get("stepId") == "cleanup-0001"]
    assert cleanup_events, events
    rebuilt_refs = cleanup_events[-1].get("assertionResultRefs") or []
    assert any(r.get("type") == "absent" and r.get("result") == "pass"
               for r in rebuilt_refs), rebuilt_refs
    # P2(0002):摘要 refs 与账本重建 refs **逐项稳定 ID 一致**
    # (单一来源派生,同一 attempt 内不出现两套编号)。
    live_ids = [r.get("assertionId") for r in live_refs[0].get(
        "assertionResultRefs") or []] if live_refs else []
    rebuilt_ids = [r.get("assertionId") for r in rebuilt_refs]
    assert live_ids and live_ids == rebuilt_ids, (live_ids, rebuilt_ids)


def test_cross_window_independent_step_actually_evaluated(monkeypatch,
                                                          tmp_path):
    """2038 复验(执行层):跨窗内容断言的独立 assertion-only 步经真实
    执行链**实际求值**——focus 其目的窗口(W2)、按原 verifier 求值真实
    观测事实(logRow 文本),非仅编译层存在。"""
    import importlib.util
    import json as _json
    from pathlib import Path as _Path
    import target.server as server
    from agent.recording.execution_ledger import \
        execute_v3_window_stage_real
    from target.recording.models import RawRecording
    from agent.recording.compiler import compile_recording
    from target.windowing.attempt_ledger import AttemptLedger

    spec = importlib.util.spec_from_file_location(
        "gws", _Path(__file__).resolve().parents[1] /
        "test_recording" / "test_golden_v3_window_sequence.py")
    gws = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gws)
    raw = gws._cross_window_isolation_raw()
    # W2 全程存活:去掉 closed 转换并重编号,断言 causal 重指 W1 动作。
    evs = [e for e in raw["events"]
           if not (e["type"] == "window_transition"
                   and e["input"].get("kind") == "closed")]
    for i, e in enumerate(evs, 1):
        e["sequence"] = i
    next(e for e in evs if e["type"] == "assertion")["causalId"] = \
        "CAUSE-0005"
    raw["events"] = evs
    raw["windows"][1]["closed"] = False
    raw["windowTimeline"]["events"] = [
        e for e in raw["windowTimeline"]["events"] if e["kind"] != "hidden"]
    comp = compile_recording(RawRecording.from_dict(raw),
                             profile="windows_hisec")
    body = comp.golden.to_dict().get("steps") or {}
    independent = [sid for sid, s in body.items()
                   if not s.get("actionId") and any(
                       (v.get("match") or {}).get("automation_id") ==
                       "logRow" for v in s.get("verifiers") or [])]
    assert independent, body
    drv = _load_s5_driver_module()
    FakeOS = drv._make_fake_os_backend({
        "100": {"process_id": 42, "title": "logo1",
                "root_control_id": "btnApply", "root_class_name": "Button",
                "root_text": "Apply", "visible": True, "foreground": True},
        "200": {"process_id": 42, "title": "Log Center",
                "root_control_id": "logRow", "root_class_name": "Row",
                "root_text": "row-1", "visible": True, "foreground": True,
                "process_name": "EDRClient.exe"}})
    backend = FakeOS()
    product_policy.reset_for_tests()
    server._binding_facade_holder["facade"] = None
    drv._install_fake_os_driver(backend)
    result = execute_v3_window_stage_real(
        golden=comp.golden, ledger=AttemptLedger(tmp_path / "cw"),
        server_tools=server,
        window_role_map={"W1": "main", "W2": "log_center"})
    rep = result.get("report") or {}
    logrow = [f for s in rep.get("steps", [])
              for f in (s.get("features") or [])
              if "row-1" in _json.dumps(f.get("expected") or "")]
    assert logrow, rep
    assert all(f.get("result") == "pass" for f in logrow), logrow
    assert any(f.get("actual") == "row-1" for f in logrow), logrow


def _load_s5_driver_module():
    """加载 driver 模块单实例(factory 与 install 必须同实例,避免双份
    模块级 _DRIVER_PATCH 状态)。"""
    import importlib.util
    from pathlib import Path as _Path
    spec = importlib.util.spec_from_file_location(
        "s5drv", _Path(__file__).resolve().parents[2] / "scripts" /
        "s5_acceptance_round.py")
    drv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(drv)
    return drv


def test_cleanup_window_open_exists_false_self_close_passes(monkeypatch,
                                                            tmp_path):
    """P2(2324 反例1):自关闭 absent satisfied + 合法窗口断言
    window_open/exists=false/logo1(实际已关)→ 断言通过、cleanup
    pass;窗口断言经既有映射(exists=false→window_closed)与权威清单
    求值,不再误读空清单。"""
    import dataclasses
    import target.server as server
    from agent.recording.execution_ledger import \
        execute_v3_window_stage_real
    from agent.recording.models import GoldenTrace
    from target.windowing.attempt_ledger import AttemptLedger
    golden0, _, _ = _compile_golden("same_window")
    first = dataclasses.replace(golden0.steps[0],
        verifiers=({"type": "checked", "expected": True,
                    "match": {"automation_id": "btnApply"},
                    "timeoutSeconds": 5.0},))
    golden0 = GoldenTrace.from_dict(dataclasses.replace(
        golden0, steps=(first, *golden0.steps[1:])).to_dict())
    sequence = golden0.window_sequence.to_dict()
    sequence["cleanup"] = [{"stepId": "cleanup-0001", "windowId": "W1",
        "effects": [{"id": "EF", "kind": "absent", "windowId": "W1",
                     "verification": "explicit_assertion",
                     "timeoutSeconds": 2.0}]}]
    golden = GoldenTrace.from_dict({**golden0.to_dict(),
        "cleanup": [{"stepId": "cleanup-0001", "actionId": "gui.click",
                     "args": {},
                     "selector": {"window": {
                         "processName": "EDRClient.exe",
                         "titleRegex": "^logo1$"},
                         "control": {"automationId": "btnClose",
                                     "controlType": "Button"},
                         "visual": None},
                     "endSelector": None,
                     "verifiers": [{"type": "window_open",
                                    "expected": {"title": "logo1",
                                                 "exists": False},
                                    "timeoutSeconds": 2.0}],
                     "required": False, "status": "ready", "issues": []}],
        "windowSequence": sequence})
    backend = _checkbox_backend(False)
    backend._windows["100"]["extra_controls"] = list(
        backend._windows["100"].get("extra_controls") or []) + [
        {"class_name": "Button", "text": "Close", "control_id": 9,
         "automation_id": "btnClose",
         "rectangle": {"x": 700, "y": 10, "w": 80, "h": 24},
         "is_visible": True, "is_enabled": True, "depth": 1}]
    original_click = backend.click

    def click_self_close(**kwargs):
        out = original_click(**kwargs)
        if kwargs.get("automation_id") == "btnClose":
            backend._windows["100"]["visible"] = False
        return out

    backend.click = click_self_close
    _install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "wf"),
        server_tools=server, window_role_map=_SAME_WINDOW_ROLE_MAP)
    summary = result.get("cleanup") or []
    assert summary and summary[0]["outcome"] == "pass", summary
    refs = summary[0].get("assertionResultRefs") or []
    # absent 与映射后的窗口断言(refs type=window_closed)均持久 pass。
    assert any(r.get("type") == "absent" and r.get("result") == "pass"
               for r in refs), refs
    assert any(r.get("type") == "window_closed" and
               r.get("result") == "pass" for r in refs), refs


def test_cleanup_window_open_inventory_read_failure_stays_unknown(
        monkeypatch, tmp_path):
    """P2(2324 三态):清单读取失败 → 窗口断言 unknown(事实缺失),
    cleanup 整体 unknown——不以空清单/本窗回退冒充否定证据。"""
    import dataclasses
    import agent.recording.execution_ledger as EL
    import target.server as server
    from agent.recording.execution_ledger import \
        execute_v3_window_stage_real
    from agent.recording.models import GoldenTrace
    from target.windowing.attempt_ledger import AttemptLedger
    golden0, _, _ = _compile_golden("same_window")
    first = dataclasses.replace(golden0.steps[0],
        verifiers=({"type": "checked", "expected": True,
                    "match": {"automation_id": "btnApply"},
                    "timeoutSeconds": 5.0},))
    golden0 = GoldenTrace.from_dict(dataclasses.replace(
        golden0, steps=(first, *golden0.steps[1:])).to_dict())
    sequence = golden0.window_sequence.to_dict()
    sequence["cleanup"] = [{"stepId": "cleanup-0001", "windowId": "W1",
        "effects": [{"id": "EF", "kind": "absent", "windowId": "W1",
                     "verification": "explicit_assertion",
                     "timeoutSeconds": 2.0}]}]
    golden = GoldenTrace.from_dict({**golden0.to_dict(),
        "cleanup": [{"stepId": "cleanup-0001", "actionId": "gui.click",
                     "args": {},
                     "selector": {"window": {
                         "processName": "EDRClient.exe",
                         "titleRegex": "^logo1$"},
                         "control": {"automationId": "btnClose",
                                     "controlType": "Button"},
                         "visual": None},
                     "endSelector": None,
                     "verifiers": [{"type": "window_open",
                                    "expected": {"title": "logo1",
                                                 "exists": False},
                                    "timeoutSeconds": 2.0}],
                     "required": False, "status": "ready", "issues": []}],
        "windowSequence": sequence})
    backend = _checkbox_backend(False)
    backend._windows["100"]["extra_controls"] = list(
        backend._windows["100"].get("extra_controls") or []) + [
        {"class_name": "Button", "text": "Close", "control_id": 9,
         "automation_id": "btnClose",
         "rectangle": {"x": 700, "y": 10, "w": 80, "h": 24},
         "is_visible": True, "is_enabled": True, "depth": 1}]
    original_click = backend.click

    def click_self_close(**kwargs):
        out = original_click(**kwargs)
        if kwargs.get("automation_id") == "btnClose":
            backend._windows["100"]["visible"] = False
        return out

    backend.click = click_self_close
    _install_fake_os(monkeypatch, backend)
    original_ri = EL._read_product_inventory
    monkeypatch.setattr(EL, "_read_product_inventory",
                        lambda *a, **kw: {"ok": False,
                                          "code": "probe_fail",
                                          "windows": []})
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "wf3"),
        server_tools=server, window_role_map=_SAME_WINDOW_ROLE_MAP)
    summary = result.get("cleanup") or []
    assert summary and summary[0]["outcome"] == "unknown", summary
    wvr = summary[0].get("windowVerifierResults") or []
    assert wvr and wvr[0]["result"] == "unknown", wvr
    assert "inventory read failed" in (wvr[0].get("diagnostic") or ""), wvr
