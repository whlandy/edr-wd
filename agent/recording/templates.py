"""Redacted visual-template generation for protected replay fallback."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PIL import Image, ImageDraw

from agent.execution import StepMaterializationError


Rect = tuple[int, int, int, int]


@dataclass(frozen=True)
class TemplateArtifacts:
    element: Path
    context: Path
    element_sha256: str
    context_sha256: str
    relative_point: tuple[float, float]


def _overlap(left: Rect, right: Rect) -> bool:
    return max(left[0], right[0]) < min(left[2], right[2]) and max(left[1], right[1]) < min(left[3], right[3])


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def generate_redacted_templates(
    screenshot: str | Path,
    output_directory: str | Path,
    *,
    step_id: str,
    element_rect: Rect,
    redactions: Iterable[Rect] = (),
    context_padding: int = 48,
    relative_point: tuple[float, float] = (0.5, 0.5),
) -> TemplateArtifacts:
    """Redact first, then crop element/context templates atomically enough for compilation."""
    source = Image.open(Path(screenshot)).convert("RGB")
    width, height = source.size
    x0, y0, x1, y1 = element_rect
    if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
        raise StepMaterializationError(
            "visual_template_rect_invalid", "element rectangle is outside the screenshot"
        )
    redaction_list = [tuple(int(value) for value in rect) for rect in redactions]
    if any(_overlap(element_rect, rect) for rect in redaction_list):
        raise StepMaterializationError(
            "visual_template_target_redacted",
            "redaction overlaps the target element; visual fallback is forbidden",
        )
    redacted = source.copy()
    painter = ImageDraw.Draw(redacted)
    for rect in redaction_list:
        painter.rectangle(rect, fill=(0, 0, 0))
    context_rect = (
        max(0, x0 - context_padding), max(0, y0 - context_padding),
        min(width, x1 + context_padding), min(height, y1 + context_padding),
    )
    output = Path(output_directory).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    element_path = output / f"{step_id}-element.png"
    context_path = output / f"{step_id}-context.png"
    redacted.crop(element_rect).save(element_path, format="PNG")
    redacted.crop(context_rect).save(context_path, format="PNG")
    return TemplateArtifacts(
        element=element_path,
        context=context_path,
        element_sha256=_digest(element_path),
        context_sha256=_digest(context_path),
        relative_point=relative_point,
    )


__all__ = ["TemplateArtifacts", "generate_redacted_templates"]

