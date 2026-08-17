from __future__ import annotations

import base64
import io
import sys
import types

import pytest
from PIL import Image

from target.recording.macos import MacOSAXCorrelator, MacOSAXResolver
from target.automation.macos_accessibility import MacOSAccessibilityBackend
from target.recording.models import CaptureScope
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
    resolved = MacOSAXResolver(_AXBackend()).element_at(50, 50)
    assert resolved["identifier"] == "btnApply"
    assert resolved["controlType"] == "AXButton"
    assert resolved["rect"] == [20, 30, 120, 70]


def test_macos_correlator_uses_same_scope_and_semantic_event_contract_as_windows():
    backend = _AXBackend()
    scope = CaptureScope("mac-dev", "macos_accessibility", "EDRClient", "^EDRClient$")
    event = MacOSAXCorrelator(
        MacOSAXResolver(backend), double_click_ms=700,
    ).correlate(_packet(), scope, 1)
    assert event.type == "pointer_click"
    assert event.scope == scope
    assert event.observed_target.identifier == "btnApply"
    assert event.evidence == {
        "foregroundPid": 7, "doubleClickIntervalMs": 700,
    }


def test_macos_correlator_drops_other_frontmost_application_before_ax_hit_test():
    backend = _AXBackend(process="OtherApp")
    scope = CaptureScope("mac-dev", "macos_accessibility", "EDRClient", "^EDRClient$")
    event = MacOSAXCorrelator(MacOSAXResolver(backend)).correlate(_packet(), scope, 1)
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

    event = MacOSAXCorrelator(MacOSAXResolver(backend)).correlate(_packet(), scope, 1)

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
    correlator = MacOSAXCorrelator(MacOSAXResolver(backend))

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
