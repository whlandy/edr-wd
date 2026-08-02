"""P1.4 acceptance gate — EvidenceRecord + persist_screenshot.

FR-P1.4-01 (digest matches on-disk bytes),
FR-P1.4-10 (path validation rejects traversal / control chars).
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
    EvidenceIntegrityError,
    EvidencePathError,
    EvidenceRecord,
    VALID_ROLES,
    persist_screenshot,
    verify_evidence_on_disk,
)
from fake_target import make_png  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_kwargs():
    return dict(
        step_no=1,
        step_id="S001",
        snapshot_id="OBS-1",
        event_id="EVT-1",
        process_name="EDRClient.exe",
        pid=1234,
        window_title="EDR Home",
    )


# ---------------------------------------------------------------------------
# FR-P1.4-01: digest matches bytes-on-disk
# ---------------------------------------------------------------------------


def test_after_screenshot_persists_with_digest(tmp_path: Path):
    png = make_png(width=8, height=8)
    rec = persist_screenshot(
        png, "after",
        trace_dir=tmp_path,
        **_make_kwargs(),
    )
    assert rec.role == "after"
    assert rec.relative_path == "screenshots/001-S001-after.png"
    assert rec.bytes_size == len(png)
    # Digest matches the bytes on disk.
    on_disk = (tmp_path / rec.relative_path).read_bytes()
    expected = "sha256:" + hashlib.sha256(on_disk).hexdigest()
    assert rec.sha256 == expected
    assert verify_evidence_on_disk(rec, tmp_path)


def test_baseline_role_persists(tmp_path: Path):
    png = make_png()
    rec = persist_screenshot(
        png, "baseline",
        trace_dir=tmp_path,
        step_no=0, step_id="S000",
        snapshot_id="OBS-0", event_id="EVT-0",
        process_name="p", pid=1, window_title="t",
    )
    assert rec.role == "baseline"
    assert rec.relative_path == "screenshots/000-S000-baseline.png"


def test_failure_role_persists(tmp_path: Path):
    png = make_png()
    rec = persist_screenshot(
        png, "failure",
        trace_dir=tmp_path,
        step_no=2, step_id="S002",
        snapshot_id="OBS-2", event_id="EVT-2",
        process_name="p", pid=1, window_title="t",
    )
    assert rec.role == "failure"
    assert "failure" in rec.relative_path


def test_invalid_role_rejected(tmp_path: Path):
    with pytest.raises(EvidencePathError, match="role"):
        persist_screenshot(
            make_png(), "midflight",
            trace_dir=tmp_path, **_make_kwargs(),
        )


# ---------------------------------------------------------------------------
# Width / height from PNG header
# ---------------------------------------------------------------------------


def test_record_carries_png_dimensions(tmp_path: Path):
    png = make_png(width=12, height=34)
    rec = persist_screenshot(
        png, "after", trace_dir=tmp_path, **_make_kwargs(),
    )
    assert rec.width == 12
    assert rec.height == 34
    assert rec.media_type == "image/png"


# ---------------------------------------------------------------------------
# FR-P1.4-10: path validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_role", ["", "../etc", "after/", "/etc/passwd"])
def test_invalid_role_rejected_for_path_validation(tmp_path, bad_role):
    """Various bad role strings are rejected before any write."""
    with pytest.raises(EvidencePathError):
        persist_screenshot(
            make_png(), bad_role,
            trace_dir=tmp_path, **_make_kwargs(),
        )


def test_path_validation_allows_dotdot_in_step_id_substring(tmp_path: Path):
    """Pre-fix: step_id=".." is folded into a filename component.
    The relative_path validation accepts it as long as it is not
    a full path component. Review #1 adds explicit step_id check."""
    rec = persist_screenshot(
        make_png(), "after",
        trace_dir=tmp_path,
        step_no=1, step_id="..",
        snapshot_id="OBS", event_id="EVT",
        process_name="p", pid=1, window_title="t",
    )
    assert rec.relative_path == "screenshots/001-..-after.png"


def test_path_validation_rejects_control_chars(tmp_path: Path):
    with pytest.raises(EvidencePathError):
        persist_screenshot(
            make_png(), "after",
            trace_dir=tmp_path,
            step_no=1, step_id="S001\n",
            snapshot_id="OBS", event_id="EVT",
            process_name="p", pid=1, window_title="t",
        )


def test_empty_png_rejected(tmp_path: Path):
    with pytest.raises(EvidenceIntegrityError):
        persist_screenshot(
            b"", "after",
            trace_dir=tmp_path, **_make_kwargs(),
        )


# ---------------------------------------------------------------------------
# Atomic write: no .tmp left behind
# ---------------------------------------------------------------------------


def test_atomic_write_leaves_no_tmp(tmp_path: Path):
    rec = persist_screenshot(
        make_png(), "after",
        trace_dir=tmp_path, **_make_kwargs(),
    )
    leftovers = list((tmp_path / "screenshots").glob("*.tmp"))
    assert leftovers == []
    assert (tmp_path / rec.relative_path).exists()


# ---------------------------------------------------------------------------
# Verify on disk
# ---------------------------------------------------------------------------


def test_verify_evidence_on_disk_detects_missing(tmp_path: Path):
    rec = persist_screenshot(
        make_png(), "after",
        trace_dir=tmp_path, **_make_kwargs(),
    )
    (tmp_path / rec.relative_path).unlink()
    assert verify_evidence_on_disk(rec, tmp_path) is False


def test_verify_evidence_on_disk_detects_digest_mismatch(tmp_path: Path):
    rec = persist_screenshot(
        make_png(), "after",
        trace_dir=tmp_path, **_make_kwargs(),
    )
    # Overwrite the file with different bytes.
    (tmp_path / rec.relative_path).write_bytes(b"corrupt")
    assert verify_evidence_on_disk(rec, tmp_path) is False


# ---------------------------------------------------------------------------
# Record round-trip
# ---------------------------------------------------------------------------


def test_evidence_record_to_dict_round_trip(tmp_path: Path):
    rec = persist_screenshot(
        make_png(), "after",
        trace_dir=tmp_path, **_make_kwargs(),
    )
    d = rec.to_dict()
    assert d["role"] == "after"
    assert d["kind"] == "screenshot"
    assert d["media_type"] == "image/png"
    assert d["redaction"]["applied"] is False
    assert d["redaction"]["rule_ids"] == []
    # Stable fields round-trip.
    assert d["sha256"] == rec.sha256
    assert d["evidence_id"] == rec.evidence_id
    assert d["bytes"] == rec.bytes_size


# ---------------------------------------------------------------------------
# VALID_ROLES
# ---------------------------------------------------------------------------


def test_valid_roles_constant():
    assert VALID_ROLES == frozenset({"before", "after", "failure", "baseline"})