"""Strict, deterministic agent-side recording and golden-trace models."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from target.recording.models import RecordingModelError

COMPILED_CASE_SCHEMA = "edr.desktop-recorded-case/v1"
GOLDEN_TRACE_SCHEMA = "edr.desktop-golden-trace/v1"
#: v2 adds the optional, strictly-parsed `entryWindow` identity record.
GOLDEN_TRACE_SCHEMA_V2 = "edr.desktop-golden-trace/v2"
#: v3 adds the strict windowSequence (design doc 09 §5): per-step window
#: ownership + effects. v3 artifacts are NOT executable until S3/S4 land.
GOLDEN_TRACE_SCHEMA_V3 = "edr.desktop-golden-trace/v3"
WINDOW_SEQUENCE_SCHEMA = "edr.window-sequence/v1"
ENTRY_WINDOW_SCHEMA = "edr.entry-window-identity/v1"
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
    # region_query flattens its v2 draft into semantic verifier fields (no
    # scalar ``expected``): match names the container, the condition tree /
    # content-time / observation blocks carry the semantics, all validated by
    # the shared region validator so compile and replay cannot drift.
    if isinstance(value, Mapping) and value.get("type") == "region_query":
        data = _strict(
            value,
            {"type", "match", "target", "quantifier", "conditions",
             "contentTime", "observation", "timeoutSeconds"},
            path,
        )
        _require(data, {"type", "match", "target", "quantifier",
                        "conditions", "contentTime", "observation"}, path)
        match = _strict(
            data["match"],
            {"automation_id", "identifier", "control_type", "text"},
            f"{path}.match",
        )
        if (not all(isinstance(item, str) and item for item in match.values())
                or not (match.get("automation_id") or match.get("identifier")
                        or (match.get("control_type") and match.get("text")))):
            raise RecordingModelError(
                "type_error", "region_query match requires a stable container identity",
                path=f"{path}.match",
            )
        try:
            from target.recording.region_assertion import (
                validate_condition_tree, validate_content_time,
                validate_observation,
            )
        except ImportError:  # pragma: no cover - flat deployment layout
            from recording.region_assertion import (  # type: ignore[no-redef]
                validate_condition_tree, validate_content_time,
                validate_observation,
            )
        quantifier = data["quantifier"]
        if quantifier not in ("any", "all"):
            raise RecordingModelError(
                "type_error", "region_query quantifier must be 'any' or 'all'",
                path=f"{path}.quantifier",
            )
        target = data["target"]
        if not isinstance(target, Mapping) or target.get("mode") not in (
                "container_rows", "fixed_components"):
            raise RecordingModelError(
                "type_error", "region_query target.mode is invalid",
                path=f"{path}.target",
            )
        content_time = validate_content_time(data["contentTime"], f"{path}.contentTime")
        validate_condition_tree(
            data["conditions"], f"{path}.conditions", root=True,
            content_time_enabled=content_time["enabled"],
        )
        validate_observation(
            data["observation"], f"{path}.observation", has_binding=True,
        )
        timeout = data.get("timeoutSeconds", 60.0)
        if (
            not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
            or not 0 < float(timeout) <= 300
        ):
            raise RecordingModelError(
                "type_error", "verifier timeoutSeconds must be in (0, 300]",
                path=f"{path}.timeoutSeconds",
            )
        return
    data = _strict(value, {"type", "expected", "timeoutSeconds", "durationSeconds", "match", "source"}, path)
    _require(data, {"type", "expected"}, path)
    kind = data["type"]
    if kind not in {
        "text_equals", "text_contains", "value_equals", "visible", "checked",
        "enabled", "window_open", "text_contains_time",
        "window_text_contains", "window_text_contains_time",
        "text_time_range", "window_text_time_range",
    }:
        raise RecordingModelError(
            "golden_verifier_unsupported", f"unsupported verifier {kind!r}",
            path=f"{path}.type",
        )
    verifier_source = data.get("source")
    if verifier_source is not None and verifier_source not in {
            "user_assertion", "native_transition"}:
        raise RecordingModelError(
            "type_error",
            f"verifier source {verifier_source!r} must be 'user_assertion' "
            "or 'native_transition'",
            path=f"{path}.source",
        )
    expected = data["expected"]
    control_kinds = {
        "text_equals", "text_contains", "value_equals", "visible", "checked",
        "enabled", "text_contains_time", "text_time_range",
    }
    if "match" in data:
        if kind not in control_kinds:
            raise RecordingModelError(
                "type_error", "match is only valid for control assertions",
                path=f"{path}.match",
            )
        match = _strict(
            data["match"],
            {"automation_id", "identifier", "control_type", "text"},
            f"{path}.match",
        )
        if (not all(isinstance(item, str) and item for item in match.values())
                or not (match.get("automation_id") or match.get("identifier")
                        or (match.get("control_type") and match.get("text")))):
            raise RecordingModelError(
                "type_error", "match requires a stable control identity",
                path=f"{path}.match",
            )
    if kind in {"text_time_range", "window_text_time_range"}:
        from target.time_assertions import parse_time_range
        try:
            parse_time_range(expected, data.get("durationSeconds"))
        except ValueError as exc:
            raise RecordingModelError("type_error", str(exc), path=f"{path}.expected") from exc
    elif "durationSeconds" in data:
        raise RecordingModelError("type_error", "durationSeconds is only valid for time ranges", path=f"{path}.durationSeconds")
    if kind in {"text_contains_time", "window_text_contains_time"} and (
        not isinstance(expected, str) or not expected
    ):
        raise RecordingModelError(
            "type_error",
            f"{kind} expected must be a non-empty strftime pattern",
            path=f"{path}.expected",
        )
    if kind in {
        "text_equals", "text_contains", "value_equals", "window_text_contains",
    } and not isinstance(expected, str):
        raise RecordingModelError("type_error", f"{kind} expected must be a string", path=f"{path}.expected")
    if kind in {"text_contains", "window_text_contains"} and not expected:
        raise RecordingModelError(
            "type_error", "contains assertion requires non-empty expected text",
            path=f"{path}.expected",
        )
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
        allowed_window = {"processName", "titleRegex", "bundleId", "rootAutomationId"}
        allowed_control = {
            "automationId", "identifier", "controlType", "name", "ancestry",
            "anchor", "fingerprint", "structural",
        }
        allowed_visual = {
            "template", "contextTemplate", "elementSha256", "contextSha256",
            "relativePoint", "contextOffset", "redacted",
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
        if "structural" in self.control:
            from agent.locator.menu_layout import valid_contract
            if (self.control.get("controlType") != "MenuItem" or
                    self.control.get("automationId") or
                    self.control.get("identifier") or not ancestry or
                    not valid_contract(self.control["structural"])):
                raise RecordingModelError(
                    "type_error", "invalid bounded menu structural selector",
                    path="selector.control.structural")
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
                if not isinstance(self.visual["contextTemplate"], str) or not self.visual["contextTemplate"]:
                    raise RecordingModelError(
                        "visual_template_missing", "context template path is invalid",
                        path="selector.visual.contextTemplate",
                    )
                context_digest = self.visual.get("contextSha256")
                if not isinstance(context_digest, str) or not re.fullmatch(
                    r"sha256:[0-9a-f]{64}", context_digest
                ):
                    raise RecordingModelError(
                        "visual_template_digest_invalid",
                        "context template requires a SHA-256 digest",
                        path="selector.visual.contextSha256",
                    )
            offset = self.visual.get("contextOffset")
            if offset is not None and (
                not self.visual.get("contextTemplate")
                or not isinstance(offset, (list, tuple)) or len(offset) != 2
                or any(not isinstance(value, int) or isinstance(value, bool)
                       or value < 0 for value in offset)
            ):
                raise RecordingModelError(
                    "visual_context_offset_invalid",
                    "contextOffset requires a context template and two non-negative integers",
                    path="selector.visual.contextOffset",
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
class EntryWindowIdentity:
    """The compiled entry-window identity record (v2 golden traces).

    One optional record, derived by the compiler from the first executable
    step's selector and its reliably-associated recorded evidence. Absent
    means "no verifiable identity" — replay then keeps the v1 behavior and
    entry-window recovery is refused with an explicit error, never downgraded
    to a weaker title-only match (design doc 08 §C2/§C6, R2 scope).
    """
    process_name: str
    title_regex: str | None
    root_automation_id: str | None
    origin: str

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "schema": ENTRY_WINDOW_SCHEMA,
            "processName": self.process_name,
            "origin": self.origin,
        }
        if self.title_regex is not None:
            data["titleRegex"] = self.title_regex
        if self.root_automation_id is not None:
            data["rootAutomationId"] = self.root_automation_id
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EntryWindowIdentity":
        d = _strict(data, {"schema", "processName", "titleRegex",
                           "rootAutomationId", "origin"}, "golden.entryWindow")
        if d.get("schema") != ENTRY_WINDOW_SCHEMA:
            raise RecordingModelError(
                "unsupported_schema",
                f"unsupported entryWindow schema {d.get('schema')!r}",
                path="golden.entryWindow.schema",
            )
        process = d.get("processName")
        if not isinstance(process, str) or not process:
            raise RecordingModelError(
                "type_error", "entryWindow.processName must be a non-empty string",
                path="golden.entryWindow.processName",
            )
        origin = d.get("origin")
        if origin not in {"already_open", "opened"}:
            raise RecordingModelError(
                "type_error",
                "entryWindow.origin must be 'already_open' or 'opened'",
                path="golden.entryWindow.origin",
            )
        title = d.get("titleRegex")
        if title is not None and (not isinstance(title, str) or not title):
            raise RecordingModelError(
                "type_error", "entryWindow.titleRegex must be a non-empty string or absent",
                path="golden.entryWindow.titleRegex",
            )
        root = d.get("rootAutomationId")
        if root is not None and (not isinstance(root, str) or not root):
            raise RecordingModelError(
                "type_error", "entryWindow.rootAutomationId must be a non-empty string or absent",
                path="golden.entryWindow.rootAutomationId",
            )
        if title is None and root is None:
            raise RecordingModelError(
                "entry_identity_unidentifiable",
                "entryWindow needs a title regex or a root automation id; "
                "an identity with neither is not verifiable",
                path="golden.entryWindow",
            )
        return cls(str(process), title, root, str(origin))



@dataclass(frozen=True)
class WindowSequence:
    """The strict v3 window-ownership sequence (design doc 09 §5)."""
    schema: str
    windows: tuple[Mapping[str, Any], ...]
    steps: tuple[Mapping[str, Any], ...]
    cleanup: tuple[Mapping[str, Any], ...] = ()

    ALLOWED_EFFECT_KINDS = {"appeared", "absent"}
    ALLOWED_EFFECT_VERIFICATION = {"explicit_assertion"}
    ALLOWED_ORIGINS = {"already_open", "observed_during_flow"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "windows": [dict(w) for w in self.windows],
            "steps": [dict(s) for s in self.steps],
            "cleanup": [dict(s) for s in self.cleanup],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "WindowSequence":
        d = _strict(data, {"schema", "windows", "steps", "cleanup"},
                    "golden.windowSequence")
        if d.get("schema") != WINDOW_SEQUENCE_SCHEMA:
            raise RecordingModelError(
                "unsupported_schema",
                f"unsupported windowSequence schema {d.get('schema')!r}",
                path="golden.windowSequence.schema",
            )
        windows = d.get("windows")
        steps = d.get("steps")
        cleanup = d.get("cleanup", [])
        for name, value in (("windows", windows), ("steps", steps),
                            ("cleanup", cleanup)):
            if not isinstance(value, (list, tuple)):
                raise RecordingModelError(
                    "type_error", f"windowSequence.{name} must be an array",
                    path=f"golden.windowSequence.{name}",
                )
        return cls(WINDOW_SEQUENCE_SCHEMA,
                   tuple(dict(w) for w in windows),
                   tuple(dict(s) for s in steps),
                   tuple(dict(s) for s in cleanup))


def _validate_window_sequence(sequence: "WindowSequence",
                              steps: tuple["RecordedStep", ...],
                              cleanup: tuple["RecordedStep", ...],
                              entry_window: "EntryWindowIdentity | None") -> None:
    """Strict validation for the v3 windowSequence (design doc 09 §5.1)."""
    known_steps = {s.step_id for s in (*steps, *cleanup)}
    window_ids = set()
    for index, window in enumerate(sequence.windows):
        path = f"golden.windowSequence.windows[{index}]"
        d = _strict(window, {"id", "identity", "origin", "ownerWindowId"},
                    path)
        wid = d.get("id")
        if not isinstance(wid, str) or not wid:
            raise RecordingModelError(
                "type_error", f"{path}.id must be a non-empty string",
                path=f"{path}.id")
        if wid in window_ids:
            raise RecordingModelError(
                "window_sequence_invalid", f"duplicate window id {wid!r}",
                path=f"{path}.id")
        window_ids.add(wid)
        # Design 11 D2: the declared owner relation is evidence between two
        # INDEPENDENT window entries — it must reference a declared window,
        # never replace one. Self-reference and dangling ids are invalid.
        owner_wid = d.get("ownerWindowId")
        if owner_wid is not None:
            if not isinstance(owner_wid, str) or not owner_wid:
                raise RecordingModelError(
                    "type_error",
                    f"{path}.ownerWindowId must be a non-empty string",
                    path=f"{path}.ownerWindowId")
            if owner_wid == wid:
                raise RecordingModelError(
                    "window_sequence_invalid",
                    f"{path}.ownerWindowId must not reference itself",
                    path=f"{path}.ownerWindowId")
        if d.get("origin") not in WindowSequence.ALLOWED_ORIGINS:
            raise RecordingModelError(
                "window_sequence_invalid",
                f"{path}.origin {d.get('origin')!r} is not declared",
                path=f"{path}.origin",
            )
        identity = d.get("identity")
        idata = _strict(identity if isinstance(identity, Mapping) else {},
                        {"processName", "titleRegex", "rootAutomationId"},
                        f"{path}.identity")
        process = idata.get("processName")
        if not isinstance(process, str) or not process:
            raise RecordingModelError(
                "type_error",
                f"{path}.identity.processName must be a non-empty string",
                path=f"{path}.identity.processName",
            )
        for key in ("titleRegex", "rootAutomationId"):
            value = idata.get(key)
            if value is not None and not isinstance(value, str):
                raise RecordingModelError(
                    "type_error", f"{path}.identity.{key} must be a string",
                    path=f"{path}.identity.{key}",
                )
            if isinstance(value, str) and key == "titleRegex":
                import re as _re
                try:
                    _re.compile(value)
                except _re.error as exc:
                    raise RecordingModelError(
                        "type_error",
                        f"{path}.identity.titleRegex is not a valid regex: {exc}",
                        path=f"{path}.identity.titleRegex",
                    ) from exc
        if not idata.get("titleRegex") and not idata.get("rootAutomationId"):
            raise RecordingModelError(
                "window_sequence_invalid",
                f"{path}.identity needs a title regex or a root automation id",
                path=f"{path}.identity",
            )
    if not window_ids:
        raise RecordingModelError(
            "window_sequence_invalid",
            "windowSequence must declare at least one window",
            path="golden.windowSequence.windows",
        )
    for index, window in enumerate(sequence.windows):
        owner_wid = window.get("ownerWindowId")
        if owner_wid is not None and owner_wid not in window_ids:
            raise RecordingModelError(
                "window_sequence_invalid",
                f"golden.windowSequence.windows[{index}].ownerWindowId "
                f"{owner_wid!r} is not a declared window",
                path=f"golden.windowSequence.windows[{index}].ownerWindowId",
            )
    # Every executable node exactly one ownership entry; step refs valid.
    owner_of = {}
    main_step_ids = {s.step_id for s in steps}
    cleanup_step_ids = {s.step_id for s in cleanup}
    effect_ids_global: set[str] = set()
    for section_name, section in (("steps", sequence.steps),
                                  ("cleanup", sequence.cleanup)):
        for index, entry in enumerate(section):
            path = f"golden.windowSequence.{section_name}[{index}]"
            d = _strict(entry, {"stepId", "windowId", "effects"}, path)
            step_id = d.get("stepId")
            if step_id not in known_steps:
                raise RecordingModelError(
                    "window_sequence_invalid",
                    f"{path}.stepId {step_id!r} is not an executable step",
                    path=f"{path}.stepId",
                )
            if step_id in owner_of:
                raise RecordingModelError(
                    "window_sequence_invalid",
                    f"duplicate ownership entry for {step_id!r}",
                    path=f"{path}.stepId",
                )
            # Partition integrity (review 0907 P2): a MAIN step's ownership
            # must be in steps, a CLEANUP step's in cleanup.
            if (section_name == "steps" and step_id in cleanup_step_ids) or (
                    section_name == "cleanup"
                    and step_id in main_step_ids):
                raise RecordingModelError(
                    "window_sequence_invalid",
                    f"{step_id!r} ownership appears in the wrong section "
                    f"({section_name})",
                    path=f"{path}.stepId",
                )
            wid = d.get("windowId")
            if wid not in window_ids:
                raise RecordingModelError(
                    "window_sequence_invalid",
                    f"{path}.windowId {wid!r} is not a declared window",
                    path=f"{path}.windowId",
                )
            owner_of[step_id] = wid
            effects = d.get("effects") or ()
            if not isinstance(effects, (list, tuple)):
                raise RecordingModelError(
                    "type_error", f"{path}.effects must be an array",
                    path=f"{path}.effects",
                )
            for e_index, effect in enumerate(effects):
                e_path = f"{path}.effects[{e_index}]"
                ed = _strict(effect, {"id", "kind", "windowId",
                                      "verification", "timeoutSeconds"},
                             e_path)
                eid = ed.get("id")
                if not isinstance(eid, str) or not eid or eid in effect_ids_global:
                    raise RecordingModelError(
                        "window_sequence_invalid",
                        f"{e_path}.id must be a unique non-empty string "
                        "(global across steps and cleanup)",
                        path=f"{e_path}.id",
                    )
                effect_ids_global.add(eid)
                if ed.get("kind") not in WindowSequence.ALLOWED_EFFECT_KINDS:
                    raise RecordingModelError(
                        "window_sequence_invalid",
                        f"{e_path}.kind {ed.get('kind')!r} is not declared",
                        path=f"{e_path}.kind",
                    )
                if ed.get("windowId") not in window_ids:
                    raise RecordingModelError(
                        "window_sequence_invalid",
                        f"{e_path}.windowId is not a declared window",
                        path=f"{e_path}.windowId",
                    )
                if ed.get("verification") not in \
                        WindowSequence.ALLOWED_EFFECT_VERIFICATION:
                    raise RecordingModelError(
                        "window_sequence_invalid",
                        f"{e_path}.verification must be explicit_assertion "
                        "(temporal associations never produce strong effects)",
                        path=f"{e_path}.verification",
                    )
                timeout = ed.get("timeoutSeconds")
                if timeout is not None and (
                        not isinstance(timeout, (int, float))
                        or isinstance(timeout, bool)
                        or not 0 < timeout <= 300):
                    raise RecordingModelError(
                        "type_error",
                        f"{e_path}.timeoutSeconds must be in (0, 300]",
                        path=f"{e_path}.timeoutSeconds",
                    )
    missing = known_steps - set(owner_of)
    if missing:
        raise RecordingModelError(
            "window_sequence_invalid",
            f"executable steps without window ownership: {sorted(missing)}",
            path="golden.windowSequence.steps",
        )
    # selector↔identity consistency (review 0747 P2): each executable
    # step's selector window must agree with the identity of the W it is
    # owned by; a mismatch means the sequence does not describe the flow.
    identity_by_id = {
        w.get("id"): (w.get("identity") or {})
        for w in sequence.windows
    }
    for step in (*steps, *cleanup):
        wid = owner_of.get(step.step_id)
        identity = identity_by_id.get(wid) or {}
        if step.selector is None:
            continue
        selector_window = step.selector.window
        process = str(selector_window.get("processName") or "")
        if process and process != identity.get("processName"):
            raise RecordingModelError(
                "window_sequence_invalid",
                f"{step.step_id}: selector processName {process!r} disagrees "
                f"with owned window identity {identity.get('processName')!r}",
                path=f"golden.windowSequence.steps",
            )
        root = selector_window.get("rootAutomationId")
        identity_root = identity.get("rootAutomationId")
        if root and identity_root and root != identity_root:
            raise RecordingModelError(
                "window_sequence_invalid",
                f"{step.step_id}: selector rootAutomationId disagrees with "
                "the owned window identity",
                path="golden.windowSequence.steps",
            )
        if root and identity_root is None and identity.get("titleRegex"):
            # The window declared itself by title only, but the step's
            # selector names a root: the identity is INCOMPLETE for this
            # step -- a missing field must not silently pass (review 0907).
            raise RecordingModelError(
                "window_sequence_invalid",
                f"{step.step_id}: owned window identity lacks the "
                "rootAutomationId the step's selector asserts",
                path="golden.windowSequence.steps",
            )
        title = selector_window.get("titleRegex")
        identity_title = identity.get("titleRegex")
        if title and identity_title and title != identity_title:
            raise RecordingModelError(
                "window_sequence_invalid",
                f"{step.step_id}: selector titleRegex disagrees with the "
                "owned window identity",
                path="golden.windowSequence.steps",
            )
        if title and identity_title is None and identity.get("rootAutomationId"):
            raise RecordingModelError(
                "window_sequence_invalid",
                f"{step.step_id}: owned window identity lacks the "
                "titleRegex the step's selector asserts",
                path="golden.windowSequence.steps",
            )
    # entryWindow must STRICTLY match the ownership of the first selector
    # step (review 0907): every field the entry declares must agree with the
    # identity, and a matching window must exist -- None==None laziness or a
    # wrong entry silently matching nothing are both refusals.
    if entry_window is not None:
        first_selector = next(
            (s for s in steps if s.selector is not None), None)
        entry_fields = {
            "processName": entry_window.process_name,
            "titleRegex": entry_window.title_regex,
            "rootAutomationId": entry_window.root_automation_id,
        }
        entry_matches = []
        for window in sequence.windows:
            identity = window.get("identity") or {}
            ok = True
            for key, value in entry_fields.items():
                if value is None:
                    continue
                if identity.get(key) != value:
                    ok = False
                    break
            if ok:
                entry_matches.append(window.get("id"))
        if first_selector is not None:
            owner = owner_of.get(first_selector.step_id)
            if owner not in entry_matches:
                raise RecordingModelError(
                    "window_sequence_invalid",
                    "entryWindow identity does not strictly match the first "
                    "selector step's ownership window",
                    path="golden.windowSequence",
                )
        elif not entry_matches:
            raise RecordingModelError(
                "window_sequence_invalid",
                "entryWindow matches no declared window identity",
                path="golden.windowSequence",
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
    #: v2 only. None (absent) keeps v1 semantics: no verifiable entry identity,
    #: entry-window recovery refused with an explicit error when requested.
    entry_window: EntryWindowIdentity | None = None
    #: v3 only. The strict window-ownership sequence (design doc 09 §5).
    #: A v3 golden is NOT executable until the S3/S4 binding/effects slices
    #: land; replay must refuse before any target side effect.
    window_sequence: WindowSequence | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise RecordingModelError("type_error", "golden name must be non-empty", path="golden.name")
        if self.status not in {"ready", "incomplete"}:
            raise RecordingModelError("type_error", "invalid golden status", path="golden.status")
        if self.entry_window is not None and self.schema not in {
                GOLDEN_TRACE_SCHEMA_V2, GOLDEN_TRACE_SCHEMA_V3}:
            raise RecordingModelError(
                "type_error",
                "entryWindow requires the v2 or v3 golden schema",
                path="golden.schema",
            )
        if self.window_sequence is not None and self.schema != GOLDEN_TRACE_SCHEMA_V3:
            raise RecordingModelError(
                "type_error",
                "windowSequence requires the v3 golden schema",
                path="golden.schema",
            )
        if self.schema == GOLDEN_TRACE_SCHEMA_V3:
            if self.window_sequence is None:
                raise RecordingModelError(
                    "window_sequence_invalid",
                    "a v3 golden trace requires windowSequence "
                    "(no silent fallback to v2 semantics)",
                    path="golden.windowSequence",
                )
            _validate_window_sequence(self.window_sequence, self.steps,
                                      self.cleanup, self.entry_window)
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
        data = {
            "schema": self.schema, "status": self.status, "name": self.name,
            "sourceRecording": dict(self.source_recording), "catalog": dict(self.catalog),
            "environment": dict(self.environment),
            "entry": self.steps[0].step_id if self.steps else None,
            "steps": step_map,
            "cleanup": [step.to_dict() for step in self.cleanup],
        }
        if self.entry_window is not None:
            data["entryWindow"] = self.entry_window.to_dict()
        if self.window_sequence is not None:
            data["windowSequence"] = self.window_sequence.to_dict()
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "GoldenTrace":
        d = _strict(data, {"schema", "status", "name", "sourceRecording", "catalog", "environment", "entry", "steps", "cleanup", "entryWindow", "windowSequence"}, "golden")
        _require(
            d,
            {"schema", "status", "name", "sourceRecording", "catalog", "environment", "entry", "steps", "cleanup"},
            "golden",
        )
        if d.get("schema") not in {GOLDEN_TRACE_SCHEMA, GOLDEN_TRACE_SCHEMA_V2,
                                   GOLDEN_TRACE_SCHEMA_V3}:
            raise RecordingModelError("unsupported_schema", f"unsupported schema {d.get('schema')!r}", path="golden.schema")
        entry_window = None
        if d.get("entryWindow") is not None:
            if d.get("schema") not in {GOLDEN_TRACE_SCHEMA_V2,
                                       GOLDEN_TRACE_SCHEMA_V3}:
                raise RecordingModelError(
                    "type_error",
                    "entryWindow requires the v2 or v3 golden schema",
                    path="golden.schema",
                )
            entry_window = EntryWindowIdentity.from_dict(d["entryWindow"])
        window_sequence = None
        if d.get("windowSequence") is not None:
            if d.get("schema") != GOLDEN_TRACE_SCHEMA_V3:
                raise RecordingModelError(
                    "type_error",
                    "windowSequence requires the v3 golden schema",
                    path="golden.schema",
                )
            window_sequence = WindowSequence.from_dict(d["windowSequence"])
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
            entry_window=entry_window,
            window_sequence=window_sequence,
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
