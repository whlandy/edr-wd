"""Runtime artifact helpers for target-side MCP tools."""

from __future__ import annotations

import os
import re
import time
from pathlib import Path


def _safe_label(label: str | None) -> str:
    if not label:
        return ""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", label.strip())
    return cleaned.strip("-_.")[:80]


def screenshot_path(path: str | None = None, label: str | None = None) -> str:
    """Return an explicit or default project-local screenshot artifact path."""
    if path:
        return path

    ts = time.strftime("%Y%m%d-%H%M%S")
    suffix = _safe_label(label)
    name = f"edr-wd-screenshot-{ts}{('-' + suffix) if suffix else ''}.png"
    root = Path(os.environ.get("EDR_WD_ARTIFACT_DIR", Path.cwd() / "tmp"))
    out_dir = root / "screenshots"
    out_dir.mkdir(parents=True, exist_ok=True)
    return str(out_dir / name)
