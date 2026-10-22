"""Text-confirmation assist: opt-in, fail-closed, and traceable.

Round-23 introduced a UIA-text disambiguation and wrongly enabled it for
the pure ``visual_only`` replay, whose architecture contract (visual
capability evaluation, product-adapter design §6.2, record/replay design
§13.1) forbids semantic channels: the recorded control name silently
changed the materialized coordinates at a refused pixel margin. Round-24
restores the strict pixel-margin refusal for ``visual_only`` — its
outcome must be independent of any UIA signal — and confines the assist
to explicitly semantic policies (the ``semantic_first`` visual
fallback), where it stays fail-closed (missing/duplicate/near-text/
out-of-window/malformed signals keep the ambiguity standing) and records
its actual evidence channel (source, frame/window references, refused
margin — never the control text) into the real ``action_requested``
event.
"""

from __future__ import annotations

import json
import runpy
from pathlib import Path

from PIL import Image, ImageDraw

import pytest

from agent.execution import AtomicExecutor, StepMaterializationError
from agent.execution.confirmation import ConfirmationGate
from agent.recording.mcp_runtime import MCPActionDispatch
from agent.recording.models import ReplaySelector
from agent.recording.pillow_matcher import PillowTemplateMatcher
from agent.recording.replay import (
    GoldenStepMaterializer,
    ResolutionEvidenceSink,
    ReplayRuntime,
    golden_to_test_case,
    replay_golden_trace,
)
from agent.recording.visual import SafeVisualResolver, VisualCandidate
from agent.trace.events import EventType
from agent.trace.store import TraceStore

pytestmark = pytest.mark.unit


def _selector(control=None, visual=None):
    return ReplaySelector(
        window={"processName": "EDRClient.exe"},
        control=control if control is not None else {"name": "Log Center"},
        visual=visual,
    )


def _observation(controls, window=(0, 0, 480, 300), screenshot_path=None):
    observation = {
        "controls": controls,
        "screenshot_scope": "window",
        "screenshot_redacted": True,
        "active_window": {"rect": list(window)},
    }
    if screenshot_path is not None:
        observation["screenshot_path"] = screenshot_path
    return observation


def _control(text, rect):
    return {"text": text, "rect": list(rect), "control_type": "Button",
            "automation_id": f"btn-{rect[0]}-{rect[1]}"}


def _two_tile_frame(tmp_path):
    """480x300 frame: two near-identical 44x34 tiles (B = A plus a 2x2
    mark, so the pixel margin between them is far below 0.05) plus a
    third control region elsewhere."""
    image = Image.new("RGB", (480, 300), (250, 251, 253))
    painter = ImageDraw.Draw(image)
    for x in (192, 240):
        painter.rectangle((x, 16, x + 43, 49), fill=(238, 242, 248),
                          outline=(190, 200, 215))
        painter.rectangle((x + 10, 24, x + 33, 28), fill=(60, 80, 120))
        painter.rectangle((x + 12, 32, x + 30, 34), fill=(90, 100, 120))
        painter.rectangle((x + 10, 40, x + 33, 42), fill=(140, 150, 165))
    painter.rectangle((272, 47, 274, 49), fill=(60, 60, 60))  # B's 2x2 mark
    painter.rectangle((60, 200, 104, 233), fill=(250, 251, 253), outline=(200, 205, 215))
    frame_path = tmp_path / "frame.png"
    template_path = tmp_path / "template.png"
    image.save(frame_path)
    image.crop((192, 16, 236, 50)).save(template_path)
    return str(frame_path), str(template_path), (192, 16, 236, 50), (240, 16, 284, 50)


def _resolver(matcher):
    return SafeVisualResolver(matcher, template_verifier=lambda _path, _visual: True)


def test_text_confirmation_rescues_ambiguous_pixel_match(tmp_path):
    """Opt-in positive: pixel margin ~0.005 (refused), the recorded name
    appears on exactly one control confirming exactly one candidate ->
    resolved with its evidence provenance; click inside the named tile."""
    frame, template, rect_a, rect_b = _two_tile_frame(tmp_path)
    resolver = _resolver(PillowTemplateMatcher(scales=(1.0,)))
    selector = _selector(visual={"template": template,
                                 "elementSha256": "sha256:" + "a" * 64,
                                 "redacted": True})
    observation = _observation([
        _control("Log Center", rect_a),
        _control("Quarantine &\nBlocking Area", rect_b),
        _control("Trusted List", (60, 200, 104, 234)),
    ], screenshot_path=frame)
    resolved = resolver.resolve("gui.click", selector, observation,
                                allow_text_confirmation=True)
    assert resolved.text_confirmed is True
    assert resolved.confidence == 1.0
    # click must land inside tile A (window origin (0,0): relative == screen)
    assert rect_a[0] <= resolved.args["x"] < rect_a[2]
    assert rect_a[1] <= resolved.args["y"] < rect_a[3]
    assert not (rect_b[0] <= resolved.args["x"] < rect_b[2])
    evidence = resolved.evidence
    assert evidence["text_confirmed"] is True
    assert evidence["source"] == "uia_control_text"
    assert evidence["margin"] < 0.05
    assert evidence["frame"]["scope"] == "window"
    assert evidence["window"]["rect"] == [0, 0, 480, 300]
    assert evidence["control"]["rect"] == list(rect_a)
    # the evidence channel never carries the control text itself
    assert "Log Center" not in json.dumps(evidence)


def test_visual_only_outcome_is_independent_of_recorded_name(tmp_path):
    """Round-24 V1 regression (resolver level): without the explicit opt-in
    the strict pixel-margin refusal stands no matter what name is recorded
    — the outcome must never depend on a UIA/semantic signal."""
    frame, template, rect_a, rect_b = _two_tile_frame(tmp_path)
    resolver = _resolver(PillowTemplateMatcher(scales=(1.0,)))
    controls = [
        _control("Log Center", rect_a),
        _control("Quarantine &\nBlocking Area", rect_b),
    ]
    for name in ("Log Center", "Wrong Window", ""):
        selector = _selector(control={"name": name},
                             visual={"template": template,
                                     "elementSha256": "sha256:" + "a" * 64,
                                     "redacted": True})
        with pytest.raises(StepMaterializationError) as exc:
            resolver.resolve("gui.click", selector,
                             _observation(controls, screenshot_path=frame))
        assert exc.value.code == "visual_match_ambiguous"


def _replay_fixtures():
    return runpy.run_path(
        str(Path(__file__).resolve().parent / "test_replay.py"))


def test_visual_only_materializer_refuses_regardless_of_recorded_name():
    """Round-24 V1 regression (materializer level, the supervisor's
    counterexample): the same two pixel candidates (1.0/0.99) and the same
    observation materialize NOTHING under visual_only, whatever name is
    recorded — only a genuinely unambiguous pixel margin may dispatch."""
    fixtures = _replay_fixtures()
    golden_factory, RecordedStep = fixtures["_golden"], fixtures["RecordedStep"]
    observation = {
        "controls": [
            {"text": "Log Center", "rect": [10, 10, 50, 50]},
            {"text": "Wrong Window", "rect": [100, 10, 140, 50]},
        ],
        "screenshot_scope": "window",
        "screenshot_redacted": True,
        "active_window": {"rect": [0, 0, 300, 200]},
    }
    resolver = _resolver(lambda *_: [VisualCandidate((10, 10, 50, 50), 1.0),
                                     VisualCandidate((100, 10, 140, 50), 0.99)])
    for name in ("Log Center", "Wrong Window", ""):
        selector = ReplaySelector(
            window={"processName": "EDRClient.exe"}, control={"name": name},
            visual={"template": "synthetic", "redacted": True,
                    "elementSha256": "sha256:" + "a" * 64},
        )
        golden = golden_factory(RecordedStep("step-0001", "gui.click", {}, selector))
        materializer = GoldenStepMaterializer(
            golden, replay_mode="visual_only", visual_resolver=resolver,
        )
        with pytest.raises(StepMaterializationError) as exc:
            materializer(golden_to_test_case(golden).steps[0], observation)
        assert exc.value.code == "visual_match_ambiguous"
        assert materializer.visual_fallbacks == 0


def test_visual_only_materializer_publishes_no_evidence():
    """A pure pixel visual_only resolution carries no assist evidence: the
    trace's action_requested event must stay free of resolution_evidence."""
    fixtures = _replay_fixtures()
    golden_factory, RecordedStep = fixtures["_golden"], fixtures["RecordedStep"]
    observation = {
        "controls": [],
        "screenshot_scope": "window",
        "screenshot_redacted": True,
        "active_window": {"rect": [0, 0, 300, 200]},
    }
    resolver = _resolver(lambda *_: [VisualCandidate((10, 10, 50, 50), 1.0),
                                     VisualCandidate((100, 10, 140, 50), 0.80)])
    selector = ReplaySelector(
        window={"processName": "EDRClient.exe"}, control={"name": "Log Center"},
        visual={"template": "synthetic", "redacted": True,
                "elementSha256": "sha256:" + "a" * 64},
    )
    golden = golden_factory(RecordedStep("step-0001", "gui.click", {}, selector))
    sink = ResolutionEvidenceSink()
    materializer = GoldenStepMaterializer(
        golden, replay_mode="visual_only", visual_resolver=resolver,
        resolution_evidence=sink,
    )
    step = materializer(golden_to_test_case(golden).steps[0], observation)
    assert step.action_id == "pointer.click_window"
    assert step.args["x"] == 30 and step.args["y"] == 30
    assert sink.latest is None


def test_assisted_semantic_first_fallback_records_evidence_channel(tmp_path):
    """Round-24 trace-level regression: in the explicitly semantic
    semantic_first mode, the opted-in text confirmation is traceable — the
    real dispatch emits its evidence channel in the actual
    action_requested event, and a wrong recorded name refuses instead of
    retargeting the click."""
    fixtures = _replay_fixtures()
    golden_factory, RecordedStep = fixtures["_golden"], fixtures["RecordedStep"]
    observations_type = fixtures["_Observations"]
    observation_control = {
        "process_name": "EDRClient.exe", "window_title": "EDRClient",
        "control_type": "Button", "automation_id": "logCenterBtn",
        "text": "Log Center", "rect": [10, 10, 50, 50],
    }

    def _run(recorded_name, trace_dir):
        # A stable automationId owns the semantic identity, so the recorded
        # name never creates a semantic hit; the fused locator sees one
        # clean candidate while the fallback re-match finds a near-tied
        # pair (margin 0.01 < 0.05) that only the opt-in may disambiguate.
        matcher_calls = {"n": 0}

        def matcher(_template, _observation):
            matcher_calls["n"] += 1
            if matcher_calls["n"] == 1:
                return [VisualCandidate((10, 10, 50, 50), 1.0)]
            return [VisualCandidate((10, 10, 50, 50), 1.0),
                    VisualCandidate((100, 10, 140, 50), 0.99)]

        selector = ReplaySelector(
            window={"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"},
            control={"automationId": "missing", "name": recorded_name},
            visual={"template": "assets/apply.png", "redacted": True,
                    "elementSha256": "sha256:" + "a" * 64,
                    "relativePoint": [0.5, 0.5]},
        )
        sink = ResolutionEvidenceSink()
        trace_store = TraceStore(trace_dir)
        trace_store.open(trace_id="TR-GOLDEN")

        class _Agent:
            def __init__(self):
                self.calls = []

            def call_tool(self, name, payload, timeout=None):
                self.calls.append(payload)
                return {"code": "ok", "ok": True,
                        "action_id": payload["action_id"],
                        "request_id": payload["request_id"]}

        agent = _Agent()
        executor = AtomicExecutor(
            dispatch=MCPActionDispatch(agent, trace_store=trace_store,
                                       resolution_evidence=sink),
            observation_provider=observations_type([observation_control]),
        )
        runtime = ReplayRuntime(
            executor, "1.0.0", "sha256:catalog",
            trace_store=trace_store, replay_mode="semantic_first",
            visual_resolver=_resolver(matcher), resolution_evidence=sink,
        )
        return replay_golden_trace(runtime, golden_factory(
            RecordedStep("step-0001", "gui.click", {}, selector))), trace_store, agent

    result, trace_store, agent = _run("Log Center", tmp_path / "trace-ok")
    assert result.task_success is True
    requested = [event for event in trace_store.read_events()
                 if event.event_type is EventType.ACTION_REQUESTED]
    assert len(requested) == 1
    evidence = requested[0].payload["resolution_evidence"]
    assert evidence["text_confirmed"] is True
    assert evidence["source"] == "uia_control_text"
    assert evidence["margin"] < 0.05
    assert evidence["frame"]["scope"] == "window"
    assert evidence["window"]["process_name"] == "EDRClient.exe"
    # traceable current-observation and stable window references
    assert evidence["frame"]["snapshot_id"].startswith("OBS-FRESH-")
    assert evidence["window"]["native_id"] == "legacy"
    # the evidence is bound to the materialized step and action
    assert evidence["step_id"] == "step-0001"
    assert evidence["action_id"] == "pointer.click_window"
    assert "Log Center" not in json.dumps(evidence)
    # consumed exactly once — the dispatched coordinates carry no evidence key
    assert agent.calls[0]["args"]["x"] == 30
    assert "resolution_evidence" not in agent.calls[0]["args"]

    refused, refused_store, refused_agent = _run("Wrong Window", tmp_path / "trace-refused")
    assert refused.task_success is False
    assert refused.case_result.step_results[0].error["code"] == "visual_match_ambiguous"
    assert refused_agent.calls == []
    assert all(event.event_type is not EventType.ACTION_REQUESTED
               for event in refused_store.read_events())


def _blocked_main_and_semantic_cleanup(tmp_path, matcher, trace_name):
    """Full offline replay: an assisted main action and a pure semantic
    cleanup step, with a confirmation gate that treats the materialized
    pointer.click_window as high-risk (no token supplied)."""
    fixtures = _replay_fixtures()
    golden_factory, RecordedStep = fixtures["_golden"], fixtures["RecordedStep"]
    observations_type = fixtures["_Observations"]
    control = {
        "process_name": "EDRClient.exe", "window_title": "EDRClient",
        "control_type": "Button", "automation_id": "real",
        "text": "Log Center", "rect": [10, 10, 50, 50],
    }
    main_selector = ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"},
        control={"automationId": "missing", "name": "Log Center"},
        visual={"template": "synthetic", "redacted": True,
                "elementSha256": "sha256:" + "a" * 64},
    )
    cleanup_selector = ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"},
        control={"automationId": "real"},
    )

    class _Agent:
        def __init__(self):
            self.calls = []

        def call_tool(self, name, payload, timeout=None):
            self.calls.append(payload)
            return {"code": "ok", "ok": True, "action_id": payload["action_id"],
                    "request_id": payload["request_id"]}

    store = TraceStore(tmp_path / trace_name)
    store.open(trace_id=trace_name.upper())
    sink, agent = ResolutionEvidenceSink(), _Agent()
    executor = AtomicExecutor(
        dispatch=MCPActionDispatch(agent, trace_store=store,
                                   resolution_evidence=sink),
        observation_provider=observations_type([control]),
        confirmation_gate=ConfirmationGate(),
        risk_lookup=lambda action: (("high", "gui_mutation")
                                    if action == "pointer.click_window"
                                    else ("low", "none")),
    )
    runtime = ReplayRuntime(
        executor, "1.0.0", "sha256:catalog", trace_store=store,
        replay_mode="semantic_first", resolution_evidence=sink,
        visual_resolver=_resolver(matcher),
    )
    golden = golden_factory(
        RecordedStep("main", "gui.click", {}, main_selector),
        cleanup=(RecordedStep("cleanup", "gui.click", {}, cleanup_selector),),
    )
    result = replay_golden_trace(runtime, golden)
    requested = [event for event in store.read_events()
                 if event.event_type is EventType.ACTION_REQUESTED]
    return result, requested, agent


def test_confirmation_blocked_main_does_not_lend_evidence_to_cleanup(tmp_path):
    """Round-24 V3 regression (the supervisor's exact counterexample): the
    assisted main action is materialized and then refused by the
    confirmation gate with zero dispatches; the semantic cleanup that
    follows must not inherit the main step's evidence channel."""
    matcher_calls = {"n": 0}

    def matcher(*_):
        matcher_calls["n"] += 1
        first = VisualCandidate((10, 10, 50, 50), 1.0)
        if matcher_calls["n"] == 1:
            return [first]
        return [first, VisualCandidate((100, 10, 140, 50), 0.99)]

    result, requested, agent = _blocked_main_and_semantic_cleanup(
        tmp_path, matcher, "trace-blocked-cleanup")
    main_result = result.case_result.step_results[0]
    assert main_result.error["code"] == "confirmation_required"
    # only the cleanup dispatched; the refused main action never did
    assert len(agent.calls) == 1
    assert agent.calls[0]["action_id"] == "gui.click"
    assert len(requested) == 1
    cleanup_event = requested[0]
    assert cleanup_event.payload["action_id"] == "gui.click"
    assert cleanup_event.payload["resolution"] == "semantic"
    assert "resolution_evidence" not in cleanup_event.payload


def test_refused_main_does_not_lend_evidence_to_cleanup(tmp_path):
    """A matcher-refused main action (ambiguous pixels, blocked before any
    dispatch) leaves no evidence for the semantic cleanup either."""
    def matcher(*_):
        first = VisualCandidate((10, 10, 50, 50), 1.0)
        return [first, VisualCandidate((100, 10, 140, 50), 0.99)]

    result, requested, agent = _blocked_main_and_semantic_cleanup(
        tmp_path, matcher, "trace-refused-cleanup")
    assert result.case_result.step_results[0].error["code"] == \
        "visual_match_ambiguous"
    assert len(requested) == 1
    assert requested[0].payload["action_id"] == "gui.click"
    assert "resolution_evidence" not in requested[0].payload


def test_consecutive_assisted_steps_carry_own_evidence(tmp_path):
    """Two consecutive assisted visual fallbacks each publish their own
    evidence envelope: the second action_requested carries the second
    step's source/margin, never the first step's."""
    fixtures = _replay_fixtures()
    golden_factory, RecordedStep = fixtures["_golden"], fixtures["RecordedStep"]
    observations_type = fixtures["_Observations"]
    control = {
        "process_name": "EDRClient.exe", "window_title": "EDRClient",
        "control_type": "Button", "automation_id": "real",
        "text": "Log Center", "rect": [10, 10, 50, 50],
    }
    selector = ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"},
        control={"automationId": "missing", "name": "Log Center"},
        visual={"template": "synthetic", "redacted": True,
                "elementSha256": "sha256:" + "a" * 64},
    )
    matcher_calls = {"n": 0}

    def matcher(*_):
        matcher_calls["n"] += 1
        first = VisualCandidate((10, 10, 50, 50), 1.0)
        if matcher_calls["n"] in (1, 3):  # fused-locator evidence per step
            return [first]
        # fallback re-match per step: near-tied pair, distinct margins
        second = 0.99 if matcher_calls["n"] == 2 else 0.98
        return [first, VisualCandidate((100, 10, 140, 50), second)]

    class _Agent:
        def __init__(self):
            self.calls = []

        def call_tool(self, name, payload, timeout=None):
            self.calls.append(payload)
            return {"code": "ok", "ok": True, "action_id": payload["action_id"],
                    "request_id": payload["request_id"]}

    store = TraceStore(tmp_path / "trace-consecutive")
    store.open(trace_id="TR-CONSECUTIVE")
    sink, agent = ResolutionEvidenceSink(), _Agent()
    executor = AtomicExecutor(
        dispatch=MCPActionDispatch(agent, trace_store=store,
                                   resolution_evidence=sink),
        observation_provider=observations_type([control]),
    )
    runtime = ReplayRuntime(
        executor, "1.0.0", "sha256:catalog", trace_store=store,
        replay_mode="semantic_first", resolution_evidence=sink,
        visual_resolver=_resolver(matcher),
    )
    result = replay_golden_trace(runtime, golden_factory(
        RecordedStep("step-0001", "gui.click", {}, selector),
        RecordedStep("step-0002", "gui.click", {}, selector),
    ))
    assert result.task_success is True
    requested = [event for event in store.read_events()
                 if event.event_type is EventType.ACTION_REQUESTED]
    assert [event.payload["action_id"] for event in requested] == \
        ["pointer.click_window", "pointer.click_window"]
    margins = [event.payload["resolution_evidence"]["margin"]
               for event in requested]
    assert margins == [pytest.approx(0.01), pytest.approx(0.02)]
    assert [event.payload["resolution_evidence"]["step_id"]
            for event in requested] == ["step-0001", "step-0002"]
    assert all(event.payload["resolution_evidence"]["source"] == "uia_control_text"
               for event in requested)


def test_dispatch_without_trace_store_ignores_pending_evidence():
    """No trace store: nothing is emitted and the pending evidence is left
    to the next materialization entry to clear; the dispatch itself stays
    clean and the tool payload never carries an evidence key."""
    sink = ResolutionEvidenceSink()
    sink.latest = {"step_id": "main", "action_id": "pointer.click_window",
                   "evidence": {"text_confirmed": True,
                                "source": "uia_control_text", "margin": 0.01}}

    class _Agent:
        def __init__(self):
            self.calls = []

        def call_tool(self, name, payload, timeout=None):
            self.calls.append(payload)
            return {"code": "ok", "ok": True, "action_id": payload["action_id"],
                    "request_id": payload["request_id"]}

    agent = _Agent()
    dispatch = MCPActionDispatch(agent, trace_store=None,
                                 resolution_evidence=sink)
    receipt = dispatch(action_id="pointer.click_window", action_code=None,
                       args={"x": 30, "y": 30}, target_ref=None,
                       request_id="R-no-trace")
    assert receipt.ok is True
    assert "resolution_evidence" not in agent.calls[0]["args"]


def test_malformed_control_rect_is_structured_refusal(tmp_path):
    """Round-24 V2 regression: a malformed named-control rectangle keeps the
    ambiguity standing with the structured refusal — never ValueError or
    TypeError, and never a fabricated successful candidate."""
    frame, template, rect_a, rect_b = _two_tile_frame(tmp_path)
    resolver = _resolver(PillowTemplateMatcher(scales=(1.0,)))
    selector = _selector(control={"name": "Log Center"},
                         visual={"template": template,
                                 "elementSha256": "sha256:" + "a" * 64,
                                 "redacted": True})
    malformed_rects = [
        [10, "bad", 50, 50],          # non-numeric member
        [10, None, 50, 50],           # None member
        [True, 10, 50, 50],           # bool member
        [10, float("nan"), 50, 50],   # non-finite member
        [10, float("inf"), 50, 50],   # non-finite member
        [10, 10, 10, 10],             # zero-area rect
        [50, 10, 10, 50],             # inverted edges
        [10, 10, 50],                 # wrong arity
        [10, 10, 50, 50, 60],         # wrong arity
        "10,10,50,50",                # not a sequence of numbers
    ]
    for rect in malformed_rects:
        controls = [_control("Log Center", rect_a),
                    _control("Quarantine &\nBlocking Area", rect_b)]
        controls[0] = {**controls[0], "rect": rect}
        with pytest.raises(StepMaterializationError) as exc:
            resolver.resolve("gui.click", selector,
                             _observation(controls, screenshot_path=frame),
                             allow_text_confirmation=True)
        assert exc.value.code == "visual_match_ambiguous", f"rect={rect!r}"


def _assert_ambiguous(tmp_path, controls, record_name="Log Center"):
    frame, template, rect_a, _ = _two_tile_frame(tmp_path)
    resolver = _resolver(PillowTemplateMatcher(scales=(1.0,)))
    selector = _selector(control={"name": record_name},
                         visual={"template": template,
                                 "elementSha256": "sha256:" + "a" * 64,
                                 "redacted": True})
    with pytest.raises(StepMaterializationError) as exc:
        resolver.resolve("gui.click", selector,
                         _observation(controls, screenshot_path=frame),
                         allow_text_confirmation=True)
    assert exc.value.code == "visual_match_ambiguous"


def test_same_named_controls_refuse(tmp_path):
    """Two controls with the recorded name cannot identify an instance."""
    frame, template, rect_a, rect_b = _two_tile_frame(tmp_path)
    _assert_ambiguous(tmp_path, [
        _control("Log Center", rect_a),
        _control("Log Center", rect_b),
    ])


def test_near_text_refuses(tmp_path):
    """Near-miss text (trailing space / extra letter) is not an exact
    match -> the ambiguity stands."""
    for near in ("Log Center ", "LogCenters", "LOG CENTER"):
        _assert_ambiguous(tmp_path, [
            _control(near, (192, 16, 236, 50)),
            _control("Quarantine &\nBlocking Area", (240, 16, 284, 50)),
        ], record_name="Log Center")


def test_missing_text_source_refuses(tmp_path):
    """No control tree (the 'missing OCR' case) or text-less controls: the
    pixel refusal stands, nothing is invented."""
    _assert_ambiguous(tmp_path, [])
    _assert_ambiguous(tmp_path, [{"rect": [192, 16, 236, 50]}])  # no text key


def test_named_control_outside_window_refuses(tmp_path):
    _assert_ambiguous(tmp_path, [
        _control("Log Center", (1000, 16, 1044, 50)),
        _control("Quarantine &\nBlocking Area", (240, 16, 284, 50)),
    ])


def test_unrecorded_name_refuses(tmp_path):
    """Selector without a recorded name: no text source at all."""
    frame, template, rect_a, _ = _two_tile_frame(tmp_path)
    resolver = _resolver(PillowTemplateMatcher(scales=(1.0,)))
    selector = _selector(control={}, visual={"template": template,
                                              "elementSha256": "sha256:" + "a" * 64,
                                              "redacted": True})
    with pytest.raises(StepMaterializationError) as exc:
        resolver.resolve("gui.click", selector, _observation([
            _control("Log Center", rect_a),
            _control("Quarantine &\nBlocking Area", (240, 16, 284, 50)),
        ], screenshot_path=frame), allow_text_confirmation=True)
    assert exc.value.code == "visual_match_ambiguous"


def test_duplicate_candidates_at_named_position_refuse(tmp_path):
    """Two high-confidence candidates centred on the named control
    (a truly repeated target) stay ambiguous even with a unique name."""
    frame, template, rect_a, _ = _two_tile_frame(tmp_path)
    resolver = _resolver(lambda _path, _obs: [
        VisualCandidate((192, 16, 236, 50), 1.0),
        VisualCandidate((194, 17, 238, 51), 0.99)])
    selector = _selector(visual={"template": template,
                                 "elementSha256": "sha256:" + "a" * 64,
                                 "redacted": True})
    with pytest.raises(StepMaterializationError) as exc:
        resolver.resolve("gui.click", selector, _observation([
            _control("Log Center", rect_a),
        ]), allow_text_confirmation=True)
    assert exc.value.code == "visual_match_ambiguous"


def test_state_change_low_confidence_still_refused():
    """A selected-state radio (template recorded unselected) drops the
    true candidate below min_confidence: the confidence gate refuses
    before any text assist; the name alone must never carry a weak match."""
    selector = _selector(visual={"template": "element.png",
                                 "elementSha256": "sha256:" + "a" * 64,
                                 "redacted": True})
    resolver = _resolver(lambda _path, _obs: [
        VisualCandidate((159, 75, 229, 102), 0.90),
        VisualCandidate((65, 75, 135, 102), 0.91)])
    observation = _observation([_control("Scan Log", (159, 75, 229, 102))],
                              window=(400, 210, 1280, 789))
    with pytest.raises(StepMaterializationError) as exc:
        resolver.resolve("gui.click", selector, observation,
                         allow_text_confirmation=True)
    assert exc.value.code == "visual_match_low_confidence"


def test_missing_template_refused():
    """A visual selector without a template is refused before any match:
    the model layer rejects it at construction (fail-closed, no fallback)."""
    from agent.recording.models import RecordingModelError
    with pytest.raises(RecordingModelError) as exc:
        _selector(visual={"elementSha256": "sha256:" + "a" * 64,
                          "redacted": True})
    assert exc.value.code == "visual_template_missing"


def test_margin_pass_keeps_legacy_behavior():
    """When the pixel margin already passes, the resolver behaves exactly
    as before: text_confirmed stays False, no evidence mapping, and the
    text path never runs even when the opt-in is set."""
    selector = _selector(visual={"template": "element.png",
                                 "elementSha256": "sha256:" + "a" * 64,
                                 "redacted": True})
    resolver = _resolver(lambda _path, _obs: [
        VisualCandidate((10, 10, 50, 50), 1.0),
        VisualCandidate((200, 10, 240, 50), 0.80)])
    resolved = resolver.resolve("gui.click", selector, _observation([]),
                                allow_text_confirmation=True)
    assert resolved.text_confirmed is False
    assert resolved.evidence is None
    assert resolved.margin == pytest.approx(0.2)
