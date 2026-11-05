"""S4 步骤结果门(09 §7.3 步骤5/§7.5/§10)。

evaluate_step_result 把窗口效果、动作 receipt 与业务断言**分列**评估:
- 任一 fail → 步骤 fail(窗口通过不能替代业务断言;§6.5/§10);
- receipt unknown → 步骤 unknown(不重复点击、不偷推进);
- cleanup 是显式映射步骤:可验证才执行(pass),无法验证 skipped 并
  记录,永不自动找替代窗口(§7.5);
- gate_next_step:仅 pass 允许推进(fail/unknown/skipped 阻断)。
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = ["evaluate_step_result", "gate_next_step"]


def _cleanup_outcome(cleanup: Mapping[str, Any] | None) -> dict | None:
    if cleanup is None:
        return None
    verifiable = cleanup.get("verifiable") is True
    binding_verified = cleanup.get("bindingVerified") is True
    if verifiable and binding_verified:
        outcome = "pass"
    else:
        # 无法验证:跳过并记录;从不尝试兄弟窗口顶替。
        outcome = "skipped"
    return {"outcome": outcome,
            "windowId": cleanup.get("windowId"),
            "attemptedSibling": False,
            "recorded": True}


def evaluate_step_result(*, window_effect: Mapping[str, Any] | None,
                         business_assertions: list | None,
                         receipt: Mapping[str, Any] | None,
                         cleanup: Mapping[str, Any] | None = None) -> dict:
    receipt_status = (receipt or {}).get("status")
    window_ok = bool((window_effect or {}).get("ok"))
    assertion_results = [
        {"assertionId": item.get("assertionId"),
         "result": item.get("result")}
        for item in (business_assertions or [])
    ]
    # §7.6/§10: a required business assertion advances ONLY on a strict
    # pass. unknown / skipped / missing / illegal results are NOT pass —
    # a non-pass assertion never upgrades the step to pass (review 2112:
    # only result=="fail" was treated as failing, letting unknown/skipped/
    # None slip through as pass).
    business_failed = any(item.get("result") == "fail"
                          for item in assertion_results)
    business_nonpass = any(item.get("result") != "pass"
                           for item in assertion_results)
    if receipt_status == "unknown" or (receipt or {}).get("ok") is not True:
        outcome = "unknown"
    elif not window_ok or business_failed:
        outcome = "fail"
    elif business_nonpass:
        # The action and window effect succeeded, but a required
        # assertion is unknown/skipped/malformed: the step's overall
        # state cannot be claimed as pass — record it as unknown
        # (post-state not fully established), never as success.
        outcome = "unknown"
    else:
        outcome = "pass"
    result = {
        "outcome": outcome,
        "windowEffect": dict(window_effect or {}),
        "businessAssertions": assertion_results,
        "receipt": dict(receipt or {}),
    }
    cleaned = _cleanup_outcome(cleanup)
    if cleaned is not None:
        result["cleanup"] = cleaned
    return result


def gate_next_step(result: Mapping[str, Any]) -> bool:
    """仅 outcome=pass 允许提交 sequence 游标并进入下一步。"""
    return result.get("outcome") == "pass"
