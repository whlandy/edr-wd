# P2.4 — Cleanup Cascade + Markdown Escaping — Design (R0)

| Field | Value |
|-------|-------|
| Phase | P2.4 |
| Status | **R1** (design gate APPROVED WITH MINOR LOCKS — patches applied; ready for implementation) |
| Reviewer | Hermes (under edr-test direction) |
| Target branch | `origin/hermes-remote` |
| Working branch | `feature/p2-4-cleanup-escape` |
| Spec source | `docs/requirements/P2-recovery-production.md` FR-P2.3-07 + P2.3 review Minor 2 |

---

## 1. Problem Statement

P2.3 delivered the run-level report (FR-P2.3-01 through -09) but
explicitly deferred **FR-P2.3-07** (cleanup cascade) and recorded
**Minor 2** (markdown escaping) as future hardening. P2.4 closes both.

Two real risks left open after P2.3:

1. **Cleanup cascade missing.** A test that passes its execution but
   fails its cleanup currently reports `passed`, hiding the leak.
   Spec FR-P2.3-07 requires: when the test definition marks cleanup as
   outcome-critical, a cleanup failure must cascade the case to
   `failed`. Non-critical cleanup failures must not affect outcome.
2. **Markdown injection possible.** `case.title`, environment values,
   metadata, and failure messages are user-supplied. Once they reach
   the report renderer, they can break markdown tables (`|` in a
   value) or inject structure (newlines, backticks, headings).

P2.4 fixes both with explicit data-flow ownership and a single
escaping contract.

---

## 2. Scope

### 2.1 In scope

| ID | Scope |
|----|-------|
| **S1** | `TestCase.cleanup_outcome_critical: bool` field (target/protocol_models) |
| **S2** | `cleanup_status` + `cleanup_outcome_critical` on `ManifestRecord` (agent/trace) |
| **S3** | Runner propagation: cleanup execution result → manifest (case-attempt-manifest.json) |
| **S4** | Cascade rule in renderer: passed→failed iff cleanup_status=failed AND cleanup_outcome_critical=true |
| **S5** | `escape_markdown(value)` helper applied to every **user-controlled value** before markdown serialization |
| **S6** | Cleanup Warnings section in report.md (new) |
| **S7** | Acceptance suite covering cleanup + escape (P2.4.D) |

### 2.2 Out of scope

| ID | Item | Defer to |
|----|------|----------|
| O1 | `case.title` rendering in Identity table | P2.5 (needs API to pull title from case def) |
| O2 | Live runner integration (Real RecoveryExecutor → cleanup result) | P2.4.B is **schema** + **mock propagation** only; live integration deferred to P2.5 |
| O3 | HTML report renderer (markdown-only this phase) | P3+ |
| O4 | FR-P2.4-01..-10 (image size, event payload size, trace limits, secret string scan, etc.) | P2.4-priorities-conflict; per spec those are separate |

---

## 3. Architecture

### 3.1 Cleanup data flow

```
TestCase
  |
  +-- cleanup_outcome_critical: bool        [NEW — P2.4.A]
  |
  v
Execution runner (target/protocol_models or agent/execution)
  |
  +-- exec cleanup steps → result {passed|failed|unknown}
  +-- lookup TestCase.cleanup_outcome_critical
  |
  v
Case-attempt-manifest.json
  |
  +-- cleanup_status: "passed|failed|unknown"   [NEW]
  +-- cleanup_outcome_critical: bool            [NEW]
  |
  v
render_report()
  |
  +-- compute display_status(m):
  |     if m.terminal_status == "passed"
  |        and m.cleanup_status == "failed"
  |        and m.cleanup_outcome_critical is True:
  |         display_status = "failed"
  |     else:
  |         display_status = m.terminal_status
  |
  +-- Headline / First-vs-Final / Aggregate use display_status
  +-- Original terminal_status preserved on disk (audit trail)
```

### 3.2 Markdown escape data flow

```
User-supplied strings (case title, env, metadata, failure msg)
  |
  v
escape_markdown(value)        [NEW — P2.4.D]
  |
  +-- replace `|` with `\|`
  +-- replace `\n` with `\\n` + literal newline → space
  +-- replace `\r` → `` (strip)
  +-- NUL bytes → `` (strip)
  +-- control chars (0x01..0x1f) → `` (strip)
  +-- backtick `` ` `` → `` \` `` (prevent inline code injection)
  |
  v
Markdown renderer
  |
  v
report.md on disk (no injection possible)
```

System-generated structure (table headers, fixed strings, headings,
numeric values, sanitized identifiers) is **never** escaped — escaping
those would corrupt the report layout.

---

## 4. Design Decisions (resolving review locks)

### Lock 1 — Cleanup outcome data ownership (resolved → D11)

```
TestCase.cleanup_outcome_critical : bool  (declarative, in def)
execution result                  : runtime evidence
case-attempt-manifest.json        : carries both

renderer MUST NOT infer cleanup semantics.
```

The renderer reads `cleanup_status` and `cleanup_outcome_critical`
verbatim from manifest. No inference, no defaults from the runner side.

### Lock 2 — Cascade semantics (resolved → D12)

Default (non-critical cleanup failure):

```
terminal_status = passed
cleanup_status  = failed
cleanup_outcome_critical = False  →  display_status = passed  ✓
```

Critical cleanup failure:

```
terminal_status = passed
cleanup_status  = failed
cleanup_outcome_critical = True   →  display_status = failed  ✓
```

Forbidden:

```
* Any cleanup failure auto-cascading  (D12.1)
* Renderer inferring outcome-critical from context (D12.2)
* Cascade modifying on-disk manifest  (D12.3 — read-only view)
```

### Lock 3 — Backward compatibility (resolved → D13)

Old manifest (no cleanup fields):

```json
{"terminal_status": "passed", ...}
```

```
cleanup_status missing  →  unknown
cleanup_outcome_critical missing  →  False (default non-critical)

display_status(m) = m.terminal_status
                  = "passed"

→ no cascade
```

Explicitly forbidden: treating `missing == failed`. The whole point of
backward compat is that P2.3-era manifests render unchanged.

### Lock 4 — Markdown escaping contract (resolved → D14)

Boundary rule:

```
renderer MUST escape every **user-controlled value** before markdown
serialization (prevent table-break / inline-code injection / control-char damage).
renderer MUST NOT escape any value generated by the renderer itself
(markdown headings, fixed structure, sanitized identifiers, numeric
counts — escaping these would corrupt the report).
```

Escaped (user-supplied):

| Field | Source |
|-------|--------|
| `case.title` (when rendered) | TestCase (P2.5 deferred, but contract set now) |
| `manifest.environment[k]` / `v` | Execution runner |
| `manifest.metadata[k]` / `v` | Execution runner |
| failure / block message | per-case-attempt-manifest |
| cleanup message | per-case-attempt-manifest |

Not escaped (renderer-generated):

| Field | Reason |
|-------|--------|
| table headers | fixed |
| numeric counts | `int` → str, no escape needed |
| sanitized identifiers (`safe_case_id`, `attempt_id`, `trace_id`) | already constrained to `[a-zA-Z0-9_-]` by `sanitize_identifier` |
| section headings | fixed |
| fixed banner / footer | fixed |

Escape table (D14.1):

| Input char | Output |
|------------|--------|
| `\|` (pipe) | `\|` |
| `\n` (LF) | ` ` (literal space) |
| `\r` (CR) | `` (stripped) |
| `\x00` (NUL) | `` (stripped) |
| `\x01..\x1f` (control) | `` (stripped) |
| `` ` `` (backtick) | `` \` `` |
| other | unchanged |

### Lock 5 — Commit plan (resolved → D15)

```
P2.4.A  schema extension (TestCase + ManifestRecord)
P2.4.B  runner-side propagation contract + mock test
P2.4.C  renderer cascade semantics + Cleanup Warnings section
P2.4.D  markdown escape helper + acceptance suite
```

Per-commit: implement → test → review gate → push (or batch-at-end
per user-elected cadence).

---

## 5. Interface Specifications

### 5.1 target/protocol_models — TestCase (delta)

```python
@dataclass(frozen=True)
class TestCase:
    # ... existing fields ...

    cleanup: tuple[AtomicTestStep, ...] = field(default_factory=tuple)
    cleanup_outcome_critical: bool = False     # NEW (P2.4.A)
    timeout_seconds: int = 120

    _ALLOWED: frozenset[str] = frozenset({
        "case_id", "title", "description", "profiles", "tags",
        "preconditions", "steps", "cleanup",
        "cleanup_outcome_critical",                    # NEW
        "timeout_seconds",
    })
```

Strict mode (`_strict_from_dict`) accepts the new field. Default
`False` preserves backward compat for existing TestCase JSON.

### 5.2 agent/trace/runs.py — ManifestRecord (delta)

```python
@dataclass(frozen=True)
class ManifestRecord:
    trace_id: str
    branch_heads: tuple[str, ...]
    catalog_digest: str
    evidence_counts: Mapping[str, int]
    terminal_status: str
    integrity_verification_result: IntegrityReport

    # NEW (P2.4.A)
    cleanup_status: str = "unknown"           # passed | failed | unknown
    cleanup_outcome_critical: bool = False    # outcome-critical flag

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "trace_id": self.trace_id,
            ...
            "terminal_status": self.terminal_status,
            "cleanup_status": self.cleanup_status,                   # NEW
            "cleanup_outcome_critical": self.cleanup_outcome_critical,  # NEW
            "integrity_verification_result": ...,
        }


def from_dict(data: Mapping[str, Any]) -> ManifestRecord:
    """Backward-compatible: missing cleanup_* fields default safely (D13)."""
    return ManifestRecord(
        ...,
        terminal_status=data["terminal_status"],
        cleanup_status=data.get("cleanup_status", "unknown"),
        cleanup_outcome_critical=bool(data.get("cleanup_outcome_critical", False)),
        ...
    )
```

### 5.3 agent/trace/render_report.py — display_status (NEW)

```python
def compute_display_status(m: Mapping[str, Any]) -> str:
    """Apply cascade rule (D12) to a per-case manifest.

    Returns the effective status to use for headline totals,
    first/final split, aggregate status, and per-case Final column.

    Read-only of m: does not mutate the underlying dict.
    """
    terminal = m.get("terminal_status", "unknown")
    cleanup_status = m.get("cleanup_status", "unknown")
    cleanup_critical = bool(m.get("cleanup_outcome_critical", False))
    if (
        terminal == "passed"
        and cleanup_status == "failed"
        and cleanup_critical
    ):
        return "failed"
    return terminal
```

Backward compat: when `cleanup_status` is missing → default
`"unknown"` → no cascade. Lock 3 satisfied.

### 5.4 agent/trace/render_report.py — escape_markdown (NEW)

```python
_MD_ESCAPE_TABLE = str.maketrans({
    "|": r"\|",
    "`": r"\`",
})


def escape_markdown(value: Any) -> str:
    """Escape user-supplied strings for safe markdown rendering (D14).

    * Pipes escaped so user values cannot break table cells.
    * Backticks escaped so user values cannot inject inline code.
    * Newlines normalized to spaces (cell-break prevention).
    * Control characters stripped (NUL, CR, 0x01-0x1f).

    The function is idempotent for already-escaped strings within the
    same call (it does NOT preserve a previous escape). Sanitized
    identifiers (safe_case_id, attempt_id, trace_id) MUST NOT pass
    through this function — they are already constrained.
    """
    if value is None:
        return ""
    s = str(value)
    # Strip control chars first (cheap pass).
    s = "".join(ch for ch in s if ch == "\t" or ch >= " ")
    s = s.replace("\r", "").replace("\n", " ").replace("\t", " ")
    return s.translate(_MD_ESCAPE_TABLE)
```

`tab` is preserved (some renderers collapse tabs to spaces — leaving
the choice to the markdown engine).

### 5.5 Runner propagation contract (P2.4.B)

**Stable interface contract.** P2.4.B defines the contract that the
future real cleanup executor (P2.5) MUST obey. Once P2.4.B lands, the
contract is frozen; P2.5 implements against this contract, not against
the mock. The contract is:

* Input: `TestCase` (with `cleanup` + `cleanup_outcome_critical`).
* Output: `case-attempt-manifest.json` extension with
  `cleanup_status` ∈ {`passed`, `failed`, `unknown`} and
  `cleanup_outcome_critical: bool`.
* Contract guarantees:
  - `cleanup_status="unknown"` iff cleanup was not executed.
  - `cleanup_status="passed"` iff cleanup ran and all steps succeeded.
  - `cleanup_status="failed"` iff cleanup ran and at least one step
    failed.
  - `cleanup_outcome_critical` is read from `TestCase` verbatim.
* Renderer reads both fields verbatim (D11) — no inference.

P2.4.B implementation: mock test runner that writes manifest per the
contract above. Real executor (P2.5) replaces the mock without
changing the contract.

The execution runner produces case-attempt-manifest.json. The contract
extension:

```
For each TestCase with cleanup steps:
    exec_result = run_cleanup_steps(case.cleanup)

    case_attempt_manifest["cleanup_status"] = exec_result   # "passed" | "failed"
    case_attempt_manifest["cleanup_outcome_critical"] = case.cleanup_outcome_critical
```

If the runner does not execute cleanup (e.g., test skipped before
cleanup), `cleanup_status = "unknown"`. Renderer treats unknown as
"do not cascade" (D12).

### 5.6 Public exports (delta)

```python
# agent/trace/__init__.py
from .render_report import (
    ...,
    escape_markdown,            # NEW
    compute_display_status,     # NEW
)
```

---

## 6. manifest.json Schema delta

### 6.1 case-attempt-manifest.json

| Field | Type | Required | Default | Notes |
|-------|------|----------|---------|-------|
| `schema_version` | string | yes | — | `"1.0"` (unchanged from P2.3) |
| `trace_id` | string | yes | — | sanitized |
| `branch_heads` | array | no | `[]` | |
| `catalog_digest` | string | yes | — | 64-hex |
| `evidence_counts` | object | no | `{}` | |
| `terminal_status` | string | yes | — | passed/failed/blocked/skipped |
| `cleanup_status` | string | **no** | `"unknown"` | **NEW P2.4.A** |
| `cleanup_outcome_critical` | bool | **no** | `false` | **NEW P2.4.A** |
| `integrity_verification_result` | object | yes | — | |

### 6.2 Schema version

`schema_version` remains `"1.0"` (P2.3 D8 lock). Cleanup fields are
**additive** and backward compatible. A separate `schema_version`
bump is reserved for breaking changes only.

`SUPPORTED_MANIFEST_SCHEMA_VERSIONS` does NOT change.

### 6.3 Backward compat tests

```
test_old_manifest_without_cleanup_status_no_cascade  # P2.4.D
```

A P2.3-era manifest (no cleanup fields) must render with display_status
== terminal_status (no cascade). Existing P2.3 acceptance tests must
remain green.

---

## 7. report.md Structure delta

### 7.1 New section: Cleanup Warnings

Renders between Failures And Blocks and Warnings when at least one
per-case manifest has `cleanup_status == "failed"` (regardless of
`cleanup_outcome_critical`):

```markdown
## Cleanup Warnings

| Case | Attempt | Cleanup Status | Outcome-Critical |
|------|---------|----------------|------------------|
| case_A | attempt-0001 | failed | false |
| case_B | attempt-0002 | failed | true |
```

Case IDs and attempt IDs are sanitized identifiers — not escaped.
The Cleanup Status cell is a fixed enum — not escaped. The
Outcome-Critical cell is `true`/`false` — not escaped.

When `cleanup_status == "passed"` or `"unknown"` on all attempts, this
section is omitted entirely (clean report, no noise).

### 7.2 Updated Identity table

When P2.5 lands and adds `case.title`, it will use `escape_markdown()`.
For now (P2.4), Identity table content unchanged from P2.3.

### 7.3 Updated Failures And Blocks

When a failure/block includes a `failure_message` field (P2.5 source),
the message is `escape_markdown()`-wrapped. For P2.4 the section
structure is unchanged; only the escape contract is introduced.

### 7.4 Updated Warnings section

The Warnings list items are renderer-generated (`f"{safe_case_id}/{attempt_id}: ..."`),
so they do NOT pass through escape. Sanitized identifiers ensure no
injection.

### 7.5 Determinism

Escape is a pure function of input bytes — no time, no RNG, no
external state. Two equal inputs produce equal escape outputs. FR-P2.3-05
determinism is preserved.

---

## 8. Functional Requirements

### 8.1 From spec (`P2-recovery-production.md`)

| FR | Title | Coverage |
|----|-------|----------|
| FR-P2.3-07 | Cleanup cascade | **NEW — P2.4.A + B + C** |

### 8.2 From P2.3 review Minor 2

| Item | Coverage |
|------|----------|
| Markdown escaping | **NEW — P2.4.D** |

### 8.3 NOT in P2.4 scope (deferred / out)

| FR | Reason |
|----|--------|
| FR-P2.4-01 to -10 | Different phase per spec priorities; P2.4 naming reused for cleanup+escape per user direction |
| FR-P2.4-05 (secret string scan) | Already covered by P2.3 FR-P2.3-08 metric redaction + metric whitelist |
| `case.title` rendering | P2.5 (needs API to pull title from case def into case-attempt-manifest) |
| Live runner cleanup execution | P2.5 (P2.4.B provides **contract** + mock test) |

---

## 9. Required Tests

### 9.1 P2.4.A — schema

| Test | Target |
|------|--------|
| `test_testcase_cleanup_outcome_critical_default_false` | TestCase defaults |
| `test_testcase_cleanup_outcome_critical_strict_accepts` | strict_from_dict accepts new field |
| `test_manifest_record_cleanup_status_default_unknown` | ManifestRecord default |
| `test_manifest_record_from_dict_missing_cleanup_fields_uses_default` | backward compat |

### 9.2 P2.4.B — runner contract

| Test | Target |
|------|--------|
| `test_cleanup_status_propagated_to_case_attempt_manifest` | mock runner writes cleanup_status |
| `test_cleanup_outcome_critical_propagated_from_test_case` | runner reads TestCase.cleanup_outcome_critical |
| `test_no_cleanup_steps_means_cleanup_status_unknown` | skipped-before-cleanup case |

### 9.3 P2.4.C — renderer cascade

| Test | Target |
|------|--------|
| `test_cleanup_noncritical_failure_does_not_fail_case` | cascade rule, non-critical |
| `test_cleanup_critical_failure_cascades_failed` | cascade rule, critical |
| `test_cleanup_passed_no_cascade` | cleanup_status=passed, terminal=passed → passed |
| `test_cleanup_unknown_status_no_cascade` | cleanup_status explicitly `"unknown"` (not missing) → no cascade; differs from `test_old_manifest_without_cleanup_status_no_cascade` which covers missing field |
| `test_cleanup_failed_terminal_failed_no_change` | cleanup_status=failed, terminal=failed → failed (no change) |
| `test_cleanup_warnings_section_renders_when_failed_present` | Cleanup Warnings markdown |
| `test_cleanup_warnings_section_omitted_when_all_passed` | no section if all passed |

### 9.4 P2.4.D — escape + acceptance

| Test | Target |
|------|--------|
| `test_markdown_escape_pipe` | `|` → `\|` |
| `test_markdown_escape_newline` | `\n` → space |
| `test_markdown_escape_backtick` | `` ` `` → `` \` `` |
| `test_markdown_escape_strips_control_chars` | NUL, CR, 0x01-0x1f stripped |
| `test_old_manifest_without_cleanup_status_no_cascade` | Lock 3 verification |
| `test_cleanup_status_persisted_in_manifest_on_disk` | P2.4.B → P2.4.A wiring |
| `test_report_determinism_with_escaped_input` | FR-P2.3-05 + escape |
| `test_fr07_cleanup_cascade_implemented_remove_deferred_marker` | REPLACES P2.3.D TestFR07CleanupCascadeDeferred |

### 9.5 Removal

**`TestFR07CleanupCascadeDeferred`** (P2.3.D test_acceptance.py) gets
replaced by `TestFR07CleanupCascadeImplemented` in P2.4.D. The
deferred-marker test_fr07_deferred_with_rationale is **deleted** (not
just rewritten) when P2.4 ships.

---

## 10. Commit Plan

```
776635e (origin/hermes-remote HEAD — P2.3.D)
   |
   v
P2.4.A  feat(models,trace): add cleanup_outcome_critical + cleanup_status schema
   |
   | TestCase.cleanup_outcome_critical: bool = False
   | ManifestRecord.{cleanup_status, cleanup_outcome_critical}
   | ManifestRecord.from_dict backward compat (default unknown / false)
   | from_dict strict accepts new field
   |
   | Tests: 4 P2.4.A schema tests
   |
P2.4.B  feat(execution): add cleanup result propagation to case-attempt-manifest
   |
   | Runner contract: cleanup_status + cleanup_outcome_critical in case-attempt-manifest.json
   | Mock test: write manifest with cleanup_status, verify readback
   |
   | Tests: 3 P2.4.B propagation tests
   |
P2.4.C  feat(trace): add cleanup cascade semantics + Cleanup Warnings section
   |
   | compute_display_status(m) — D12 rule
   | Headline / First-Final / Aggregate / Per-case use display_status
   | Cleanup Warnings section rendered when any failed
   | Original terminal_status preserved on disk
   |
   | Tests: 7 P2.4.C cascade tests
   |
P2.4.D  test(trace): add markdown escape + cleanup acceptance suite (P2.4.D)
   |
   | escape_markdown helper (D14)
   | Boundary applications (when case title / env / metadata appear)
   | Acceptance: 8 tests (FR-P2.3-07 + escape)
   | REPLACE TestFR07CleanupCascadeDeferred with TestFR07CleanupCascadeImplemented
   |
   | Tests: 8 P2.4.D tests
   |
   v
P2.4-push  single push of A+B+C+D → origin/hermes-remote
```

Each commit:

```
implement
   ↓
test (≥3 new tests per commit, must include at least one FR-level
      test for A/B/C and at least one escape-determinism test for D)
   ↓
review gate
   ↓
push origin/hermes-remote
        (per-commit or batch-at-end per user-elected cadence)
```

---

## 11. Branch and Push Workflow

```
feature/p2-4-cleanup-escape
        |
        +-- created from origin/hermes-remote @ 776635e
        |
        +-- P2.4.A → P2.4.D (4 commits)
        |
   fast-forward
        |
origin/hermes-remote
```

Per the user's elected P2.3 cadence:

* Per-commit review gate (each commit individually reviewed + APPROVED)
* Per-commit OR batch-at-end push (user's choice per cadence call)
* No merge commit, no cherry-pick
* No rebase onto `main` / `origin/main`
* Local tracking branch `hermes_remote_origin` updated after push:
  `git branch -f hermes_remote_origin origin/hermes-remote`

---

## 12. Out of Scope

Per §2.2 + FR list §8.3:

* `case.title` rendering (P2.5)
* Live runner cleanup execution (P2.5)
* HTML report renderer (P3+)
* FR-P2.4-01..-10 (separate phase per spec)
* Top-level TestCase validation / catalog changes beyond
  `cleanup_outcome_critical` field

---

## 13. Decision Log

| # | Decision | Rationale | Source |
|---|----------|-----------|--------|
| D11 | `cleanup_status` + `cleanup_outcome_critical` are explicit manifest fields; renderer reads verbatim, no inference | Lock 1: renderer must not own business semantics | review Lock 1 |
| D12 | Cascade: passed→failed iff `terminal=passed` AND `cleanup=failed` AND `outcome_critical=True`; otherwise display_status = terminal | Lock 2: fixed rule, no auto-cascade, no inference | review Lock 2 |
| D13 | Missing `cleanup_*` fields → default `"unknown"` / `False` → no cascade | Lock 3: backward compat without silent reinterpretation | review Lock 3 |
| D14 | `escape_markdown()` applied to every **user-controlled value** before markdown serialization; renderer-generated strings NOT escaped | Lock 4: prevent injection without corrupting structure (M1: boundary → user-controlled) | review Lock 4 + M1 |
| D14.1 | Escape table: `\|`, `` \` ``, newline→space, control strip | Defensive default | review Lock 4 |
| D15 | Commit plan A (schema) → B (runner contract) → C (cascade) → D (escape + acceptance) | Each commit independently runnable; per-commit review boundary | review Lock 5 |
| D16 | `schema_version` stays `"1.0"` (no bump for additive cleanup fields) | Additive changes don't break existing consumers | D8 continuity |
| D17 | Cascade is a read-time view; manifest.json on disk is never mutated by renderer | Audit trail: original terminal_status must remain recoverable | D12.3 |

---

## R1 Changelog

R0 → R1 patches applied per user review (APPROVED WITH MINOR LOCKS):

| Lock | Patch location | Change |
|------|----------------|--------|
| **M1** | §2.1 S5, §3.2, §13 D14, §14 | Replaced "every renderer boundary" → "every **user-controlled value** before markdown serialization" |
| **M2** | §5.5 | Added "Stable interface contract" paragraph — P2.4.B contract is frozen; P2.5 real executor implements against this contract, not the mock |
| **Add test** | §9.3 | Renamed + clarified `test_cleanup_unknown_no_cascade` → `test_cleanup_unknown_status_no_cascade`; description now distinguishes from `test_old_manifest_without_cleanup_status_no_cascade` (explicit `"unknown"` vs missing field) |

R1 closes all 5 review locks + 2 minor patches. No further design
review required unless implementation surfaces a new blocker.

---

## 14. Review Stop Point

**R1 design-review APPROVED → implementation phase (P2.4.A → P2.4.D).**

R1 closes all 5 review-issue locks (D11/D12/D13/D14/D15) and the 2
minor patches (M1/M2 + the `test_cleanup_unknown_status_no_cascade`
clarification). No further design-review round required unless
implementation discovers a new blocker.

R1 implementation gating checklist (these are now MANDATORY in the
implementation, not review items):

- [ ] D11: `TestCase.cleanup_outcome_critical` field added; strict mode accepts.
- [ ] D11: `ManifestRecord.cleanup_status` + `cleanup_outcome_critical` fields added; from_dict backward-compat default.
- [ ] D12: `compute_display_status(m)` implements cascade rule; called from headline / first-final / aggregate / per-case.
- [ ] D12: cleanup cascade does NOT mutate manifest.json on disk.
- [ ] D13: missing `cleanup_*` fields → `"unknown"` / `False` → no cascade.
- [ ] D14: `escape_markdown()` helper applied to every **user-controlled value** before markdown serialization (M1: boundary → user-controlled).
- [ ] D14.1: Escape table covers `|`, backtick, newline, control chars.
- [ ] D15: 4-commit topology A/B/C/D with per-commit review gate.
- [ ] D16: `schema_version` unchanged; cleanup fields are additive.
- [ ] D17: On-disk manifest preserved (cascade is read-time only).

Implementation phase gates:

- [ ] P2.4.A reviewed & pushed (schema)
- [ ] P2.4.B reviewed & pushed (runner contract)
- [ ] P2.4.C reviewed & pushed (cascade + Cleanup Warnings)
- [ ] P2.4.D reviewed & pushed (escape + acceptance)
- [ ] `TestFR07CleanupCascadeDeferred` (P2.3.D) **deleted**; replaced by `TestFR07CleanupCascadeImplemented`
- [ ] Full regression: P0/P1 116/116 + P2.1 43/43 + P2.2 124/124 + P2.3 148/148 + P2.4 N/N PASS, baseline stable
- [ ] Push to `origin/hermes-remote`

---

## Frontmatter

```yaml
---
status: R1 (APPROVED WITH MINOR LOCKS — patches applied; ready for implementation)
target_branch: origin/hermes-remote
working_branch: feature/p2-4-cleanup-escape
spec_source: docs/requirements/P2-recovery-production.md FR-P2.3-07 + P2.3 review Minor 2
review_verdict: APPROVED WITH MINOR LOCKS
minor_locks_patched:
  M1: "escape boundary → user-controlled value before serialization"
  M2: "P2.4.B stable interface contract for P2.5"
  test_add: "test_cleanup_unknown_status_no_cascade"
---
```