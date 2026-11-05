"""S3:window-binding MCP 工具在 target server 的实际注册、真实初始化链路
与 §7 绑定契约(09 号设计;1744 复验六缺口的失败回归)。

驱动真实 target.server 模块:真实 begin_product_run → WindowSession/
registry → facade → bind_entry_window/focus_bound_window → 真实
execute_action dispatcher。fake 只替换底层 OS 事实层(枚举/连接/锁定/
输入注入),不替换授权、租约、dispatcher、observation。

六组缺口(1744 复验 §"S3 原范围仍未闭合的反例"):
1. golden W 引用 vs profile role 混同 — 本 run 须接受严格 golden 窗口
   声明并在已装产品授权域内验证;W1/W2 A→B→A 接口链;
2. supported=true, coverage=unknown, generation=0 仍 capability true 且
   bind 成功 — 严格 supported、所需覆盖与有效 generation 必须同时满足;
3. 候选解析未走 path-owned 归属 — 同名 UNTRUSTED exe 首绑成功、exe 查询
   0 次;首次与聚焦后/dispatch 前均须核查;
4. 真实 dispatcher 正向链不通 — focus_bound_window 未建立权威
   connect/lock/binding,execute_action window_identity_stale、OS 回调
   0 次;须补恰一次正确实例动作的正例;
5. 泛化异常(非 WindowBindingRefusal)直接逃逸 — 须结构化 executed=
   unknown,不误称零执行;
6. end_product_run 后 holder 仍持旧 facade、_bindings/_token_to_window
   各留 1 — 须用既有 run 清理路径清本 run 状态;测试保留旧引用核对。

无 GUI:backend 为 fake OS 层,租约走真实 product_policy。
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

from target import product_policy
from target.product_profiles import get_capability
from target.windowing.binding import WindowBindingRefusal

REAL_DIGEST = get_capability("windows_hisec", "windows_pywinauto").digest
_INSTALLED_PATH = "C:\\Program Files\\HiSec-Endpoint\\core\\EDRClient.exe"


@pytest.fixture(autouse=True)
def _clean_lease():
    product_policy.reset_for_tests()
    import target.server as server
    server._binding_facade_holder["facade"] = None
    yield
    product_policy.reset_for_tests()
    server._binding_facade_holder["facade"] = None


def _register_tools():
    import target.server as server
    for name in ("bind_entry_window", "focus_bound_window",
                 "window_binding_state"):
        tool = asyncio.run(server.mcp.get_tool(name))
        assert tool is not None, f"{name} 必须注册在 FastMCP"
        assert callable(getattr(server, name)), (
            f"{name} 的直接 Python 调用路径必须保留")


class _FakeWindow:
    """替身已连接窗口(WindowsGUI.main_window 的角色):
    _connected_window_state / _activate_locked_window 走它。"""

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
    """替身 OS 事实层:匹配真实 WindowsPywinautoBackend 对外协议
    (_enumerate_top_windows_windows/is_window_open/connect_window_identity/
    lock_window/get_window_lock/verify_window_lock/click/click_at/
    dump_tree/process_name_for_pid),经真实 WindowSession/registry/
    WindowGuard/dispatcher 全链路。路径归属经 psutil.Process(pid).exe()
    缝隙注入(默认返回已装路径)。"""

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
        # 聚焦/激活计数(复验 1845 缺口2:归属拒绝前不得激活):
        # focus_existing_window 与 lock_window(activate=True) 分别计数。
        self.focus_calls: list[dict] = []
        self.lock_activate_calls: list[dict] = []

    # ---- WindowSession.refresh 协议 ----

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

    # ---- 连接/锁定(权威锁链 §7.2 步骤3) ----

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
        if activate:
            self.lock_activate_calls.append({"pid": pid})
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

    # ---- 路径归属缝隙(§7.1 授权产品域) ----

    def exe_path_for_pid(self, pid):
        self.exe_queries.append(int(pid))
        return self._exe_path

    # ---- 观测/输入(真实 dispatcher 后端方法) ----

    def dump_tree(self, window_title_re=None, max_depth=10):
        if self._lock_state is None:
            return {"ok": False, "error": "No window connected"}
        controls = []
        index = 0
        for native_id, w in sorted(self._windows.items(),
                                   key=lambda kv: int(kv[0])):
            index += 1
            # 窗口可声明自身根控件的类别/文本(root_class_name/root_text;
            # 缺省保持原投影 — 既有测试不变)。
            controls.append({
                "class_name": w.get("root_class_name", "Button"),
                "text": w.get("root_text", w.get("title", "")),
                "control_id": index,
                "automation_id": w.get("root_control_id", ""),
                "rectangle": {"x": 10, "y": 10, "w": 100, "h": 30},
                "is_visible": True, "is_enabled": True, "depth": 0,
            })
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
        return {"ok": True, "method": "mouse.click", "x": int(x), "y": int(y)}

    def activate_app(self, app_name=None, bundle_id=None):
        self.dispatch_calls.append({"method": "activate_app",
                                    "app_name": app_name})
        return {"ok": True, "method": "activate_registered"}

    def is_window_open(self, title_re=None, process_name=None,
                       class_name=None):
        live = self._enumerate_top_windows_windows()
        return {"ok": True, "windows": live, "count": len(live)}

    def focus_existing_window(self, *, handle, pid, title, process_name):
        self.focus_calls.append({"handle": int(handle), "pid": int(pid)})
        if self._lock_state is not None and str(
                self._lock_state.get("handle")) == str(handle):
            return {"ok": True, "method": "focus_existing_window",
                    "handle": int(handle), "pid": int(pid)}
        connected = self.connect_window_identity(handle, pid)
        if not connected.get("ok"):
            return {"ok": False, "error": connected.get("error", "gone")}
        return {"ok": True, "method": "focus_existing_window",
                "handle": int(handle), "pid": int(pid)}

    def window_sequence_capability(self):
        return dict(self._capability)

    def lifecycle_event_stream(self):
        """本 run 生命周期事件源替身(fake OS 事件源):healthy 流 +
        create 基线;capability 协商经跟踪事实与自报取交集。"""
        from target.windowing import instance_tracker
        windows = []
        for native_id, w in self._windows.items():
            if w.get("visible", True) is False:
                continue
            windows.append({"kind": "create", "native_id": str(native_id),
                            "pid": w.get("process_id", 42),
                            "root_control_id": w.get("root_control_id", ""),
                            "class_name": w.get("class_name",
                                                "FakeWnd")})
        return instance_tracker.LifecycleEventStream(
            events=windows, healthy=True)


_MAIN_WINDOW = {"process_id": 42, "title": "logo1",
                "root_control_id": "MainRoot",
                "visible": True, "foreground": True}
_LOG_WINDOW = {"process_id": 43, "title": "Log Center",
               "root_control_id": "LogRoot",
               "visible": True, "foreground": True}


def _real_begin(server, monkeypatch, backend, lease_seconds=300):
    """真实初始化入口:注入 fake OS backend 后调用 server.begin_product_run。
    windows_hisec 的 window_roles 含 entry/main/log_center/dialog。
    同时把统一 dispatcher 的 backend resolver 指回 server 的活 backend
    (其他测试会覆盖该 resolver;本文件的正向链必须走注入的 fake OS 层)。"""
    monkeypatch.setattr(server, "_backend", backend, raising=False)
    monkeypatch.setattr(server, "_backend_kind", "windows_pywinauto")
    try:
        from target.action_dispatcher import set_backend_resolver
    except ImportError:
        from action_dispatcher import set_backend_resolver
    set_backend_resolver(lambda: server._backend)
    result = json.loads(server.begin_product_run(
        "windows_hisec", REAL_DIGEST, lease_seconds=lease_seconds))
    assert result["ok"] is True, result
    return result


def _bind_declared(server, run, window_id, identity):
    return json.loads(server.bind_entry_window(
        window_id, dict(identity), run["run_token"]))


# ---- 保留:注册面与租约门禁 ----

def test_binding_tools_registered_on_fastmcp():
    _register_tools()


def test_no_active_run_lease_refused():
    import target.server as server
    result = json.loads(server.bind_entry_window(
        "W1", {"processName": "EDRClient.exe"}, "tok-nobody"))
    assert result["ok"] is False
    assert result["code"] == "window_binding_lease_refused"
    state = json.loads(server.window_binding_state("W1", "WB-x", "tok-nobody"))
    assert state["code"] == "window_binding_lease_refused"
    focus = json.loads(server.focus_bound_window(
        "W1", "WB-x", "tok-nobody"))
    assert focus["code"] == "window_binding_lease_refused"
    assert focus["executed"] is False


# ---- 缺口1:golden W 声明与 role 分离,W1/W2 A→B→A 接口链 ----

def test_golden_W_declaration_accepted_and_ab_flow_binds(monkeypatch):
    """本 run 接受调用者提交的严格 golden 窗口声明(W1/W2 各自身份),
    在已装产品授权域(profile roles 的 process 域)内验证;W1+main 合法
    身份绑定成功,W2 独立绑定,同一 token 可 A→B→A 往返 reverify。
    golden W 是引用不是授权:身份冲突/未声明 W 仍拒绝。"""
    import target.server as server
    backend = _FakeOSBackend({"100": dict(_MAIN_WINDOW),
                              "200": dict(_LOG_WINDOW)})
    run = _real_begin(server, monkeypatch, backend)
    try:
        golden = {
            "W1": {"processName": "EDRClient.exe",
                   "titleRegex": "^logo1$"},
            "W2": {"processName": "EDRClient.exe",
                   "titleRegex": "^Log Center$"},
        }
        reg = json.loads(server.register_window_declaration(
            json.dumps(golden, ensure_ascii=False), run["run_token"]))
        assert reg.get("ok") is True, reg
        b1 = _bind_declared(server, run, "W1", golden["W1"])
        assert "binding_token" in b1, b1
        assert str(b1["bound_native_id"]) == "100"
        b2 = _bind_declared(server, run, "W2", golden["W2"])
        assert "binding_token" in b2, b2
        assert str(b2["bound_native_id"]) == "200"
        # A→B→A:W1 的原 token 往返 reverify 仍 BOUND。
        re1 = json.loads(server.reverify_bound_window(
            "W1", b1["binding_token"], run["run_token"]))
        assert re1.get("state") == "BOUND", re1
        re2 = json.loads(server.reverify_bound_window(
            "W2", b2["binding_token"], run["run_token"]))
        assert re2.get("state") == "BOUND", re2
        re3 = json.loads(server.reverify_bound_window(
            "W1", b1["binding_token"], run["run_token"]))
        assert re3.get("state") == "BOUND", re3
        # 调用者身份与声明冲突 → 拒绝;未声明 W3 → 拒绝。
        conflict = json.loads(server.bind_entry_window(
            "W1", {"processName": "Other.exe"}, run["run_token"]))
        assert conflict.get("ok") is False
        undeclared = json.loads(server.bind_entry_window(
            "W3", {"processName": "EDRClient.exe"}, run["run_token"]))
        assert undeclared.get("ok") is False
        # golden 声明越出产品授权域(非已装 profile 进程)→ 拒绝。
        outside = json.loads(server.register_window_declaration(
            json.dumps({"W9": {"processName": "Notepad.exe",
                               "titleRegex": "^x$"}}),
            run["run_token"]))
        assert outside.get("ok") is False, outside
    finally:
        product_policy.end_product_run(run["run_token"])


# ---- 缺口2:supported/coverage/generation 三者同时门 ----

def test_unknown_coverage_or_zero_generation_refuses(monkeypatch):
    """supported=true 但 coverage=unknown 或 generation<=0 → capability
    必须 false,bind 副作用前拒绝(§8:任一字段自报 true 不放行)。"""
    import target.server as server
    for capability in ({"supported": True, "coverage": "unknown",
                        "generation": 0},
                       {"supported": True, "coverage": "unknown",
                        "generation": 2},
                       {"supported": True, "coverage": "strong",
                        "generation": 0}):
        backend = _FakeOSBackend({"100": dict(_MAIN_WINDOW)},
                                 capability=capability)
        run = _real_begin(server, monkeypatch, backend)
        try:
            facade = server._binding_facade_holder.get("facade")
            assert facade.capability()["supported"] is not True, (
                f"{capability} 不得 capability true,实际 "
                f"{facade.capability()}")
            declared = facade._declared
            bind_result = _bind_declared(server, run, "main",
                                         declared.get("main", {}))
            assert bind_result.get("ok") is False, bind_result
            assert bind_result.get("code") == (
                "window_sequence_capability_unavailable")
            assert backend.dispatch_calls == []
        finally:
            product_policy.end_product_run(run["run_token"])


# ---- 保留(1744 已认可):supported=false 严格透传 ----

def test_backend_unsupported_capability_refuses_bind(monkeypatch):
    import target.server as server
    backend = _FakeOSBackend(
        {"100": dict(_MAIN_WINDOW)},
        capability={"supported": False, "coverage": "strong",
                    "generation": 2})

    class Sourceless:
        """无事件源工厂的 backend(真 unsupported:协商权威缺失)。"""

        def __init__(self, delegate):
            self._delegate = delegate

        def __getattr__(self, name):
            if name == "lifecycle_event_stream":
                raise AttributeError(name)
            return getattr(self._delegate, name)

    run = _real_begin(server, monkeypatch, Sourceless(backend))
    try:
        facade = server._binding_facade_holder.get("facade")
        assert facade.capability()["supported"] is False
        declared = facade._declared
        bind_result = _bind_declared(server, run, "main",
                                     declared.get("main", {}))
        assert bind_result.get("ok") is False, bind_result
        assert bind_result.get("code") == (
            "window_sequence_capability_unavailable")
        assert backend.dispatch_calls == []
    finally:
        product_policy.end_product_run(run["run_token"])


# ---- 缺口3:path-owned 归属核查 ----

def test_unowned_same_name_exe_refused_and_path_checked(monkeypatch):
    """候选进程 exe 指向 UNTRUSTED 同名路径:首绑拒绝,进程路径核查
    ≥1 次;租约不是归属证明,不能以标题/进程名授权同名无关进程。"""
    import target.server as server
    backend = _FakeOSBackend({"100": dict(_MAIN_WINDOW)},
                             exe_path="C:\\UNTRUSTED\\EDRClient.exe")
    run = _real_begin(server, monkeypatch, backend)
    try:
        facade = server._binding_facade_holder.get("facade")
        declared = facade._declared
        bind_result = _bind_declared(server, run, "main",
                                     dict(declared["main"]))
        assert bind_result.get("ok") is False, (
            f"UNTRUSTED 同名进程不得绑定,实际 {bind_result}")
        assert backend.exe_queries, (
            "首绑必须核查候选进程路径(路径归属),实际 0 次")
    finally:
        product_policy.end_product_run(run["run_token"])


def test_focus_and_dispatch_path_rechecked(monkeypatch):
    """聚焦后/dispatch 前归属漂移(运行中 exe 换为 UNTRUSTED 路径):
    focus 必须 0 dispatch。"""
    import target.server as server
    backend = _FakeOSBackend({"100": dict(_MAIN_WINDOW)})
    run = _real_begin(server, monkeypatch, backend)
    try:
        facade = server._binding_facade_holder.get("facade")
        declared = facade._declared
        bind_result = _bind_declared(server, run, "main",
                                     dict(declared["main"]))
        assert "binding_token" in bind_result, bind_result
        token = bind_result["binding_token"]
        # 绑定后进程路径漂移到 UNTRUSTED。
        backend._exe_path = "C:\\UNTRUSTED\\EDRClient.exe"
        receipt = json.loads(server.focus_bound_window(
            "main", token, run["run_token"], action_id="app.activate"))
        assert receipt.get("ok") is False, receipt
        assert backend.dispatch_calls == [], (
            "归属漂移后不得进入执行回调")
    finally:
        product_policy.end_product_run(run["run_token"])


# ---- 缺口4:真实 dispatcher 正向链(权威锁 + 恰一次执行) ----

def test_real_dispatch_through_authoritative_lock(monkeypatch):
    """bind → focus_bound_window → 真实 execute_action:经权威
    connect/lock/binding 链,app.activate 与 pointer.click_screen 各恰
    一次命中 fake OS 输入层;session.registry.locked 非空。"""
    import target.server as server
    backend = _FakeOSBackend({"100": dict(_MAIN_WINDOW)})
    run = _real_begin(server, monkeypatch, backend)
    try:
        facade = server._binding_facade_holder.get("facade")
        declared = facade._declared
        bind_result = _bind_declared(server, run, "main",
                                     dict(declared["main"]))
        token = bind_result["binding_token"]
        session = server._window_session
        locked_before = session.registry.locked
        r1 = json.loads(server.focus_bound_window(
            "main", token, run["run_token"], action_id="app.activate"))
        assert r1.get("ok") is True, f"激活应成功,实际 {r1}"
        assert session.registry.locked is not None, (
            "权威窗口锁必须建立")
        r2 = json.loads(server.focus_bound_window(
            "main", token, run["run_token"],
            action_id="pointer.click_screen",
            action_args={"x": 10, "y": 10}))
        assert r2.get("ok") is True, f"屏幕点击应成功,实际 {r2}"
        methods = [c["method"] for c in backend.dispatch_calls]
        assert methods.count("activate_app") == 1, methods
        assert methods.count("click_at") == 1, methods
    finally:
        product_policy.end_product_run(run["run_token"])


def test_real_dispatch_replacement_zero_actions(monkeypatch):
    """真实 dispatcher 路径下的替换防护:聚焦成功后锁定实例身份漂移
    (pid 变化)→ 恰 0 次执行,execute_action 旧安全门保留。"""
    import target.server as server
    backend = _FakeOSBackend({"100": dict(_MAIN_WINDOW)})
    run = _real_begin(server, monkeypatch, backend)
    try:
        facade = server._binding_facade_holder.get("facade")
        declared = facade._declared
        bind_result = _bind_declared(server, run, "main",
                                     dict(declared["main"]))
        token = bind_result["binding_token"]
        # 聚焦回调内替换 owner pid(真实 connect/lock 之间漂移)。
        original_connect = backend.connect_window_identity

        def connect_then_replace(handle, pid, timeout=10.0):
            result = original_connect(handle, pid, timeout)
            backend._windows["100"]["process_id"] = 99
            backend._gui.main_window = _FakeWindow(100, 99, "logo1")
            return result

        backend.connect_window_identity = connect_then_replace
        receipt = json.loads(server.focus_bound_window(
            "main", token, run["run_token"], action_id="app.activate"))
        assert receipt.get("ok") is False, receipt
        assert backend.dispatch_calls == [], (
            "替换后必须 0 执行,实际 "
            f"{backend.dispatch_calls}")
    finally:
        product_policy.end_product_run(run["run_token"])


# ---- 保留(1744 已认可):visible/foreground 各自 0 回调 ----

def test_invisible_or_background_instance_never_executes(monkeypatch):
    import target.server as server
    for field in ("visible", "foreground"):
        backend = _FakeOSBackend({"100": dict(_MAIN_WINDOW)})
        run = _real_begin(server, monkeypatch, backend)
        try:
            facade = server._binding_facade_holder.get("facade")
            declared = facade._declared
            bind_result = _bind_declared(server, run, "main",
                                         dict(declared["main"]))
            token = bind_result["binding_token"]
            backend._windows["100"][field] = False
            receipt = json.loads(server.focus_bound_window(
                "main", token, run["run_token"], action_id="app.activate"))
            assert receipt.get("ok") is False, receipt
            assert backend.dispatch_calls == [], (
                f"{field}=false 实例不得进入执行回调")
        finally:
            product_policy.end_product_run(run["run_token"])


# ---- 缺口5:泛化异常结构化 unknown ----

def test_generic_exception_after_dispatch_structured_unknown(monkeypatch):
    """执行回调进入分发(副作用已发生)后抛 RuntimeError:必须结构化
    executed=unknown 返回,不得未捕获逃逸、不得误称零执行、不得吞原因。"""
    import target.server as server
    backend = _FakeOSBackend({"100": dict(_MAIN_WINDOW)})
    run = _real_begin(server, monkeypatch, backend)
    try:
        facade = server._binding_facade_holder.get("facade")
        declared = facade._declared
        bind_result = _bind_declared(server, run, "main",
                                     dict(declared["main"]))
        token = bind_result["binding_token"]
        side_effects = []
        original = server.execute_action

        def exploding_execute(**kwargs):
            side_effects.append(kwargs)
            raise RuntimeError("local fake callback after side effect")

        server.execute_action = exploding_execute
        try:
            response = json.loads(server.focus_bound_window(
                "main", token, run["run_token"], action_id="app.activate"))
        finally:
            server.execute_action = original
        assert side_effects, "执行回调必须已被调用(测试前提)"
        assert isinstance(response, dict), response
        assert response.get("executed") == "unknown", (
            f"泛化异常须结构化 unknown,实际 {response}")
        assert response.get("ok") is False
        assert "RuntimeError" in json.dumps(response), (
            "不得吞掉既有错误原因")
    finally:
        product_policy.end_product_run(run["run_token"])


# ---- 保留(1744 已认可):WindowBindingRefusal → unknown ----

def test_binding_refusal_after_dispatch_reports_unknown(monkeypatch):
    import target.server as server
    backend = _FakeOSBackend({"100": dict(_MAIN_WINDOW)})
    run = _real_begin(server, monkeypatch, backend)
    try:
        facade = server._binding_facade_holder.get("facade")
        declared = facade._declared
        bind_result = _bind_declared(server, run, "main",
                                     dict(declared["main"]))
        token = bind_result["binding_token"]
        side_effects = []
        original = server.execute_action

        def refusing_execute(**kwargs):
            side_effects.append(kwargs)
            raise WindowBindingRefusal(
                "window_binding_dispatch_unknown",
                "receipt lost after the action entered dispatch")

        server.execute_action = refusing_execute
        try:
            response = json.loads(server.focus_bound_window(
                "main", token, run["run_token"], action_id="app.activate"))
        finally:
            server.execute_action = original
        assert side_effects
        assert response.get("executed") == "unknown", response
    finally:
        product_policy.end_product_run(run["run_token"])


# ---- 缺口6:end 后 facade 内存状态随 run 清理路径清空 ----

def test_end_product_run_clears_facade_memory(monkeypatch):
    """真实 end_product_run(既有 run 清理 hook)后:holder facade 为空,
    旧 facade._bindings/_token_to_window 清空(保留旧对象引用核对,
    不手工 holder=None 掩盖)。"""
    import target.server as server
    backend = _FakeOSBackend({"100": dict(_MAIN_WINDOW)})
    run = _real_begin(server, monkeypatch, backend)
    facade = server._binding_facade_holder.get("facade")
    declared = facade._declared
    bind_result = _bind_declared(server, run, "main", dict(declared["main"]))
    token = bind_result["binding_token"]
    assert len(facade._bindings) == 1
    assert len(facade._token_to_window) == 1
    end = json.loads(server.end_product_run(run["run_token"]))
    assert end.get("ok") is True, end
    assert server._binding_facade_holder.get("facade") is None, (
        "清理 hook 必须释放本 run 的 facade")
    assert len(facade._bindings) == 0, (
        f"旧 facade 绑定表必须清空,实际 {facade._bindings}")
    assert len(facade._token_to_window) == 0, (
        f"旧 facade token 映射必须清空,实际 {facade._token_to_window}")
    # 旧 token 访问仍拒绝(1744 已认可项不回退)。
    state = json.loads(server.window_binding_state(
        "main", token, run["run_token"]))
    assert state.get("ok") is False, state


# ---- 保留(1744 已认可):租约失效后访问拒绝 ----

def test_state_refuses_after_real_end_product_run(monkeypatch):
    import target.server as server
    backend = _FakeOSBackend({"100": dict(_MAIN_WINDOW)})
    run = _real_begin(server, monkeypatch, backend)
    facade = server._binding_facade_holder.get("facade")
    declared = facade._declared
    bind_result = _bind_declared(server, run, "main", dict(declared["main"]))
    token = bind_result["binding_token"]
    state = json.loads(server.window_binding_state(
        "main", token, run["run_token"]))
    assert state.get("ok") is True and state.get("state") == "BOUND"
    end = json.loads(server.end_product_run(run["run_token"]))
    assert end.get("ok") is True, end
    state2 = json.loads(server.window_binding_state(
        "main", token, run["run_token"]))
    assert state2.get("ok") is False, state2
    bind2 = _bind_declared(server, run, "main", dict(declared["main"]))
    assert bind2.get("ok") is False, bind2
    focus2 = json.loads(server.focus_bound_window(
        "main", token, run["run_token"], action_id="app.activate"))
    assert focus2.get("ok") is False
    assert focus2.get("executed") is False


# ---- 1845 缺口1:只读重验的完整归属/身份 + 失效锁存 ----

def test_reverify_checks_product_ownership(monkeypatch):
    """绑定后进程路径漂移为 UNTRUSTED:reverify 必须核查产品归属
    (路径查询≥1)并拒绝 — 不得仍 BOUND、不得路径查询 0 次。"""
    import target.server as server
    backend = _FakeOSBackend({"100": dict(_MAIN_WINDOW)})
    run = _real_begin(server, monkeypatch, backend)
    try:
        facade = server._binding_facade_holder.get("facade")
        bind_result = _bind_declared(server, run, "main",
                                     dict(facade._declared["main"]))
        token = bind_result["binding_token"]
        assert server._window_session.registry.locked is None
        backend._exe_path = "C:\\UNTRUSTED\\EDRClient.exe"
        backend.exe_queries.clear()
        result = json.loads(server.reverify_bound_window(
            "main", token, run["run_token"]))
        assert result.get("ok") is False, (
            f"UNTRUSTED 归属漂移必须拒绝,实际 {result}")
        assert backend.exe_queries, (
            "reverify 必须核查候选/绑定进程路径,实际 0 次")
        assert facade._bindings["main"].state == "LOST", (
            "归属失效必须锁存 LOST")
    finally:
        product_policy.end_product_run(run["run_token"])


def test_identity_failure_latches_lost_and_never_revives(monkeypatch):
    """标题漂移重验失败:必须锁存 LOST(内部 state 不得仍 BOUND);
    属性改回后下一次重验仍拒绝 — 不以属性恢复自动复活失效绑定。"""
    import target.server as server
    backend = _FakeOSBackend({"100": dict(_MAIN_WINDOW)})
    run = _real_begin(server, monkeypatch, backend)
    try:
        facade = server._binding_facade_holder.get("facade")
        bind_result = _bind_declared(server, run, "main",
                                     dict(facade._declared["main"]))
        token = bind_result["binding_token"]
        backend._windows["100"]["title"] = "Wrong title"
        first = json.loads(server.reverify_bound_window(
            "main", token, run["run_token"]))
        assert first.get("ok") is False, first
        assert first.get("code") == "window_binding_lost"
        assert facade._bindings["main"].state == "LOST", (
            "身份重验失败必须锁存 LOST,实际 "
            f"{facade._bindings['main'].state}")
        # 属性恢复:LOST 绑定不自动复活。
        backend._windows["100"]["title"] = "logo1"
        second = json.loads(server.reverify_bound_window(
            "main", token, run["run_token"]))
        assert second.get("ok") is False, (
            f"LOST 绑定不得因属性恢复自动 BOUND,实际 {second}")
        assert second.get("code") == "window_binding_lost"
        assert facade._bindings["main"].state == "LOST"
        assert server._window_session.registry.locked is None, (
            "失效绑定的权威锁不得保留")
    finally:
        product_policy.end_product_run(run["run_token"])


# ---- 1845 缺口2:归属失效时不得先聚焦/激活 ----

def test_unowned_process_never_focused_or_activated(monkeypatch):
    """绑定后进程路径漂移:focus 必须 0 次 focus_existing_window、
    0 次 lock(activate=True)、0 次输入 — 归属/绑定完整事实在首次
    聚焦/连接激活前核查。"""
    import target.server as server
    backend = _FakeOSBackend({"100": dict(_MAIN_WINDOW)})
    run = _real_begin(server, monkeypatch, backend)
    try:
        facade = server._binding_facade_holder.get("facade")
        bind_result = _bind_declared(server, run, "main",
                                     dict(facade._declared["main"]))
        token = bind_result["binding_token"]
        backend._exe_path = "C:\\UNTRUSTED\\EDRClient.exe"
        receipt = json.loads(server.focus_bound_window(
            "main", token, run["run_token"],
            action_id="pointer.click_screen", action_args={"x": 10, "y": 10}))
        assert receipt.get("ok") is False, receipt
        assert backend.focus_calls == [], (
            f"归属失效不得先聚焦,实际 focus={backend.focus_calls}")
        assert backend.lock_activate_calls == [], (
            f"归属失效不得先激活锁定,实际 lock={backend.lock_activate_calls}")
        assert backend.dispatch_calls == [], "输入必须 0 次"
    finally:
        product_policy.end_product_run(run["run_token"])


# ---- 1845 缺口3:语义动作 target_ref 贯通 ----

def test_semantic_click_with_target_ref_executes_once(monkeypatch):
    """同租约内贯通绑定新观测与 target_ref:真实 gui.click 经
    observation→ref 校验,恰一次命中正确目标;stale ref 与其他窗口
    的 ref 均 0 输入。dispatcher 安全门保留。两步契约:先
    action_id=None+observe=True 建立权威锁并取本窗口新观测(返回
    dispatcher 校验所用的同一投影),再携 ref 派发。"""
    import target.server as server
    backend = _FakeOSBackend({"100": dict(_MAIN_WINDOW)})
    run = _real_begin(server, monkeypatch, backend)
    try:
        facade = server._binding_facade_holder.get("facade")
        bind_result = _bind_declared(server, run, "main",
                                     dict(facade._declared["main"]))
        token = bind_result["binding_token"]
        # 第一步:权威锁 + 新观测(无动作)。
        locked_obs = json.loads(server.focus_bound_window(
            "main", token, run["run_token"], action_id=None,
            observe=True))
        assert locked_obs.get("ok") is True, locked_obs
        assert locked_obs.get("executed") is False
        observation_id = locked_obs["observation_id"]
        projected = locked_obs["controls"]["T0001"]
        target_ref = {
            "snapshot_id": observation_id,
            "target_id": "T0001",
            "fingerprint": projected["fingerprint"],
            "expected_process_name": "EDRClient.exe",
            "selector_hint": {"automation_id":
                                  projected.get("automation_id")},
        }
        receipt = json.loads(server.focus_bound_window(
            "main", token, run["run_token"], action_id="gui.click",
            action_args={"expected_process_name": "EDRClient.exe"},
            target_ref=target_ref, observe=False))
        assert receipt.get("ok") is True, (
            f"语义点击应成功,实际 {receipt}")
        clicks = [c for c in backend.dispatch_calls
                  if c.get("method") == "click"]
        assert len(clicks) == 1, backend.dispatch_calls
        # stale ref(指向已被作废的观测)→ 0 输入。
        stale = json.loads(server.focus_bound_window(
            "main", token, run["run_token"], action_id="gui.click",
            action_args={"expected_process_name": "EDRClient.exe"},
            target_ref=target_ref, observe=False))
        assert stale.get("ok") is False, stale
        # 其他窗口的 target_id → 0 输入。
        bad_ref = dict(target_ref)
        bad_ref["target_id"] = "T9999"
        bad = json.loads(server.focus_bound_window(
            "main", token, run["run_token"], action_id="gui.click",
            action_args={"expected_process_name": "EDRClient.exe"},
            target_ref=bad_ref, observe=False))
        assert bad.get("ok") is False, bad
        assert len([c for c in backend.dispatch_calls
                    if c.get("method") == "click"]) == 1, (
            backend.dispatch_calls)
    finally:
        product_policy.end_product_run(run["run_token"])


# ---- 1845 缺口4:解锁异常不漏清理 ----

def test_unlock_failure_still_clears_run_memory(monkeypatch):
    """unlock_window 抛异常:清理 hook 用 finally/分项清理保证本 run
    facade/绑定/token 始终清除,同时保留解锁错误(不吞、不掩盖)。"""
    import target.server as server
    backend = _FakeOSBackend({"100": dict(_MAIN_WINDOW)})
    run = _real_begin(server, monkeypatch, backend)
    facade = server._binding_facade_holder.get("facade")
    bind_result = _bind_declared(server, run, "main",
                                 dict(facade._declared["main"]))
    original_unlock = backend.unlock_window

    def failing_unlock():
        raise RuntimeError("fake OS unlock failed")

    backend.unlock_window = failing_unlock
    try:
        end = json.loads(server.end_product_run(run["run_token"]))
        assert end.get("ok") is True, (
            f"清理完成应成功返回并保留解锁错误信息,实际 {end}")
        assert "unlock" in json.dumps(end).lower(), (
            f"解锁失败原因必须可见,实际 {end}")
    finally:
        backend.unlock_window = original_unlock
    assert server._binding_facade_holder.get("facade") is None
    assert len(facade._bindings) == 0, facade._bindings
    assert len(facade._token_to_window) == 0, facade._token_to_window


def test_lease_expiry_still_clears_run_memory(monkeypatch):
    """租约过期路径同样清理 facade 内存(旧对象引用核对)。"""
    import target.server as server
    import time as _time
    backend = _FakeOSBackend({"100": dict(_MAIN_WINDOW)})
    run = _real_begin(server, monkeypatch, backend, lease_seconds=1)
    facade = server._binding_facade_holder.get("facade")
    bind_result = _bind_declared(server, run, "main",
                                 dict(facade._declared["main"]))
    assert len(facade._bindings) == 1
    _time.sleep(1.1)
    # 过期在任何 require_product_run 时触发清理 hook。
    state = json.loads(server.window_binding_state(
        "main", bind_result["binding_token"], run["run_token"]))
    assert state.get("ok") is False, state
    assert server._binding_facade_holder.get("facade") is None
    assert len(facade._bindings) == 0, facade._bindings
    assert len(facade._token_to_window) == 0, facade._token_to_window


# ---- 1908 缺口1:失效必须撤销权威执行能力(真实 GuardedBackend 链) ----

def _guarded_run(server, monkeypatch, windows=None):
    """真实 begin → 真实 GuardedBackend 包裹 fake OS(1908 口径),
    返回 (backend内核, run, facade, binding)。"""
    import target.server as server
    inner = _FakeOSBackend(windows or {"100": dict(_MAIN_WINDOW)})
    run = _real_begin(server, monkeypatch,
                      product_policy.GuardedBackend(inner, "windows_pywinauto"))
    facade = server._binding_facade_holder.get("facade")
    binding = _bind_declared(server, run, "main",
                             dict(facade._declared["main"]))
    return inner, run, facade, binding


def test_lost_binding_revokes_authoritative_lock(monkeypatch):
    """先真实建锁(focus+observe),再标题漂移:reverify 返回 lost 且
    LOST 后,registry 权威锁必须已撤销;恢复标题后直接旧
    execute_action(pointer.click_screen)0 输入 — 旧 dispatcher/ref
    不能再执行;属性恢复不复活。"""
    import target.server as server
    inner, run, facade, binding = _guarded_run(server, monkeypatch)
    try:
        locked_obs = json.loads(server.focus_bound_window(
            "main", binding["binding_token"], run["run_token"],
            action_id=None, observe=True))
        assert locked_obs.get("ok") is True, locked_obs
        session = server._window_session
        assert session.registry.locked is not None, "前置:锁已建立"
        inner._windows["100"]["title"] = "Wrong title"
        failure = json.loads(server.reverify_bound_window(
            "main", binding["binding_token"], run["run_token"]))
        assert failure.get("code") == "window_binding_lost"
        assert facade._bindings["main"].state == "LOST"
        assert session.registry.locked is None, (
            "LOST 必须撤销本 binding 的权威 registry 锁")
        # 属性恢复后,旧 dispatcher 直接派发必须 0 输入。
        inner._windows["100"]["title"] = "logo1"
        inner.dispatch_calls.clear()
        receipt = json.loads(server.execute_action(
            action_id="pointer.click_screen", args={"x": 10, "y": 10},
            run_token=run["run_token"]))
        assert receipt.get("ok") is False, (
            f"失效后旧 dispatcher 门必须拒绝,实际 {receipt}")
        assert inner.dispatch_calls == [], (
            "失效绑定不得保留任何输入执行能力")
        # LOST 绑定自身也不能复活。
        revive = json.loads(server.reverify_bound_window(
            "main", binding["binding_token"], run["run_token"]))
        assert revive.get("ok") is False, revive
    finally:
        product_policy.end_product_run(run["run_token"])


def test_focus_ownership_failure_latches_lost(monkeypatch):
    """focus 路径归属失败(unowned)也必须锁存 LOST;路径恢复后同
    token 不得直接输入 — 与 reverify 失效语义统一。"""
    import target.server as server
    inner, run, facade, binding = _guarded_run(server, monkeypatch)
    try:
        inner._exe_path = "C:\\UNTRUSTED\\EDRClient.exe"
        first = json.loads(server.focus_bound_window(
            "main", binding["binding_token"], run["run_token"],
            action_id="pointer.click_screen", action_args={"x": 10, "y": 10}))
        assert first.get("code") == "window_binding_unowned_process"
        assert facade._bindings["main"].state == "LOST", (
            f"focus 归属失败必须锁存 LOST,实际 "
            f"{facade._bindings['main'].state}")
        assert server._window_session.registry.locked is None
        # 路径恢复:LOST 绑定不复活、同 token 0 输入。
        inner._exe_path = _INSTALLED_PATH
        inner.dispatch_calls.clear()
        second = json.loads(server.focus_bound_window(
            "main", binding["binding_token"], run["run_token"],
            action_id="pointer.click_screen", action_args={"x": 10, "y": 10}))
        assert second.get("ok") is False, (
            f"LOST 绑定同 token 不得复活执行,实际 {second}")
        assert inner.dispatch_calls == []
    finally:
        product_policy.end_product_run(run["run_token"])


# ---- 1908 缺口2:未进入 dispatcher 的观测拒绝是 executed=false ----

def test_pre_dispatch_observation_refusal_is_not_unknown(monkeypatch):
    """observe=True + 旧 snapshot ref:拒绝发生在观测准备阶段,
    dispatcher 调用 0、输入 0 → executed 必须 false(不得误称动作
    回执未知);真正进入 dispatcher 后异常才 unknown。"""
    import target.server as server
    inner, run, facade, binding = _guarded_run(server, monkeypatch)
    try:
        calls = []
        original = server.execute_action

        def counting_dispatch(**kwargs):
            calls.append(kwargs)
            return original(**kwargs)

        server.execute_action = counting_dispatch
        try:
            result = json.loads(server.focus_bound_window(
                "main", binding["binding_token"], run["run_token"],
                action_id="gui.click", observe=True,
                target_ref={"snapshot_id": "OLD", "target_id": "T0001"}))
        finally:
            server.execute_action = original
        assert result.get("code") == "observation_stale", result
        assert calls == [], "观测准备失败不得调用 dispatcher"
        assert inner.dispatch_calls == []
        assert result.get("executed") is False, (
            f"未进入 dispatcher 的拒绝不得标 unknown,实际 "
            f"{result.get('executed')}")
    finally:
        product_policy.end_product_run(run["run_token"])


# ---- 1908 缺口3:cleanup 结果经真实路径传回 end 响应 ----

def test_cleanup_error_reported_through_real_guarded_backend(monkeypatch):
    """真实 GuardedBackend 下底层 unlock 抛错:end 响应必须结构化
    携带清理错误(不靠重复预解锁猜测 hook 结果);内存仍清理;
    错误 token 不解锁他人锁。"""
    import target.server as server
    inner, run, facade, binding = _guarded_run(server, monkeypatch)
    try:
        locked_obs = json.loads(server.focus_bound_window(
            "main", binding["binding_token"], run["run_token"],
            action_id=None, observe=True))
        assert locked_obs.get("ok") is True
        assert server._window_session.registry.locked is not None

        calls = []
        original_unlock = inner.unlock_window

        def failing_unlock():
            calls.append(True)
            raise RuntimeError("fake OS unlock failed behind real guard")

        inner.unlock_window = failing_unlock
        end = json.loads(server.end_product_run(run["run_token"]))
        assert len(calls) == 1, f"实际底层 unlock 恰一次,实际 {calls}"
        assert end.get("ok") is True, end
        assert end.get("cleanup_errors"), (
            f"清理错误必须结构化呈现在 end 响应,实际 {end}")
        assert "RuntimeError" in json.dumps(end.get("cleanup_errors"))
        # 内存仍清理(1908 认可项不回退)。
        assert server._binding_facade_holder.get("facade") is None
        assert len(facade._bindings) == 0
        assert server._window_session is None
        # 错误 token 不解锁他人:此场景已无他人锁,验证 owner run
        # 已结束且 unlock 只发生 1 次(不是重复预解锁)。
        conflict = json.loads(server.end_product_run("invalid-token"))
        assert conflict.get("ok") is False, conflict
        assert len(calls) == 1, "错误 token 不得触发底层 unlock"
    finally:
        inner.unlock_window = original_unlock


def test_invalid_end_token_never_unlocks_other_run(monkeypatch):
    """错误 token 对照(1908):真实 GuardedBackend 保护下,unlock 0 次、
    原锁保留、owner run 仍活动。"""
    import target.server as server
    inner, run, facade, binding = _guarded_run(server, monkeypatch)
    try:
        locked_obs = json.loads(server.focus_bound_window(
            "main", binding["binding_token"], run["run_token"],
            action_id=None, observe=True))
        assert locked_obs.get("ok") is True
        calls = []
        original_unlock = inner.unlock_window

        def counting_unlock():
            calls.append(True)
            return original_unlock()

        inner.unlock_window = counting_unlock
        result = json.loads(server.end_product_run("invalid-other-run-token"))
        assert result.get("ok") is False, result
        assert calls == [], "错误 token 不得触发底层 unlock"
        assert server._window_session.registry.locked is not None, (
            "原锁必须保留")
        assert product_policy.require_product_run(
            run["run_token"]) is not None, "owner run 仍活动"
    finally:
        inner.unlock_window = original_unlock
        product_policy.end_product_run(run["run_token"])


# ---- 1938 缺口:后端撤锁的精确绑定/租约边界 ----

def _bind_second_window(server, run):
    """注册并绑定 W2(Log Center),返回 (declaration, binding)。"""
    declaration = {"W2": {"processName": "EDRClient.exe",
                          "titleRegex": "^Log Center$"}}
    registered = json.loads(server.register_window_declaration(
        json.dumps(declaration), run["run_token"]))
    assert registered.get("ok") is True, registered
    binding = _bind_declared(server, run, "W2", declaration["W2"])
    assert "binding_token" in binding, binding
    return declaration, binding


def test_backend_revocation_isolated_to_own_binding(monkeypatch):
    """A(main) 与 B(W2) 各自绑定、只 B 建锁:B 直接 dispatcher 前置正例
    成功;A 归属失效拒绝并 LOST 后,B 的 registry/backend 锁、观测与
    合法执行能力必须全部保持(A 的撤销不得触碰 B 的 backend 锁)。"""
    import target.server as server
    inner = _FakeOSBackend({"100": dict(_MAIN_WINDOW),
                            "200": dict(_LOG_WINDOW)})
    inner._windows["100"]["foreground"] = False
    run = _real_begin(server, monkeypatch,
                      product_policy.GuardedBackend(inner, "windows_pywinauto"))
    try:
        facade = server._binding_facade_holder.get("facade")
        binding_a = _bind_declared(server, run, "main",
                                   dict(facade._declared["main"]))
        _, binding_b = _bind_second_window(server, run)
        prepared = json.loads(server.focus_bound_window(
            "W2", binding_b["binding_token"], run["run_token"],
            action_id=None, observe=True))
        assert prepared.get("ok") is True, prepared
        registry = server._window_session.registry
        assert registry.locked.native_id == "200"
        assert str(inner._lock_state.get("handle")) == "200"
        before = json.loads(server.execute_action(
            action_id="pointer.click_screen", args={"x": 10, "y": 10},
            run_token=run["run_token"]))
        assert before.get("ok") is True, before
        inner.dispatch_calls.clear()
        # A 的 exe 漂移 → focus A 拒绝。
        inner._exe_path = "C:\\UNTRUSTED\\EDRClient.exe"
        unlock_calls = []
        original_unlock = inner.unlock_window

        def counting_unlock():
            unlock_calls.append(True)
            return original_unlock()

        inner.unlock_window = counting_unlock
        failure = json.loads(server.focus_bound_window(
            "main", binding_a["binding_token"], run["run_token"],
            action_id="pointer.click_screen", action_args={"x": 10, "y": 10}))
        assert failure.get("code") == "window_binding_unowned_process"
        assert facade._bindings["main"].state == "LOST"
        # B 全部保持:registry 锁、backend 锁、合法执行能力。
        assert registry.locked is not None and registry.locked.native_id == "200", (
            "B 的 registry 锁必须保持")
        assert inner._lock_state is not None and str(
            inner._lock_state.get("handle")) == "200", (
            f"A 的失效不得清 B 的 backend 锁,实际 {inner._lock_state}")
        assert unlock_calls == [], (
            f"A 的撤销不得触碰 B 持有的 backend 锁,unlock={unlock_calls}")
        after = json.loads(server.execute_action(
            action_id="pointer.click_screen", args={"x": 10, "y": 10},
            run_token=run["run_token"]))
        assert after.get("ok") is True, (
            f"B 的合法执行能力必须保持,实际 {after}")
    finally:
        product_policy.end_product_run(run["run_token"])


def test_own_backend_lock_revoked_inside_lease_boundary(monkeypatch):
    """A 自己建锁后失效:registry 锁与 backend 锁都必须真正撤销
    (撤销发生在本 run 的授权边界内,GuardedBackend 不以
    product_run_conflict 拒绝;unlock 结构化返回必须检查)。"""
    import target.server as server
    inner, run, facade, binding = _guarded_run(server, monkeypatch)
    try:
        prepared = json.loads(server.focus_bound_window(
            "main", binding["binding_token"], run["run_token"],
            action_id=None, observe=True))
        assert prepared.get("ok") is True, prepared
        assert inner._lock_state is not None
        unlock_calls = []
        original_unlock = inner.unlock_window

        def counting_unlock():
            unlock_calls.append(True)
            return original_unlock()

        inner.unlock_window = counting_unlock
        inner._windows["100"]["title"] = "Wrong title"
        failure = json.loads(server.reverify_bound_window(
            "main", binding["binding_token"], run["run_token"]))
        assert failure.get("code") == "window_binding_lost"
        assert server._window_session.registry.locked is None
        assert len(unlock_calls) == 1, (
            f"本绑定 backend 锁必须在授权边界内真实撤销,unlock={unlock_calls}")
        assert inner._lock_state is None, (
            f"backend 锁必须已清,实际 {inner._lock_state}")
    finally:
        product_policy.end_product_run(run["run_token"])


def test_expired_lease_never_touches_foreign_backend_lock(monkeypatch):
    """错误/过期 lease 对照(1938):错误 token 的失效尝试不撤销他人
    backend/registry 锁;owner run 保持活动。"""
    import target.server as server
    inner, run, facade, binding = _guarded_run(server, monkeypatch)
    try:
        prepared = json.loads(server.focus_bound_window(
            "main", binding["binding_token"], run["run_token"],
            action_id=None, observe=True))
        assert prepared.get("ok") is True
        unlock_calls = []
        original_unlock = inner.unlock_window

        def counting_unlock():
            unlock_calls.append(True)
            return original_unlock()

        inner.unlock_window = counting_unlock
        # 错误 token 的绑定状态读取/失效尝试都被租约门禁拒绝,
        # 不触发任何 backend unlock。
        state = json.loads(server.window_binding_state(
            "main", binding["binding_token"], "invalid-token"))
        assert state.get("ok") is False
        reverify = json.loads(server.reverify_bound_window(
            "main", binding["binding_token"], "invalid-token"))
        assert reverify.get("ok") is False
        assert unlock_calls == [], (
            f"错误 token 不得触发 backend unlock,实际 {unlock_calls}")
        assert server._window_session.registry.locked is not None, (
            "他人 registry 锁必须保持")
        assert inner._lock_state is not None, "他人 backend 锁必须保持"
        assert product_policy.require_product_run(
            run["run_token"]) is not None
    finally:
        inner.unlock_window = original_unlock
        product_policy.end_product_run(run["run_token"])


# ---- 1956 验收补验:真实过期(非 invalid-token)撤锁对照 ----

def test_actual_lease_expiry_then_new_owner_stale_revocation(monkeypatch):
    """真实过期(policy 时钟前移 1 秒 lease 实际到期触发 cleanup):
    旧 run RPC 拒绝、旧 facade 内存清空;新 owner 同 backend 取得
    新锁;旧 token 的 reverify 与旧 facade 对象的直接撤销尝试均
    0 unlock — 不影响新 owner 的 registry/backend 锁与合法执行。"""
    import target.server as server
    inner = _FakeOSBackend({"100": dict(_MAIN_WINDOW)})
    guarded = product_policy.GuardedBackend(inner, "windows_pywinauto")
    run = _real_begin(server, monkeypatch, guarded, lease_seconds=1)
    old_facade = server._binding_facade_holder.get("facade")
    old_bound = _bind_declared(server, run, "main",
                               dict(old_facade._declared["main"]))
    old_binding = old_facade._bindings["main"]
    try:
        # 真实过期:policy 时钟前移(无 sleep),触发 lazy expiry cleanup。
        import target.product_policy as policy_module
        frozen = policy_module.time.monotonic()
        monkeypatch.setattr(policy_module.time, "monotonic",
                            lambda: frozen + 2)
        expired = json.loads(server.window_binding_state(
            "main", old_bound["binding_token"], run["run_token"]))
        assert expired.get("ok") is False, expired
        assert len(old_facade._bindings) == 0, (
            "过期 cleanup 必须清空旧 facade 绑定表")
        # 新 owner:同 backend 取得新 run 并建立新锁。
        new_run = _real_begin(server, monkeypatch, guarded)
        new_facade = server._binding_facade_holder.get("facade")
        new_bound = _bind_declared(server, new_run, "main",
                                   dict(new_facade._declared["main"]))
        prepared = json.loads(server.focus_bound_window(
            "main", new_bound["binding_token"], new_run["run_token"],
            action_id=None, observe=True))
        assert prepared.get("ok") is True, prepared
        assert server._window_session.registry.locked is not None
        assert inner._lock_state is not None
        # 旧 RPC 与旧 facade 对象的撤销尝试:0 unlock。
        unlock_calls = []
        original_unlock = inner.unlock_window

        def counting_unlock():
            unlock_calls.append(True)
            return original_unlock()

        inner.unlock_window = counting_unlock
        stale_rpc = json.loads(server.reverify_bound_window(
            "main", old_bound["binding_token"], run["run_token"]))
        assert stale_rpc.get("ok") is False, stale_rpc
        old_facade._revoke_execution(old_binding)
        assert unlock_calls == [], (
            f"过期 lease 的撤销尝试不得触碰新 owner 锁,unlock={unlock_calls}")
        assert server._window_session.registry.locked is not None
        assert inner._lock_state is not None
        receipt = json.loads(server.execute_action(
            action_id="pointer.click_screen", args={"x": 10, "y": 10},
            run_token=new_run["run_token"]))
        assert receipt.get("ok") is True, (
            f"新 owner 合法执行必须保持,实际 {receipt}")
    finally:
        product_policy.end_product_run(new_run["run_token"])
