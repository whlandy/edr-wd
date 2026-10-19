from __future__ import annotations

import base64
import io
import json
from dataclasses import replace

import pytest
import jsonschema
from PIL import Image, ImageDraw

from agent.recording.artifacts import write_compilation_artifacts
from agent.recording.compiler import compile_recording
from agent.recording.pillow_matcher import PillowTemplateMatcher
from agent.recording.replay import GoldenStepMaterializer, golden_to_test_case, load_golden_trace
from agent.recording.visual import SafeVisualResolver
from target.recording.evidence import (
    RecordingEvidenceStore,
    capture_redacted_window_frame,
)
from target.recording.models import (
    CaptureScope,
    ObservedTarget,
    RawCaptureEvent,
    RawRecording,
)


pytestmark = pytest.mark.unit


class _ScreenshotBackend:
    def __init__(self) -> None:
        image = Image.new("RGB", (300, 200), "white")
        painter = ImageDraw.Draw(image)
        painter.rectangle((20, 30, 80, 70), fill="blue")
        painter.rectangle((150, 50, 190, 80), fill="red")
        painter.rectangle((200, 100, 250, 140), fill="yellow")
        output = io.BytesIO()
        image.save(output, format="PNG")
        self.payload = output.getvalue()

    def screenshot(self, path=None):
        assert path is None
        return {
            "ok": True,
            "image_b64": base64.b64encode(self.payload).decode("ascii"),
            "origin": [100, 200],
            "width": 300,
            "height": 200,
            "capture_scope": "window",
        }

    def dump_tree(self, max_depth=15):
        assert max_depth == 15
        return {"ok": True, "controls": [{
            "control_type": "Edit",
            "is_password": True,
            "rectangle": {"x": 250, "y": 250, "w": 40, "h": 30},
        }]}

    def list_windows(self):
        return {"ok": True, "windows": [{
            "title": "EDR-WD Recorder [recorder_ui=true]",
            "rectangle": {"x": 300, "y": 300, "w": 50, "h": 40},
        }]}


class _FullScreenBackend(_ScreenshotBackend):
    def screenshot(self, path=None):
        result = super().screenshot(path)
        result["capture_scope"] = "screen"
        return result


class _FailedTreeBackend(_ScreenshotBackend):
    def __init__(self) -> None:
        super().__init__()
        self.screenshot_calls = 0

    def dump_tree(self, max_depth=15):
        return {"ok": False, "error": "accessibility unavailable"}

    def screenshot(self, path=None):
        self.screenshot_calls += 1
        return super().screenshot(path)


class _UnboundedSecretBackend(_FailedTreeBackend):
    def dump_tree(self, max_depth=15):
        return {"ok": True, "controls": [{
            "control_type": "Edit",
            "is_password": True,
        }]}


class _MissingOriginBackend(_ScreenshotBackend):
    def screenshot(self, path=None):
        result = super().screenshot(path)
        result.pop("origin")
        return result


def _event(*, protected=False):
    scope = CaptureScope("win-dev", "windows_pywinauto", "EDRClient.exe", "^EDRClient$")
    target = ObservedTarget(
        "OBS-1", "T0001", "sha256:button", "Button", "btnApply",
        text="应用", rect=(120, 230, 180, 270), protected=protected,
    )
    return RawCaptureEvent(
        1, "2026-08-17T00:00:00Z", 10, "pointer_click", scope,
        input={"screenPoint": [140, 250]}, observed_target=target,
    )


def test_source_redacts_before_capture_transfer_and_json_contains_only_metadata():
    store = RecordingEvidenceStore(_ScreenshotBackend())

    attached = store.attach(_event())
    metadata = attached.evidence["capture"]
    fetched = store.get(metadata["id"])
    transferred = base64.b64decode(fetched["image_b64"])
    image = Image.open(io.BytesIO(transferred)).convert("RGB")

    assert image.getpixel((160, 60)) == (0, 0, 0)
    assert image.getpixel((210, 110)) == (0, 0, 0)
    assert image.getpixel((30, 40)) == (0, 0, 255)
    assert "image_b64" not in json.dumps(attached.to_dict())
    assert fetched["sha256"] == metadata["sha256"]


def test_capture_lifecycle_reuses_each_after_frame_as_the_next_before_frame():
    store = RecordingEvidenceStore(_ScreenshotBackend())

    initialized = store.initialize()
    first = store.attach(_event())
    second = store.attach(_event())

    assert initialized["ok"] is True
    assert first.evidence["beforeCapture"]["id"] == initialized["capture"]["id"]
    assert second.evidence["beforeCapture"]["id"] == first.evidence["capture"]["id"]
    assert second.evidence["capture"]["id"] != first.evidence["capture"]["id"]


def test_recording_refuses_a_full_screen_frame_at_the_source_boundary():
    store = RecordingEvidenceStore(_FullScreenBackend())

    initialized = store.initialize()

    assert initialized["ok"] is False
    assert "locked window" in initialized["error"]


def test_recording_refuses_to_capture_when_secret_enumeration_is_unavailable():
    backend = _FailedTreeBackend()
    store = RecordingEvidenceStore(backend)

    initialized = store.initialize()

    assert initialized["ok"] is False
    assert initialized["code"] == "recording_capture_evidence_unavailable"
    assert "protected-control enumeration failed" in initialized["error"]
    assert backend.screenshot_calls == 0


def test_recording_refuses_to_capture_an_unbounded_secret_control():
    backend = _UnboundedSecretBackend()

    initialized = RecordingEvidenceStore(backend).initialize()

    assert initialized["ok"] is False
    assert "no redaction rectangle" in initialized["error"]
    assert backend.screenshot_calls == 0


def test_recording_refuses_a_window_frame_without_a_trustworthy_origin():
    initialized = RecordingEvidenceStore(_MissingOriginBackend()).initialize()

    assert initialized["ok"] is False
    assert "origin is invalid" in initialized["error"]


def test_recording_refuses_frame_if_locked_window_changes_during_capture():
    class _ChangingWindowBackend(_ScreenshotBackend):
        def __init__(self):
            super().__init__()
            self.handle = 101

        def get_window_lock(self):
            return {"ok": True, "locked": True, "lock": {
                "pid": 42, "handle": self.handle,
                "snapshot": {"pid": 42, "handle": self.handle, "title": "logo1"},
            }}

        def screenshot(self, path=None):
            frame = super().screenshot(path)
            self.handle += 1
            return frame

    with pytest.raises(ValueError, match="window changed during capture"):
        capture_redacted_window_frame(_ChangingWindowBackend())


def test_recording_reenumerates_redactions_after_one_window_replacement():
    class _OnceChangingBackend(_ScreenshotBackend):
        def __init__(self):
            super().__init__()
            self.handle = 101
            self.tree_calls = 0
            self.screenshot_calls = 0

        def get_window_lock(self):
            return {"ok": True, "locked": True, "lock": {
                "pid": 42, "handle": self.handle,
                "snapshot": {"pid": 42, "handle": self.handle, "title": "logo1"},
            }}

        def dump_tree(self, max_depth=15):
            self.tree_calls += 1
            return super().dump_tree(max_depth=max_depth)

        def screenshot(self, path=None):
            self.screenshot_calls += 1
            frame = super().screenshot(path)
            if self.screenshot_calls == 1:
                self.handle = 102
            return frame

    backend = _OnceChangingBackend()
    png, metadata = capture_redacted_window_frame(backend)

    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert metadata["redacted"] is True
    assert backend.tree_calls == 2
    assert backend.screenshot_calls == 2


def test_agent_artifact_pipeline_attaches_redacted_visual_templates(tmp_path):
    store = RecordingEvidenceStore(_ScreenshotBackend())
    assert store.initialize()["ok"] is True
    attached = store.attach(_event())
    capture_id = attached.evidence["capture"]["id"]
    payload = base64.b64decode(store.get(capture_id)["image_b64"])
    before_id = attached.evidence["beforeCapture"]["id"]
    before_payload = base64.b64decode(store.get(before_id)["image_b64"])
    recording = RawRecording("REC-1", "policy flow", (attached,))

    artifacts = write_compilation_artifacts(
        tmp_path,
        recording,
        compile_recording(recording),
        captures={before_id: before_payload, capture_id: payload},
    )
    golden = json.loads(artifacts.golden_trace.read_text(encoding="utf-8"))
    visual = golden["steps"]["step-0001"]["selector"]["visual"]

    assert visual["redacted"] is True
    assert visual["template"] == "assets/step-0001-element.png"
    assert (artifacts.directory / visual["template"]).exists()
    assert (artifacts.directory / visual["contextTemplate"]).exists()
    assert (artifacts.directory / f"assets/observations/{capture_id}.png").exists()
    schema_root = __import__("pathlib").Path(__file__).resolve().parents[1] / "schema"
    for document_name, schema_name in (
        ("case.json", "desktop-recorded-case.schema.json"),
        ("golden-trace.json", "desktop-golden-trace.schema.json"),
    ):
        document = json.loads((artifacts.directory / document_name).read_text())
        schema = json.loads((schema_root / schema_name).read_text())
        jsonschema.validate(document, schema)


def test_click_without_semantic_identity_is_promoted_only_by_safe_visual_artifact(tmp_path):
    store = RecordingEvidenceStore(_ScreenshotBackend())
    assert store.initialize()["ok"] is True
    weak_target = replace(
        _event().observed_target, automation_id=None, identifier=None,
        control_type="Pane", text=None,
    )
    attached = store.attach(replace(_event(), observed_target=weak_target))
    recording = RawRecording("REC-1", "image only flow", (attached,))
    compiled = compile_recording(recording)
    assert compiled.golden.status == "incomplete"
    assert compiled.case.steps[0].selector is None

    before_id = attached.evidence["beforeCapture"]["id"]
    after_id = attached.evidence["capture"]["id"]
    captures = {
        capture_id: base64.b64decode(store.get(capture_id)["image_b64"])
        for capture_id in (before_id, after_id)
    }
    artifacts = write_compilation_artifacts(tmp_path, recording, compiled, captures=captures)
    golden = json.loads(artifacts.golden_trace.read_text(encoding="utf-8"))
    report = json.loads(artifacts.compile_report.read_text(encoding="utf-8"))
    assert artifacts.compilation.golden.status == golden["status"] == report["status"] == "ready"
    assert artifacts.compilation.issues == ()
    selector = golden["steps"]["step-0001"]["selector"]
    assert selector["control"] == {}
    assert selector["visual"]["redacted"] is True
    assert (artifacts.directory / selector["visual"]["template"]).is_file()

    # Exercise the actual generated PNGs and matcher, not a supplied hit.
    trace = load_golden_trace(artifacts.golden_trace)
    matcher = PillowTemplateMatcher(scales=(1.0,))
    materialize = GoldenStepMaterializer(
        trace, replay_mode="visual_only",
        visual_resolver=SafeVisualResolver(
            lambda path, observation: matcher(str(artifacts.directory / path), observation),
        ),
    )
    step = golden_to_test_case(trace).steps[0]
    for index, origin in enumerate(((100, 200), (300, 400)), start=1):
        observation = {
            "snapshot_id": f"OBS-REPLAY-{index}", "controls": [],
            "active_window": {
                "process_name": "EDRClient.exe", "title": "EDRClient",
                "rect": [origin[0], origin[1], origin[0] + 300, origin[1] + 200],
            },
            "screenshot_scope": "window", "screenshot_redacted": True,
            "screenshot_path": str(artifacts.directory / "assets" / "observations" / f"{before_id}.png"),
            "screenshot_origin": list(origin),
        }
        action = materialize(step, observation)
        assert action.action_id == "pointer.click_window"
        assert action.target_ref is None
        assert action.args["x"] == 50
        assert action.args["y"] == 50


def test_click_without_semantic_identity_stays_incomplete_without_safe_template(tmp_path):
    store = RecordingEvidenceStore(_ScreenshotBackend())
    assert store.initialize()["ok"] is True
    weak_target = replace(
        _event().observed_target, automation_id=None, identifier=None,
        control_type="Pane", text=None,
    )
    attached = store.attach(replace(_event(), observed_target=weak_target))
    recording = RawRecording("REC-1", "image only flow", (attached,))
    compiled = compile_recording(recording)
    before_id = attached.evidence["beforeCapture"]["id"]
    before_payload = base64.b64decode(store.get(before_id)["image_b64"])
    for label, captures in (("missing", {}), ("tampered", {before_id: before_payload[:-1]})):
        artifacts = write_compilation_artifacts(tmp_path / label, recording, compiled,
                                                captures=captures)
        assert artifacts.compilation.golden.status == "incomplete"
        assert any(issue.code == "compile_selector_ambiguous"
                   for issue in artifacts.compilation.issues)


@pytest.mark.parametrize("protected", [True, None])
@pytest.mark.parametrize("weak_identity", [False, True])
def test_protected_or_unknown_target_never_gets_a_visual_click_template(
    tmp_path, protected, weak_identity,
):
    store = RecordingEvidenceStore(_ScreenshotBackend())
    assert store.initialize()["ok"] is True
    event = _event(protected=protected)
    if weak_identity:
        event = replace(event, observed_target=replace(
            event.observed_target, automation_id=None, identifier=None,
            control_type="Pane", text=None,
        ))
    attached = store.attach(event)
    capture_id = attached.evidence["capture"]["id"]
    payload = base64.b64decode(store.get(capture_id)["image_b64"])
    before_id = attached.evidence["beforeCapture"]["id"]
    before_payload = base64.b64decode(store.get(before_id)["image_b64"])
    recording = RawRecording("REC-1", "secret flow", (attached,))

    artifacts = write_compilation_artifacts(
        tmp_path,
        recording,
        compile_recording(recording),
        captures={before_id: before_payload, capture_id: payload},
    )
    golden = json.loads(artifacts.golden_trace.read_text(encoding="utf-8"))

    selector = golden["steps"]["step-0001"]["selector"]
    if weak_identity:
        assert golden["status"] == "incomplete"
        assert selector is None
    else:
        assert selector["visual"] is None


def test_unbound_window_transition_does_not_shift_later_visual_evidence(tmp_path):
    store = RecordingEvidenceStore(_ScreenshotBackend())
    assert store.initialize()["ok"] is True
    attached = replace(store.attach(_event()), sequence=2, monotonic_ms=20)
    capture_id = attached.evidence["capture"]["id"]
    payload = base64.b64decode(store.get(capture_id)["image_b64"])
    before_id = attached.evidence["beforeCapture"]["id"]
    before_payload = base64.b64decode(store.get(before_id)["image_b64"])
    transition = RawCaptureEvent(
        1, "2026-08-17T00:00:00Z", 10, "window_transition",
        attached.scope, input={"kind": "opened"}, causal_id="orphan",
    )
    recording = RawRecording("REC-1", "transition flow", (transition, attached))

    artifacts = write_compilation_artifacts(
        tmp_path,
        recording,
        compile_recording(recording),
        captures={before_id: before_payload, capture_id: payload},
    )
    golden = json.loads(artifacts.golden_trace.read_text(encoding="utf-8"))

    assert golden["steps"]["step-0001"]["selector"]["visual"] is None
    assert golden["steps"]["step-0002"]["selector"]["visual"]["redacted"] is True


class _RedactionBackend:
    """Minimal backend exposing exactly what the redaction pipeline reads."""

    def __init__(self, *, controls=(), windows=(), scope="window", origin=(0, 0)):
        self._controls = list(controls)
        self._windows = list(windows)
        self._scope = scope
        self._origin = list(origin)

    def dump_tree(self, max_depth=15):
        return {"ok": True, "controls": self._controls}

    def list_windows(self):
        return {"ok": True, "windows": self._windows}

    def screenshot(self, path):
        from PIL import Image
        import io

        buffer = io.BytesIO()
        Image.new("RGB", (40, 30), (200, 200, 200)).save(buffer, format="PNG")
        return {
            "ok": True,
            "capture_scope": self._scope,
            "origin": self._origin,
            "image_b64": base64.b64encode(buffer.getvalue()).decode("ascii"),
        }


def _pixels(png_bytes):
    from PIL import Image
    import io

    return Image.open(io.BytesIO(png_bytes)).convert("RGB")


def test_replay_capture_paints_out_protected_controls_before_returning():
    backend = _RedactionBackend(controls=[{
        "control_type": "Edit", "is_password": True,
        "rectangle": {"x": 0, "y": 0, "w": 10, "h": 10},
    }])

    png, metadata = capture_redacted_window_frame(backend)

    assert metadata["redacted"] is True
    assert metadata["redactions"] == [[0, 0, 10, 10]]
    assert _pixels(png).getpixel((5, 5)) == (0, 0, 0)
    assert _pixels(png).getpixel((30, 20)) != (0, 0, 0)


def test_replay_capture_masks_the_recorder_window():
    backend = _RedactionBackend(windows=[{
        "title": "EDR-WD Recorder [recorder_ui=true]",
        "rectangle": {"x": 20, "y": 0, "w": 20, "h": 30},
    }])

    png, metadata = capture_redacted_window_frame(backend)

    assert metadata["redactions"] == [[20, 0, 40, 30]]
    assert _pixels(png).getpixel((30, 15)) == (0, 0, 0)


def test_replay_capture_refuses_a_frame_that_is_not_window_scoped():
    with pytest.raises(ValueError, match="locked window"):
        capture_redacted_window_frame(_RedactionBackend(scope="screen"))


def test_replay_capture_refuses_when_protected_controls_cannot_be_enumerated():
    class _Broken(_RedactionBackend):
        def dump_tree(self, max_depth=15):
            return {"ok": False}

    with pytest.raises(ValueError, match="protected-control enumeration failed"):
        capture_redacted_window_frame(_Broken())


def test_protected_scan_prefers_a_backend_that_answers_directly():
    """Deriving this from a full tree dump costs 18-24 s per call on macOS.

    It runs after every recorded step, so that cost showed up as each step
    taking about ten seconds to appear in the recording.
    """
    from target.recording.evidence import protected_rectangles

    class _Backend:
        def __init__(self):
            self.dump_tree_calls = 0

        def protected_rectangles(self):
            return [{"rectangle": {"x": 10, "y": 20, "w": 100, "h": 30}}]

        def dump_tree(self, max_depth=15):
            self.dump_tree_calls += 1
            return {"ok": True, "controls": []}

        def list_windows(self):
            return {"ok": True, "windows": []}

    backend = _Backend()
    rects = protected_rectangles(backend)

    assert rects == [(10, 20, 110, 50)]
    assert backend.dump_tree_calls == 0


def test_protected_scan_falls_back_when_the_backend_cannot_answer():
    from target.recording.evidence import protected_rectangles

    class _Backend:
        def protected_rectangles(self):
            return None  # native path unavailable on this host

        def dump_tree(self, max_depth=15):
            return {"ok": True, "controls": [
                {"role": "AXSecureTextField", "protected": True,
                 "rectangle": {"x": 1, "y": 2, "w": 10, "h": 20}},
            ]}

        def list_windows(self):
            return {"ok": True, "windows": []}

    assert protected_rectangles(_Backend()) == [(1, 2, 11, 22)]


def test_a_failing_native_scan_is_not_treated_as_nothing_to_redact():
    """Silently skipping redaction would let a password into a stored frame."""
    from target.recording.evidence import protected_rectangles

    class _Backend:
        def protected_rectangles(self):
            raise RuntimeError("accessibility unavailable")

    with pytest.raises(ValueError):
        protected_rectangles(_Backend())


def test_the_native_path_still_masks_the_recorders_own_window():
    """The fast path must not skip the recorder mask.

    Returning early after the native scan would leave the recorder's own
    always-on-top window visible in every stored evidence frame.
    """
    from target.recording.evidence import protected_rectangles

    class _Backend:
        def protected_rectangles(self):
            return []

        def list_windows(self):
            return {"ok": True, "windows": [
                {"title": "EDR-WD Recorder [recorder_ui=true]",
                 "rectangle": {"x": 1696, "y": 856, "w": 328, "h": 136}},
            ]}

    assert protected_rectangles(_Backend()) == [(1696, 856, 2024, 992)]


def test_recorder_window_lookup_prefers_the_direct_backend_answer():
    """Deriving it from a full window enumeration costs 5.3 s per call.

    That runs after every recorded step, which together with the tree dump
    is why each step took about ten seconds to reach the recording.
    """
    from target.recording.evidence import protected_rectangles

    class _Backend:
        def __init__(self):
            self.list_windows_calls = 0

        def protected_rectangles(self):
            return []

        def recorder_ui_rectangles(self):
            return [{"rectangle": {"x": 1696, "y": 856, "w": 328, "h": 136}}]

        def list_windows(self):
            self.list_windows_calls += 1
            return {"ok": True, "windows": []}

    backend = _Backend()
    rects = protected_rectangles(backend)

    assert rects == [(1696, 856, 2024, 992)]
    assert backend.list_windows_calls == 0


def test_a_refused_enumeration_is_not_retried_for_every_step():
    """Some applications only reveal the refusal by timing out after 20 s.

    Retrying per step charged every recorded step that timeout, which is what
    made the recording advance about one step every ten seconds.
    """
    from target.recording.evidence import _cached_protected_controls

    class _Backend:
        def __init__(self):
            self.calls = 0
            self._connected_window_snapshot = {
                "pid": 42, "title": "logo1", "handle": 101,
            }

        def dump_tree(self, max_depth=15):
            self.calls += 1
            return {"ok": False, "error": "timeout after 20s"}

    backend = _Backend()
    for _ in range(3):
        with pytest.raises(ValueError):
            _cached_protected_controls(backend)

    assert backend.calls == 1


def test_a_successful_enumeration_is_reused_for_the_same_window():
    from target.recording.evidence import _cached_protected_controls

    class _Backend:
        def __init__(self):
            self.calls = 0
            self._connected_window_snapshot = {
                "pid": 42, "title": "logo1", "handle": 101,
            }

        def dump_tree(self, max_depth=15):
            self.calls += 1
            return {"ok": True, "controls": [
                {"role": "AXSecureTextField", "protected": True,
                 "rectangle": {"x": 1, "y": 2, "w": 10, "h": 20}},
            ]}

    backend = _Backend()
    first = _cached_protected_controls(backend)
    second = _cached_protected_controls(backend)

    assert first == second
    assert backend.calls == 1


def test_a_different_window_is_enumerated_again():
    """Freshness is traded within one page, never across a change of page."""
    from target.recording.evidence import _cached_protected_controls

    class _Backend:
        def __init__(self):
            self.calls = 0
            self._connected_window_snapshot = {"pid": 42, "title": "日志中心"}

        def dump_tree(self, max_depth=15):
            self.calls += 1
            return {"ok": True, "controls": []}

    backend = _Backend()
    _cached_protected_controls(backend)
    backend._connected_window_snapshot = {"pid": 42, "title": "升级日志"}
    _cached_protected_controls(backend)

    assert backend.calls == 2


def test_protected_cache_rechecks_same_title_when_native_window_changes():
    """A replaced HWND must not inherit another window's secret rectangles."""
    from target.recording.evidence import _cached_protected_controls

    class _Backend:
        def __init__(self):
            self.handle = 101
            self.calls = 0

        def get_window_lock(self):
            return {"ok": True, "locked": True, "lock": {
                "pid": 42, "handle": self.handle,
                "snapshot": {"pid": 42, "handle": self.handle, "title": "logo1"},
            }}

        def dump_tree(self, max_depth=15):
            self.calls += 1
            return {"ok": True, "controls": [{
                "protected": True,
                "rectangle": {"x": self.handle, "y": 2, "w": 10, "h": 20},
            }]}

    backend = _Backend()
    first = _cached_protected_controls(backend)
    again = _cached_protected_controls(backend)
    backend.handle = 102
    replacement = _cached_protected_controls(backend)

    assert first == again
    assert first != replacement
    assert backend.calls == 2


def test_protected_cache_does_not_reuse_a_lock_without_native_identity():
    from target.recording.evidence import _cached_protected_controls

    class _Backend:
        def __init__(self):
            self.calls = 0

        def get_window_lock(self):
            return {"ok": True, "locked": True, "lock": {
                "pid": 42, "snapshot": {"pid": 42, "title": "logo1"},
            }}

        def dump_tree(self, max_depth=15):
            self.calls += 1
            return {"ok": True, "controls": []}

    backend = _Backend()
    _cached_protected_controls(backend)
    _cached_protected_controls(backend)
    assert backend.calls == 2


# ── a replaced window must not cost the rest of the recording ────────────────


class _StaleThenFresh:
    """A backend whose window wrapper goes stale until it reconnects.

    HiSec replaces its own top-level window mid-recording; every UIA call on
    the old wrapper then fails identically.
    """

    COM_ERROR = "(-2147220991, '事件无法调用任何订户', (None, None, None, 0, None))"

    def __init__(self, *, reconnects=True):
        self.connected = False
        self.dump_calls = 0
        self.reconnects = reconnects
        self.reconnect_calls = 0
        self._edr_protected_cache = None

    def dump_tree(self, max_depth=15):
        self.dump_calls += 1
        if not self.connected:
            raise OSError(self.COM_ERROR)
        return {"ok": True, "controls": []}

    def reconnect_locked_window(self):
        self.reconnect_calls += 1
        if not self.reconnects:
            return {"ok": False, "error": "no window lock to reconnect from"}
        self.connected = True
        return {"ok": True}

    def list_windows(self):
        return {"ok": True, "windows": []}


def test_a_stale_window_is_reconnected_instead_of_failing_the_step():
    from target.recording.evidence import protected_rectangles

    backend = _StaleThenFresh()

    assert protected_rectangles(backend) == []
    assert backend.reconnect_calls == 1
    assert backend.dump_calls == 2  # the failure, then the retry


def test_a_reconnect_that_cannot_help_still_fails_closed():
    """The original cause is preserved rather than replaced by a generic one."""
    from target.recording.evidence import protected_rectangles

    backend = _StaleThenFresh(reconnects=False)

    with pytest.raises(OSError, match="事件无法调用任何订户"):
        protected_rectangles(backend)
    assert backend.reconnect_calls == 1


def test_the_failure_is_only_cached_once_the_reconnect_has_been_tried():
    """Caching before retrying is what turned one bad moment into nine."""
    from target.recording.evidence import protected_rectangles

    backend = _StaleThenFresh()
    protected_rectangles(backend)

    assert backend._edr_protected_cache.get("failed") is not True


def test_a_backend_without_reconnect_support_is_unaffected():
    from target.recording.evidence import protected_rectangles

    class _Old(_StaleThenFresh):
        reconnect_locked_window = None

    with pytest.raises(Exception):
        protected_rectangles(_Old())
