"""
P0.3 acceptance gate — fingerprint determinism (FR-P0.3-04, §8.3).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
_TARGET = _REPO / "target"

if str(_TARGET) not in sys.path:
    sys.path.insert(0, str(_TARGET))


import observations as obs  # noqa: E402


# ---------------------------------------------------------------------------
# Stable-field fingerprint (FR-P0.3-04)
# ---------------------------------------------------------------------------


def test_fingerprint_stable_across_calls():
    t = obs.Target(target_id="T0001", kind="window", process_name="X.exe",
                   pid=1, native_window_id="w1", title="Main",
                   control_type="Window")
    fp1 = obs.compute_fingerprint(t)
    fp2 = obs.compute_fingerprint(t)
    assert fp1.digest == fp2.digest
    assert fp1.digest.startswith("sha256:")
    assert len(fp1.digest) == 7 + 64


def test_fingerprint_excludes_transient_fields():
    """Removing rect / pid / screen-coords does NOT change the
    fingerprint (architecture §8.3: stable fields only)."""
    base_dict = {"process_name": "X", "pid": 1, "native_window_id": "w",
                 "title": "T", "kind": "window"}
    t_base = obs.Target(target_id="T0001", kind="window",
                        process_name="X", pid=1, native_window_id="w",
                        title="T")
    fp_base = obs.compute_fingerprint(t_base)
    fp_dict = obs.compute_fingerprint_from_dict(base_dict)
    assert fp_base.digest == fp_dict.digest


def test_fingerprint_changes_when_title_changes():
    t1 = obs.Target(target_id="T0001", kind="window", process_name="X.exe",
                    pid=1, native_window_id="w1", title="T1")
    t2 = obs.Target(target_id="T0001", kind="window", process_name="X.exe",
                    pid=1, native_window_id="w1", title="T2")
    assert (obs.compute_fingerprint(t1).digest
            != obs.compute_fingerprint(t2).digest)


def test_fingerprint_stable_across_process_restart():
    """The whole point of the P0.3 IDENTITY / OWNERSHIP split:
    same control after an EDR client restart (process_name / pid
    change) keeps the same fingerprint. The resolver's ownership
    step filters by process_name; the fingerprint only encodes
    identity."""
    t1 = obs.Target(target_id="T0001", kind="control",
                    process_name="EDRClient.exe", pid=1234,
                    native_window_id="w1", title="OK",
                    control_type="Button", automation_id="btn-ok")
    t2 = obs.Target(target_id="T0001", kind="control",
                    process_name="EDRClient.exe", pid=9999,  # restarted
                    native_window_id="w1", title="OK",
                    control_type="Button", automation_id="btn-ok")
    t3 = obs.Target(target_id="T0001", kind="control",
                    process_name="EDRClient2.exe",  # renamed
                    pid=1234, native_window_id="w1", title="OK",
                    control_type="Button", automation_id="btn-ok")
    fp1 = obs.compute_fingerprint(t1).digest
    assert fp1 == obs.compute_fingerprint(t2).digest
    assert fp1 == obs.compute_fingerprint(t3).digest


def test_fingerprint_changes_when_automation_id_changes():
    """automation_id is an IDENTITY field; changing it changes the
    fingerprint (it is the most stable identity for UIA controls)."""
    t1 = obs.Target(target_id="T0001", kind="control",
                    process_name="X.exe", pid=1, native_window_id="w1",
                    title="OK", control_type="Button",
                    automation_id="btn-ok")
    t2 = obs.Target(target_id="T0001", kind="control",
                    process_name="X.exe", pid=1, native_window_id="w1",
                    title="OK", control_type="Button",
                    automation_id="btn-different")
    assert (obs.compute_fingerprint(t1).digest
            != obs.compute_fingerprint(t2).digest)


def test_fingerprint_records_fields_used():
    t = obs.Target(target_id="T0001", kind="window", process_name="X",
                   pid=1, native_window_id="w1", title="T",
                   control_type="Window", automation_id="a")
    fp = obs.compute_fingerprint(t)
    # IDENTITY_FIELDS, not OWNERSHIP_FIELDS.
    assert "process_name" not in fp.fields
    assert "pid" not in fp.fields
    assert "automation_id" in fp.fields
    assert "native_window_id" in fp.fields
    assert "kind" in fp.fields


def test_fingerprint_from_dict_handles_missing_keys():
    """Missing keys are treated as None; missing fields are
    excluded from the `fields` list (architecture §8.3)."""
    fp = obs.compute_fingerprint_from_dict({"automation_id": "x"})
    assert fp.fields == ("automation_id",)


def test_identity_and_ownership_constants_are_distinct():
    """P0.3 review invariant: IDENTITY_FIELDS and OWNERSHIP_FIELDS
    are disjoint. The resolver uses ownership for step-1
    filtering; the fingerprint uses identity only."""
    assert not (set(obs.IDENTITY_FIELDS) & set(obs.OWNERSHIP_FIELDS))


# ---------------------------------------------------------------------------
# Snapshot tree_digest stability
# ---------------------------------------------------------------------------


def test_tree_digest_changes_when_any_target_changes():
    base = [
        {"process_name": "X", "pid": 1, "native_window_id": "w",
         "title": "Main", "kind": "window"},
        {"process_name": "X", "pid": 1, "native_window_id": "w",
         "title": "OK", "kind": "control", "control_type": "Button"},
    ]
    snap_a = obs.build_snapshot(targets=list(base), backend="x",
                                host="h", captured_at="t")
    modified = list(base)
    modified[1]["title"] = "OKAY"
    snap_b = obs.build_snapshot(targets=modified, backend="x",
                                host="h", captured_at="t")
    assert snap_a.tree_digest != snap_b.tree_digest


def test_tree_digest_stable_for_same_target_set():
    """Two snapshots built from the same target set produce the same
    tree_digest (FR-P0.3-01 determinism)."""
    base = [
        {"process_name": "X", "pid": 1, "native_window_id": "w",
         "title": "Main", "kind": "window"},
        {"process_name": "X", "pid": 1, "native_window_id": "w",
         "title": "OK", "kind": "control", "control_type": "Button"},
    ]
    snap_a = obs.build_snapshot(targets=list(base), backend="x",
                                host="h", captured_at="t")
    snap_b = obs.build_snapshot(targets=list(reversed(base)),
                                backend="x", host="h", captured_at="t")
    assert snap_a.tree_digest == snap_b.tree_digest