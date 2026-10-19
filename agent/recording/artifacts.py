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
from agent.execution import StepMaterializationError


_CAPTURE_ID = re.compile(r"CAP-[0-9a-f]{32}\Z")


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
        origin = capture.get("origin") or [0, 0]
        if not isinstance(origin, (list, tuple)) or len(origin) != 2:
            origin = [0, 0]
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


def write_compilation_artifacts(
    output_root: str | Path,
    recording: RawRecording,
    result: CompilationResult,
    *,
    captures: Mapping[str, bytes] | None = None,
    artifact_issues: Iterable[Mapping[str, object]] = (),
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
    _atomic_text(artifacts.golden_trace, result.golden_json())
    # 顺手导出 maa-fw 的节点表。靠人记得跑一条命令是不行的 —— 忘了不会报错，
    # 只会变成「maa-fw 那边加载了什么都不做」，两边都看不出来。
    maa_note = _write_maa_nodes(directory, result)
    _atomic_text(artifacts.generated_test, render_pytest(recording.name))
    _atomic_text(artifacts.conftest, render_conftest())
    _atomic_text(artifacts.pytest_ini, render_pytest_ini())
    report = result.report_dict()
    report["artifactIssues"] = [dict(issue) for issue in artifact_issues]
    report["maaExport"] = maa_note
    _atomic_text(artifacts.compile_report, canonical_json(report))
    return artifacts
