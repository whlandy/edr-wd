"""Atomic persistence for deterministic Phase-0 recording artifacts."""

from __future__ import annotations

import json
import os
import tempfile
import hashlib
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable, Mapping

from target.recording.models import RawRecording

from .compiler import CompilationResult, events_for_compiled_steps
from .generate_pytest import (
    render_conftest,
    render_pytest,
    render_pytest_ini,
    safe_test_name,
)
from .models import ReplaySelector, canonical_json
from .selectors import window_for_event
from .templates import generate_redacted_templates
from .trajectory_layout import TrajectoryLayout, source_map_payload_from_diagnostics
from agent.execution import StepMaterializationError


_CAPTURE_ID = re.compile(r"CAP-[0-9a-f]{32}\Z")

#: sources folder name = the recording session id (design §5 ``REC-<id>``).
#: Session ids are produced as ``REC-`` + hex by the target service; the
#: pattern bars path separators/escapes so a hostile id cannot steer the
#: landing path out of ``sources/``.
_SESSION_ID = re.compile(r"REC-[0-9a-zA-Z._-]{1,128}\Z")


@dataclass(frozen=True)
class RecordingArtifacts:
    directory: Path
    recording: Path
    case: Path
    golden_trace: Path
    generated_test: Path
    conftest: Path
    pytest_ini: Path
    compile_report: Path
    compilation: CompilationResult
    #: v3 (design §5): the single-folder TRJ root and its main trace —
    #: both None for v1/v2 flat runs.  Consumers MUST read golden_trace
    #: (the v1/v2 flat path) OR trace.v3_path, never assume the same
    #: directory holds the main trace.
    trajectory_root: Path | None = None
    trace_v3_path: Path | None = None


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content);handle.flush();os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try: os.unlink(temporary)
        except FileNotFoundError: pass
        raise


def _atomic_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content);handle.flush();os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try: os.unlink(temporary)
        except FileNotFoundError: pass
        raise


def _attach_visual_templates(
    directory: Path,
    recording: RawRecording,
    result: CompilationResult,
    captures: Mapping[str, bytes],
) -> CompilationResult:
    """Project target-redacted capture evidence into replay selectors."""
    compiled_events = events_for_compiled_steps(recording.events)
    updated_steps = []
    promoted_sequences: set[int] = set()
    for step, event in zip(result.case.steps, compiled_events):
        # A selector must find the element before dispatch. ``capture`` is
        # the post-action frame; clicks that navigate/close a window may have
        # removed the target by then. The recording lifecycle explicitly
        # links the preceding frame as ``beforeCapture``.
        capture = event.evidence.get("beforeCapture")
        if (
            step.action_id != "gui.click"
            or (step.selector is None and "compile_selector_ambiguous" not in step.issues)
            or event.observed_target is None
            or event.observed_target.rect is None
            or event.observed_target.protected is not False
            or not isinstance(capture, Mapping)
            or capture.get("redacted") is not True
        ):
            updated_steps.append(step)
            continue
        capture_id = capture.get("id")
        if not isinstance(capture_id, str) or _CAPTURE_ID.fullmatch(capture_id) is None:
            updated_steps.append(step)
            continue
        payload = captures.get(capture_id) if isinstance(capture_id, str) else None
        expected_digest = capture.get("sha256")
        actual_digest = (
            "sha256:" + hashlib.sha256(payload).hexdigest() if payload is not None else None
        )
        if payload is None or actual_digest != expected_digest:
            updated_steps.append(step)
            continue

        observation = directory / "assets" / "observations" / f"{capture_id}.png"
        if not observation.is_file():
            updated_steps.append(step)
            continue
        origin = capture.get("origin")
        if (
            not isinstance(origin, (list, tuple))
            or len(origin) != 2
            or not all(
                isinstance(value, int) and not isinstance(value, bool)
                for value in origin
            )
        ):
            # A missing or malformed origin (legacy or tampered evidence) must
            # not silently rebase the crop to (0, 0): the element rectangle
            # would be cut from the wrong region while the PNG digest still
            # verifies, so replay could match the wrong place.  The current
            # target evidence builder always records a strict integer origin;
            # anything else keeps the step non-promotable.
            updated_steps.append(step)
            continue
        origin = [int(origin[0]), int(origin[1])]
        x0, y0, x1, y1 = event.observed_target.rect
        element_rect = (
            x0 - int(origin[0]), y0 - int(origin[1]),
            x1 - int(origin[0]), y1 - int(origin[1]),
        )
        redactions = capture.get("redactions") or []
        try:
            templates = generate_redacted_templates(
                observation,
                directory / "assets",
                step_id=step.step_id,
                element_rect=element_rect,
                redactions=redactions,
            )
        except (OSError, ValueError, StepMaterializationError):
            updated_steps.append(step)
            continue
        visual = {
            "template": templates.element.relative_to(directory).as_posix(),
            "contextTemplate": templates.context.relative_to(directory).as_posix(),
            "elementSha256": templates.element_sha256,
            "contextSha256": templates.context_sha256,
            "contextOffset": list(templates.context_offset),
            "relativePoint": list(templates.relative_point),
            "redacted": True,
        }
        selector = ReplaySelector(
            window=step.selector.window if step.selector is not None else window_for_event(event),
            control=step.selector.control if step.selector is not None else {},
            visual=visual,
        )
        if step.selector is None:
            # A click without semantic identity becomes replayable only after
            # an actual, digest-verified, redacted pre-action template exists.
            # Other compile issues still keep the step incomplete.
            remaining = tuple(code for code in step.issues
                              if code != "compile_selector_ambiguous")
            updated_steps.append(replace(
                step, selector=selector, issues=remaining,
                status="ready" if not remaining else "incomplete",
            ))
            promoted_sequences.add(event.sequence)
        else:
            updated_steps.append(replace(step, selector=selector))

    steps = tuple(updated_steps)
    issues = tuple(issue for issue in result.issues if not (
        issue.code == "compile_selector_ambiguous"
        and issue.sequence in promoted_sequences
    ))
    status = "ready" if not issues and all(step.status == "ready" for step in steps) else "incomplete"
    return replace(
        result,
        case=replace(result.case, steps=steps, status=status),
        golden=replace(result.golden, steps=steps, status=status),
        issues=issues,
    )


def _persist_capture_observations(
    directory: Path,
    recording: RawRecording,
    captures: Mapping[str, bytes],
) -> None:
    """Persist each digest-verified referenced capture exactly once."""
    persisted: set[str] = set()
    for event in recording.events:
        for key in ("beforeCapture", "capture"):
            metadata = event.evidence.get(key)
            if not isinstance(metadata, Mapping):
                continue
            capture_id = metadata.get("id")
            if (
                not isinstance(capture_id, str)
                or _CAPTURE_ID.fullmatch(capture_id) is None
                or capture_id in persisted
            ):
                continue
            payload = captures.get(capture_id)
            expected_digest = metadata.get("sha256")
            actual_digest = (
                "sha256:" + hashlib.sha256(payload).hexdigest()
                if payload is not None else None
            )
            if payload is None or actual_digest != expected_digest:
                continue
            _atomic_bytes(
                directory / "assets" / "observations" / f"{capture_id}.png",
                payload,
            )
            persisted.add(capture_id)


def _write_maa_nodes(directory: Path, result: CompilationResult) -> dict[str, object]:
    """导出 maa 节点表。做不成不影响编译 —— 它是附加产物，不是必需品。"""
    from agent.recording.maa_export import to_maa_nodes

    trace, note = to_maa_nodes(json.loads(result.golden_json()))
    if trace is None:
        return {"written": False, "reason": note}
    path = directory / "trace.json"
    _atomic_text(path, canonical_json(trace))
    return {"written": True, "path": path.name, "nodes": len(trace) - 1}


def _persist_maa_export(trajectory_root: Path, flat_nodes: Path | None) -> None:
    """Copy the flat maa node table into ``revisions/REV-0001/exports/maa/``.

    Design §5: the MAA projection lives inside the revision —
    ``exports/maa/pipeline.json``.  The flat node table (``_write_maa_nodes``,
    the pre-existing face) is the source of truth for this copy: one fact,
    two faces, byte-identical, no second conversion that could drift.  When
    the converter is absent or refused, nothing lands and no placeholder is
    left — the README already declares the file absent until generated.
    """
    target = trajectory_root / "revisions" / "REV-0001" / "exports" / "maa" / "pipeline.json"
    if flat_nodes is not None and flat_nodes.is_file() and not target.exists():
        _atomic_bytes(target, flat_nodes.read_bytes())


_MAA_CAPABILITIES_SCHEMA = "edr.maa-capabilities/v1"


def _maa_capabilities_payload(
    pipeline_path: Path, trace_path: Path,
) -> dict[str, object] | None:
    """Derive the capabilities declaration from the landed projection bytes.

    Design §5:77 declares ``exports/maa/capabilities.json`` as "runtime
    模式、支持/缺口、来源hash".  All three keys are derived from the bytes
    already on disk — never re-converted, never re-declared:

    - runtime mode + supported/gaps: the projection itself carries the
      dependency markers (converter writes ``provenance.actionScope`` /
      ``attach.verification.scope`` only where maa-fw cannot execute
      natively, §6 "明确标注依赖").  Scanning for the *presence* of those
      keys keeps the vocabulary in exactly one place (the converter); a
      new marker kind lands there without a second definition to sync.
    - sourceHashes: sha256 of the landed pipeline.json and trace.v3.json
      bytes — the declaration anchors the very bytes it describes.
    - ``runtimeId`` (§6) is a live-runtime fact that only exists when an
      actual MAA runtime has run the pipeline; it is not forged here —
      a missing key is "not measured", never a fabricated id.

    Returns ``None`` (nothing lands) when the projection is missing,
    malformed, empty of nodes, or when the anchor trace is absent —
    a declaration without its bytes would be a claim, not a fact.
    """
    if not pipeline_path.is_file() or not trace_path.is_file():
        return None
    try:
        pipeline = json.loads(pipeline_path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    if not isinstance(pipeline, dict):
        return None
    supported: list[str] = []
    gaps: list[dict[str, object]] = []
    for node_id, node in pipeline.items():
        if node_id == "$meta" or not isinstance(node, dict):
            continue
        attach = node.get("attach")
        if not isinstance(attach, dict):
            continue
        provenance = attach.get("provenance")
        verification = attach.get("verification")
        kinds: list[str] = []
        if isinstance(provenance, dict) and "actionScope" in provenance:
            kinds.append("action")
        if isinstance(verification, dict) and "scope" in verification:
            kinds.append("verification")
        if kinds:
            gaps.append({
                "node": node_id,
                "kinds": kinds,
                "sourceStepId": (
                    provenance.get("sourceStepId")
                    if isinstance(provenance, dict) else None),
            })
        else:
            supported.append(node_id)
    if not supported and not gaps:
        return None
    return {
        "schema": _MAA_CAPABILITIES_SCHEMA,
        "runtime": {
            "mode": "bridged" if gaps else "pure",
        },
        "supported": supported,
        "gaps": gaps,
        "sourceHashes": {
            "pipeline": "sha256:" + hashlib.sha256(
                pipeline_path.read_bytes()).hexdigest(),
            "trace": "sha256:" + hashlib.sha256(
                trace_path.read_bytes()).hexdigest(),
        },
    }


def _persist_maa_capabilities(trajectory_root: Path) -> None:
    """Land the capabilities declaration beside the landed MAA projection.

    Design §5:77 — ``exports/maa/capabilities.json``.  Derived entirely
    from the on-disk revision bytes (see ``_maa_capabilities_payload``);
    write-once like every revision artifact — revisions are immutable,
    and an existing declaration is never overwritten.  Nothing lands and
    no placeholder is left when the derivation declines (projection
    absent/malformed/empty, anchor trace absent).
    """
    maa_dir = trajectory_root / "revisions" / "REV-0001" / "exports" / "maa"
    target = maa_dir / "capabilities.json"
    if target.exists():
        return
    payload = _maa_capabilities_payload(
        maa_dir / "pipeline.json",
        trajectory_root / "revisions" / "REV-0001" / "trace.v3.json")
    if payload is None:
        return
    _atomic_text(target, canonical_json(payload))


def _rewrite_visual_templates_for_folder(
    trace: dict, flat_assets: Path, assets_dir: Path,
) -> dict:
    """Copy declared visual templates into the trajectory's assets and
    rewrite the on-disk trace's template names to folder-relative ones.

    Design §5: ``assets/sha256/`` holds the shared redacted resources so
    the single folder is self-contained ("整夹复制后仍可定位资源").  The
    flat compile chain has already digest-verified every template it
    declared (``_attach_visual_templates`` writes the PNG and the
    ``elementSha256`` together); here the declared file is re-verified
    against the declared digest before it is copied — a missing or
    tampered source is NOT promoted into the shared resource face, the
    on-disk trace keeps the flat name, and the read-side integrity gate
    stays the fail-closed backstop.  The in-memory golden is untouched:
    the rewrite applies to the landing dict only.
    """
    tables = ("steps", "cleanup")
    for table in tables:
        entries = trace.get(table)
        if isinstance(entries, Mapping):
            iterator = entries.items()
        elif isinstance(entries, (list, tuple)):
            iterator = enumerate(entries)
        else:
            continue
        for key, entry in iterator:
            selector = entry.get("selector") if isinstance(entry, Mapping) else None
            visual = selector.get("visual") if isinstance(selector, Mapping) else None
            if not isinstance(visual, Mapping):
                continue
            for name_key, digest_key in (("template", "elementSha256"),
                                         ("contextTemplate", "contextSha256")):
                name = visual.get(name_key)
                if not isinstance(name, str) or not name:
                    continue
                source = (flat_assets / name).resolve()
                expected = visual.get(digest_key)
                if not source.is_file() or not isinstance(expected, str) or \
                        "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest() != expected:
                    continue
                digest_hex = expected.split(":", 1)[1]
                folder_name = f"assets/sha256/{digest_hex}.png"
                destination = assets_dir / f"{digest_hex}.png"
                if not destination.exists():
                    _atomic_bytes(destination, source.read_bytes())
                visual[name_key] = folder_name
    return trace


def _persist_raw_sources(
    trajectory_root: Path,
    directory: Path,
    recording: RawRecording,
) -> None:
    """Copy the raw-collection evidence into ``sources/REC-<session>/``.

    Design §5: the single folder's ``sources/REC-<id>/`` holds the raw
    collection, stop/persistence/collection receipts.  This slice lands
    the two pieces this chain already writes to the flat directory — the
    ``recording.json`` original and the digest-verified capture frames —
    byte-identical to their flat twins (one fact, two faces, no drift).
    The flat faces stay (§7: raw originals and audit artifacts are not
    removed; the capture-consumer contract keeps its paths).  An existing
    sources original is never overwritten (§7 raw 不覆盖) — the copy is
    per-file write-once and a refusal does not disturb the flat writes.
    The flat observation files are themselves the digest gate: they only
    exist for captures that verified, so scanning them needs no second
    digest pass.
    """
    session = recording.session_id
    if _SESSION_ID.fullmatch(session) is None:
        return
    sources_dir = trajectory_root / "sources" / session
    flat_recording = directory / "recording.json"
    target = sources_dir / "recording.json"
    if flat_recording.is_file() and not target.exists():
        _atomic_bytes(target, flat_recording.read_bytes())
    for event in recording.events:
        for key in ("beforeCapture", "capture"):
            metadata = event.evidence.get(key)
            if not isinstance(metadata, Mapping):
                continue
            capture_id = metadata.get("id")
            if (
                not isinstance(capture_id, str)
                or _CAPTURE_ID.fullmatch(capture_id) is None
            ):
                continue
            flat_capture = directory / "assets" / "observations" / f"{capture_id}.png"
            target = sources_dir / f"{capture_id}.png"
            if flat_capture.is_file() and not target.exists():
                _atomic_bytes(target, flat_capture.read_bytes())


def _persist_stop_receipt(
    trajectory_root: Path,
    directory: Path,
    recording: RawRecording,
    *,
    golden_status: str,
    capture_issues: Iterable[Mapping[str, object]],
    persistence: Mapping[str, object] | None = None,
) -> None:
    """Land the stop/collection receipt into ``sources/REC-<session>/``.

    Design §5: the sources folder also holds the stop/persistence/collection
    receipts.  The three stop-chain callers (CLI ``record stop``, the UI
    bridge, the rerecord loop) each assemble their own operator-facing
    payload, but they share this chain — so the receipt lands here, once,
    with the fields that are chain facts (not operator stdout): the golden
    status, the capture-evidence gaps, and the flat artifact directory.
    ``persistence`` is the target's ``resultStore`` fact when the stop
    response carries one (CLI/rerecord-loop paths); the UI bridge reads back
    through the durable store, where that fact does not exist, so the key is
    simply absent — a missing key is "fact not on this channel", never a
    forged ``null``.  The raw target result is deliberately not copied —
    ``recording.json`` in the same folder is already that fact's original
    (§7: one fact, two faces; a receipt that duplicates the raw can drift).
    Write-once like every sources artifact: an existing receipt is never
    overwritten.  The folder name reuses the ``_SESSION_ID`` guard from
    ``_persist_raw_sources`` — a hostile session id lands nothing.
    """
    session = recording.session_id
    if _SESSION_ID.fullmatch(session) is None:
        return
    receipt = {
        "sessionId": session,
        "goldenStatus": golden_status,
        "directory": str(directory),
        "captureIssues": [dict(issue) for issue in capture_issues],
    }
    if persistence is not None:
        receipt["persistence"] = dict(persistence)
    target = trajectory_root / "sources" / session / "stop-receipt.json"
    if not target.exists():
        _atomic_bytes(target, canonical_json(receipt).encode("utf-8"))


def _write_v3_trajectory(
    output_root: Path,
    recording: RawRecording,
    result: CompilationResult,
    flat_assets: Path | None = None,
    capture_issues: Iterable[Mapping[str, object]] = (),
    persistence: Mapping[str, object] | None = None,
) -> Path:
    """Land a v3 golden in the §5 single-folder layout under output_root.

    Design §5: one trajectory = one folder, trace.v3.json is the only main
    trace, source-map.json is the same-revision raw-event mapping.  This
    reuses the TrajectoryLayout write entries (round 18/19) — no third
    writer is invented here.  The manifest pointer is NOT moved: publishing
    a revision is a separate, explicit step.  The flat golden-trace.json
    write is intentionally skipped for v3 — two copies of the main trace
    would be two copies that can drift.  Declared visual templates are
    copied into ``assets/sha256/`` (digest-verified against the flat
    compile output) so the folder is self-contained, and the raw
    collection evidence is copied into ``sources/REC-<session>/`` (the
    recording original + digest-verified frames, byte-identical to the
    flat twins — see ``_persist_raw_sources``) and the stop/collection
    receipt lands there too (``_persist_stop_receipt`` — chain facts
    only, no raw payload).  ``flat_assets`` is
    the flat artifact root — golden template names ("assets/<step>...png")
    are relative to it.
    """
    from .models import GOLDEN_TRACE_SCHEMA_V3

    if result.golden.schema != GOLDEN_TRACE_SCHEMA_V3:
        return None
    layout = TrajectoryLayout.create(output_root, recording.name)
    trace = result.golden.to_dict()
    if flat_assets is not None:
        trace = _rewrite_visual_templates_for_folder(
            trace, flat_assets, layout.root / "assets" / "sha256")
    layout.write_revision_trace("REV-0001", trace)
    layout.write_revision_source_map(
        "REV-0001", source_map_payload_from_diagnostics(dict(result.diagnostics)))
    # §5: the views and README are rendered from the on-disk trace (the
    # folder's truth, including the visual template rewrite above) by the
    # round-3 projection — no third renderer is invented here.
    layout.write_revision_views("REV-0001")
    _persist_raw_sources(layout.root, flat_assets, recording)
    _persist_stop_receipt(
        layout.root, flat_assets, recording,
        golden_status=result.golden.status, capture_issues=capture_issues,
        persistence=persistence)
    return layout.root


def write_compilation_artifacts(
    output_root: str | Path,
    recording: RawRecording,
    result: CompilationResult,
    *,
    captures: Mapping[str, bytes] | None = None,
    artifact_issues: Iterable[Mapping[str, object]] = (),
    persistence: Mapping[str, object] | None = None,
) -> RecordingArtifacts:
    directory = Path(output_root).expanduser().resolve() / safe_test_name(recording.name)
    if captures:
        _persist_capture_observations(directory, recording, captures)
        result = _attach_visual_templates(directory, recording, result, captures)
    artifacts = RecordingArtifacts(
        directory=directory,
        recording=directory / "recording.json",
        case=directory / "case.json",
        golden_trace=directory / "golden-trace.json",
        generated_test=directory / f"test_{safe_test_name(recording.name)}.py",
        conftest=directory / "conftest.py",
        pytest_ini=directory / "pytest.ini",
        compile_report=directory / "compile-report.json",
        compilation=result,
    )
    _atomic_text(artifacts.recording, canonical_json(recording.to_dict()))
    _atomic_text(artifacts.case, result.case_json())
    # v3 (design §5): the main trace lands in the single-folder layout —
    # not a flat golden-trace.json, which for v3 would be a second,
    # drift-capable copy.  v1/v2 keep the flat write unchanged.
    trajectory_root = _write_v3_trajectory(
        Path(output_root).expanduser().resolve(), recording, result,
        flat_assets=directory, capture_issues=artifact_issues,
        persistence=persistence)
    if trajectory_root is not None:
        # 评审 P1-3: the returned object must point at the real main trace.
        # from_manifest reads manifest.json's currentRevision pointer — the
        # same contract the Skill uses, so a published revision move never
        # desyncs the reported path.
        from .trajectory_layout import from_manifest
        artifacts = replace(
            artifacts,
            trajectory_root=trajectory_root,
            trace_v3_path=from_manifest(trajectory_root))
    else:
        _atomic_text(artifacts.golden_trace, result.golden_json())
    # 顺手导出 maa-fw 的节点表。靠人记得跑一条命令是不行的 —— 忘了不会报错，
    # 只会变成「maa-fw 那边加载了什么都不做」，两边都看不出来。
    maa_note = _write_maa_nodes(directory, result)
    # v3 (design §5): the MAA projection also lives inside the revision
    # (exports/maa/pipeline.json) — a byte-identical copy of the flat
    # table above (one fact, two faces).  Nothing lands when the flat
    # write was refused (converter absent / conversion failed).
    if trajectory_root is not None:
        _persist_maa_export(
            trajectory_root,
            directory / "trace.json" if maa_note.get("written") else None)
        _persist_maa_capabilities(trajectory_root)
    report = result.report_dict()
    report["artifactIssues"] = [dict(issue) for issue in artifact_issues]
    report["maaExport"] = maa_note
    # Review 0747 P1-4b: the CORE artifacts (recording/case/golden/
    # compile-report with sourceEvents) must always land -- a stop/collect
    # run must not lose its diagnostics because an OPTIONAL export is
    # unsupported. The optional pytest projection for golden v3 is refused
    # explicitly (no legacy executable file, no silent contract drop) AFTER
    # the core write completes.
    _atomic_text(artifacts.compile_report, canonical_json(report))
    if result.golden.schema == "edr.desktop-golden-trace/v3":
        # 减容(§4.2 默认不再重复生成 pytest): the v3 pytest projection is
        # refused, so the scaffold that exists only to serve a generated
        # pytest run (the non-executable placeholder, conftest.py,
        # pytest.ini) is not written per compile. The refusal stays in the
        # compile report; v1/v2 keep their full flat set unchanged.
        report["pytestExport"] = {
            "supported": False,
            "reason": "golden v3 requires the window-binding and effects "
                      "capabilities (design slices S3/S4)",
        }
        _atomic_text(artifacts.compile_report, canonical_json(report))
    else:
        _atomic_text(artifacts.generated_test, render_pytest(recording.name))
        _atomic_text(artifacts.conftest, render_conftest())
        _atomic_text(artifacts.pytest_ini, render_pytest_ini())
    return artifacts
