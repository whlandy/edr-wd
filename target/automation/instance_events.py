"""Windows 原生生命周期事件源(生产实装;09 §7.4/§8)。

``WindowsEventHookSource`` 用 Win32 ``SetWinEventHook`` 订阅
EVENT_OBJECT_CREATE/DESTROY/HIDE/SHOW,**同一 owner worker 线程**
完成注册/消息泵/退订(Microsoft 官方要求:out-of-context 事件在注册
线程交付,UnhookWinEvent 必须同注册线程 — 见
https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwineventhook
与 .../unhookwinevent)。

ABI:自行定义 WINFUNCTYPE 回调原型与 HWINEVENTHOOK/MSG 结构原型
(CPython wintypes 无 WINEVENTPROC;不使用 create_string_buffer 冒充
已声明 LPMSG)。

过滤:回调按 idObject/idChild 过滤 — 仅顶层窗口对象
(OBJID_WINDOW=0, idChild=0)进入实例事实;OBJID_CLIENT(-4)/child=9
等控件级 DESTROY 不映射为窗口销毁。

健康/覆盖事实(如实,证据不足 → 上层协商 unsupported):
- hook 注册失败(返回 0)→ healthy=False + failure;
- 泵线程退出(异常/GetMessageW<=0)→ healthy=False + failure;
- 有界队列溢出 → 丢事件事实(fail closed);
- 非 Windows 平台(user32 不可得)→ healthy=False + failure。

stop():向泵线程投递退出(PostThreadMessageW/唤醒)→ UnhookWinEvent
(同 owner 线程)→ join 确认退出 → 结构化退订结果(失败如实降级)。

本地回归用 mock user32 入口(_user32 可替换);真实 Windows 正确性
仍需授权实机验收 — 本模块不宣称实机通过。
"""

from __future__ import annotations

import ctypes
import queue
import threading

__all__ = ["WindowsEventHookSource", "LIFECYCLE_EVENT_IDS",
           "EVENT_OBJECT_CREATE", "EVENT_OBJECT_DESTROY",
           "EVENT_OBJECT_HIDE", "EVENT_OBJECT_SHOW",
           "EVENT_OBJECT_CLOAKED", "EVENT_OBJECT_UNCLOAKED",
           "OBJID_WINDOW"]

# Win32 EVENT_* 常量(winuser.h)。
EVENT_OBJECT_CREATE = 0x8000
EVENT_OBJECT_DESTROY = 0x8001
EVENT_OBJECT_SHOW = 0x8002
EVENT_OBJECT_HIDE = 0x8003
EVENT_OBJECT_CLOAKED = 0x8017
EVENT_OBJECT_UNCLOAKED = 0x8018

# 对象标识:仅顶层窗口对象进入实例事实。
OBJID_WINDOW = 0
OBJID_CLIENT = -4

LIFECYCLE_EVENT_IDS = frozenset({
    EVENT_OBJECT_CREATE, EVENT_OBJECT_DESTROY,
    EVENT_OBJECT_SHOW, EVENT_OBJECT_HIDE,
    EVENT_OBJECT_CLOAKED, EVENT_OBJECT_UNCLOAKED,
})

WINEVENT_OUTOFCONTEXT = 0x0000
WINEVENT_SKIPOWNPROCESS = 0x0002

# 模块级 Win32 入口(测试可替换的接缝)。
_user32 = None
_user32_loaded = False
_threading_start = threading.Thread

# 自定义 ABI(CPython wintypes 无 WINEVENTPROC;HWINEVENTHOOK 为
# DWORD_PTR;MSG 结构按 winuser.h 定义)。
_WINFUNCTYPE = None
_HWINEVENTHOOK = None
_MSG = None


class _MSG(ctypes.Structure):
    """winuser.h MSG 结构(自行定义;不使用 create_string_buffer 冒充
    已声明 LPMSG 参数)。"""
    _fields_ = [("hwnd", ctypes.c_void_p),
                ("message", ctypes.c_uint),
                ("wParam", ctypes.c_size_t),
                ("lParam", ctypes.c_ssize_t),
                ("time", ctypes.c_uint),
                ("pt_x", ctypes.c_long),
                ("pt_y", ctypes.c_long)]


# HWINEVENTHOOK = DWORD_PTR(自行定义,CPython wintypes 无该类型)。
_HWINEVENTHOOK = ctypes.c_size_t
# Wineventproc 回调原型在注册时构建(WINFUNCTYPE 仅 Windows 提供;
# loader 不依赖它 — 复审 0148 缺口2)。
_WINFUNCTYPE = getattr(ctypes, "WINFUNCTYPE", None)


def _win_event_proc_prototype():
    if _WINFUNCTYPE is None:
        return None
    return _WINFUNCTYPE(
        None, _HWINEVENTHOOK, ctypes.c_ulong, _HWINEVENTHOOK,
        ctypes.c_long, ctypes.c_long, ctypes.c_ulong, ctypes.c_ulong)


def _get_user32():
    """延迟加载 ctypes user32 并绑定自行定义的原型。加载成功只要求
    user32 可得 + 原型可用 c_void_p/c_size_t 表达(回调原型在注册时
    构建);非 Windows 平台无 windll.user32 → None(如实 unavailable)。"""
    global _user32, _user32_loaded
    if _user32 is not None:
        return _user32
    if _user32_loaded:
        return None
    try:
        user32 = ctypes.windll.user32
    except Exception:  # noqa: BLE001 — 非 Windows 平台
        _user32_loaded = True
        return None
    try:
        # 原型:SetWinEventHook(...) → HWINEVENTHOOK(pfn 以 c_void_p
        # 传入;注册时经 _win_event_proc_prototype 构建真实回调)。
        user32.SetWinEventHook.restype = _HWINEVENTHOOK
        user32.SetWinEventHook.argtypes = [
            ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
        user32.UnhookWinEvent.restype = ctypes.c_long
        user32.UnhookWinEvent.argtypes = [_HWINEVENTHOOK]
        user32.GetMessageW.restype = ctypes.c_long
        user32.GetMessageW.argtypes = [
            ctypes.POINTER(_MSG), ctypes.c_void_p,
            ctypes.c_uint, ctypes.c_uint]
        _user32 = user32
        _user32_loaded = True
        return user32
    except Exception:  # noqa: BLE001
        _user32_loaded = True
        return None


_KIND_BY_EVENT = {
    EVENT_OBJECT_CREATE: "create",
    EVENT_OBJECT_DESTROY: "destroy",
    EVENT_OBJECT_SHOW: "show",
    EVENT_OBJECT_HIDE: "hide",
    EVENT_OBJECT_CLOAKED: "hide",
    EVENT_OBJECT_UNCLOAKED: "show",
}


class WindowsEventHookSource:
    """SetWinEventHook 订阅源:单一 owner worker 线程完成注册/泵/退订。

    健康事实:
    - hook 注册失败(返回 0)→ healthy=False + failure;
    - 泵线程退出(异常/GetMessageW<=0)→ healthy=False + failure;
    - 有界队列溢出 → 丢事件事实(fail closed);
    - 非 Windows 平台 → healthy=False + failure。
    """

    def __init__(self, *, process_id: int = 0, max_queue: int = 4096,
                 existing_windows_provider=None):
        """``existing_windows_provider``: 订阅建立时枚举**既有**顶层窗口
        (0148 缺口1:生产创建源只发未来 hook 事件,尚无既有窗口基线
        接线 — 不能要求用户先重开窗口才能绑定)。provider 返回
        [{handle, pid, root_control_id, class_name}, ...];基线以
        create 事实入流,供跟踪器建立初始代际。"""
        self._existing_windows_provider = existing_windows_provider
        self._process_id = process_id
        self._queue: queue.Queue = queue.Queue(maxsize=max_queue)
        self._healthy = False
        self._stopped = False
        self.failure = ""
        self._hook = None
        self._thread = None
        self._ready = threading.Event()
        self._wake = threading.Event()
        self._start_error: str | None = None
        self._unhook_result: dict = {}

    # ---- 生命周期 ----

    def start(self) -> None:
        if self._stopped:
            raise RuntimeError("event source already stopped")
        if self._thread is not None:
            # 重入保护:同一源实例只启动一次(worker 线程/句柄不可
            # 复用启动;二次 start 为调用方缺陷,如实拒绝)。
            return
        # owner worker 线程:注册 + 泵 + 退订都在它上面(官方要求)。
        self._thread = _threading_start(
            target=self._worker, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5.0):
            self._healthy = False
            self.failure = self._start_error or \
                "event source worker did not become ready"
        # worker 失败(如 user32 不可得)在握手里如实反映。
        if self._start_error:
            self._healthy = False
            self.failure = self._start_error

    def _worker(self) -> None:
        """owner worker:注册 → 泵 → (stop 唤醒) → 退订 → 退出。"""
        self._owner_thread_id = threading.get_ident()
        user32 = _get_user32()
        if user32 is None:
            self._start_error = self.failure or \
                "user32 unavailable (non-Windows platform)"
            self._ready.set()
            return
        try:
            callback = self._win_event_proc()
            hook = user32.SetWinEventHook(
                min(LIFECYCLE_EVENT_IDS), max(LIFECYCLE_EVENT_IDS),
                None, callback, self._process_id, 0,
                WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS)
        except Exception as exc:  # noqa: BLE001
            self._start_error = f"SetWinEventHook failed: {exc}"
            self._ready.set()
            return
        if not hook:
            self._start_error = \
                "SetWinEventHook registration failed (returned 0)"
            self._ready.set()
            return
        self._hook = hook
        self._healthy = True
        # 既有窗口基线(0148 缺口1;0256/0346 修订):订阅建立即枚举
        # 当前窗口,以 **initial_observed** 事实入流(不是新 CREATE —
        # 基线与生命周期事件分离,窗口竞争时协调边界明确)。
        if self._existing_windows_provider is not None:
            try:
                for window in self._existing_windows_provider() or []:
                    self._queue.put_nowait({
                        "kind": "initial_observed",
                        "native_id": str(window.get("handle")),
                        "pid": window.get("pid"),
                        "root_control_id": window.get("root_control_id"),
                        "class_name": window.get("class_name"),
                    })
            except queue.Full:
                self._healthy = False
                self.failure = "baseline queue overflow"
            except Exception as exc:  # noqa: BLE001 — 基线失败如实降级
                self._healthy = False
                self.failure = f"baseline enumeration failed: {exc}"
        self._ready.set()
        # 消息泵:官方要求 pump 在注册线程。MSG 实际分配并 byref 传入
        # (0256 缺口3:NULL MSG 违反 Win32 参数契约);返回值 0=WM_QUIT
        # 正常退出,-1=错误;真实 Windows 阻塞由消息驱动,stop() 经
        # PostThreadMessageW 投递退出消息唤醒。
        pump_msg = _MSG()
        try:
            while not self._stopped:
                result = user32.GetMessageW(ctypes.byref(pump_msg), None,
                                            0, 0)
                if result == -1:
                    self._healthy = False
                    self.failure = "GetMessageW error (-1)"
                    break
                if result == 0:
                    break  # WM_QUIT:正常退出
                self._wake.wait(timeout=0.05)
                self._wake.clear()
        except Exception as exc:  # noqa: BLE001 — 泵故障事实
            self._healthy = False
            self.failure = (f"message loop thread fault: "
                            f"{type(exc).__name__}: {exc}")
        # 退订:同一 owner worker 线程(官方要求),结构化结果。
        if self._hook is not None:
            try:
                ok = user32.UnhookWinEvent(self._hook)
                self._unhook_result = {"ok": bool(ok),
                                       "unhook": {"hook": bool(hook),
                                                  "returned": bool(ok)}}
                if not ok:
                    self.failure = self.failure or \
                        "UnhookWinEvent returned FALSE (structured)"
            except Exception as exc:  # noqa: BLE001
                self._unhook_result = {"ok": False,
                                       "error": str(exc)}
                self.failure = self.failure or \
                    f"UnhookWinEvent failed: {exc}"
            self._hook = None

    def stop(self) -> None:
        """停止:投递原生退出消息(唤醒阻塞泵)→ 同 owner 线程退订 →
        join 确认退出。结构化结果(unhook 失败/超时)不吞 — 经 tracker
        cleanup 透传给 end/expiry(0256 缺口3)。"""
        if self._stopped:
            # 幂等:不重复副作用,但返回**累计结果**(含首轮投递失败/
            # worker 未退/退订失败的首错 — 0346 缺口3:幂等不得丢错)。
            result = dict(self._unhook_result or {})
            result["stopped"] = True
            worker_exited = not (self._thread is not None
                                 and self._thread.is_alive())
            result["worker_exited"] = worker_exited
            if (self.failure and "post" in self.failure.lower()) or \
                    not worker_exited:
                result["ok"] = False
                result["error"] = self.failure or \
                    "worker did not exit on stop (native wake unsuccessful)"
            return result
        self._stopped = True
        self._healthy = False
        self._wake.set()
        # 原生唤醒:向 owner 线程投递 WM_QUIT(PostThreadMessageW);
        # mock 之外的 user32 契约:线程 id 来自注册时记录。
        user32 = _get_user32()
        if user32 is not None and getattr(self, "_owner_thread_id", None):
            try:
                posted = user32.PostThreadMessageW(
                    self._owner_thread_id, 0x0012, 0, 0)  # WM_QUIT
                if not posted:
                    self.failure = self.failure or \
                        "PostThreadMessageW failed (no native wake)"
            except Exception as exc:  # noqa: BLE001
                self.failure = self.failure or \
                    f"native wake failed: {exc}"
        if self._thread is not None and self._thread.is_alive():
            try:
                self._thread.join(timeout=2.0)
            except TypeError:
                self._thread.join()  # 测试 mock 无 timeout 形参
        result = dict(self._unhook_result or {})
        result["stopped"] = self.stopped
        worker_exited = not (self._thread is not None
                             and self._thread.is_alive())
        result["worker_exited"] = worker_exited
        # 投递失败/异常/超时(原生唤醒未成功)与退订失败同为结构化失败:
        if (self.failure and "post" in self.failure.lower()) or \
                not worker_exited:
            result["ok"] = False
            result["error"] = self.failure or \
                "worker did not exit on stop (native wake unsuccessful)"
        return result

    # ---- Win32 回调(系统线程;过滤后入队) ----

    _callback_ref = None  # 注册的回调引用(worker 设置)

    def _win_event_proc(self):
        """WinEventproc 回调:纯 Python 可调用(测试可捕获);真实
        Windows 注册路径用 WINFUNCTYPE 包装(可用时)。"""
        self._callback_ref = self._on_win_event_raw
        proto = _win_event_proc_prototype()
        if proto is not None:
            self._callback_ref = proto(self._on_win_event_raw)
        return self._callback_ref

    def _on_win_event_raw(self, hook, event, hwnd, id_object, id_child,
                          thread_id, time_ms):
        owner = None
        if hwnd:
            try:
                # Design 11 D2 R1: native owner edge captured at the hook —
                # cheap Win32, no UIA; 0/self → absent, never fabricated.
                owner = self._user32().GetAncestor(hwnd, 3) or None  # GA_ROOTOWNER
            except Exception:
                owner = None
        self.on_win_event(event, native_id=str(hwnd or ""),
                          id_object=id_object, id_child=id_child,
                          owner_handle=int(owner) if owner else None)

    def _user32(self):
        import ctypes
        dll = ctypes.WinDLL("user32", use_last_error=True)
        dll.GetAncestor.restype = ctypes.c_void_p
        dll.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        return dll

    def on_win_event(self, event_id: int, *, native_id: str,
                     id_object: int = OBJID_WINDOW,
                     id_child: int = 0,
                     owner_handle: int | None = None) -> None:
        """生命周期事件入口(hook 回调或测试注入)。过滤:仅顶层窗口
        对象(OBJID_WINDOW, idChild=0)进入实例事实 — 控件级
        (OBJID_CLIENT/child!=0)不映射为窗口生命周期。"""
        if self._stopped:
            return
        if id_object != OBJID_WINDOW or id_child != 0:
            return
        kind = _KIND_BY_EVENT.get(event_id)
        if kind is None:
            return
        try:
            event_row = {"kind": kind, "native_id": native_id}
            if owner_handle:
                event_row["ownerHandle"] = owner_handle
            self._queue.put_nowait(event_row)
        except queue.Full:
            # 有界队列:溢出 = 事件丢失事实(丢事件可检测性)。
            self._healthy = False
            self.failure = "event queue overflow (events lost)"

    # ---- 消费者接口(LifecycleEventStream 协议) ----

    @property
    def healthy(self) -> bool:
        thread_alive = (self._thread is not None
                        and self._thread.is_alive())
        return self._healthy and (self._thread is None or thread_alive)

    @property
    def stopped(self) -> bool:
        return self._stopped

    def drain_events(self) -> list[dict]:
        batch = []
        while True:
            try:
                batch.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return batch
