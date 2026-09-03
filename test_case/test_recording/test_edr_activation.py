"""Activating the EDR UI, and saying why when it cannot be activated."""

from __future__ import annotations

import sys
import types

import pytest

pytestmark = pytest.mark.unit


def _client_class():
    """Import the client with its Windows-only dependencies stubbed out."""
    if "target.pywinauto_client" in sys.modules:
        return sys.modules["target.pywinauto_client"].WindowsGUI
    for name in ("pywinauto", "pywinauto.controls", "pywinauto.controls.uiawrapper"):
        sys.modules.setdefault(name, types.ModuleType(name))
    pywinauto = sys.modules["pywinauto"]
    for attr in ("Application", "timings", "mouse", "keyboard", "Desktop"):
        if not hasattr(pywinauto, attr):
            setattr(pywinauto, attr, types.SimpleNamespace())
    import target.pywinauto_client as module

    return module.WindowsGUI


class _Client:
    """Just enough of the client to drive step 1 of activate_edr."""

    def __init__(self, *, service_state, window_found=False, ipc=False):
        cls = _client_class()
        self.activate_edr = cls.activate_edr.__get__(self)
        self._ensure_ui_service = cls._ensure_ui_service.__get__(self)
        self._service_state = lambda name: service_state
        self._start_service = lambda name, timeout=30.0: (False, "access denied")
        self._ipc_listening = lambda port, timeout=1.0: ipc
        self._window_found = window_found
        self.launched: list[list[str]] = []

    def is_window_open(self, process_name=None, **kwargs):
        return {"ok": True, "found": self._window_found}

    def wait_window(self, **kwargs):
        return {"ok": True, "found": False}


def test_a_missing_ui_service_is_named_rather_than_reported_as_a_missing_window():
    """The failure has to name the service, not the symptom.

    `cmd ui` is a gRPC client: it exits 0 whether or not anything received the
    message. Reporting only "window did not appear" sends the reader looking
    at the window, the launcher, and the desktop session — none of which is
    the cause.
    """
    result = _Client(service_state="missing").activate_edr(wait=True, timeout=1.0)
    assert result["ok"] is False
    assert result["stage"] == "ui_service_missing"
    assert "HiSec OneAgent Service" in result["error"]
    assert "reinstall" in result["error"]


def test_the_show_ui_request_is_never_sent_into_the_void():
    client = _Client(service_state="missing")
    client.activate_edr(wait=True, timeout=1.0)
    assert client.launched == []


def test_a_service_that_will_not_start_is_distinguished_from_one_not_installed():
    result = _Client(service_state="stopped").activate_edr(wait=True, timeout=1.0)
    assert result["stage"] == "ui_service_will_not_start"
    assert "access denied" in result["error"]


def test_a_running_service_with_no_listener_names_the_port():
    result = _Client(service_state="running", ipc=False).activate_edr(
        wait=True, timeout=1.0,
    )
    assert result["stage"] == "ui_service_ipc_unreachable"
    assert "58299" in result["error"]


def test_an_open_agent_window_skips_the_service_check_entirely():
    """Activation must not depend on the service when the window is already up."""
    client = _Client(service_state="missing", window_found=True)
    client._service_state = lambda name: pytest.fail("should not be consulted")
    result = client.activate_edr(wait=False, timeout=1.0)
    assert result.get("stage") != "ui_service_missing"
