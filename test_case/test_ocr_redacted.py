"""OCR must reject unredacted masks and never emit protected text."""

from __future__ import annotations

import io
import sys
import types

import pytest
from PIL import Image

from target.automation.ocr import extract_redacted_text


def _png(masked=True):
    image = Image.new("RGB", (40, 20), (255, 255, 255))
    if masked:
        for x in range(20, 30):
            for y in range(0, 10):
                image.putpixel((x, y), (0, 0, 0))
    output = io.BytesIO()
    image.save(output, "PNG")
    return output.getvalue()


def test_ocr_keeps_only_in_window_nonmasked_boxes():
    def fake_engine(image):
        return [{"text": "Open", "rect": [0, 0, 10, 10], "confidence": .99},
                {"text": "secret", "rect": [20, 0, 30, 10], "confidence": .99},
                {"text": "outside", "rect": [39, 0, 50, 10], "confidence": .99}]
    found = extract_redacted_text(_png(), ((20, 0, 30, 10),), engine=fake_engine)
    assert [item["text"] for item in found] == ["Open"]


def test_ocr_rejects_unpainted_mask():
    with pytest.raises(ValueError, match="ocr_image_unredacted"):
        extract_redacted_text(_png(masked=False), ((20, 0, 30, 10),), engine=lambda image: [])


def test_ocr_binary_absence_is_explicitly_unsupported(monkeypatch):
    class MissingTesseract(OSError):
        pass

    fake = types.SimpleNamespace(
        TesseractNotFoundError=MissingTesseract,
        Output=types.SimpleNamespace(DICT="dict"),
        image_to_data=lambda *args, **kwargs: (_ for _ in ()).throw(MissingTesseract()),
    )
    monkeypatch.setitem(sys.modules, "pytesseract", fake)
    with pytest.raises(ValueError, match="ocr_unsupported"):
        extract_redacted_text(_png(), ())


def test_ocr_ignores_nonfinite_or_non_numeric_confidence():
    scores = [float("nan"), float("inf"), "bad", True, 0.99]
    found = extract_redacted_text(
        _png(), (), engine=lambda _image: [
            {"text": str(index), "rect": [0, 0, 10, 10], "confidence": score}
            for index, score in enumerate(scores)
        ],
    )
    assert [item["text"] for item in found] == ["4"]


def test_tesseract_nan_score_is_not_clamped_to_perfect_confidence(monkeypatch):
    fake = types.SimpleNamespace(
        Output=types.SimpleNamespace(DICT="dict"),
        image_to_data=lambda *_args, **_kwargs: {
            "text": ["invalid", "valid"], "conf": ["nan", "99"],
            "left": [0, 10], "top": [0, 0], "width": [8, 8], "height": [8, 8],
        },
    )
    monkeypatch.setitem(sys.modules, "pytesseract", fake)
    found = extract_redacted_text(_png(), ())
    assert [item["text"] for item in found] == ["valid"]
