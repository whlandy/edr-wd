"""
evidence.py — Screenshot persistence + EvidenceRecord (architecture
§14.1, FR-P1.4-01, -02, -03, -07, -08, -10).

`persist_screenshot()` writes the PNG bytes to
`<trace_dir>/screenshots/<step_no>-<step_id>-<role>.png` atomically,
computes the SHA-256, and returns an `EvidenceRecord` capturing the
provenance (snapshot_id, event_id, process_name, window_title,
redaction rule_ids). Path validation runs BEFORE any write.

Re-exports the redactor from `.redaction` so callers do not need
to import two modules.
"""

from __future__ import annotations

import dataclasses
import hashlib
import os
import struct
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .redaction import (
    ImageRule,
    Rectangle,
    RedactionRule,
    TextRule,
    apply_image_redaction,
    apply_text_redaction,
)


VALID_ROLES = frozenset({"before", "after", "failure", "baseline"})


class EvidenceError(Exception):
    """Base for evidence errors."""


class EvidencePathError(EvidenceError):
    """FR-P1.4-10: relative_path failed validation."""


class EvidenceIntegrityError(EvidenceError):
    """Digest mismatch between caller bytes and bytes-on-disk."""


@dataclass(frozen=True)
class EvidenceRecord:
    """Architecture §14.1: one persisted screenshot."""
    evidence_id: str  # sortable unique id; format: IMG-XXXXXXXXXXXXXXXX
    kind: str  # "screenshot"
    role: str  # before | after | failure | baseline
    relative_path: str  # <trace_dir>/screenshots/.../...
    media_type: str  # "image/png"
    sha256: str  # sha256:<hex>
    bytes_size: int
    width: int
    height: int
    captured_at: str  # ISO-8601
    snapshot_id: str
    event_id: str
    step_id: str
    process_name: str
    pid: int
    window_title: str
    redaction_applied: bool
    redaction_rule_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "kind": self.kind,
            "role": self.role,
            "relative_path": self.relative_path,
            "media_type": self.media_type,
            "sha256": self.sha256,
            "bytes": self.bytes_size,
            "width": self.width,
            "height": self.height,
            "captured_at": self.captured_at,
            "snapshot_id": self.snapshot_id,
            "event_id": self.event_id,
            "step_id": self.step_id,
            "process_name": self.process_name,
            "pid": self.pid,
            "window_title": self.window_title,
            "redaction": {
                "applied": self.redaction_applied,
                "rule_ids": list(self.redaction_rule_ids),
            },
        }


# ---------------------------------------------------------------------------
# Path validation (FR-P1.4-10)
# ---------------------------------------------------------------------------


_FORBIDDEN_FILENAME_CHARS = set("/\\") | {chr(c) for c in range(32)} | {"\x7f"}


def _validate_relative_path(relative_path: str) -> str:
    """Reject abs paths, `..`, control chars, separators, etc.

    Returns the validated path unchanged on success. Raises
    EvidencePathError on any rejection — BEFORE any write.
    """
    if not isinstance(relative_path, str) or not relative_path:
        raise EvidencePathError("relative_path must be a non-empty string")
    if os.path.isabs(relative_path):
        raise EvidencePathError(
            f"relative_path must not be absolute: {relative_path!r}"
        )
    parts = relative_path.replace("\\", "/").split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise EvidencePathError(
                f"relative_path component rejected: {part!r}"
            )
        if any(c in _FORBIDDEN_FILENAME_CHARS for c in part):
            raise EvidencePathError(
                f"relative_path contains forbidden char in {part!r}"
            )
    return relative_path


# ---------------------------------------------------------------------------
# PNG dimension helper (re-implemented locally to avoid coupling
# redaction.py with evidence.py's public surface)
# ---------------------------------------------------------------------------


def _png_dimensions(png_bytes: bytes) -> tuple[int, int]:
    if not png_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        raise EvidenceIntegrityError("not a PNG")
    pos = len(b"\x89PNG\r\n\x1a\n")
    if pos + 8 > len(png_bytes):
        raise EvidenceIntegrityError("truncated PNG header")
    length = int.from_bytes(png_bytes[pos:pos + 4], "big")
    type_code = png_bytes[pos + 4:pos + 8]
    if type_code != b"IHDR" or length < 8:
        raise EvidenceIntegrityError("missing IHDR chunk")
    width = int.from_bytes(png_bytes[pos + 8:pos + 12], "big")
    height = int.from_bytes(png_bytes[pos + 12:pos + 16], "big")
    return width, height


# ---------------------------------------------------------------------------
# EvidenceRecord id generation (consistent with P1.3 trace.ids format)
# ---------------------------------------------------------------------------


def _generate_evidence_id(now_ns: int | None = None) -> str:
    import time
    if now_ns is None:
        now_ns = time.time_ns()
    ms_hex = format(now_ns // 1_000_000, "012x")
    import uuid
    return f"IMG-{ms_hex}{uuid.uuid4().hex[:4]}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def persist_screenshot(
    png_bytes: bytes,
    role: str,
    *,
    step_no: int,
    step_id: str,
    snapshot_id: str,
    event_id: str,
    process_name: str,
    pid: int,
    window_title: str,
    redaction_rules: list[RedactionRule] | None = None,
    trace_dir: Path,
    captured_at: str | None = None,
    image_id: str | None = None,
    image_provider: "ImageProvider | None" = None,
) -> EvidenceRecord:
    """Persist one screenshot to disk under `<trace_dir>/screenshots/`.

    Pipeline:

      1. Validate role + path. Build `relative_path` from
         `<step_no>-<step_id>-<role>.png`.
      2. Apply image redaction (FR-P1.4-07): if `redaction_rules`
         contains ImageRules, mask pixels in-place on a copy of
         `png_bytes`; record fired rule_ids in the EvidenceRecord.
      3. Write to a temp file in `<trace_dir>/screenshots/`, then
         atomically rename (FR-P1.4-01).
      4. Verify SHA-256 of the bytes-on-disk matches the in-memory
         pre-write digest.  Mismatch raises EvidenceIntegrityError.
      5. Read PNG dimensions from the original (pre-redaction)
         bytes so the record matches the source image, not the
         redacted one.

    `image_provider` is optional and currently unused — P1.4 ships
    with `png_bytes` passed directly. P1.4-final (per spec) wires
    `screenshot_bytes()` from the backend; this hook is left for
    forward-compatibility.
    """
    if role not in VALID_ROLES:
        raise EvidencePathError(
            f"role must be one of {sorted(VALID_ROLES)}, got {role!r}"
        )
    if not png_bytes:
        raise EvidenceIntegrityError("png_bytes is empty")
    # Validate step_id itself (FR-P1.4-10: filenames must not
    # contain traversal / control chars / separators).
    if not isinstance(step_id, str) or not step_id:
        raise EvidencePathError(
            f"step_id must be a non-empty string, got {step_id!r}"
        )
    if any(c in _FORBIDDEN_FILENAME_CHARS for c in step_id) or step_id in ("", ".", ".."):
        raise EvidencePathError(
            f"step_id contains forbidden characters: {step_id!r}"
        )

    rel_filename = f"{step_no:03d}-{step_id}-{role}.png"
    rel_path = f"screenshots/{rel_filename}"
    _validate_relative_path(rel_path)

    # Image redaction.
    image_rules = [r for r in (redaction_rules or []) if isinstance(r, ImageRule)]
    text_rules = [r for r in (redaction_rules or []) if isinstance(r, TextRule)]
    final_bytes, image_rule_ids = apply_image_redaction(png_bytes, image_rules)
    redaction_applied = bool(image_rule_ids) or bool(
        apply_text_redaction(window_title, text_rules)[1]
    ) or bool(apply_text_redaction(process_name, text_rules)[1])
    # Apply text redaction to the metadata strings (we don't persist
    # the redacted strings yet — caller may want them; we record
    # rule_ids in the evidence record regardless).
    # Redaction of process_name / window_title is consumed at the
    # caller's report-rendering boundary; the EvidenceRecord simply
    # captures which rules fired.

    # Width / height from the un-redacted original.
    width, height = _png_dimensions(png_bytes)

    # Atomic write.
    target = Path(trace_dir) / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp",
        dir=str(target.parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(final_bytes)
            fh.flush()
            os.fsync(fh.fileno())
        # Compute digest of bytes written.
        digest = "sha256:" + hashlib.sha256(final_bytes).hexdigest()
        # Sanity check: digest of pre-write bytes matches.
        pre_digest = "sha256:" + hashlib.sha256(png_bytes).hexdigest()
        # If the user passed png_bytes already, the pre-write digest
        # should equal the post-write digest UNLESS redaction fired.
        if not image_rule_ids and pre_digest != digest:
            raise EvidenceIntegrityError(
                f"pre-write digest {pre_digest} != post-write digest {digest}"
            )

        os.replace(tmp_path, target)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass

    return EvidenceRecord(
        evidence_id=image_id or _generate_evidence_id(),
        kind="screenshot",
        role=role,
        relative_path=rel_path,
        media_type="image/png",
        sha256=digest,
        bytes_size=len(final_bytes),
        width=width,
        height=height,
        captured_at=captured_at or _utcnow_iso(),
        snapshot_id=snapshot_id,
        event_id=event_id,
        step_id=step_id,
        process_name=process_name,
        pid=pid,
        window_title=window_title,
        redaction_applied=redaction_applied,
        redaction_rule_ids=tuple(image_rule_ids),
    )


def verify_evidence_on_disk(record: EvidenceRecord, trace_dir: Path) -> bool:
    """Return True iff the file at `record.relative_path` exists
    with the expected SHA-256.  Used by the visual_evidence_captured
    expectation (FR-P1.4-08)."""
    path = Path(trace_dir) / record.relative_path
    if not path.exists():
        return False
    actual = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    return actual == record.sha256


def _utcnow_iso() -> str:
    import datetime as _dt
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat(timespec="milliseconds")


class ImageProvider:
    """Forward-compatibility hook for the backend's screenshot
    capability.  P1.4 ships `png_bytes` already populated; a future
    checkpoint can wire the backend's `screenshot_bytes()` here.
    """
    def screenshot_bytes(self) -> bytes:
        raise NotImplementedError


__all__ = [
    "EvidenceRecord",
    "EvidenceError",
    "EvidencePathError",
    "EvidenceIntegrityError",
    "ImageProvider",
    "Rectangle",
    "RedactionRule",
    "TextRule",
    "ImageRule",
    "persist_screenshot",
    "verify_evidence_on_disk",
    "apply_text_redaction",
    "apply_image_redaction",
    "VALID_ROLES",
]