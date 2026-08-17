"""Dependency-light, scale-aware Pillow template matcher."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence
from io import BytesIO

from PIL import Image, ImageChops, ImageStat

from .visual import VisualCandidate


def _confidence(region: Image.Image, template: Image.Image) -> float:
    difference = ImageChops.difference(region, template)
    mean = ImageStat.Stat(difference).mean[0]
    return max(0.0, 1.0 - float(mean) / 255.0)


def _iou(left: tuple[int, int, int, int], right: tuple[int, int, int, int]) -> float:
    x0, y0 = max(left[0], right[0]), max(left[1], right[1])
    x1, y1 = min(left[2], right[2]), min(left[3], right[3])
    intersection = max(0, x1 - x0) * max(0, y1 - y0)
    if not intersection:
        return 0.0
    left_area = (left[2] - left[0]) * (left[3] - left[1])
    right_area = (right[2] - right[0]) * (right[3] - right[1])
    return intersection / float(left_area + right_area - intersection)


class PillowTemplateMatcher:
    def __init__(
        self,
        *,
        scales: Sequence[float] = (0.75, 1.0, 1.25, 1.5),
        max_candidates: int = 8,
    ) -> None:
        self.scales = tuple(scales)
        self.max_candidates = max_candidates

    def __call__(self, template_path: str, observation: Any) -> list[VisualCandidate]:
        screenshot_path = (
            observation.get("screenshot_path")
            if isinstance(observation, Mapping)
            else getattr(observation, "screenshot_path", None)
        )
        screenshot_bytes = (
            observation.get("screenshot_bytes")
            if isinstance(observation, Mapping)
            else getattr(observation, "screenshot_bytes", None)
        )
        if screenshot_bytes is not None:
            screenshot = Image.open(BytesIO(screenshot_bytes)).convert("L")
        elif screenshot_path:
            screenshot = Image.open(Path(screenshot_path)).convert("L")
        else:
            return []
        origin = (
            observation.get("screenshot_origin", [0, 0])
            if isinstance(observation, Mapping)
            else getattr(observation, "screenshot_origin", [0, 0])
        )
        if not isinstance(origin, (list, tuple)) or len(origin) != 2:
            origin = [0, 0]
        offset_x, offset_y = int(origin[0]), int(origin[1])
        template_source = Image.open(Path(template_path)).convert("L")
        scored: list[VisualCandidate] = []
        for scale in self.scales:
            width = max(1, round(template_source.width * scale))
            height = max(1, round(template_source.height * scale))
            if width > screenshot.width or height > screenshot.height:
                continue
            template = template_source.resize((width, height), Image.Resampling.LANCZOS)
            stride = max(2, min(width, height) // 4)
            coarse = []
            for y in range(0, screenshot.height - height + 1, stride):
                for x in range(0, screenshot.width - width + 1, stride):
                    score = _confidence(screenshot.crop((x, y, x + width, y + height)), template)
                    coarse.append((score, x, y))
            for _, coarse_x, coarse_y in sorted(coarse, reverse=True)[:5]:
                x_start, x_end = max(0, coarse_x - stride), min(screenshot.width - width, coarse_x + stride)
                y_start, y_end = max(0, coarse_y - stride), min(screenshot.height - height, coarse_y + stride)
                best = (0.0, coarse_x, coarse_y)
                for y in range(y_start, y_end + 1):
                    for x in range(x_start, x_end + 1):
                        score = _confidence(screenshot.crop((x, y, x + width, y + height)), template)
                        if score > best[0]:
                            best = (score, x, y)
                scored.append(VisualCandidate(
                    (
                        best[1] + offset_x, best[2] + offset_y,
                        best[1] + width + offset_x, best[2] + height + offset_y,
                    ),
                    best[0],
                    scale,
                ))
        distinct = []
        for candidate in sorted(scored, key=lambda item: item.confidence, reverse=True):
            if any(_iou(candidate.rect, prior.rect) > 0.5 for prior in distinct):
                continue
            distinct.append(candidate)
            if len(distinct) >= self.max_candidates:
                break
        return distinct


__all__ = ["PillowTemplateMatcher"]
