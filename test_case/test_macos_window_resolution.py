import pytest

from target.automation import macos_accessibility
from target.automation.macos_accessibility import MacOSAccessibilityBackend


pytestmark = pytest.mark.unit


def test_is_window_open_prefers_exact_process_over_agent_substring(monkeypatch):
    backend = MacOSAccessibilityBackend()
    monkeypatch.setattr(backend, "list_windows", lambda: {
        "ok": True,
        "windows": [
            {"app_name": "HiSecEndpointAgent", "window_title": "Agent", "pid": 906},
            {"app_name": "HiSecEndpoint", "window_title": "HiSec Endpoint", "pid": 89316},
        ],
    })

    result = backend.is_window_open(process_name="HiSecEndpoint")

    assert result["found"] is True
    assert result["windows"][0]["pid"] == 89316


def test_connect_applies_title_when_process_name_is_present(monkeypatch):
    backend = MacOSAccessibilityBackend()
    observed = {}

    def find_window(**kwargs):
        observed.update(kwargs)
        return {
            "ok": True,
            "found": True,
            "windows": [{
                "app_name": "HiSecEndpoint",
                "window_title": "HiSec Endpoint",
                "pid": 89316,
                "rectangle": {"x": 10, "y": 20, "w": 920, "h": 600},
            }],
        }

    monkeypatch.setattr(backend, "is_window_open", find_window)

    result = backend.connect(
        process_name="HiSecEndpoint",
        title_re="^HiSec Endpoint$",
    )

    assert result == {"ok": True, "matched": "process_name", "pid": 89316}
    assert observed == {
        "process_name": "HiSecEndpoint",
        "title_re": "^HiSec Endpoint$",
    }
    assert backend._connected_window_snapshot["title"] == "HiSec Endpoint"


def test_cached_process_window_does_not_bypass_title_filter(monkeypatch):
    backend = MacOSAccessibilityBackend()

    class Window:
        _win_info = {"title": "HiSec Endpoint", "pid": 89316}

        def rectangle(self):
            raise RuntimeError("no AX rectangle")

    class ConnectedApp:
        def windows(self):
            return [Window()]

    backend._connected_app = "HiSecEndpoint"
    backend._connected_app_instance = ConnectedApp()
    monkeypatch.setattr(backend, "list_windows", lambda: {
        "ok": True,
        "windows": [{
            "app_name": "HiSecEndpoint",
            "window_title": "日志中心",
            "pid": 89316,
        }],
    })

    result = backend.is_window_open(
        process_name="HiSecEndpoint",
        title_re="^日志中心$",
    )

    assert result["found"] is True
    assert result["windows"][0]["window_title"] == "日志中心"


def test_explicit_title_does_not_use_hisec_fallback(monkeypatch):
    backend = MacOSAccessibilityBackend()
    monkeypatch.setattr(backend, "list_windows", lambda: {
        "ok": True,
        "windows": [{
            "app_name": "HiSecEndpoint",
            "window_title": "HiSec Endpoint",
            "pid": 89316,
        }],
    })

    result = backend.is_window_open(
        process_name="HiSecEndpoint",
        title_re="^日志中心$",
    )

    assert result == {"ok": True, "found": False, "windows": [], "count": 0}


def test_activate_app_uses_exact_window_owner_pid(monkeypatch):
    backend = MacOSAccessibilityBackend()
    scripts = []
    monkeypatch.setattr(backend, "list_windows", lambda: {
        "ok": True,
        "windows": [
            {"app_name": "HiSecEndpointAgent", "pid": 906},
            {"app_name": "HiSecEndpoint", "pid": 89316},
        ],
    })
    monkeypatch.setattr(backend, "_frontmost_window_state", lambda: {
        "ok": True,
        "process_name": "EDRClient",
        "pid": 89316,
        "title": "HiSec Endpoint",
    })

    def run_osascript(script, timeout=10):
        scripts.append(script)
        if script.startswith("tell application \"") and "System Events" not in script:
            return 1, "application alias not found"
        return 0, ""

    monkeypatch.setattr(macos_accessibility, "_run_osascript", run_osascript)

    result = backend.activate_app(app_name="HiSecEndpoint")

    assert result["ok"] is True
    assert result["pid"] == 89316
    assert result["method"] == "pid"
    assert any("unix id is 89316" in script for script in scripts)


def test_window_lock_uses_pid_as_canonical_process_identity():
    backend = MacOSAccessibilityBackend()

    assert backend._state_matches_lock(
        {
            "ok": True,
            "process_name": "EDRClient",
            "pid": 89316,
            "title": "HiSec Endpoint",
        },
        {
            "process_name": "HiSecEndpoint",
            "pid": 89316,
            "title_re": "^HiSec Endpoint$",
        },
    )


def test_recording_screenshot_rectangle_must_come_from_matching_cg_window(monkeypatch):
    backend = MacOSAccessibilityBackend()
    backend._connected_pid = 89316
    backend._connected_app = "EDRClient"
    backend._connected_window_snapshot = {
        "pid": 89316,
        "owner": "HiSecEndpoint",
        "title": "华为HiSec Endpoint",
        "rectangle": {"x": 0, "y": 0, "w": 800, "h": 600},
    }
    monkeypatch.setattr(backend, "_list_windows_cg", lambda: [
        {
            "app_name": "Other", "window_title": "Other", "pid": 10,
            "rectangle": {"x": 1, "y": 2, "w": 1200, "h": 800},
        },
        {
            "app_name": "HiSecEndpoint", "window_title": "华为HiSec Endpoint",
            "pid": 89316,
            "rectangle": {"x": 120, "y": 80, "w": 920, "h": 600},
        },
    ])

    assert backend._refresh_connected_window_rectangle() is True
    assert backend._connected_window_snapshot["rectangle"] == {
        "x": 120, "y": 80, "w": 920, "h": 600,
    }
    assert backend._connected_window_snapshot["rectangle_verified"] is True


def test_recording_screenshot_rectangle_fails_closed_without_matching_window(monkeypatch):
    backend = MacOSAccessibilityBackend()
    backend._connected_pid = 89316
    backend._connected_window_snapshot = {
        "pid": 89316,
        "owner": "HiSecEndpoint",
        "title": "华为HiSec Endpoint",
        "rectangle": {"x": 0, "y": 0, "w": 800, "h": 600},
    }
    monkeypatch.setattr(backend, "_list_windows_cg", lambda: [])

    assert backend._refresh_connected_window_rectangle() is False
    assert backend._connected_window_snapshot.get("rectangle_verified") is not True


def test_list_windows_enriches_system_events_window_with_cg_rectangle(monkeypatch):
    backend = MacOSAccessibilityBackend()
    monkeypatch.setattr(
        macos_accessibility,
        "_run_osascript",
        lambda _script, timeout=15: (
            0, "Python\t321\tEDR-WD Recorder [recorder_ui=true]\n",
        ),
    )
    monkeypatch.setattr(backend, "_list_windows_cg", lambda: [{
        "app_name": "Python",
        "window_title": "EDR-WD Recorder [recorder_ui=true]",
        "pid": 321,
        "rectangle": {"x": 400, "y": 100, "w": 300, "h": 120},
        "source": "cgwindowlist",
    }])

    result = backend.list_windows()

    assert result["count"] == 1
    assert result["windows"][0]["rectangle"] == {
        "x": 400, "y": 100, "w": 300, "h": 120,
    }
    assert result["windows"][0]["source"] == "system_events+cgwindowlist"


def _lock(pid=1981, process="HiSecEndpointAgent", title="华为智能终端安全系统"):
    return {
        "backend": "macos_accessibility",
        "process_name": process,
        "pid": pid,
        "title_re": title,
        "snapshot": {"process_name": process, "pid": pid, "title": title},
    }


def _zorder_window(pid, title, rect):
    return {"pid": pid, "title": title, "number": pid, "rect": rect}


def test_a_background_only_window_lock_verifies_by_z_order(monkeypatch):
    """A tray-style agent never becomes System Events' frontmost process.

    HiSec's agent and client are both background-only, so the frontmost-process
    check can never succeed for them no matter how visible the window is.
    Without a fallback, a lock on the real product's own window was
    unverifiable and every recording of it refused to start.
    """
    backend = MacOSAccessibilityBackend()
    backend._window_lock = _lock()
    monkeypatch.setattr(backend, "_is_background_only", lambda name: True)
    monkeypatch.setattr(backend, "_desktop_chrome_pids", lambda: set())
    monkeypatch.setattr(backend, "_cg_window_zorder", lambda: [
        _zorder_window(1981, "华为智能终端安全系统", (0.0, 99.0, 920.0, 610.0)),
        _zorder_window(4242, "Some Editor", (0.0, 800.0, 500.0, 200.0)),
    ])

    state = backend._locked_window_visible_state(backend._window_lock)

    assert state is not None
    assert state["process_name"] == "HiSecEndpointAgent"
    assert state["matched_by"] == "cg_zorder_unoccluded"


def test_a_genuinely_covered_background_window_is_not_treated_as_locked(monkeypatch):
    backend = MacOSAccessibilityBackend()
    backend._window_lock = _lock()
    monkeypatch.setattr(backend, "_is_background_only", lambda name: True)
    monkeypatch.setattr(backend, "_desktop_chrome_pids", lambda: set())
    monkeypatch.setattr(backend, "_cg_window_zorder", lambda: [
        _zorder_window(4242, "Another App", (0.0, 0.0, 1920.0, 1080.0)),
        _zorder_window(1981, "华为智能终端安全系统", (0.0, 99.0, 920.0, 610.0)),
    ])

    assert backend._locked_window_visible_state(backend._window_lock) is None


def test_the_docks_desktop_spanning_entry_does_not_count_as_occlusion(monkeypatch):
    """Dock registers a full-screen CGWindowList entry it does not paint.

    Discovered live: it made a fully visible HiSec window read as covered,
    so the z-order fallback rejected a lock that was in fact perfectly valid.
    """
    backend = MacOSAccessibilityBackend()
    backend._window_lock = _lock()
    monkeypatch.setattr(backend, "_is_background_only", lambda name: True)
    monkeypatch.setattr(backend, "_desktop_chrome_pids", lambda: {1631})
    monkeypatch.setattr(backend, "_cg_window_zorder", lambda: [
        _zorder_window(1631, "Dock", (0.0, 0.0, 1920.0, 1080.0)),
        _zorder_window(1981, "华为智能终端安全系统", (0.0, 99.0, 920.0, 610.0)),
    ])

    state = backend._locked_window_visible_state(backend._window_lock)

    assert state is not None
    assert state["matched_by"] == "cg_zorder_unoccluded"


def test_a_regular_application_does_not_use_the_z_order_fallback(monkeypatch):
    """The fallback exists for processes that cannot be frontmost at all.

    A normal app that simply is not in front right now must still fail the
    lock check, or the lock would stop meaning anything.
    """
    backend = MacOSAccessibilityBackend()
    backend._window_lock = _lock(pid=36245, process="TextEdit", title="notes.txt")
    monkeypatch.setattr(backend, "_is_background_only", lambda name: False)

    assert backend._locked_window_visible_state(backend._window_lock) is None


def test_a_background_window_that_is_not_on_screen_is_never_matched(monkeypatch):
    backend = MacOSAccessibilityBackend()
    backend._window_lock = _lock()
    monkeypatch.setattr(backend, "_is_background_only", lambda name: True)
    monkeypatch.setattr(backend, "_desktop_chrome_pids", lambda: set())
    monkeypatch.setattr(backend, "_cg_window_zorder", lambda: [])

    assert backend._locked_window_visible_state(backend._window_lock) is None
