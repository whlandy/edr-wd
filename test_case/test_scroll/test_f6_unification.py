"""
f6 — machine drives; ScrollCoordinator executes (single execution loop).

Proves the unification bridge: `run(...)` (the machine) is the DECISION
engine and `ScrollCoordinator.step` (via `machine_step_fn`) is the ONE
execution engine. `NavigateContext.detection` is passed through so the
coordinator reads the machine's cached `DetectionResult` and dispatches
through the REAL dispatcher (default DispatcherAdapter).

  NavigateContext.start(..., detection=result)
      -> run(..., step_fn=machine_step_fn(coordinator, before))
          -> coordinator.step (plan -> real dispatch -> bounded verify)
          -> transition decides retry / advance / terminate
          -> terminal ScrollResult
"""

from __future__ import annotations

from action_dispatcher.receipts import ActionReceipt
from observations.snapshot import build_snapshot
from scroll.actions import DispatcherAdapter
from scroll.controller import NavigationMode, PageController
from scroll.detect import DetectionResult, PageDetector, PageStructure
from scroll.executor import ScrollExecutor
from scroll.policy import ScrollPolicy
from scroll.results import Reason
from scroll.state_machine import NavigateContext, machine_step_fn, run
from scroll.verifier import ScrollCoordinator, PageVerifier


def _snap(targets):
    return build_snapshot(targets=targets, backend="x", host="h",
                          captured_at="2026-08-01T00:00:00Z")


def _win():
    return {"process_name": "X", "pid": 1, "native_window_id": "w",
            "title": "Main", "kind": "window"}


def _btn(title, aid, text):
    return {"process_name": "X", "pid": 1, "native_window_id": "w",
            "title": title, "kind": "control", "control_type": "Button",
            "automation_id": aid, "text": text}


class _RealDispatcher:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    def __call__(self, action_id, *, action_code=None, args=None, target_ref=None):
        self.calls.append(action_id)
        if self.ok:
            return ActionReceipt.from_ok(
                action_id=action_id, action_code=action_code,
                request_id=None, result=args,
            )
        return ActionReceipt.from_error(
            code="backend_error", action_id=action_id,
            action_code=action_code, request_id=None, message="boom",
        )


class _StubObserver:
    def __init__(self, after):
        self.after = after

    def snapshot(self):
        return self.after


def _result():
    return DetectionResult(
        structure=PageStructure.PAGINATED,
        next_controls=(),
        confidence=0.95,
    )


def _coordinator(before, after, real):
    from scroll.detect import DetectedControl
    from observations.models import Target

    owner = DetectedControl(
        target=Target.from_dict({**_btn("next", "nextPageButton", "下一页"),
                                 "target_id": "T0001"}),
        enabled=True,
    )
    result = DetectionResult(
        structure=PageStructure.PAGINATED,
        next_controls=(owner,),
        confidence=0.95,
    )
    return ScrollCoordinator(
        detector=PageDetector(),
        controller=PageController(),
        verifier=PageVerifier(),
        observer=_StubObserver(after),
        executor=ScrollExecutor(DispatcherAdapter(dispatch=real)),
    ), result


def test_machine_drives_coordinator_to_success():
    """f6: machine + coordinator bridge reaches SUCCESS through the REAL
    dispatcher when content moves, and the machine's transition terminates."""
    before = _snap([_win(), _btn("next", "nextPageButton", "下一页")])
    after = _snap([_win(), _btn("next", "nextPageButton", "第二页")])
    real = _RealDispatcher(ok=True)
    coord, result = _coordinator(before, after, real)

    ctx = NavigateContext.start(
        owner="w",
        policy=ScrollPolicy(max_attempts=1),
        detection=result,
    )
    out = run(ctx, dispatch=real, verify=lambda: True,
              step_fn=machine_step_fn(coord, before))
    assert out.reason is Reason.NEXT_PAGE
    assert out.moved and out.success
    # The coordinator dispatched the real action through the dispatcher.
    assert real.calls


def test_machine_detection_is_relayed_to_coordinator():
    """f6: the machine passes NavigateContext.detection to the coordinator's
    step via machine_step_fn (no re-detect; cached result drives the plan)."""
    before = _snap([_win(), _btn("next", "nextPageButton", "下一页")])
    after = _snap([_win(), _btn("next", "nextPageButton", "第二页")])
    real = _RealDispatcher(ok=True)
    coord, result = _coordinator(before, after, real)

    seen = {}

    orig_step = coord.step

    def spy_step(mode, snapshot, policy):
        seen["detection"] = coord._result
        return orig_step(mode, snapshot, policy)

    coord.step = spy_step  # type: ignore[assignment]
    ctx = NavigateContext.start(
        owner="w", policy=ScrollPolicy(max_attempts=1), detection=result,
    )
    run(ctx, dispatch=real, verify=lambda: True,
        step_fn=machine_step_fn(coord, before))
    assert seen["detection"] is result


def test_machine_no_effect_terminates():
    """f6: coordinator NO_SCROLL_EFFECT (no content change) is surfaced by the
    machine as NO_SCROLL_EFFECT after the bounded attempt."""
    before = _snap([_win(), _btn("next", "nextPageButton", "下一页")])
    same = _snap([_win(), _btn("next", "nextPageButton", "下一页")])
    real = _RealDispatcher(ok=True)
    coord, result = _coordinator(before, same, real)

    ctx = NavigateContext.start(
        owner="w", policy=ScrollPolicy(max_attempts=1), detection=result,
    )
    out = run(ctx, dispatch=real, verify=lambda: True,
              step_fn=machine_step_fn(coord, before))
    assert out.moved is False
    assert out.reason is Reason.NO_SCROLL_EFFECT
