"""A bundle cannot return mixed tree/image evidence after window movement."""

from __future__ import annotations

import hashlib

import pytest

from target.windowing.observation import observe_window_bundle
from target.windowing.models import WindowIdentityError


class FakeBackend:
    def __init__(self):
        self.rect = {"x": 10, "y": 20, "w": 100, "h": 80}
        self.moves_during_tree = False

    def verify_window_lock(self, activate=False):
        return {"ok": True, "locked": True}

    def get_window_lock(self):
        return {"lock": {"handle": 4, "pid": 9, "process_name": "Demo",
                         "snapshot": {"handle": 4, "pid": 9, "title": "Demo",
                                      "rectangle": self.rect.copy()}}}

    def dump_tree(self, max_depth=15):
        if self.moves_during_tree:
            self.rect["x"] += 1
        return {"ok": True, "controls": []}


def _capture(backend):
    return b"png-data", {"redacted": True, "origin": [10, 20], "redactions": []}


def test_stable_bundle_has_digest_and_one_window_identity():
    result = observe_window_bundle(FakeBackend(), session_id="s", backend_kind="windows_pywinauto",
                                   run_token=None, capture_image=True, include_ocr=False,
                                   capture_redacted_frame=_capture)
    assert result["ok"]
    assert result["screenshot_sha256"] == "sha256:" + hashlib.sha256(b"png-data").hexdigest()
    assert result["window_identity"]["native_id"] == "4"


def test_window_move_discards_entire_bundle():
    backend = FakeBackend()
    backend.moves_during_tree = True
    with pytest.raises(WindowIdentityError, match="observation_window_changed"):
        observe_window_bundle(backend, session_id="s", backend_kind="windows_pywinauto",
                              run_token=None, capture_image=True, include_ocr=False,
                              capture_redacted_frame=_capture)


def test_ocr_boxes_are_translated_to_screen_and_bound_to_observation():
    result = observe_window_bundle(
        FakeBackend(), session_id="s", backend_kind="windows_pywinauto",
        run_token=None, capture_image=False, include_ocr=True,
        capture_redacted_frame=_capture,
        ocr_extract=lambda png, masks: [{"text": "Open", "rect": [1, 2, 10, 12],
                                         "confidence": .99}],
    )
    assert result["ocr_status"] == "ok"
    assert result["ocr_boxes"][0]["rect"] == [11, 22, 20, 32]
    assert result["ocr_boxes"][0]["observation_id"] == result["observation_id"]
