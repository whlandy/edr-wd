"""Frame-window consistency regression for protected visual replay (2026-10-30).

Deferred dimension of the 2026-10-30 02:35 round: window move / stale
frame misuse. Probe finding (this round): ``SafeVisualResolver`` projected
matcher candidates into screen coordinates with the capture-time
``screenshot_origin`` but validated them against the CURRENT locked
window rectangle, with no consistency check between the two — a frame
captured before the window moved still resolved and the click landed at
the stale frame's position. Fresh replays keep ``screenshot_origin ==
active_window.rect[:2]`` (all three 2026-10-30 live traces: origin
[80,14] == rect[:2]); the invariant was real but unenforced at the final
dispatch boundary.

Fix under test: the resolver refuses with ``visual_frame_window_mismatch``
when a well-formed ``screenshot_origin`` disagrees with the locked
window's top-left by more than the target-side bundle tolerance (32 px —
the UIA invisible-frame skew the target contract already permits).
Consistent frames — including negative-coordinate multi-monitor windows —
resolve unchanged; observations without an origin keep legacy behavior
(the malformed-origin refusal stays pinned by
test_visual_templates::test_visual_replay_does_not_click_with_malformed_capture_origin).
"""

from __future__ import annotations

import pytest

from agent.execution import StepMaterializationError
from agent.recording.models import ReplaySelector
from agent.recording.visual import SafeVisualResolver, VisualCandidate

pytestmark = pytest.mark.unit


def _selector() -> ReplaySelector:
    return ReplaySelector(
        window={"processName": "EDRClient.exe"}, control={"name": "按钮"},
        visual={"template": "element.png", "elementSha256": "sha256:" + "a" * 64,
                "redacted": True},
    )


def _resolver(candidate: VisualCandidate) -> SafeVisualResolver:
    return SafeVisualResolver(
        lambda _path, _obs: [candidate],
        template_verifier=lambda _path, _visual: True,
    )


def _observation(rect, origin=None) -> dict:
    observation = {
        "screenshot_scope": "window", "screenshot_redacted": True,
        "active_window": {"rect": list(rect)},
    }
    if origin is not None:
        observation["screenshot_origin"] = list(origin)
    return observation


def test_stale_frame_after_window_move_refused():
    """Counterexample (the defect): the frame was captured while the window
    sat at [100,200]; the lock now reports [0,0,600,500] after a move. The
    matcher projects a candidate to screen (500,400) from the stale origin
    — inside the current rectangle, so without the gate the resolver
    clicked stale-frame coordinates. Must refuse, never click."""
    resolver = _resolver(VisualCandidate((500, 400, 560, 440), 0.99, scale=1.0))
    with pytest.raises(StepMaterializationError) as exc:
        resolver.resolve("gui.click", _selector(),
                         _observation((0, 0, 600, 500), origin=(100, 200)))
    assert exc.value.code == "visual_frame_window_mismatch"
    assert exc.value.details == {"frame_origin": [100, 200],
                                 "window_origin": [0, 0]}


def test_consistent_origin_resolves_window_relative():
    """Live-trace geometry (2026-10-30 TR-01a18904: origin [80,14] ==
    rect[:2]): a fresh frame resolves with window-relative coordinates
    identical to the pre-gate behavior."""
    resolver = _resolver(VisualCandidate((500, 400, 560, 440), 0.99, scale=1.0))
    resolved = resolver.resolve("gui.click", _selector(),
                                _observation((80, 14, 1000, 614), origin=(80, 14)))
    assert resolved.args["x"] == 450
    assert resolved.args["y"] == 406


def test_negative_origin_multi_monitor_resolves():
    """Window on a monitor left of the primary (negative coordinates):
    origin == rect[:2] == (-60,-40); a candidate at frame (10,20) resolves
    to window-relative (10,20) — the gate must not reject legitimate
    negative-origin geometry."""
    resolver = _resolver(VisualCandidate((-50, -20, -30, 0), 0.99, scale=1.0))
    resolved = resolver.resolve("gui.click", _selector(),
                                _observation((-60, -40, 200, 120), origin=(-60, -40)))
    # click point = candidate centre (-40,-10) screen → window-relative (20,30)
    assert resolved.args["x"] == 20
    assert resolved.args["y"] == 30


@pytest.mark.parametrize("origin,allowed", [((32, 0), True), ((33, 0), False)])
def test_origin_skew_boundary_matches_bundle_contract(origin, allowed):
    """The gate accepts the same skew the target-side bundle contract
    permits (UIA rectangles can exclude the invisible Win32 frame the
    capture includes) and refuses one pixel beyond it."""
    resolver = _resolver(VisualCandidate((10, 10, 50, 50), 0.99, scale=1.0))
    if allowed:
        resolved = resolver.resolve("gui.click", _selector(),
                                    _observation((0, 0, 200, 120), origin=origin))
        assert resolved.args["x"] == 30
    else:
        with pytest.raises(StepMaterializationError) as exc:
            resolver.resolve("gui.click", _selector(),
                             _observation((0, 0, 200, 120), origin=origin))
        assert exc.value.code == "visual_frame_window_mismatch"


def test_observation_without_origin_keeps_legacy_behavior():
    """Observations that carry no screenshot_origin keep the pre-gate
    contract (existing anchors in test_visual_templates rely on it)."""
    resolver = _resolver(VisualCandidate((110, 210, 120, 220), 0.99, scale=1.0))
    resolved = resolver.resolve("gui.click", _selector(),
                                _observation((100, 200, 140, 240)))
    # click point = candidate centre (115,215) screen → window-relative (15,15);
    # matches test_visual_templates's anchor contract on identical geometry.
    assert resolved.args["x"] == 15
    assert resolved.args["y"] == 15


def test_collect_evidence_refuses_stale_frame():
    """collect_evidence feeds fused locator decisions from the same frame;
    a stale frame must not poison the semantic/visual fusion either."""
    resolver = _resolver(VisualCandidate((500, 400, 560, 440), 0.99, scale=1.0))
    with pytest.raises(StepMaterializationError) as exc:
        resolver.collect_evidence(_selector(),
                                  _observation((0, 0, 600, 500), origin=(100, 200)))
    assert exc.value.code == "visual_frame_window_mismatch"


def test_collect_evidence_accepts_consistent_frame():
    resolver = _resolver(VisualCandidate((500, 400, 560, 440), 0.99, scale=1.0))
    evidence = resolver.collect_evidence(
        _selector(), _observation((80, 14, 1000, 614), origin=(80, 14)))
    assert len(evidence) == 1
    assert evidence[0]["rect"] == (500, 400, 560, 440)
