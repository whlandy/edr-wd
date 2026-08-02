"""P1.2 acceptance gate — atomic write (FR-P1.2-06)."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
for p in (str(_REPO / "target"), str(_REPO / "agent")):
    if p not in sys.path:
        sys.path.insert(0, p)


from execution import load_step_results, write_atomic  # noqa: E402


def test_write_atomic_creates_file(tmp_path: Path):
    target = tmp_path / "step-results.json"
    payload = {"schema_version": "1.0.0", "case_id": "TC-1", "step_results": []}
    out = write_atomic(target, payload)
    assert out == target
    assert target.exists()
    loaded = json.loads(target.read_text())
    assert loaded["case_id"] == "TC-1"


def test_write_atomic_overwrites_existing(tmp_path: Path):
    target = tmp_path / "step-results.json"
    write_atomic(target, {"step_results": [{"step_id": "S001"}]})
    write_atomic(target, {"step_results": [{"step_id": "S002"}]})
    loaded = json.loads(target.read_text())
    assert loaded["step_results"][0]["step_id"] == "S002"


def test_no_tmp_files_left_behind(tmp_path: Path):
    """After a successful write, no .tmp files remain."""
    target = tmp_path / "step-results.json"
    write_atomic(target, {"x": 1})
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []


def test_load_step_results_missing(tmp_path: Path):
    assert load_step_results(tmp_path / "nope.json") is None


def test_load_step_results_round_trip(tmp_path: Path):
    target = tmp_path / "step-results.json"
    payload = {"schema_version": "1.0.0", "step_results": [{"step_id": "S1"}]}
    write_atomic(target, payload)
    loaded = load_step_results(target)
    assert loaded == payload


def test_write_atomic_creates_parent_dirs(tmp_path: Path):
    target = tmp_path / "deep" / "nested" / "step-results.json"
    write_atomic(target, {"x": 1})
    assert target.exists()


def test_executor_write_failure_leaves_prior_file(tmp_path: Path, monkeypatch):
    """If the temp-write succeeds but `os.replace` fails, the prior
    file must remain intact (FR-P1.2-06: readers always see a complete
    file or the prior complete file)."""
    target = tmp_path / "step-results.json"
    prior = {"version": 1, "step_results": [{"step_id": "prior"}]}
    write_atomic(target, prior)

    # Patch os.replace to raise.
    real_replace = os.replace
    def fail_replace(*args, **kwargs):
        raise OSError("simulated disk failure")
    monkeypatch.setattr(os, "replace", fail_replace)

    with pytest.raises(OSError):
        write_atomic(target, {"version": 2})

    # Restore so we can read; prior file must be intact.
    monkeypatch.setattr(os, "replace", real_replace)
    loaded = json.loads(target.read_text())
    assert loaded == prior