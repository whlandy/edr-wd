"""Whole-window material capture: scope growth, walking, and redaction."""

from __future__ import annotations

import threading
import time

import pytest

from target.recording.inventory import (
    ControlInventory,
    content_hash,
    normalize_controls,
)
from target.recording.models import CaptureScope, RawRecording, RecordingModelError
from target.recording.windows import WindowsUIACorrelator, WindowsUIAResolver

pytestmark = pytest.mark.unit


SCOPE = CaptureScope(
    "win-dev", "windows_pywinauto", "HisecEndpointAgent.exe", "^logo1$",
)


class _Resolver:
    """A resolver whose application gains a process mid-recording."""

    def __init__(self, running: dict[str, str], root: str) -> None:
        self.running = running
        self.root = root
        self.walks: list[int] = []
        self.trees: dict[int, dict] = {}
        self.walk_delay = 0.0

    def application_process_names(self, process_name: str) -> list[str]:
        prefix = self.root.lower() + "\\"
        return sorted(
            name for name, exe in self.running.items()
            if exe.lower().startswith(prefix)
        )

    def application_root(self, process_name: str) -> str:
        return self.root

    def process_belongs_to_root(self, process_name: str, root: str):
        executable = self.running.get(process_name)
        if executable is None:
            return None  # not running: undecided, not "no"
        return executable.lower().startswith(root.lower() + "\\")

    def windows(self):
        return []

    def control_tree(self, handle: int, max_depth: int = 12):
        self.walks.append(handle)
        if self.walk_delay:
            time.sleep(self.walk_delay)
        return self.trees.get(handle)


def _tree(*controls) -> dict:
    return {"ok": True, "title": "Log Center", "controls": list(controls)}


def _control(automation_id: str, text: str = "", **extra) -> dict:
    control = {
        "automation_id": automation_id,
        "control_type": "Button",
        "class_name": "Qt",
        "text": text,
        "rectangle": {"x": 1, "y": 2, "w": 10, "h": 20},
        "is_visible": True,
        "is_enabled": True,
        "depth": 1,
    }
    control.update(extra)
    return control


# --- scope grows with the application, not with a seed-time snapshot -------

def test_a_process_launched_after_seeding_still_belongs_to_the_application():
    """The client is started by the agent during the very flow being recorded.

    Resolving the application's process set once at seed time excludes it, and
    with it every click the user then makes, the window it opens, and the step
    that opened it.
    """
    resolver = _Resolver(
        {"hisecendpointagent": r"C:\Program Files\HiSec\agent.exe"},
        r"C:\Program Files\HiSec",
    )
    correlator = WindowsUIACorrelator(resolver)
    correlator.seed_scope(SCOPE)

    assert not correlator._process_in_scope("EDRClient.exe", SCOPE)

    # The agent launches the client, part-way through the recording.
    resolver.running["edrclient"] = r"C:\Program Files\HiSec\client\EDRClient.exe"

    assert correlator._process_in_scope("EDRClient.exe", SCOPE)
    assert "edrclient" in correlator.scope_process_names


def test_an_unrelated_application_is_never_admitted_by_growth():
    resolver = _Resolver(
        {
            "hisecendpointagent": r"C:\Program Files\HiSec\agent.exe",
            "notepad": r"C:\Windows\System32\notepad.exe",
        },
        r"C:\Program Files\HiSec",
    )
    correlator = WindowsUIACorrelator(resolver)
    correlator.seed_scope(SCOPE)
    assert not correlator._process_in_scope("notepad.exe", SCOPE)


def test_a_process_that_is_not_running_stays_undecided_rather_than_refused():
    """Caching "no" for a process nobody has started recreates the bug.

    The seed-time snapshot failed precisely by turning "not running yet" into
    a permanent verdict, so an absent process must leave no cached answer.
    """
    resolver = _Resolver(
        {"hisecendpointagent": r"C:\Program Files\HiSec\agent.exe"},
        r"C:\Program Files\HiSec",
    )
    correlator = WindowsUIACorrelator(resolver)
    correlator.seed_scope(SCOPE)
    correlator._process_in_scope("EDRClient.exe", SCOPE)
    assert "edrclient" not in correlator._process_scope_cache


# --- the inventory itself -------------------------------------------------

def _drain(inventory: ControlInventory, *, expected: int, timeout: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(inventory.snapshots) >= expected:
            return
        time.sleep(0.01)


def test_a_window_the_flow_enters_is_walked_without_waiting_out_the_debounce():
    resolver = _Resolver({}, "root")
    resolver.trees[7] = _tree(_control("LogCenterWindow.table"))
    inventory = ControlInventory(resolver, debounce_ms=60_000)
    inventory.start()
    try:
        inventory.request(handle=7, reason="window_changed", title="Log Center")
        _drain(inventory, expected=1)
        snapshots = inventory.snapshots
    finally:
        inventory.stop()
    assert len(snapshots) == 1
    assert snapshots[0]["window"]["rootAutomationId"] == "LogCenterWindow"
    assert snapshots[0]["controlCount"] == 1


def test_a_burst_of_steps_in_one_window_costs_a_single_walk():
    """Correlation must not pay for the walk, and neither must the user.

    A walk costs ~0.4-1.2s against this application; taking one per event
    would spend longer collecting material than the flow takes to perform.
    """
    resolver = _Resolver({}, "root")
    resolver.trees[7] = _tree(_control("W.a"))
    inventory = ControlInventory(resolver, debounce_ms=40)
    inventory.start()
    try:
        for _ in range(25):
            inventory.request(handle=7, reason="step")
        _drain(inventory, expected=1)
        time.sleep(0.15)
    finally:
        inventory.stop()
    assert len(resolver.walks) == 1


def test_an_unchanged_window_is_not_stored_twice():
    resolver = _Resolver({}, "root")
    resolver.trees[7] = _tree(_control("W.a", "同一页"))
    inventory = ControlInventory(resolver, debounce_ms=0)
    inventory.start()
    try:
        inventory.request(handle=7, reason="window_changed")
        _drain(inventory, expected=1)
        inventory.request(handle=7, reason="window_changed")
        time.sleep(0.15)
    finally:
        inventory.stop()
    assert len(inventory.snapshots) == 1
    assert inventory.skipped.get("unchanged") == 1


def test_a_tab_switch_inside_one_window_is_new_material():
    resolver = _Resolver({}, "root")
    resolver.trees[7] = _tree(_control("W.tab1"))
    inventory = ControlInventory(resolver, debounce_ms=0)
    inventory.start()
    try:
        inventory.request(handle=7, reason="window_changed")
        _drain(inventory, expected=1)
        resolver.trees[7] = _tree(_control("W.tab2"))
        inventory.request(handle=7, reason="step")
        _drain(inventory, expected=2)
    finally:
        inventory.stop()
    assert len(inventory.snapshots) == 2


def test_a_moved_window_is_not_mistaken_for_a_changed_one():
    """Geometry is recorded but does not define content.

    A generated trace resolves controls by identity, so re-storing a whole
    tree because the user dragged the window adds bytes and no material.
    """
    resolver = _Resolver({}, "root")
    resolver.trees[7] = _tree(_control("W.a"))
    inventory = ControlInventory(resolver, debounce_ms=0)
    inventory.start()
    try:
        inventory.request(handle=7, reason="window_changed")
        _drain(inventory, expected=1)
        moved = _control("W.a")
        moved["rectangle"] = {"x": 500, "y": 400, "w": 10, "h": 20}
        resolver.trees[7] = _tree(moved)
        inventory.request(handle=7, reason="step")
        time.sleep(0.15)
    finally:
        inventory.stop()
    assert len(inventory.snapshots) == 1


def test_the_page_the_user_ended_on_survives_the_stop():
    """The last window reached is the one a trace most needs.

    It is also always the one whose debounce has not expired when recording
    stops, so a stop that simply discarded pending walks would lose it.
    """
    resolver = _Resolver({}, "root")
    resolver.trees[7] = _tree(_control("W.a"))
    inventory = ControlInventory(resolver, debounce_ms=60_000)
    inventory.start()
    inventory.request(handle=7, reason="step")
    inventory.stop()
    assert len(inventory.snapshots) == 1


def test_a_slow_walk_cannot_hold_the_recording_open():
    resolver = _Resolver({}, "root")
    resolver.trees[7] = _tree(_control("W.a"))
    resolver.walk_delay = 1.5
    inventory = ControlInventory(resolver, debounce_ms=0, stop_timeout=0.2)
    inventory.start()
    inventory.request(handle=7, reason="window_changed")
    time.sleep(0.05)
    started = time.monotonic()
    inventory.stop()
    assert time.monotonic() - started < 1.0
    assert any("did not stop" in error for error in inventory.errors)


def test_a_failed_walk_is_counted_rather_than_raised():
    resolver = _Resolver({}, "root")
    resolver.trees[7] = {"ok": False, "error": "window gone"}
    inventory = ControlInventory(resolver, debounce_ms=0)
    inventory.start()
    try:
        inventory.request(handle=7, reason="window_changed")
        time.sleep(0.15)
    finally:
        inventory.stop()
    assert inventory.snapshots == ()
    assert inventory.skipped.get("walk_failed") == 1


def test_requesting_a_walk_never_raises_on_the_correlation_path():
    inventory = ControlInventory(_Resolver({}, "root"))
    inventory.request(handle=None, reason="step")
    inventory.request(handle=0, reason="step")
    assert inventory.skipped.get("no_window_handle") == 2


# --- redaction ------------------------------------------------------------

def test_a_password_field_is_listed_but_its_contents_never_are():
    """The field has to be nameable — a generated script types into it.

    What must not reach the document is what was in it.
    """
    controls = normalize_controls([
        _control("W.password", "hunter2", is_password=True),
    ])
    assert controls[0]["automationId"] == "W.password"
    assert controls[0]["protected"] is True
    assert "text" not in controls[0]
    assert "hunter2" not in str(controls)


def test_content_identity_ignores_geometry_but_not_text():
    a = normalize_controls([_control("W.a", "确定")])
    b = normalize_controls([_control("W.a", "取消")])
    moved = _control("W.a", "确定")
    moved["rectangle"] = {"x": 99, "y": 99, "w": 1, "h": 1}
    c = normalize_controls([moved])
    assert content_hash(a) == content_hash(c)
    assert content_hash(a) != content_hash(b)


# --- the document ---------------------------------------------------------

def _recording(**kwargs) -> RawRecording:
    return RawRecording(
        "s", "n", (),
        {"droppedPackets": 0, "correlationErrorCount": 0},
        **kwargs,
    )


def test_material_and_rejections_survive_a_document_round_trip():
    snapshot = {
        "snapshotId": "CS-0001",
        "capturedAtMs": 10,
        "reason": "window_changed",
        "durationMs": 430,
        "window": {"handle": 7, "title": "Log Center", "processName": "EDRClient.exe",
                   "rootAutomationId": "LogCenterWindow"},
        "contentHash": "sha256:abc",
        "controlCount": 1,
        "controls": [{"automationId": "LogCenterWindow.table", "controlType": "Table"}],
    }
    rejection = {"reason": "out_of_scope", "kind": "pointer_up",
                 "resolvedProcess": "EDRClient.exe"}
    document = _recording(
        control_snapshots=(snapshot,), rejections=(rejection,),
    ).to_dict()
    restored = RawRecording.from_dict(document)
    assert restored.control_snapshots[0]["window"]["rootAutomationId"] == "LogCenterWindow"
    assert restored.rejections[0]["reason"] == "out_of_scope"


def test_duplicate_snapshot_ids_are_rejected():
    snapshot = {
        "snapshotId": "CS-0001", "capturedAtMs": 1, "window": {},
        "contentHash": "sha256:a", "controls": [],
    }
    with pytest.raises(RecordingModelError, match="unique"):
        _recording(control_snapshots=(snapshot, dict(snapshot)))


# --- wiring ---------------------------------------------------------------

class _RecordingInventory:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    def request(self, **kwargs) -> None:
        self.requests.append(kwargs)


def test_registering_a_window_asks_for_its_material_at_once():
    resolver = _Resolver(
        {"hisecendpointagent": r"C:\Program Files\HiSec\agent.exe"},
        r"C:\Program Files\HiSec",
    )
    inventory = _RecordingInventory()
    correlator = WindowsUIACorrelator(resolver, inventory=inventory)
    correlator._register_window(
        title="Log Center", process_name="EDRClient.exe", handle=7,
        pid=42, origin="opened",
    )
    assert inventory.requests == [{
        "handle": 7, "title": "Log Center", "process_name": "EDRClient.exe",
        "monotonic_ms": None, "reason": "window_changed",
    }]


def test_a_window_already_registered_is_not_re_requested():
    inventory = _RecordingInventory()
    correlator = WindowsUIACorrelator(_Resolver({}, "root"), inventory=inventory)
    for _ in range(3):
        correlator._register_window(
            title="logo1", process_name="agent.exe", handle=7,
            pid=1, origin="already_open",
        )
    assert len(inventory.requests) == 1


def test_a_broken_inventory_never_costs_the_step_that_was_recorded():
    class _Broken:
        def request(self, **kwargs):
            raise RuntimeError("walk queue exploded")

    correlator = WindowsUIACorrelator(_Resolver({}, "root"), inventory=_Broken())
    correlator._register_window(
        title="logo1", process_name="agent.exe", handle=7, pid=1,
        origin="already_open",
    )
    assert [entry["title"] for entry in correlator.window_registry] == ["logo1"]


# --- the indicator must not be able to abort the server -------------------

@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_the_indicator_runs_out_of_process_on_every_tk_platform(monkeypatch, platform):
    """A Tk panic killed the MCP server, not just the indicator.

    Windows drove Tk from a worker thread; the third recording of one server
    session aborted python.exe inside tcl86t.dll (0x80000003, a Tcl panic).
    Both Tk platforms now get a child process, so a panic can only kill the
    child.
    """
    import target.recording.indicator as indicator_module

    monkeypatch.setattr(indicator_module.sys, "platform", platform)
    made = indicator_module.tkinter_indicator_factory(
        "case", SCOPE, indicator_module.IndicatorCallbacks(
            pause=lambda: None, resume=lambda: None, stop=lambda: None,
            add_assertion=lambda target: None,
        ),
    )
    assert isinstance(made, indicator_module.SubprocessTkIndicator)


def test_the_indicator_child_import_follows_the_deployed_layout():
    """The target deploys this package flattened.

    A child spawned with a hard-coded `target.recording.indicator` import
    cannot start there, which is what kept Windows on the in-process Tk that
    aborted the server.
    """
    import inspect
    import target.recording.indicator as indicator_module

    body = inspect.getsource(indicator_module.SubprocessTkIndicator.start)
    assert "from {__name__} import" in body
    assert "from target.recording.indicator import" not in body
