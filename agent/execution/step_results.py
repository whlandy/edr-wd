"""
step_results.py — Atomic writer for `step-results.json` (FR-P1.2-06).

The architecture doc (§9.4 + §11) treats `step-results.json` as the
projection of the live executor state. P1.3 introduces the canonical
trace chain; until then this file is the durable record of step
outcomes per case.

Atomicity is provided by `os.replace` on a temp file written into the
same directory:

    write_atomic(path, payload):
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(...)
        os.replace(tmp, path)

This gives readers one of two outcomes:

    * the previous complete file, or
    * the new complete file.

If the process dies between `write_text` and `os.replace`, the previous
file remains untouched and the `.tmp` is left for forensics.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping


def write_atomic(path: Path, payload: Mapping[str, Any]) -> Path:
    """Serialize `payload` to `path` atomically.

    Returns the destination path on success.
    """
    path = Path(path)
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)

    # Same-directory temp so os.replace is atomic (cross-device moves
    # are not). NamedTemporaryFile would put the temp in /tmp; we
    # want it next to the target.
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp_path, path)
    finally:
        # If os.replace failed before completing the swap, the temp
        # file is still around; clean it up so we don't leak it.
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
    return path


def load_step_results(path: Path) -> dict[str, Any] | None:
    """Read `step-results.json` if present. Returns None if absent."""
    path = Path(path)
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


__all__ = ["write_atomic", "load_step_results"]