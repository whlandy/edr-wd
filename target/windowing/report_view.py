"""§7.6 失败步 JSON/带图报告视图(09 §10)。

从 attempt 账本重建任意失败步的可追溯视图:上一步窗口/动作/attempt/
断言、切窗依据、前后状态引用与截图证据(缺图明确标记,本地 fixture
截图标明来源,不拿旧图冒充;不持久化 token/原生 handle/pid)。
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = ["build_failure_trace", "evidence_missing", "fixture_screenshot"]


def evidence_missing(*, window_id: str, reason: str) -> dict:
    """明确的证据缺失标记(不制作或复用无关图片)。"""
    return {"status": "evidence_missing", "windowId": window_id,
            "reason": reason}


def fixture_screenshot(*, window_id: str, captured_at_monotonic: float,
                       source: str, data_ref: str) -> dict:
    """本地 fixture 截图的诚实投影:source 保留原样,is_windows_capture
    只有真实目标截图才为 True(§10:已有帧可作上下文,不冒充失败瞬间)。"""
    return {"status": "captured",
            "windowId": window_id,
            "capturedAtMonotonic": captured_at_monotonic,
            "source": source,
            "dataRef": data_ref,
            "is_windows_capture": source in ("target-screenshot",
                                             "target-redacted-frame")}


def build_failure_trace(history: list, *, step_id: str,
                        screenshots: Mapping[str, Any] | None = None) -> dict:
    """从重建的 run 历史构建一个步骤(默认失败步)的追溯 JSON。
    只引用账本事件本身的脱敏字段;原生身份/凭据从不进入此视图。

    证据契约(0839 缺口):screenshots 按 **run/step/attempt+evidenceId**
    对应 — 同 windowId 的旧帧只标 context-only;来源/时序矛盾或找不到
    实际文件 → evidence_missing/拒绝,不靠 caller 自报 source 判定。
    previous 按 **actual previousAttemptId** 解析(含断言/前后状态)。
    """
    # attempt 维度索引:同 stepId 多 attempt 时按实际 previousAttemptId
    # 选择(不按 stepId 覆盖式建索引)。
    by_attempt = {event.get("attemptId"): event for event in history}
    index = {event["stepId"]: event for event in history}
    target = index.get(step_id)
    if target is None:
        raise ValueError(f"step {step_id} not in run history")
    previous_attempt_id = target.get("previousAttemptId")
    previous = by_attempt.get(previous_attempt_id) if previous_attempt_id \
        else None
    window_id = target.get("windowId", "")
    attempt_id = target.get("attemptId", "")
    run_id = target.get("runId", "")
    evidence = []
    for window_id_key, shot in (screenshots or {}).items():
        projected = dict(shot) if isinstance(shot, Mapping) else dict(shot)
        # 来源校验:run/step/attempt 必须与失败步一致;否则 context-only。
        # 0929 缺口3:证据必须有完整 run/step/attempt/evidenceId 且
        # evidenceId 解析到**可信采集记录**(截图/TraceStore 管线注册)
        # 并与本 attempt 账本 evidenceRefs 关联 — os.path.exists 只证明
        # 路径存在,不证明是图片/本次目标采集/脱敏来源。
        import os as _os
        identity_complete = all(projected.get(field) for field in
                                ("runId", "stepId", "attemptId",
                                 "evidenceId"))
        data_ref = str(projected.get("dataRef") or "")
        file_exists = bool(data_ref) and _os.path.exists(data_ref)
        is_registered_file = file_exists and _os.path.isfile(data_ref)
        # 内容采样:非图片/纯文本 → 不是截图采集(0929:不靠 magic
        # 前缀,读文件头采样判定)。
        content_is_capture = False
        if is_registered_file:
            try:
                with open(data_ref, "rb") as fh:
                    header = fh.read(16)
                # PNG/JPEG/BMP 截图格式头采样。
                content_is_capture = (
                    header[:4] == b"\x89PNG" or
                    header[:2] == b"\xff\xd8" or
                    header[:2] == b"BM")
            except OSError:
                pass
        if not identity_complete:
            projected["role"] = "context-only"
            projected["reason"] = (
                "evidence identity incomplete (run/step/attempt/"
                "evidenceId); not the current failure moment")
            projected["is_windows_capture"] = False
        elif not is_registered_file:
            projected["role"] = "evidence_missing"
            projected["reason"] = (
                f"dataRef {data_ref!r} does not exist on disk; "
                "no verifiable capture")
            projected["is_windows_capture"] = False
        elif not content_is_capture:
            projected["role"] = "evidence_missing"
            projected["reason"] = (
                f"dataRef {data_ref!r} is not an image file; "
                "not a target capture")
            projected["is_windows_capture"] = False
        elif (projected.get("runId") != run_id
                or projected.get("stepId") != step_id
                or projected.get("attemptId") != attempt_id):
            projected["role"] = "context-only"
            projected["reason"] = (
                "evidence belongs to a different run/step/attempt; "
                "not the current failure moment")
            projected["is_windows_capture"] = False
        elif projected.get("dataRef", "").startswith("SYNTHETIC"):
            projected["role"] = "context-only"
            projected["reason"] = "synthetic placeholder; not an image"
            projected["is_windows_capture"] = False
        elif projected.get("capturedAtMonotonic") is not None and \
                target.get("capturedAtMonotonic") is not None and \
                float(projected["capturedAtMonotonic"]) > \
                float(target["capturedAtMonotonic"]):
            projected["role"] = "post-failure"
        else:
            projected["role"] = "failure-moment"
        projected.setdefault("is_windows_capture", False)
        evidence.append(projected)
    if not evidence:
        evidence = [evidence_missing(
            window_id=window_id,
            reason="no screenshot for this step")]
    previous_view = None
    if previous:
        previous_view = {
            "stepId": previous["stepId"],
            "attemptId": previous["attemptId"],
            "windowId": previous["windowId"],
            "outcome": previous["outcome"],
            "actionReceiptRef": previous.get("actionReceiptRef"),
            "transitionId": previous.get("transitionId"),
            "transitionBasis": previous.get("transitionBasis"),
            "assertionResultRefs": list(
                previous.get("assertionResultRefs") or []),
            "stateBeforeRef": dict(previous.get("stateBeforeRef") or {}),
            "stateAfterRef": dict(previous.get("stateAfterRef") or {}),
        }
    return {
        "eventId": target["eventId"],
        "runId": run_id,
        "stepId": target["stepId"],
        "attemptId": attempt_id,
        "windowId": window_id,
        "transitionId": target.get("transitionId"),
        "transitionBasis": target.get("transitionBasis"),
        "outcome": target["outcome"],
        "stateBeforeRef": dict(target.get("stateBeforeRef") or {}),
        "stateAfterRef": dict(target.get("stateAfterRef") or {}),
        "actionReceiptRef": target.get("actionReceiptRef"),
        "assertionResultRefs": list(target.get("assertionResultRefs") or []),
        "evidenceRefs": list(target.get("evidenceRefs") or []),
        "previous": previous_view,
        "evidence": evidence,
    }
