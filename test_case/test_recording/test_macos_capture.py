from __future__ import annotations

import base64
import io
import sys
import types

import pytest
from PIL import Image

from target.recording.macos import MacOSAXCorrelator, MacOSAXResolver
from target.automation.macos_accessibility import MacOSAccessibilityBackend
from target.recording.models import CaptureScope, RecordingModelError
from target.recording.source import HookPacket

pytestmark = pytest.mark.unit


class _AXBackend:
    def __init__(self, process="EDRClient", title="EDRClient"):
        self.process = process
        self.title = title
        self.controls = [
            {
                "role": "AXGroup", "text": "container",
                "rectangle": {"x": 0, "y": 0, "w": 500, "h": 500},
            },
            {
                "role": "AXButton", "identifier": "btnApply", "text": "应用",
                "rectangle": {"x": 20, "y": 30, "w": 100, "h": 40},
                "is_password": False,
            },
        ]

    def get_window_lock(self):
        return {"ok": True, "lock": {
            "process_name": self.process,
            "snapshot": {"process_name": self.process, "title": self.title, "pid": 7},
        }}

    def dump_tree(self, max_depth=12):
        return {"ok": True, "controls": self.controls}


def _packet():
    return HookPacket(
        "pointer_up", 100, "2026-08-17T00:00:00.000Z",
        screen_point=(50, 50), button="left",
    )


def test_macos_ax_resolver_chooses_smallest_containing_control():
    resolved = MacOSAXResolver(_AXBackend(), native_ax=None, native_windows=None).element_at(50, 50)
    assert resolved["identifier"] == "btnApply"
    assert resolved["controlType"] == "AXButton"
    assert resolved["rect"] == [20, 30, 120, 70]


def test_macos_correlator_uses_same_scope_and_semantic_event_contract_as_windows():
    backend = _AXBackend()
    scope = CaptureScope("mac-dev", "macos_accessibility", "EDRClient", "^EDRClient$")
    event = MacOSAXCorrelator(
        MacOSAXResolver(backend, native_ax=None, native_windows=None), double_click_ms=700,
    ).correlate(_packet(), scope, 1)
    assert event.type == "pointer_click"
    assert event.scope == scope
    assert event.observed_target.identifier == "btnApply"
    assert event.evidence == {
        "foregroundPid": 7, "doubleClickIntervalMs": 700,
        "window": {"title": "EDRClient", "processName": "EDRClient"},
    }


def test_macos_correlator_drops_other_frontmost_application_before_ax_hit_test():
    backend = _AXBackend(process="OtherApp")
    scope = CaptureScope("mac-dev", "macos_accessibility", "EDRClient", "^EDRClient$")
    event = MacOSAXCorrelator(MacOSAXResolver(backend, native_ax=None, native_windows=None)).correlate(_packet(), scope, 1)
    assert event is None


def test_macos_ax_parser_source_redacts_buggy_secure_value():
    backend = MacOSAccessibilityBackend.__new__(MacOSAccessibilityBackend)
    line = "\t".join([
        "1", "1", "EDRClient", "AXSecureTextField", "", "密码", "",
        "must-not-escape", "password", "true", "10", "20", "100", "30",
        "true", "false", "w1.1",
    ])

    control = backend._parse_ax_tree_lines(line)[0]

    assert control["protected"] is True
    assert control["value"] == ""
    assert "must-not-escape" not in str(control)
    assert control["focused"] is True


def test_macos_ax_parser_normalizes_checkbox_and_selection_state():
    backend = MacOSAccessibilityBackend.__new__(MacOSAccessibilityBackend)
    line = "\t".join([
        "2", "1", "EDRClient", "AXCheckBox", "", "启用备份", "",
        "1", "backupEnabled", "true", "10", "20", "100", "30",
        "true", "true", "w1.2",
    ])

    control = backend._parse_ax_tree_lines(line)[0]

    assert control["checked"] is True
    assert control["selected"] is True


def test_macos_pointer_toggle_uses_normalized_ax_control_type():
    backend = _AXBackend()
    backend.controls = [{
        "role": "AXCheckBox", "identifier": "backupEnabled", "text": "启用备份",
        "checked": True, "is_password": False,
        "rectangle": {"x": 20, "y": 30, "w": 100, "h": 40},
    }]
    scope = CaptureScope("mac-dev", "macos_accessibility", "EDRClient", "^EDRClient$")

    event = MacOSAXCorrelator(MacOSAXResolver(backend, native_ax=None, native_windows=None)).correlate(_packet(), scope, 1)

    assert event.type == "toggle_change"
    assert event.input == {"value": True}


def test_macos_focused_ax_text_control_drives_source_redacted_text_activity():
    backend = _AXBackend()
    backend.controls.append({
        "role": "AXTextField", "identifier": "username", "text": "用户名",
        "value": "alice", "focused": True, "is_password": False,
        "rectangle": {"x": 20, "y": 100, "w": 200, "h": 40},
    })
    scope = CaptureScope("mac-dev", "macos_accessibility", "EDRClient", "^EDRClient$")
    correlator = MacOSAXCorrelator(MacOSAXResolver(backend, native_ax=None, native_windows=None))

    assert correlator.correlate(HookPacket(
        "text_activity", 100, "2026-08-17T00:00:00.000Z",
    ), scope, 1) is None
    committed = correlator.flush(scope, 1)

    assert committed.type == "text_commit"
    assert committed.input == {"value": "alice"}
    assert committed.observed_target.identifier == "username"


def test_macos_screenshot_without_path_returns_in_memory_png(monkeypatch):
    output = io.BytesIO()
    Image.new("RGB", (40, 30), "green").save(output, format="PNG")
    png = output.getvalue()

    class _Representation:
        @classmethod
        def alloc(cls):
            return cls()

        def initWithCGImage_(self, _image):
            return self

        def representationUsingType_properties_(self, _kind, _properties):
            return png

    appkit = types.SimpleNamespace(
        NSBitmapImageRep=_Representation,
        NSBitmapImageFileTypePNG=4,
    )
    quartz = types.SimpleNamespace(
        CGRectInfinite=object(),
        kCGWindowListOptionOnScreenOnly=1,
        kCGNullWindowID=0,
        kCGWindowImageDefault=0,
        CGWindowListCreateImage=lambda *_args: object(),
        CGImageGetWidth=lambda _image: 40,
        CGImageGetHeight=lambda _image: 30,
    )
    monkeypatch.setitem(sys.modules, "AppKit", appkit)
    monkeypatch.setitem(sys.modules, "Quartz", quartz)
    backend = MacOSAccessibilityBackend.__new__(MacOSAccessibilityBackend)

    result = backend.screenshot(None)

    assert result["ok"] is True
    assert base64.b64decode(result["image_b64"]) == png
    assert result["origin"] == [0, 0]
    assert "path" not in result


class _FakeAXElement:
    """Stand-in AXUIElement: a plain attribute bag with a parent link."""

    def __init__(self, attributes, parent=None):
        self.attributes = attributes
        self.parent = parent


class _FakeAXModule:
    """Minimal stand-in for the ApplicationServices AX entry points."""

    kAXValueCGPointType = 1
    kAXValueCGSizeType = 2

    def __init__(self, element_at_point=None, focused=None):
        self._element_at_point = element_at_point
        self._focused = focused
        self.position_calls = 0

    def AXUIElementCreateSystemWide(self):
        return "system-wide"

    def AXUIElementCopyElementAtPosition(self, element, x, y, _):
        del element, x, y
        if self._element_at_point is None:
            return 1, None
        return 0, self._element_at_point

    def AXUIElementCopyAttributeValue(self, element, name, _):
        if element == "system-wide":
            if name == "AXFocusedUIElement" and self._focused is not None:
                return 0, self._focused
            return 1, None
        if name == "AXParent":
            return (0, element.parent) if element.parent is not None else (1, None)
        if name in element.attributes:
            if name == "AXPosition":
                self.position_calls += 1
            return 0, element.attributes[name]
        return 1, None

    def AXValueGetValue(self, value, kind, _):
        if kind == self.kAXValueCGPointType:
            return True, types.SimpleNamespace(x=value[0], y=value[1])
        return True, types.SimpleNamespace(width=value[0], height=value[1])


def _fake_button(**overrides):
    attributes = {
        "AXRole": "AXButton",
        "AXIdentifier": "btnApply",
        "AXTitle": "应用",
        "AXPosition": (20, 30),
        "AXSize": (100, 40),
        "AXEnabled": True,
    }
    attributes.update(overrides)
    window = _FakeAXElement({"AXRole": "AXWindow"})
    return _FakeAXElement(attributes, parent=window)


def test_macos_native_hit_test_replaces_the_tree_walk():
    """The per-event hot path must not enumerate the whole AX tree.

    A full dump_tree costs 10-20 s on a live desktop, which loses nearly
    every captured event, so element_at resolves through the single-point AX
    API and must not touch the backend at all when it succeeds.
    """
    class _ExplodingBackend(_AXBackend):
        def dump_tree(self, max_depth=12):
            raise AssertionError("element_at must not walk the tree natively")

    resolver = MacOSAXResolver(
        _ExplodingBackend(), native_ax=_FakeAXModule(element_at_point=_fake_button()),
    )

    resolved = resolver.element_at(50, 50)

    assert resolved["identifier"] == "btnApply"
    assert resolved["controlType"] == "AXButton"
    assert resolved["rect"] == [20, 30, 120, 70]
    assert resolved["ancestry"] == ["AXWindow"]


def test_macos_native_refresh_rereads_the_cached_handle():
    element = _fake_button()
    module = _FakeAXModule(element_at_point=element)
    resolver = MacOSAXResolver(_AXBackend(), native_ax=module)
    resolved = resolver.element_at(50, 50)
    before = module.position_calls

    refreshed = resolver.refresh(resolved)

    assert refreshed["_identity"] == resolved["_identity"]
    # The handle is re-read rather than re-discovered.
    assert module.position_calls > before


def test_macos_native_hit_test_never_reads_a_secure_field_value():
    element = _fake_button(
        AXRole="AXSecureTextField", AXValue="must-not-escape", AXTitle="密码",
    )
    resolver = MacOSAXResolver(
        _AXBackend(), native_ax=_FakeAXModule(element_at_point=element),
    )

    resolved = resolver.element_at(50, 50)

    assert resolved["protected"] is True
    assert resolved["value"] is None
    assert "must-not-escape" not in str(resolved)


def test_macos_falls_back_to_the_tree_walk_when_native_ax_is_missing():
    resolver = MacOSAXResolver(_AXBackend(), native_ax=None, native_windows=None)

    resolved = resolver.element_at(50, 50)

    assert resolved["identifier"] == "btnApply"


class _WindowListBackend(_AXBackend):
    def __init__(self, windows=None):
        super().__init__()
        self._windows = windows

    def list_windows(self):
        if self._windows is None:
            return {"ok": False, "error": "enumeration refused"}
        return {"ok": True, "windows": self._windows, "count": len(self._windows)}


def test_macos_resolver_enumerates_windows_for_scope_seeding():
    """Seeding was skipped entirely on macOS for want of this method.

    Without it seed_scope() reports `resolver cannot enumerate windows` and
    returns nothing, so an application window that was already open when
    recording began is never admitted and every click inside it is dropped.
    """
    backend = _WindowListBackend([
        {
            "app_name": "TextEdit", "title": "notes.txt", "window_title": "notes.txt",
            "pid": 42, "process_id": 42, "handle": 1731,
        },
        {"app_name": "", "title": "orphan", "pid": 7},
    ])

    windows = MacOSAXResolver(backend, native_ax=None, native_windows=None).windows()

    assert windows == [{
        "title": "notes.txt", "pid": 42, "processName": "TextEdit", "handle": 1731,
    }]


def test_macos_scope_seeding_surfaces_a_failed_enumeration():
    backend = _WindowListBackend(None)

    with pytest.raises(RecordingModelError) as excinfo:
        MacOSAXResolver(backend, native_ax=None, native_windows=None).windows()

    assert excinfo.value.code == "recording_scope_seed_failed"


def _cg_window(owner_pid, name, number):
    return {"kCGWindowOwnerPID": owner_pid, "kCGWindowName": name, "kCGWindowNumber": number}


def test_foreground_reports_the_handle_matching_pid_and_title():
    backend = _AXBackend()  # pid=7, title="EDRClient"
    resolver = MacOSAXResolver(
        backend, native_ax=None,
        native_windows=[
            _cg_window(99, "unrelated", 1),
            _cg_window(7, "EDRClient", 2735),
        ],
    )

    foreground = resolver.foreground()

    assert foreground["handle"] == 2735


def test_foreground_omits_handle_rather_than_guessing_when_pid_has_no_match():
    """A stale pid must not silently borrow an unrelated window's handle.

    Before this was fixed, a pid/title that matched nothing fell back to the
    unfiltered CGWindowList snapshot and returned whatever window happened to
    come first — a real window on the machine running the test, in a
    recording session that had nothing to do with it.
    """
    backend = _AXBackend()  # pid=7
    resolver = MacOSAXResolver(
        backend, native_ax=None,
        native_windows=[_cg_window(99, "unrelated", 1), _cg_window(100, "also unrelated", 2)],
    )

    foreground = resolver.foreground()

    assert "handle" not in foreground


def test_foreground_omits_handle_when_cgwindowlist_is_unavailable():
    resolver = MacOSAXResolver(_AXBackend(), native_ax=None, native_windows=None)

    foreground = resolver.foreground()

    assert "handle" not in foreground


def test_foreground_disambiguates_same_pid_multiple_windows_by_title():
    backend = _AXBackend()  # pid=7, title="EDRClient"
    resolver = MacOSAXResolver(
        backend, native_ax=None,
        native_windows=[
            _cg_window(7, "some other window", 1),
            _cg_window(7, "EDRClient", 2735),
        ],
    )

    foreground = resolver.foreground()

    assert foreground["handle"] == 2735


def test_seeding_reports_the_executable_name_not_the_application_name():
    """CGWindowList's owner name is not the process the scope matches on.

    HiSec's client windows report an owner of "HiSecEndpoint" while the
    process that owns them is "EDRClient". Capture-time scope checks use the
    executable name, so seeding on the listing's app_name refused to admit
    exactly the cross-process windows seeding exists for.
    """
    backend = _WindowListBackend([
        {
            "app_name": "HiSecEndpoint", "title": "日志中心",
            "window_title": "日志中心", "pid": 41916, "process_id": 41916,
            "handle": 2874,
        },
    ])
    resolver = MacOSAXResolver(backend, native_ax=None, native_windows=None)
    # Stand in for psutil: the pid resolves to the real executable name.
    resolver._process_name_for = staticmethod(
        lambda pid: "EDRClient" if int(pid) == 41916 else ""
    )

    windows = resolver.windows()

    assert windows == [{
        "title": "日志中心", "pid": 41916,
        "processName": "EDRClient", "handle": 2874,
    }]

    # A pid that can no longer be resolved falls back to the listing's own
    # name rather than dropping the window.
    resolver._process_name_for = staticmethod(lambda pid: "")
    assert resolver.windows()[0]["processName"] == "HiSecEndpoint"


def _cg_layer_window(pid, owner, name, number, layer=0, w=800, h=600):
    return {
        "kCGWindowOwnerPID": pid, "kCGWindowOwnerName": owner,
        "kCGWindowName": name, "kCGWindowNumber": number,
        "kCGWindowLayer": layer,
        "kCGWindowBounds": {"X": 0, "Y": 0, "Width": w, "Height": h},
    }


def test_foreground_reports_the_real_front_window_not_the_lock():
    """This used to hand the window lock straight back.

    Every event was then stamped with the locked window wherever the user
    actually clicked, `_in_scope` compared the lock against the scope it came
    from and so could never fail, and a flow crossing into another of the
    application's windows looked like it never left the first. A live capture
    stamped a click at (1846, 877) — outside the locked window entirely — as
    belonging to it.
    """
    backend = _AXBackend()  # lock says HiSecEndpointAgent / "EDRClient"
    resolver = MacOSAXResolver(
        backend, native_ax=None,
        native_windows=[_cg_layer_window(41916, "HiSecEndpoint", "日志中心", 2874)],
    )
    resolver._process_name_for = staticmethod(lambda pid: "EDRClient")

    foreground = resolver.foreground()

    assert foreground["processName"] == "EDRClient"
    assert foreground["windowTitle"] == "日志中心"
    assert foreground["handle"] == 2874


def test_foreground_ignores_menu_bar_and_dock_layers():
    """Chrome layers sit permanently in front of every application window.

    Searching the stacking order without filtering by layer returned a menu
    bar extra ("ControlCenter / Item-0") as the foreground window every time.
    """
    resolver = MacOSAXResolver(
        _AXBackend(), native_ax=None,
        native_windows=[
            _cg_layer_window(1632, "ControlCenter", "Item-0", 1, layer=25, w=42, h=30),
            _cg_layer_window(1631, "Dock", "Dock", 2, layer=20, w=1920, h=1080),
            _cg_layer_window(41916, "HiSecEndpoint", "日志中心", 2874, layer=0),
        ],
    )
    resolver._process_name_for = staticmethod(lambda pid: "EDRClient")

    assert resolver.foreground()["windowTitle"] == "日志中心"


def test_the_transition_driver_sees_windows_of_the_whole_application():
    """A new window opened by another process of the app must be visible.

    Windows already open at start are covered by seeding; windows opened
    *during* a recording are only noticed here. This filtered on the scope's
    single process name and on kCGWindowOwnerName, so a security-centre
    window HiSec opened mid-capture was never reported, the scope never grew,
    and every click inside it was dropped.
    """
    from target.recording.macos import MacOSWindowEventDriver

    driver = MacOSWindowEventDriver(
        "HiSecEndpointAgent",
        process_names=lambda: ["HiSecEndpointAgent", "EDRClient"],
    )
    driver._name_for_pid = lambda pid: {1981: "HiSecEndpointAgent", 41916: "EDRClient"}.get(pid, "")
    admitted = driver._admitted()

    assert "edrclient" in admitted
    assert "hisecendpointagent" in admitted


def test_a_failing_application_lookup_leaves_the_driver_on_the_scope_process():
    from target.recording.macos import MacOSWindowEventDriver

    def _boom():
        raise RuntimeError("bundle lookup unavailable")

    driver = MacOSWindowEventDriver("HiSecEndpointAgent", process_names=_boom)

    # Never widen, never crash the polling thread.
    assert driver._admitted() == {"hisecendpointagent"}


def test_window_at_returns_the_window_under_the_pointer():
    """Pointer input carries its own answer about which window it belongs to.

    Attributing a click to whatever was *frontmost* dropped 671 events in one
    live session: the user clicked inside the target's log window while
    another application was stacked in front, and every one of those clicks
    was judged out of scope.
    """
    resolver = MacOSAXResolver(
        _AXBackend(), native_ax=None,
        native_windows=[
            _cg_layer_window(54459, "Claude", "Claude", 1731, w=1920, h=970),
            _cg_layer_window(41916, "HiSecEndpoint", "日志中心", 2874, w=880, h=560),
        ],
    )
    resolver._process_name_for = staticmethod(
        lambda pid: {54459: "Claude", 41916: "EDRClient"}.get(int(pid), "")
    )

    # Both rects contain the point; the one earlier in the stacking order wins.
    assert resolver.window_at(100, 100)["processName"] == "Claude"

    # And when the target is the one on top, the click belongs to it.
    resolver._cg_window_source = [
        _cg_layer_window(41916, "HiSecEndpoint", "日志中心", 2874, w=880, h=560),
        _cg_layer_window(54459, "Claude", "Claude", 1731, w=1920, h=970),
    ]
    located = resolver.window_at(100, 100)
    assert located["processName"] == "EDRClient"
    assert located["handle"] == 2874


def test_window_at_never_returns_the_recorders_own_window():
    from target.recording.indicator import RECORDER_UI_MARKER

    resolver = MacOSAXResolver(
        _AXBackend(), native_ax=None,
        native_windows=[
            _cg_layer_window(999, "Python", f"EDR-WD Recorder {RECORDER_UI_MARKER}", 1, w=328, h=136),
            _cg_layer_window(41916, "HiSecEndpoint", "日志中心", 2874, w=880, h=560),
        ],
    )
    resolver._process_name_for = staticmethod(lambda pid: "EDRClient")

    assert resolver.window_at(10, 10)["windowTitle"] == "日志中心"


def _hook_packet(kind="pointer_up", point=(100, 100)):
    from target.recording.source import HookPacket

    return HookPacket(kind, 100, "2026-08-25T00:00:00Z", screen_point=point)


def test_the_gate_refuses_pointer_input_outside_the_application():
    """Foreign input must not reach the queue at all.

    Everything used to be queued and correlated before being discarded: one
    session processed 301 foreign packets against 2 real ones, and the
    backlog delayed a real click reaching the recording by ten seconds.
    """
    from target.recording.macos import CaptureScopeGate

    gate = CaptureScopeGate()
    gate.publish(
        ({"processName": "EDRClient", "windowTitle": "日志中心", "handle": 2874,
          "rect": (500.0, 200.0, 400.0, 300.0)},),
        app_in_front=True,
    )

    admitted, window = gate.resolve(_hook_packet(point=(600, 300)))
    assert admitted is True
    # The window is decided here, while the input is happening.
    assert window["windowTitle"] == "日志中心"

    assert gate.admits(_hook_packet(point=(50, 50))) is False
    assert gate.refused == 1


def test_the_gate_judges_keyboard_input_by_whether_the_app_is_in_front():
    """Keystrokes carry no coordinate, so focus is the only signal."""
    from target.recording.macos import CaptureScopeGate

    gate = CaptureScopeGate()
    typing = _hook_packet(kind="text_activity", point=None)

    windows = ({"processName": "EDRClient", "windowTitle": "日志中心",
                "rect": (0.0, 0.0, 100.0, 100.0)},)
    gate.publish(windows, app_in_front=True, front=windows[0])
    assert gate.admits(typing) is True

    gate.publish(windows, app_in_front=False)
    assert gate.admits(typing) is False


def test_the_gate_fails_open_before_it_knows_anything():
    """A slow first snapshot must not cost real steps."""
    from target.recording.macos import CaptureScopeGate

    gate = CaptureScopeGate()

    assert gate.admits(_hook_packet(point=(9999, 9999))) is True
    assert gate.refused == 0


def test_the_window_decided_at_capture_time_survives_to_correlation():
    """Correlation must not re-derive a window the tap already resolved.

    Deciding it twice means deciding it against two different stacking
    orders: one live capture admitted 109 packets at the tap and judged them
    to belong to another application milliseconds later, losing all of them.
    """
    from target.recording.macos import CaptureScopeGate
    from target.recording.windows import WindowsUIACorrelator

    gate = CaptureScopeGate()
    window = {
        "processName": "EDRClient", "windowTitle": "日志中心",
        "handle": 2874, "rect": (500.0, 200.0, 400.0, 300.0),
    }
    gate.publish((window,), app_in_front=True)
    _, resolved = gate.resolve(_hook_packet(point=(600, 300)))

    from target.recording.source import HookPacket

    packet = HookPacket(
        "pointer_up", 100, "2026-08-25T00:00:00Z",
        screen_point=(600, 300), native={"window": dict(resolved)},
    )

    class _MovedOnResolver:
        """Stands for a stacking order that changed after the click."""

        def foreground(self):
            return {"processName": "Claude", "windowTitle": "Claude"}

        def window_at(self, x, y):
            return {"processName": "Claude", "windowTitle": "Claude"}

    correlator = WindowsUIACorrelator(_MovedOnResolver())

    assert correlator._window_for(packet)["windowTitle"] == "日志中心"
