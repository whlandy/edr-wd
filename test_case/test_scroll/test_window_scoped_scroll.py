"""
test_window_scoped_scroll.py — P0.1: window-scoped scroll & verification.

Covers:
  1. target/automation/window_scope.py pure logic — unique-window resolution,
     coordinate conversion, ownership & occlusion checks (unit-tested on any
     host, including macOS).
  2. WindowsPywinautoBackend.scroll_window orchestration — driven with injected
     fake enumeration + hit-test + pyautogui stubs so it runs without a live
     Windows target.
  3. server.scroll_window MCP tool — wired to a fake backend.

The live Win32/pywinauto helpers (_enumerate_top_windows_windows /
_hit_test_top_window_windows) are Windows-only and are replaced by fakes here;
they are not exercised on macOS.
"""

from __future__ import annotations

import json
import sys

import pytest

from automation.window_scope import (
    WindowScopeError,
    assert_owned_and_unoccluded,
    matches_window,
    point_in_rect,
    resolve_unique_window,
    to_screen,
)


# ── Shared window info fixtures ──────────────────────────────────────────────
# A 800x600 "日志中心" window (EDRClient.exe) at screen origin (0,0) and a
# partially-overlapping "ovr" window on top-right.

def _win(handle, title, process_name, pid, left, top, width, height):
    return {
        "handle": handle,
        "title": title,
        "process_name": process_name,
        "pid": pid,
        "rect": {
            "left": left, "top": top,
            "right": left + width, "bottom": top + height,
            "width": width, "height": height,
        },
    }


TARGET = _win(1001, "日志中心", "EDRClient.exe", 6752, 0, 0, 800, 600)
OTHER_SAME_PROC = _win(1002, "日志中心 - 子窗口", "EDRClient.exe", 6752, 100, 0, 400, 300)
OVERLAY = _win(2001, "系统警告", "OvlWin.exe", 8888, 500, 300, 300, 300)
UNRELATED = _win(3001, "记事本", "Notepad.exe", 7777, 900, 50, 200, 200)

ALL_WINDOWS = [TARGET, OTHER_SAME_PROC, OVERLAY, UNRELATED]


class TestMatchesWindow:
    def test_title_re_ignores_case_and_substring(self):
        assert matches_window(TARGET, title_re="日志中心")
        assert matches_window(TARGET, title_re="日志")
        assert matches_window(TARGET, title_re="LOG") is False

    def test_process_name_with_exe_normalised(self):
        assert matches_window(TARGET, process_name="EDRClient.exe")
        assert matches_window(TARGET, process_name="edrclient")
        assert matches_window(TARGET, process_name="Notepad.exe") is False

    def test_pid_exact(self):
        assert matches_window(TARGET, pid=6752)
        assert matches_window(TARGET, pid=9999) is False

    def test_all_selectors_together(self):
        assert matches_window(
            TARGET,
            title_re="日志中心",
            process_name="EDRClient",
            pid=6752,
        )

    def test_bad_regex_is_a_miss_not_a_crash(self):
        assert matches_window(TARGET, title_re="(") is False

    def test_empty_selectors_match_anything(self):
        assert matches_window(TARGET)


class TestResolveUniqueWindow:
    def test_unique_by_title(self):
        info, err = resolve_unique_window(
            ALL_WINDOWS, title_re="^日志中心$",
            process_name="EDRClient.exe",
        )
        assert err is None
        assert info["handle"] == 1001

    def test_ambiguous_without_pid(self):
        info, err = resolve_unique_window(
            ALL_WINDOWS, title_re="日志中心", process_name="EDRClient.exe"
        )
        assert info is None
        assert err is not None
        assert err.code == "target_ambiguous"

    def test_pid_disambiguates(self):
        # Both 日志中心 windows share pid 6752, so adding pid doesn't help
        # here; verify a distinctive title still resolves.
        info, err = resolve_unique_window(
            ALL_WINDOWS, title_re="^日志中心$", pid=6752
        )
        assert err is None
        assert info["handle"] == 1001

    def test_not_found(self):
        info, err = resolve_unique_window(ALL_WINDOWS, process_name="Ghost.exe")
        assert info is None
        assert err is not None
        assert err.code == "target_not_found"

    def test_no_windows_not_found(self):
        info, err = resolve_unique_window([], title_re="日志中心")
        assert info is None
        assert err.code == "target_not_found"


class TestCoordinateConversion:
    def test_to_screen(self):
        assert to_screen(TARGET["rect"], 416, 174) == (416, 174)
        # Non-origin window
        assert to_screen(_win(9, "x", "p", 1, 100, 50, 300, 200)["rect"], 10, 20) == (110, 70)

    def test_point_in_rect(self):
        assert point_in_rect(TARGET["rect"], 0, 0) is True
        assert point_in_rect(TARGET["rect"], 799, 599) is True
        assert point_in_rect(TARGET["rect"], 800, 600) is False  # exclusive
        assert point_in_rect(TARGET["rect"], -1, 0) is False


class TestOwnershipAndOcclusion:
    def test_point_outside_target(self):
        with pytest.raises(WindowScopeError) as ei:
            assert_owned_and_unoccluded(TARGET, ALL_WINDOWS, 1000, 1000)
        assert ei.value.code == "point_outside_window"

    def test_clean_own_point_no_occlusion(self):
        # (50,200) is inside target (0..800 x 0..600) but NOT inside
        # OTHER_SAME_PROC (100,0,500,300) nor OVERLAY (500,300,800,600).
        assert_owned_and_unoccluded(TARGET, ALL_WINDOWS, 50, 200)

    def test_occluded_by_overlay_rect(self):
        # (600,400) is inside target (0..800 x 0..600) but also inside OVERLAY.
        with pytest.raises(WindowScopeError) as ei:
            assert_owned_and_unoccluded(TARGET, ALL_WINDOWS, 600, 400)
        assert ei.value.code == "target_occluded"

    def test_occluded_when_hit_test_conflicts(self):
        # hit-test returns OVERLAY's handle at a point inside OVERLAY.
        with pytest.raises(WindowScopeError) as ei:
            assert_owned_and_unoccluded(
                TARGET, ALL_WINDOWS, 600, 400, hit_test_handle=2001
            )
        assert ei.value.code == "target_occluded"

    def test_hit_test_matches_target_is_ok(self):
        # Even though another rect encloses the point, an accurate Win32
        # hit-test showing the target is on top permits the dispatch.
        assert_owned_and_unoccluded(
            TARGET, ALL_WINDOWS, 600, 400, hit_test_handle=1001
        )

    def test_hit_test_none_with_covering_rect_occludes(self):
        # Without a live hit-test handle, a mere rect overlap is refused.
        with pytest.raises(WindowScopeError) as ei:
            assert_owned_and_unoccluded(TARGET, ALL_WINDOWS, 600, 400)
        assert ei.value.code == "target_occluded"

    def test_same_window_excluded_from_covering(self):
        # The target window itself should never count as covering itself.
        assert_owned_and_unoccluded(TARGET, [TARGET], 600, 400)


# ── Backend orchestration (injected fakes) ───────────────────────────────────
#
# The Windows backend module does a top-level `import pyautogui`, which is a
# Windows-only dependency not installed in the macOS dev venv. Provide a stub
# so the module can be imported and its `scroll_window` orchestration driven
# with injected fakes. The stub only needs the handful of pyautogui entry
# points the backend calls at runtime.

@pytest.fixture(scope="module", autouse=True)
def _pyautogui_stub():
    if "pyautogui" in sys.modules:
        yield sys.modules["pyautogui"]
        return
    stub = type(sys)("pyautogui")
    stub.moveTo = lambda *a, **k: None
    stub.scroll = lambda *a, **k: None
    stub.doubleClick = lambda *a, **k: None
    stub.rightClick = lambda *a, **k: None
    stub.middleClick = lambda *a, **k: None
    stub.dragTo = lambda *a, **k: None
    sys.modules["pyautogui"] = stub
    yield stub
    sys.modules.pop("pyautogui", None)


class TestScrollWindowBackend:
    def _backend(self, windows, hit_handle):
        from automation.windows_pywinauto import WindowsPywinautoBackend
        impl = WindowsPywinautoBackend.__new__(WindowsPywinautoBackend)
        impl._enumerate_top_windows_windows = lambda: windows
        impl._hit_test_top_window_windows = lambda sx, sy: hit_handle
        return impl

    def test_dispatches_to_target_window(self, monkeypatch):
        calls = {"moved": None, "scrolled": None}

        def fake_move(x, y):
            calls["moved"] = (x, y)

        def fake_scroll(clicks):
            calls["scrolled"] = clicks

        monkeypatch.setattr("pyautogui.moveTo", fake_move)
        monkeypatch.setattr("pyautogui.scroll", fake_scroll)

        impl = self._backend(ALL_WINDOWS, 1001)
        result = impl.scroll_window(
            -5, 100, 150,
            window_title_re="^日志中心$",
            expected_process_name="EDRClient.exe",
            expected_pid=6752,
        )
        assert result["ok"] is True
        assert result["scope"] == "window"
        assert result["clicks"] == -5
        assert result["point"] == {"x": 100, "y": 150}
        assert result["window"]["handle"] == 1001
        assert calls["moved"] == (100, 150)
        assert calls["scrolled"] == -5

    def test_target_not_found(self, monkeypatch):
        impl = self._backend(ALL_WINDOWS, 1001)
        result = impl.scroll_window(
            3, 10, 10, window_title_re="不存在的窗口"
        )
        assert result["ok"] is False
        assert result["code"] == "target_not_found"

    def test_target_ambiguous(self, monkeypatch):
        impl = self._backend(ALL_WINDOWS, 1001)
        result = impl.scroll_window(
            3, 10, 10, window_title_re="日志中心", expected_process_name="EDRClient"
        )
        assert result["ok"] is False
        assert result["code"] == "target_ambiguous"

    def test_point_outside_window(self, monkeypatch):
        moved = {"n": 0}

        def fake_move(x, y):
            moved["n"] += 1

        monkeypatch.setattr("pyautogui.moveTo", fake_move)
        monkeypatch.setattr("pyautogui.scroll", lambda clicks: None)

        impl = self._backend(ALL_WINDOWS, 1001)
        result = impl.scroll_window(
            3, 900, 900, window_title_re="^日志中心$"
        )
        assert result["ok"] is False
        assert result["code"] == "point_outside_window"
        assert moved["n"] == 0  # no pointer movement on failure

    def test_occluded(self, monkeypatch):
        def fake_move(x, y):
            raise AssertionError("should not move")

        monkeypatch.setattr("pyautogui.moveTo", fake_move)
        monkeypatch.setattr("pyautogui.scroll", lambda clicks: None)

        impl = self._backend(ALL_WINDOWS, 2001)  # hit-test reports overlay on top
        result = impl.scroll_window(
            3, 600, 400, window_title_re="^日志中心$"
        )
        assert result["ok"] is False
        assert result["code"] == "target_occluded"


# ── Server MCP tool ──────────────────────────────────────────────────────────

def test_server_scroll_window_tool_passthrough(monkeypatch):
    import target.server as srv

    class FakeBackend:
        def scroll_window(self, clicks, x, y, **kw):
            return {"ok": True, "clicks": clicks, "scope": "window", **kw}

    monkeypatch.setattr(srv, "_backend", FakeBackend())
    out = srv.scroll_window(
        -2, 5, 6,
        window_title_re="日志中心",
        expected_process_name="EDRClient.exe",
        expected_pid=6752,
    )
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["scope"] == "window"
    assert payload["window_title_re"] == "日志中心"

def test_server_scroll_window_backend_unsupported(monkeypatch):
    import target.server as srv

    class OtherBackend:
        pass

    monkeypatch.setattr(srv, "_backend", OtherBackend())
    out = srv.scroll_window(1, 0, 0, window_title_re="x")
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["code"] == "backend_unsupported"

def test_server_scroll_window_no_backend(monkeypatch):
    import target.server as srv

    monkeypatch.setattr(srv, "_backend", None)
    out = srv.scroll_window(1, 0, 0, window_title_re="x")
    payload = json.loads(out)
    assert payload["ok"] is False
