"""
test_pointer_result_envelope.py — P0.3: Unified Pointer Result Envelope.

Locks in the P0.3 contract (docs/todo/window-scoped-scroll-and-verification.md):

  1. Raw pointer results expose at least ``event_dispatched``.
  2. Verified composites expose ``dispatched`` / ``moved`` / ``reason``.
  3. Callers must NOT present raw ``ok=true`` as user-visible scroll success —
     only a verified composite with ``moved=True`` is a success.
  4. Failure states have stable machine-readable codes:
     target_occluded / target_ambiguous / verification_unavailable /
     no_effect / not_dispatched.
"""

from __future__ import annotations

import json

import pytest

from scroll.pointer_result import (
    CODE_NO_EFFECT,
    CODE_NOT_DISPATCHED,
    CODE_OK,
    CODE_TARGET_AMBIGUOUS,
    CODE_TARGET_OCCLUDED,
    CODE_VERIFICATION_UNAVAILABLE,
    STABLE_POINTER_CODES,
    code_for,
    event_dispatched_for,
    is_verified_success,
    normalize,
    reason_to_code,
)
from scroll.results import Reason, ScrollResult, Strategy


pytestmark = pytest.mark.unit  # contract/unit — offline, no live target needed


# ── Acceptance #4: stable machine-readable codes ───────────────────────────

def test_required_failure_codes_present():
    for code in (
        CODE_TARGET_OCCLUDED,
        CODE_TARGET_AMBIGUOUS,
        CODE_VERIFICATION_UNAVAILABLE,
        CODE_NO_EFFECT,
        CODE_NOT_DISPATCHED,
    ):
        assert code in STABLE_POINTER_CODES
        assert isinstance(code, str) and "_" in code


def test_all_codes_lower_snake_and_unique():
    assert len(STABLE_POINTER_CODES) == len({c for c in STABLE_POINTER_CODES})
    for c in STABLE_POINTER_CODES:
        assert c == c.lower()
        assert " " not in c


# ── Acceptance #1: raw results expose event_dispatched ─────────────────────

def test_normalize_injects_envelope_fields():
    raw = {"ok": True, "method": "pyautogui.scroll", "clicks": -3}
    out = normalize(raw, tool="scroll")
    assert out["event_dispatched"] is True
    assert out["ok"] is True
    assert out["code"] == CODE_OK
    assert out["tool"] == "scroll"
    assert out["scope"] == "screen_unscoped"
    # additive: original keys preserved
    assert out["clicks"] == -3


def test_normalize_window_scope():
    raw = {"ok": True, "clicks": -2, "scope": "window"}
    out = normalize(raw, tool="scroll_window", scope="window")
    assert out["event_dispatched"] is True
    assert out["ok"] is True
    assert out["scope"] == "window"


def test_dry_run_is_not_an_actual_dispatch():
    # A backend reports ok=True but method=dry_run — no real OS event sent.
    raw = {"ok": True, "method": "dry_run", "clicks": 5}
    out = normalize(raw, tool="scroll")
    assert out["ok"] is True
    assert out["event_dispatched"] is False


def test_failed_payload_not_dispatched():
    raw = {"ok": False, "error": "boom"}
    out = normalize(raw, tool="scroll")
    assert out["ok"] is False
    assert out["event_dispatched"] is False
    assert out["code"] == CODE_NOT_DISPATCHED


def test_explicit_event_dispatched_wins():
    out = normalize({"ok": False, "event_dispatched": True, "error": "x"},
                    tool="scroll")
    assert out["event_dispatched"] is True


# ── Acceptance #4 detail: code derivation ──────────────────────────────────

def test_code_for_occluded():
    assert code_for({"ok": False, "code": "target_occluded"}) == CODE_TARGET_OCCLUDED
    assert code_for({"ok": False, "error": "target is occluded by overlay"}) == CODE_TARGET_OCCLUDED


def test_code_for_ambiguous():
    assert code_for({"ok": False, "code": "target_ambiguous"}) == CODE_TARGET_AMBIGUOUS
    assert code_for({"ok": False, "error": "window match is ambiguous"}) == CODE_TARGET_AMBIGUOUS


def test_code_for_ok_when_success():
    assert code_for({"ok": True, "method": "pyautogui.scroll"}) == CODE_OK


# ── Acceptance #3: raw ok is NOT user-visible success ──────────────────────

def test_raw_ok_alone_is_not_verified_success():
    # A raw primitive says "sent ok" but that never means the content moved.
    raw = {"ok": True, "event_dispatched": True, "clicks": -3}
    assert is_verified_success(raw) is False


def test_verified_success_requires_moved_and_dispatched():
    assert is_verified_success(
        {"dispatched": True, "moved": True, "reason": "wheel_moved"}
    ) is True
    assert is_verified_success(
        {"dispatched": True, "moved": False, "reason": "no_scroll_effect"}
    ) is False
    assert is_verified_success(
        {"dispatched": False, "moved": False, "reason": "not_dispatched"}
    ) is False


# ── Acceptance #2: verified composites expose dispatched/moved/reason ──────

def test_scroll_result_dict_has_envelope_fields():
    r = ScrollResult.success_result(Strategy.WHEEL)
    d = r.to_dict()
    assert d["dispatched"] is True
    assert d["event_dispatched"] is True
    assert d["moved"] is True
    assert d["reason"] == "wheel_moved"
    assert d["code"] == CODE_OK


def test_scroll_result_no_effect_code():
    d = ScrollResult.no_effect().to_dict()
    assert d["dispatched"] is True
    assert d["moved"] is False
    assert d["reason"] == "no_scroll_effect"
    assert d["code"] == CODE_NO_EFFECT


def test_scroll_result_not_dispatched_code():
    d = ScrollResult.not_dispatched().to_dict()
    assert d["dispatched"] is False
    assert d["event_dispatched"] is False
    assert d["reason"] == "not_dispatched"
    assert d["code"] == CODE_NOT_DISPATCHED


# ── reason_to_code mapping ─────────────────────────────────────────────────

def test_reason_to_code_mapping():
    assert reason_to_code("wheel_moved") == CODE_OK
    assert reason_to_code("no_scroll_effect") == CODE_NO_EFFECT
    assert reason_to_code("not_dispatched") == CODE_NOT_DISPATCHED
    assert reason_to_code("verification_unavailable") == CODE_VERIFICATION_UNAVAILABLE
    assert reason_to_code("target_occluded") == CODE_TARGET_OCCLUDED
    assert reason_to_code("target_ambiguous") == CODE_TARGET_AMBIGUOUS


# ── Server MCP tools emit the envelope ─────────────────────────────────────

def test_server_scroll_tool_emits_event_dispatched(monkeypatch):
    import target.server as srv

    class FakeBackend:
        def scroll(self, clicks, x=None, y=None):
            return {"ok": True, "method": "pyautogui.scroll", "clicks": clicks}

    monkeypatch.setattr(srv, "_backend", FakeBackend())
    payload = json.loads(srv.scroll(-3))
    assert payload["ok"] is True
    assert payload["event_dispatched"] is True
    assert payload["code"] == CODE_OK
    assert payload["tool"] == "scroll"


def test_server_scroll_window_tool_emits_envelope(monkeypatch):
    import target.server as srv

    class FakeBackend:
        def scroll_window(self, clicks, x, y, **kw):
            return {"ok": True, "clicks": clicks, "scope": "window"}

    monkeypatch.setattr(srv, "_backend", FakeBackend())
    payload = json.loads(srv.scroll_window(-2, 5, 6, window_title_re="x"))
    assert payload["ok"] is True
    assert payload["event_dispatched"] is True
    assert payload["tool"] == "scroll_window"
    assert payload["scope"] == "window"


def test_server_scroll_window_occluded_code_passthrough(monkeypatch):
    import target.server as srv

    class FakeBackend:
        def scroll_window(self, clicks, x, y, **kw):
            return {"ok": False, "code": "target_occluded", "error": "occluded"}

    monkeypatch.setattr(srv, "_backend", FakeBackend())
    payload = json.loads(srv.scroll_window(3, 600, 400, window_title_re="x"))
    assert payload["ok"] is False
    assert payload["code"] == CODE_TARGET_OCCLUDED
    assert payload["event_dispatched"] is False


def test_exported_from_package():
    import scroll as pkg
    assert pkg.normalize_pointer_result is normalize
    assert pkg.CODE_TARGET_OCCLUDED == CODE_TARGET_OCCLUDED
    assert pkg.is_verified_success({"dispatched": True, "moved": True}) is True
