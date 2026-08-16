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
