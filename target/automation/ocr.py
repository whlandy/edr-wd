"""OCR on an already source-redacted, window-only PNG.

The default engine is optional. If Tesseract is unavailable, callers receive
``ocr_unsupported`` and must not silently use an unredacted screenshot.
"""

from __future__ import annotations

import io
import math
from typing import Callable, Mapping

from PIL import Image


def _intersects(a, b) -> bool:
    return max(a[0], b[0]) < min(a[2], b[2]) and max(a[1], b[1]) < min(a[3], b[3])


def _tesseract(image: Image.Image) -> list[dict]:
    try:
        import pytesseract
    except ImportError as exc:
        raise ValueError("ocr_unsupported") from exc
    try:
        data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
    except (OSError, getattr(pytesseract, "TesseractNotFoundError", OSError)) as exc:
        raise ValueError("ocr_unsupported") from exc
    out = []
    for index, text in enumerate(data.get("text", ())):
        if not str(text).strip():
            continue
        try:
            raw_confidence = float(data["conf"][index])
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        if not math.isfinite(raw_confidence):
            continue
        confidence = max(0.0, min(1.0, raw_confidence / 100.0))
        left, top = int(data["left"][index]), int(data["top"][index])
        out.append({"text": str(text), "rect": [left, top,
                    left + int(data["width"][index]), top + int(data["height"][index])],
                    "confidence": confidence})
    return out


def extract_redacted_text(window_png: bytes, masks: tuple,
                          *, engine: Callable[[Image.Image], list[Mapping]] | None = None) -> list[dict]:
    if not isinstance(window_png, bytes) or not window_png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("ocr_image_invalid")
    image = Image.open(io.BytesIO(window_png)).convert("RGB")
    width, height = image.size
    redactions = tuple(tuple(map(int, mask)) for mask in masks)
    # Reject a caller that claims masks without actually painting them.
    for left, top, right, bottom in redactions:
        if not (0 <= left < right <= width and 0 <= top < bottom <= height):
            raise ValueError("ocr_mask_invalid")
        crop = image.crop((left, top, right, bottom))
        pixels = crop.get_flattened_data() if hasattr(crop, "get_flattened_data") else crop.getdata()
        if any(pixel != (0, 0, 0) for pixel in pixels):
            raise ValueError("ocr_image_unredacted")
    raw = (engine or _tesseract)(image)
    safe = []
    for item in raw:
        rect = tuple(map(int, item.get("rect") or ()))
        if len(rect) != 4 or not (0 <= rect[0] < rect[2] <= width and
                                 0 <= rect[1] < rect[3] <= height):
            continue
        if any(_intersects(rect, mask) for mask in redactions):
            continue
        raw_confidence = item.get("confidence", 0)
        if isinstance(raw_confidence, bool):
            continue
        try:
            confidence = float(raw_confidence)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(confidence) or not 0 <= confidence <= 1:
            continue
        safe.append({"text": str(item.get("text") or ""), "rect": list(rect),
                     "confidence": confidence, "language": item.get("language") or "unknown"})
    return safe
