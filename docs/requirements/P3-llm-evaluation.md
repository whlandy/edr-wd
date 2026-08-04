# P3 — LLM Planning And Evaluation

## Status

- State: requirements (no implementation yet)
- Source contract: `../architecture/01-action-trace-test-report-design.md` §24, §18 Phase C/D, §20 Live E2E, §21 Metrics
- Mapped checkpoints: **P3.1 Structured LLM Planner**, **P3.2 Evaluation And Metrics**
- Predecessors: **P2.5 Design Review Gate** (closed; see `P2-5-design-gate.md`) → **P3.1 Design Review Gate** (closed round 1; see `P3-1-design-gate.md`) → **P3.2 Design Review Gate** (in review; see `P3-2-evaluation-design-gate.md`)
- Completion state: **P2.5 closed; P3.1 design gate CLOSED; P3.1 implementation CLOSED; P3.2 design gate IN REVIEW (round 1, awaiting sign-off); P3.2 implementation UNDONE**
- Milestone: **P3.2 closes the design**. After P3.2, EDR-WD can hand a
  target snapshot to a structured-output model and execute an
  autonomous plan with reproducible evaluation evidence.

P3 must wait for P2.5 (design review gate) before any
implementation begins. P2.5 surfaces 10 deferred decisions (D1..D10)
that P3 will lock in via its prompt + output schema; closing them in
P2.5 avoids a redesign round inside P3 itself.

---

## Checkpoint P3.1 — Structured LLM Planner

### Scope

Give the LLM only live enabled actions for the active backend/profile.
Require strict structured `ActionSequence` output. Add planner
guidance that prefers semantic actions over coordinate fallbacks.
Re-observe and replan after state-changing steps. Persist sanitized
plans and planning outcomes. Reject coordinate fallbacks when a unique
semantic target exists. Keep high-risk actions behind deterministic
confirmation policy.

### In Scope

- `agent/planner/catalog_view.py`:
  - Filter the catalog by live enablement for the current backend
    and any declared profile constraints (`windows_hisec`,
    `macos_hisec`, `macos_generic`).
  - Expose `enabled_actions_for(backend, profile) -> list[ActionSpec]`
    and a JSON view consumable as a planner tool list.
- `agent/planner/schema.py`:
  - JSON Schema for `ActionSequence` derived from the P0.2 models.
    Mark `additionalProperties: false` on every object.
  - `LLMPlanRequest`: `{snapshot_id, target, profile, allowed_actions:
    [action_id...], schema_ref, hints}`.
- `agent/planner/prompt.py`:
  - System prompt listing only `allowed_actions` with descriptions,
    `input_schema`, `requires`, `side_effect`, `risk`,
    `rollback_class`, `preferred_over`.
  - Explicit guidance:
    - prefer semantic actions (`gui.click`, `gui.type_text`,
      `gui.select`) over `pointer.*` fallbacks;
    - never choose a coordinate when a unique semantic target
      exists in the latest snapshot;
    - declare `transition.expected` and `on_error` per step;
    - never bypass `requires` (`connected_window`, `window_lock`,
      `target_ref`).
- `agent/planner/parse.py`:
  - Strict JSON parser; rejects any deviation from the schema; on
    failure, surfaces `code: "plan_schema_invalid"` with a JSON
    pointer to the offending field.
  - Validates each step against the live catalog (`enabled_actions`),
    rejects disabled actions with `code: "backend_disabled"`.
  - Resolves optional `action_code` against the chosen `action_id`
    and rejects mismatches.
  - Validates `target_ref` belongs to the supplied `snapshot_id`.
- `agent/planner/persist.py`:
  - Persist `plan_created` event with sanitized args (no secret
    arguments, no full selector text if it matches a redaction rule).
  - Persist `plan_validated` or `plan_rejected` with structured
    errors.
- `agent/planner/post_step.py`:
  - After any state-changing step, the planner view is re-queried
    for a fresh snapshot; if the previous `target_ref` is stale,
    the planner is asked to replan (a single bounded replan — no
    arbitrary loops).
- `agent/planner/confirmation.py`:
  - High-risk actions (`risk ∈ {high, irreversible}`,
    `side_effect ∈ {gui_mutation, system_mutation}` when policy
    demands confirmation) require a deterministic policy gate; the
    LLM cannot bypass it. The gate returns
    `confirmation_required` with the action_id and asks the calling
    system to provide a confirmation token; without one, the action
    is rejected.

### Out Of Scope

- LLM-provider-specific tooling (P3.1 must work with any provider that
  supports structured output).
- Generation of free-form prose plans; only structured output is
  accepted.
- Run-level execution multi-case (P2.3 already aggregates; P3.1 only
  drives single-case plans through the executor).
- Evaluation datasets (P3.2).

### Dependencies

- All P2 checkpoints.
- P0.1 catalog (live enablement).
- P0.2 models / validator.
- P0.3 resolver (snapshot/target identity).
- Existing safety policy (HiSec `expected_process_name` checks).

### Functional Requirements

| ID | Requirement |
|----|-------------|
| FR-P3.1-01 | The planner tool list returned to the LLM contains exactly the enabled actions for `(backend, profile)`; disabled actions are absent, not just marked. |
| FR-P3.1-02 | Any deviation from the JSON schema returns `code: "plan_schema_invalid"` and prevents dispatch. |
| FR-P3.1-03 | Disabled action chosen by the model returns `code: "backend_disabled"` and the offending `action_id`. |
| FR-P3.1-04 | Coordinate fallback chosen while a unique semantic target exists in the latest snapshot returns `code: "coordinate_fallback_not_allowed"`. |
| FR-P3.1-05 | Mismatch between `action_id` and `action_code` returns `code: "action_code_mismatch"`. |
| FR-P3.1-06 | A `target_ref.snapshot_id` not matching the current snapshot returns `code: "target_ref_snapshot_mismatch"` and triggers replan. |
| FR-P3.1-07 | After a state-changing step (mutation or transition), the next request asks for a fresh snapshot; stale target_refs are rejected. |
| FR-P3.1-08 | High-risk / irreversible actions receive a deterministic policy gate; LLM cannot bypass; missing confirmation returns `code: "confirmation_required"`. |
| FR-P3.1-09 | Persisted plan events contain no secret arguments (verified by an audit that scans for configured patterns). |
| FR-P3.1-10 | Plans with dependency cycles are rejected at validation time with `code: "dependency_cycle"` and the involved `step_id`s. |

### Interface And Data Requirements

```python
# agent/planner/catalog_view.py
def enabled_actions_for(backend: str, profile: str) -> list[ActionSpec]: ...
def planner_tool_list(backend: str, profile: str) -> list[dict]: ...

# agent/planner/schema.py
PLAN_JSON_SCHEMA: dict  # derived from ActionSequence + TestCase
LLMPlanRequest: dataclass
LLMPlanResponse: dataclass

# agent/planner/parse.py
def parse_plan(payload: dict, *,
               catalog: CatalogView,
               snapshot_id: str) -> tuple[ActionSequence, list[ValidationError]]: ...

# agent/planner/post_step.py
def needs_replan(prev_plan: ActionSequence,
                 snapshot_after: ObservationSnapshot,
                 catalog: CatalogView) -> bool: ...

# agent/planner/confirmation.py
class ConfirmationPolicy:
    def requires_confirmation(self, action: ActionSpec) -> bool: ...
    def confirm(self, action_id: str, token: str) -> bool: ...
```

### Acceptance Criteria

1. Catalog view for `(macos_accessibility, macos_hisec)` excludes
   `type_text`, `select`, `get_text`.
2. Invalid model output (extra field, wrong enum, missing
   `step_id`) is rejected with stable `code` and JSON pointer.
3. A coordinate action chosen while a unique `gui.click` candidate
   exists returns `coordinate_fallback_not_allowed`.
4. A stale `target_ref.snapshot_id` is rejected.
5. An irreversible action without a confirmation token is rejected
   even if the model "asks" for it.
6. Persisted plan events contain no configured secret string.

### Required Tests

| Test | Purpose |
|------|---------|
| `test_enabled_actions_filter` | Per `(backend, profile)` table. |
| `test_invalid_plan_schema_rejected` | Several negative fixtures. |
| `test_disabled_action_in_plan_rejected` | `backend_disabled`. |
| `test_coordinate_fallback_not_allowed_when_semantic_exists` | `coordinate_fallback_not_allowed`. |
| `test_action_code_mismatch_in_plan` | Code path. |
| `test_target_ref_snapshot_mismatch` | `target_ref_snapshot_mismatch`. |
| `test_replan_after_state_changing_step` | Replan flag set; no loops. |
| `test_high_risk_action_requires_confirmation` | Without token → rejected. |
| `test_persisted_plan_audit_no_secrets` | Negative persistence test. |
| `test_dependency_cycle_in_plan_rejected` | Cycle detection on plans. |

### Deliverables

- `agent/planner/{catalog_view,schema,prompt,parse,persist,post_step,confirmation}.py`.
- JSON Schema export under `test_case/schema/plan.schema.json`
  (matching `test-case.schema.json` placement in architecture §6).
- Tests under `test_case/test_planner/` (unit) and
  `test_case/test_planner_integration/` (with stub LLM provider).
- Migration note: planner is opt-in; legacy callers continue to
  work.

### Review Stop Point

Stop after all P3.1 acceptance criteria pass. Reviewer confirms:

- the LLM only sees enabled actions;
- invalid output never reaches dispatch;
- coordinate fallbacks are blocked when semantic targets exist;
- confirmation gate cannot be bypassed by the model;
- plans carry no secrets.

---

## Checkpoint P3.2 — Evaluation And Metrics

### Scope

Add action-selection and sequence-success evaluation datasets.
Measure semantic vs coordinate ratio, replans per task, stale target
counts, safety rejection, recovery success, screenshot cost, and
execution latency. Add regression thresholds without making live GUI
availability a unit-CI requirement.

### In Scope

- `agent/eval/datasets.py`:
  - Versioned fixtures: `eval-fixtures/v1/...` with deterministic
    snapshots, expected plans, expected outcomes, and expected
    metric ranges.
  - Each fixture is a tuple: `(snapshot, profile, expected_plan_id?,
    allowed_actions, expected_step_results, expected_metrics_delta)`.
- `agent/eval/runner.py`:
  - Run fixtures against a stub planner + executor; collect
    metrics; compare against expected ranges.
  - Run against real planner with stub executor; verify selection
    distribution and reasoning quality.
- `agent/eval/metrics.py`:
  - Aggregations required (architecture §21):
    - action selections and validation failures by `action_id`
    - semantic vs coordinate ratio
    - step/case success rate
    - expectation failure categories
    - stale/ambiguous target counts
    - transition and unexpected-transition counts
    - retries, replans, recoveries, recovery success
    - screenshot count, bytes, capture failures, redactions
    - execution, observation, assertion, persistence, rendering
      durations
  - Metrics writers must reject payloads containing
    `args`, observed text, or selector hints.
- `agent/eval/thresholds.py`:
  - Configurable thresholds with sane defaults; regression
    reports which metric, expected range, and observed value, and
    points back to the trace evidence.
- `agent/eval/ci_gate.py`:
  - Command-line tool that runs evaluation; supports `--no-gui`
    for unit-CI (uses fixtures and stub executor).
- Reproducibility: a regression report must include a content
  digest of the catalog, fixtures, and planner prompt template so
  results can be traced back to inputs.

### Out Of Scope

- Model fine-tuning (out of scope of this design).
- Online A/B testing.
- Live E2E mandatory in CI (live runs remain opt-in; P2.4 already
  provides sample artifacts).

### Dependencies

- All P0/P1/P2/P3.1 checkpoints.

### Functional Requirements

| ID | Requirement |
|----|-------------|
| FR-P3.2-01 | Evaluation is reproducible from versioned fixtures; running twice produces identical reports given frozen inputs. |
| FR-P3.2-02 | Metrics contain no sensitive text (audit confirms zero matches against the configured secret registry and zero observed screen text). |
| FR-P3.2-03 | Regressions are reported by `(action_id, backend, profile)` and link back to the underlying trace evidence. |
| FR-P3.2-04 | Evaluation supports a `--no-gui` mode for unit CI; live E2E is opt-in. |
| FR-P3.2-05 | Each metric writer refuses to serialize a payload that contains observed text or secret patterns. |
| FR-P3.2-06 | Datasets are content-addressed; the dataset digest is part of the regression report. |
| FR-P3.2-07 | Thresholds are externalized to a config file; CI can override per metric. |
| FR-P3.2-08 | Replan rate, recovery success rate, and semantic/coordinate ratio each have explicit targets documented in the eval README. |

### Interface And Data Requirements

```python
# agent/eval/runner.py
class EvalRun:
    fixtures: list[Fixture]
    metrics: Metrics
    regressions: list[Regression]

def run_eval(fixtures_root: Path,
             planner: PlannerProvider,
             executor_factory: ExecutorFactory,
             *,
             no_gui: bool = True) -> EvalRun: ...

# agent/eval/metrics.py
class Metrics:
    def add(self, key: str, value: float, *,
            tags: dict | None = None) -> None: ...
    def to_json(self) -> dict: ...   # raises on secret pattern

# agent/eval/thresholds.py
@dataclass(frozen=True)
class Thresholds:
    max_replan_rate: float = 0.2
    min_recovery_success_rate: float = 0.6
    min_semantic_ratio: float = 0.8
    # ...
```

### Acceptance Criteria

1. Running eval on the same fixtures twice produces identical
   reports (deterministic).
2. Each metric writer audit confirms no secret / no screen text.
3. Regression report shows the offending metric, expected range,
   observed value, and trace evidence link.
4. `--no-gui` CI run completes without any MCP backend.
5. Live E2E run on a real target (manual sample) produces a
   metric bundle that includes screenshot cost and recovery rate.

### Required Tests

| Test | Purpose |
|------|---------|
| `test_eval_determinism` | Two runs → identical reports. |
| `test_metrics_secret_audit` | Fuzz attempt: secret string never serialized. |
| `test_regression_report_links_to_evidence` | Trace evidence id present. |
| `test_no_gui_ci_path` | No MCP connection attempted. |
| `test_thresholds_per_metric_override` | CLI flag changes threshold. |
| `test_dataset_content_addressing` | Dataset digest stable. |

### Deliverables

- `agent/eval/{datasets,runner,metrics,thresholds,ci_gate}.py`.
- Eval fixtures under `agent/eval/fixtures/v1/`.
- Eval README documenting the regression thresholds and how to
  reproduce.
- Sample regression report under
  `test_case/fixtures/eval_report/`.

### Review Stop Point (Design Complete)

Stop after all P3.2 acceptance criteria pass. Reviewer confirms:

- evaluation is reproducible;
- metrics writers never leak secrets;
- regressions point back to evidence;
- thresholds are documented and overridable;
- no live GUI is required for unit CI.

This checkpoint closes the design. After approval, the project may
begin incremental delivery (small PRs per checkpoint, per
architecture §23).

---

## Status Of Existing Code

| Area | Existing piece | Reusable in P3 |
|------|----------------|-----------------|
| Backend | `target/automation/*` | Yes — planner and eval continue to dispatch through existing backend methods. |
| Executor | (built in P1.2) | Yes — eval runs against the same executor. |
| Tests | `test_case/test_e2e/*` | Reused as the basis for live-eval sample runs; P3.2 does not modify their scripts. |
| Skill instructions | `SKILL.md` | Will need a one-line addendum once P3 ships (mention planner gate for irreversible actions); out of scope for this requirements document. |

No P3 piece is implemented. P3 is entirely new code that consumes all
prior artifacts.

## Open Decisions Deferred From Earlier

These were deferred in P0/P1/P2 and need final resolution before
P3.1 implementation:

1. **UUIDv7 vs ULID**: must be picked once and applied to every ID
   (`event_id`, `trace_id`, `branch_id`, `snapshot_id`,
   `evidence_id`, `checkpoint_id`, `plan_id`).
2. **LLM provider abstraction**: P3.1 must not bind to one
   provider. A thin `PlannerProvider` interface is required so a
   different backend (or a stub for CI) can be substituted.
3. **Confirmation token source**: policy gate is deterministic; the
   source of the token (operator / external system) is decided
   when the first irreversible SOP lands. P3.1 only requires that
   the gate exists and is bypass-proof.