"""Case memory uses real compilation/artifact paths, never real GUI input."""
from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
from pathlib import Path

import pytest

from agent.recording.artifacts import write_compilation_artifacts
from agent.recording.case_memory import CaseMemory, _immutable
from agent.recording.compiler import compile_recording
from test_case.test_recording import test_golden_v3_window_sequence as fixtures

pytestmark = pytest.mark.unit


def _make(root, *, session="REC-one", aid="btnApply", events=True, v3=True):
    raw = fixtures._recording(
        [fixtures._click(1, 100, "C1", 100, automation_id=aid, instance_id="RI-0001")]
        if events else [], windows=[fixtures._MAIN])
    raw = dataclasses.replace(raw, session_id=session)
    if v3:
        raw = fixtures._with_timeline(raw, instances=("RI-0001",))
    result = compile_recording(raw, profile="windows_hisec")
    return write_compilation_artifacts(root, raw, result)


def _paths(art):
    raw = next((art.trajectory_root / "sources").glob("*/recording.json")) if art.trajectory_root else art.recording
    return dict(raw_path=raw, trace_path=art.trace_v3_path or art.golden_trace, report_path=art.compile_report)


def test_real_compile_auto_registers_ready_without_claiming_replay_pass(tmp_path):
    art = _make(tmp_path)
    assert art.case_memory["status"] == "indexed"
    rows = CaseMemory(tmp_path).search("ready")
    assert len(rows) == 1
    assert rows[0]["replay"] == "not_run"
    assert rows[0]["business"] == "not_verified"
    assert rows[0]["verifiedLiveReference"] is False
    assert rows[0]["capture"]["completeness"] == "unknown"


def test_empty_recording_is_not_lost_or_promoted(tmp_path):
    art = _make(tmp_path, events=False)
    assert art.case_memory["status"] == "indexed"
    row = CaseMemory(tmp_path).search()[0]
    assert row["capture"]["eventCount"] == 0
    assert row["compile"]["status"] == "incomplete"
    assert row["runs"] == []


def test_duplicate_import_idempotent_and_same_name_new_session_separate(tmp_path):
    a = _make(tmp_path)
    memory = CaseMemory(tmp_path)
    again = memory.register(**_paths(a))
    assert again["caseId"] == a.case_memory["caseId"]
    assert len(memory.search()) == 1
    b = _make(tmp_path, session="REC-two")
    assert b.case_memory["caseId"] != again["caseId"]
    assert len(memory.search()) == 2
    assert a.trace_v3_path.is_file()


def test_flat_same_name_overwrite_preserves_earlier_source_bytes(tmp_path):
    first = _make(tmp_path, v3=False)
    before = CaseMemory(tmp_path).search()[0]
    old_bytes = CaseMemory(tmp_path)._resolve(before["refs"]["raw"]).read_bytes()
    _make(tmp_path, session="REC-second", v3=False)
    assert CaseMemory(tmp_path)._resolve(before["refs"]["raw"]).read_bytes() == old_bytes
    assert len(CaseMemory(tmp_path).search()) == 2


def test_preview_zero_writes_and_rejects_external_source(tmp_path, monkeypatch):
    monkeypatch.setenv("EDR_GA_CASE_MEMORY", "0")
    art = _make(tmp_path)
    assert art.case_memory == {"status": "disabled"}
    memory = CaseMemory(tmp_path)
    assert memory.preview(**_paths(art))["status"] == "dry_run"
    assert not memory.home.exists()
    with pytest.raises(ValueError, match="outside_record_root"):
        CaseMemory(tmp_path / "elsewhere").preview(**_paths(art))


def test_index_failure_visible_does_not_relabel_compilation(tmp_path, monkeypatch, caplog):
    def broken(*a, **kw):
        raise OSError("private token must not be printed")
    monkeypatch.setattr(CaseMemory, "register", broken)
    art = _make(tmp_path)
    assert art.compilation.golden.status == "ready"
    assert art.trace_v3_path.is_file()
    assert art.case_memory["status"] == "error"
    assert "case_memory_index_failed" in caplog.text
    assert "private token" not in caplog.text


def test_v3_sources_referenced_without_duplicate_objects(tmp_path):
    art = _make(tmp_path)
    row = CaseMemory(tmp_path).search()[0]
    assert row["refs"]["raw"]["path"].startswith("trajectories/")
    assert row["refs"]["trace"]["path"].startswith("trajectories/")
    # Only mutable flat compile report needs a content-addressed snapshot.
    assert len(list((tmp_path / "case-memory" / "objects").glob("*.json"))) == 1


def test_source_changed_not_recommended_and_rebuild_refuses(tmp_path):
    art = _make(tmp_path)
    art.trace_v3_path.write_text("{}")
    memory = CaseMemory(tmp_path)
    assert memory.search() == []
    with pytest.raises(ValueError, match="source_changed"):
        memory.rebuild()


def test_rebuild_after_move_uses_relative_sources(tmp_path):
    original = tmp_path / "old"
    _make(original)
    copied = tmp_path / "new"
    shutil.copytree(original, copied)
    original.rename(tmp_path / "unavailable")
    (copied / "case-memory" / "catalog.sqlite").unlink()
    memory = CaseMemory(copied)
    assert memory.rebuild() == {"status": "rebuilt", "cases": 1, "runs": 0}
    assert len(memory.search()) == 1
    assert all(not Path(r["path"]).is_absolute() for r in memory.search()[0]["refs"].values())


@pytest.mark.parametrize("outcome,ok,latched,expected", [
    ("pass", True, False, "pass"), ("unknown", False, True, "unknown"),
    ("fail", False, True, "fail"), ("pass", True, True, "unknown")])
def test_actual_persisted_run_separate_from_compile_and_live_claim(
        tmp_path, outcome, ok, latched, expected):
    art = _make(tmp_path)
    summary = art.trajectory_root / "runs" / "RUN-one" / "summary.json"
    summary.parent.mkdir(parents=True)
    summary.write_text(json.dumps({"runId": "server-one", "outcome": outcome,
                                   "ok": ok, "latched": latched}))
    memory = CaseMemory(tmp_path)
    first = memory.record_run(trace_path=art.trace_v3_path, summary_path=summary)
    assert memory.record_run(trace_path=art.trace_v3_path, summary_path=summary) == first
    run = memory.search()[0]["runs"][0]
    assert run["outcome"] == expected
    assert run["evidenceKind"] == "reported_run"
    assert run["business"] == "not_independently_verified"
    assert memory.rebuild()["runs"] == 1


def test_same_run_id_conflicting_receipt_not_overwritten(tmp_path):
    art = _make(tmp_path)
    p = art.trajectory_root / "runs" / "RUN-one" / "summary.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"runId": "r1", "outcome": "unknown", "ok": False, "latched": True}))
    memory = CaseMemory(tmp_path)
    memory.record_run(trace_path=art.trace_v3_path, summary_path=p)
    p.write_text(json.dumps({"runId": "r1", "outcome": "pass", "ok": True, "latched": False}))
    with pytest.raises(ValueError, match="immutable_conflict"):
        memory.record_run(trace_path=art.trace_v3_path, summary_path=p)


def test_similarity_compares_structure_not_instructions_or_root_cause(tmp_path):
    a = _make(tmp_path)
    b = _make(tmp_path, session="REC-two", events=False)
    memory = CaseMemory(tmp_path)
    # Empty capture lacks context; refuse cross-context automatic comparison.
    with pytest.raises(ValueError, match="context_mismatch"):
        memory.compare(a.case_memory["caseId"], b.case_memory["caseId"])
    c = _make(tmp_path, session="REC-three")
    report = memory.compare(a.case_memory["caseId"], c.case_memory["caseId"])
    assert report["guiExecuted"] is False
    assert report["diagnosis"] == "hypothesis_only"
    assert report["firstStructuralDifference"] is None
    assert len(memory.search(similar_to=a.case_memory["caseId"])) == 1


def test_index_does_not_store_labels_arguments_credentials(tmp_path):
    a = _make(tmp_path)
    db = (tmp_path / "case-memory" / "catalog.sqlite").read_bytes()
    assert b'"text"' not in db
    assert b'"args"' not in db
    assert b'"windowTitle"' not in db
    assert b"EDRClient.exe" not in db


def test_case_memory_cli_offline_no_target(tmp_path, capsys):
    from agent.cli import main
    a = _make(tmp_path)
    assert main(["memory", "--record-root", str(tmp_path), "search", "ready"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["cases"][0]["caseId"] == a.case_memory["caseId"]
    assert main(["memory", "--record-root", str(tmp_path), "rebuild"]) == 0


def test_cli_replay_hook_reads_saved_summary_without_dispatch(tmp_path):
    from agent.cli import _remember_v3_run
    art = _make(tmp_path)
    ledger = art.trajectory_root / "runs" / "RUN-one"
    ledger.mkdir(parents=True)
    (ledger / "summary.json").write_text(json.dumps({
        "runId": "run-one", "outcome": "unknown", "ok": False, "latched": True}))
    result = _remember_v3_run(art.trajectory_root, art.compilation.golden, ledger)
    assert result["status"] == "indexed"
    assert CaseMemory(tmp_path).search()[0]["runs"][0]["outcome"] == "unknown"


def test_mixed_recording_and_golden_refused_before_any_import_write(tmp_path, monkeypatch):
    monkeypatch.setenv("EDR_GA_CASE_MEMORY", "0")
    a = _make(tmp_path)
    b = _make(tmp_path, session="REC-other")
    paths = _paths(a)
    paths["trace_path"] = b.trace_v3_path
    memory = CaseMemory(tmp_path)
    with pytest.raises(ValueError, match="raw_trace_mismatch"):
        memory.preview(**paths)
    with pytest.raises(ValueError, match="raw_trace_mismatch"):
        memory.register(**paths)
    assert not memory.home.exists()


def test_reference_symlink_escape_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "external"
    outside.mkdir()
    (root / "case-memory").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="directory_escape"):
        CaseMemory(root)


def test_other_trajectory_run_cannot_be_linked_by_trace_argument(tmp_path):
    a = _make(tmp_path)
    b = _make(tmp_path, session="REC-second")
    p = b.trajectory_root / "runs" / "RUN-other" / "summary.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"runId": "r2", "outcome": "pass", "ok": True, "latched": False}))
    with pytest.raises(ValueError, match="outside_trajectory"):
        CaseMemory(tmp_path).record_run(trace_path=a.trace_v3_path, summary_path=p)
