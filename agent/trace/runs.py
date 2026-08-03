"""
runs.py — Run-level lifecycle and manifest (P2.3 architecture §Checkpoint P2.3).

Provides:

  * `RunContext`         — sole lifecycle owner of a run
                            (`artifacts/test-runs/<run_id>/`).
  * `RunState`           — explicit state machine (CREATED → RUNNING → FINALIZED).
  * `CaseAttemptRef`     — pointer to one case attempt on disk.
  * `Manifest`           — run-level `manifest.json` schema (with
                            `schema_version` field, D8 / R1).
  * `sanitize_identifier` — strict regex `[a-zA-Z0-9_-]+` (D4).
  * `MetricRecord`       — factory-validated metric (whitelist, D5).
  * `make_attempt_id`    — `attempt-NNNN` formatter (D3).

Boundaries (P2.3 §3.1):
  * RunContext is the sole mutator of the run directory. Observer
    modules (trace/store.py, manifest.py, trace_md.py) only emit
    events into RunContext.
  * State transitions go through `InvalidStateTransitionError` (D9).
  * Manifests are versioned; consumers reject unknown schema versions
    with `UnsupportedManifestSchemaError` (D8).
  * Metrics cannot leak screen_text / args / creds / env via the
    whitelist factory (D5).

Predecessor: P2.2 (origin/hermes-remote @ 4295987).
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, FrozenSet, Mapping, Sequence


# ----------------------------------------------------------------------
# Manifest schema version (D8 / R1 Issue 1)
# ----------------------------------------------------------------------

MANIFEST_SCHEMA_VERSION: str = "1.0"
"""Current run-level manifest schema version. Only this value (and any
future values added to `SUPPORTED_MANIFEST_SCHEMA_VERSIONS`) is
accepted by consumers."""

SUPPORTED_MANIFEST_SCHEMA_VERSIONS: FrozenSet[str] = frozenset({"1.0"})
"""Set of manifest schema versions consumers will accept. A manifest
with a `schema_version` outside this set raises
`UnsupportedManifestSchemaError`."""


class UnsupportedManifestSchemaError(ValueError):
    """Raised when a manifest's `schema_version` is not in the supported
    set, or is missing entirely."""


# ----------------------------------------------------------------------
# RunState (D9 / R1 Issue 2)
# ----------------------------------------------------------------------

class RunState(str, Enum):
    """Explicit run-level execution lifecycle state.

    CREATED   — RunContext instantiated; no attempts yet.
    RUNNING   — at least one case attempt has been added.
    FINALIZED — `finalize()` was called; the run is immutable.
    """
    CREATED = "created"
    RUNNING = "running"
    FINALIZED = "finalized"


class InvalidStateTransitionError(RuntimeError):
    """Raised when a RunContext mutator is invoked in the wrong state
    (typically: state == FINALIZED)."""


# ----------------------------------------------------------------------
# Filename sanitization (D4)
# ----------------------------------------------------------------------

SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z0-9_-]+$")


def sanitize_identifier(name: str) -> str:
    """Strict filename-safe identifier.

    Accepts only `[a-zA-Z0-9_-]+`. Empty strings and anything outside
    the allowed set raise `ValueError`. Applied to: `run_id`,
    `safe_case_id` (from case.case_id), `attempt_id`, and any
    `trace_id` used as a path component.

    Raises:
        ValueError: if `name` is empty or contains forbidden chars.
    """
    if not isinstance(name, str) or not name:
        raise ValueError(
            f"identifier must be non-empty str, got {name!r}"
        )
    if not SAFE_IDENTIFIER.match(name):
        raise ValueError(
            f"identifier {name!r} contains forbidden characters "
            f"(only [a-zA-Z0-9_-] allowed)"
        )
    return name


# ----------------------------------------------------------------------
# MetricRecord with whitelist factory (D5 / §4.4)
# ----------------------------------------------------------------------

ALLOWED_METRIC_KEYS: FrozenSet[str] = frozenset({
    "duration_ms",
    "step_count",
    "retry_count",
    "replan_count",
    "recovery_count",
    "outcome",
    "screenshot_count",
    "screenshot_bytes",
    "first_attempt_outcome",
    "final_attempt_outcome",
})
"""Allowlist of metric keys; FR-P2.3-08 forbids screen_text, OCR,
command args, credentials, environment dumps, etc."""


class MetricKeyForbiddenError(ValueError):
    """Raised when a metric key is not in ALLOWED_METRIC_KEYS."""


class MetricValueTypeError(TypeError):
    """Raised when a metric value is not int or str."""


@dataclass(frozen=True)
class MetricRecord:
    """Schema-locked metric record. Constructed via `MetricRecord.make()`
    which validates key + value type against the whitelist."""
    key: str
    value: int | str

    @classmethod
    def make(cls, key: str, value: int | str) -> "MetricRecord":
        if key not in ALLOWED_METRIC_KEYS:
            raise MetricKeyForbiddenError(
                f"metric key {key!r} not in whitelist; "
                f"allowed: {sorted(ALLOWED_METRIC_KEYS)}"
            )
        if not isinstance(value, (int, str)) or isinstance(value, bool):
            raise MetricValueTypeError(
                f"metric value must be int or str (no bool), got {type(value).__name__}"
            )
        return cls(key=key, value=value)

    def to_dict(self) -> dict[str, Any]:
        return {"key": self.key, "value": self.value}


# ----------------------------------------------------------------------
# Attempt ID formatter (D3)
# ----------------------------------------------------------------------

def make_attempt_id(attempt_no: int) -> str:
    """Format `attempt-NNNN` (4-digit zero-padded). Counter is monotonic
    per (run_id, safe_case_id). Counter starts at 1.

    Raises:
        ValueError: if attempt_no < 1.
    """
    if not isinstance(attempt_no, int) or attempt_no < 1:
        raise ValueError(
            f"attempt_no must be int >= 1, got {attempt_no!r}"
        )
    return f"attempt-{attempt_no:04d}"


# ----------------------------------------------------------------------
# CaseAttemptRef
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class CaseAttemptRef:
    """Pointer to one case attempt on disk under
    `<run_dir>/cases/<safe_case_id>/<attempt_id>-<trace_id>/`."""
    safe_case_id: str
    attempt_id: str
    trace_id: str
    path: Path

    def trace_md_path(self) -> Path:
        return self.path / "trace.md"

    def step_results_path(self) -> Path:
        return self.path / "step-results.json"

    def case_attempt_manifest_path(self) -> Path:
        return self.path / "case-attempt-manifest.json"

    def to_dict(self) -> dict[str, Any]:
        return {
            "safe_case_id": self.safe_case_id,
            "attempt_id": self.attempt_id,
            "trace_id": self.trace_id,
            "path": str(self.path),
        }


# ----------------------------------------------------------------------
# Manifest
# ----------------------------------------------------------------------

@dataclass(frozen=True)
class Manifest:
    """Run-level `manifest.json` schema (P2.3 §6).

    schema_version is REQUIRED (D8). On construction it defaults to
    MANIFEST_SCHEMA_VERSION. On deserialization (from_dict), the value
    must be in SUPPORTED_MANIFEST_SCHEMA_VERSIONS or
    UnsupportedManifestSchemaError is raised.
    """
    schema_version: str
    run_id: str
    started_at: str
    ended_at: str | None
    requested_targets: tuple[str, ...]
    environment: Mapping[str, str]
    case_attempts: tuple[CaseAttemptRef, ...]
    renderer_version: str
    schema_versions: Mapping[str, str]
    aggregate_status: str
    metrics_summary: tuple[MetricRecord, ...]
    report_status: str
    evidence_status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "renderer_version": self.renderer_version,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "requested_targets": list(self.requested_targets),
            "environment": dict(self.environment),
            "case_attempts": [ca.to_dict() for ca in self.case_attempts],
            "aggregate_status": self.aggregate_status,
            "report_status": self.report_status,
            "evidence_status": self.evidence_status,
            "metrics_summary": [m.to_dict() for m in self.metrics_summary],
            "schema_versions": dict(self.schema_versions),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Manifest":
        """Deserialize. Validates schema_version; raises
        `UnsupportedManifestSchemaError` if missing or not in
        `SUPPORTED_MANIFEST_SCHEMA_VERSIONS`.
        """
        if not isinstance(data, Mapping):
            raise UnsupportedManifestSchemaError(
                f"manifest data must be Mapping, got {type(data).__name__}"
            )
        sv = data.get("schema_version")
        if sv is None:
            raise UnsupportedManifestSchemaError(
                "manifest missing required field 'schema_version'"
            )
        if sv not in SUPPORTED_MANIFEST_SCHEMA_VERSIONS:
            raise UnsupportedManifestSchemaError(
                f"manifest schema_version {sv!r} not supported; "
                f"supported: {sorted(SUPPORTED_MANIFEST_SCHEMA_VERSIONS)}"
            )
        case_attempts = tuple(
            CaseAttemptRef(
                safe_case_id=ca["safe_case_id"],
                attempt_id=ca["attempt_id"],
                trace_id=ca["trace_id"],
                path=Path(ca["path"]),
            )
            for ca in data.get("case_attempts", [])
        )
        metrics = tuple(
            MetricRecord(m["key"], m["value"])
            for m in data.get("metrics_summary", [])
        )
        return cls(
            schema_version=sv,
            run_id=data["run_id"],
            started_at=data["started_at"],
            ended_at=data.get("ended_at"),
            requested_targets=tuple(data.get("requested_targets", [])),
            environment=dict(data.get("environment", {})),
            case_attempts=case_attempts,
            renderer_version=data.get("renderer_version", "p2.3"),
            schema_versions=dict(data.get("schema_versions", {})),
            aggregate_status=data.get("aggregate_status", "unknown"),
            metrics_summary=metrics,
            report_status=data.get("report_status", "invalid"),
            evidence_status=data.get("evidence_status", "degraded"),
        )


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def now_utc_iso() -> str:
    """UTC ISO-8601 timestamp with millisecond precision and 'Z' suffix."""
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def atomic_write_json(path: Path, data: Mapping[str, Any]) -> None:
    """Write `data` as JSON atomically: temp file + os.replace.

    Atomic write guarantees readers never observe a half-written file
    (P2.3 §4.5 / FR-P2.3-04).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


# ----------------------------------------------------------------------
# RunContext — lifecycle owner
# ----------------------------------------------------------------------

class RunContext:
    """Lifecycle owner of a run (P2.3 §3.1, §3.1.1).

    Single source of truth for run-level state. All disk mutations
    under `artifacts/test-runs/<run_id>/` flow through this class.

    State machine (D9):
        CREATED --first add_case_attempt--> RUNNING --finalize--> FINALIZED
    """

    def __init__(self, root: Path, run_id: str) -> None:
        """Idempotent — opens existing run or creates a new one.

        Args:
            root: parent directory under which `<run_id>/` lives.
            run_id: must satisfy `sanitize_identifier`.

        Raises:
            ValueError: if run_id fails sanitize_identifier.
            UnsupportedManifestSchemaError: if an existing
                `manifest.json` has an unknown `schema_version`.
        """
        self._run_id: str = sanitize_identifier(run_id)
        self._root: Path = Path(root)
        self.run_dir: Path = self._root / self._run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.state: RunState = RunState.CREATED
        self._attempt_counters: dict[str, int] = {}
        self._metrics: list[MetricRecord] = []
        self._case_attempts: list[CaseAttemptRef] = []
        self._started_at: str = now_utc_iso()
        self._ended_at: str | None = None
        self._aggregate_status: str | None = None
        self._renderer_version: str = "p2.3"
        self._schema_versions: dict[str, str] = {
            "case_attempt_manifest": "1.0.0",
            "trace_event": "1.0.0",
            "metric_record": "1.0.0",
        }
        self._environment: dict[str, str] = {}
        self._requested_targets: list[str] = []
        self._manifest: Manifest | None = None

    # ---- properties ----

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def root(self) -> Path:
        return self._root

    def case_attempts(self) -> tuple[CaseAttemptRef, ...]:
        return tuple(self._case_attempts)

    def metrics(self) -> tuple[MetricRecord, ...]:
        return tuple(self._metrics)

    # ---- mutators ----

    def record_environment(self, environment: Mapping[str, str]) -> None:
        """Record the environment under which the run executes.
        Idempotent: merges into the existing environment map."""
        if self.state == RunState.FINALIZED:
            raise InvalidStateTransitionError(
                f"cannot record_environment in state {self.state}"
            )
        for k, v in environment.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise TypeError("environment keys + values must be str")
            self._environment[k] = v

    def record_requested_targets(self, targets: Sequence[str]) -> None:
        """Record which targets the run was requested against."""
        if self.state == RunState.FINALIZED:
            raise InvalidStateTransitionError(
                f"cannot record_requested_targets in state {self.state}"
            )
        for t in targets:
            if not isinstance(t, str):
                raise TypeError("requested_targets entries must be str")
            self._requested_targets.append(t)

    def add_case_attempt(
        self,
        case_id: str,
        trace_id: str,
    ) -> CaseAttemptRef:
        """Create `cases/<safe_case_id>/attempt-NNNN-<trace_id>/`.

        Monotonic per (run_id, safe_case_id): counter starts at 1 and
        increments. Refuses to overwrite an existing attempt.

        State transition (D9):
            CREATED → RUNNING  (on first call)
            RUNNING  → RUNNING (subsequent calls)

        Returns CaseAttemptRef pointing at the new directory.

        Raises:
            ValueError: if `case_id` or `trace_id` fails
                sanitize_identifier.
            InvalidStateTransitionError: if state == FINALIZED.
            FileExistsError: if the attempt directory already exists.
        """
        if self.state == RunState.FINALIZED:
            raise InvalidStateTransitionError(
                f"cannot add_case_attempt in state {self.state}"
            )
        safe_case_id = sanitize_identifier(case_id)
        safe_trace_id = sanitize_identifier(trace_id)

        # transition CREATED → RUNNING on first call
        if self.state == RunState.CREATED:
            self.state = RunState.RUNNING

        # monotonic counter per safe_case_id
        counter = self._attempt_counters.get(safe_case_id, 0) + 1
        attempt_id = make_attempt_id(counter)
        attempt_dir = (
            self.run_dir / "cases" / safe_case_id / f"{attempt_id}-{safe_trace_id}"
        )
        if attempt_dir.exists():
            raise FileExistsError(
                f"attempt directory already exists: {attempt_dir}"
            )
        attempt_dir.mkdir(parents=True, exist_ok=False)
        self._attempt_counters[safe_case_id] = counter

        ref = CaseAttemptRef(
            safe_case_id=safe_case_id,
            attempt_id=attempt_id,
            trace_id=safe_trace_id,
            path=attempt_dir,
        )
        self._case_attempts.append(ref)
        return ref

    def record_metric(self, key: str, value: int | str) -> None:
        """Append a metric to the run. Validates against whitelist (D5).

        Raises:
            InvalidStateTransitionError: if state == FINALIZED.
            MetricKeyForbiddenError: if `key` is not in
                ALLOWED_METRIC_KEYS.
            MetricValueTypeError: if `value` is not int or str.
        """
        if self.state == RunState.FINALIZED:
            raise InvalidStateTransitionError(
                f"cannot record_metric in state {self.state}"
            )
        self._metrics.append(MetricRecord.make(key, value))

    def finalize(self) -> Manifest:
        """Close the run. Writes `manifest.json` atomically. Returns it.

        State transition (D9): CREATED|RUNNING → FINALIZED.
        After finalize(), the run is immutable; further calls to
        `add_case_attempt`, `record_metric`, or `finalize` itself raise
        `InvalidStateTransitionError`.

        Note on `aggregate_status` (per review Minor 1 / R1):
            RunContext does NOT compute aggregate_status. That is the
            renderer's job (P2.3.B / `render_report.compute_aggregate_status`).
            We write `"unknown"` as a placeholder; the renderer overwrites
            its display value when constructing `report.md`.

        Raises:
            InvalidStateTransitionError: if state == FINALIZED.
        """
        if self.state == RunState.FINALIZED:
            raise InvalidStateTransitionError(
                f"cannot finalize in state {self.state}"
            )
        self.state = RunState.FINALIZED
        self._ended_at = now_utc_iso()
        # Per Minor 1 review: RunContext does NOT compute aggregate_status.
        # Renderer (P2.3.B) computes it from case-attempt-manifest.json
        # per-case terminal_status when rendering report.md.
        self._aggregate_status = "unknown"

        manifest = Manifest(
            schema_version=MANIFEST_SCHEMA_VERSION,
            run_id=self._run_id,
            started_at=self._started_at,
            ended_at=self._ended_at,
            requested_targets=tuple(self._requested_targets),
            environment=dict(self._environment),
            case_attempts=tuple(self._case_attempts),
            renderer_version=self._renderer_version,
            schema_versions=dict(self._schema_versions),
            aggregate_status="unknown",
            metrics_summary=tuple(self._metrics),
            report_status="valid",
            evidence_status="complete",
        )
        atomic_write_json(self.run_dir / "manifest.json", manifest.to_dict())
        self._manifest = manifest
        return manifest

    def load_manifest(self) -> Manifest:
        """Read manifest.json from disk. Used by reporters/renderers.

        Raises:
            FileNotFoundError: if manifest.json does not exist.
            UnsupportedManifestSchemaError: if schema_version unknown.
        """
        path = self.run_dir / "manifest.json"
        if not path.exists():
            raise FileNotFoundError(f"manifest.json missing at {path}")
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return Manifest.from_dict(data)


__all__ = [
    "MANIFEST_SCHEMA_VERSION",
    "SUPPORTED_MANIFEST_SCHEMA_VERSIONS",
    "UnsupportedManifestSchemaError",
    "RunState",
    "InvalidStateTransitionError",
    "SAFE_IDENTIFIER",
    "sanitize_identifier",
    "ALLOWED_METRIC_KEYS",
    "MetricKeyForbiddenError",
    "MetricValueTypeError",
    "MetricRecord",
    "make_attempt_id",
    "CaseAttemptRef",
    "Manifest",
    "now_utc_iso",
    "atomic_write_json",
    "RunContext",
]