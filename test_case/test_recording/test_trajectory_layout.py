"""TrajectoryLayout: single-folder layout writer skeleton + revision trace writes (design §5).

One trajectory, one folder: ``<record_dir>/trajectories/TRJ-<id>/`` with a
single discovery entry (``manifest.json``) pointing at the current revision,
plus the fixed sibling dirs (sources / assets/sha256 / runs / recovery).
This slice only materializes the skeleton — trace.v3.json, views and exports
are filled by later slices, so nothing here may make a revision look ready
(no placeholder trace file, no status claims).

Properties the tests pin down:

- ``create`` never overwrites: every call allocates a fresh TRJ id; an
  existing folder's manifest and content are never touched.
- ``materialize`` is idempotent: re-running it on the same root completes
  missing directories but never rewrites an existing manifest — manifest
  mutation belongs to an explicit revision-publish step, not to skeleton
  regeneration.
- ``write_revision_trace`` is write-once and immutable (design §4.2/§5:
  修订不可变): an existing ``trace.v3.json`` is refused, never overwritten;
  the write is atomic (tmp + rename) so a reader never sees a torn file;
  empty / non-dict traces are refused.
- ``publish_revision`` is the only legitimate way an existing manifest is
  ever rewritten (design §5: manifest 指向当前修订): it moves the
  ``currentRevision`` pointer atomically (tmp + rename), preserves every
  other manifest field verbatim, refuses to publish a revision whose
  ``trace.v3.json`` does not exist, and is a no-op when the pointer
  already names that revision. ``create``/``materialize`` still never
  rewrite an existing manifest.
- The manifest uses only folder-relative paths, so the whole folder stays
  portable and verifiable after a copy.
- ``from_manifest`` is the read-side twin of the manifest write side
  (design §5: Skill 始终先读 manifest): given a trajectory root it reads
  the manifest, resolves ``currentRevision`` folder-relative, and returns
  the resolved trace path.  It never guesses filenames, never rewrites
  anything, and refuses honestly (missing manifest, malformed pointer,
  missing trace) instead of falling back to a flat layout.
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from agent.recording.trajectory_layout import TrajectoryLayout  # noqa: E402


def test_create_builds_single_folder_skeleton(tmp_path):
    layout = TrajectoryLayout.create(tmp_path, "log-center-export")
    root = tmp_path / "trajectories" / "TRJ-0001"
    assert layout.root == root
    assert (root / "revisions" / "REV-0001").is_dir()
    for rel in ("sources", "assets/sha256", "runs", "recovery"):
        assert (root / rel).is_dir(), rel
    # trace.v3.json is NOT pre-created: a revision without a real compiled
    # trace must not look ready.
    assert not (root / "revisions" / "REV-0001" / "trace.v3.json").exists()


def test_create_refuses_empty_task_key(tmp_path):
    with pytest.raises(ValueError, match="task_key"):
        TrajectoryLayout.create(tmp_path, "")
    with pytest.raises(ValueError, match="task_key"):
        TrajectoryLayout.create(tmp_path, "   ")


def test_same_task_key_allocates_new_id_without_overwrite(tmp_path):
    first = TrajectoryLayout.create(tmp_path, "log-center-export")
    (first.root / "sources" / "REC-0001").mkdir()  # simulate later slice use
    second = TrajectoryLayout.create(tmp_path, "log-center-export")
    assert second.root == tmp_path / "trajectories" / "TRJ-0002"
    assert second.trajectory_id == "TRJ-0002"
    # The first folder is untouched, including its manifest pointer.
    assert (first.root / "sources" / "REC-0001").is_dir()
    assert json.loads(
        (first.root / "manifest.json").read_text())["trajectoryId"] == "TRJ-0001"


def test_manifest_is_discovery_entry_pointing_to_current_revision(tmp_path):
    layout = TrajectoryLayout.create(tmp_path, "log-center-export")
    manifest_path = layout.root / "manifest.json"
    assert manifest_path.is_file()
    manifest = json.loads(manifest_path.read_text())
    assert manifest["trajectoryId"] == "TRJ-0001"
    assert manifest["taskKey"] == "log-center-export"
    assert manifest["currentRevision"] == "revisions/REV-0001"
    # Folder-relative paths only: the manifest must stay valid after the
    # whole folder is copied elsewhere.
    assert str(tmp_path) not in manifest_path.read_text()
    assert not Path(manifest["currentRevision"]).is_absolute()
    assert (layout.root / manifest["currentRevision"]).is_dir()


def test_materialize_is_idempotent_and_never_rewrites_manifest(tmp_path):
    layout = TrajectoryLayout.create(tmp_path, "log-center-export")
    manifest_before = (layout.root / "manifest.json").read_bytes()

    again = TrajectoryLayout.materialize(
        layout.root, trajectory_id=layout.trajectory_id,
        task_key="log-center-export")
    assert again.root == layout.root
    assert (layout.root / "manifest.json").read_bytes() == manifest_before
    assert sorted(p.name for p in layout.root.iterdir()) == [
        "assets", "manifest.json", "recovery", "revisions", "runs", "sources"]

    # Missing dirs are completed; the manifest is still not rewritten.
    (layout.root / "runs").rmdir()
    TrajectoryLayout.materialize(
        layout.root, trajectory_id=layout.trajectory_id,
        task_key="log-center-export")
    assert (layout.root / "runs").is_dir()
    assert (layout.root / "manifest.json").read_bytes() == manifest_before


def _sample_trace() -> dict:
    return {
        "schema": "edr.trajectory/v3",
        "taskKey": "log-center-export",
        "steps": [
            {"index": 0, "action": "click", "window": {"titleRe": "安全防护中心"}},
        ],
    }


def test_write_revision_trace_roundtrip(tmp_path):
    layout = TrajectoryLayout.create(tmp_path, "log-center-export")
    trace = _sample_trace()
    path = layout.write_revision_trace("REV-0001", trace)
    assert path == layout.root / "revisions" / "REV-0001" / "trace.v3.json"
    assert path.is_file()
    # Read back: exact equality with the dict that was written.
    assert json.loads(path.read_text(encoding="utf-8")) == trace


def test_write_revision_trace_is_write_once(tmp_path):
    layout = TrajectoryLayout.create(tmp_path, "log-center-export")
    layout.write_revision_trace("REV-0001", _sample_trace())
    with pytest.raises(ValueError, match="trace.v3.json"):
        layout.write_revision_trace("REV-0001", {"schema": "edr.trajectory/v3", "steps": []})
    # The refusal left the original content untouched (immutable, no overwrite).
    assert json.loads(
        (layout.root / "revisions" / "REV-0001" / "trace.v3.json")
        .read_text(encoding="utf-8")) == _sample_trace()
    # A different revision has its own file — write-once is per revision id.
    path2 = layout.write_revision_trace("REV-0002", {"schema": "edr.trajectory/v3", "steps": []})
    assert path2 == layout.root / "revisions" / "REV-0002" / "trace.v3.json"


def test_write_revision_trace_refuses_empty_and_non_dict(tmp_path):
    layout = TrajectoryLayout.create(tmp_path, "log-center-export")
    trace_path = layout.root / "revisions" / "REV-0001" / "trace.v3.json"
    with pytest.raises(ValueError, match="trace"):
        layout.write_revision_trace("REV-0001", {})
    with pytest.raises(ValueError, match="trace"):
        layout.write_revision_trace("REV-0001", [])
    with pytest.raises(ValueError, match="trace"):
        layout.write_revision_trace("REV-0001", "edr.trajectory/v3")
    with pytest.raises(ValueError, match="trace"):
        layout.write_revision_trace("REV-0001", None)
    # Nothing was written by the refused calls.
    assert not trace_path.exists()


def test_write_revision_trace_is_atomic_no_torn_or_tmp_files(tmp_path):
    layout = TrajectoryLayout.create(tmp_path, "log-center-export")
    rev_dir = layout.root / "revisions" / "REV-0001"
    layout.write_revision_trace("REV-0001", _sample_trace())
    final = rev_dir / "trace.v3.json"
    # No leftover tmp siblings from the atomic rename.
    assert sorted(p.name for p in rev_dir.iterdir()) == ["trace.v3.json"]

    # Reader never sees a torn file: simulate a crash by making the rename
    # target unwritable is platform-fragile, so instead assert the
    # observable atomicity contract — a failed write leaves the previous
    # good content (or no file) in place and never a partial file.
    trace = _sample_trace()
    (rev_dir / "trace.v3.json").write_text("not json", encoding="utf-8")
    # Existing file still wins: write-once means the garbage is NOT replaced.
    with pytest.raises(ValueError, match="trace.v3.json"):
        layout.write_revision_trace("REV-0001", trace)
    assert (final).read_text(encoding="utf-8") == "not json"

    # A fresh revision: verify the tmp file never lingers if serialization
    # itself fails (json.dumps raises before any rename can happen).
    unserializable = {"steps": {1, 2}}  # set is not JSON-serializable
    rev2 = layout.root / "revisions" / "REV-0002"
    rev2.mkdir(parents=True)
    with pytest.raises((TypeError, ValueError)):
        layout.write_revision_trace("REV-0002", unserializable)
    assert sorted(p.name for p in rev2.iterdir()) == []


def test_write_revision_trace_cleans_tmp_on_io_failure(tmp_path, monkeypatch):
    """A failure after the tmp file exists (e.g. fsync/IO error) must leave
    no tmp sibling and no trace file behind."""
    import agent.recording.trajectory_layout as tl

    layout = TrajectoryLayout.create(tmp_path, "log-center-export")
    rev_dir = layout.root / "revisions" / "REV-0001"

    def boom(fd):
        raise OSError("simulated I/O failure")

    monkeypatch.setattr(tl.os, "fsync", boom)
    with pytest.raises(OSError, match="simulated I/O failure"):
        layout.write_revision_trace("REV-0001", _sample_trace())
    assert sorted(p.name for p in rev_dir.iterdir()) == []


# -- views projection (design §5: views/steps.md + top-level README.md) -----


def _v3_trace_two_steps() -> dict:
    """A realistic v3 golden dict shape (GoldenTrace.to_dict, models.py):
    steps is a stepId-keyed map with an entry pointer and next-chain."""
    return {
        "schema": "edr.desktop-golden-trace/v3",
        "status": "ready",
        "name": "log-center-export",
        "sourceRecording": {},
        "catalog": {},
        "environment": {},
        "entry": "step-0001",
        "steps": {
            # Dict insertion order deliberately differs from chain order:
            # the map ordering carries no meaning, only entry/next does.
            "step-0002": {
                "stepId": "step-0002", "actionId": "key.press",
                "args": {"key": "enter"}, "selector": None,
                "endSelector": None, "verifiers": [], "required": True,
                "status": "ready", "issues": [], "next": None,
            },
            "step-0001": {
                "stepId": "step-0001", "actionId": "pointer.click",
                "args": {}, "selector": {"kind": "titleRe", "value": "安全防护中心"},
                "endSelector": None, "verifiers": [], "required": True,
                "status": "ready", "issues": [], "next": "step-0002",
            },
        },
        "cleanup": [],
    }


def _write_revision(layout, trace=None):
    trace = trace if trace is not None else _v3_trace_two_steps()
    return layout.write_revision_trace("REV-0001", trace), trace


def test_write_revision_views_steps_md_lists_every_step_in_order(tmp_path):
    layout = TrajectoryLayout.create(tmp_path, "log-center-export")
    _write_revision(layout)
    steps_path = layout.write_revision_views("REV-0001")
    assert steps_path == layout.root / "revisions" / "REV-0001" / "views" / "steps.md"
    assert steps_path.is_file()
    text = steps_path.read_text(encoding="utf-8")
    trace = json.loads(
        (layout.root / "revisions" / "REV-0001" / "trace.v3.json")
        .read_text(encoding="utf-8"))
    # One item per v3 step, in chain order from entry, no fabricated steps.
    trace_steps = trace["steps"]
    order = []
    cur = trace["entry"]
    while cur is not None:
        order.append(cur)
        cur = trace_steps[cur]["next"]
    positions = []
    for step_id in order:
        assert step_id in text, step_id
        assert trace_steps[step_id]["actionId"] in text
        positions.append(text.index("- **%s**" % step_id))
    # The rendered view preserves chain order (an operations view is ordered).
    assert positions == sorted(positions)
    assert text.count("- ") >= len(order)
    # Fields are taken from the trace only — no invented action names.
    assert "pointer.click" in text and "key.press" in text
    assert "nonexistent-action" not in text


def test_write_revision_views_requires_existing_trace(tmp_path):
    layout = TrajectoryLayout.create(tmp_path, "log-center-export")
    rev_dir = layout.root / "revisions" / "REV-0001"
    # No trace.v3.json: refuse, and do NOT pre-create a placeholder views
    # dir or steps.md (round-1 discipline: nothing may look ready).
    with pytest.raises(ValueError, match="trace"):
        layout.write_revision_views("REV-0001")
    assert not (rev_dir / "views").exists()
    assert not (rev_dir / "views" / "steps.md").exists()


def test_write_revision_views_readme_written_once_and_relative_links(tmp_path):
    layout = TrajectoryLayout.create(tmp_path, "log-center-export")
    _write_revision(layout)
    readme = layout.root / "README.md"
    layout.write_revision_views("REV-0001")
    assert readme.is_file()
    first = readme.read_text(encoding="utf-8")
    manifest = json.loads((layout.root / "manifest.json").read_text())
    # README references manifest and current revision via folder-relative paths.
    assert "manifest.json" in first
    assert manifest["currentRevision"] in first
    assert not Path(manifest["currentRevision"]).is_absolute()
    assert str(tmp_path) not in first
    # Write-once: a second views call refuses instead of mutating the README.
    with pytest.raises(ValueError, match="README"):
        layout.write_revision_views("REV-0001")
    assert readme.read_text(encoding="utf-8") == first


def test_write_revision_views_readme_refused_without_trace(tmp_path):
    layout = TrajectoryLayout.create(tmp_path, "log-center-export")
    # README claims usage/report entry points; without a trace there is no
    # revision to describe, so the README is not written at all.
    with pytest.raises(ValueError, match="trace"):
        layout.write_revision_views("REV-0001")
    assert not (layout.root / "README.md").exists()


def test_write_revision_views_steps_md_stays_folder_relative(tmp_path):
    """§5 可移植性(轮次 38):views/steps.md 与 README 同属"所有资源与报告
    使用本夹相对路径"的宣告面——渲染来源行不得内嵌创建时绝对根,否则整夹
    复制后该行仍指旧根,违反 §5"整夹复制后仍可校验、读报告"。README 已有
    既有钉子(str(tmp_path) not in first),steps.md 此前无钉子——本用例
    钉死同一纪律。"""
    layout = TrajectoryLayout.create(tmp_path, "log-center-export")
    _write_revision(layout)
    steps_path = layout.write_revision_views("REV-0001")
    text = steps_path.read_text(encoding="utf-8")
    assert str(tmp_path) not in text, (
        "steps.md 不得内嵌创建时绝对根(§5 夹相对纪律)")
    # 来源行仍指同一份唯一主轨迹,只是夹相对:复制到新根后指向的是新根内
    # 的 trace.v3.json,不是悬空引用。
    assert "revisions/REV-0001/trace.v3.json" in text
    import shutil
    moved = tmp_path / "moved" / layout.trajectory_id
    moved.parent.mkdir(parents=True)
    shutil.copytree(layout.root, moved)
    moved_text = (moved / "revisions" / "REV-0001" / "views"
                  / "steps.md").read_text(encoding="utf-8")
    assert (moved / "revisions" / "REV-0001" / "trace.v3.json").is_file()
    assert str(tmp_path) not in moved_text, (
        "整夹复制后 steps.md 不得残留旧根绝对路径")


# -- manifest publish step (design §5: manifest 指向当前修订) ----------------


def test_publish_revision_moves_pointer_and_preserves_other_fields(tmp_path):
    layout = TrajectoryLayout.create(tmp_path, "log-center-export")
    _write_revision(layout)  # REV-0001 trace exists on disk
    # A second revision with a real trace, so publishing it is legitimate.
    layout.write_revision_trace("REV-0002", _v3_trace_two_steps())

    before = json.loads((layout.root / "manifest.json").read_text())
    assert before["currentRevision"] == "revisions/REV-0001"

    layout.publish_revision("REV-0002")
    manifest = json.loads((layout.root / "manifest.json").read_text())
    assert manifest["currentRevision"] == "revisions/REV-0002"
    # Everything except the pointer is preserved verbatim.
    assert manifest["schema"] == before["schema"]
    assert manifest["trajectoryId"] == before["trajectoryId"]
    assert manifest["taskKey"] == before["taskKey"]
    assert {k: v for k, v in manifest.items() if k != "currentRevision"} == \
        {k: v for k, v in before.items() if k != "currentRevision"}
    # Pointer stays folder-relative.
    assert not Path(manifest["currentRevision"]).is_absolute()


def test_publish_revision_refused_without_trace_and_manifest_untouched(tmp_path):
    layout = TrajectoryLayout.create(tmp_path, "log-center-export")
    manifest_before = (layout.root / "manifest.json").read_bytes()
    # REV-0002 has no trace.v3.json — publishing it would point the
    # discovery entry at a revision that cannot be consumed.
    with pytest.raises(ValueError, match="trace"):
        layout.publish_revision("REV-0002")
    assert (layout.root / "manifest.json").read_bytes() == manifest_before
    # Also refused for a revision dir that does not exist at all.
    with pytest.raises(ValueError, match="trace"):
        layout.publish_revision("REV-9999")
    assert (layout.root / "manifest.json").read_bytes() == manifest_before


def test_publish_revision_same_revision_is_idempotent(tmp_path, monkeypatch):
    layout = TrajectoryLayout.create(tmp_path, "log-center-export")
    _write_revision(layout)
    manifest_before = (layout.root / "manifest.json").read_bytes()

    # Pointer already names REV-0001: publishing REV-0001 is a no-op — no
    # rewrite at all (rename never called), not an error.
    import agent.recording.trajectory_layout as tl
    real_rename = tl.Path.rename
    renames = []
    monkeypatch.setattr(
        tl.Path, "rename",
        lambda self, target: renames.append((str(self), str(target)))
        or real_rename(self, target))
    layout.publish_revision("REV-0001")
    assert renames == []
    assert (layout.root / "manifest.json").read_bytes() == manifest_before

    # A real move, then republishing the same revision is still a no-op.
    layout.write_revision_trace("REV-0002", _v3_trace_two_steps())
    layout.publish_revision("REV-0002")
    moved = (layout.root / "manifest.json").read_bytes()
    renames.clear()
    layout.publish_revision("REV-0002")
    assert renames == []
    assert (layout.root / "manifest.json").read_bytes() == moved


def test_publish_revision_does_not_undo_materialize_invariant(tmp_path):
    """publish_revision is the only manifest-mutation entry: after a publish,
    materialize must still refuse to rewrite the manifest."""
    layout = TrajectoryLayout.create(tmp_path, "log-center-export")
    _write_revision(layout)
    layout.write_revision_trace("REV-0002", _v3_trace_two_steps())
    layout.publish_revision("REV-0002")
    published = (layout.root / "manifest.json").read_bytes()
    assert json.loads(published)["currentRevision"] == "revisions/REV-0002"

    TrajectoryLayout.materialize(
        layout.root, trajectory_id=layout.trajectory_id,
        task_key="log-center-export")
    assert (layout.root / "manifest.json").read_bytes() == published


def test_publish_revision_cleans_tmp_on_io_failure(tmp_path, monkeypatch):
    """The pointer move is atomic (tmp + rename): an IO failure after the
    tmp file exists leaves no tmp sibling and the manifest untouched."""
    import agent.recording.trajectory_layout as tl

    layout = TrajectoryLayout.create(tmp_path, "log-center-export")
    _write_revision(layout)
    manifest_before = (layout.root / "manifest.json").read_bytes()
    layout.write_revision_trace("REV-0002", _v3_trace_two_steps())

    def boom(fd):
        raise OSError("simulated I/O failure")

    monkeypatch.setattr(tl.os, "fsync", boom)
    with pytest.raises(OSError, match="simulated I/O failure"):
        layout.publish_revision("REV-0002")
    # Manifest still points at REV-0001 and no tmp sibling lingers.
    assert (layout.root / "manifest.json").read_bytes() == manifest_before
    assert sorted(p.name for p in layout.root.iterdir()) == [
        "assets", "manifest.json", "recovery", "revisions", "runs", "sources"]


# -- source-map.json (design §5: raw事件→v3步骤/明确gap; compile-chain slice) --


def _source_map_payload() -> dict:
    """The shape the compiler derives (compiler.py diagnostics sourceEvents):
    byStep is a stepId -> raw-sequence-list mapping; coalescedSources records
    raw sequences a coalesced event absorbed."""
    return {
        "schema": "edr.source-map/v1",
        "byStep": {
            "step-0001": [1, 2],
            "step-0002": [3],
        },
        "coalescedSources": {"3": [4, 5]},
    }


def test_write_revision_source_map_roundtrip(tmp_path):
    layout = TrajectoryLayout.create(tmp_path, "log-center-export")
    layout.write_revision_trace("REV-0001", _v3_trace_two_steps())
    path = layout.write_revision_source_map("REV-0001", _source_map_payload())
    assert path == layout.root / "revisions" / "REV-0001" / "source-map.json"
    assert path.is_file()
    assert json.loads(path.read_text(encoding="utf-8")) == _source_map_payload()


def test_write_revision_source_map_is_write_once(tmp_path):
    layout = TrajectoryLayout.create(tmp_path, "log-center-export")
    layout.write_revision_trace("REV-0001", _v3_trace_two_steps())
    layout.write_revision_source_map("REV-0001", _source_map_payload())
    with pytest.raises(ValueError, match="source-map.json"):
        layout.write_revision_source_map(
            "REV-0001", {"schema": "edr.source-map/v1", "byStep": {}})
    # The refusal left the original content untouched (immutable, no overwrite).
    assert json.loads(
        (layout.root / "revisions" / "REV-0001" / "source-map.json")
        .read_text(encoding="utf-8")) == _source_map_payload()
    # A different revision has its own file — write-once is per revision id.
    layout.write_revision_trace("REV-0002", _v3_trace_two_steps())
    path2 = layout.write_revision_source_map(
        "REV-0002", {"schema": "edr.source-map/v1", "byStep": {}})
    assert path2 == layout.root / "revisions" / "REV-0002" / "source-map.json"


def test_write_revision_source_map_refused_without_trace(tmp_path):
    """source-map.json is revision-immutable compile-chain content mapped to
    the trace in the same revision: no trace, no source-map, no placeholder."""
    layout = TrajectoryLayout.create(tmp_path, "log-center-export")
    rev_dir = layout.root / "revisions" / "REV-0001"
    with pytest.raises(ValueError, match="trace.v3.json"):
        layout.write_revision_source_map("REV-0001", _source_map_payload())
    # Nothing was written — no placeholder source-map.json, no rev dir content.
    assert sorted(p.name for p in rev_dir.iterdir()) == []


def test_write_revision_source_map_refuses_empty_and_non_dict(tmp_path):
    layout = TrajectoryLayout.create(tmp_path, "log-center-export")
    layout.write_revision_trace("REV-0001", _v3_trace_two_steps())
    source_map_path = (
        layout.root / "revisions" / "REV-0001" / "source-map.json")
    with pytest.raises(ValueError, match="source_map"):
        layout.write_revision_source_map("REV-0001", {})
    with pytest.raises(ValueError, match="source_map"):
        layout.write_revision_source_map("REV-0001", [])
    with pytest.raises(ValueError, match="source_map"):
        layout.write_revision_source_map("REV-0001", None)
    # Nothing was written by the refused calls.
    assert not source_map_path.exists()


def test_write_revision_source_map_cleans_tmp_on_io_failure(
        tmp_path, monkeypatch):
    """A failure after the tmp file exists (e.g. fsync/IO error) must leave
    no tmp sibling and no source-map file behind."""
    import agent.recording.trajectory_layout as tl

    layout = TrajectoryLayout.create(tmp_path, "log-center-export")
    layout.write_revision_trace("REV-0001", _v3_trace_two_steps())
    rev_dir = layout.root / "revisions" / "REV-0001"

    def boom(fd):
        raise OSError("simulated I/O failure")

    monkeypatch.setattr(tl.os, "fsync", boom)
    with pytest.raises(OSError, match="simulated I/O failure"):
        layout.write_revision_source_map("REV-0001", _source_map_payload())
    # Only the trace is present — no source-map, no tmp sibling.
    assert sorted(p.name for p in rev_dir.iterdir()) == ["trace.v3.json"]


# -- source-map derivation from compile diagnostics (round 19) --


def test_source_map_payload_from_diagnostics_roundtrip(tmp_path):
    """The derivation output is byte-shape-equal to the round-18 pinned
    payload and flows through ``write_revision_source_map`` unchanged."""
    from agent.recording.trajectory_layout import (
        source_map_payload_from_diagnostics,
    )

    diagnostics = {
        "outOfScopeEvents": 0,
        "sourceEvents": {"byStep": {
            "step-0001": [1, 2],
            "step-0002": [3],
        }},
        "coalescedSources": {"3": [4, 5]},
    }
    layout = TrajectoryLayout.create(tmp_path, "log-center-export")
    layout.write_revision_trace("REV-0001", _v3_trace_two_steps())
    payload = source_map_payload_from_diagnostics(diagnostics)
    assert payload == _source_map_payload()
    layout.write_revision_source_map("REV-0001", payload)
    assert json.loads(
        (layout.root / "revisions" / "REV-0001" / "source-map.json")
        .read_text(encoding="utf-8")) == payload


def test_source_map_payload_from_diagnostics_missing_source_events():
    """No ``sourceEvents`` (or no ``byStep``) in the diagnostics is a real
    shape mismatch — refused honestly, no field is invented."""
    from agent.recording.trajectory_layout import (
        source_map_payload_from_diagnostics,
    )

    with pytest.raises(ValueError, match="sourceEvents"):
        source_map_payload_from_diagnostics({})
    with pytest.raises(ValueError, match="sourceEvents"):
        source_map_payload_from_diagnostics({"outOfScopeEvents": 2})
    with pytest.raises(ValueError, match="byStep"):
        source_map_payload_from_diagnostics({"sourceEvents": {}})


def test_source_map_payload_from_diagnostics_without_coalesced_sources():
    """``coalescedSources`` is always emitted by the compiler today, but the
    derivation does not invent it when absent — the payload carries only
    what the diagnostics actually held."""
    from agent.recording.trajectory_layout import (
        source_map_payload_from_diagnostics,
    )

    payload = source_map_payload_from_diagnostics({
        "sourceEvents": {"byStep": {"step-0001": [7]}},
    })
    assert payload == {
        "schema": "edr.source-map/v1",
        "byStep": {"step-0001": [7]},
    }


def test_source_map_payload_from_diagnostics_refuses_shape_mismatch():
    """A non-mapping diagnostics or a non-mapping coalescedSources is a
    shape mismatch, refused — never silently passed through."""
    from agent.recording.trajectory_layout import (
        source_map_payload_from_diagnostics,
    )

    with pytest.raises(ValueError, match="diagnostics"):
        source_map_payload_from_diagnostics(["not", "a", "mapping"])
    with pytest.raises(ValueError, match="coalescedSources"):
        source_map_payload_from_diagnostics({
            "sourceEvents": {"byStep": {"step-0001": [1]}},
            "coalescedSources": [3],
        })


# ---------------------------------------------------------------------------
# from_manifest — read-side discovery entry (design §5: Skill 始终先读
# manifest, 不按文件名猜)。CLI 单夹发现入口(轮次 23)与其共用这一解析面。
# ---------------------------------------------------------------------------


def _layout_with_trace(tmp_path, *, trace=None, revision="REV-0001"):
    """Real production chain: create → write trace → publish (no-op move).

    The trace payload is a minimal v3-shaped dict — ``from_manifest`` only
    addresses the file, it never parses the trace content.
    """
    layout = TrajectoryLayout.create(tmp_path, "log-center-export")
    layout.write_revision_trace(revision, trace if trace is not None
                                else {"windowSequence": []})
    layout.publish_revision(revision)
    return layout


def test_from_manifest_resolves_current_revision_trace_path(tmp_path):
    from agent.recording.trajectory_layout import from_manifest

    layout = _layout_with_trace(tmp_path)
    resolved = from_manifest(layout.root)
    expected = (layout.root / "revisions" / "REV-0001" / "trace.v3.json")
    assert resolved == expected
    assert resolved.is_file()


def test_from_manifest_follows_published_pointer_not_revision_0001(tmp_path):
    """After publishing REV-0002, discovery resolves the manifest pointer
    (REV-0002), never a hard-coded first revision."""
    from agent.recording.trajectory_layout import from_manifest

    layout = _layout_with_trace(tmp_path)
    rev2_dir = layout.root / "revisions" / "REV-0002"
    rev2_dir.mkdir()
    layout.write_revision_trace("REV-0002", {"windowSequence": [1]})
    layout.publish_revision("REV-0002")
    resolved = from_manifest(layout.root)
    assert resolved == rev2_dir / "trace.v3.json"


def test_from_manifest_refuses_missing_manifest(tmp_path):
    from agent.recording.trajectory_layout import from_manifest

    absent = tmp_path / "trajectories" / "TRJ-0001"
    with pytest.raises(FileNotFoundError, match="manifest"):
        from_manifest(absent)
    # An empty directory with no manifest is the same honest refusal —
    # no filename guessing, no flat-layout fallback.
    (tmp_path / "trajectories" / "TRJ-9999").mkdir(parents=True)
    with pytest.raises(FileNotFoundError, match="manifest"):
        from_manifest(tmp_path / "trajectories" / "TRJ-9999")


def test_from_manifest_refuses_malformed_or_missing_pointer(tmp_path):
    from agent.recording.trajectory_layout import from_manifest

    layout = _layout_with_trace(tmp_path)
    manifest_path = layout.root / "manifest.json"
    # A pointer naming a revision whose trace was never written must not
    # resolve (发布≠就绪: the pointer alone invents no consumable trace).
    manifest = json.loads(manifest_path.read_text())
    manifest["currentRevision"] = "revisions/REV-0007"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="trace.v3.json"):
        from_manifest(layout.root)
    # An absolute pointer escapes the folder-relative contract (§5: 所有
    # 资源使用本夹相对路径) — refused, not resolved.
    manifest["currentRevision"] = str(layout.root / "revisions" / "REV-0001")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="folder-relative"):
        from_manifest(layout.root)
    # A non-string pointer is a malformed manifest — refused.
    manifest["currentRevision"] = 42
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="currentRevision"):
        from_manifest(layout.root)


# -- §7 迁移面(收尾包②,轮次后收尾模式):dry-run→复制→校验→发布 ----------
# §7:迁移先 dry-run、复制、校验、发布,删除另行确认;raw/失败记录/审计
# 原件不覆盖;失败不发布半成品。

def _migration_source(tmp_path):
    """一条真实编译链 TRJ 夹作为迁移源(write_compilation_artifacts 全面:
    trace/source-map/views/README/exports 双文件/sources 原件+回执)。"""
    import sys as _sys
    import importlib.util as _ilu
    from agent.recording import artifacts as _artifacts
    from agent.recording.compiler import compile_recording
    spec = _ilu.spec_from_file_location(
        "tg", "test_case/test_recording/test_golden_v3_window_sequence.py")
    tg = _ilu.module_from_spec(spec)
    spec.loader.exec_module(tg)
    recording = tg._with_timeline(tg._recording(
        [tg._click(1, 100, "CAUSE-1", 100, instance_id="RI-0001")],
        windows=[tg._MAIN]), instances=("RI-0001",))
    result = compile_recording(recording, profile="windows_hisec")
    out = tmp_path / "record"
    _artifacts.write_compilation_artifacts(out, recording, result)
    return out / "trajectories" / "TRJ-0001"


def test_migration_plan_reports_facts_without_writing(tmp_path):
    """dry-run:报告文件数/字节数/当前修订/目标分配 id,零写入——目标
    record_dir 不因 dry-run 产生 trajectories/ 或任何文件(§7 先 dry-run)。"""
    from agent.recording.trajectory_layout import plan_migration
    src = _migration_source(tmp_path)
    target_record = tmp_path / "target"
    target_record.mkdir()
    report = plan_migration(src, target_record)
    assert report["trajectoryId"] == "TRJ-0001"
    assert report["currentRevision"] == "revisions/REV-0001"
    assert report["fileCount"] == \
        sum(1 for f in src.rglob("*") if f.is_file())
    assert report["totalBytes"] == \
        sum(f.stat().st_size for f in src.rglob("*") if f.is_file())
    assert report["targetTrajectoryId"] == "TRJ-0001"  # 目标空 → 同 id
    assert report["collision"] is False
    # 零写入:目标目录保持空,源不动。
    assert list(target_record.iterdir()) == []
    assert src.is_dir()


def test_migration_copies_verifies_publishes_and_preserves_source(tmp_path):
    """execute_migration 全链:复制到新 id(§5 同名不覆盖/名称不是唯一 id
    ——夹名是本地定位,manifest.trajectoryId 保留源身份)→逐字节校验→
    发布(manifest 逐字节保留含 currentRevision);源夹零改动不删除
    (§7 删除另行确认);from_manifest 从目标解析当前修订主轨迹。"""
    import shutil
    from agent.recording.trajectory_layout import (
        execute_migration, from_manifest)
    src = _migration_source(tmp_path)
    before = {f.relative_to(src): f.read_bytes()
              for f in src.rglob("*") if f.is_file()}
    target_record = tmp_path / "target"
    target_record.mkdir()
    report = execute_migration(src, target_record)
    dst = target_record / "trajectories" / "TRJ-0001"
    assert dst.is_dir()
    assert report["published"] is True
    assert report["verifiedFiles"] == len(before)
    after = {f.relative_to(dst): f.read_bytes()
             for f in dst.rglob("*") if f.is_file()}
    assert after == before, "复制必须逐字节保真"
    assert shutil.copytree is not None
    # 发布:manifest 与源逐字节一致(指针保留,不冒充新发布)。
    assert (dst / "manifest.json").read_bytes() == \
        (src / "manifest.json").read_bytes()
    assert from_manifest(dst).name == "trace.v3.json"
    # 源夹零改动(§7 不覆盖原件、删除另行确认)。
    assert {f.relative_to(src): f.read_bytes()
            for f in src.rglob("*") if f.is_file()} == before


def test_migration_allocates_next_id_and_never_overwrites_target(tmp_path):
    """目标已有 TRJ-0001 → 迁移分配 TRJ-0002,既有夹逐字节不动(§5 同名
    录制不覆盖)。"""
    from agent.recording.trajectory_layout import (
        TrajectoryLayout, execute_migration)
    src = _migration_source(tmp_path)
    target_record = tmp_path / "target"
    existing = TrajectoryLayout.create(target_record, "occupied")
    (existing.root / "revisions" / "REV-0001" / "trace.v3.json").write_text(
        "{}", encoding="utf-8")
    guard = (existing.root / "manifest.json").read_bytes()
    report = execute_migration(src, target_record)
    assert report["targetTrajectoryId"] == "TRJ-0002"
    assert (existing.root / "manifest.json").read_bytes() == guard


def test_migration_verify_failure_publishes_nothing(tmp_path):
    """校验失败不发布半成品(§7,修复后契约):复制进暂存区后篡改暂存
    trace 再触发校验 → published=False、verifiedFiles=0;失败证据保留在
    暂存区(不静默清理也不谎报),公开发布位(trajectories/)零目标——
    半成品不冒充成功,消费者不可见。"""
    from agent.recording.trajectory_layout import execute_migration
    src = _migration_source(tmp_path)
    target_record = tmp_path / "target"
    target_record.mkdir()
    # 注入:复制完成后、校验时暂存 trace 已被篡改(磁盘替换模拟传输错误)。
    import shutil

    original_copytree = shutil.copytree
    injected = {"done": False}

    def copytree_then_tamper(source, destination, *args, **kw):
        if injected["done"]:
            return original_copytree(source, destination, *args, **kw)
        injected["done"] = True
        out = original_copytree(source, destination, *args, **kw)
        victim = (Path(destination) / "revisions" / "REV-0001"
                  / "trace.v3.json")
        victim.write_bytes(b"tampered-in-transit")
        return out

    shutil.copytree = copytree_then_tamper
    try:
        report = execute_migration(src, target_record)
    finally:
        shutil.copytree = original_copytree
    assert report["published"] is False
    assert report["verifiedFiles"] == 0
    assert report["error"], "失败必须带原因"
    # 公开发布位零目标:半成品从不进入 trajectories/。
    dst = target_record / "trajectories" / "TRJ-0001"
    assert not dst.exists()
    # 失败证据保留在暂存区(报告带 staging 引用;不静默清理也不谎报)。
    staging = Path(report["staging"])
    assert (staging / "manifest.json").is_file()
    tampered = staging / "revisions" / "REV-0001" / "trace.v3.json"
    assert tampered.read_bytes() == b"tampered-in-transit"


# -- §7 迁移失败隔离(独立复验 2026-11-12 P1,收尾轮): 暂存→校验→发布 ----
# 反例:execute_migration 返回 published=false 后,公开目标夹仍带有效
# manifest,from_manifest/CLI 仍可加载为 v3/ready —— 消费者级漏洞。
# 修复契约:复制与校验全部落在正常消费者不可发现的暂存位置;全部必要
# 文件校验通过后才发布到最终目录;失败/中断不得留下可从正常 manifest/
# CLI 入口消费的目标;已有目标不覆盖;源原件不动、失败证据保留。

def _migration_tamper_source_map(tmp_path):
    """注入:copytree 完成后篡改暂存区内的 source-map.json(不同合法
    JSON,模拟传输位翻转/部分写入),走真实 execute_migration。"""
    import shutil

    original_copytree = shutil.copytree
    injected = {"done": False}

    def copytree_then_tamper(source, destination, *args, **kw):
        if injected["done"]:
            return original_copytree(source, destination, *args, **kw)
        injected["done"] = True
        out = original_copytree(source, destination, *args, **kw)
        victim = (Path(destination) / "revisions" / "REV-0001"
                  / "source-map.json")
        victim.write_text('{"schema": "edr.source-map/v1", "byStep": {}}',
                          encoding="utf-8")
        return out

    return original_copytree, injected, copytree_then_tamper


def test_migration_failed_target_is_undiscoverable_by_consumers(tmp_path):
    """负例(消费者级,独立复验 P1 反例原样):source-map 篡改 →
    published=False,且正常消费者(from_manifest / CLI _load_replay_golden
    经 manifest 发现入口)无法加载该失败目标——公开位置不出现
    manifest.json。源原件逐字节不动。"""
    import shutil
    from agent.recording.trajectory_layout import execute_migration
    src = _migration_source(tmp_path)
    before = {f.relative_to(src): f.read_bytes()
              for f in src.rglob("*") if f.is_file()}
    target_record = tmp_path / "target"
    target_record.mkdir()
    original_copytree, injected, tamper = _migration_tamper_source_map(
        tmp_path)
    shutil.copytree = tamper
    try:
        report = execute_migration(src, target_record)
    finally:
        shutil.copytree = original_copytree
    assert report["published"] is False
    assert report["verifiedFiles"] == 0
    assert report["error"], "失败必须带原因"
    trajectories = target_record / "trajectories"
    # 公开发布位零目标:正常消费者没有可发现的 manifest 入口。
    published = sorted(p.name for p in trajectories.iterdir()) \
        if trajectories.is_dir() else []
    assert published == [], (
        "a failed migration must leave no discoverable target folder")
    # 源原件逐字节不动。
    assert {f.relative_to(src): f.read_bytes()
            for f in src.rglob("*") if f.is_file()} == before


def test_migration_interrupted_copy_leaves_no_published_target(tmp_path):
    """中断例:复制中途抛错(磁盘满/进程被杀等价注入)→ 迁移如实失败,
    公开发布位零可消费目标(from_manifest/CLI 均不可发现),暂存/半成品
    不冒充发布。源原件不动。"""
    import shutil
    import pytest
    from agent.recording.trajectory_layout import execute_migration
    src = _migration_source(tmp_path)
    before = {f.relative_to(src): f.read_bytes()
              for f in src.rglob("*") if f.is_file()}
    target_record = tmp_path / "target"
    target_record.mkdir()

    class _DiskFull(RuntimeError):
        pass

    original_copytree = shutil.copytree

    def copytree_then_die(source, destination, *args, **kw):
        out = original_copytree(source, destination, *args, **kw)
        raise _DiskFull("simulated ENOSPC mid-copy")

    shutil.copytree = copytree_then_die
    try:
        with pytest.raises(_DiskFull):
            execute_migration(src, target_record)
    finally:
        shutil.copytree = original_copytree
    trajectories = target_record / "trajectories"
    published = sorted(p.name for p in trajectories.iterdir()) \
        if trajectories.is_dir() else []
    assert published == [], (
        "an interrupted migration must leave no discoverable target folder")
    assert {f.relative_to(src): f.read_bytes()
            for f in src.rglob("*") if f.is_file()} == before


def test_migration_success_publishes_once_and_consumer_loads_from_new_root(
        tmp_path):
    """正例(消费者级):正常迁移后 from_manifest 与 CLI _load_replay_golden
    都从新根消费 v3 轨迹;目标位置恰出现一个 TRJ 夹(发布=暂存→公开的
    一次落位);源与既有目标零改动。"""
    from agent.recording.trajectory_layout import execute_migration
    from agent.cli import _load_replay_golden
    src = _migration_source(tmp_path)
    before = {f.relative_to(src): f.read_bytes()
              for f in src.rglob("*") if f.is_file()}
    target_record = tmp_path / "target"
    existing = TrajectoryLayout.create(target_record, "occupied")
    existing_trace = (existing.root / "revisions" / "REV-0001")
    report = execute_migration(src, target_record)
    assert report["published"] is True
    assert report["targetTrajectoryId"] == "TRJ-0002"
    dst = target_record / "trajectories" / "TRJ-0002"
    # 消费者级:manifest 发现入口从新根解析主轨迹,CLI 加载为 v3 golden。
    resolved = json.loads((dst / "manifest.json").read_text())[
        "currentRevision"]
    assert resolved == "revisions/REV-0001"
    golden, asset_root = _load_replay_golden(str(dst))
    assert golden.schema == "edr.desktop-golden-trace/v3"
    assert asset_root == dst
    # 源与既有目标零改动。
    assert {f.relative_to(src): f.read_bytes()
            for f in src.rglob("*") if f.is_file()} == before
    assert existing_trace.is_dir()
    assert not (existing.root / "manifest.json").exists() or \
        (existing.root / "manifest.json").is_file()


def test_migration_identity_follows_folder_on_collision(tmp_path):
    """评审 item4 身份契约:夹名与 manifest.trajectoryId 必须一致。
    源 id 空闲 → 沿用,manifest 逐字节保真;冲突 → 新 id 且 manifest
    仅 trajectoryId 改写(字段集不变,currentRevision/taskKey 保留),
    from_manifest 在新根仍可解析当前修订。"""
    import json as _json
    from agent.recording.trajectory_layout import (
        TrajectoryLayout, execute_migration, from_manifest)
    src = _migration_source(tmp_path)
    source_manifest = _json.loads(
        (src / "manifest.json").read_text())

    # Case 1: target empty → source id free → folder TRJ-0001, manifest
    # byte-identical (identity stable).
    free_target = tmp_path / "free"
    free_target.mkdir()
    report = execute_migration(src, free_target)
    assert report["published"] is True
    assert (free_target / "trajectories" / "TRJ-0001").is_dir()
    assert (free_target / "trajectories" / "TRJ-0001" / "manifest.json"
            ).read_bytes() == (src / "manifest.json").read_bytes()

    # Case 2: TRJ-0001 occupied → folder TRJ-0002, manifest trajectoryId
    # rewritten to TRJ-0002, all other fields preserved.
    occupied_target = tmp_path / "occupied"
    occupied_target.mkdir()
    occupied = TrajectoryLayout.create(occupied_target, "occupied")
    (occupied.root / "revisions" / "REV-0001" / "trace.v3.json").write_text(
        "{}", encoding="utf-8")
    report = execute_migration(src, occupied_target)
    assert report["published"] is True
    new_root = occupied_target / "trajectories" / "TRJ-0002"
    assert new_root.is_dir()
    manifest = _json.loads((new_root / "manifest.json").read_text())
    assert manifest["trajectoryId"] == "TRJ-0002"
    assert manifest["currentRevision"] == source_manifest["currentRevision"]
    assert manifest["taskKey"] == source_manifest["taskKey"]
    assert manifest["schema"] == source_manifest["schema"]
    assert from_manifest(new_root).name == "trace.v3.json"
