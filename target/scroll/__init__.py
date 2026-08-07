"""
scroll — the composite scroll layer (ScrollResult / Observer / strategy
machine / ScrollCoordinator).

Landing sequence (docs/todo/scroll-and-paged-table-actions.md): this package is
built over three PRs so the action dispatcher never becomes a monolith:

  * PR1 — `results` (ScrollResult/Reason/Strategy) + `observer` +
          `scroll_and_verify`: the "dispatch vs. moved" contract + verify
          ownership.
  * PR2 — `policy` (ScrollPolicy/scroll_with_policy) + `state_machine`
          (Step/transition/NavigateContext): bounded strategy chain.
  * PR3 — `detect`/`controller`/`verifier` (PageDetector/PageController/
          PageVerifier + ScrollCoordinator): structured-content abstraction.
  * P0-1 — `actions` (PlannedAction/DispatcherAdapter): the Receipt/Command
          separation; the adapter is the only bridge to the real dispatcher.

All modules are pure (no MCP transport); they compose `dispatch`/`verify`
callables injected by the caller (server.py tools, the test suite, or the
edr-rag pipeline).
"""

from __future__ import annotations

from .results import Reason, ScrollResult, Strategy
from .observer import Diff, DiffStrategy, Observer
from .policy import ScrollPolicy, scroll_with_policy
from .actions import DispatcherAdapter, PlannedAction
from .executor import ScrollExecutor
from .controller import NavigationMode, PageController
from .detect import (
    DetectedControl,
    DetectionResult,
    PageChange,
    PageDetector,
    PageStructure,
    structure_to_strategy,
)
from .verifier import ScrollCoordinator, PageVerifier
from .backend import (
    BackendUnavailableError,
    ScrollBackendSource,
    run_scroll_region,
)
from .state_machine import (
    Step,
    NavigateContext,
    transition,
    run,
    machine_step_fn,
)
from .pointer_result import (
    CODE_NO_EFFECT,
    CODE_NOT_DISPATCHED,
    CODE_OK,
    CODE_POINT_OUTSIDE_WINDOW,
    CODE_TARGET_AMBIGUOUS,
    CODE_TARGET_NOT_FOUND,
    CODE_TARGET_OCCLUDED,
    CODE_VERIFICATION_UNAVAILABLE,
    STABLE_POINTER_CODES,
    is_verified_success,
    normalize as normalize_pointer_result,
    reason_to_code,
)

__all__ = [
    "Reason",
    "Strategy",
    "ScrollResult",
    "Diff",
    "DiffStrategy",
    "Observer",
    "ScrollPolicy",
    "scroll_with_policy",
    "PlannedAction",
    "DispatcherAdapter",
    "ScrollExecutor",
    "NavigationMode",
    "PageStructure",
    "PageChange",
    "DetectedControl",
    "DetectionResult",
    "PageDetector",
    "structure_to_strategy",
    "PageController",
    "PageVerifier",
    "ScrollCoordinator",
    "ScrollBackendSource",
    "BackendUnavailableError",
    "run_scroll_region",
    "Step",
    "NavigateContext",
    "transition",
    "run",
    "machine_step_fn",
    "CODE_OK",
    "CODE_TARGET_OCCLUDED",
    "CODE_TARGET_AMBIGUOUS",
    "CODE_VERIFICATION_UNAVAILABLE",
    "CODE_NO_EFFECT",
    "CODE_NOT_DISPATCHED",
    "CODE_TARGET_NOT_FOUND",
    "CODE_POINT_OUTSIDE_WINDOW",
    "STABLE_POINTER_CODES",
    "normalize_pointer_result",
    "reason_to_code",
    "is_verified_success",
]
