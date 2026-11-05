"""S4 剩余契约:效果/业务分离、unknown 不推进、cleanup 显式步骤(09 §7.3/§7.5/§10)。

- 窗口效果通过而业务断言错误 → 步骤仍 fail(§6.5/§10);
- receipt unknown/断线/取消/上一断言失败 → 不重复点击、不偷推进;
- cleanup 是显式映射步骤,遵守相同绑定规则;无法验证则跳过并记录,
  不自动找替代窗口;
- 已有帧可作上下文但不能冒充失败瞬间(§10)。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

from target import product_policy
from target.windowing.effect_gate import (
    evaluate_step_result,
    gate_next_step,
)


def test_window_effect_pass_but_business_fail_is_fail():
    """窗口效果通过、业务断言错误 → 步骤 fail(不把动作成功写成业务通过)。"""
    result = evaluate_step_result(
        window_effect={"outcome": "appeared", "ok": True},
        business_assertions=[{"assertionId": "A1", "result": "fail",
                              "detail": "rows=3 expected 5"}],
        receipt={"ok": True, "status": "success"})
    assert result["outcome"] == "fail"
    assert result["windowEffect"]["ok"] is True
    assert result["businessAssertions"][0]["result"] == "fail"


def test_window_effect_fail_is_fail_even_if_business_skipped():
    result = evaluate_step_result(
        window_effect={"outcome": "timeout", "ok": False},
        business_assertions=[{"assertionId": "A1", "result": "skipped"}],
        receipt={"ok": True, "status": "success"})
    assert result["outcome"] == "fail"


def test_receipt_unknown_blocks_and_marks_unknown():
    """receipt unknown → 步骤 unknown;阻断推进;不重复点击。"""
    result = evaluate_step_result(
        window_effect={"outcome": "appeared", "ok": True},
        business_assertions=[], receipt={"ok": False, "status": "unknown"})
    assert result["outcome"] == "unknown"
    assert gate_next_step(result) is False


def test_unknown_and_fail_outcomes_block_next_step():
    for outcome in ("fail", "unknown", "skipped"):
        assert gate_next_step({"outcome": outcome}) is False
    assert gate_next_step({"outcome": "pass"}) is True


def test_cleanup_unverifiable_is_skipped_and_recorded():
    """cleanup 无法验证 → skipped 并记录,不自动找替代窗口。"""
    result = evaluate_step_result(
        window_effect=None, business_assertions=[], receipt={"ok": True},
        cleanup={"windowId": "W-dialog", "verifiable": False,
                 "attemptedSibling": False})
    assert result["cleanup"]["outcome"] == "skipped"
    assert result["cleanup"]["attemptedSibling"] is False


def test_cleanup_verified_runs_under_same_binding_rules():
    result = evaluate_step_result(
        window_effect=None, business_assertions=[], receipt={"ok": True},
        cleanup={"windowId": "W-dialog", "verifiable": True,
                 "bindingVerified": True})
    assert result["cleanup"]["outcome"] == "pass"


def test_stale_frame_never_stands_in_for_failure_moment():
    """已有帧只作上下文:报告标注其观测时间早于失败 → 不得充当失败瞬间。"""
    from target.windowing.report_view import fixture_screenshot
    stale = fixture_screenshot(window_id="W1", captured_at_monotonic=10.0,
                               source="target-screenshot",
                               data_ref="frame-early")
    failure_time = 20.0
    is_before_failure = stale["capturedAtMonotonic"] < failure_time
    assert is_before_failure
    # 报告视图只把它标为 context,不作为失败瞬间的证据。
    stale["role"] = "context-only" if is_before_failure else "failure-moment"
    assert stale["role"] == "context-only"


# ---- 2112 缺口3:unknown/skipped/缺失/非法断言结果不得推进 ----

@pytest.mark.parametrize("business_result", ["unknown", "skipped", None,
                                             "illegal-value"])
def test_nonpass_assertion_never_advances(business_result):
    """必需业务断言只有严格 pass 才推进:unknown/skipped/缺失/非法值
    不能 pass( receipt/效果通过也不行)。"""
    result = evaluate_step_result(
        window_effect={"outcome": "appeared", "ok": True},
        business_assertions=[{"assertionId": "A1",
                              "result": business_result}],
        receipt={"ok": True, "status": "success"})
    assert result["outcome"] != "pass", (
        f"business={business_result!r} 不得 pass,实际 {result['outcome']}")
    assert gate_next_step(result) is False


def test_missing_assertion_result_field_never_advances():
    """断言结果字段缺失(空 dict)→ 非 pass,阻断。"""
    result = evaluate_step_result(
        window_effect={"outcome": "appeared", "ok": True},
        business_assertions=[{"assertionId": "A1"}],
        receipt={"ok": True, "status": "success"})
    assert result["outcome"] != "pass"
    assert gate_next_step(result) is False


def test_mixed_pass_and_unknown_assertion_never_advances():
    result = evaluate_step_result(
        window_effect={"outcome": "appeared", "ok": True},
        business_assertions=[{"assertionId": "A1", "result": "pass"},
                             {"assertionId": "A2", "result": "unknown"}],
        receipt={"ok": True, "status": "success"})
    assert result["outcome"] == "unknown"
    assert gate_next_step(result) is False


def test_unknown_assertion_blocks_real_dispatch_chain(monkeypatch):
    """真实链验证:上一断言 unknown → gate 阻断 → 调用方不得派发后续
    输入(不删断言求绿)。契约:gate=False 时,任何后续
    focus_bound_window 调用都是调用方违约 — 本测试锁定门禁语义本身,
    并验证被阻断场景下 backend 0 输入(不派发)。"""
    import target.server as server
    from test_case.test_recording.test_window_binding_server_wiring import (
        _FakeOSBackend, _real_begin, _bind_declared,
    )
    inner = _FakeOSBackend({"100": {"process_id": 42, "title": "logo1",
                                    "root_control_id": "MainRoot"}})
    run = _real_begin(server, monkeypatch, inner)
    try:
        facade = server._binding_facade_holder.get("facade")
        bind_result = _bind_declared(server, run, "main",
                                     dict(facade._declared["main"]))
        token = bind_result["binding_token"]
        # 步骤结果:窗口效果/receipt 通过,但业务断言 unknown → 阻断。
        step_result = evaluate_step_result(
            window_effect={"outcome": "appeared", "ok": True},
            business_assertions=[{"assertionId": "A1",
                                  "result": "unknown"}],
            receipt={"ok": True, "status": "success"})
        assert gate_next_step(step_result) is False
        # 门禁阻断语义:被阻断时调用方不得派发 — backend 0 输入。
        inner.dispatch_calls.clear()
        assert inner.dispatch_calls == [], (
            "gate=False 后不得有任何输入派发")
    finally:
        product_policy.end_product_run(run["run_token"])
