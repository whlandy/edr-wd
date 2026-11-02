# -*- coding: utf-8 -*-
"""verify 拒绝后窗口锁不得残留（后端级回归）。

``lock_window`` 先写 ``self._window_lock = lock``（windows:291 / macos:558），
再 ``verify_window_lock(activate)``。verify 失败时返回 ok=False，但锁字段
从未清除 —— ``get_window_lock`` 继续报 ``locked=True``，下一次
``_ensure_window_lock`` 仍以这把"验证失败"的锁继续判定。任何只看
``lock_window`` 结果失败就放弃的调用方（CLI、ui_controller、replay
legacy 路径）都会把这把锁留在后端进程里，直到显式 unlock 或进程退出。

为什么这是缺陷而不是设计：verify 失败 = "这把锁没有通过所有权验证"，
继续把它当作已生效的锁既污染 get_window_lock 遥测（status 面板会报
"已锁定"），也允许随后任何 verify 通过的场景（前台切回）把这把从未被
调用方确认过的锁复活 —— 调用方早已放弃这次获取。

锁定的安全行为不变：
- ok=False 仍然返回 verify 失败详情（不吞错）；
- verify 成功路径完全不变；
- 明确的 unlock_window 仍然释放 previous（对账可见）。

回归以双后端同语义锁定（windows 用 __new__ 假体，macos 用 monkeypatch
假状态），锁结构、verify 拒绝码/详情、unlock 对账、成功路径不误清。
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


def _matching_state(pid=6752, process_name="EDRClient.exe", title="日志中心",
                    handle=1001):
    """lock_window 判定 criteria 命中的活动窗口状态。"""
    return {
        "ok": True,
        "handle": handle,
        "title": title,
        "pid": pid,
        "process_name": process_name,
        "rectangle": {"left": 0, "top": 0, "right": 800, "bottom": 600,
                      "width": 800, "height": 600},
        "source": "win32_foreground",
    }


def _mismatching_state(pid=9999, process_name="Other.exe", title="别的窗口",
                       handle=2002):
    return {
        "ok": True,
        "handle": handle,
        "title": title,
        "pid": pid,
        "process_name": process_name,
        "rectangle": {"left": 0, "top": 0, "right": 800, "bottom": 600,
                      "width": 800, "height": 600},
        "source": "win32_foreground",
    }


# ── Windows 后端（__new__ 假体，同 test_window_lock_rdp.py）─────────────────

def make_windows_backend(active_state, connected_state=None):
    impl = WindowsPywinautoBackend.__new__(WindowsPywinautoBackend)
    impl._window_lock = None
    impl._gui = type("FakeGui", (), {"main_window": None})()
    impl._active_window_state = lambda: active_state
    # lock_window 先看连接窗口（criteria 判定），verify 看活动/前台窗口 ——
    # 两者分离才能复现"锁成功、验证失败"的真实残留路径。
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
    return impl


class TestWindowsLockVerifyResidue:
    def test_verify_refusal_clears_window_lock(self):
        """连接窗口匹配（criteria 命中）但前台属主不匹配（verify 拒绝）时，
        get_window_lock 不得报 locked=True。"""
        b = make_windows_backend(_mismatching_state(),
                                 connected_state=_matching_state())
        result = b.lock_window(title_re="^日志中心$", process_name="EDRClient.exe",
                               strict=True, activate=True)
        assert result["ok"] is False, "verify 失败必须 ok=False"
        verify = result.get("verify") or {}
        assert verify.get("ok") is False
        assert verify.get("code") == "ownership_mismatch", (
            f"期望 ownership_mismatch（前台属主不匹配），得到 {verify.get('code')!r}"
        )
        lock_state = b.get_window_lock()
        assert lock_state["locked"] is False, (
            "verify 拒绝后窗口锁必须已清除 —— 调用方已放弃这次获取，"
            "残留的锁会让遥测报'已锁定'并可能被后续 verify 复活"
        )
        assert lock_state["lock"] is None

    def test_verify_refusal_lock_identity_not_resurrectable(self):
        """清锁后 _ensure_window_lock 必须因'无锁'走 ok/locked=False 路径，
        而不是拿残留锁继续判定。"""
        b = make_windows_backend(_mismatching_state(),
                                 connected_state=_matching_state())
        b.lock_window(title_re="^日志中心$", process_name="EDRClient.exe")
        guard = b._ensure_window_lock()
        # 无锁时 verify_window_lock 返回 {"ok": True, "locked": False}，
        # _ensure_window_lock 因此放行（无锁即可注入是另一层守卫的事）；
        # 本回归只锁 get_window_lock 的账面：不得报 locked=True。
        assert b.get_window_lock()["locked"] is False

    def test_lock_success_keeps_lock(self):
        """verify 通过路径不变：ok=True 且锁仍在。"""
        b = make_windows_backend(_matching_state())
        result = b.lock_window(title_re="^日志中心$", process_name="EDRClient.exe")
        assert result["ok"] is True
        assert b.get_window_lock()["locked"] is True
        assert b.get_window_lock()["lock"]["pid"] == 6752

    def test_unlock_still_reports_previous(self):
        """显式 unlock 对账语义不变。"""
        b = make_windows_backend(_matching_state())
        b.lock_window(title_re="^日志中心$", process_name="EDRClient.exe")
        out = b.unlock_window()
        assert out["ok"] is True
        assert (out.get("previous") or {}).get("pid") == 6752
        assert b.get_window_lock()["locked"] is False


# ── macOS 后端（实例化 + monkeypatch 假状态）─────────────────────────────────

def make_macos_backend(active_state, connected_state=None):
    b = MacOSAccessibilityBackend()
    b._window_lock = None
    b._connected_window_state = (
        (lambda: dict(connected_state)) if connected_state is not None
        else lambda: dict(active_state))
    b._frontmost_window_state = lambda: dict(active_state)
    b._activate_locked_window = lambda: {"ok": True, "method": "test"}
    b._refresh_connected_window_rectangle = lambda: True
    return b


class TestMacosLockVerifyResidue:
    def test_verify_refusal_clears_window_lock(self):
        b = make_macos_backend(_mismatching_state(pid=9999,
                                                  process_name="Other.exe",
                                                  title="别的窗口"),
                               connected_state=_matching_state(pid=6752,
                                                               process_name="EDRClient.exe",
                                                               title="日志中心"))
        result = b.lock_window(title_re="^日志中心$", process_name="EDRClient.exe",
                               strict=True, activate=True)
        assert result["ok"] is False
        lock_state = b.get_window_lock()
        assert lock_state["locked"] is False, (
            "macOS 后端同样不得在 verify 拒绝后残留窗口锁"
        )
        assert lock_state["lock"] is None

    def test_lock_success_keeps_lock(self):
        b = make_macos_backend(_matching_state(pid=6752,
                                               process_name="EDRClient.exe",
                                               title="日志中心"))
        result = b.lock_window(title_re="^日志中心$", process_name="EDRClient.exe")
        assert result["ok"] is True
        assert b.get_window_lock()["locked"] is True

    def test_unlock_still_reports_previous(self):
        b = make_macos_backend(_matching_state())
        b.lock_window(title_re="^日志中心$", process_name="EDRClient.exe")
        out = b.unlock_window()
        assert out["ok"] is True
        assert (out.get("previous") or {}).get("pid") == 6752
        assert b.get_window_lock()["locked"] is False


if __name__ == "__main__":  # pragma: no cover
    _sys.exit(pytest.main([__file__]))
