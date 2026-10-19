"""Cross-evidence hard gates and planner candidate parity."""

from __future__ import annotations

import pytest

from agent.locator import HybridLocator, LocatorError, LocatorRequest, LocatorPolicy
from agent.planner.candidates import candidate_view


def _bundle(controls):
    return {"observation_id": "OBS-1", "window_identity": {"native_id": "4", "rect": [0, 0, 100, 100]},
            "active_window": {"process_name": "Demo", "title": "Main"},
            "screenshot_scope": "window", "screenshot_redacted": True,
            "controls": controls}


def _control(target_id, rect):
    return {"target_id": target_id, "process_name": "Demo", "window_title": "Main",
            "text": "Open", "control_type": "Button", "rect": rect}


def _request():
    return LocatorRequest("gui.click", {"processName": "Demo"},
                          {"controlType": "Button", "name": "Open"})


def _visual(rect, confidence=0.98, observation_id="OBS-1", native="4"):
    return {"rect": rect, "confidence": confidence,
            "observation_id": observation_id, "window_native_id": native}


def test_unique_semantic_and_planner_have_same_candidate_ids():
    bundle = _bundle([_control("T1", [0, 0, 30, 30])])
    locator = HybridLocator()
    result = locator.resolve(_request(), bundle, LocatorPolicy())
    assert result.candidate.target_id == "T1"
    assert {item["target_id"] for item in candidate_view(bundle, _request())} == {"T1"}


def test_visual_breaks_two_semantic_matches():
    bundle = _bundle([_control("T1", [0, 0, 30, 30]),
                      _control("T2", [50, 0, 80, 30])])
    result = HybridLocator().resolve(
        _request(), bundle, LocatorPolicy(mode="semantic_first"),
        visual_candidates=(_visual([50, 0, 80, 30]),),
    )
    assert result.candidate.target_id == "T2"


def test_duplicate_visual_matches_do_not_outvote_one_stronger_match():
    bundle = _bundle([_control("T1", [0, 0, 30, 30]),
                      _control("T2", [50, 0, 80, 30])])
    result = HybridLocator().resolve(
        _request(), bundle, LocatorPolicy(mode="semantic_first"),
        visual_candidates=(_visual([0, 0, 30, 30], 0.94),
                           _visual([0, 0, 30, 30], 0.94),
                           _visual([50, 0, 80, 30], 0.99)),
    )
    assert result.candidate.target_id == "T2"


def test_conflict_low_confidence_and_cross_window_rejected():
    bundle = _bundle([_control("T1", [0, 0, 30, 30])])
    locator = HybridLocator()
    with pytest.raises(LocatorError, match="locator_evidence_conflict"):
        locator.resolve(_request(), bundle, LocatorPolicy(mode="semantic_first"),
                        visual_candidates=(_visual([60, 0, 80, 30]),))
    with pytest.raises(LocatorError, match="locator_low_confidence"):
        locator.resolve(LocatorRequest("gui.click", {}, {"name": "Missing"}),
                        bundle, LocatorPolicy(mode="semantic_first"),
                        visual_candidates=(_visual([0, 0, 30, 30], 0.2),))
    with pytest.raises(LocatorError, match="locator_low_confidence"):
        locator.resolve(LocatorRequest("gui.click", {}, {"name": "Missing"}),
                        bundle, LocatorPolicy(mode="semantic_first"),
                        visual_candidates=(_visual([0, 0, 30, 30], native="other"),))


def test_weak_visual_overlap_cannot_mask_strong_conflicting_match():
    bundle = _bundle([_control("T1", [0, 0, 30, 30])])
    with pytest.raises(LocatorError, match="locator_evidence_conflict"):
        HybridLocator().resolve(
            _request(), bundle, LocatorPolicy(mode="semantic_first"),
            visual_candidates=(_visual([0, 0, 30, 30], 0.2),
                               _visual([50, 0, 80, 30], 0.99)),
        )


def test_ambiguous_candidates_never_choose_by_order():
    bundle = _bundle([_control("T1", [0, 0, 30, 30]),
                      _control("T2", [50, 0, 80, 30])])
    for _ in range(20):
        with pytest.raises(LocatorError, match="locator_ambiguous"):
            HybridLocator().resolve(_request(), bundle, LocatorPolicy())


def test_visual_evidence_outside_locked_window_is_rejected():
    bundle = _bundle([])
    with pytest.raises(LocatorError, match="visual_match_outside_window"):
        HybridLocator().resolve(_request(), bundle, LocatorPolicy(mode="visual_only"),
                                visual_candidates=(_visual([101, 0, 120, 20]),))


@pytest.mark.parametrize("confidence", [float("nan"), float("inf"), "bad", True, -0.1, 1.1])
def test_locator_rejects_invalid_visual_confidence_before_ranking(confidence):
    with pytest.raises(LocatorError, match="locator_evidence_invalid"):
        HybridLocator().resolve(
            _request(), _bundle([]), LocatorPolicy(mode="visual_only"),
            visual_candidates=(_visual([0, 0, 30, 30], confidence),),
        )


def test_locator_rejects_invalid_ocr_confidence_before_fusion():
    ocr = {"rect": [0, 0, 30, 30], "text": "Open", "confidence": float("nan"),
           "observation_id": "OBS-1", "window_native_id": "4"}
    with pytest.raises(LocatorError, match="locator_evidence_invalid"):
        HybridLocator().resolve(_request(), _bundle([_control("T1", [0, 0, 30, 30])]),
                                LocatorPolicy(), ocr_boxes=(ocr,))


def test_ocr_cannot_disambiguate_without_redacted_window_capture():
    bundle = _bundle([_control("T1", [0, 0, 30, 30]),
                      _control("T2", [50, 0, 80, 30])])
    bundle["screenshot_scope"] = "screen"
    ocr = {**_visual([50, 0, 80, 30]), "text": "Open"}
    with pytest.raises(LocatorError, match="ocr_window_unverified"):
        HybridLocator().resolve(_request(), bundle, LocatorPolicy(),
                                ocr_boxes=(ocr,))


def test_ocr_box_extending_outside_locked_window_is_rejected():
    bundle = _bundle([_control("T1", [0, 0, 30, 30]),
                      _control("T2", [50, 0, 80, 30])])
    ocr = {**_visual([-10, 0, 25, 30]), "text": "Open"}
    with pytest.raises(LocatorError, match="ocr_box_outside_window"):
        HybridLocator().resolve(_request(), bundle, LocatorPolicy(),
                                ocr_boxes=(ocr,))
