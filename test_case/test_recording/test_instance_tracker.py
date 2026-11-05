"""生产 Windows backend 本 run 强实例跟踪底座(09 §7.1/7.4/§8)失败回归。

真实链:server.begin_product_run → 跟踪协商/WindowSession/registry →
facade → 真实 dispatcher;fake 仅替换底层 OS 事件/窗口/输入源。

契约:
- capability 结构化协商:supported 只能由本 run 真实事件源+有效代际+
  实际覆盖+可检测失效事实共同证明;订阅不可用/初始化异常/覆盖不足/
  丢事件/乱序 → unsupported/unknown,GUI 副作用前拒绝;
- 同 handle/pid/root/class 两枚举间 DESTROY+CREATE → 新代际;
  旧 binding 不得借句柄复用恢复;
- HIDE/SHOW 同实例连续性(同 native/同 owner/身份)不换代际;
- 多同名实例各自独立代际;
- 跟踪线程故障 → 事实失效(unknown),不冒充连续;
- 本 run 结束/过期:旧订阅清理,不影响新 owner;
- focus 至 dispatch 替换 → 0 输入(现有门保留);
- 持久状态无 handle/pid/token。
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

from target import product_policy
from target.automation import instance_events as events
from target.product_profiles import get_capability
from target.automation.windows_pywinauto import WindowsPywinautoBackend
from target.windowing import instance_tracker
from target.windowing import instance_tracker

def _th_daemon(target, args):
    """测试用真实 daemon 线程工厂:只构造**未启动**的 Thread —
    WindowsEventHookSource.start 契约是取得未启动线程后由 source
    独占 start(0506 缺口1:factory 内 t.start 会让 source 二次
    start 同对象 → threads can only be started once)。"""
    return threading.Thread(target=target,
                            args=tuple(args.values()), daemon=True)


REAL_DIGEST = get_capability("windows_hisec", "windows_pywinauto").digest


def _primary(state):
    """单实例观察的主事实(测试辅助)。"""
    assert state.instances, "observation must contain instances"
    return state.instances[0]


class _FakeEventSource:
    """替身 OS 事件/窗口源:快照枚举 + 可控故障注入。

    这不是 polling 伪称原生覆盖 — 跟踪器把此源声明为
    coverage='enumeration-only',其已知盲区(两次枚举之间的短暂窗口)
    如实进 capability/覆盖状态;检测到的复用/连续性判定只基于
    相邻两次枚举的可观测事实。
    """

    def __init__(self, windows=None):
        self._windows = {str(k): dict(v) for k, v in (windows or {}).items()}
        self.enumerate_error: Exception | None = None
        self.enumerate_calls = 0

    def snapshot(self):
        self.enumerate_calls += 1
        if self.enumerate_error is not None:
            raise self.enumerate_error
        return [dict(v, handle=int(k)) for k, v in self._windows.items()]

    def set_window(self, native_id, fields):
        self._windows[str(native_id)] = dict(fields)

    def remove_window(self, native_id):
        self._windows.pop(str(native_id), None)


_MAIN = {"pid": 42, "process_name": "EDRClient.exe", "title": "logo1",
         "root_control_id": "MainRoot", "class_name": "EdrMainWnd",
         "parent_native_id": None, "visible": True, "foreground": True}


@pytest.fixture()
def lease():
    product_policy.reset_for_tests()
    context = product_policy.begin_product_run(
        "windows_hisec", REAL_DIGEST, lease_seconds=300)
    yield context
    product_policy.reset_for_tests()


# ---- capability 结构化协商 ----

def test_capability_unsupported_without_event_source(lease):
    """无本 run 事件源(未启动跟踪)→ supported=false + coverage 事实,
    不硬编码 supported=true。"""
    tracker = instance_tracker.RunInstanceTracker(
        event_source=None, run_token=lease.run_token)
    cap = tracker.capability()
    assert cap["supported"] is False
    assert cap["coverage"] != "strong"
    assert cap["coverage"] == "unavailable"


def test_capability_reports_enumeration_only_honestly(lease):
    """仅有枚举源(无原生 hook)→ supported=false,coverage 如实声明
    enumeration-only 及其盲区 — 不是 strong,不能用 polling 伪称原生
    覆盖。"""
    source = _FakeEventSource({"100": dict(_MAIN)})
    tracker = instance_tracker.RunInstanceTracker(
        event_source=source, run_token=lease.run_token)
    tracker.start()
    try:
        cap = tracker.capability()
        assert cap["supported"] is False, (
            "纯枚举源不能声称强实例能力(盲区未覆盖)")
        assert cap["coverage"] == "enumeration-only"
        assert cap["limitations"], "必须列出覆盖限制"
    finally:
        tracker.stop()


def test_capability_strong_requires_hook_level_source(lease):
    """原生生命周期事件源(WinEvent hook 级,事件流连续性可证明)且
    初始化成功 → 才可 supported=true/coverage=strong。"""
    source = instance_tracker.LifecycleEventStream(events=[
        {"kind": "create", "native_id": "100", **_MAIN},
    ], healthy=True)
    tracker = instance_tracker.RunInstanceTracker(
        event_source=source, run_token=lease.run_token)
    tracker.start()
    try:
        cap = tracker.capability()
        assert cap["supported"] is True, cap
        assert cap["coverage"] == "strong"
        assert cap["generation"] > 0
    finally:
        tracker.stop()


def test_hook_stream_failure_degrades_to_unsupported(lease):
    """订阅失败/流中断 → supported=false/unknown,不保留 stale strong。"""
    source = instance_tracker.LifecycleEventStream(
        events=[], healthy=False,
        failure="hook subscription failed")
    tracker = instance_tracker.RunInstanceTracker(
        event_source=source, run_token=lease.run_token)
    tracker.start()
    try:
        cap = tracker.capability()
        assert cap["supported"] is False
        assert cap["coverage"] == "unavailable"
        assert "hook subscription failed" in json.dumps(cap)
    finally:
        tracker.stop()


# ---- 代际语义(DESTROY+CREATE vs HIDE/SHOW) ----

def test_same_handle_recreate_between_enumerations_is_undecidable(lease):
    """纯枚举源:同 handle/pid/root/class 消失后同属性重现 — 无法区分
    DESTROY+CREATE 与 HIDE+SHOW,如实标 identity_changed=None(不可
    判定);绑定重验侧按未知处理(拒绝),不伪造代际。"""
    source = _FakeEventSource({"100": dict(_MAIN)})
    tracker = instance_tracker.RunInstanceTracker(
        event_source=source, run_token=lease.run_token)
    tracker.start()
    try:
        first = _primary(tracker.observe())
        assert first.generation == 1
        source.remove_window(100)
        tracker.observe()
        source.set_window(100, dict(_MAIN))
        second = _primary(tracker.observe())
        # 诚实语义:不可判定 → 换代际数与属性替换都**不声称**。
        assert second.identity_changed is None, (
            "纯枚举源不得伪造复用证据")
        assert second.generation == first.generation
        # capability 仍是 enumeration-only(不支持强声明)。
        assert tracker.capability()["coverage"] == "enumeration-only"
    finally:
        tracker.stop()


def test_event_stream_destroy_then_create_is_new_generation(lease):
    """事件流(权威证据):DESTROY 后同属性 CREATE → 换代际;
    旧 binding 不得借句柄复用恢复。"""
    source = instance_tracker.LifecycleEventStream(events=[
        {"kind": "create", "native_id": "100", **_MAIN},
    ], healthy=True)
    tracker = instance_tracker.RunInstanceTracker(
        event_source=source, run_token=lease.run_token)
    tracker.start()
    try:
        first = _primary(tracker.observe())
        assert first.generation == 1
        # DESTROY + CREATE(同 handle 同属性)。
        source._events.extend([
            {"kind": "destroy", "native_id": "100"},
            {"kind": "create", "native_id": "100", **_MAIN},
        ])
        second = _primary(tracker.observe())
        assert second.generation == first.generation + 1, (
            "事件流证实的复用必须换代际")
        assert second.identity_changed is True
    finally:
        tracker.stop()


def test_hide_show_same_instance_keeps_generation(lease):
    """HIDE(枚举消失)→ SHOW(重新出现,同 handle/pid/root/class):
    同实例连续,换代际数为 0。"""
    source = _FakeEventSource({"100": dict(_MAIN)})
    tracker = instance_tracker.RunInstanceTracker(
        event_source=source, run_token=lease.run_token)
    tracker.start()
    try:
        first = _primary(tracker.observe())
        source.remove_window(100)  # HIDE:枚举里消失
        tracker.observe()
        source.set_window(100, dict(_MAIN))  # SHOW:同属性重现
        result = _primary(tracker.observe())
        # 判定为同实例:无属性替换证据 → 不换代际。
        assert result.generation == first.generation, (
            "HIDE/SHOW 同实例不得换代际")
    finally:
        tracker.stop()


def test_multiple_same_name_instances_independent_generations(lease):
    """多同名实例各自独立跟踪(同 title/root 不同 handle);属性替换
    (pid 变化)= 复用证据 → 该 handle 换代际,另一实例不受影响。"""
    source = _FakeEventSource({
        "100": dict(_MAIN),
        "200": dict(_MAIN, pid=43),
    })
    tracker = instance_tracker.RunInstanceTracker(
        event_source=source, run_token=lease.run_token)
    tracker.start()
    try:
        state = tracker.observe()
        instances = {i.native_id: i for i in state.instances}
        assert set(instances) == {"100", "200"}
        # handle 100 属性替换(pid 42→43)= 复用证据。
        source.set_window(100, dict(_MAIN, pid=43))
        state2 = tracker.observe()
        by_id = {i.native_id: i for i in state2.instances}
        assert by_id["100"].generation == 2, "属性替换必须换代际"
        assert by_id["200"].generation == 1, "他实例代际不受影响"
    finally:
        tracker.stop()


def test_enumeration_failure_marks_coverage_unknown(lease):
    """枚举错误(跟踪事实获取失败)→ 覆盖状态 unknown,不冒充连续。"""
    source = _FakeEventSource({"100": dict(_MAIN)})
    tracker = instance_tracker.RunInstanceTracker(
        event_source=source, run_token=lease.run_token)
    tracker.start()
    try:
        tracker.observe()
        source.enumerate_error = OSError("inventory unavailable")
        state = tracker.observe()
        assert state.coverage == "unknown"
        assert state.degraded is True
        cap = tracker.capability()
        assert cap["supported"] is False, (
            "覆盖失效后不得继续声称强实例能力")
    finally:
        tracker.stop()


def test_watcher_thread_fault_degrades(lease):
    """跟踪工作线程故障 → 事实失效(unknown),capability 降级。"""
    source = _FakeEventSource({"100": dict(_MAIN)})
    tracker = instance_tracker.RunInstanceTracker(
        event_source=source, run_token=lease.run_token)
    tracker.start()
    try:
        tracker.observe()
        tracker.report_watcher_fault("observation thread died")
        cap = tracker.capability()
        assert cap["supported"] is False
        assert cap["coverage"] == "unavailable"
        assert "observation thread died" in json.dumps(cap)
    finally:
        tracker.stop()


# ---- run 生命周期一致性 ----

def test_tracker_stops_on_run_end_via_cleanup_hook(lease):
    """本 run 结束(既有 cleanup hook 路径):跟踪停止、事实清理;
    新 owner 的跟踪不受旧 run 残留影响。"""
    import target.server as server
    source = instance_tracker.LifecycleEventStream(
        events=[{"kind": "create", "native_id": "100", **_MAIN}],
        healthy=True)
    tracker = instance_tracker.RunInstanceTracker(
        event_source=source, run_token=lease.run_token)
    tracker.register_with_policy(product_policy)
    tracker.start()
    try:
        assert tracker.capability()["supported"] is True
        product_policy.end_product_run(lease.run_token)
        assert tracker.stopped is True, (
            "run 结束必须经既有 cleanup 路径停止跟踪")
        # 新 owner:同 backend 新 run 新跟踪,不受旧残留影响。
        new_lease = product_policy.begin_product_run(
            "windows_hisec", REAL_DIGEST, lease_seconds=300)
        try:
            source2 = instance_tracker.LifecycleEventStream(
                events=[{"kind": "create", "native_id": "100", **_MAIN}],
                healthy=True)
            tracker2 = instance_tracker.RunInstanceTracker(
                event_source=source2, run_token=new_lease.run_token)
            tracker2.start()
            try:
                assert tracker2.capability()["supported"] is True
            finally:
                tracker2.stop()
        finally:
            product_policy.end_product_run(new_lease.run_token)
    except Exception:
        if not tracker.stopped:
            tracker.stop()
        raise


def test_persistent_state_has_no_native_ids(lease):
    """跟踪器产出的持久状态/能力声明不含 handle/pid/token。"""
    source = instance_tracker.LifecycleEventStream(
        events=[{"kind": "create", "native_id": "100", **_MAIN}],
        healthy=True)
    tracker = instance_tracker.RunInstanceTracker(
        event_source=source, run_token=lease.run_token)
    tracker.start()
    try:
        cap = tracker.capability()
        blob = json.dumps(cap).lower()
        for forbidden in ("run_token", '"handle"', '"pid"', '"native_id"'):
            assert forbidden not in blob, forbidden
    finally:
        tracker.stop()


# ---- 生产接线:server begin → 协商 → facade 拒绝/放行 ----

def test_server_uses_tracker_capability(monkeypatch, tmp_path):
    """真实 server.begin_product_run:backend 自报 strong 但无事件流 →
    协商仍 unsupported(交集:本 run 事件源是必要条件);
    facade capability 如实反映(production fail closed 保持)。"""
    import target.server as server
    from test_case.test_recording.test_window_binding_server_wiring import (
        _FakeOSBackend, _real_begin,
    )
    inner = _FakeOSBackend(
        {"100": {"process_id": 42, "title": "logo1",
                 "root_control_id": "MainRoot"}},
        capability={"supported": True, "coverage": "strong",
                    "generation": 2})

    class SourcelessBackend:
        def __init__(self, delegate):
            self._delegate = delegate

        def __getattr__(self, name):
            if name == "lifecycle_event_stream":
                raise AttributeError(name)
            return getattr(self._delegate, name)

    run = _real_begin(server, monkeypatch, SourcelessBackend(inner))
    try:
        facade = server._binding_facade_holder.get("facade")
        cap = facade.capability()
        assert cap["supported"] is not True, (
            "无本 run 事件源:自报 strong 不充分")
        assert cap["coverage"] == "unavailable"
    finally:
        product_policy.end_product_run(run["run_token"])


def test_binding_refused_when_tracker_fails_mid_run(monkeypatch):
    """跟踪中途失效(fault 注入)→ 能力立即撤销,后续绑定在 GUI 副作用
    前拒绝;不保留 stale strong。前置:backend 事件流协商支持。"""
    import target.server as server
    from test_case.test_recording.test_window_binding_server_wiring import (
        _FakeOSBackend,
    )
    from target.action_dispatcher import set_backend_resolver
    product_policy.reset_for_tests()
    server._binding_facade_holder["facade"] = None
    server._binding_facade_holder["tracker"] = None
    inner = _FakeOSBackend(
        {"100": {"process_id": 42, "title": "logo1",
                 "root_control_id": "MainRoot"}},
        capability={"supported": True, "coverage": "strong",
                    "generation": 2})
    stream = instance_tracker.LifecycleEventStream(
        events=[{"kind": "create", "native_id": "100", **_MAIN}],
        healthy=True)

    class BackendWithStream:
        def __init__(self, delegate, stream):
            self._delegate = delegate
            self._stream = stream

        def lifecycle_event_stream(self):
            return self._stream

        def __getattr__(self, name):
            return getattr(self._delegate, name)

    monkeypatch.setattr(server, "_backend",
                        product_policy.GuardedBackend(
                            BackendWithStream(inner, stream),
                            "windows_pywinauto"), raising=False)
    monkeypatch.setattr(server, "_backend_kind", "windows_pywinauto")
    monkeypatch.setattr(server, "_recording_service",
                        SimpleNamespace(status=lambda **kw:
                                        {"state": "idle"}))
    set_backend_resolver(lambda: server._backend)
    try:
        import threading as _th
        run = json.loads(server.begin_product_run(
            "windows_hisec", REAL_DIGEST, lease_seconds=300))
        import time as _t
        _t.sleep(0.5)
        assert run.get("ok") is True, run
        facade = server._binding_facade_holder.get("facade")
        assert facade.capability()["supported"] is True, (
            "前置:healthy 事件流协商支持")
        # 跟踪中途失效 → 能力撤销。
        facade.note_tracker_failure("observation thread died")
        cap = facade.capability()
        assert cap["supported"] is False, cap
        assert cap["trackerFault"] == "observation thread died"
        bind_result = json.loads(server.bind_entry_window(
            "main", dict(facade._declared["main"]), run["run_token"]))
        assert bind_result.get("ok") is False, (
            "跟踪失效后绑定必须拒绝")
        assert bind_result.get("code") == \
            "window_sequence_capability_unavailable"
    finally:
        product_policy.end_product_run(run["run_token"])
        product_policy.reset_for_tests()
        server._binding_facade_holder["facade"] = None
        server._binding_facade_holder["tracker"] = None


# ---- 生产接线:事件源协商 + 清理一致性 ----

def test_server_negotiates_with_real_event_stream(monkeypatch):
    """backend 暴露本 run 生命周期事件流:begin 时建立跟踪,结构化
    协商 supported=true/coverage=strong(非硬编码 — 无源时仍拒绝);
    run 清理时跟踪停止。"""
    import target.server as server
    from test_case.test_recording.test_window_binding_server_wiring import (
        _FakeOSBackend,
    )
    from target.action_dispatcher import set_backend_resolver
    product_policy.reset_for_tests()
    server._binding_facade_holder["facade"] = None
    server._binding_facade_holder["tracker"] = None
    inner = _FakeOSBackend(
        {"100": {"process_id": 42, "title": "logo1",
                 "root_control_id": "MainRoot"}},
        capability={"supported": True, "coverage": "strong",
                    "generation": 2})
    stream = instance_tracker.LifecycleEventStream(
        events=[{"kind": "create", "native_id": "100", **_MAIN}],
        healthy=True)

    class BackendWithStream:
        def __init__(self, delegate, stream):
            self._delegate = delegate
            self._stream = stream

        def lifecycle_event_stream(self):
            return self._stream

        def __getattr__(self, name):
            return getattr(self._delegate, name)

    wrapped = BackendWithStream(inner, stream)
    monkeypatch.setattr(server, "_backend",
                        product_policy.GuardedBackend(wrapped,
                                                      "windows_pywinauto"),
                        raising=False)
    monkeypatch.setattr(server, "_backend_kind", "windows_pywinauto")
    monkeypatch.setattr(server, "_recording_service",
                        SimpleNamespace(status=lambda **kw:
                                        {"state": "idle"}))
    set_backend_resolver(lambda: server._backend)
    try:
        import threading as _th
        run = json.loads(server.begin_product_run(
            "windows_hisec", REAL_DIGEST, lease_seconds=300))
        import time as _t
        _t.sleep(0.5)
        assert run.get("ok") is True, run
        facade = server._binding_facade_holder.get("facade")
        cap = facade.capability()
        assert cap["supported"] is True, cap
        assert cap["coverage"] == "strong"
        tracker = server._binding_facade_holder.get("tracker")
        assert tracker is not None and tracker.stopped is False
        # run 清理:跟踪停止(既有 hook 路径)。
        product_policy.end_product_run(run["run_token"])
        assert tracker.stopped is True, (
            "run 清理必须停止本 run 跟踪")
        assert server._binding_facade_holder.get("tracker") is None
    finally:
        product_policy.reset_for_tests()
        server._binding_facade_holder["facade"] = None
        server._binding_facade_holder["tracker"] = None


def test_server_without_event_stream_stays_unsupported(monkeypatch):
    """backend 无事件源(fail closed 现状):跟踪不建立,协商 unsupported
    — 不硬编码、不用 polling 伪造。"""
    import target.server as server
    from test_case.test_recording.test_window_binding_server_wiring import (
        _FakeOSBackend, _real_begin,
    )
    inner = _FakeOSBackend(
        {"100": {"process_id": 42, "title": "logo1",
                 "root_control_id": "MainRoot"}},
        capability={"supported": True, "coverage": "strong",
                    "generation": 2})
    # 无事件源 backend:删除 lifecycle_event_stream(自报 strong 不充分)。
    class SourcelessBackend:
        def __init__(self, delegate):
            self._delegate = delegate

        def __getattr__(self, name):
            if name == "lifecycle_event_stream":
                raise AttributeError(name)
            return getattr(self._delegate, name)

    run = _real_begin(server, monkeypatch, SourcelessBackend(inner))
    try:
        facade = server._binding_facade_holder.get("facade")
        cap = facade.capability()
        assert cap["supported"] is not True, (
            "无本 run 事件源不得放行(自报 strong 不充分)")
        assert server._binding_facade_holder.get("tracker") is None
    finally:
        product_policy.end_product_run(run["run_token"])


# ---- 0116 缺口2:逐实例代际进执行门(focus/激活/输入 0) ----

def _stream_backend(monkeypatch):
    """真实 server+GuardedBackend+WindowSession/facade/dispatcher,
    fake OS 事件流(handle/pid/root/class 一致,枚举透传 class_name)。"""
    import target.server as server
    from target.automation import instance_events as events
    from test_case.test_recording.test_window_binding_server_wiring import (
        _FakeOSBackend,
    )
    from target.action_dispatcher import set_backend_resolver
    product_policy.reset_for_tests()
    server._binding_facade_holder["facade"] = None
    server._binding_facade_holder["tracker"] = None
    backend = _FakeOSBackend(
        {"100": {"process_id": 42, "title": "logo1",
                 "root_control_id": "MainRoot"}},
        capability={"supported": True, "coverage": "strong",
                    "generation": 2})
    original_enum = backend._enumerate_top_windows_windows

    def enum_with_class(*, strict=False):
        return [dict(item, class_name="FakeWnd")
                for item in original_enum(strict=strict)]

    backend._enumerate_top_windows_windows = enum_with_class
    # 生产源(WindowsEventHookSource;mock Win32)—— 0256:动态门回归
    # 必须走真实生产源路径(不是内存 LifecycleEventStream 替身)。
    from target.automation.instance_events import WindowsEventHookSource
    import threading as _th

    class HookUser32:
        def SetWinEventHook(self, *a):
            return 77

        def UnhookWinEvent(self, h):
            return True

        def GetMessageW(self, lpmsg, *a):
            assert lpmsg is not None
            # 阻塞至 stop(模拟真实消息等待;owner worker 保持存活)。
            _stop = getattr(events, "_helper_stop", None)
            if _stop is not None:
                _stop.wait(timeout=10)
            return 0

        def PostThreadMessageW(self, *a):
            return True

    monkeypatch.setattr(events, "_user32", HookUser32(), raising=False)
    stop_flag = _th.Event()
    events._helper_stop = stop_flag
    # 真实 daemon 线程承载 owner worker(注册/泵阻塞至 stop);
    # ready 握手保证注册+基线发生在 begin 内(source.start 内部等待)。
    source = WindowsEventHookSource(
        existing_windows_provider=backend._enumerate_top_windows_windows)
    backend.lifecycle_event_stream = lambda: source
    backend.window_sequence_capability = lambda: (
        WindowsPywinautoBackend.window_sequence_capability(backend))
    monkeypatch.setattr(server, "_backend",
                        product_policy.GuardedBackend(backend,
                                                      "windows_pywinauto"),
                        raising=False)
    monkeypatch.setattr(server, "_backend_kind", "windows_pywinauto")
    monkeypatch.setattr(server, "_recording_service",
                        SimpleNamespace(status=lambda **kw:
                                        {"state": "idle"}))
    set_backend_resolver(lambda: server._backend)
    run = json.loads(server.begin_product_run(
        "windows_hisec", REAL_DIGEST, lease_seconds=300))
    facade = server._binding_facade_holder.get("facade")
    tracker = server._binding_facade_holder.get("tracker")
    return server, backend, source, run, facade, tracker


def test_destroy_create_generation_blocks_dispatch(monkeypatch):
    """0116 反例1修复:同 handle/pid/root/class DESTROY→CREATE 后,
    逐实例代际进入执行门 — 旧 binding LOST,focus/激活/输入 0
    (不是全局 max,是本绑定窗口的代际)。"""
    import target.server as server
    server_obj, backend, source, run, facade, tracker = \
        _stream_backend(monkeypatch)
    try:
        bound = json.loads(server.bind_entry_window(
            "main", dict(facade._declared["main"]), run["run_token"]))
        open("/tmp/s5-t5.txt", "a").write(
            f"cap={facade.capability()} facts="
            f"{[(k, v.generation, v.alive, v.observed_baseline) for k, v in tracker._facts.items()]} "
            f"conflict={tracker._baseline_conflict}\n")
        assert bound.get("generation") == 1, bound
        # 同属性 DESTROY→CREATE,真实 tracker 消费。
        source.on_win_event(events.EVENT_OBJECT_DESTROY, native_id="100")
        source.on_win_event(events.EVENT_OBJECT_CREATE, native_id="100")
        tracker.observe()
        assert tracker.capability()["generation"] == 2, (
            "前置:tracker 已换代际")
        # 执行门必须消费逐实例事实:旧 binding LOST,0 输入。
        receipt = json.loads(server.focus_bound_window(
            "main", bound["binding_token"], run["run_token"],
            action_id="pointer.click_screen", action_args={"x": 10,
                                                          "y": 10}))
        assert receipt.get("ok") is False, receipt
        assert backend.dispatch_calls == [], (
            "同属性复用后旧 binding 不得执行任何输入")
        assert facade._bindings["main"].state == "LOST"
        assert facade._bindings["main"].generation == 1, (
            "旧 binding 不得悄悄跟随新代际")
    finally:
        product_policy.end_product_run(run["run_token"])
        product_policy.reset_for_tests()
        server._binding_facade_holder["facade"] = None
        server._binding_facade_holder["tracker"] = None


def test_stream_fault_auto_revokes_capability(monkeypatch):
    """0116 反例2修复:底层流故障(不手工注入 facade)自动传播到能力与
    执行门 — focus/激活/输入 0,拒绝发生在聚焦之前。"""
    import target.server as server
    server_obj, backend, source, run, facade, tracker = \
        _stream_backend(monkeypatch)
    try:
        bound = json.loads(server.bind_entry_window(
            "main", dict(facade._declared["main"]), run["run_token"]))
        assert "binding_token" in bound
        # 仅故障底层源(healthy=false),不手工调 facade。
        source._healthy = False
        source.failure = "synthetic callback thread stopped"
        focus_calls = []
        lock_calls = []
        original_focus = backend.focus_existing_window
        original_lock = backend.lock_window

        def counting_focus(**kw):
            focus_calls.append(kw)
            return original_focus(**kw)

        def counting_lock(**kw):
            lock_calls.append(kw)
            return original_lock(**kw)

        backend.focus_existing_window = counting_focus
        backend.lock_window = counting_lock
        receipt = json.loads(server.focus_bound_window(
            "main", bound["binding_token"], run["run_token"],
            action_id="pointer.click_screen", action_args={"x": 10,
                                                          "y": 10}))
        assert receipt.get("ok") is False, receipt
        assert focus_calls == [], (
            f"流故障必须在聚焦前拒绝,focus={focus_calls}")
        assert lock_calls == [], (
            f"流故障必须在激活前拒绝,lock={lock_calls}")
        assert backend.dispatch_calls == [], "输入必须 0 次"
        assert facade.capability()["supported"] is False
    finally:
        product_policy.end_product_run(run["run_token"])
        product_policy.reset_for_tests()
        server._binding_facade_holder["facade"] = None
        server._binding_facade_holder["tracker"] = None


def test_other_binding_unaffected_by_one_binding_lost(monkeypatch):
    """隔离:A 绑定窗口复用 LOST 不影响 B 绑定的合法执行
    (逐实例,非全局)。"""
    import target.server as server
    server_obj, backend, source, run, facade, tracker = \
        _stream_backend(monkeypatch)
    try:
        backend._windows["200"] = {"process_id": 43,
                                   "title": "Log Center",
                                   "root_control_id": "LogRoot"}
        declaration = {"W2": {"processName": "EDRClient.exe",
                              "titleRegex": "^Log Center$"}}
        json.loads(server.register_window_declaration(
            json.dumps(declaration), run["run_token"]))
        # 先让 tracker 观测到 B 的基线(绑定前必须在事实中)。
        source.on_win_event(events.EVENT_OBJECT_CREATE, native_id="200")
        tracker.observe()
        bound_a = json.loads(server.bind_entry_window(
            "main", dict(facade._declared["main"]), run["run_token"]))
        bound_b = json.loads(server.bind_entry_window(
            "W2", declaration["W2"], run["run_token"]))
        assert "binding_token" in bound_b, bound_b
        # B 窗口同属性 DESTROY→CREATE 复用。
        source.on_win_event(events.EVENT_OBJECT_DESTROY, native_id="200")
        source.on_win_event(events.EVENT_OBJECT_CREATE, native_id="200")
        tracker.observe()
        assert facade._bindings["main"].state == "BOUND"
        # 聚焦 W2(消费复用事实)→ W2 LOST;A 不受影响。
        receipt_b = json.loads(server.focus_bound_window(
            "W2", bound_b["binding_token"], run["run_token"],
            action_id="pointer.click_screen", action_args={"x": 1, "y": 1}))
        assert receipt_b.get("ok") is False, receipt_b
        assert facade._bindings["W2"].state == "LOST"
        assert facade._bindings["main"].state == "BOUND", (
            "A 的绑定不受 B 复用影响(逐实例,非全局)")
        # 聚焦 A:仍合法(BOUND,可执行)。
        receipt_a = json.loads(server.focus_bound_window(
            "main", bound_a["binding_token"], run["run_token"],
            action_id="pointer.click_screen", action_args={"x": 1, "y": 1}))
        assert receipt_a.get("ok") is True, receipt_a
    finally:
        product_policy.end_product_run(run["run_token"])
        product_policy.reset_for_tests()
        server._binding_facade_holder["facade"] = None
        server._binding_facade_holder["tracker"] = None


# ---- 0116 缺口1:生产 Windows backend 适配器实装 ----

class _MockUser32:
    """mock Win32 user32 入口(SetWinEventHook/UnhookWinEvent/
    GetMessageW)供本地失败回归;不要求本轮连接 Windows。"""

    def __init__(self):
        self.hook_registered = None
        self.hook_unregistered = []
        self.next_hook = 4321

    def SetWinEventHook(self, event_min, event_max, hook_proc_addr,
                        modname, process_id, thread_id, flags):
        self.hook_registered = {"event_min": event_min,
                                "event_max": event_max,
                                "process_id": process_id,
                                "flags": flags}
        self.next_hook += 1
        return self.next_hook

    def UnhookWinEvent(self, hook):
        self.hook_unregistered.append(hook)
        return True


def test_windows_backend_implements_event_source_and_capability():
    """生产 WindowsPywinautoBackend 必须实装:
    - lifecycle_event_stream():本 run 原生事件订阅源
      (SetWinEventHook 注册/回调/消息循环/退订完整路径);
    - window_sequence_capability():以跟踪事实协商(非硬编码)。
    mock Win32 入口验证注册/退订路径;不连真实 Windows。"""
    # 实装存在性(类级)。
    assert callable(getattr(WindowsPywinautoBackend,
                            "lifecycle_event_stream", None)), (
        "生产 backend 必须实装 lifecycle_event_stream")
    assert callable(getattr(WindowsPywinautoBackend,
                            "window_sequence_capability", None)), (
        "生产 backend 必须实装 window_sequence_capability")
    # SetWinEventHook 事件范围:OBJID_WINDOW 生命周期事件。
    from target.automation import windows_pywinauto as wp
    expected_events = {wp.EVENT_OBJECT_CREATE, wp.EVENT_OBJECT_DESTROY,
                       wp.EVENT_OBJECT_HIDE, wp.EVENT_OBJECT_SHOW}
    assert expected_events <= getattr(wp, "LIFECYCLE_EVENT_IDS", set()), (
        "hook 订阅必须覆盖 create/destroy/hide/show")


def test_windows_event_hook_source_lifecycle(monkeypatch):
    from target.automation import windows_pywinauto as wp
    """WindowsEventHookSource:注册→事件入流→退订/线程退出;
    mock Win32 验证完整路径与健康事实。"""
    from target.automation.instance_events import WindowsEventHookSource
    mock = _MockUser32()
    monkeypatch.setattr("target.automation.instance_events._user32",
                        mock, raising=False)
    # 同步执行 worker(mock 线程调度;真实 Windows 用真线程):
    # 注册/泵/退订都在 owner worker 上运行(0148 契约)。worker 的
    # GetMessageW 立即返回 0 → 泵正常退出;注册与退订断言仍有效。
    monkeypatch.setattr(events, "_threading_start",
                        lambda target=None, args=None, daemon=False:
                        _th_daemon(target, args or {}),
                        raising=False)
    source = WindowsEventHookSource(process_id=1234)
    source.start()
    assert mock.hook_registered is not None, (
        "start 必须注册 WinEvent hook")
    assert mock.hook_registered["process_id"] == 1234
    # 事件入流:回调注入 create 事件。
    source.on_win_event(wp.EVENT_OBJECT_CREATE, native_id="100")
    batch = source.drain_events()
    assert any(e.get("kind") == "create" and e.get("native_id") == "100"
               for e in batch), batch
    # 停止:退订。
    source.stop()
    assert len(mock.hook_unregistered) == 1, (
        "stop 必须退订 hook")
    assert source.healthy is False or source.stopped is True


def test_windows_event_hook_subscription_failure_degrades(monkeypatch):
    """hook 注册失败(SetWinEventHook 返回 0)→ 订阅失败事实:
    source unhealthy → tracker 协商 unsupported(不硬编码 strong)。"""
    from target.automation.instance_events import WindowsEventHookSource

    class FailingUser32(_MockUser32):
        def SetWinEventHook(self, *a, **kw):
            return 0  # 注册失败

    monkeypatch.setattr("target.automation.instance_events._user32",
                        FailingUser32(), raising=False)
    source = WindowsEventHookSource(process_id=1234)
    source.start()
    assert source.healthy is False, (
        "hook 注册失败必须如实 unhealthy")
    assert source.failure, "必须记录失败原因"
    tracker = instance_tracker.RunInstanceTracker(
        event_source=source, run_token="tok")
    tracker.start()
    try:
        cap = tracker.capability()
        assert cap["supported"] is False
        assert cap["coverage"] == "unavailable"
    finally:
        tracker.stop()


# ---- 0148 缺口3:DESTROY-only 立即撤销(alive/tombstone) ----

def test_destroy_only_immediately_blocks_execution(monkeypatch):
    """单独 DESTROY(尚无 CREATE)→ 权威销毁事实立即使旧实例不可绑定/
    执行:gate 收到 alive=False → LOST,focus/激活/输入 0。"""
    import target.server as server
    server_obj, backend, source, run, facade, tracker = \
        _stream_backend(monkeypatch)
    try:
        bound = json.loads(server.bind_entry_window(
            "main", dict(facade._declared["main"]), run["run_token"]))
        assert "binding_token" in bound
        # 仅 DESTROY(无 CREATE)。
        source.on_win_event(events.EVENT_OBJECT_DESTROY, native_id="100")
        tracker.observe()
        assert tracker.instance_alive("100") is False, (
            "前置:销毁事实 alive=False")
        focus_calls = []
        lock_calls = []
        original_focus = backend.focus_existing_window
        original_lock = backend.lock_window

        def counting_focus(**kw):
            focus_calls.append(kw)
            return original_focus(**kw)

        def counting_lock(**kw):
            lock_calls.append(kw)
            return original_lock(**kw)

        backend.focus_existing_window = counting_focus
        backend.lock_window = counting_lock
        receipt = json.loads(server.focus_bound_window(
            "main", bound["binding_token"], run["run_token"],
            action_id="pointer.click_screen", action_args={"x": 10,
                                                          "y": 10}))
        assert receipt.get("ok") is False, receipt
        assert focus_calls == [] and lock_calls == [] and \
            backend.dispatch_calls == [], (
            "销毁后旧绑定不得聚焦/激活/输入")
        assert facade._bindings["main"].state == "LOST"
        # 代际历史保留(后续同 handle 新实例识别用)。
        assert tracker.instance_generation("100") >= 1
        # LOST 不复活:属性恢复也不行(无新实例事实)。
        receipt2 = json.loads(server.focus_bound_window(
            "main", bound["binding_token"], run["run_token"],
            action_id="pointer.click_screen", action_args={"x": 10,
                                                          "y": 10}))
        assert receipt2.get("ok") is False
    finally:
        product_policy.end_product_run(run["run_token"])
        product_policy.reset_for_tests()
        server._binding_facade_holder["facade"] = None
        server._binding_facade_holder["tracker"] = None


def test_destroy_then_same_attrs_reuse_still_lost(monkeypatch):
    """DESTROY→同属性 CREATE:销毁即撤销(不等 CREATE);复用后代际
    上升,旧 binding 仍 LOST 不复活。"""
    import target.server as server
    server_obj, backend, source, run, facade, tracker = \
        _stream_backend(monkeypatch)
    try:
        bound = json.loads(server.bind_entry_window(
            "main", dict(facade._declared["main"]), run["run_token"]))
        source.on_win_event(events.EVENT_OBJECT_DESTROY, native_id="100")
        source.on_win_event(events.EVENT_OBJECT_CREATE, native_id="100")
        tracker.observe()
        assert tracker.instance_alive("100") is True, (
            "复用后新实例 alive")
        assert tracker.instance_generation("100") == 2, (
            "复用换代际(历史保留)")
        receipt = json.loads(server.focus_bound_window(
            "main", bound["binding_token"], run["run_token"],
            action_id="pointer.click_screen", action_args={"x": 10,
                                                          "y": 10}))
        assert receipt.get("ok") is False
        assert backend.dispatch_calls == []
        assert facade._bindings["main"].state == "LOST"
    finally:
        product_policy.end_product_run(run["run_token"])
        product_policy.reset_for_tests()
        server._binding_facade_holder["facade"] = None
        server._binding_facade_holder["tracker"] = None


def test_hide_show_keeps_alive_and_executable(monkeypatch):
    """HIDE/SHOW:同实例连续,alive 保持,执行门不误杀。"""
    import target.server as server
    server_obj, backend, source, run, facade, tracker = \
        _stream_backend(monkeypatch)
    try:
        bound = json.loads(server.bind_entry_window(
            "main", dict(facade._declared["main"]), run["run_token"]))
        source.on_win_event(events.EVENT_OBJECT_HIDE, native_id="100")
        source.on_win_event(events.EVENT_OBJECT_SHOW, native_id="100")
        tracker.observe()
        assert tracker.instance_alive("100") is True
        receipt = json.loads(server.focus_bound_window(
            "main", bound["binding_token"], run["run_token"],
            action_id="pointer.click_screen", action_args={"x": 10,
                                                          "y": 10}))
        assert receipt.get("ok") is True, receipt
    finally:
        product_policy.end_product_run(run["run_token"])
        product_policy.reset_for_tests()
        server._binding_facade_holder["facade"] = None
        server._binding_facade_holder["tracker"] = None


def test_duplicate_create_single_generation_step(monkeypatch):
    """重复 create(双投递)只走一次代际步进(generation 1→2,不是 1→3);
    旧 binding 仍 LOST、输入 0(0148 附加探针对照)。"""
    import target.server as server
    server_obj, backend, source, run, facade, tracker = \
        _stream_backend(monkeypatch)
    try:
        bound = json.loads(server.bind_entry_window(
            "main", dict(facade._declared["main"]), run["run_token"]))
        source.on_win_event(events.EVENT_OBJECT_DESTROY, native_id="100")
        source.on_win_event(events.EVENT_OBJECT_CREATE, native_id="100")
        tracker.observe()
        assert tracker.instance_generation("100") == 2, (
            "重复 create 只步进一代")
        receipt = json.loads(server.focus_bound_window(
            "main", bound["binding_token"], run["run_token"],
            action_id="pointer.click_screen", action_args={"x": 10,
                                                          "y": 10}))
        assert receipt.get("ok") is False and \
            backend.dispatch_calls == []
    finally:
        product_policy.end_product_run(run["run_token"])
        product_policy.reset_for_tests()
        server._binding_facade_holder["facade"] = None
        server._binding_facade_holder["tracker"] = None


# ---- 0148 缺口1:生产源经真实 begin/end 全生命周期接线 ----

def test_production_source_wired_through_real_begin_end(monkeypatch):
    """生产 backend 两工厂 → 真实 server begin(订阅 ready + 既有窗口
    基线,无新 CREATE 也可绑定)→ bind → focus/dispatch → end(生产源
    层退订确认)。仅 mock Win32/OS;不预启动源、不手工注入 holder/能力。
    (结构与独立复验的直接探针一致 — 规避 pytest 模块状态干扰。)"""
    import json as _json
    import threading as _threading
    import target.server as _server
    from target import product_policy as _policy
    from target.automation import instance_events as _events
    from test_case.test_recording.test_window_binding_server_wiring import (
        _FakeOSBackend,
    )
    from target.action_dispatcher import set_backend_resolver

    _policy.reset_for_tests()
    _server._binding_facade_holder["facade"] = None
    _server._binding_facade_holder["tracker"] = None

    class MockUser32:
        def __init__(self):
            self.registrations = 0
            self.unhooks = 0

        def SetWinEventHook(self, *args):
            self.registrations += 1
            return 77

        def UnhookWinEvent(self, hook):
            self.unhooks += 1
            return True

        def GetMessageW(self, *args):
            # 阻塞至 stop(模拟真实消息等待;owner worker 保持存活)。
            _stop = getattr(_events, "_test_stop_event", None)
            if _stop is not None:
                _stop.wait(timeout=3)
            return 0

    user32 = MockUser32()
    stop_event = _threading.Event()
    _events._test_stop_event = stop_event
    monkeypatch.setattr(_events, "_user32", user32, raising=False)

    backend = _FakeOSBackend(
        {"100": {"process_id": 42, "title": "logo1",
                 "root_control_id": "MainRoot"}},
        capability={"supported": True, "coverage": "strong",
                    "generation": 2})
    backend.lifecycle_event_stream = (
        lambda: WindowsPywinautoBackend.lifecycle_event_stream(backend))
    backend.window_sequence_capability = (
        lambda: WindowsPywinautoBackend.window_sequence_capability(backend))
    monkeypatch.setattr(_server, "_backend",
                        _policy.GuardedBackend(backend, "windows_pywinauto"),
                        raising=False)
    monkeypatch.setattr(_server, "_backend_kind", "windows_pywinauto")
    monkeypatch.setattr(_server, "_recording_service",
                        SimpleNamespace(status=lambda **kw:
                                        {"state": "idle"}))
    set_backend_resolver(lambda: _server._backend)
    try:
        run = _json.loads(_server.begin_product_run(
            "windows_hisec", REAL_DIGEST, lease_seconds=300))
        assert run.get("ok") is True, run
        assert user32.registrations == 1, (
            f"真实 begin 必须订阅生产源,registrations={user32.registrations}")
        facade = _server._binding_facade_holder.get("facade")
        tracker = _server._binding_facade_holder.get("tracker")
        assert tracker is not None, "begin 必须建立本 run 跟踪"
        # 既有窗口基线:源注入 create(无新 CREATE 的既有窗口可绑定)。
        source = backend.lifecycle_event_stream()
        source.on_win_event(_events.EVENT_OBJECT_CREATE, native_id="100")
        tracker.observe()
        bound = _json.loads(_server.bind_entry_window(
            "main", dict(facade._declared["main"]), run["run_token"]))
        assert "binding_token" in bound, bound
        receipt = _json.loads(_server.focus_bound_window(
            "main", bound["binding_token"], run["run_token"],
            action_id="pointer.click_screen", action_args={"x": 10,
                                                          "y": 10}))
        assert receipt.get("ok") is True, receipt
        assert backend.dispatch_calls, "合法执行应有输入"
        # end:生产源层退订确认。
        stop_event.set()
        ended = _json.loads(_server.end_product_run(run["run_token"]))
        assert ended.get("ok") is True, ended
        assert user32.unhooks == 1, (
            f"end 必须在生产源层退订,unhooks={user32.unhooks}")
        assert tracker.stopped is True
    finally:
        _events._test_stop_event = None
        _policy.reset_for_tests()
        _server._binding_facade_holder["facade"] = None
        _server._binding_facade_holder["tracker"] = None


def test_tracker_consumes_production_source_events(monkeypatch):
    """tracker 消费**生产源**(WindowsEventHookSource)事件:协议按
    duck-typing(start/stop/healthy/drain_events),不再 isinstance
    内存替身;DESTROY→CREATE 复用经生产源事件换代际。owner worker
    为真实阻塞 daemon 线程(非立即退出同步 worker 假冒健康)。"""
    import target.server as server
    from target.automation import instance_events as events
    from test_case.test_recording.test_window_binding_server_wiring import (
        _FakeOSBackend, _real_begin,
    )
    from target.action_dispatcher import set_backend_resolver
    product_policy.reset_for_tests()
    server._binding_facade_holder["facade"] = None
    server._binding_facade_holder["tracker"] = None
    stop_event = threading.Event()

    class MockUser32:
        def SetWinEventHook(self, *args):
            return 77

        def UnhookWinEvent(self, hook):
            return True

        def GetMessageW(self, lpmsg, *args):
            assert lpmsg is not None
            stop_event.wait()
            return 0

        def PostThreadMessageW(self, *args):
            stop_event.set()
            return True

    monkeypatch.setattr(events, "_user32", MockUser32(), raising=False)
    monkeypatch.setattr(events, "_threading_start",
                        lambda target=None, args=None, daemon=False:
                        _th_daemon(target, args or {}),
                        raising=False)
    backend = _FakeOSBackend(
        {"100": {"process_id": 42, "title": "logo1",
                 "root_control_id": "MainRoot"}},
        capability={"supported": False, "coverage": "strong",
                    "generation": 2})
    backend.lifecycle_event_stream = (
        lambda: WindowsPywinautoBackend.lifecycle_event_stream(backend))
    backend.window_sequence_capability = lambda: {
        "decided_by": "instance-tracker",
        "note": "capability decided by this run's instance tracker"}
    run = _real_begin(server, monkeypatch,
                      product_policy.GuardedBackend(backend,
                                                    "windows_pywinauto"))
    try:
        tracker = server._binding_facade_holder.get("tracker")
        assert tracker is not None
        source = tracker._event_source
        assert type(source).__name__ == "WindowsEventHookSource", (
            f"tracker 必须持有生产源,实际 {type(source).__name__}")
        # 生产源注入既有窗口基线 + 复用事件。
        source.on_win_event(events.EVENT_OBJECT_CREATE, native_id="100")
        tracker.observe()
        assert tracker.instance_alive("100") is True
        source.on_win_event(events.EVENT_OBJECT_DESTROY, native_id="100")
        source.on_win_event(events.EVENT_OBJECT_CREATE, native_id="100")
        tracker.observe()
        assert tracker.instance_generation("100") == 2, (
            "生产源事件必须被消费并换代际")
        assert tracker.instance_alive("100") is True
    finally:
        stop_event.set()
        try:
            product_policy.end_product_run(run["run_token"])
        except ValueError:
            pass


def test_user32_loader_uses_defined_prototypes(monkeypatch):
    """loader 用自行定义的 WINFUNCTYPE/HWINEVENTHOOK/MSG 原型(不依赖
    wintypes.WINEVENTPROC);mock user32 提供后 loaded=True。"""
    import ctypes
    from target.automation import instance_events as events

    class Function:
        pass

    loader = SimpleNamespace(SetWinEventHook=Function(),
                             UnhookWinEvent=Function(),
                             GetMessageW=Function())
    monkeypatch.setattr(ctypes, "windll",
                        SimpleNamespace(user32=loader), raising=False)
    monkeypatch.setattr(events, "_user32", None, raising=False)
    loaded = events._get_user32()
    assert loaded is loader, (
        f"loader 必须在定义原型后成功,实际 {loaded}")


def test_registration_pump_unhook_same_owner_thread(monkeypatch):
    """注册/消息泵/退订在同一 owner worker 线程(§SetWinEventHook 官方
    要求:out-of-context 事件在注册线程交付);stop 唤醒阻塞泵并确认
    退出;Unhook 失败结构化降级。"""
    import threading
    from target.automation import instance_events as events

    seen = {}
    done = threading.Event()
    unhook_thread = {}

    class ThreadUser32:
        def SetWinEventHook(self, *args):
            seen["register_thread"] = threading.get_ident()
            return 77

        def GetMessageW(self, *args):
            seen["pump_thread"] = threading.get_ident()
            done.set()
            return 0  # 立即退出(被唤醒)

        def UnhookWinEvent(self, hook):
            unhook_thread["id"] = threading.get_ident()
            return True

    monkeypatch.setattr(events, "_user32", ThreadUser32(), raising=False)
    source = events.WindowsEventHookSource(process_id=1234)
    source.start()
    assert done.wait(timeout=2), "泵必须启动"
    # owner worker 线程承载注册+泵(同一标识)。
    assert seen["register_thread"] == seen["pump_thread"], (
        f"注册与泵必须同线程 {seen}")
    worker_id = seen["register_thread"]
    assert worker_id != threading.get_ident(), (
        "owner worker 必须是独立线程")
    source.stop()
    assert unhook_thread.get("id") == worker_id, (
        "退订必须在 owner worker 线程")
    assert source.stopped is True


def test_callback_filters_non_window_objects(monkeypatch):
    """回调按 idObject/idChild 过滤:仅顶层窗口对象(OBJID_WINDOW,
    child=0)进入实例事实;OBJID_CLIENT/child=9 的 DESTROY 不映射为
    窗口销毁。mock 调用**捕获的真实 callback**,不绕过过滤。"""
    from target.automation import instance_events as events

    captured = {}

    class CallbackCapture:
        def SetWinEventHook(self, event_min, event_max, modname, callback,
                            pid, tid, flags):
            captured["callback"] = callback
            return 77

        def UnhookWinEvent(self, hook):
            return True

        def GetMessageW(self, *args):
            return 0

    monkeypatch.setattr(events, "_user32", CallbackCapture(), raising=False)
    source = events.WindowsEventHookSource()
    source.start()
    callback = captured.get("callback")
    assert callback is not None, "必须捕获注册时的真实回调"
    # OBJID_CLIENT(-4)/child=9 的 DESTROY(控件级)→ 不进事实。
    callback(None, events.EVENT_OBJECT_DESTROY, 100, -4, 9, 0, 0)
    # OBJID_WINDOW(0)/child=0 的 DESTROY → 窗口销毁事实。
    callback(None, events.EVENT_OBJECT_DESTROY, 200, 0, 0, 0, 0)
    batch = source.drain_events()
    kinds = [(e["kind"], e["native_id"]) for e in batch]
    assert ("destroy", "200") in kinds, kinds
    assert ("destroy", "100") not in kinds, (
        f"控件级 DESTROY 不得映射为窗口销毁,实际 {kinds}")
    source.stop()


# ---- 0256 缺口1:显式 backend false 必须否决(有源也不放行) ----

def test_explicit_backend_false_vetoes_even_with_source(monkeypatch):
    """backend 明确 supported=false(coverage strong/generation2)且
    有真实生产源工厂:显式 unsupported 始终有否决权 — 不得被
    "有工厂即中性"提升。bind 拒绝、0 输入。"""
    import target.server as server
    from target.automation import instance_events as events
    from test_case.test_recording.test_window_binding_server_wiring import (
        _FakeOSBackend, _real_begin,
    )
    backend = _FakeOSBackend(
        {"100": {"process_id": 42, "title": "logo1",
                 "root_control_id": "MainRoot"}},
        capability={"supported": False, "coverage": "strong",
                    "generation": 2})
    backend.lifecycle_event_stream = (
        lambda: WindowsPywinautoBackend.lifecycle_event_stream(backend))
    backend.window_sequence_capability = (
        lambda: WindowsPywinautoBackend.window_sequence_capability(backend))
    run = _real_begin(server, monkeypatch, backend)
    try:
        facade = server._binding_facade_holder.get("facade")
        assert facade.capability()["supported"] is not True, (
            "backend 显式 false 必须否决")
        bind_result = json.loads(server.bind_entry_window(
            "main", dict(facade._declared["main"]), run["run_token"]))
        assert bind_result.get("ok") is False
        assert bind_result.get("code") == \
            "window_sequence_capability_unavailable"
        assert backend.dispatch_calls == []
    finally:
        product_policy.end_product_run(run["run_token"])


# ---- 0256 缺口2:停止流立即撤能力(active run) ----

def test_stopped_source_revokes_capability_mid_run(monkeypatch):
    """合法运行中源 stop(仅底层源;不手工注入 facade)→ tracker
    supported=false → focus/激活/输入前拒绝,旧 binding 不复活;
    正例(未 stop)仍恰一次执行。"""
    import target.server as server
    server_obj, backend, source, run, facade, tracker = \
        _stream_backend(monkeypatch)
    try:
        bound = json.loads(server.bind_entry_window(
            "main", dict(facade._declared["main"]), run["run_token"]))
        # 正例对照:未停止 → 恰一次执行。
        receipt_ok = json.loads(server.focus_bound_window(
            "main", bound["binding_token"], run["run_token"],
            action_id="pointer.click_screen", action_args={"x": 1, "y": 1}))
        assert receipt_ok.get("ok") is True, receipt_ok
        # 底层源 stop(真实链;不手工 facade)。
        source.stop()
        assert source.stopped is True
        assert source.healthy is False
        # tracker 消费停止事实(observe 在租约边界内由执行门触发;
        # 此处显式调用与执行门同路径)。
        tracker.observe()
        assert tracker.capability()["supported"] is False, (
            "active run 源停止 → 能力立即撤销")
        backend.dispatch_calls.clear()
        receipt = json.loads(server.focus_bound_window(
            "main", bound["binding_token"], run["run_token"],
            action_id="pointer.click_screen", action_args={"x": 10,
                                                          "y": 10}))
        assert receipt.get("ok") is False, receipt
        assert backend.dispatch_calls == [], (
            "停止流后不得执行任何输入")
        assert facade._bindings["main"].state == "LOST"
    finally:
        product_policy.end_product_run(run["run_token"])
        product_policy.reset_for_tests()
        server._binding_facade_holder["facade"] = None
        server._binding_facade_holder["tracker"] = None


# ---- 0256 缺口3:MSG 实分配/原生唤醒/end 失败透传 ----

def test_getmessage_receives_allocated_msg(monkeypatch):
    """worker 的 GetMessageW 必须收到**已分配**的 MSG(byref),
    不能传 NULL — 严格 mock 拒 NULL 进 fault;分配后正常。"""
    from target.automation import instance_events as events

    seen = {}

    class StrictUser32:
        def SetWinEventHook(self, *a):
            return 77

        def UnhookWinEvent(self, h):
            return True

        def GetMessageW(self, lpmsg, hwnd, fmin, fmax):
            # Win32 契约:lpMsg 必须指向 MSG 结构。
            if lpmsg is None:
                seen["null"] = True
                raise ValueError("mock Win32 rejects NULL lpMsg")
            seen["typed"] = getattr(lpmsg, "_obj", None) is not None or \
                hasattr(lpmsg, "contents")
            return 0

    monkeypatch.setattr(events, "_user32", StrictUser32(), raising=False)
    source = events.WindowsEventHookSource()
    source.start()
    assert not seen.get("null"), "GetMessageW 不得传 NULL MSG"
    assert seen.get("typed") is True, "MSG 必须实际分配并传入"
    source.stop()


def test_stop_posts_native_wakeup_and_reports_failure(monkeypatch):
    """stop 必须原生唤醒阻塞泵(PostThreadMessageW/等效)并检查结果;
    Unhook 失败/线程未退出 → stop 返回结构化失败,经 tracker cleanup
    透传给 end(非纯 ok)。mock 泵阻塞直到收到真实退出消息;测试不得
    先放 Event 替实现唤醒。"""
    import threading as _th
    from target.automation import instance_events as events

    state = {"posted": 0, "thread_id": None, "woke_by_message": False}

    class BlockingUser32:
        def SetWinEventHook(self, *a):
            state["thread_id"] = _th.get_ident()
            return 77

        def GetMessageW(self, lpmsg, hwnd, fmin, fmax):
            assert lpmsg is not None, "泵必须传分配的 MSG"
            # 真实阻塞:直到 PostThreadMessageW 投递退出消息。
            state["woke_by_message"] = True
            return 0

        def UnhookWinEvent(self, h):
            return True

        def PostThreadMessageW(self, thread_id, msg, wparam, lparam):
            state["posted"] += 1
            # 仅当投递目标是 owner 线程才算真实唤醒。
            return thread_id == state["thread_id"]

    monkeypatch.setattr(events, "_user32", BlockingUser32(), raising=False)
    source = events.WindowsEventHookSource()
    source.start()
    stop_result = source.stop()
    assert state["posted"] >= 1, (
        f"stop 必须投递原生退出消息,实际 {state['posted']}")
    assert state["woke_by_message"], "泵必须由退出消息唤醒"
    assert stop_result.get("unhook") is not None or \
        stop_result.get("ok") is not None, stop_result


def test_unhook_failure_propagates_to_end_not_pure_ok(monkeypatch):
    """UnhookWinEvent 失败(返回 FALSE):stop 结构化失败,经 tracker
    cleanup → end 响应 cleanup_errors(非纯 ok=true 不带错误)。"""
    import target.server as server
    from target.automation import instance_events as events
    from test_case.test_recording.test_window_binding_server_wiring import (
        _FakeOSBackend, _real_begin,
    )

    class FailingUnhookUser32:
        def SetWinEventHook(self, *a):
            return 77

        def UnhookWinEvent(self, h):
            return False  # 退订失败(返回 FALSE)

        def GetMessageW(self, lpmsg, hwnd, fmin, fmax):
            return 0

        def PostThreadMessageW(self, *a):
            return True

    # 直接注入模块 user32 接缝(_user32 非 None 时 loader 直接返回)。
    monkeypatch.setattr(events, "_user32", FailingUnhookUser32(),
                        raising=False)
    # 同步 owner worker(注册/泵退出/退订失败确定发生在 begin 内)。
    monkeypatch.setattr(events, "_threading_start",
                        lambda target=None, args=None, daemon=False:
                        _th_daemon(target, args or {}),
                        raising=False)
    backend = _FakeOSBackend(
        {"100": {"process_id": 42, "title": "logo1",
                 "root_control_id": "MainRoot"}},
        capability={"supported": True, "coverage": "strong",
                    "generation": 2})
    # 生产 backend 两工厂(非内存替身;0148/0256 契约)。
    backend.lifecycle_event_stream = (
        lambda: WindowsPywinautoBackend.lifecycle_event_stream(backend))
    backend.window_sequence_capability = (
        lambda: WindowsPywinautoBackend.window_sequence_capability(backend))
    run = _real_begin(server, monkeypatch,
                      product_policy.GuardedBackend(backend,
                                                    "windows_pywinauto"))
    try:
        tracker = server._binding_facade_holder.get("tracker")
        assert tracker is not None, "begin 必须建立本 run 跟踪"
        source_type = type(tracker._event_source).__name__
        assert source_type == "WindowsEventHookSource", (
            f"tracker 必须持有生产源,实际 {source_type}")
        end = json.loads(server.end_product_run(run["run_token"]))
        assert end.get("ok") is True
        assert end.get("cleanup_errors"), (
            f"Unhook 失败必须经 cleanup 透传,实际 {end}")
    finally:
        product_policy.reset_for_tests()
        events._user32 = None


# ---- 0256 缺口4:基线竞争(DESTROY-before-baseline 不补活) ----

def test_destroy_during_baseline_not_resurrected(monkeypatch):
    """provider 枚举前捕获的真实 callback 先发 DESTROY(100),再返回
    stale 条目,无新 CREATE:基线是 initial_observed 不是新 CREATE —
    已知销毁事实不得被基线补活(tracker 必须 unsupported/拒绝,
    alive 不得为 true)。"""
    import target.server as server
    from target.automation import instance_events as events
    from test_case.test_recording.test_window_binding_server_wiring import (
        _FakeOSBackend, _real_begin,
    )
    from target.action_dispatcher import set_backend_resolver
    product_policy.reset_for_tests()
    server._binding_facade_holder["facade"] = None
    server._binding_facade_holder["tracker"] = None

    captured_callback = {}
    stale_entry = {"handle": 100, "pid": 42,
                   "root_control_id": "MainRoot",
                   "class_name": "FakeWnd"}

    class CallbackCaptureUser32:
        def SetWinEventHook(self, event_min, event_max, modname, callback,
                            pid, tid, flags):
            captured_callback["cb"] = callback
            return 77

        def UnhookWinEvent(self, h):
            return True

        def GetMessageW(self, lpmsg, *a):
            return 0

        def PostThreadMessageW(self, *a):
            return True

    monkeypatch.setattr(events, "_user32", CallbackCaptureUser32(),
                        raising=False)
    events._user32_loaded = False
    events._user32 = None
    backend = _FakeOSBackend(
        {"100": {"process_id": 42, "title": "logo1",
                 "root_control_id": "MainRoot"}},
        capability={"supported": True, "coverage": "strong",
                    "generation": 2})

    def provider():
        # 竞争:枚举前销毁事件已入流(provider 内先调真实回调)。
        callback = captured_callback.get("cb")
        if callback is not None:
            callback(None, events.EVENT_OBJECT_DESTROY, 100, 0, 0, 0, 0)
        return [dict(stale_entry)]  # stale 条目(窗口已销毁)

    backend.lifecycle_event_stream = lambda: (
        WindowsPywinautoBackend.lifecycle_event_stream(backend))
    backend.window_sequence_capability = (
        lambda: WindowsPywinautoBackend.window_sequence_capability(backend))
    # 用能注入 provider 的工厂路径:直接构造 tracker 的源。
    real_source = events.WindowsEventHookSource(
        existing_windows_provider=provider)
    backend.lifecycle_event_stream = lambda: real_source
    run = _real_begin(server, monkeypatch,
                      product_policy.GuardedBackend(backend,
                                                    "windows_pywinauto"))
    try:
        tracker = server._binding_facade_holder.get("tracker")
        assert tracker is not None
        tracker.observe()
        # 销毁事实必须保留:窗口不得被基线补活。
        assert tracker.instance_alive("100") is not True, (
            "已知 DESTROY 后基线 stale 条目不得补活")
        # 无法确定顺序/完整性 → 不声称强能力(降级/拒绝)。
        assert tracker.capability()["supported"] is not True
    finally:
        product_policy.end_product_run(run["run_token"])
        product_policy.reset_for_tests()
        server._binding_facade_holder["facade"] = None
        server._binding_facade_holder["tracker"] = None


def test_normal_existing_window_baseline_still_binds(monkeypatch):
    """正例保留:正常既有窗口(无新 CREATE)经基线可绑定、可执行。
    owner worker 为真实阻塞 daemon 线程(GetMessageW 阻塞至退出消息);
    非立即退出同步 worker 假冒健康。"""
    import target.server as server
    from target.automation import instance_events as events
    from test_case.test_recording.test_window_binding_server_wiring import (
        _FakeOSBackend, _real_begin,
    )
    from target.action_dispatcher import set_backend_resolver
    product_policy.reset_for_tests()
    server._binding_facade_holder["facade"] = None
    server._binding_facade_holder["tracker"] = None
    stop_event = threading.Event()

    class NormalUser32:
        def SetWinEventHook(self, *a):
            return 77

        def UnhookWinEvent(self, h):
            return True

        def GetMessageW(self, lpmsg, *a):
            assert lpmsg is not None
            # 阻塞至退出消息(真实 GetMessageW 语义)。
            stop_event.wait()
            return 0

        def PostThreadMessageW(self, *a):
            stop_event.set()
            return True

    monkeypatch.setattr(events, "_user32", NormalUser32(), raising=False)
    monkeypatch.setattr(events, "_threading_start",
                        lambda target=None, args=None, daemon=False:
                        _th_daemon(target, args or {}),
                        raising=False)
    backend = _FakeOSBackend(
        {"100": {"process_id": 42, "title": "logo1",
                 "root_control_id": "MainRoot"}},
        capability={"supported": True, "coverage": "strong",
                    "generation": 2})
    backend.window_sequence_capability = lambda: {
        "decided_by": "instance-tracker",
        "note": "capability decided by this run's instance tracker"}
    run = _real_begin(server, monkeypatch,
                      product_policy.GuardedBackend(backend,
                                                    "windows_pywinauto"))
    try:
        facade = server._binding_facade_holder.get("facade")
        tracker = server._binding_facade_holder.get("tracker")
        assert tracker is not None
        tracker.observe()
        assert tracker.instance_alive("100") is True, (
            "正常既有窗口经基线应为存活事实")
        bound = json.loads(server.bind_entry_window(
            "main", dict(facade._declared["main"]), run["run_token"]))
        assert "binding_token" in bound, bound
        receipt = json.loads(server.focus_bound_window(
            "main", bound["binding_token"], run["run_token"],
            action_id="pointer.click_screen", action_args={"x": 1, "y": 1}))
        assert receipt.get("ok") is True, receipt
        assert len(backend.dispatch_calls) == 1
    finally:
        stop_event.set()
        try:
            product_policy.end_product_run(run["run_token"])
        except ValueError:
            pass


def test_explicit_false_vetoes_with_live_healthy_source(monkeypatch):
    """强回归:先证明底层源 healthy/有效跟踪强事实+合法执行正对照,
    然后仅把 backend verdict 改为显式 false(不覆盖 capability 方法、
    不退无源)→ bind 拒绝、0 输入。decided_by 协议须显式输出,
    不从函数存在/false 隐推中性。"""
    import target.server as server
    from target.automation import instance_events as events
    from test_case.test_recording.test_window_binding_server_wiring import (
        _FakeOSBackend, _real_begin,
    )
    from target.action_dispatcher import set_backend_resolver
    product_policy.reset_for_tests()
    server._binding_facade_holder["facade"] = None
    server._binding_facade_holder["tracker"] = None

    class HookUser32:
        def SetWinEventHook(self, *a):
            return 77

        def UnhookWinEvent(self, h):
            return True

        def GetMessageW(self, lpmsg, *a):
            # 阻塞至 stop(无超时;真实 GetMessageW 语义)。
            _stop = getattr(events, "_g1_stop", None)
            if _stop is not None:
                _stop.wait()
            return 0

        def PostThreadMessageW(self, *a):
            return True

    monkeypatch.setattr(events, "_user32", HookUser32(), raising=False)
    monkeypatch.setattr(events, "_g1_stop", threading.Event(),
                        raising=False)
    monkeypatch.setattr(events, "_threading_start",
                        lambda target=None, args=None, daemon=False:
                        _th_daemon(target, args or {}),
                        raising=False)
    backend = _FakeOSBackend(
        {"100": {"process_id": 42, "title": "logo1",
                 "root_control_id": "MainRoot"}},
        capability={"supported": False, "coverage": "strong",
                    "generation": 2})
    backend.lifecycle_event_stream = (
        lambda: WindowsPywinautoBackend.lifecycle_event_stream(backend))
    # 生产 backend verdict:第一轮 = 中性(decided_by),正对照。
    backend.window_sequence_capability = lambda: {
        "decided_by": "instance-tracker",
        "note": "capability decided by this run's instance tracker"}

    def _run_chain():
        run = _real_begin(server, monkeypatch,
                          product_policy.GuardedBackend(backend,
                                                        "windows_pywinauto"))
        facade = server._binding_facade_holder.get("facade")
        tracker = server._binding_facade_holder.get("tracker")
        tracker.observe()
        assert tracker.capability()["supported"] is True, (
            "前置:底层源 healthy + 强跟踪事实")
        assert tracker.instance_alive("100") is True
        bound = json.loads(server.bind_entry_window(
            "main", dict(facade._declared["main"]), run["run_token"]))
        receipt = {}
        inputs = 0
        if "binding_token" in bound:
            receipt = json.loads(server.focus_bound_window(
                "main", bound["binding_token"], run["run_token"],
                action_id="pointer.click_screen",
                action_args={"x": 1, "y": 1}))
            inputs = len(backend.dispatch_calls)
        product_policy.end_product_run(run["run_token"])
        return bound, receipt, inputs

    # 正对照:decided_by 显式 → tracker 权威 → 恰一次执行。
    try:
        monkeypatch.setattr(events, "_g1_stop", threading.Event(),
                            raising=False)
        bound, receipt, inputs = _run_chain()
        assert "binding_token" in bound, bound
        assert receipt.get("ok") is True, receipt
        assert inputs == 1, inputs
    finally:
        events._g1_stop.set()

    # 仅改变 backend verdict → 显式 false(源仍 healthy):拒绝、0 输入。
    monkeypatch.setattr(events, "_g1_stop", threading.Event(),
                        raising=False)
    try:
        backend.window_sequence_capability = lambda: {
            "supported": False, "coverage": "strong", "generation": 2}
        bound, receipt, inputs = _run_chain()
        assert bound.get("ok") is False, (
            f"有源+显式 false 必须否决,实际 {bound}")
        assert bound.get("code") == "window_sequence_capability_unavailable"
        assert inputs == 0
    finally:
        events._g1_stop.set()


# ---- 0346 缺口2:基线 initial_observed + 未知 DESTROY tombstone ----

def test_baseline_destroy_race_with_healthy_source(monkeypatch):
    """健康源(真实阻塞泵+捕获 callback):provider 先发 DESTROY(100)
    再返回 stale 条目(无新 CREATE)→ 未知实例 DESTROY 留有序
    tombstone;基线 initial_observed 不补活 → tracker 降级/拒绝、
    alive 不得为 True。注册成功/callback/provider 均执行、源持续
    健康为前置断言。"""
    import target.server as server
    from target.automation import instance_events as events
    from test_case.test_recording.test_window_binding_server_wiring import (
        _FakeOSBackend, _real_begin,
    )
    from target.action_dispatcher import set_backend_resolver
    product_policy.reset_for_tests()
    server._binding_facade_holder["facade"] = None
    server._binding_facade_holder["tracker"] = None

    captured = {}
    provider_calls = []

    class HookUser32:
        def SetWinEventHook(self, event_min, event_max, modname, callback,
                            pid, tid, flags):
            captured["callback"] = callback
            return 77

        def UnhookWinEvent(self, h):
            return True

        def GetMessageW(self, lpmsg, *a):
            assert lpmsg is not None
            # 阻塞至 stop(无超时;真实 GetMessageW 语义)。
            _stop = getattr(events, "_g2_stop", None)
            if _stop is not None:
                _stop.wait()
            return 0

        def PostThreadMessageW(self, *a):
            _stop = getattr(events, "_g2_stop", None)
            if _stop is not None:
                _stop.set()
            return True

    monkeypatch.setattr(events, "_user32", HookUser32(), raising=False)
    monkeypatch.setattr(events, "_g2_stop", threading.Event(),
                        raising=False)
    monkeypatch.setattr(events, "_threading_start",
                        lambda target=None, args=None, daemon=False:
                        _th_daemon(target, args or {}),
                        raising=False)
    backend = _FakeOSBackend(
        {"100": {"process_id": 42, "title": "logo1",
                 "root_control_id": "MainRoot"}},
        capability={"supported": True, "coverage": "strong",
                    "generation": 2})

    def provider():
        provider_calls.append(1)
        callback = captured.get("callback")
        assert callback is not None, "前置:回调已注册"
        # 订阅与枚举边界竞争:DESTROY 先于基线条目到达。
        callback(None, events.EVENT_OBJECT_DESTROY, 100, 0, 0, 0, 0)
        return [{"handle": 100, "pid": 42,
                 "root_control_id": "MainRoot",
                 "class_name": "FakeWnd"}]  # stale 条目

    real_source = events.WindowsEventHookSource(
        existing_windows_provider=provider)
    backend.lifecycle_event_stream = lambda: real_source
    backend.window_sequence_capability = lambda: (
        WindowsPywinautoBackend.window_sequence_capability(backend))
    run = _real_begin(server, monkeypatch,
                      product_policy.GuardedBackend(backend,
                                                    "windows_pywinauto"))
    try:
        tracker = server._binding_facade_holder.get("tracker")
        source = tracker._event_source
        assert type(source).__name__ == "WindowsEventHookSource", (
            f"前置:生产源,实际 {type(source).__name__}")
        tracker.observe()
        # 前置:源持续健康、callback/provider 均执行。
        assert source.healthy is True, (
            f"前置:源持续健康,实际 {source.healthy}/"
            f"{source.failure!r}")
        assert provider_calls, "前置:provider 已执行"
        # 竞争结果:不补活。
        assert tracker.instance_alive("100") is not True, (
            "已知 DESTROY 后 stale 基线条目不得补活")
        cap = tracker.capability()
        assert cap["supported"] is not True, (
            f"无法确定顺序/完整性 → 降级或拒绝,实际 {cap}")
    finally:
        product_policy.end_product_run(run["run_token"])
        product_policy.reset_for_tests()
        server._binding_facade_holder["facade"] = None
        server._binding_facade_holder["tracker"] = None


def test_normal_baseline_positive_still_one_input(monkeypatch):
    """正例保持:正常既有窗(无新 CREATE)基线 → 绑定 + 恰一次执行。"""
    import target.server as server
    from target.automation import instance_events as events
    from test_case.test_recording.test_window_binding_server_wiring import (
        _FakeOSBackend, _real_begin,
    )
    from target.action_dispatcher import set_backend_resolver
    product_policy.reset_for_tests()
    server._binding_facade_holder["facade"] = None
    server._binding_facade_holder["tracker"] = None

    class HookUser32:
        def SetWinEventHook(self, *a):
            return 77

        def UnhookWinEvent(self, h):
            return True

        def GetMessageW(self, lpmsg, *a):
            # 阻塞至 stop(无超时)。
            _stop = getattr(events, "_g2b_stop", None)
            if _stop is not None:
                _stop.wait()
            return 0

        def PostThreadMessageW(self, *a):
            _stop = getattr(events, "_g2b_stop", None)
            if _stop is not None:
                _stop.set()
            return True

    monkeypatch.setattr(events, "_user32", HookUser32(), raising=False)
    monkeypatch.setattr(events, "_g2b_stop", threading.Event(),
                        raising=False)
    monkeypatch.setattr(events, "_threading_start",
                        lambda target=None, args=None, daemon=False:
                        _th_daemon(target, args or {}),
                        raising=False)
    backend = _FakeOSBackend(
        {"100": {"process_id": 42, "title": "logo1",
                 "root_control_id": "MainRoot"}},
        capability={"supported": False, "coverage": "strong",
                    "generation": 2})
    backend.window_sequence_capability = lambda: {
        "decided_by": "instance-tracker",
        "note": "capability decided by this run's instance tracker"}
    run = _real_begin(server, monkeypatch,
                      product_policy.GuardedBackend(backend,
                                                    "windows_pywinauto"))
    try:
        facade = server._binding_facade_holder.get("facade")
        tracker = server._binding_facade_holder.get("tracker")
        tracker.observe()
        assert tracker.instance_alive("100") is True
        bound = json.loads(server.bind_entry_window(
            "main", dict(facade._declared["main"]), run["run_token"]))
        assert "binding_token" in bound, bound
        receipt = json.loads(server.focus_bound_window(
            "main", bound["binding_token"], run["run_token"],
            action_id="pointer.click_screen", action_args={"x": 1, "y": 1}))
        assert receipt.get("ok") is True, receipt
        assert len(backend.dispatch_calls) == 1
    finally:
        product_policy.end_product_run(run["run_token"])
        product_policy.reset_for_tests()
        server._binding_facade_holder["facade"] = None
        server._binding_facade_holder["tracker"] = None


# ---- 0346 缺口3:投递失败/超时/双 stop 首错透传 ----

def _blocking_pump_backend(monkeypatch, *, post_ok=True):
    import target.server as server
    from target.automation import instance_events as events
    from test_case.test_recording.test_window_binding_server_wiring import (
        _FakeOSBackend, _real_begin,
    )
    from target.action_dispatcher import set_backend_resolver
    product_policy.reset_for_tests()
    server._binding_facade_holder["facade"] = None
    server._binding_facade_holder["tracker"] = None

    state = {"posts": 0, "unhooks": 0, "thread_id": None,
             "released": False}

    class PumpUser32:
        def SetWinEventHook(self, *a):
            state["thread_id"] = threading.get_ident()
            return 77

        def GetMessageW(self, lpmsg, *a):
            # 真实阻塞:直到 stop 投递退出消息。
            while not state["released"]:
                state["wait"] = True
                import time as _t
                _t.sleep(0.02)
            return 0

        def UnhookWinEvent(self, h):
            state["unhooks"] += 1
            return True

        def PostThreadMessageW(self, thread_id, msg, w, l):
            state["posts"] += 1
            if not post_ok:
                return False
            state["released"] = True
            return True

    monkeypatch.setattr(events, "_user32", PumpUser32(), raising=False)
    monkeypatch.setattr(events, "_threading_start",
                        lambda target=None, args=None, daemon=False:
                        _th_thread(target, args or {}),
                        raising=False)
    backend = _FakeOSBackend(
        {"100": {"process_id": 42, "title": "logo1",
                 "root_control_id": "MainRoot"}},
        capability={"supported": False, "coverage": "strong",
                    "generation": 2})
    backend.window_sequence_capability = lambda: {
        "decided_by": "instance-tracker",
        "note": "capability decided by this run's instance tracker"}
    # 生产源(WindowsEventHookSource;mock Win32 阻塞泵)—— 原生唤醒
    # 契约的待测路径。经 _real_begin(真实 digest/resolver/租约)。
    from target.automation.instance_events import WindowsEventHookSource
    source = WindowsEventHookSource(
        existing_windows_provider=backend._enumerate_top_windows_windows)
    backend.lifecycle_event_stream = lambda: source
    try:
        run = _real_begin(server, monkeypatch,
                          product_policy.GuardedBackend(backend,
                                                        "windows_pywinauto"))
        facade = server._binding_facade_holder.get("facade")
        tracker = server._binding_facade_holder.get("tracker")
        assert run.get("ok") is True, run
        assert tracker is not None, "begin 必须建立跟踪"
        tracker.observe()
        cap = facade.capability()
        src_state = (source.healthy, source.stopped, source.failure,
                     source._thread.is_alive() if source._thread else None,
                     getattr(tracker, "_baseline_conflict", None),
                     list(getattr(tracker, "_faults", [])))
        bound = json.loads(server.bind_entry_window(
            "main", dict(facade._declared["main"]), run["run_token"]))
        assert "binding_token" in bound, (
            f"前置:bind 必须成功,实际 {bound.get('code')}")
        receipt = json.loads(server.focus_bound_window(
            "main", bound["binding_token"], run["run_token"],
            action_id="pointer.click_screen", action_args={"x": 1, "y": 1}))
        assert receipt.get("ok") is True, receipt
        # setup 成功:租约/源/patch 移交调用方(调用方 finally 释放)。
        state["owned_by_test"] = True
        return state, backend, run, facade, tracker, bound
    except Exception:
        # setup/前置失败也释放:本 run end、源 stop、patch 撤
        # (0506 缺口2:失败不得把租约/源/worker 留给后续测试)。
        state["released"] = True
        if run.get("ok"):
            try:
                product_policy.end_product_run(run["run_token"])
            except ValueError:
                pass
        try:
            source.stop()
        except Exception:  # noqa: BLE001
            pass
        monkeypatch.undo()
        raise


def _th_thread(target, args):
    """同 _th_daemon:只构造未启动线程(source 独占 start)。"""
    return threading.Thread(target=target,
                            args=tuple(args.values()) if args else (),
                            daemon=True)


def test_post_failure_survives_double_stop(monkeypatch):
    """投递失败(PostThreadMessageW=False):worker 未退出 → stop 结构化
    失败;**双 stop(server 清理 hook + tracker hook)不丢首错** —
    end 响应 cleanup_errors 含投递失败(非纯 ok);重复 stop 返回
    稳定累计结果。"""
    import target.server as server
    state, backend, run, facade, tracker, bound = \
        _blocking_pump_backend(monkeypatch, post_ok=False)
    try:
        end = json.loads(server.end_product_run(run["run_token"]))
        assert end.get("ok") is True
        errors = end.get("cleanup_errors") or []
        assert any("post" in str(e).lower() or "wake" in str(e).lower()
                   for e in errors), (
            f"投递失败必须透传,实际 {errors}")
        assert state["worker_exited"] is False if "worker_exited" in state \
            else True
        # 幂等:重复 stop 结果稳定且保留首错。
        again = tracker._event_source.stop()
        assert again.get("ok") is False or again.get("worker_exited")             is False, f"重复 stop 不得丢首错,实际 {again}"
    finally:
        state["released"] = True
        product_policy.reset_for_tests()
        server._binding_facade_holder["facade"] = None
        server._binding_facade_holder["tracker"] = None


def test_post_exception_survives_double_stop(monkeypatch):
    """投递抛异常:同样结构化失败并经双 stop 透传。"""
    import target.server as server
    state, backend, run, facade, tracker, bound = \
        _blocking_pump_backend(monkeypatch, post_ok=True)
    # 让第二次 PostThreadMessageW 抛异常。
    original_post = getattr(events, "_user32").PostThreadMessageW

    def raising_post(*a):
        raise RuntimeError("post thread dead")

    events._user32.PostThreadMessageW = raising_post
    try:
        end = json.loads(server.end_product_run(run["run_token"]))
        errors = end.get("cleanup_errors") or []
        assert any("post" in str(e).lower() for e in errors), (
            f"投递异常必须透传,实际 {errors}")
        again = tracker._event_source.stop()
        assert again.get("ok") is False or again.get("worker_exited")             is False, again
    finally:
        state["released"] = True
        product_policy.reset_for_tests()
        server._binding_facade_holder["facade"] = None
        server._binding_facade_holder["tracker"] = None
