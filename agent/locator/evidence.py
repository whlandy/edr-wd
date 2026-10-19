"""Align evidence only within one observation and one window rectangle."""

from __future__ import annotations

from typing import Any, Mapping


def _rect(raw: Any) -> tuple[int, int, int, int] | None:
    if isinstance(raw, (list, tuple)) and len(raw) == 4:
        return tuple(map(int, raw))
    if isinstance(raw, Mapping):
        x = int(raw.get("left", raw.get("x", 0)))
        y = int(raw.get("top", raw.get("y", 0)))
        return (x, y, int(raw.get("right", x + int(raw.get("w", raw.get("width", 0))))),
                int(raw.get("bottom", y + int(raw.get("h", raw.get("height", 0))))))
    return None


def overlap_ratio(left, right) -> float:
    a, b = _rect(left), _rect(right)
    if a is None or b is None:
        return 0.0
    area = max(0, min(a[2], b[2]) - max(a[0], b[0])) * max(0, min(a[3], b[3]) - max(a[1], b[1]))
    base = max(1, (a[2] - a[0]) * (a[3] - a[1]))
    return area / base


def align_evidence(candidates: tuple, templates: tuple, ocr_boxes: tuple,
                   *, observation_id: str, window_native_id: str,
                   min_overlap: float = 0.4) -> dict[str, tuple]:
    aligned = {}
    for candidate in candidates:
        if candidate.observation_id != observation_id or candidate.window_native_id != window_native_id:
            continue
        matches = []
        for kind, items in (("visual", templates), ("ocr", ocr_boxes)):
            for item in items:
                if item.get("observation_id") != observation_id or item.get("window_native_id") != window_native_id:
                    continue
                if overlap_ratio(candidate.rect, item.get("rect")) >= min_overlap:
                    matches.append((kind, item))
        aligned[candidate.target_id] = tuple(matches)
    return aligned
