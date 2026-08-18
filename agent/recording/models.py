"""Strict, deterministic agent-side recording and golden-trace models."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from target.recording.models import RecordingModelError

COMPILED_CASE_SCHEMA = "edr.desktop-recorded-case/v1"
GOLDEN_TRACE_SCHEMA = "edr.desktop-golden-trace/v1"
REPLAY_EVALUATION_SCHEMA = "edr.desktop-replay-evaluation/v1"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"


def _strict(data: Mapping[str, Any], allowed: set[str], path: str) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise RecordingModelError("type_error", f"{path} must be an object", path=path)
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise RecordingModelError("unknown_field", f"{path} has unknown fields: {unknown}", path=path)
    return dict(data)


def _require(data: Mapping[str, Any], required: set[str], path: str) -> None:
    missing = sorted(required - set(data))
    if missing:
        raise RecordingModelError(
            "missing_field", f"{path} is missing required fields: {missing}", path=path
        )


def _validate_verifier(value: Mapping[str, Any], path: str) -> None:
    data = _strict(value, {"type", "expected", "timeoutSeconds"}, path)
    _require(data, {"type", "expected"}, path)
    kind = data["type"]
    if kind not in {
        "text_equals", "text_contains", "value_equals", "visible", "checked",
        "enabled", "window_open", "text_contains_time",
    }:
        raise RecordingModelError(
            "golden_verifier_unsupported", f"unsupported verifier {kind!r}",
            path=f"{path}.type",
        )
    expected = data["expected"]
    if kind == "text_contains_time" and (not isinstance(expected, str) or not expected):
        raise RecordingModelError(
            "type_error",
            "text_contains_time expected must be a non-empty strftime pattern",
            path=f"{path}.expected",
        )
    if kind in {"text_equals", "text_contains", "value_equals"} and not isinstance(expected, str):
        raise RecordingModelError("type_error", f"{kind} expected must be a string", path=f"{path}.expected")
    if kind in {"visible", "checked", "enabled"} and not isinstance(expected, bool):
        raise RecordingModelError("type_error", f"{kind} expected must be boolean", path=f"{path}.expected")
    if kind == "window_open":
        if not isinstance(expected, (bool, Mapping)):
            raise RecordingModelError("type_error", "window_open expected must be boolean or object", path=f"{path}.expected")
        if isinstance(expected, Mapping):
            window = _strict(
                expected,
                {"exists", "processName", "process_name", "title", "titleRegex"},
                f"{path}.expected",
            )
            if "exists" in window and not isinstance(window["exists"], bool):
                raise RecordingModelError("type_error", "window exists must be boolean", path=f"{path}.expected.exists")
            for key in ("processName", "process_name", "title", "titleRegex"):
                if key in window and not isinstance(window[key], str):
                    raise RecordingModelError("type_error", f"window {key} must be a string", path=f"{path}.expected.{key}")
    timeout = data.get("timeoutSeconds", 10.0)
    if (
        not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
        or not 0 < float(timeout) <= 300
    ):
        raise RecordingModelError("type_error", "verifier timeoutSeconds must be in (0, 300]", path=f"{path}.timeoutSeconds")


@dataclass(frozen=True)
class ReplaySelector:
    window: Mapping[str, Any]
    control: Mapping[str, Any]
    visual: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.window, Mapping) or not isinstance(self.control, Mapping):
            raise RecordingModelError("type_error", "selector window/control must be objects", path="selector")
        allowed_window = {"processName", "titleRegex", "bundleId"}
        allowed_control = {
            "automationId", "identifier", "controlType", "name", "ancestry",
            "anchor", "fingerprint",
        }
        allowed_visual = {
            "template", "contextTemplate", "elementSha256", "contextSha256",
            "relativePoint", "redacted",
        }
        unknown = (
            {f"window.{key}" for key in set(self.window) - allowed_window}
            | {f"control.{key}" for key in set(self.control) - allowed_control}
            | (
                {f"visual.{key}" for key in set(self.visual) - allowed_visual}
                if isinstance(self.visual, Mapping) else set()
            )
        )
        if unknown:
            raise RecordingModelError(
                "unknown_field", f"selector has unknown fields: {sorted(unknown)}",
                path="selector",
            )
        if self.visual is not None and not isinstance(self.visual, Mapping):
            raise RecordingModelError(
                "type_error", "selector visual must be an object or null", path="selector.visual"
            )
        for field_name, value in self.window.items():
            if value is not None and not isinstance(value, str):
                raise RecordingModelError(
                    "type_error", f"selector.window.{field_name} must be a string or null",
                    path=f"selector.window.{field_name}",
                )
        for field_name in ("automationId", "identifier", "controlType", "name", "fingerprint"):
            value = self.control.get(field_name)
            if value is not None and not isinstance(value, str):
                raise RecordingModelError(
                    "type_error", f"selector.control.{field_name} must be a string",
                    path=f"selector.control.{field_name}",
                )
        ancestry = self.control.get("ancestry", [])
        if not isinstance(ancestry, (list, tuple)) or not all(
            isinstance(item, Mapping) for item in ancestry
        ):
            raise RecordingModelError(
                "type_error", "selector.control.ancestry must be an array of objects",
                path="selector.control.ancestry",
            )
        anchor = self.control.get("anchor")
        if anchor is not None and not isinstance(anchor, Mapping):
            raise RecordingModelError(
                "type_error", "selector.control.anchor must be an object or null",
                path="selector.control.anchor",
            )
        if self.visual is not None:
            template = self.visual.get("template")
            digest = self.visual.get("elementSha256")
            if not isinstance(template, str) or not template:
                raise RecordingModelError(
                    "visual_template_missing", "visual template path is missing",
                    path="selector.visual.template",
                )
            if self.visual.get("redacted") is not True:
                raise RecordingModelError(
                    "visual_template_not_redacted", "visual template must be source-redacted",
                    path="selector.visual.redacted",
                )
            if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise RecordingModelError(
                    "visual_template_digest_invalid", "visual template requires a SHA-256 digest",
                    path="selector.visual.elementSha256",
                )
            if self.visual.get("contextTemplate") is not None:
                context_digest = self.visual.get("contextSha256")
                if not isinstance(context_digest, str) or not re.fullmatch(
                    r"sha256:[0-9a-f]{64}", context_digest
                ):
                    raise RecordingModelError(
                        "visual_template_digest_invalid",
                        "context template requires a SHA-256 digest",
                        path="selector.visual.contextSha256",
                    )
            relative = self.visual.get("relativePoint", [0.5, 0.5])
            if (
                not isinstance(relative, (list, tuple)) or len(relative) != 2
                or not all(
                    isinstance(value, (int, float)) and not isinstance(value, bool)
                    and 0 <= value <= 1 for value in relative
                )
            ):
                raise RecordingModelError(
                    "visual_relative_point_invalid",
                    "relativePoint must contain two numbers in [0, 1]",
                    path="selector.visual.relativePoint",
                )
        forbidden = {"snapshotId", "snapshot_id", "targetId", "target_id", "pid", "handle", "native_window_id"}
        leaked = forbidden.intersection(self.window) | forbidden.intersection(self.control)
        if leaked:
            raise RecordingModelError("ephemeral_selector", f"selector contains ephemeral fields: {sorted(leaked)}", path="selector")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ReplaySelector":
        d = _strict(data, {"window", "control", "visual"}, "selector")
        _require(d, {"window", "control", "visual"}, "selector")
        if not isinstance(d["window"], Mapping) or not isinstance(d["control"], Mapping):
            raise RecordingModelError(
                "type_error", "selector window/control must be objects", path="selector"
            )
        if d["visual"] is not None and not isinstance(d["visual"], Mapping):
            raise RecordingModelError(
                "type_error", "selector visual must be object or null", path="selector.visual"
            )
        return cls(
            dict(d["window"]), dict(d["control"]),
            dict(d["visual"]) if d["visual"] is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {"window": dict(self.window), "control": dict(self.control), "visual": dict(self.visual) if self.visual else None}


@dataclass(frozen=True)
class RecordedStep:
    step_id: str
    action_id: str | None
    args: Mapping[str, Any]
    selector: ReplaySelector | None
    verifiers: tuple[Mapping[str, Any], ...] = ()
    required: bool = True
    status: str = "ready"
    issues: tuple[str, ...] = ()
    #: Release target of a two-point action (currently ``pointer.drag``).  It is
    #: resolved against the same fresh observation as ``selector``, so a drag
    #: never replays recorded screen coordinates.
    end_selector: "ReplaySelector | None" = None

    def __post_init__(self) -> None:
        if not isinstance(self.step_id, str) or not self.step_id:
            raise RecordingModelError("type_error", "step_id must be non-empty", path="step.stepId")
        if (
            self.action_id is None and not self.verifiers
            and not (self.status == "incomplete" and self.issues)
        ):
            raise RecordingModelError(
                "recorded_step_empty", "step requires an action or verifier", path=f"step.{self.step_id}"
            )
        if self.status not in {"ready", "incomplete"}:
            raise RecordingModelError("type_error", "invalid step status", path=f"step.{self.step_id}.status")
        if not isinstance(self.args, Mapping) or not isinstance(self.required, bool):
            raise RecordingModelError("type_error", "invalid recorded step fields", path=f"step.{self.step_id}")
        if self.action_id is not None and not isinstance(self.action_id, str):
            raise RecordingModelError("type_error", "actionId must be string or null", path=f"step.{self.step_id}.actionId")
        if self.selector is not None and not isinstance(self.selector, ReplaySelector):
            raise RecordingModelError("type_error", "selector has invalid type", path=f"step.{self.step_id}.selector")
        if self.end_selector is not None and not isinstance(self.end_selector, ReplaySelector):
            raise RecordingModelError(
                "type_error", "endSelector has invalid type", path=f"step.{self.step_id}.endSelector"
            )
        if not all(isinstance(item, Mapping) for item in self.verifiers):
            raise RecordingModelError("type_error", "verifiers must contain objects", path=f"step.{self.step_id}.verifiers")
        for index, verifier in enumerate(self.verifiers):
            _validate_verifier(verifier, f"step.{self.step_id}.verifiers.{index}")
        if not all(isinstance(item, str) for item in self.issues):
            raise RecordingModelError("type_error", "issues must contain strings", path=f"step.{self.step_id}.issues")

    def to_dict(self) -> dict[str, Any]:
        return {
            "stepId": self.step_id, "actionId": self.action_id,
            "args": dict(self.args), "selector": self.selector.to_dict() if self.selector else None,
            "endSelector": self.end_selector.to_dict() if self.end_selector else None,
            "verifiers": [dict(x) for x in self.verifiers], "required": self.required,
            "status": self.status, "issues": list(self.issues),
        }


@dataclass(frozen=True)
class RecordedTestCase:
    name: str
    source_session_id: str
    steps: tuple[RecordedStep, ...]
    status: str
    cleanup: tuple[RecordedStep, ...] = ()
    schema: str = COMPILED_CASE_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise RecordingModelError("type_error", "case name must be non-empty", path="case.name")
        if not isinstance(self.source_session_id, str) or not self.source_session_id:
            raise RecordingModelError(
                "type_error", "sourceSessionId must be non-empty", path="case.sourceSessionId"
            )
        if self.status not in {"ready", "incomplete"}:
            raise RecordingModelError("type_error", "invalid case status", path="case.status")
        identifiers = [step.step_id for step in (*self.steps, *self.cleanup)]
        if len(identifiers) != len(set(identifiers)):
            raise RecordingModelError("step_graph_invalid", "step ids must be unique", path="case.steps")
        if self.status == "ready" and any(step.status != "ready" for step in (*self.steps, *self.cleanup)):
            raise RecordingModelError(
                "case_status_invalid", "ready case contains an incomplete step", path="case.status"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "name": self.name,
            "sourceSessionId": self.source_session_id,
            "status": self.status,
            "steps": [x.to_dict() for x in self.steps],
            "cleanup": [x.to_dict() for x in self.cleanup],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RecordedTestCase":
        d = _strict(
            data,
            {"schema", "name", "sourceSessionId", "status", "steps", "cleanup"},
            "case",
        )
        _require(
            d, {"schema", "name", "sourceSessionId", "status", "steps", "cleanup"},
            "case",
        )
        if d.get("schema") != COMPILED_CASE_SCHEMA:
            raise RecordingModelError(
                "unsupported_schema", f"unsupported schema {d.get('schema')!r}",
                path="case.schema",
            )
        raw_steps = d.get("steps")
        raw_cleanup = d.get("cleanup", [])
        if not isinstance(raw_steps, list) or not isinstance(raw_cleanup, list):
            raise RecordingModelError(
                "type_error", "steps and cleanup must be arrays", path="case.steps"
            )
        return cls(
            name=d.get("name", ""),
            source_session_id=d.get("sourceSessionId", ""),
            status=d.get("status", "incomplete"),
            steps=tuple(
                _recorded_step_from(item, f"step-{index:04d}", f"case.steps.{index}")
                for index, item in enumerate(raw_steps, start=1)
            ),
            cleanup=tuple(
                _recorded_step_from(item, f"cleanup-{index:04d}", f"case.cleanup.{index}")
                for index, item in enumerate(raw_cleanup, start=1)
            ),
            schema=d["schema"],
        )


@dataclass(frozen=True)
class GoldenTrace:
    name: str
    source_recording: Mapping[str, Any]
    catalog: Mapping[str, Any]
    environment: Mapping[str, Any]
    steps: tuple[RecordedStep, ...]
    status: str
    cleanup: tuple[RecordedStep, ...] = ()
    schema: str = GOLDEN_TRACE_SCHEMA

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise RecordingModelError("type_error", "golden name must be non-empty", path="golden.name")
        if self.status not in {"ready", "incomplete"}:
            raise RecordingModelError("type_error", "invalid golden status", path="golden.status")
        identifiers = [step.step_id for step in (*self.steps, *self.cleanup)]
        if len(identifiers) != len(set(identifiers)):
            raise RecordingModelError("step_graph_invalid", "step ids must be unique", path="golden.steps")
        if self.status == "ready" and any(step.status != "ready" for step in (*self.steps, *self.cleanup)):
            raise RecordingModelError(
                "golden_status_invalid", "ready golden trace contains an incomplete step",
                path="golden.status",
            )

    def to_dict(self) -> dict[str, Any]:
        step_map: dict[str, Any] = {}
        for index, step in enumerate(self.steps):
            item = step.to_dict()
            item["next"] = self.steps[index + 1].step_id if index + 1 < len(self.steps) else None
            step_map[step.step_id] = item
        return {
            "schema": self.schema, "status": self.status, "name": self.name,
            "sourceRecording": dict(self.source_recording), "catalog": dict(self.catalog),
            "environment": dict(self.environment),
            "entry": self.steps[0].step_id if self.steps else None,
            "steps": step_map,
            "cleanup": [step.to_dict() for step in self.cleanup],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GoldenTrace":
        d = _strict(data, {"schema", "status", "name", "sourceRecording", "catalog", "environment", "entry", "steps", "cleanup"}, "golden")
        _require(
            d,
            {"schema", "status", "name", "sourceRecording", "catalog", "environment", "entry", "steps", "cleanup"},
            "golden",
        )
        if d.get("schema") != GOLDEN_TRACE_SCHEMA:
            raise RecordingModelError("unsupported_schema", f"unsupported schema {d.get('schema')!r}", path="golden.schema")
        raw_steps = d.get("steps")
        if not isinstance(raw_steps, Mapping):
            raise RecordingModelError("type_error", "steps must be an object", path="golden.steps")
        ordered: list[RecordedStep] = []
        current = d.get("entry")
        seen: set[str] = set()
        while current is not None:
            if current in seen or current not in raw_steps:
                raise RecordingModelError("step_graph_invalid", "golden step graph is cyclic or dangling", path="golden.steps")
            seen.add(current); item = dict(raw_steps[current]); nxt = item.pop("next", None)
            item = _strict(item, _STEP_FIELDS, f"golden.steps.{current}")
            parsed = _recorded_step_from(item, current, f"golden.steps.{current}")
            if parsed.step_id != current:
                raise RecordingModelError(
                    "step_graph_invalid", "stepId must match its graph key", path=f"golden.steps.{current}.stepId"
                )
            ordered.append(parsed)
            current = nxt
        if seen != set(raw_steps):
            raise RecordingModelError(
                "step_graph_invalid", "golden step graph contains disconnected steps", path="golden.steps"
            )
        raw_cleanup = d.get("cleanup", [])
        if not isinstance(raw_cleanup, list):
            raise RecordingModelError("type_error", "cleanup must be an array", path="golden.cleanup")
        cleanup = tuple(
            _recorded_step_from(item, f"cleanup-{index:04d}", f"golden.cleanup.{index}")
            for index, item in enumerate(raw_cleanup, start=1)
        )
        source_recording = _strict(
            d["sourceRecording"], {"schema", "sha256"}, "golden.sourceRecording"
        )
        _require(source_recording, {"schema", "sha256"}, "golden.sourceRecording")
        catalog = _strict(d["catalog"], {"version", "digest"}, "golden.catalog")
        _require(catalog, {"version", "digest"}, "golden.catalog")
        environment = _strict(
            d["environment"], {"profile", "backend", "application", "target"},
            "golden.environment",
        )
        for path, value in (
            ("golden.name", d["name"]),
            ("golden.sourceRecording.schema", source_recording["schema"]),
            ("golden.sourceRecording.sha256", source_recording["sha256"]),
            ("golden.catalog.version", catalog["version"]),
            ("golden.catalog.digest", catalog["digest"]),
        ):
            if not isinstance(value, str) or not value:
                raise RecordingModelError("type_error", f"{path} must be non-empty", path=path)
        if not all(isinstance(value, str) for value in environment.values()):
            raise RecordingModelError(
                "type_error", "golden environment values must be strings",
                path="golden.environment",
            )
        return cls(
            name=d["name"], source_recording=source_recording,
            catalog=catalog, environment=environment,
            steps=tuple(ordered), status=d.get("status", "incomplete"),
            cleanup=cleanup, schema=d["schema"],
        )


_STEP_FIELDS = {
    "stepId", "actionId", "args", "selector", "endSelector", "verifiers",
    "required", "status", "issues",
}


def _recorded_step_from(data: Mapping[str, Any], fallback_id: str, path: str) -> RecordedStep:
    item = _strict(data, _STEP_FIELDS, path)
    _require(item, _STEP_FIELDS, path)
    if not isinstance(item["args"], Mapping):
        raise RecordingModelError("type_error", "step args must be an object", path=f"{path}.args")
    if not isinstance(item["verifiers"], list) or not all(
        isinstance(value, Mapping) for value in item["verifiers"]
    ):
        raise RecordingModelError(
            "type_error", "step verifiers must be an array of objects", path=f"{path}.verifiers"
        )
    if not isinstance(item["issues"], list):
        raise RecordingModelError("type_error", "step issues must be an array", path=f"{path}.issues")
    for key in ("selector", "endSelector"):
        if item[key] is not None and not isinstance(item[key], Mapping):
            raise RecordingModelError(
                "type_error", f"step {key} must be object or null", path=f"{path}.{key}"
            )
    return RecordedStep(
        step_id=item["stepId"],
        action_id=item["actionId"],
        args=dict(item["args"]),
        selector=ReplaySelector.from_dict(item["selector"]) if item["selector"] is not None else None,
        verifiers=tuple(dict(value) for value in item["verifiers"]),
        required=item["required"],
        status=item["status"],
        issues=tuple(item["issues"]),
        end_selector=(
            ReplaySelector.from_dict(item["endSelector"])
            if item["endSelector"] is not None else None
        ),
    )


@dataclass(frozen=True)
class ReplayEvaluation:
    task_success: bool
    required_steps: int
    passed_steps: int
    assertion_pass_rate: float
    semantic_resolution_rate: float
    visual_fallback_count: int = 0
    coordinate_fallback_count: int = 0
    extra_action_count: int = 0
    retry_count: int = 0
    path_fidelity: float = 1.0
    cleanup_passed: bool = True
    trace_integrity: bool = True
    schema: str = REPLAY_EVALUATION_SCHEMA

    def __post_init__(self) -> None:
        for field_name in ("assertion_pass_rate", "semantic_resolution_rate", "path_fidelity"):
            value = getattr(self, field_name)
            if not isinstance(value, (int, float)) or not 0.0 <= value <= 1.0:
                raise RecordingModelError(
                    "evaluation_rate_invalid", f"{field_name} must be in [0, 1]",
                    path=f"evaluation.{field_name}",
                )
        for field_name in (
            "required_steps", "passed_steps", "visual_fallback_count",
            "coordinate_fallback_count", "extra_action_count", "retry_count",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, int) or value < 0:
                raise RecordingModelError(
                    "evaluation_count_invalid", f"{field_name} must be non-negative",
                    path=f"evaluation.{field_name}",
                )
        if self.passed_steps > self.required_steps:
            raise RecordingModelError(
                "evaluation_count_invalid", "passed_steps cannot exceed required_steps",
                path="evaluation.passedSteps",
            )
        computed = (
            self.passed_steps == self.required_steps
            and self.assertion_pass_rate == 1.0
            and self.extra_action_count == 0
            and self.cleanup_passed and self.trace_integrity
        )
        if self.task_success and not computed:
            raise RecordingModelError("task_success_invalid", "taskSuccess contradicts required outcomes", path="evaluation.taskSuccess")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema, "taskSuccess": self.task_success,
            "requiredSteps": self.required_steps, "passedSteps": self.passed_steps,
            "assertionPassRate": self.assertion_pass_rate,
            "semanticResolutionRate": self.semantic_resolution_rate,
            "visualFallbackCount": self.visual_fallback_count,
            "coordinateFallbackCount": self.coordinate_fallback_count,
            "extraActionCount": self.extra_action_count, "retryCount": self.retry_count,
            "pathFidelity": self.path_fidelity,
            "cleanupPassed": self.cleanup_passed, "traceIntegrity": self.trace_integrity,
        }
