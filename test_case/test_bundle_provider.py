"""Bundle ingestion checks image integrity and never persists OCR full text."""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

from agent.execution import BackendUnavailable
from agent.recording.mcp_runtime import MCPObservationProvider
from agent.trace.store import TraceStore


PNG = b"\x89PNG\r\n\x1a\nsynthetic"


class Agent:
    def __init__(self, digest=None):
        self.digest = digest or "sha256:" + hashlib.sha256(PNG).hexdigest()

    def call_tool(self, name, args, timeout=None):
        assert name == "observe_window_bundle"
        return {"ok": True, "observation_id": "OBS-BUNDLE-1",
                "window_identity": {"native_id": "4", "generation": 1,
                                    "process_name": "Demo", "title": "Main",
                                    "rect": [10, 20, 110, 100]},
                "tree": {"ok": True, "controls": [{"text": "hidden-secret",
                                                  "protected": True,
                                                  "rectangle": [11, 22, 20, 30]}]},
                "screenshot_scope": "window", "screenshot_redacted": True,
                "screenshot_b64": base64.b64encode(PNG).decode(),
                "screenshot_sha256": self.digest, "screenshot_origin": [10, 20],
                "ocr_boxes": [{"text": "ocr-secret", "rect": [11, 22, 20, 30]}],
                "ocr_status": "ok"}


def test_bundle_provider_redacts_trace_ocr_and_protected_control(tmp_path):
    trace = TraceStore(tmp_path)
    trace.open()
    provider = MCPObservationProvider(Agent(), use_bundle=True,
                                      capture_screenshot=True, include_ocr=True,
                                      trace_store=trace)
    snapshot = provider.get_snapshot(provider.refresh())
    assert snapshot["controls"][0]["text"] is None
    assert snapshot["ocr_boxes"][0]["text"] == "ocr-secret"
    serialized = json.dumps([event.to_dict() for event in trace.read_events()])
    assert "ocr-secret" not in serialized
    assert "hidden-secret" not in serialized


def test_bundle_provider_rejects_corrupt_digest():
    provider = MCPObservationProvider(Agent("sha256:bad"), use_bundle=True,
                                      capture_screenshot=True)
    with pytest.raises(BackendUnavailable, match="digest mismatch"):
        provider.refresh()


def test_bundle_provider_rejects_malformed_control_entries():
    class MalformedAgent(Agent):
        def call_tool(self, name, args, timeout=None):
            bundle = super().call_tool(name, args, timeout=timeout)
            bundle["tree"]["controls"].append(None)
            return bundle

    with pytest.raises(BackendUnavailable, match="invalid control entry"):
        MCPObservationProvider(MalformedAgent(), use_bundle=True).refresh()


@pytest.mark.parametrize("origin", [None, [10], [10.0, 20], [400, 400]])
def test_bundle_provider_rejects_invalid_or_misaligned_screenshot_origin(origin):
    class WrongOriginAgent(Agent):
        def call_tool(self, name, args, timeout=None):
            bundle = super().call_tool(name, args, timeout=timeout)
            bundle["screenshot_origin"] = origin
            return bundle

    with pytest.raises(BackendUnavailable, match="screenshot origin"):
        MCPObservationProvider(WrongOriginAgent(), use_bundle=True,
                               capture_screenshot=True).refresh()


def test_bundle_provider_allows_small_window_frame_offset():
    class FrameOffsetAgent(Agent):
        def call_tool(self, name, args, timeout=None):
            bundle = super().call_tool(name, args, timeout=timeout)
            bundle["screenshot_origin"] = [2, 12]
            return bundle

    provider = MCPObservationProvider(FrameOffsetAgent(), use_bundle=True,
                                      capture_screenshot=True)
    snapshot = provider.get_snapshot(provider.refresh())
    assert snapshot["screenshot_origin"] == [2, 12]


def test_recorded_root_focus_uses_exact_product_window_selector():
    calls = []

    class RootAgent:
        def call_tool(self, name, args, timeout=None):
            calls.append((name, args))
            if name == "focus_window_selector":
                return {"ok": True, "identity": {"native_id": "202",
                                                  "root_control_id": "LogRoot"}}
            if name == "observe_window_bundle":
                return {"ok": True, "observation_id": "OBS-ROOT",
                        "window_identity": {"native_id": "202", "generation": 1,
                                            "root_control_id": "LogRoot",
                                            "process_name": "EDRClient.exe", "title": "logo1",
                                            "rect": [10, 20, 110, 100]},
                        "tree": {"ok": True, "controls": []}}
            raise AssertionError(f"unexpected tool {name}")

    provider = MCPObservationProvider(RootAgent(), use_bundle=True, run_token="TOKEN")
    snapshot = provider.get_snapshot(provider.focus_window("EDRClient.exe", "^logo1$", "LogRoot"))

    assert snapshot["window_identity"]["native_id"] == "202"
    assert [name for name, _ in calls] == ["focus_window_selector", "observe_window_bundle"]
    assert calls[0][1]["root_control_id"] == "LogRoot"


def test_root_focus_rejects_bundle_identity_that_disagrees_with_control_prefix():
    class WrongRootAgent:
        def call_tool(self, name, args, timeout=None):
            if name == "focus_window_selector":
                return {"ok": True}
            if name == "observe_window_bundle":
                return {"ok": True, "observation_id": "OBS-ROOT",
                        "window_identity": {"native_id": "101", "generation": 1,
                                            "root_control_id": "WrongRoot",
                                            "process_name": "EDRClient.exe", "title": "logo1",
                                            "rect": [10, 20, 110, 100]},
                        "tree": {"ok": True, "controls": [
                            {"automation_id": "LogRoot.logTime", "text": "10:10:59"},
                        ]}}
            raise AssertionError(name)

    provider = MCPObservationProvider(WrongRootAgent(), use_bundle=True, run_token="TOKEN")
    with pytest.raises(BackendUnavailable, match="root identity changed"):
        provider.focus_window("EDRClient.exe", "^logo1$", "LogRoot")
