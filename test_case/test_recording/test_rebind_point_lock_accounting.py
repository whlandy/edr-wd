# -*- coding: utf-8 -*-
"""换绑点（activate_edr / legacy connect 别名）不得残留上一会话的窗口锁。

§7h（批次 14）已保证 backend ``connect()`` 换目标时清除旧锁并对账；
但仍有绕过该对账的**换绑点**：

- ``WindowsPywinautoBackend.activate_edr`` 在 ``_gui`` 层内部调用
  ``connect_by_process``（HisecEndpointAgent 回退、EDRClient 收尾连接），
  把会话的已连接窗口换成本进程**第一个**可连顶层窗口 —— EDRClient.exe
  拥有多个顶层窗口（logo1 主窗、详情窗等）时可能落到与调用方
  connect/lock 不同的窗口上；
- 三个 legacy 别名 ``connect_by_title`` / ``connect_by_process`` /
  ``connect_by_pid`` 直接换绑 ``_gui.main_window``，完全没有 §7h 对账；
- ``MacOSAccessibilityBackend.activate_edr`` 通过
  ``_mark_edr_client_connected`` / swift helper 直接重写 ``_connected_*``
  连接状态（快照里 handle 会丢失，pid 可能取到回退值）。

残留后果与 §7h 相同：``get_window_lock`` 对新会话谎报
``locked=True``（锁身份却指向旧窗口）；下一次 verify 可能因前台恰好
匹配旧锁而"复活"一把从未被本会话确认过的锁。

语义（两条规则，都只收紧不放宽）：

- **activate_edr（对账后置）**：操作后已连接窗口若不再匹配既有锁
  （pid/handle/title 任一可观测字段漂移）→ 丢弃锁并在 result 里报告
  被释放身份；仍然匹配（activate 的 already_open 路径连回同一窗口）
  → 保留，不打断仍描述同一窗口的所有权链。"无法观测"（连接状态
  ok=False）视为不能反驳，保留 —— 与 ``lock_criteria_mismatch`` 的
  只比较可观测字段同一哲学。清锁只会移除注入授权（随后在
  ``_ensure_window_lock`` 被拒），绝不会放宽授权。
- **legacy 别名（§7h 同形）**：尝试前清除，成功（真实换绑）→ 报告
  released；失败 → 恢复。与 §7h ``connect()`` 完全同形，只是补齐
  它漏掉的三条路。

产品运行路径不受影响：``connect_window_identity`` →
``connect_by_window`` 是解析后精确绑定，紧随 ``lock_window`` 重建
所有权，不属于本修复的对账点（批次 14
``TestProductRunPathUnaffected`` 已锁定）。
"""

from __future__ import annotations

import sys as _sys

# windows 后端模块顶层 import pyautogui（仅 Windows 有）；macOS dev 环境
# 先放桩，同 test_connect_resets_stale_window_lock.py 的做法。
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

pytestmark = pytest.mark.unit


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


class _FakeWindow:
    """够 ``_connected_window_state`` 读的 pywinauto 窗口假体。"""

    def __init__(self, title, pid, handle):
        self._title, self._pid, self._handle = title, pid, handle

    def window_text(self):
        return self._title

    def process_id(self):
        return self._pid

    @property
    def handle(self):
        return self._handle

    def rectangle(self):
        class _R:
            left, top, width, height = 0, 0, 800, 600
        return _R()


def make_windows_backend(window=None, gui_activate=None):
    impl = WindowsPywinautoBackend.__new__(WindowsPywinautoBackend)
    impl._window_lock = None
    impl._gui = type("FakeGui", (), {"main_window": window})()
    if gui_activate is not None:
        impl._gui.activate_edr = gui_activate
    return impl


class TestWindowsActivateEdrReconcilesLock:
    def test_rebind_to_another_window_drops_and_reports_the_lock(self):
        """activate_edr 内部换绑到别的窗口（同名不同 pid / 本进程另一窗口）
        后，旧锁不得残留 —— get_window_lock 不得对新会话谎报锁定。"""
        old_window = _FakeWindow("日志中心", 6752, 1001)
        new_window = _FakeWindow("日志中心", 7020, 2002)
        impl = make_windows_backend(window=old_window)

        def fake_activate(**kw):
            impl._gui.main_window = new_window
            return {"ok": True, "already_open": True}

        impl._gui.activate_edr = fake_activate
        impl._window_lock = _stale_lock()

        result = impl.activate_edr()
        assert result["ok"] is True
        assert impl.get_window_lock()["locked"] is False, (
            "activate_edr 已把会话换绑到另一窗口 —— 描述旧窗口的锁必须清除"
        )
        released = result.get("released_window_lock")
        assert isinstance(released, dict), (
            "被清除的锁身份必须对账可见，不得静默丢失"
        )
        assert released.get("pid") == 6752
        assert released.get("handle") == 1001

    def test_same_window_rebind_keeps_the_ownership_chain(self):
        """activate_edr 连回同一窗口（already_open 常态）不得打断所有权链。"""
        window = _FakeWindow("日志中心", 6752, 1001)
        impl = make_windows_backend(
            window=window,
            gui_activate=lambda **kw: {"ok": True, "already_open": True})
        impl._window_lock = _stale_lock()

        result = impl.activate_edr()
        assert result["ok"] is True
        assert result.get("released_window_lock") is None, (
            "连接窗口未变 —— 不得谎报释放"
        )
        assert impl.get_window_lock()["locked"] is True, (
            "锁仍描述当前连接的同一窗口 —— 保留"
        )

    def test_same_pid_same_title_other_handle_drops_the_lock(self):
        """同进程多顶层窗口（EDRClient 的 logo1 主窗 / 详情窗）：pid、title
        都不变，只有 handle 漂移 —— 锁仍然必须清除，否则指针注入会以旧
        窗口的身份授权到本进程的另一个窗口上。"""
        window = _FakeWindow("日志中心", 6752, 1001)
        impl = make_windows_backend(window=window)

        def fake_activate(**kw):
            impl._gui.main_window = _FakeWindow("日志中心", 6752, 3003)
            return {"ok": True, "already_open": True}

        impl._gui.activate_edr = fake_activate
        impl._window_lock = _stale_lock()

        result = impl.activate_edr()
        assert result["ok"] is True
        assert impl.get_window_lock()["locked"] is False, (
            "handle 漂移 = 换到了本进程另一个窗口 —— 旧锁的身份凭据失效"
        )
        assert result["released_window_lock"]["handle"] == 1001

    def test_no_lock_leaves_result_untouched(self):
        impl = make_windows_backend(
            window=_FakeWindow("日志中心", 6752, 1001),
            gui_activate=lambda **kw: {"ok": True})
        result = impl.activate_edr()
        assert result["ok"] is True
        assert "released_window_lock" not in result

    def test_unobservable_connected_state_keeps_the_lock(self):
        """连接状态不可观测（ok=False）= 不能反驳锁，保留。"""
        impl = make_windows_backend(
            gui_activate=lambda **kw: {"ok": True})
        impl._connected_window_state = lambda: {"ok": False, "error": "Not connected"}
        impl._window_lock = _stale_lock()

        result = impl.activate_edr()
        assert result["ok"] is True
        assert impl.get_window_lock()["locked"] is True

    def test_title_only_drift_drops_the_lock_when_pid_handle_unobservable(self):
        """pid/handle 不可观测（降级观测形态）时 title 是唯一可比字段 ——
        标题漂移仍必须清锁。"""
        window = _FakeWindow("日志中心", None, None)
        impl = make_windows_backend(window=window)

        def fake_activate(**kw):
            impl._gui.main_window = _FakeWindow("扫描详情", None, None)
            return {"ok": True, "already_open": True}

        impl._gui.activate_edr = fake_activate
        impl._window_lock = _stale_lock()

        result = impl.activate_edr()
        assert result["ok"] is True
        assert impl.get_window_lock()["locked"] is False, (
            "pid/handle 不可观测时 title 漂移 = 锁不能继续描述当前连接"
        )


class TestWindowsLegacyAliasesAccounting:
    def test_connect_by_title_success_clears_and_reports(self):
        impl = make_windows_backend(window=_FakeWindow("日志中心", 6752, 1001))
        impl._window_lock = _stale_lock()
        impl._gui.connect_by_title = (
            lambda title_re, timeout=10.0: {"ok": True, "title": "logo1"})

        result = impl.connect_by_title("^logo1$")
        assert result["ok"] is True
        assert impl.get_window_lock()["locked"] is False, (
            "legacy 别名换绑成功 = 真实换目标 —— §7h 同形清账"
        )
        released = result.get("released_window_lock")
        assert isinstance(released, dict) and released.get("pid") == 6752

    def test_connect_by_title_failure_keeps_the_lock(self):
        impl = make_windows_backend(window=_FakeWindow("日志中心", 6752, 1001))
        impl._window_lock = _stale_lock()
        impl._gui.connect_by_title = (
            lambda title_re, timeout=10.0: {"ok": False, "error": "no such window"})

        result = impl.connect_by_title("^不存在的窗口$")
        assert result["ok"] is False
        assert impl.get_window_lock()["locked"] is True, (
            "别名失败路径不得动账 —— 只有真实换绑才清锁"
        )

    def test_connect_by_process_success_clears_and_reports(self):
        impl = make_windows_backend(window=_FakeWindow("日志中心", 6752, 1001))
        impl._window_lock = _stale_lock()
        impl._gui.connect_by_process = (
            lambda name, timeout=10.0: {"ok": True, "pid": 5948})

        result = impl.connect_by_process("EDRClient.exe")
        assert result["ok"] is True
        assert impl.get_window_lock()["locked"] is False
        assert result["released_window_lock"]["handle"] == 1001

    def test_connect_by_pid_success_clears_and_reports(self):
        impl = make_windows_backend(window=_FakeWindow("日志中心", 6752, 1001))
        impl._window_lock = _stale_lock()
        impl._gui.connect_by_pid = lambda pid: {"ok": True, "pid": pid}

        result = impl.connect_by_pid(5948)
        assert result["ok"] is True
        assert impl.get_window_lock()["locked"] is False

    def test_alias_without_lock_adds_no_key(self):
        impl = make_windows_backend(window=_FakeWindow("日志中心", 6752, 1001))
        impl._gui.connect_by_title = (
            lambda title_re, timeout=10.0: {"ok": True, "title": "logo1"})
        result = impl.connect_by_title("^logo1$")
        assert result["ok"] is True
        assert "released_window_lock" not in result


# ── macOS 后端 ────────────────────────────────────────────────────────────────

def make_macos_backend():
    b = MacOSAccessibilityBackend()
    b._window_lock = None
    return b


class TestMacosActivateEdrReconcilesLock:
    def test_remark_drift_drops_and_reports_the_lock(self):
        """activate_edr 的 _mark_edr_client_connected 重写连接快照（handle
        丢失、pid 可回退）—— 漂移后旧锁不得残留。"""
        b = make_macos_backend()
        b._window_lock = _stale_lock()
        b._connected_pid = 6752
        b._connected_app = "EDRClient"
        b._connected_window_snapshot = {
            "pid": 6752, "title": "日志中心", "owner": "EDRClient",
            "handle": 1001,
        }

        def fake_impl(**kwargs):
            # 重绑到另一窗口（不同 pid、无 handle —— 快照被重写的真实形状）
            b._connected_pid = 7020
            b._connected_window_snapshot = {
                "pid": 7020, "title": "华为HiSec Endpoint",
                "owner": "HiSecEndpoint",
            }
            return {"ok": True, "stage": "done"}

        b._activate_edr_impl = fake_impl
        result = b.activate_edr()
        assert result["ok"] is True
        assert b.get_window_lock()["locked"] is False, (
            "macOS activate_edr 重写连接状态后，旧锁不得残留"
        )
        released = result.get("released_window_lock")
        assert isinstance(released, dict)
        assert released.get("pid") == 6752
        assert released.get("handle") == 1001

    def test_matching_state_keeps_the_lock(self):
        b = make_macos_backend()
        b._window_lock = _stale_lock()
        b._connected_pid = 6752
        b._connected_app = "EDRClient"
        b._connected_window_snapshot = {
            "pid": 6752, "title": "日志中心", "owner": "EDRClient",
            "handle": 1001,
        }
        b._activate_edr_impl = lambda **kwargs: {"ok": True, "stage": "done"}
        result = b.activate_edr()
        assert result["ok"] is True
        assert result.get("released_window_lock") is None
        assert b.get_window_lock()["locked"] is True

    def test_pid_only_drift_drops_the_lock(self):
        """macOS 快照重写常丢 handle（_mark_edr_client_connected 不写
        handle）—— 仅 pid 漂移也必须清锁。"""
        b = make_macos_backend()
        b._window_lock = _stale_lock()
        b._connected_pid = 6752
        b._connected_app = "EDRClient"
        b._connected_window_snapshot = {
            "pid": 6752, "title": "日志中心", "owner": "EDRClient",
            "handle": 1001,
        }

        def fake_impl(**kwargs):
            # 标题正则用 escape 后的旧标题不匹配新标题；这里让新快照的
            # 标题**仍然匹配** title_re（^日志中心$，正则对子串宽松），
            # handle 不变 —— 只让 pid 漂移，保证清锁只能来自 pid 检查。
            b._connected_pid = 7020
            b._connected_window_snapshot = {
                "pid": 7020, "title": "日志中心", "owner": "EDRClient",
                "handle": 1001,
            }
            return {"ok": True}

        b._activate_edr_impl = fake_impl
        result = b.activate_edr()
        assert result["ok"] is True
        assert b.get_window_lock()["locked"] is False, (
            "仅 pid 漂移（handle 不变、标题仍匹配）= 换窗口 —— 清锁只能"
            "来自 pid 检查"
        )

    def test_no_lock_adds_no_key(self):
        b = make_macos_backend()
        b._connected_pid = 6752
        b._connected_app = "EDRClient"
        b._activate_edr_impl = lambda **kwargs: {"ok": True}
        result = b.activate_edr()
        assert "released_window_lock" not in result

    def test_unobservable_state_keeps_the_lock(self):
        b = make_macos_backend()
        b._window_lock = _stale_lock()
        b._activate_edr_impl = lambda **kwargs: {"ok": True}
        result = b.activate_edr()
        assert result["ok"] is True
        assert b.get_window_lock()["locked"] is True


if __name__ == "__main__":  # pragma: no cover
    _sys.exit(pytest.main([__file__]))
