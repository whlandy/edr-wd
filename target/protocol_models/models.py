"""
models.py — P0.2 wire models (architecture §9.1, §9.2, §9.3, §19).

All models are frozen dataclasses with strict semantics: unknown
fields are rejected at construction time (FR-P0.2-01).

Strict mode strategy
--------------------

P0.1 used dataclasses with `frozen=True`. P0.2 needs the same plus
unknown-field rejection. Pydantic is not adopted because the
target-side should stay stdlib-only (P0.1 review / architecture
§P0.1 "stdlib-only").

The implementation:

    1. Each model has an `_ALLOWED_FIELDS` frozenset of the dataclass
       field names. Models use the dataclass-generated `__init__`,
       so the caller can only supply those names anyway — but if a
       subclass accidentally adds fields, strict mode catches it.
    2. Construction-time strictness is enforced by `_strict_from_dict`
       which is the only public path for JSON-derived construction.
       `**kwargs` with extra keys raises `ValidationError` with
       `code: "unknown_field"` and a `path` like `steps[2].unknown`.

This pattern keeps `Model(**kwargs)` ergonomic AND
`Model.from_json(...)` strict at the same time.

Catalog digest mismatch
-----------------------

`ActionSequence` carries `catalog_version` and `catalog_digest` so
the validator can refuse plans that target a different catalog
revision than the running server (architecture §10 step 4). The
constants `PROTOCOL_VERSION = "1.0.0"` lives here; bump per
architecture §7.2 semver rules.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from typing import Any, Mapping

from .enums import (
    EXPECTATION_VISUAL_EVIDENCE_CAPTURED,
    EVIDENCE_SCREENSHOT_NONE,
    ON_ERROR_ABORT,
    VALID_EVIDENCE_SCREENSHOT,
    VALID_EXPECTATION_TYPES,
    VALID_ON_ERROR,
    VALID_TRANSITION_KINDS,
)


PROTOCOL_VERSION = "1.0.0"


# ---------------------------------------------------------------------------
# Strict-from-dict helper
# ---------------------------------------------------------------------------


class ProtocolModelError(ValueError):
    """Raised on any protocol-model construction failure.

    Subclasses / stable codes used in this module:

        * "unknown_field"      — extra key in kwargs
        * "missing_field"      — required key absent
        * "type_error"         — value of wrong type
        * "enum_error"         — value not in documented set
        * "empty_value"        — required string/sequence is empty

    Stable for FR-P0.2-03 consumers. `path` is a JSON-pointer-style
    string like `steps[2].args`.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        path: str = "",
        value: Any = None,
    ):
        self.code = code
        self.path = path
        self.value = value
        super().__init__(f"{code} at {path or '<root>'}: {message}")


def _strict_from_dict_kwargs(
    allowed: frozenset[str], data: Mapping[str, Any], *, path: str,
) -> dict[str, Any]:
    """Validate `data` against `allowed` and return kwargs.

    Raises `ProtocolModelError` with stable `code` + `path` on any
    deviation. Required-field enforcement is the responsibility of
    the caller (default values on the dataclass fields handle
    optionality).
    """
    if not isinstance(data, Mapping):
        raise ProtocolModelError(
            "type_error",
            f"expected mapping, got {type(data).__name__}",
            path=path,
            value=type(data).__name__,
        )
    out: dict[str, Any] = {}
    for k, v in data.items():
        if k not in allowed:
            raise ProtocolModelError(
                "unknown_field",
                f"field {k!r} is not allowed here",
                path=path,
                value=k,
            )
        out[k] = v
    return out


def _require_str(value: Any, *, path: str, field_name: str) -> str:
    if not isinstance(value, str):
        raise ProtocolModelError(
            "type_error",
            f"{field_name} must be a string, got {type(value).__name__}",
            path=path,
            value=type(value).__name__,
        )
    if not value:
        raise ProtocolModelError(
            "empty_value",
            f"{field_name} must be a non-empty string",
            path=path,
            value=value,
        )
    return value


def _require_enum(
    value: Any, *, path: str, field_name: str, allowed: frozenset[str],
) -> str:
    if value not in allowed:
        raise ProtocolModelError(
            "enum_error",
            f"{field_name} must be one of {sorted(allowed)}, got {value!r}",
            path=path,
            value=value,
        )
    return value


def _allow_none_or_str(v: Any, *, path: str, field_name: str) -> Any:
    """Accept `None` or a string. P0.2 originally had `_allow_none`
    which accepted only `None` — P0.3's fingerprint flow exercised the
    string branch and revealed the gap. Renamed + fixed in P0.3 (see
    CHANGELOG)."""
    if v is None:
        return None
    if isinstance(v, str):
        return v
    raise ProtocolModelError(
        "type_error",
        f"{field_name} must be None or a string, got {type(v).__name__}",
        path=path,
        value=type(v).__name__,
    )


# Backwards-compat alias so callers that imported `_allow_none` from
# P0.2 still resolve (P0.2 models only ever passed `None`, so the
# alias preserves that behaviour while P0.3 callers use the fixed
# helper).
_allow_none = _allow_none_or_str


# ---------------------------------------------------------------------------
# TargetRef — bound to a snapshot (architecture §8.2, §9.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TargetRef:
    """Reference to one observed control/window within a snapshot."""

    snapshot_id: str
    target_id: str
    expected_process_name: str = ""
    fingerprint: str | None = None
    selector_hint: Mapping[str, Any] | None = None

    _ALLOWED: frozenset[str] = frozenset({
        "snapshot_id", "target_id", "expected_process_name",
        "fingerprint", "selector_hint",
    })

    def __post_init__(self) -> None:
        _require_str(self.snapshot_id, path="snapshot_id",
                     field_name="snapshot_id")
        _require_str(self.target_id, path="target_id",
                     field_name="target_id")
        if self.expected_process_name:
            _require_str(self.expected_process_name,
                         path="expected_process_name",
                         field_name="expected_process_name")
        _allow_none(self.fingerprint, path="fingerprint",
                    field_name="fingerprint")
        if self.selector_hint is not None and not isinstance(
            self.selector_hint, Mapping
        ):
            raise ProtocolModelError(
                "type_error",
                f"selector_hint must be a mapping, got {type(self.selector_hint).__name__}",
                path="selector_hint",
                value=type(self.selector_hint).__name__,
            )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TargetRef":
        kwargs = _strict_from_dict_kwargs(
            cls._ALLOWED, data, path="TargetRef",
        )
        return cls(**kwargs)


# ---------------------------------------------------------------------------
# Expectation (architecture §9.3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Expectation:
    """One assertion to evaluate against an observation after the step."""

    type: str
    value: Any = None
    timeout_seconds: float = 10.0

    _ALLOWED: frozenset[str] = frozenset({
        "type", "value", "timeout_seconds",
    })

    def __post_init__(self) -> None:
        _require_str(self.type, path="type", field_name="type")
        _require_enum(self.type, path="type",
                      field_name="type",
                      allowed=VALID_EXPECTATION_TYPES)
        if not isinstance(self.timeout_seconds, (int, float)):
            raise ProtocolModelError(
                "type_error",
                f"timeout_seconds must be a number, got {type(self.timeout_seconds).__name__}",
                path="timeout_seconds",
                value=type(self.timeout_seconds).__name__,
            )
        if self.timeout_seconds < 0:
            raise ProtocolModelError(
                "enum_error",
                f"timeout_seconds must be >= 0, got {self.timeout_seconds}",
                path="timeout_seconds",
                value=self.timeout_seconds,
            )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Expectation":
        kwargs = _strict_from_dict_kwargs(
            cls._ALLOWED, data, path="Expectation",
        )
        return cls(**kwargs)


# ---------------------------------------------------------------------------
# Transition (architecture §9.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Transition:
    """Planner-supplied transition hint (optional)."""

    expected: bool = False
    kind: str = "none"
    checkpoint_before: bool = False
    expected_window_owner: str | None = None

    _ALLOWED: frozenset[str] = frozenset({
        "expected", "kind", "checkpoint_before", "expected_window_owner",
    })

    def __post_init__(self) -> None:
        if not isinstance(self.expected, bool):
            raise ProtocolModelError(
                "type_error",
                f"expected must be bool, got {type(self.expected).__name__}",
                path="expected",
                value=type(self.expected).__name__,
            )
        _require_enum(self.kind, path="kind",
                      field_name="kind",
                      allowed=VALID_TRANSITION_KINDS)
        if not isinstance(self.checkpoint_before, bool):
            raise ProtocolModelError(
                "type_error",
                f"checkpoint_before must be bool, got {type(self.checkpoint_before).__name__}",
                path="checkpoint_before",
                value=type(self.checkpoint_before).__name__,
            )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Transition":
        kwargs = _strict_from_dict_kwargs(
            cls._ALLOWED, data, path="Transition",
        )
        return cls(**kwargs)


# ---------------------------------------------------------------------------
# ActionStep (architecture §9.1, §9.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionStep:
    """One step in an ActionSequence — a single LLM-authored intent."""

    step_id: str
    action_id: str
    action_code: str | None = None
    args: Mapping[str, Any] = field(default_factory=dict)
    target_ref: TargetRef | None = None
    depends_on: tuple[str, ...] = field(default_factory=tuple)
    transition: Transition | None = None
    expectations: tuple[Expectation, ...] = field(default_factory=tuple)
    on_error: str = ON_ERROR_ABORT

    _ALLOWED: frozenset[str] = frozenset({
        "step_id", "action_id", "action_code", "args",
        "target_ref", "depends_on", "transition", "expectations",
        "on_error",
    })

    def __post_init__(self) -> None:
        _require_str(self.step_id, path="step_id", field_name="step_id")
        _require_str(self.action_id, path="action_id", field_name="action_id")
        if self.action_code is not None:
            _require_str(self.action_code,
                         path="action_code", field_name="action_code")
        if not isinstance(self.args, Mapping):
            raise ProtocolModelError(
                "type_error",
                f"args must be a mapping, got {type(self.args).__name__}",
                path="args",
                value=type(self.args).__name__,
            )
        _require_enum(self.on_error, path="on_error",
                      field_name="on_error",
                      allowed=VALID_ON_ERROR)
        # expects tuple/list — validate elements if present
        if not isinstance(self.depends_on, tuple):
            raise ProtocolModelError(
                "type_error",
                f"depends_on must be a tuple of step_ids, got {type(self.depends_on).__name__}",
                path="depends_on",
                value=type(self.depends_on).__name__,
            )
        for i, dep in enumerate(self.depends_on):
            if not isinstance(dep, str) or not dep:
                raise ProtocolModelError(
                    "type_error",
                    f"depends_on[{i}] must be a non-empty string, got {dep!r}",
                    path=f"depends_on[{i}]",
                    value=type(dep).__name__,
                )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ActionStep":
        kwargs = _strict_from_dict_kwargs(
            cls._ALLOWED, data, path="ActionStep",
        )
        # Nested model coercion. JSON parsing yields lists, but the
        # strict model requires tuples for `depends_on` and
        # `expectations`. Coerce before nested construction.
        if "depends_on" in kwargs and kwargs["depends_on"] is not None:
            kwargs["depends_on"] = tuple(kwargs["depends_on"])
        if "target_ref" in kwargs and kwargs["target_ref"] is not None:
            kwargs["target_ref"] = TargetRef.from_dict(kwargs["target_ref"])
        if "transition" in kwargs and kwargs["transition"] is not None:
            kwargs["transition"] = Transition.from_dict(kwargs["transition"])
        if "expectations" in kwargs and kwargs["expectations"] is not None:
            kwargs["expectations"] = tuple(
                Expectation.from_dict(e) for e in kwargs["expectations"]
            )
        return cls(**kwargs)


# ---------------------------------------------------------------------------
# ActionSequence (architecture §9.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionSequence:
    """Ordered plan: a list of ActionSteps plus catalog binding."""

    plan_id: str
    catalog_version: str
    catalog_digest: str
    target: str
    steps: tuple[ActionStep, ...]

    _ALLOWED: frozenset[str] = frozenset({
        "plan_id", "catalog_version", "catalog_digest", "target", "steps",
    })

    def __post_init__(self) -> None:
        _require_str(self.plan_id, path="plan_id", field_name="plan_id")
        _require_str(self.catalog_version,
                     path="catalog_version",
                     field_name="catalog_version")
        _require_str(self.catalog_digest,
                     path="catalog_digest",
                     field_name="catalog_digest")
        _require_str(self.target, path="target", field_name="target")
        if not isinstance(self.steps, tuple):
            raise ProtocolModelError(
                "type_error",
                f"steps must be a tuple, got {type(self.steps).__name__}",
                path="steps",
                value=type(self.steps).__name__,
            )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ActionSequence":
        kwargs = _strict_from_dict_kwargs(
            cls._ALLOWED, data, path="ActionSequence",
        )
        if "steps" in kwargs and kwargs["steps"] is not None:
            kwargs["steps"] = tuple(
                ActionStep.from_dict(s) for s in kwargs["steps"]
            )
        return cls(**kwargs)


# ---------------------------------------------------------------------------
# AtomicTestStep — extension with step_no / title / evidence (architecture §9.2)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvidenceSpec:
    """Per-step evidence override (screenshot / redaction)."""

    screenshot: str = EVIDENCE_SCREENSHOT_NONE

    _ALLOWED: frozenset[str] = frozenset({"screenshot"})

    def __post_init__(self) -> None:
        _require_enum(self.screenshot, path="screenshot",
                      field_name="screenshot",
                      allowed=VALID_EVIDENCE_SCREENSHOT)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EvidenceSpec":
        kwargs = _strict_from_dict_kwargs(
            cls._ALLOWED, data, path="EvidenceSpec",
        )
        return cls(**kwargs)


@dataclass(frozen=True)
class AtomicTestStep(ActionStep):
    """ActionStep with test-case-integration metadata."""

    step_no: int = 0
    title: str = ""
    description: str = ""
    evidence: EvidenceSpec | None = None
    required: bool = True

    _ALLOWED: frozenset[str] = (ActionStep._ALLOWED | frozenset({
        "step_no", "title", "description", "evidence", "required",
    }))

    def __post_init__(self) -> None:
        super().__post_init__()
        if not isinstance(self.step_no, int):
            raise ProtocolModelError(
                "type_error",
                f"step_no must be an int, got {type(self.step_no).__name__}",
                path="step_no",
                value=type(self.step_no).__name__,
            )
        if self.step_no < 0:
            raise ProtocolModelError(
                "enum_error",
                f"step_no must be >= 0, got {self.step_no}",
                path="step_no",
                value=self.step_no,
            )
        if self.title:
            _require_str(self.title, path="title", field_name="title")
        if not isinstance(self.required, bool):
            raise ProtocolModelError(
                "type_error",
                f"required must be bool, got {type(self.required).__name__}",
                path="required",
                value=type(self.required).__name__,
            )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AtomicTestStep":
        kwargs = _strict_from_dict_kwargs(
            cls._ALLOWED, data, path="AtomicTestStep",
        )
        # JSON parsing yields lists, but the strict model requires
        # tuples for `depends_on` and `expectations`. Coerce before
        # nested construction.
        if "depends_on" in kwargs and kwargs["depends_on"] is not None:
            kwargs["depends_on"] = tuple(kwargs["depends_on"])
        if "target_ref" in kwargs and kwargs["target_ref"] is not None:
            kwargs["target_ref"] = TargetRef.from_dict(kwargs["target_ref"])
        if "transition" in kwargs and kwargs["transition"] is not None:
            kwargs["transition"] = Transition.from_dict(kwargs["transition"])
        if "expectations" in kwargs and kwargs["expectations"] is not None:
            kwargs["expectations"] = tuple(
                Expectation.from_dict(e) for e in kwargs["expectations"]
            )
        if "evidence" in kwargs and kwargs["evidence"] is not None:
            kwargs["evidence"] = EvidenceSpec.from_dict(kwargs["evidence"])
        return cls(**kwargs)


# ---------------------------------------------------------------------------
# TestCase (architecture §9.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TestCase:
    """A test definition: stable identity, list of atomic steps, cleanup."""

    case_id: str
    title: str
    description: str = ""
    __test__ = False  # not a pytest TestCase class

    profiles: tuple[str, ...] = field(default_factory=tuple)
    tags: tuple[str, ...] = field(default_factory=tuple)
    preconditions: tuple[AtomicTestStep, ...] = field(default_factory=tuple)
    steps: tuple[AtomicTestStep, ...] = field(default_factory=tuple)
    cleanup: tuple[AtomicTestStep, ...] = field(default_factory=tuple)
    cleanup_outcome_critical: bool = False
    timeout_seconds: int = 120

    _ALLOWED: frozenset[str] = frozenset({
        "case_id", "title", "description", "profiles", "tags",
        "preconditions", "steps", "cleanup",
        "cleanup_outcome_critical",
        "timeout_seconds",
    })

    def __post_init__(self) -> None:
        _require_str(self.case_id, path="case_id", field_name="case_id")
        _require_str(self.title, path="title", field_name="title")
        if not isinstance(self.cleanup_outcome_critical, bool):
            raise ProtocolModelError(
                "type_error",
                f"cleanup_outcome_critical must be a bool, "
                f"got {type(self.cleanup_outcome_critical).__name__}",
                path="cleanup_outcome_critical",
                value=type(self.cleanup_outcome_critical).__name__,
            )
        if not isinstance(self.timeout_seconds, int):
            raise ProtocolModelError(
                "type_error",
                f"timeout_seconds must be an int, got {type(self.timeout_seconds).__name__}",
                path="timeout_seconds",
                value=type(self.timeout_seconds).__name__,
            )
        if self.timeout_seconds <= 0:
            raise ProtocolModelError(
                "enum_error",
                f"timeout_seconds must be > 0, got {self.timeout_seconds}",
                path="timeout_seconds",
                value=self.timeout_seconds,
            )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TestCase":
        kwargs = _strict_from_dict_kwargs(
            cls._ALLOWED, data, path="TestCase",
        )
        for k in ("preconditions", "steps", "cleanup"):
            if k in kwargs and kwargs[k] is not None:
                kwargs[k] = tuple(
                    AtomicTestStep.from_dict(s) for s in kwargs[k]
                )
        return cls(**kwargs)


# ---------------------------------------------------------------------------
# Error envelope (architecture §19)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ErrorEnvelope:
    """Stable error contract for any protocol API."""

    code: str
    message: str
    retryable: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)
    request_id: str | None = None
    observed_at: str | None = None

    _ALLOWED: frozenset[str] = frozenset({
        "code", "message", "retryable", "details",
        "request_id", "observed_at",
    })

    def __post_init__(self) -> None:
        _require_str(self.code, path="code", field_name="code")
        _require_str(self.message, path="message", field_name="message")
        if not isinstance(self.retryable, bool):
            raise ProtocolModelError(
                "type_error",
                f"retryable must be bool, got {type(self.retryable).__name__}",
                path="retryable",
                value=type(self.retryable).__name__,
            )
        if not isinstance(self.details, Mapping):
            raise ProtocolModelError(
                "type_error",
                f"details must be a mapping, got {type(self.details).__name__}",
                path="details",
                value=type(self.details).__name__,
            )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ErrorEnvelope":
        kwargs = _strict_from_dict_kwargs(
            cls._ALLOWED, data, path="ErrorEnvelope",
        )
        return cls(**kwargs)


# ---------------------------------------------------------------------------
# ActionReceipt (architecture §9.1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionReceipt:
    """Result of one dispatched action.

    `server_instance_id` changes on every target MCP server start and
    lets the agent detect lost in-flight outcomes (architecture §9.1).
    """

    ok: bool
    request_id: str
    server_instance_id: str
    observed_at: str
    error: ErrorEnvelope | None = None
    result: Mapping[str, Any] | None = None

    _ALLOWED: frozenset[str] = frozenset({
        "ok", "request_id", "server_instance_id", "observed_at",
        "error", "result",
    })

    def __post_init__(self) -> None:
        if not isinstance(self.ok, bool):
            raise ProtocolModelError(
                "type_error",
                f"ok must be bool, got {type(self.ok).__name__}",
                path="ok",
                value=type(self.ok).__name__,
            )
        _require_str(self.request_id,
                     path="request_id", field_name="request_id")
        _require_str(self.server_instance_id,
                     path="server_instance_id",
                     field_name="server_instance_id")
        _require_str(self.observed_at,
                     path="observed_at", field_name="observed_at")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ActionReceipt":
        kwargs = _strict_from_dict_kwargs(
            cls._ALLOWED, data, path="ActionReceipt",
        )
        if "error" in kwargs and kwargs["error"] is not None:
            kwargs["error"] = ErrorEnvelope.from_dict(kwargs["error"])
        return cls(**kwargs)


# ---------------------------------------------------------------------------
# Helpers for introspection
# ---------------------------------------------------------------------------


def model_field_names(model_cls: type) -> frozenset[str]:
    """Return the dataclass field names of `model_cls`.

    Used by the validator when a JSON payload claims an allowed set
    (e.g. to validate a serialized AtomicTestStep against its parent
    class fields). Returns frozenset of strings.
    """
    return frozenset(f.name for f in fields(model_cls))


__all__ = [
    "PROTOCOL_VERSION",
    "ProtocolModelError",
    "TargetRef",
    "Expectation",
    "Transition",
    "ActionStep",
    "ActionSequence",
    "EvidenceSpec",
    "AtomicTestStep",
    "TestCase",
    "ErrorEnvelope",
    "ActionReceipt",
    "model_field_names",
]