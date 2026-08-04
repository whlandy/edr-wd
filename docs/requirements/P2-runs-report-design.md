# P2.3 — Runs, Reruns, Run-Level Report — Design (R0)

**Status**: APPROVED R1 — implementation phase (P2.3.A → P2.3.D) authorized.
**Predecessor**: P2.2 Recovery Branches (`4295987` on `origin/hermes-remote`).
**Successor**: P2.4 Production Hardening.
**Review stop point**: R0 design reviewed; 3 minor locks + 1 new test applied in R1 (D8/D9/D10, test_manifest_schema_version, P2.3.C renamed). Implementation now authorized; no further design-review gate required unless blocker discovered.

**R1 changes vs R0**:
- D8: `manifest.schema_version` mandatory; only `"1.0"` accepted (Issue 1 / HIGH).
- D9: `RunState` enum + state machine `CREATED → RUNNING → FINALIZED`; explicit `InvalidStateTransitionError` (Issue 2 / MEDIUM).
- D10: Two-axis output status `report_status` (valid/invalid) + `evidence_status` (complete/degraded) (Issue 3 / MEDIUM).
- Test added: `test_manifest_schema_version`.
- P2.3.C commit description renamed from "wiring" to "run artifact persistence layer".

---

## 1. Problem Statement

P2.2 produces per-case traces (`trace.md`, `step-results.json`) under
`artifacts/test-runs/<run_id>/cases/<safe_case_id>/attempt-<n>-<trace_id>/`.
A *run* (multi-case suite) is the unit a human evaluates, not a single
case. Today there is no run-level artifact:

* The user cannot see at a glance which cases passed/failed/blocked in
  this run.
* Headline totals are computed by hand from per-case outputs.
* Re-running the same case after a fix silently overwrites the previous
  attempt, destroying forensic history.
* No way to compare first-attempt stability vs final-attempt outcomes.
* Evidence-link rot (missing `trace.md`, corrupt `manifest.json`) is
  not surfaced; report either silently succeeds or hard-fails.

P2.3 introduces a run-level aggregation layer that:

1. Persists run identity, target set, environment, timing.
2. Aggregates per-case attempt outcomes into deterministic headline
   totals.
3. Renders a human-readable `report.md` with reconciliation guarantees.
4. Makes reruns strictly additive — no attempt ever overwrites another.
5. Surfaces (warns on) missing/corrupt evidence rather than hiding it.

---

## 2. Scope

### In Scope

* `agent/trace/runs.py`:
  * `RunContext` — lifecycle owner of a run.
  * `CaseAttemptRef` — pointer to one case attempt on disk.
  * `Manifest` — run-level `manifest.json` schema.
  * `sanitize_identifier(name)` — strict filename-safe sanitizer
    (only `[a-zA-Z0-9_-]` allowed).
  * `MetricsWhitelist` — schema-locked metric keys (FR-08).
* `agent/trace/render_report.py`:
  * `render_report(run, attempts, *, frozen_generated_at) -> str` —
    deterministic `report.md` body.
  * `reconcile_totals(manifests, headline) -> None` — raises on mismatch
    (FR-02).
  * `Totals` — pass/fail/blocked/skipped counts (first + final split).
* Atomic writes for `manifest.json` and `report.md` (no half-written
  file on disk).
* Run-level metrics persistence (allowlist-only; FR-08).
* Evidence-failure severity matrix (required metadata vs optional
  evidence; FR-09).

### Out Of Scope

* LLM-generated replans (P3.1).
* Production hardening — rate limits, adversarial handling, target
  restart policy (P2.4).
* Cross-case recovery sharing (each case keeps its own recovery state).

---

## 3. Architecture

### 3.1 RunContext Ownership (CRITICAL — addresses review #3)

```
RunContext                ← lifecycle owner
    |
    +-- create run dir
    +-- add_case_attempt()    ← only this method mutates disk state
    +-- finalize()            ← closes run, writes manifest.json
    +-- emit_event() / link_evidence() ← observers feed in (read-only)
```

`RunContext` is the **single owner** of run-level state. No other
module may:

* Create or delete `artifacts/test-runs/<run_id>/`.
* Write `manifest.json` directly.
* Rename or move attempt directories.
* Decide whether a rerun is additive (that is RunContext's policy).

Observer modules (`trace/store.py`, `trace/manifest.py`, P2.2
`trace_md.py`) **only emit events into RunContext**. They never mutate
the run directory directly.

#### 3.1.1 RunState State Machine (Issue 2 / D9)

```
RunState.CREATED ──add_case_attempt()──> RunState.RUNNING
                                              |
                                         finalize()
                                              |
                                              v
                                       RunState.FINALIZED
                                              |
                                       (immutable)
```

Transitions:

| From | Trigger | To |
|------|---------|-----|
| `CREATED` | first `add_case_attempt()` call | `RUNNING` |
| `CREATED` | `finalize()` (no attempts added) | `FINALIZED` (empty run) |
| `RUNNING` | `add_case_attempt()` | `RUNNING` (counter increments) |
| `RUNNING` | `record_metric()` | `RUNNING` |
| `RUNNING` | `finalize()` | `FINALIZED` |
| `FINALIZED` | any mutator | `InvalidStateTransitionError` |

State is **not** purely a file lock — it is the execution lifecycle of
the run. After `FINALIZED`, the run is closed: any further
`add_case_attempt()`, `record_metric()`, or attempt to re-finalize
raises `InvalidStateTransitionError` (a `RuntimeError` subclass). This
applies whether or not the file lock is held.

```
executor.run(TestCase)
    |
    +-- creates CaseAttempt dir under RunContext  (← P2.2 behavior)
    +-- emits TraceEvents to TraceStore             (observer)
    +-- calls RunContext.add_case_attempt(ref)      (← hook here)
```

### 3.2 Module Boundaries

```
agent/trace/runs.py           ← RunContext, CaseAttemptRef, Manifest
        |
        v
agent/trace/render_report.py   ← render_report, reconcile_totals
        |
        v
agent/trace/markdown.py       ← existing low-level md helpers (read-only)
agent/trace/manifest.py       ← existing case-attempt ManifestRecord
target/protocol_models        ← TestCase (input)
```

`render_report.py` consumes `RunContext` (read-only) + `list[CaseAttemptRef]`.
It does **not** mutate run state. It only renders.

---

## 4. Design Decisions (resolving review points)

### 4.1 Attempt directory naming (addresses review #5)

```
artifacts/test-runs/<run_id>/
    manifest.json
    cases/
        <safe_case_id>/
            attempt-0001-<trace_id>/    ← NNNN zero-padded 4 digits
            attempt-0002-<trace_id>/    ← monotonic, never overwrites
            attempt-0003-<trace_id>/
```

* `attempt-NNNN` is **monotonic per (run_id, case_id)**. Counter starts
  at 1, increments on each `add_case_attempt`.
* Use counter, **NOT timestamp**, to satisfy FR-05 determinism.
* Attempt counter lives in `RunContext._attempt_counters: dict[str, int]`.
* Concurrent writers (future) protected by file-lock on
  `manifest.json.lock` (out of P2.3 scope — single-writer assumed).

### 4.2 Filename sanitization (addresses review #6)

```python
SAFE_IDENTIFIER = re.compile(r"^[a-zA-Z0-9_-]+$")

def sanitize_identifier(name: str) -> str:
    """Reject `..`, separators, control chars, absolute paths.
    
    Raises ValueError if the input contains anything outside
    [a-zA-Z0-9_-]. Empty string raises ValueError.
    """
    if not isinstance(name, str) or not name:
        raise ValueError(f"identifier must be non-empty str, got {name!r}")
    if not SAFE_IDENTIFIER.match(name):
        raise ValueError(
            f"identifier {name!r} contains forbidden characters "
            f"(only [a-zA-Z0-9_-] allowed)"
        )
    return name
```

Applied to: `run_id`, `case_id` (when constructing safe_case_id),
`attempt_id`, `trace_id` (when used in path).

### 4.3 reconcile_totals contract (addresses review #4)

```python
def reconcile_totals(
    manifests: Sequence[CaseAttemptManifest],  # last attempt per case
    headline: Totals,
) -> None:
    """Verify headline totals match the sum of per-case final attempts.
    
    Raises ReconciliationError if any count in headline disagrees
    with the sum derived from manifests. Does NOT silently fix.
    """
    expected_pass = sum(1 for m in manifests if m.terminal_status == "passed")
    expected_fail = sum(1 for m in manifests if m.terminal_status == "failed")
    # ... same for blocked, skipped
    
    if headline.passed != expected_pass:
        raise ReconciliationError(
            f"passed: headline={headline.passed} "
            f"sum(manifests)={expected_pass}"
        )
    # ... etc
```

**Inputs**: `Sequence[CaseAttemptManifest]` (one per case, final attempt
only) + `Totals` (the headline summary derived from these manifests).

**Rule**: `headline.X == sum(manifests, where terminal_status=X)`. If any
disagree, raise. **No silent fix.** This is FR-P2.3-02.

### 4.4 Metrics privacy (addresses review #7)

```python
ALLOWED_METRIC_KEYS = frozenset({
    "duration_ms",       # int — milliseconds
    "step_count",        # int
    "retry_count",       # int
    "replan_count",      # int
    "recovery_count",    # int
    "outcome",           # str — one of {passed,failed,blocked,skipped}
    "screenshot_count",  # int
    "screenshot_bytes",  # int
    "first_attempt_outcome",  # str
    "final_attempt_outcome",  # str
})

class MetricRecord:
    """Schema-locked metric; constructed via factory that validates keys.
    
    No constructor that accepts arbitrary Mapping — prevents accidental
    leakage of screen_text, OCR, command args, credentials, env dumps.
    """
    
    @classmethod
    def make(cls, key: str, value: int | str) -> "MetricRecord":
        if key not in ALLOWED_METRIC_KEYS:
            raise MetricKeyForbiddenError(
                f"metric key {key!r} not in whitelist; "
                f"allowed: {sorted(ALLOWED_METRIC_KEYS)}"
            )
        if not isinstance(value, (int, str)):
            raise MetricValueTypeError(...)
        return cls(key=key, value=value)
```

FR-P2.3-08: "Metrics must not contain raw screen text or secret
arguments." Enforced at construction time, not by convention.

### 4.5 Evidence failure severity matrix (addresses review #8)

| Failure type | Severity | Action |
|--------------|----------|--------|
| `manifest.json` missing | **fail** | report refuses to render |
| `manifest.json` corrupt (json decode error) | **fail** | report refuses |
| `trace.md` missing | **warn** | render warning block, continue |
| `step-results.json` missing | **warn** | render warning, continue |
| screenshot file missing | **warn** | render warning, continue |
| screenshot file corrupt | **warn** | render warning, continue |
| hash chain verification fail | **warn** | render warning, mark evidence as untrusted |

Rationale: required metadata (manifest) is what makes the report
trustworthy. If manifest is corrupt, the report itself cannot be
trusted; we refuse to render. Optional evidence (trace.md,
screenshots) is supplementary; their absence degrades but doesn't
invalidate the report.

### 4.6 report.md deterministic output (FR-P2.3-05)

* All section ordering is fixed by code (not by input order).
* Per-case attempts listed in `case_id` lex order.
* Within a case, attempts listed in `attempt-NNNN` numeric order.
* All durations formatted with `str(duration_ms)` (no locale-sensitive
  formatting).
* `frozen_generated_at` (if provided) goes into the header verbatim;
  otherwise `now_utc_iso()` is used once at function entry and frozen
  for the rest of the render.

---

## 5. Interface Specifications

### 5.1 agent/trace/runs.py

```python
# --- Schema version (Issue 1 / D8) ---
MANIFEST_SCHEMA_VERSION = "1.0"
SUPPORTED_MANIFEST_SCHEMA_VERSIONS = frozenset({"1.0"})


class UnsupportedManifestSchemaError(ValueError):
    """Raised when a manifest's schema_version is not in SUPPORTED set."""


# --- RunState (Issue 2 / D9) ---
class RunState(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    FINALIZED = "finalized"


class InvalidStateTransitionError(RuntimeError):
    """Raised when RunContext mutator is called in wrong state."""


class RunContext:
    """Lifecycle owner of a run.

    Single source of truth for run-level state. All disk mutations
    under artifacts/test-runs/<run_id>/ flow through this class.

    State machine (see §3.1.1):
        CREATED --first add_case_attempt--> RUNNING --finalize--> FINALIZED
    """
    state: RunState
    _attempt_counters: dict[str, int]
    _metrics: list[MetricRecord]
    _case_attempts: list[CaseAttemptRef]
    _started_at: str
    _ended_at: str | None
    _aggregate_status: str | None

    def __init__(self, root: Path, run_id: str) -> None:
        """Idempotent — opens existing run or creates new one.

        State starts at CREATED. Raises ValueError if run_id fails
        sanitize_identifier.
        """

    def add_case_attempt(
        self, case: TestCase, trace_id: str,
    ) -> CaseAttemptRef:
        """Create cases/<safe_case_id>/attempt-NNNN-<trace_id>/.

        Monotonic per (run_id, safe_case_id): counter starts at 1 and
        increments. Refuses if attempt-NNNN already exists.

        State transition:
            CREATED -> RUNNING  (on first call)
            RUNNING  -> RUNNING (subsequent calls)

        Raises InvalidStateTransitionError if state == FINALIZED.
        Returns CaseAttemptRef pointing at the new directory.
        """

    def record_metric(self, key: str, value: int | str) -> None:
        """Append a metric to the run. Validates against whitelist.

        Raises InvalidStateTransitionError if state == FINALIZED.
        Raises MetricKeyForbiddenError if key not in ALLOWED_METRIC_KEYS.
        """

    def finalize(self) -> Manifest:
        """Close the run. Writes manifest.json atomically. Returns it.

        State transition: CREATED|RUNNING -> FINALIZED.
        After finalize(), the run is immutable; further calls to
        add_case_attempt, record_metric, or finalize itself raise
        InvalidStateTransitionError.

        Raises InvalidStateTransitionError if state == FINALIZED
        (double finalize).
        """

    @property
    def run_dir(self) -> Path: ...
    @property
    def run_id(self) -> str: ...


class CaseAttemptRef:
    """Pointer to one case attempt on disk."""
    safe_case_id: str
    attempt_id: str        # "attempt-NNNN"
    trace_id: str
    path: Path             # absolute path to attempt dir

    def trace_md_path(self) -> Path: ...
    def step_results_path(self) -> Path: ...
    def case_attempt_manifest_path(self) -> Path: ...


class Manifest:
    """Run-level manifest.json schema (see §6).

    schema_version is REQUIRED (D8). On construction it defaults to
    MANIFEST_SCHEMA_VERSION ("1.0"). On deserialization (from_dict),
    the value must be in SUPPORTED_MANIFEST_SCHEMA_VERSIONS or
    UnsupportedManifestSchemaError is raised.
    """
    schema_version: str         # REQUIRED; default "1.0"
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
    report_status: str          # "valid" | "invalid"  (D10)
    evidence_status: str        # "complete" | "degraded"  (D10)

    def to_dict(self) -> dict[str, Any]: ...

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Manifest":
        """Deserialize. Validates schema_version; raises
        UnsupportedManifestSchemaError if not in supported set.
        """


def sanitize_identifier(name: str) -> str: ...
def make_attempt_id(attempt_no: int) -> str: ...  # "attempt-NNNN"
```

### 5.2 agent/trace/render_report.py

```python
class Totals:
    """Pass/fail/blocked/skipped counts; first + final split."""
    passed: int
    failed: int
    blocked: int
    skipped: int

    @classmethod
    def from_manifests(cls, manifests) -> "Totals": ...

    @property
    def total(self) -> int: ...


# --- Two-axis status (Issue 3 / D10) ---
@dataclass(frozen=True)
class ReportStatus:
    """Two-axis output status.

    report_status:
        "valid"   — headline totals reconciled successfully
        "invalid" — manifest missing/corrupt; report cannot be trusted

    evidence_status:
        "complete" — all per-case trace.md / step-results / screenshots
                     present and parseable
        "degraded" — one or more pieces of supplementary evidence
                     missing/corrupt; report totals still valid
    """
    report_status: str
    evidence_status: str


def render_report(
    run: RunContext,
    *,
    attempts: Sequence[CaseAttemptRef],
    frozen_generated_at: str | None = None,
) -> tuple[str, ReportStatus]:
    """Render report.md body and emit ReportStatus.

    Returns (body, status):
        body   — markdown string
        status — ReportStatus with report_status + evidence_status

    Order: header -> report status banner -> headline totals ->
    first/final split -> per-case table -> failed/blocked details ->
    recovery stats -> evidence integrity -> cleanup warnings ->
    evidence warnings.

    Raises:
        ManifestMissingError: if run has no manifest.json (FR-09 fail
            severity). report_status="invalid", evidence_status="degraded".
        ManifestCorruptError: same.
        UnsupportedManifestSchemaError: if manifest schema_version
            not in supported set (D8). report_status="invalid".
    """


def reconcile_totals(
    manifests: Sequence[CaseAttemptManifest],
    headline: Totals,
) -> None:
    """Verify headline matches sum of per-case manifests.

    Raises ReconciliationError on mismatch. No silent fix.
    """
```

---

## 6. manifest.json Schema

```json
{
  "schema_version": "1.0",
  "renderer_version": "p2.3",
  "run_id": "run-2026-08-03-...",
  "started_at": "2026-08-03T10:00:00.000Z",
  "ended_at": "2026-08-03T10:15:23.456Z",
  "requested_targets": ["edr-mac-01", "edr-win-01"],
  "environment": {
    "platform": "macOS-26.3.0",
    "python_version": "3.14.5"
  },
  "case_attempts": [
    {
      "safe_case_id": "case_A",
      "trace_id": "...",
      "attempt_id": "attempt-0001",
      "terminal_status": "passed",
      "duration_ms": 12345
    }
  ],
  "aggregate_status": "failed",
  "report_status": "valid",
  "evidence_status": "degraded",
  "metrics_summary": [
    {"key": "duration_ms", "value": 923456},
    {"key": "step_count", "value": 17},
    {"key": "outcome", "value": "passed"}
  ],
  "schema_versions": {
    "case_attempt_manifest": "1.0.0",
    "trace_event": "1.0.0",
    "metric_record": "1.0.0"
  }
}
```

**schema_version field** (D8):
* Top-level `"schema_version": "1.0"` is **mandatory**.
* Consumers MUST reject (raise `UnsupportedManifestSchemaError`) any
  manifest whose `schema_version` is not in
  `SUPPORTED_MANIFEST_SCHEMA_VERSIONS = {"1.0"}`.
* Future migrations: bump to `"1.1"` only when the schema is
  backwards-compatible enough to be parsed by `"1.0"` consumers;
  otherwise bump major and add to a new supported set.

---

## 7. report.md Structure

```markdown
# Run Report — run-2026-08-03-... (generated 2026-08-03T10:15:30.000Z)

> **Report Status**: `valid` — totals reconciled against per-case manifests.
> **Evidence Status**: `degraded` — some supplementary evidence missing; see Warnings section.

## Identity

| Field | Value |
|-------|-------|
| Run ID | ... |
| Started | ... |
| Ended | ... |
| Duration | ... |
| Targets | ... |
| Environment | ... |
| Renderer | p2.3 |
| Manifest schema_version | 1.0 |

## Headline Totals

| Status | Count |
|--------|-------|
| Passed | 1 |
| Failed | 1 |
| Blocked | 1 |
| Skipped | 0 |
| **Total** | **3** |

## First vs Final Attempt

| Status | First | Final |
|--------|-------|-------|
| Passed | 0 | 1 |
| Failed | 1 | 1 |
| Blocked | 1 | 1 |
| Skipped | 0 | 0 |

(First = outcome of attempt-0001; Final = outcome of last attempt per case.)

## Per-Case Attempts

| Case | Attempts | Final | Duration | Trace |
|------|----------|-------|----------|-------|
| case_A | 1 | passed | 12.3s | [trace.md](...) |
| case_B | 2 | failed | 45.6s | [attempt-0002 trace.md](...) |
| case_C | 1 | blocked | 7.8s | [trace.md](...) |

## Failures And Blocks

### case_B — attempt-0002 (failed)

- Last step: action_id=...
- Error code: ...
- Recovery attempts: 2
- [Full trace.md](...)
- [Screenshot](...)

## Recovery Stats

| Metric | Value |
|--------|-------|
| Total replans | 2 |
| Total recoveries | 5 |
| Cases that recovered | 1 / 3 |

## Evidence Integrity

| Source | Status |
|--------|--------|
| Hash chain | OK / WARN |
| Manifest chain | OK |
| Cross-trace integrity | OK |

## Cleanup Warnings

- case_X: cleanup step failed (non-critical)

## Warnings

- case_B/attempt-0001: trace.md missing (attempt preserved for forensics)

---

*Renderer: p2.3 | Reconciliation: passed*
```

---

## 8. Functional Requirements (from P2-recovery-production.md §FR-P2.3)

| ID | Requirement | Addressed in |
|----|-------------|--------------|
| FR-P2.3-01 | Reruns additive; separate dir per attempt | §4.1, §5.1 |
| FR-P2.3-02 | Reconciliation raises on mismatch | §4.3, §5.2 |
| FR-P2.3-03 | First vs final attempt split | §7 (First vs Final Attempt section) |
| FR-P2.3-04 | Missing trace.md → warning | §4.5, §7 (Warnings) |
| FR-P2.3-05 | Deterministic given frozen timestamps | §4.6 |
| FR-P2.3-06 | Filename sanitization rejects bad inputs | §4.2, §5.1 |
| FR-P2.3-07 | Cleanup-outcome-critical flips status | §5.2 (render_report) |
| FR-P2.3-08 | Metrics no screen text / secret args | §4.4 (whitelist) |
| FR-P2.3-09 | Tolerate missing/corrupt evidence | §4.5 (severity matrix) |

---

## 9. Required Tests (from spec lines 450-460 + review additions)

### Spec tests (6)

| Test | Purpose |
|------|---------|
| `test_run_three_cases_totals` | 3-case run (pass/fail/blocked) → correct counts |
| `test_rerun_additive` | Second run → new attempt dir, first intact |
| `test_first_vs_final_attempt_split` | Headline uses final; both shown |
| `test_reconciliation_raises_on_mismatch` | Tampered attempt → raise |
| `test_missing_trace_md_warning` | Warning block in report |
| `test_cleanup_outcome_critical` | Flag flips status |

### Additional tests (per review #9)

| Test | Purpose |
|------|---------|
| `test_attempt_id_monotonic` | Counter increments, never reuses; refuses to overwrite |
| `test_metric_redaction` | Forbidden keys (screen_text, args, etc.) raise at construction |
| `test_sanitize_identifier_strict` | `..`, `/`, `\`, control chars, empty → raise |
| `test_evidence_severity_matrix` | manifest corrupt → fail; trace.md missing → warn |
| `test_runcontext_ownership` | Other modules cannot mutate run dir (defensive copy / API surface test) |
| `test_manifest_schema_version` | Old schema `"0.9"` rejected; `"1.0"` accepted; unknown raises `UnsupportedManifestSchemaError` |
| `test_run_state_machine` | `CREATED` → `RUNNING` (after first add) → `FINALIZED` (after finalize); post-FINALIZED mutators raise `InvalidStateTransitionError` |
| `test_report_status_two_axis` | `ReportStatus.report_status` / `evidence_status` returned alongside body; manifest-missing produces `invalid` + `degraded` |

**Total: 14 tests** (6 spec + 8 added).

---

## 10. Commit Plan (per review #2)

| Commit | Module(s) | Tests | Layer |
|--------|-----------|-------|-------|
| **P2.3.A** | `agent/trace/runs.py` — `RunContext`, `RunState`, `CaseAttemptRef`, `Manifest` (schema_version), `sanitize_identifier`, `MetricRecord` whitelist | `test_sanitize_identifier_strict`, `test_attempt_id_monotonic`, `test_metric_redaction`, `test_runcontext_ownership`, `test_run_state_machine`, `test_manifest_schema_version` | core |
| **P2.3.B** | `agent/trace/render_report.py` — `Totals`, `ReportStatus`, `render_report`, `reconcile_totals`, severity matrix | `test_reconciliation_raises_on_mismatch`, `test_report_status_two_axis`, basic report rendering tests | renderer |
| **P2.3.C** | `agent/trace/__init__.py` exports + `manifest.json` atomic write helpers + run artifact persistence layer (`manifest.json` writer, `report.md` writer) | integration tests for RunContext + render persistence | persistence |
| **P2.3.D** | Full P2.3 acceptance suite (6 spec tests + extra) | `test_run_three_cases_totals`, `test_rerun_additive`, `test_first_vs_final_attempt_split`, `test_missing_trace_md_warning`, `test_cleanup_outcome_critical`, `test_evidence_severity_matrix` | acceptance |

Each commit is independently runnable. After P2.3.A, you can construct
RunContexts and create attempts. After P2.3.B, you can render reports
(in tests). After P2.3.C, exports + persistence layer is in place
(`manifest.json` and `report.md` get written atomically). After P2.3.D,
the spec acceptance criteria are met.

---

## 11. Branch and Push Workflow

```
feature/p2-3-runs-reports  (created from origin/hermes-remote at 4295987)
    |
    +-- P2.3.A (review gate)
    +-- P2.3.B (review gate)
    +-- P2.3.C (review gate)
    +-- P2.3.D (review gate)
    |
 fast-forward merge (local, no merge commit)
    |
hermes_remote_origin (local tracking) → 4295987 + 4 commits
    |
git push origin hermes_remote_origin:hermes-remote
    |
origin/hermes-remote updated
```

Each commit is pushed to `origin/hermes-remote` after review approval,
following the P2.2 D/E/F/G pattern.

---

## 12. Out of Scope (cross-references)

* **P3.1 LLM Replans** — out. P2.3 only consumes the P2.2 replan events
  that already exist; doesn't generate new ones.
* **P2.4 Production Hardening** — out. Rate limits, target restart
  policy, adversarial handling deferred.
* **Multi-run aggregation** — out. One run = one `manifest.json`.
* **Run-level diff (compare two runs)** — out. P2.3 produces the
  artifacts; comparison is a future feature.

---

## 13. Decision Log

| # | Decision | Rationale | Source |
|---|----------|-----------|--------|
| D1 | RunContext is sole lifecycle owner | Avoids multi-owner race; clean dependency | review #3 |
| D2 | reconcile_totals raises on mismatch, no silent fix | Trust > availability | review #4 |
| D3 | attempt-NNNN monotonic counter (4-digit zero-pad) | Determinism (FR-05) | review #5 |
| D4 | sanitize_identifier allows only [a-zA-Z0-9_-] | Stricter than spec; prevents path injection | review #6 |
| D5 | MetricRecord factory with whitelist | Schema-locked, not convention | review #7 |
| D6 | Manifest corrupt → fail; trace.md missing → warn | Trust boundary | review #8 |
| D7 | Commit order: A (runs) → B (render) → C (persistence) → D (acceptance) | Each commit independently runnable | review #2 |
| D8 | `manifest.schema_version` mandatory; only `"1.0"` accepted; `UnsupportedManifestSchemaError` on unknown | Future P2.4/P3 readers need version discrimination | review Issue 1 (HIGH) |
| D9 | `RunState` enum: `CREATED → RUNNING → FINALIZED`; explicit `InvalidStateTransitionError` | Execution lifecycle not just file lock | review Issue 2 (MEDIUM) |
| D10 | Two-axis `ReportStatus`: `report_status` (valid/invalid) + `evidence_status` (complete/degraded) | Caller distinguishes statistics credibility from evidence completeness | review Issue 3 (MEDIUM) |

---

## 14. Review Stop Point

**R1 design-review APPROVED → implementation phase (P2.3.A → P2.3.D).**

R1 closes all 3 review-issue locks (D8/D9/D10). No further
design-review round required unless implementation discovers a new
blocker.

R1 implementation gating checklist (these are now MANDATORY in the
implementation, not review items):

- [x] D8: `manifest.schema_version` is mandatory; only `"1.0"` is
      accepted by consumers; `UnsupportedManifestSchemaError` raised
      on unknown / missing / unsupported values.
- [x] D9: `RunState` enum + state machine implemented in `RunContext`;
      `InvalidStateTransitionError` raised on post-FINALIZED mutators
      and double-finalize.
- [x] D10: `ReportStatus` dataclass with `report_status` and
      `evidence_status` returned alongside report body from
      `render_report()`.

Implementation phase gates:

- [x] P2.3.A reviewed & merged (core)
- [x] P2.3.B reviewed & merged (renderer)
- [x] P2.3.C reviewed & merged (persistence)
- [x] P2.3.D reviewed & merged (acceptance)
- [x] Full P2.3 acceptance suite passed; see `P2-RETROSPECTIVE.md`.
- [x] Pushed and merged to `origin/hermes-remote`.

---

*Document owner: P2.3 implementation cycle.*
*Predecessor: P2-recovery-production.md §Checkpoint P2.3 (lines 343-460).*
