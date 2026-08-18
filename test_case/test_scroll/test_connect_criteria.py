# -*- coding: utf-8 -*-
"""connect() must satisfy every supplied criterion, not just the first.

Two HiSec windows share the title `logo1` (EDRClient.exe and
HiSecEndpointAgent.exe). Dispatching on `title_re` alone and ignoring
`process_name` connects to whichever the enumeration happens to yield first,
and the wrong window then propagates into the window lock and every pointer
action taken through it.
"""

import pytest

import sys as _sys
if "pyautogui" not in _sys.modules:
    _pag = _sys.modules.setdefault("pyautogui", type("pyautogui_stub", (), {})())
    _pag.moveTo = lambda *a, **k: None
    _pag.scroll = lambda *a, **k: None
    _pag.doubleClick = lambda *a, **k: None
    _pag.rightClick = lambda *a, **k: None
    _pag.middleClick = lambda *a, **k: None
    _pag.dragTo = lambda *a, **k: None

from automation.windows_pywinauto import WindowsPywinautoBackend

pytestmark = pytest.mark.unit


EDR_CLIENT = {
    "title": "logo1", "process_id": 5948, "handle": 131700,
    "class_name": "Dialog",
}
ENDPOINT_AGENT = {
    "title": "logo1", "process_id": 6960, "handle": 328664,
    "class_name": "Dialog",
}


class _GUI:
    """Records which connect path the backend chose."""

    def __init__(self, windows):
        self._windows = windows
        self.calls = []

    def is_window_open(self, title_re=None, process_name=None, class_name=None):
        matched = [
            w for w in self._windows
            if (title_re is None or title_re.strip("^$") in w["title"])
            and (process_name is None or w["process_name"].lower() == process_name.lower())
        ]
        return {"ok": True, "found": bool(matched), "count": len(matched), "windows": matched}

    def connect_by_window(self, handle, pid, timeout=10.0):
        self.calls.append(("window", handle, pid))
        return {"ok": True, "pid": pid, "handle": handle, "title": "logo1"}

    def connect_by_pid(self, pid):
        self.calls.append(("pid", pid))
        return {"ok": True, "pid": pid}

    def connect_by_title(self, title_re, timeout=10.0):
        self.calls.append(("title", title_re))
        return {"ok": True, "title": title_re}

    def connect_by_process(self, process_name, timeout=10.0):
        self.calls.append(("process", process_name))
        return {"ok": True, "process_name": process_name}


class _GUIWithoutWindowConnect(_GUI):
    """An older client that can only bind a whole process."""

    connect_by_window = None


def _backend(windows, gui_class=_GUI):
    impl = WindowsPywinautoBackend.__new__(WindowsPywinautoBackend)
    for w in windows:
        w.setdefault("process_name", "")
    impl._gui = gui_class(windows)
    return impl


def _windows():
    return [
        {**EDR_CLIENT, "process_name": "EDRClient.exe"},
        {**ENDPOINT_AGENT, "process_name": "HiSecEndpointAgent.exe"},
    ]


def test_title_and_process_together_select_the_matching_window():
    impl = _backend(_windows())
    out = impl.connect(title_re="^logo1$", process_name="EDRClient.exe")
    assert out["ok"] is True
    assert out["pid"] == 5948
    assert impl._gui.calls == [("window", 131700, 5948)]


def test_the_same_title_alone_still_uses_the_legacy_single_criterion_path():
    impl = _backend(_windows())
    out = impl.connect(title_re="^logo1$")
    assert out["ok"] is True
    assert impl._gui.calls == [("title", "^logo1$")]


def test_a_contradicting_pid_and_process_pair_matches_nothing():
    impl = _backend(_windows())
    out = impl.connect(process_name="EDRClient.exe", pid=6960)
    assert out["ok"] is False
    assert out["code"] == "connect_target_not_found"
    assert impl._gui.calls == []


def test_criteria_matching_several_windows_are_refused_with_candidates():
    duplicates = [
        {**EDR_CLIENT, "process_name": "EDRClient.exe"},
        {**EDR_CLIENT, "process_name": "EDRClient.exe", "handle": 999},
    ]
    impl = _backend(duplicates)
    out = impl.connect(title_re="^logo1$", process_name="EDRClient.exe")
    assert out["ok"] is False
    assert out["code"] == "connect_target_ambiguous"
    assert {c["handle"] for c in out["candidates"]} == {131700, 999}
    assert impl._gui.calls == []


def test_app_name_is_treated_as_a_process_criterion():
    impl = _backend(_windows())
    out = impl.connect(title_re="^logo1$", app_name="EDRClient.exe")
    assert out["ok"] is True and out["pid"] == 5948


def test_a_backend_without_connect_by_window_falls_back_to_pid():
    impl = _backend(_windows(), gui_class=_GUIWithoutWindowConnect)
    out = impl.connect(title_re="^logo1$", process_name="EDRClient.exe")
    assert out["ok"] is True
    assert impl._gui.calls == [("pid", 5948)]


def test_no_criteria_is_still_an_explicit_error():
    impl = _backend(_windows())
    out = impl.connect()
    assert out["ok"] is False and "Must specify" in out["error"]
