"""
Dispatch-contract test — the scroll executor chain talks to the REAL
`action_dispatcher.dispatch` with a real-signature agreement.

(Review P2 "测试偏 unit，没有覆盖真实链路" + "adapter 集成测试";
Review P1 #5 "test_scroll_integration 容易误导，应为 contract test".)

Unit tests inject a fake dispatcher. Here we prove the shape contract against
the repository's actual `action_dispatcher.dispatch(action_id, *, ...)`:

  PlannedAction --(DispatcherAdapter default)--> real dispatch() --> ActionReceipt

No TypeError means the `PlannedAction.dispatch_kwargs()` shape exactly matches
the real dispatcher signature — the precise thing P0 #2 flagged as broken.

This is a CONTRACT test, not an end-to-end test: no real Windows backend is
configured here, so the real dispatcher returns a stable error-code receipt
(dispatch_target_missing / missing_selector_hint) for backend-bound actions —
proving argument-shape compatibility without a live GUI. A genuine E2E test
(real backend click) lives separately in test_scroll_e2e.
"""

from __future__ import annotations

from action_dispatcher.receipts import STABLE_DISPATCH_CODES, ActionReceipt
from scroll.actions import DispatcherAdapter, PlannedAction


def test_default_adapter_calls_real_dispatch_shape():
    """The default adapter calls the real dispatch() and returns a genuine
    ActionReceipt with a stable code — proving kwarg-shape compatibility."""
    adapter = DispatcherAdapter()  # default -> real action_dispatcher.dispatch
    r = adapter.execute(PlannedAction(
        action_id="gui.click", action_code="A020", args={},
        target_ref={"target_id": "T0001"},
    ))
    assert isinstance(r, ActionReceipt)
    assert r.code in STABLE_DISPATCH_CODES  # well-formed, never a crash


def test_default_adapter_wheel_returns_receipt():
    adapter = DispatcherAdapter()
    r = adapter.execute(PlannedAction(
        action_id="pointer.scroll", action_code="A029", args={"clicks": -3},
    ))
    assert isinstance(r, ActionReceipt)
    assert r.code in STABLE_DISPATCH_CODES


def test_known_catalog_action_id_resolves_through_dispatcher():
    """gui.click / pointer.scroll are real catalog ids; the dispatcher routes
    them (to a backend-or-null target), never 'unknown_action_id'."""
    adapter = DispatcherAdapter()
    for aid, code, args in [
        ("gui.click", "A020", {}),
        ("pointer.scroll", "A029", {"clicks": -3}),
    ]:
        r = adapter.execute(PlannedAction(action_id=aid, action_code=code,
                                          args=args))
        # unknown_action_id would mean the catalog lookup failed.
        assert r.code != "unknown_action_id", (aid, r.code)


def test_unknown_action_id_through_real_dispatcher():
    """A bogus action_id surfaces as unknown_action_id from the real
    dispatcher — the adapter passes it through untouched."""
    adapter = DispatcherAdapter()
    r = adapter.execute(PlannedAction(action_id="nope.not_real", args={}))
    assert isinstance(r, ActionReceipt)
    assert r.code == "unknown_action_id"


def test_executor_wraps_real_dispatch():
    from scroll.executor import ScrollExecutor

    executor = ScrollExecutor()  # default adapter -> real dispatch
    plan = [
        PlannedAction(action_id="pointer.scroll", action_code="A029",
                      args={"clicks": -3}),
    ]
    receipts = executor.execute_plan(plan)
    assert len(receipts) == 1
    assert isinstance(receipts[0], ActionReceipt)
    assert receipts[0].code in STABLE_DISPATCH_CODES
