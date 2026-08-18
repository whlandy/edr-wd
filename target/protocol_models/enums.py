"""
enums.py — P0.2 protocol enum sets.

Architecture §9.3 (expectation types), §10 (validation codes),
§11.4 (cleanup outcome), §19 (error envelope), §15.1 (transition kind).

Each enum value is documented with the source clause so future edits
can be cross-checked. Enum value changes are MAJOR bumps in any
schema that persists the value (architecture §7.2).
"""

from __future__ import annotations


# Architecture §9.3 — typed expectation registry (V1).
EXPECTATION_ACTION_OK               = "action_ok"
EXPECTATION_WINDOW_OPEN             = "window_open"
EXPECTATION_WINDOW_CLOSED           = "window_closed"
EXPECTATION_ACTIVE_WINDOW_OWNER     = "active_window_owner"
EXPECTATION_CONTROL_EXISTS          = "control_exists"
EXPECTATION_CONTROL_ABSENT          = "control_absent"
EXPECTATION_CONTROL_TEXT_EQUALS     = "control_text_equals"
EXPECTATION_CONTROL_TEXT_CONTAINS   = "control_text_contains"
EXPECTATION_CONTROL_VALUE_EQUALS    = "control_value_equals"
EXPECTATION_CONTROL_CHECKED_EQUALS  = "control_checked_equals"
EXPECTATION_CONTROL_ENABLED_EQUALS  = "control_enabled_equals"
EXPECTATION_WINDOW_TEXT_CONTAINS    = "window_text_contains"
#: Compares control text against a strftime pattern rendered when the
#: expectation is evaluated, so a recorded "today" means the replay's today.
EXPECTATION_CONTROL_TEXT_CONTAINS_TIME = "control_text_contains_time"
#: Window-wide variant: the pattern need only appear somewhere in the window,
#: so an assertion can be written without knowing any control's identity.
EXPECTATION_WINDOW_TEXT_CONTAINS_TIME = "window_text_contains_time"
EXPECTATION_VISUAL_EVIDENCE_CAPTURED = "visual_evidence_captured"

VALID_EXPECTATION_TYPES: frozenset[str] = frozenset({
    EXPECTATION_ACTION_OK,
    EXPECTATION_WINDOW_OPEN,
    EXPECTATION_WINDOW_CLOSED,
    EXPECTATION_ACTIVE_WINDOW_OWNER,
    EXPECTATION_CONTROL_EXISTS,
    EXPECTATION_CONTROL_ABSENT,
    EXPECTATION_CONTROL_TEXT_EQUALS,
    EXPECTATION_CONTROL_TEXT_CONTAINS,
    EXPECTATION_CONTROL_VALUE_EQUALS,
    EXPECTATION_CONTROL_CHECKED_EQUALS,
    EXPECTATION_CONTROL_ENABLED_EQUALS,
    EXPECTATION_WINDOW_TEXT_CONTAINS,
    EXPECTATION_CONTROL_TEXT_CONTAINS_TIME,
    EXPECTATION_WINDOW_TEXT_CONTAINS_TIME,
    EXPECTATION_VISUAL_EVIDENCE_CAPTURED,
})


# Architecture §9.4 / §11 — on_error policy per step.
ON_ERROR_ABORT             = "abort"
ON_ERROR_RETRY             = "retry"
ON_ERROR_CAPTURE_AND_ABORT = "capture_and_abort"
ON_ERROR_REOBSERVE_REPLAN  = "reobserve_replan"

VALID_ON_ERROR: frozenset[str] = frozenset({
    ON_ERROR_ABORT,
    ON_ERROR_RETRY,
    ON_ERROR_CAPTURE_AND_ABORT,
    ON_ERROR_REOBSERVE_REPLAN,
})


# Architecture §15.1 — transition kinds (declared here so models and
# the P2.1 detector share the same names).
TRANSITION_NONE                   = "none"
TRANSITION_CONTROL_STATE_CHANGE   = "control_state_change"
TRANSITION_PAGE_NAVIGATION        = "page_navigation"
TRANSITION_MODAL_OPEN             = "modal_open"
TRANSITION_MODAL_CLOSE            = "modal_close"
TRANSITION_WINDOW_OPEN            = "window_open"
TRANSITION_WINDOW_CLOSE           = "window_close"
TRANSITION_WINDOW_OWNER_CHANGE    = "window_owner_change"
TRANSITION_APPLICATION_RESTART    = "application_restart"
TRANSITION_UNKNOWN_MATERIAL_CHANGE = "unknown_material_change"

VALID_TRANSITION_KINDS: frozenset[str] = frozenset({
    TRANSITION_NONE,
    TRANSITION_CONTROL_STATE_CHANGE,
    TRANSITION_PAGE_NAVIGATION,
    TRANSITION_MODAL_OPEN,
    TRANSITION_MODAL_CLOSE,
    TRANSITION_WINDOW_OPEN,
    TRANSITION_WINDOW_CLOSE,
    TRANSITION_WINDOW_OWNER_CHANGE,
    TRANSITION_APPLICATION_RESTART,
    TRANSITION_UNKNOWN_MATERIAL_CHANGE,
})


# Architecture §9.4 / §11 — step status set (consumer of executor state).
STEP_STATUS_PENDING  = "pending"
STEP_STATUS_RUNNING  = "running"
STEP_STATUS_PASSED   = "passed"
STEP_STATUS_FAILED   = "failed"
STEP_STATUS_BLOCKED  = "blocked"
STEP_STATUS_SKIPPED  = "skipped"
STEP_STATUS_ABORTED  = "aborted"

VALID_STEP_STATUSES: frozenset[str] = frozenset({
    STEP_STATUS_PENDING,
    STEP_STATUS_RUNNING,
    STEP_STATUS_PASSED,
    STEP_STATUS_FAILED,
    STEP_STATUS_BLOCKED,
    STEP_STATUS_SKIPPED,
    STEP_STATUS_ABORTED,
})


# Architecture §9.4 — case outcome precedence.
CASE_OUTCOME_PASSED  = "passed"
CASE_OUTCOME_FAILED  = "failed"
CASE_OUTCOME_BLOCKED = "blocked"
CASE_OUTCOME_SKIPPED = "skipped"

VALID_CASE_OUTCOMES: frozenset[str] = frozenset({
    CASE_OUTCOME_PASSED,
    CASE_OUTCOME_FAILED,
    CASE_OUTCOME_BLOCKED,
    CASE_OUTCOME_SKIPPED,
})


# Architecture §9.2 — AtomicTestStep.evidence.screenshot override values.
EVIDENCE_SCREENSHOT_NONE         = "none"
EVIDENCE_SCREENSHOT_AFTER        = "after"
EVIDENCE_SCREENSHOT_BEFORE_AFTER = "before_after"
EVIDENCE_SCREENSHOT_ON_FAILURE   = "on_failure"

VALID_EVIDENCE_SCREENSHOT: frozenset[str] = frozenset({
    EVIDENCE_SCREENSHOT_NONE,
    EVIDENCE_SCREENSHOT_AFTER,
    EVIDENCE_SCREENSHOT_BEFORE_AFTER,
    EVIDENCE_SCREENSHOT_ON_FAILURE,
})


# Architecture §8.2 — Target.kind values.
TARGET_KIND_WINDOW  = "window"
TARGET_KIND_CONTROL = "control"

VALID_TARGET_KINDS: frozenset[str] = frozenset({
    TARGET_KIND_WINDOW,
    TARGET_KIND_CONTROL,
})


__all__ = [
    # Expectation types (§9.3)
    "EXPECTATION_ACTION_OK",
    "EXPECTATION_WINDOW_OPEN",
    "EXPECTATION_WINDOW_CLOSED",
    "EXPECTATION_ACTIVE_WINDOW_OWNER",
    "EXPECTATION_CONTROL_EXISTS",
    "EXPECTATION_CONTROL_ABSENT",
    "EXPECTATION_CONTROL_TEXT_EQUALS",
    "EXPECTATION_CONTROL_TEXT_CONTAINS",
    "EXPECTATION_CONTROL_VALUE_EQUALS",
    "EXPECTATION_CONTROL_CHECKED_EQUALS",
    "EXPECTATION_CONTROL_ENABLED_EQUALS",
    "EXPECTATION_WINDOW_TEXT_CONTAINS",
    "EXPECTATION_VISUAL_EVIDENCE_CAPTURED",
    "EXPECTATION_CONTROL_TEXT_CONTAINS_TIME",
    "EXPECTATION_WINDOW_TEXT_CONTAINS_TIME",
    "VALID_EXPECTATION_TYPES",
    # on_error policy
    "ON_ERROR_ABORT",
    "ON_ERROR_RETRY",
    "ON_ERROR_CAPTURE_AND_ABORT",
    "ON_ERROR_REOBSERVE_REPLAN",
    "VALID_ON_ERROR",
    # Transition kinds (§15.1)
    "TRANSITION_NONE",
    "TRANSITION_CONTROL_STATE_CHANGE",
    "TRANSITION_PAGE_NAVIGATION",
    "TRANSITION_MODAL_OPEN",
    "TRANSITION_MODAL_CLOSE",
    "TRANSITION_WINDOW_OPEN",
    "TRANSITION_WINDOW_CLOSE",
    "TRANSITION_WINDOW_OWNER_CHANGE",
    "TRANSITION_APPLICATION_RESTART",
    "TRANSITION_UNKNOWN_MATERIAL_CHANGE",
    "VALID_TRANSITION_KINDS",
    # Step status / case outcome
    "STEP_STATUS_PENDING",
    "STEP_STATUS_RUNNING",
    "STEP_STATUS_PASSED",
    "STEP_STATUS_FAILED",
    "STEP_STATUS_BLOCKED",
    "STEP_STATUS_SKIPPED",
    "STEP_STATUS_ABORTED",
    "VALID_STEP_STATUSES",
    "CASE_OUTCOME_PASSED",
    "CASE_OUTCOME_FAILED",
    "CASE_OUTCOME_BLOCKED",
    "CASE_OUTCOME_SKIPPED",
    "VALID_CASE_OUTCOMES",
    # Evidence
    "EVIDENCE_SCREENSHOT_NONE",
    "EVIDENCE_SCREENSHOT_AFTER",
    "EVIDENCE_SCREENSHOT_BEFORE_AFTER",
    "EVIDENCE_SCREENSHOT_ON_FAILURE",
    "VALID_EVIDENCE_SCREENSHOT",
    # Target kinds (§8.2)
    "TARGET_KIND_WINDOW",
    "TARGET_KIND_CONTROL",
    "VALID_TARGET_KINDS",
]
