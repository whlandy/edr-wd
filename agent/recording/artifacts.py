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
    for step, event in zip(result.case.steps, compiled_events):
        capture = event.evidence.get("capture")
        if (
            step.action_id != "gui.click"
            or step.selector is None
            or event.observed_target is None
            or event.observed_target.rect is None
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
            "relativePoint": list(templates.relative_point),
            "redacted": True,
        }
        selector = ReplaySelector(
            window=step.selector.window,
            control=step.selector.control,
            visual=visual,
        )
        updated_steps.append(replace(step, selector=selector))

    steps = tuple(updated_steps)
    return replace(
        result,
        case=replace(result.case, steps=steps),
        golden=replace(result.golden, steps=steps),
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
    artifacts = RecordingArtifacts(
        directory=directory,
        recording=directory / "recording.json",
        case=directory / "case.json",
        golden_trace=directory / "golden-trace.json",
        generated_test=directory / f"test_{safe_test_name(recording.name)}.py",
        conftest=directory / "conftest.py",
        pytest_ini=directory / "pytest.ini",
        compile_report=directory / "compile-report.json",
    )
    if captures:
        _persist_capture_observations(directory, recording, captures)
        result = _attach_visual_templates(directory, recording, result, captures)
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
