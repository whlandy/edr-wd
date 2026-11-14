"""Single-folder trajectory layout (design §5).

One trajectory = one folder under ``<record_dir>/trajectories/TRJ-<id>/``:

- ``manifest.json`` is the only discovery entry; it points at the current
  revision with a folder-relative path so the folder stays portable.
- ``revisions/REV-<id>/`` holds the immutable revision content (the v3
  trace, later also views/exports).  Revisions are never mutated in place —
  new content means a new REV id.
- ``sources/``, ``assets/sha256/``, ``runs/``, ``recovery/`` are the fixed
  siblings design §5 assigns.

This module materializes the skeleton, writes the revision trace
(``write_revision_trace`` — write-once, atomic, no placeholder), the
compile-derived source map (``write_revision_source_map``), projects the
views (``write_revision_views``), and moves the manifest's
current-revision pointer (``publish_revision`` — the only legitimate
manifest mutation).  MAA exports are later slices' jobs; nothing here
publishes readiness.

``revisions/REV-<id>/source-map.json`` (design §5: raw事件→v3步骤/明确gap
mapping) is written by ``write_revision_source_map`` — same guards as the
trace (write-once, atomic, refused without the revision's trace, no
placeholder).  It is not written by any other entry point.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Union

__all__ = ["TrajectoryLayout", "source_map_payload_from_diagnostics",
           "from_manifest", "plan_migration", "execute_migration"]

#: Fixed layout under the trajectory root, relative to it.
_FIXED_DIRS = ("sources", "assets/sha256", "runs", "recovery")
_REVISIONS_DIR = "revisions"

_TASK_KEY_RE = re.compile(r"\S")
_TRAJECTORY_ID_RE = re.compile(r"^TRJ-(\d{4})$")

#: Schema tag for the compile-derived source map (design §5). The payload
#: shape is pinned by ``test_write_revision_source_map_roundtrip``.
_SOURCE_MAP_SCHEMA = "edr.source-map/v1"

#: The current-revision pointer inside ``manifest.json``, folder-relative.
_MANIFEST_NAME = "manifest.json"
_TRACE_NAME = "trace.v3.json"


def from_manifest(trajectory_root: Union[str, Path]) -> Path:
    """Resolve the current revision's ``trace.v3.json`` from the manifest.

    Design §5's read-side twin of the manifest write side: Skill 始终先读
    manifest — given a ``TRJ-<id>`` folder this reads ``manifest.json``,
    takes the folder-relative ``currentRevision`` pointer, and returns the
    path of that revision's ``trace.v3.json``.  It never guesses file
    names, never falls back to a flat layout, and never writes anything.

    Honest refusals: missing ``manifest.json`` → ``FileNotFoundError``; a
    non-string pointer or one that escapes the folder-relative contract →
    ``ValueError``; a pointer whose revision carries no trace →
    ``FileNotFoundError`` (发布≠就绪 — the pointer alone invents no
    consumable trace).
    """
    root = Path(trajectory_root)
    manifest_path = root / _MANIFEST_NAME
    if not manifest_path.is_file():
        raise FileNotFoundError(
            "manifest.json is the only discovery entry and does not "
            "exist: %s" % (manifest_path,))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    pointer = manifest.get("currentRevision")
    if not isinstance(pointer, str):
        raise ValueError(
            "manifest currentRevision must be a folder-relative string, "
            "got %r" % (pointer,))
    revision_parts = Path(pointer).parts
    if Path(pointer).is_absolute() or ".." in revision_parts:
        raise ValueError(
            "manifest currentRevision must stay folder-relative (design "
            "§5: the folder must stay portable), got %r" % (pointer,))
    trace_path = root / pointer / _TRACE_NAME
    if not trace_path.is_file():
        raise FileNotFoundError(
            "current revision %r has no %s — the manifest pointer alone "
            "is not a consumable trace: %s" % (pointer, _TRACE_NAME,
                                               trace_path))
    return trace_path


# -- §7 migration (dry-run → copy → verify → publish; delete separately) ----

def _next_trajectory_id(record_dir: Path) -> str:
    """Allocate the next TRJ id without creating anything (dry-run safe)."""
    base = Path(record_dir) / "trajectories"
    existing = []
    if base.is_dir():
        existing = sorted(
            (m.group(1) for p in base.iterdir()
             if (m := _TRAJECTORY_ID_RE.fullmatch(p.name))),
            key=int)
    return "TRJ-%04d" % ((int(existing[-1]) + 1) if existing else 1)


def plan_migration(source_root: Union[str, Path],
                   target_record_dir: Union[str, Path]) -> dict:
    """§7 dry-run: report the migration facts, write nothing.

    Reads the source folder and the target record dir's id sequence and
    reports file count / total bytes / current revision / the target id
    that ``execute_migration`` would allocate / whether that target
    already exists.  Raises the same honest refusals as ``from_manifest``
    when the source is not a consumable trajectory folder (the discovery
    entry must resolve — a folder whose manifest cannot be read is not a
    migration source).
    """
    src = Path(source_root)
    from_manifest(src)
    target = Path(target_record_dir)
    target_id = _next_trajectory_id(target)
    files = [f for f in src.rglob("*") if f.is_file()]
    return {
        "trajectoryId": src.name,
        "sourceIdFreeInTarget": not (target / "trajectories" / src.name).exists(),
        "currentRevision": json.loads(
            (src / _MANIFEST_NAME).read_text(encoding="utf-8"),
        ).get("currentRevision"),
        "fileCount": len(files),
        "totalBytes": sum(f.stat().st_size for f in files),
        "targetTrajectoryId": target_id,
        "collision": (target / "trajectories" / target_id).exists(),
    }


def execute_migration(source_root: Union[str, Path],
                      target_record_dir: Union[str, Path]) -> dict:
    """§7 migrate one trajectory folder: copy → verify → publish.

    The copy lands in a STAGING area first —
    ``<record_dir>/.migration-staging/<TRJ-id>`` — outside
    ``trajectories/`` entirely, so the normal discovery surface (the
    ``TRJ-<id>`` folders a consumer or the id allocator scans) never sees
    a half-migrated target.  Verification is byte-for-byte over every
    staged file; only when ALL files verify does the folder publish to
    its final ``trajectories/<TRJ-id>`` name via an atomic rename
    (失败不发布半成品 — a mismatch, an error, or an interruption leaves
    the public side untouched; the staging folder is kept on failure as
    evidence, never auto-deleted, and the id allocator ignores it).  The
    source folder is never modified and never deleted (§7: 删除另行
    确认).  An existing target folder is never overwritten (§5: 同名
    录制不覆盖) — the id allocation skips it.
    """
    import shutil
    src = Path(source_root)
    target = Path(target_record_dir)
    report = plan_migration(src, target)
    # 评审 item4 身份契约:目录身份与 manifest.trajectoryId 必须一致。
    # 源 id 在目标记录目录空闲 → 沿用(身份稳定,manifest 逐字节保真);
    # 冲突 → 分配新 id 并把 manifest.trajectoryId 改写为目录 id(身份随
    # 目录 — 一个事实一个面;currentRevision/taskKey 等其余字段逐字节
    # 保留)。_next_trajectory_id 从不碰撞(独占递增),不改写即不会发。
    if report.get("sourceIdFreeInTarget"):
        target_id = src.name
    else:
        target_id = report["targetTrajectoryId"]
    dst = target / "trajectories" / target_id
    staging_root = target / ".migration-staging"
    staging_root.mkdir(parents=True, exist_ok=True)
    staging = staging_root / target_id
    try:
        shutil.copytree(src, staging)
        if target_id != src.name:
            manifest_path = staging / _MANIFEST_NAME
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["trajectoryId"] = target_id
            manifest_path.write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8")
        source_files = sorted(
            f.relative_to(src) for f in src.rglob("*") if f.is_file())
        rewritten = target_id != src.name
        for rel in source_files:
            if rewritten and rel == Path(_MANIFEST_NAME):
                continue  # rewritten manifest verified by its own check below
            if src / rel != staging / rel and \
                    (src / rel).read_bytes() != (staging / rel).read_bytes():
                return {"published": False, "verifiedFiles": 0,
                        "targetTrajectoryId": target_id,
                        "error": f"byte mismatch after copy: {rel}",
                        "staging": str(staging)}
        if target_id == src.name and \
                (src / _MANIFEST_NAME).read_bytes() != \
                (staging / _MANIFEST_NAME).read_bytes():
            return {"published": False, "verifiedFiles": 0,
                    "targetTrajectoryId": target_id,
                    "error": "manifest bytes differ after copy",
                    "staging": str(staging)}
        if target_id != src.name:
            rewritten = json.loads((staging / _MANIFEST_NAME).read_text(
                encoding="utf-8"))
            if set(rewritten) != set(json.loads(
                    (src / _MANIFEST_NAME).read_text(encoding="utf-8"))):
                return {"published": False, "verifiedFiles": 0,
                        "targetTrajectoryId": target_id,
                        "error": "manifest rewrite changed fields",
                        "staging": str(staging)}
            if rewritten["trajectoryId"] != target_id:
                return {"published": False, "verifiedFiles": 0,
                        "targetTrajectoryId": target_id,
                        "error": "manifest trajectoryId not rewritten",
                        "staging": str(staging)}
        # Publish: atomic rename of the verified staging folder into the
        # public trajectories/ surface (same filesystem — same record
        # dir).  An existing destination would make the rename fail on
        # POSIX (non-empty dir) — the overwrite guard holds.
        dst.parent.mkdir(parents=True, exist_ok=True)
        staging.rename(dst)
    except BaseException:
        # Failure evidence stays in staging (never auto-deleted); the
        # public side stays untouched.  No cleanup — §7 evidence discipline.
        raise
    return {"published": True, "verifiedFiles": len(source_files),
            "targetTrajectoryId": target_id,
            "currentRevision": report["currentRevision"]}


def source_map_payload_from_diagnostics(diagnostics) -> dict:
    """Derive the ``source-map.json`` payload from compile diagnostics.

    ``compile_recording`` (compiler.py) emits ``sourceEvents.byStep``
    (stepId → raw sequences it compiled from, merges/bindings/collapse
    included) and ``coalescedSources`` (raw sequences a coalesced event
    absorbed) in its ``CompilationResult.diagnostics``. This function
    projects exactly that evidence into the payload shape
    ``write_revision_source_map`` persists — it adds nothing and invents
    no field: when ``coalescedSources`` is absent the payload simply has
    no such key, and when ``sourceEvents`` (or its ``byStep`` map) is
    missing the shape mismatch is refused with ``ValueError`` rather
    than papered over with an empty mapping.
    """
    if not isinstance(diagnostics, dict):
        raise ValueError(
            "diagnostics must be a mapping, got %s"
            % (type(diagnostics).__name__,))
    source_events = diagnostics.get("sourceEvents")
    if not isinstance(source_events, dict):
        raise ValueError(
            "diagnostics has no sourceEvents mapping — the source map is "
            "derived from compile diagnostics only, nothing is invented: "
            "got %r" % (source_events,))
    by_step = source_events.get("byStep")
    if not isinstance(by_step, dict):
        raise ValueError(
            "diagnostics.sourceEvents has no byStep mapping: got %r"
            % (by_step,))
    payload = {"schema": _SOURCE_MAP_SCHEMA, "byStep": by_step}
    coalesced = diagnostics.get("coalescedSources")
    if coalesced is not None:
        if not isinstance(coalesced, dict):
            raise ValueError(
                "diagnostics.coalescedSources must be a mapping, got %s"
                % (type(coalesced).__name__,))
        payload["coalescedSources"] = coalesced
    return payload


class TrajectoryLayout:
    """Materialize and address one ``TRJ-<id>`` single-folder layout."""

    def __init__(self, root: Path, trajectory_id: str, task_key: str):
        self.root = root
        self.trajectory_id = trajectory_id
        self.task_key = task_key

    # -- creation -----------------------------------------------------------

    @classmethod
    def create(cls, record_dir: Union[str, Path],
               task_key: str) -> "TrajectoryLayout":
        """Allocate a fresh ``TRJ-<id>`` folder under ``record_dir``.

        Every call allocates a new id — same task_key never overwrites an
        existing trajectory folder (design §5: names are not unique ids).
        The manifest is written once at creation and stays immutable until
        an explicit revision-publish step (a later slice) changes it.
        """
        if not task_key or not _TASK_KEY_RE.search(task_key):
            raise ValueError(
                "task_key must be a non-empty string, got %r" % (task_key,))

        record_dir = Path(record_dir)
        base = record_dir / "trajectories"
        existing = []
        if base.is_dir():
            existing = sorted(
                (m.group(1) for p in base.iterdir()
                 if (m := _TRAJECTORY_ID_RE.fullmatch(p.name))),
                key=int)
        # Next id = max existing + 1; gaps (renamed/pruned folders) are fine.
        next_num = (int(existing[-1]) + 1) if existing else 1
        trajectory_id = "TRJ-%04d" % next_num
        root = base / trajectory_id
        # mkdir(exist_ok=False) is the overwrite guard: if two processes race
        # past the scan above, exactly one wins and the loser raises.
        root.mkdir(parents=True, exist_ok=False)
        layout = cls._write_skeleton(root, trajectory_id, task_key)
        (root / _REVISIONS_DIR / "REV-0001").mkdir(parents=True, exist_ok=True)
        return layout

    @classmethod
    def materialize(
        cls, root: Union[str, Path], trajectory_id: str,
        task_key: str, *, revision_id: str = "REV-0001",
    ) -> "TrajectoryLayout":
        """Re-complete an existing folder's skeleton without touching content.

        Completes missing fixed dirs and the revision dir.  Never rewrites
        an existing ``manifest.json`` — manifest mutation belongs to the
        revision-publish step.  Idempotent.
        """
        root = Path(root)
        if not _TRAJECTORY_ID_RE.fullmatch(trajectory_id):
            raise ValueError(
                "trajectory_id must match TRJ-<4 digits>, got %r"
                % (trajectory_id,))
        if not root.is_dir():
            raise FileNotFoundError(
                "trajectory folder does not exist: %s" % (root,))
        layout = cls._write_skeleton(root, trajectory_id, task_key)
        (root / _REVISIONS_DIR / revision_id).mkdir(parents=True, exist_ok=True)
        return layout

    # -- revision trace ------------------------------------------------------

    def write_revision_trace(self, revision_id: str, trace: dict) -> Path:
        """Write ``revisions/REV-<id>/trace.v3.json`` — write-once, atomic.

        An existing trace is refused, never overwritten (design §5: 修订
        不可变; §4.2: raw原件一份、主v3一份).  The write goes through a
        tmp file + rename so a concurrent reader never observes a torn
        file.  The manifest's current-revision pointer is NOT touched —
        publishing a revision is the manifest step's job, not the write's.
        The sibling ``source-map.json`` (raw event → step/gap mapping) is
        not written here; it is revision-immutable compile-chain output,
        a later slice's job (no placeholder is pre-created for it).
        """
        if not isinstance(trace, dict) or not trace:
            raise ValueError(
                "trace must be a non-empty dict, got %s"
                % (type(trace).__name__,))
        rev_dir = self.root / _REVISIONS_DIR / revision_id
        trace_path = rev_dir / "trace.v3.json"
        if trace_path.exists():
            raise ValueError(
                "revision trace already exists, revisions are immutable: %s"
                % (trace_path,))
        rev_dir.mkdir(parents=True, exist_ok=True)
        # Serialize BEFORE creating the tmp file: a serialization failure
        # must not leave a tmp sibling behind.
        payload = json.dumps(trace, indent=2, ensure_ascii=False) + "\n"
        fd = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(rev_dir), prefix=".trace.v3.",
            suffix=".json.tmp", delete=False)
        try:
            with fd:
                fd.write(payload)
                fd.flush()
                os.fsync(fd.fileno())
            # Same-directory rename is atomic on POSIX and Windows.
            Path(fd.name).rename(trace_path)
        except BaseException:
            try:
                Path(fd.name).unlink()
            except FileNotFoundError:
                pass
            raise
        return trace_path

    # -- revision source map (design §5) --------------------------------------

    def write_revision_source_map(self, revision_id: str,
                                  source_map: dict) -> Path:
        """Write ``revisions/REV-<id>/source-map.json`` — write-once, atomic.

        The raw event → v3 step/gap mapping (design §5) is revision-immutable
        compile-chain content: derived by the caller from the compiler's
        ``sourceEvents`` diagnostics (compiler.py) for the trace this same
        revision already carries.  Guards mirror ``write_revision_trace``:
        an existing file is refused, never overwritten; the write is atomic
        (tmp + fsync + rename, serialize before tmp creation); empty /
        non-dict payloads are refused.  Writing a source map for a revision
        whose ``trace.v3.json`` does not exist is refused — the mapping is
        anchored to that trace and no placeholder may exist for it.
        """
        if not isinstance(source_map, dict) or not source_map:
            raise ValueError(
                "source_map must be a non-empty dict, got %s"
                % (type(source_map).__name__,))
        rev_dir = self.root / _REVISIONS_DIR / revision_id
        if not (rev_dir / "trace.v3.json").is_file():
            raise ValueError(
                "trace.v3.json does not exist for %s — source-map.json is "
                "compiled against the revision's trace, no placeholder is "
                "written: %s"
                % (revision_id, rev_dir / "trace.v3.json"))
        map_path = rev_dir / "source-map.json"
        if map_path.exists():
            raise ValueError(
                "source-map.json already exists, revisions are immutable: %s"
                % (map_path,))
        # Serialize BEFORE creating the tmp file: a serialization failure
        # must not leave a tmp sibling behind.
        payload = json.dumps(source_map, indent=2, ensure_ascii=False) + "\n"
        fd = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(rev_dir), prefix=".source-map.",
            suffix=".json.tmp", delete=False)
        try:
            with fd:
                fd.write(payload)
                fd.flush()
                os.fsync(fd.fileno())
            # Same-directory rename is atomic on POSIX and Windows.
            Path(fd.name).rename(map_path)
        except BaseException:
            try:
                Path(fd.name).unlink()
            except FileNotFoundError:
                pass
            raise
        return map_path

    def write_revision_views(self, revision_id: str) -> Path:
        """Project ``revisions/REV-<id>/views/steps.md`` and the top-level
        ``README.md`` from the already-written ``trace.v3.json``.

        Both are views over the single v3 trace (design §2: 不独立维护另一
        套动作序列) — the trace is read back from disk and rendered, never
        invented.  A revision without its trace is refused and leaves no
        placeholder behind.  The README is written once per folder: an
        existing README is immutable, a second call refuses.  Neither view
        writes the revision's ``source-map.json`` — that mapping is
        compile-chain content, not a rendered view.
        """
        rev_dir = self.root / _REVISIONS_DIR / revision_id
        trace_path = rev_dir / "trace.v3.json"
        if not trace_path.is_file():
            raise ValueError(
                "trace.v3.json does not exist for %s — views are projected "
                "from the compiled trace, no placeholder is written: %s"
                % (revision_id, trace_path))
        trace = json.loads(trace_path.read_text(encoding="utf-8"))
        # §5 portability: the view is a report face — its rendered source
        # line must stay folder-relative so a copied folder still points at
        # its own trace, never at the creation-time absolute root.
        steps_text = self._render_steps_md(
            trace, Path(_REVISIONS_DIR) / revision_id / _TRACE_NAME)
        readme_text = self._render_readme(revision_id)

        # The top-level README is the folder-level immutable artifact:
        # refuse before touching any revision content.
        readme_path = self.root / "README.md"
        if readme_path.exists():
            raise ValueError(
                "README.md already exists, the top-level README is written "
                "once: %s" % (readme_path,))

        views_dir = rev_dir / "views"
        views_dir.mkdir(parents=True, exist_ok=True)
        steps_md = views_dir / "steps.md"
        if steps_md.exists():
            raise ValueError(
                "steps.md already exists, revision views are immutable: %s"
                % (steps_md,))
        steps_md.write_text(steps_text, encoding="utf-8")

        readme_path.write_text(readme_text, encoding="utf-8")
        return steps_md

    # -- manifest publish step (design §5) ------------------------------------

    def publish_revision(self, revision_id: str) -> Path:
        """Point the manifest's ``currentRevision`` at ``revisions/REV-<id>``.

        This is the only legitimate way an existing manifest is ever
        rewritten (design §5: manifest 指向当前修订; ``create``/
        ``materialize`` never touch an existing manifest).  Publishing a
        revision without a ``trace.v3.json`` is refused — the discovery
        entry must never point at a revision that cannot be consumed
        (发布≠就绪声明: this moves the pointer only, it invents no
        ready/status field).  The replacement is atomic (tmp + rename) and
        every other manifest field is preserved verbatim.  Republishing
        the revision the pointer already names is a no-op.  Publishing
        does not imply the revision carries a ``source-map.json`` — that
        mapping arrives with the compile-chain slice; §8's stage-C
        acceptance ("来源映射完整") stays open until then.
        """
        rev_dir = self.root / _REVISIONS_DIR / revision_id
        trace_path = rev_dir / "trace.v3.json"
        if not trace_path.is_file():
            raise ValueError(
                "trace.v3.json does not exist for %s — the manifest is not "
                "moved to a revision without its trace: %s"
                % (revision_id, trace_path))

        manifest_path = self.root / "manifest.json"
        pointer = _REVISIONS_DIR + "/" + revision_id
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest.get("currentRevision") == pointer:
            return manifest_path

        manifest["currentRevision"] = pointer
        # Serialize BEFORE creating the tmp file: a serialization failure
        # must not leave a tmp sibling behind.  Same-directory rename is
        # atomic, so a reader of the discovery entry never sees a torn or
        # missing manifest.
        payload = json.dumps(manifest, indent=2, ensure_ascii=False) + "\n"
        fd = tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=str(self.root), prefix=".manifest.",
            suffix=".json.tmp", delete=False)
        try:
            with fd:
                fd.write(payload)
                fd.flush()
                os.fsync(fd.fileno())
            Path(fd.name).rename(manifest_path)
        except BaseException:
            try:
                Path(fd.name).unlink()
            except FileNotFoundError:
                pass
            raise
        return manifest_path

    @staticmethod
    def _render_steps_md(trace: dict, trace_path: Path) -> str:
        """Render the atomic-operation view from the v3 trace dict.

        The v3 golden keeps ``steps`` as a stepId-keyed map with an
        ``entry`` pointer and a per-step ``next`` link (GoldenTrace.
        to_dict, models.py).  Order follows the chain from ``entry``;
        steps not on the chain are listed after it, never silently
        dropped.  Every field shown comes from the trace.
        """
        steps = trace.get("steps")
        if not isinstance(steps, dict):
            raise ValueError(
                "trace.v3.json has no stepId-keyed 'steps' map: %s"
                % (trace_path,))
        order: list[str] = []
        seen: set[str] = set()
        current = trace.get("entry")
        while current is not None and current in steps and current not in seen:
            seen.add(current)
            order.append(current)
            entry = steps[current] if isinstance(steps[current], dict) else {}
            current = entry.get("next")
        order.extend(step_id for step_id in steps if step_id not in seen)

        lines = ["# 原子操作说明 (views/steps.md)", ""]
        lines.append("来源: %s (唯一主轨迹, 修订不可变)" % (trace_path,))
        lines.append("")
        for step_id in order:
            entry = steps[step_id] if isinstance(steps[step_id], dict) else {}
            action = entry.get("actionId")
            lines.append("- **%s**: actionId=%s" % (step_id, action))
            selector = entry.get("selector")
            if selector:
                lines.append("  - selector: %s" % (json.dumps(
                    selector, ensure_ascii=False, sort_keys=True),))
            end_selector = entry.get("endSelector")
            if end_selector:
                lines.append("  - endSelector: %s" % (json.dumps(
                    end_selector, ensure_ascii=False, sort_keys=True),))
            if entry.get("args"):
                lines.append("  - args: %s" % (json.dumps(
                    entry["args"], ensure_ascii=False, sort_keys=True),))
            verifiers = entry.get("verifiers") or []
            for i, verifier in enumerate(verifiers):
                lines.append("  - verifier[%d]: %s" % (i, json.dumps(
                    verifier, ensure_ascii=False, sort_keys=True)))
            if entry.get("issues"):
                lines.append("  - issues: %s" % (
                    json.dumps(entry["issues"], ensure_ascii=False),))
        lines.append("")
        return "\n".join(lines)

    def _render_readme(self, revision_id: str) -> str:
        """Render the single top-level README (design §5).

        Written once per trajectory folder.  All references are
        folder-relative so the folder stays portable after a copy.
        """
        current = "revisions/" + revision_id
        return (
            "# 轨迹文件夹 README (TRJ-%s)\n"
            "\n"
            "manifest.json 是唯一发现入口: 先读它, 取 `currentRevision` 定位当前修订, "
            "不按目录中哪个文件名字像轨迹来猜。\n"
            "\n"
            "- manifest: [manifest.json](manifest.json) — 指向当前修订 `%s`\n"
            "\n"
            "## 三种用途 (设计 §2, 按 §6 消费顺序)\n"
            "\n"
            "1. 原子操作说明: 读当前修订下的 `views/steps.md` "
            "(如 [%s/views/steps.md](%s/views/steps.md)); "
            "未编译成可执行步骤的原始操作在其中显示为明确 gap。\n"
            "2. EDR-WD 录制与回放: 读当前修订下的 `trace.v3.json` "
            "(如 [%s/trace.v3.json](%s/trace.v3.json)) — 唯一主轨迹。\n"
            "3. MAA 执行: 读当前修订下的 `exports/maa/pipeline.json` "
            "(属 D 阶段片, 生成前该文件不存在, 不预建占位)。\n"
            "\n"
            "固定目录: `sources/`(原始采集与回执), `assets/sha256/`(共享脱敏资源), "
            "`runs/`(执行账本与报告), `recovery/`(修复与重录)。"
            "所有资源与报告使用本夹相对路径, 整夹复制后仍可校验。\n"
        ) % (self.trajectory_id, current, current, current, current, current)

    # -- internals ----------------------------------------------------------

    @classmethod
    def _write_skeleton(cls, root: Path, trajectory_id: str,
                        task_key: str) -> "TrajectoryLayout":
        """Create the fixed sibling dirs and the manifest (write-once).

        An existing manifest is never rewritten: it is the immutable
        discovery entry, and re-materializing a folder must not move its
        current-revision pointer or rewrite its history.
        """
        for rel in (*_FIXED_DIRS, _REVISIONS_DIR):
            (root / rel).mkdir(parents=True, exist_ok=True)

        manifest_path = root / "manifest.json"
        if not manifest_path.exists():
            manifest = {
                "schema": "edr.trajectory-manifest/v1",
                "trajectoryId": trajectory_id,
                "taskKey": task_key,
                "currentRevision": _REVISIONS_DIR + "/REV-0001",
            }
            manifest_path.write_text(
                json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8")
        return cls(root, trajectory_id, task_key)
