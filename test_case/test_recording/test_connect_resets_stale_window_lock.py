# -*- coding: utf-8 -*-
"""connect 换目标时旧窗口锁不得残留（后端级回归）。

既有窗口所有权链是 connect → lock_window → verify_window_lock
（录制：ui_controller.py::_run_ownership_chain；回放 legacy：
runtime.py::acquire_target_window）。§7g 已保证 verify 拒绝时 lock_window
自清；但上一会话的锁若在显式 unlock 之前（进程崩溃恢复、调用方直接
放弃、lock_window ok=True 之后调用方进程死亡）留在后端进程里，
下一次 connect 成功后这把旧锁仍然生效：

- ``get_window_lock`` 报 ``locked=True``，锁身份却指向上一个窗口 ——
  status 遥测对当前会话说谎；
- 下一次 ``verify_window_lock`` 可能因前台恰好匹配旧锁身份而"复活"
  一把从未被本会话确认过的锁；
- 新链的 lock_window 写入新锁之前，_ensure_window_lock 拿旧锁判定，
  若旧窗口恰在前面，注入会被授权到错误的所有权上下文。

为什么这是缺陷而不是设计：connect 的语义是"绑定到新窗口"。旧锁的
所有权凭据与新连接的窗口无关；保留它只会在新会话里延续旧会话的
授权状态。安全行为不变 —— 无锁时指针注入在 WindowGuard 边界
fail-closed（空 snapshot → WindowIdentity.from_native 抛错），清锁
绝不放宽注入授权；产品运行路径走 connect_window_identity +
随后的 lock_window，同样不依赖 connect 保留旧锁。

回归以双后端同语义锁定（windows 用 __new__ 假体，macos 用实例化 +
monkeypatch 假状态），断言：
- connect 成功后旧锁必须清除，previous 身份对账可见；
- connect 失败（参数缺失/目标不存在）不得清除锁（失败路径不动账）；
- 无参 connect 的参数校验拒绝同样不动账；
- 产品路径 connect_window_identity 不受本修复影响（不在此处清锁，
  它紧随 lock_window 重新建立所有权）。
"""

from __future__ import annotations

import sys as _sys

# windows 后端模块顶层 import pyautogui（仅 Windows 有）；macOS dev 环境先
# 放桩，同 test_window_lock_rdp.py 的做法。
if "pyautogui" not in _sys.modules:
    _pag = _sys.modules.setdefault("pyautogui", type("pyautogui_stub", (), {})())
    _pag.moveTo = lambda *a, **k: None
    _pag.scroll = lambda *a, **k: None
    _pag.doubleClick = lambda *a, **k: None
    _pag.rightClick = lambda *a, **k: None
    _pag.middleClick = lambda *a, **k: None
    _pag.dragTo = lambda *a, **k: None

import pytest

from target.automation.macos_accessibility import MacOSAccessibilityBackend
from target.automation.windows_pywinauto import WindowsPywinautoBackend


def _stale_lock(handle=1001, pid=6752, title="日志中心"):
    """上一会话留下的、已确认过的锁（verify 通过的形状）。"""
    return {
        "backend": "test",
        "title_re": "^日志中心$",
        "process_name": "EDRClient.exe",
        "pid": pid,
        "handle": handle,
        "strict": True,
        "snapshot": {
            "ok": True,
            "handle": handle,
            "title": title,
            "pid": pid,
            "process_name": "EDRClient.exe",
            "rectangle": {"left": 0, "top": 0, "right": 800, "bottom": 600},
            "source": "win32_foreground",
        },
    }


# ── Windows 后端（__new__ 假体，同 batch-13 假体做法）────────────────────────

def make_windows_backend(active_state, connected_state=None, gui_connect=None):
    impl = WindowsPywinautoBackend.__new__(WindowsPywinautoBackend)
    impl._window_lock = None
    impl._gui = type("FakeGui", (), {"main_window": None})()
    impl._active_window_state = lambda: active_state
    impl._connected_window_state = (
        (lambda: connected_state) if connected_state is not None
        else lambda: dict(active_state))
    impl._state_matches_lock = WindowsPywinautoBackend._state_matches_lock.__get__(
        impl, WindowsPywinautoBackend)
    impl._activate_locked_window = lambda: {"ok": True, "method": "test"}
    impl._win32_foreground_state = lambda: active_state
    impl._enumerate_top_windows_windows = lambda: []
    impl._locked_window_still_exists = (
        WindowsPywinautoBackend._locked_window_still_exists.__get__(
            impl, WindowsPywinautoBackend))
    if gui_connect is not None:
        impl._gui.connect_by_title = gui_connect
    return impl


class TestWindowsConnectResetsStaleLock:
    def test_successful_connect_clears_stale_lock(self):
        """connect 成功必须清掉上一会话的锁 —— 新连接窗口与旧锁身份无关。"""
        b = make_windows_backend(
            dict(_stale_lock()["snapshot"]),
            gui_connect=lambda *a, **k: {"ok": True, "title": "日志中心"})
        b._window_lock = _stale_lock()
        assert b.get_window_lock()["locked"] is True

        result = b.connect(title_re="^日志中心$")
        assert result["ok"] is True, f"connect 应成功：{result}"
        lock_state = b.get_window_lock()
        assert lock_state["locked"] is False, (
            "connect 换目标后旧窗口锁必须清除 —— 新会话不得继承上一会话的"
            "所有权授权；无锁注入由 WindowGuard fail-closed 保证"
        )
        assert lock_state["lock"] is None

    def test_successful_connect_reports_cleared_identity(self):
        """清除必须对账可见：result 带 released 锁身份，不是静默丢失。"""
        b = make_windows_backend(
            dict(_stale_lock()["snapshot"]),
            gui_connect=lambda *a, **k: {"ok": True, "title": "日志中心"})
        b._window_lock = _stale_lock()
        result = b.connect(title_re="^日志中心$")
        assert result["ok"] is True
        released = result.get("released_window_lock")
        assert isinstance(released, dict), (
            "connect 必须报告被释放的旧锁身份（对账），不得静默清除"
        )
        assert released.get("pid") == 6752
        assert released.get("handle") == 1001

    def test_failed_connect_keeps_lock(self):
        """connect 失败（目标不存在）不得动账 —— 只有真实换目标才清锁。"""
        b = make_windows_backend(
            dict(_stale_lock()["snapshot"]),
            gui_connect=lambda *a, **k: {"ok": False, "error": "no such window"})
        b._window_lock = _stale_lock()
        result = b.connect(title_re="^不存在的窗口$")
        assert result["ok"] is False
        assert b.get_window_lock()["locked"] is True, (
            "connect 失败不得清除既有锁 —— 失败路径不改所有权状态"
        )

    def test_argument_validation_refusal_keeps_lock(self):
        """无参 connect 的参数校验拒绝发生在'真实尝试连接'之前，不得动账。"""
        b = make_windows_backend(dict(_stale_lock()["snapshot"]))
        b._window_lock = _stale_lock()
        result = b.connect()
        assert result["ok"] is False
        assert b.get_window_lock()["locked"] is True

    def test_then_lock_verify_chain_still_works(self):
        """清锁后的所有权链 connect → lock → verify 行为不变。"""
        b = make_windows_backend(
            dict(_stale_lock()["snapshot"]),
            gui_connect=lambda *a, **k: {"ok": True, "title": "日志中心"})
        b._window_lock = _stale_lock()
        b.connect(title_re="^日志中心$")
        assert b.get_window_lock()["locked"] is False
        locked = b.lock_window(title_re="^日志中心$",
                               process_name="EDRClient.exe",
                               strict=True, activate=True)
        assert locked["ok"] is True, f"lock_window 应成功：{locked}"
        assert b.get_window_lock()["locked"] is True
        verified = b.verify_window_lock(activate=False)
        assert verified["ok"] is True


# ── macOS 后端（实例化 + monkeypatch 假状态）─────────────────────────────────

def make_macos_backend(active_state, is_window_open=None):
    b = MacOSAccessibilityBackend()
    b._window_lock = None
    b._connected_window_state = lambda: dict(active_state)
    b._frontmost_window_state = lambda: dict(active_state)
    b._activate_locked_window = lambda: {"ok": True, "method": "test"}
    b._refresh_connected_window_rectangle = lambda: True
    if is_window_open is not None:
        b.is_window_open = is_window_open
    return b


class TestMacosConnectResetsStaleLock:
    def test_successful_connect_clears_stale_lock(self):
        snapshot = dict(_stale_lock()["snapshot"])
        b = make_macos_backend(
            snapshot,
            is_window_open=lambda **k: {"ok": True, "found": True, "windows": [{
                "pid": 6752, "window_title": "日志中心", "app_name": "EDRClient",
                "handle": 2002, "rectangle": snapshot["rectangle"],
            }]})
        b._window_lock = _stale_lock(handle=1001)
        assert b.get_window_lock()["locked"] is True

        result = b.connect(title_re="^日志中心$")
        assert result["ok"] is True, f"connect 应成功：{result}"
        lock_state = b.get_window_lock()
        assert lock_state["locked"] is False, (
            "macOS 后端同样不得在 connect 换目标后残留上一会话的锁"
        )
        assert lock_state["lock"] is None

    def test_successful_connect_reports_cleared_identity(self):
        snapshot = dict(_stale_lock()["snapshot"])
        b = make_macos_backend(
            snapshot,
            is_window_open=lambda **k: {"ok": True, "found": True, "windows": [{
                "pid": 6752, "window_title": "日志中心", "app_name": "EDRClient",
                "handle": 2002, "rectangle": snapshot["rectangle"],
            }]})
        b._window_lock = _stale_lock(handle=1001)
        result = b.connect(title_re="^日志中心$")
        assert result["ok"] is True
        released = result.get("released_window_lock")
        assert isinstance(released, dict)
        assert released.get("pid") == 6752
        assert released.get("handle") == 1001

    def test_failed_connect_keeps_lock(self):
        b = make_macos_backend(
            dict(_stale_lock()["snapshot"]),
            is_window_open=lambda **k: {"ok": True, "found": False, "windows": []})
        b._window_lock = _stale_lock()
        result = b.connect(title_re="^不存在的窗口$")
        assert result["ok"] is False
        assert b.get_window_lock()["locked"] is True


# ── 产品运行路径不受影响 ──────────────────────────────────────────────────────

class TestProductRunPathUnaffected:
    def test_connect_window_identity_does_not_clear_lock(self):
        """产品路径用 connect_window_identity + 随后 lock_window 重新建立
        所有权；本修复不得波及它（它不在这里清锁，语义与 connect 不同）。"""
        impl = WindowsPywinautoBackend.__new__(WindowsPywinautoBackend)
        impl._window_lock = _stale_lock()
        impl._gui = type("FakeGui", (), {"main_window": None})()
        impl._gui.connect_by_window = (
            lambda handle, pid, timeout=10.0: {"ok": True, "title": "日志中心"})
        result = impl.connect_window_identity(2002, 6752)
        assert result["ok"] is True
        assert impl.get_window_lock()["locked"] is True, (
            "connect_window_identity 不是所有权重置点 —— 清锁只属于 connect"
        )


if __name__ == "__main__":  # pragma: no cover
    _sys.exit(pytest.main([__file__]))
