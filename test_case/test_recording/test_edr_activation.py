"""Activating the EDR UI: which path is required, and which is only a fallback."""

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
    """Enough of the client to drive activate_edr's three steps."""

    def __init__(self, *, service_state="missing", agent_window=False,
                 client_opens=True):
        cls = _client_class()
        self.activate_edr = cls.activate_edr.__get__(self)
        self._ensure_ui_service = cls._ensure_ui_service.__get__(self)
        self._service_state = lambda name: service_state
        self._start_service = lambda name, timeout=30.0: (False, "access denied")
        self._ipc_listening = lambda port, timeout=1.0: service_state == "running"
        self._agent_window = agent_window
        self._client_opens = client_opens
        self.launched: list[list[str]] = []

    # activate_edr launches through subprocess.Popen; capture instead.
    def _record(self, args, cwd=None):
        self.launched.append(list(args))
        return True, None

    def is_window_open(self, process_name=None, **kwargs):
        found = (
            self._agent_window if "Hisec" in (process_name or "")
            else False
        )
        return {"ok": True, "found": found}

    def wait_window(self, process_name=None, **kwargs):
        if "EDRClient" in (process_name or ""):
            return {"ok": True, "found": self._client_opens}
        return {"ok": True, "found": self._agent_window}

    def connect_by_process(self, name, timeout=10):
        return {"ok": False, "error": "no such process"}


@pytest.fixture
def patched_launch(monkeypatch):
    """Route activate_edr's Popen through the fake client's recorder."""
    _client_class()  # installs the platform stubs before the import
    module = sys.modules["target.pywinauto_client"]

    holder: dict = {}

    class _Popen:
        def __init__(self, args, cwd=None):
            holder["client"].launched.append(list(args))

    monkeypatch.setattr(module.subprocess, "Popen", _Popen)
    return holder


def test_a_missing_agent_service_does_not_block_the_client_path(patched_launch):
    """The agent window is not a precondition for activation.

    Step 2 launches the client directly and is the primary path; step 1 only
    feeds step 3's fallback click. Treating a missing agent as fatal hid a
    target where the client would have opened on the first attempt.
    """
    client = _Client(service_state="missing", client_opens=True)
    patched_launch["client"] = client
    result = client.activate_edr(wait=True, timeout=1.0)
    assert result["ok"] is True
    assert any("EDRClient.exe" in args[0] for args in client.launched)


def test_the_show_ui_request_is_never_sent_when_nothing_can_receive_it(patched_launch):
    """`cmd ui` is a gRPC client that exits 0 whether or not anyone listened."""
    client = _Client(service_state="missing")
    patched_launch["client"] = client
    client.activate_edr(wait=True, timeout=1.0)
    assert not any(args[1:] == ["cmd", "ui"] for args in client.launched)


def test_the_agent_diagnosis_survives_into_a_failure_that_needed_it(patched_launch):
    """When the client will not open either, the fallback needs the agent.

    That is the point at which the agent's own failure becomes the useful
    explanation rather than "cannot connect".
    """
    client = _Client(service_state="missing", client_opens=False)
    patched_launch["client"] = client
    result = client.activate_edr(wait=True, timeout=1.0)
    assert result["ok"] is False
    assert result["stage"] == "fallback_connect_hisec"
    assert "HiSec OneAgent Service" in result["agent_error"]
    assert result["service_check"]["service_state"] == "missing"


@pytest.mark.parametrize(
    "state, expected",
    [("missing", "ui_service_missing"),
     ("stopped", "ui_service_will_not_start")],
)
def test_the_service_check_distinguishes_absent_from_broken(state, expected):
    check = _Client(service_state=state)._ensure_ui_service(
        "HiSec OneAgent Service", 58299, 1.0,
    )
    assert check["ok"] is False
    assert check["stage"] == expected


def test_a_running_service_with_no_listener_names_the_port():
    client = _Client(service_state="running")
    client._ipc_listening = lambda port, timeout=1.0: False
    check = client._ensure_ui_service("HiSec OneAgent Service", 58299, 1.0)
    assert check["stage"] == "ui_service_ipc_unreachable"
    assert "58299" in check["error"]
