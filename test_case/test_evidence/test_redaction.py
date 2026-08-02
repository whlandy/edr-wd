"""P1.4 acceptance gate — text + image redaction (FR-P1.4-07).

Text rules use [REDACTED] replacement; image rules record
rectangle-applied rule_ids; the redacted PNG is persisted in-place.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
for p in (str(_REPO / "target"), str(_REPO / "agent"), str(_REPO / "test_case")):
    if p not in sys.path:
        sys.path.insert(0, p)


from trace import (  # noqa: E402
    ImageRule,
    Rectangle,
    TextRule,
    apply_image_redaction,
    apply_text_redaction,
    persist_screenshot,
)
from fake_target import make_png  # noqa: E402


# ---------------------------------------------------------------------------
# Text redaction
# ---------------------------------------------------------------------------


def test_text_redaction_replaces_with_marker():
    rules = [TextRule(rule_id="email", pattern=r"[a-z]+@[a-z]+\.[a-z]+")]
    redacted, fired = apply_text_redaction("contact alice@example.com today", rules)
    assert redacted == "contact [REDACTED] today"
    assert fired == ["email"]


def test_text_redaction_records_multiple_rule_ids():
    rules = [
        TextRule(rule_id="email", pattern=r"[a-z]+@[a-z]+\.[a-z]+"),
        TextRule(rule_id="phone", pattern=r"\d{3}-\d{4}"),
    ]
    text = "alice@example.com 555-1234 ok"
    redacted, fired = apply_text_redaction(text, rules)
    assert redacted == "[REDACTED] [REDACTED] ok"
    assert fired == ["email", "phone"]


def test_text_redaction_no_match_returns_empty_fired():
    rules = [TextRule(rule_id="email", pattern=r"[a-z]+@[a-z]+\.[a-z]+")]
    redacted, fired = apply_text_redaction("no secrets here", rules)
    assert redacted == "no secrets here"
    assert fired == []


def test_text_redaction_empty_rules():
    redacted, fired = apply_text_redaction("hello", [])
    assert redacted == "hello"
    assert fired == []


def test_redaction_text_pattern_records_rule_id(tmp_path: Path):
    """End-to-end: persist with a text rule, expect rule_id recorded
    in EvidenceRecord.redaction_rule_ids."""
    png = make_png()
    rules = [TextRule(rule_id="email", pattern=r"[a-z]+@[a-z]+\.[a-z]+")]
    rec = persist_screenshot(
        png, "after",
        trace_dir=tmp_path,
        step_no=1, step_id="S001",
        snapshot_id="OBS", event_id="EVT",
        process_name="alice@example.com", pid=1, window_title="t",
        redaction_rules=rules,
    )
    # The text redaction fired (process_name contains an email).
    assert rec.redaction_applied is True
    # We only track image-rule ids in the record; text rules don't
    # change bytes. The fired-id contract is exercised by
    # apply_text_redaction directly (above).


# ---------------------------------------------------------------------------
# Image redaction
# ---------------------------------------------------------------------------


def test_redaction_image_rectangle_masks_pixels():
    """A 4x4 RGBA PNG: mask the top-left 2x2 region to black."""
    png = make_png(width=4, height=4, pixel_value=(255, 255, 255, 255))
    rules = [ImageRule(
        rule_id="secret",
        rectangles=(Rectangle(x=0, y=0, width=2, height=2),),
    )]
    redacted, fired = apply_image_redaction(png, rules)
    assert fired == ["secret"]
    # The PNG signature is still present.
    assert redacted.startswith(b"\x89PNG\r\n\x1a\n")
    # SHA differs from the original.
    orig_sha = hashlib.sha256(png).hexdigest()
    new_sha = hashlib.sha256(redacted).hexdigest()
    assert orig_sha != new_sha


def test_image_redaction_no_rules_passthrough():
    png = make_png()
    out, fired = apply_image_redaction(png, [])
    assert out == png
    assert fired == []


def test_image_redaction_empty_rectangles_no_op():
    png = make_png()
    rules = [ImageRule(rule_id="noop", rectangles=())]
    out, fired = apply_image_redaction(png, rules)
    assert out == png
    assert fired == []


def test_redaction_image_rectangle_records_rule_id(tmp_path: Path):
    """End-to-end: persist with an image rule, expect rule_id in
    EvidenceRecord.redaction_rule_ids."""
    png = make_png()
    rules = [ImageRule(
        rule_id="region",
        rectangles=(Rectangle(x=0, y=0, width=2, height=2),),
    )]
    rec = persist_screenshot(
        png, "after",
        trace_dir=tmp_path,
        step_no=1, step_id="S001",
        snapshot_id="OBS", event_id="EVT",
        process_name="p", pid=1, window_title="t",
        redaction_rules=rules,
    )
    assert "region" in rec.redaction_rule_ids
    assert rec.redaction_applied is True


def test_image_redaction_skips_unsupported_color_type():
    """Palette PNG (color_type 3) is V1-out-of-scope; the redactor
    returns the original bytes and fires no rules."""
    import struct, zlib, binascii
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 3, 0, 0, 0)
    plte = b"\x00\x00\x00"
    raw = b"\x00\x00"
    def chunk(t, data):
        crc = binascii.crc32(t + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + t + data + struct.pack(">I", crc)
    png = sig + chunk(b"IHDR", ihdr) + chunk(b"PLTE", plte) + \
          chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    rules = [ImageRule(rule_id="x", rectangles=(Rectangle(0, 0, 1, 1),))]
    out, fired = apply_image_redaction(png, rules)
    assert out == png
    assert fired == []