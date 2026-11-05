"""§7.6 可回溯步骤状态:attempt 账本(append-only 持久历史)。

契约(09 §7.6;用户验收要求):
- attempt 记录 runId/stepId/attemptId/previousStepId/previousAttemptId/
  windowId/previousWindowId/transitionId/transitionBasis/stateBeforeRef/
  stateAfterRef/actionReceiptRef/assertionResultRefs/evidenceRefs/outcome;
- outcome ∈ pass/fail/unknown/skipped;unknown 不得推进为成功;
- 脱敏:原生 handle/pid/租约 token 不进入持久状态(状态记录只含序列
  游标、绑定修订号、可见性/效果结果与观测引用);
- append-only + 稳定事件 ID(每事件一份 JSON,追加后不可改);
- 重启后从落盘记录重建执行历史;
- 写盘失败先于动作 → 该 run 禁止后续副作用;动作后写失败 → 该 attempt
  标记 unknown(不确定边界,不声称动作未执行);
- 上一结果不允许推进时,下一步 begin 被拒绝(不能靠重新聚焦绕过失败)。

本模块不做 GUI 动作、不做自动恢复、不重试未知动作;只提供账本与门禁。
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from pathlib import Path
from typing import Any, Mapping

__all__ = ["AttemptLedger"]

_OUTCOMES = ("pass", "fail", "unknown", "skipped")
# 持久状态禁止字段(原生身份/凭据);ref 字符串可以指向原生事实,
# 但原生值本身不落盘。
_FORBIDDEN_STATE_KEYS = {"handle", "native_id", "nativeid", "pid",
                         "process_id", "processid", "hwnd", "run_token",
                         "runtoken", "binding_token", "bindingtoken",
                         "lease_token", "leasetoken", "token"}
_EVENT_ID_RE = re.compile(r"^EVT-[0-9a-f]{32}$")


def _redact_state(state: Any, where: str) -> Any:
    """Recursively validate a durable state tree (§7.6): native identity
    and credential fields are forbidden at ANY depth — dict values, list
    items and nested structures are all walked (review 2112: the previous
    top-level-only check let nested secrets persist)."""
    if state is None:
        return {}
    if isinstance(state, Mapping):
        cleaned = {}
        for key, value in state.items():
            if str(key).lower() in _FORBIDDEN_STATE_KEYS:
                raise ValueError(
                    f"{where}: forbidden native identity field {key!r} in "
                    "persisted state (§7.6: handle/pid/lease token never "
                    "enter durable state at any depth; reference them by "
                    "ref instead)")
            cleaned[str(key)] = _redact_state(value, f"{where}.{key}")
        return cleaned
    if isinstance(state, (list, tuple)):
        return [_redact_state(item, f"{where}[{i}]")
                for i, item in enumerate(state)]
    return state


class AttemptLedger:
    """Append-only attempt history with stable event IDs, redacted
    durable state and advance gating (§7.6)."""

    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # 测试注入的存储故障(模拟磁盘故障);None = 正常。
        self._fail_storage = False
        # 存储故障锁存(§7.6 写入失败停止后续副作用):任一次预写/
        # 动作后写失败即置位 — 即使磁盘瞬时恢复,本 run 的 GUI 副作用
        # 全部停止(不确定边界不可清除;不自动恢复/不重试未知动作)。
        self._storage_failed = False

    def _assert_storage_gate(self):
        """写盘故障后的副作用门:锁存置位后,任何 begin/complete
        抛出确定边界错误(不是透传原 OSError — 瞬时恢复也不能
        继续执行)。"""
        if self._storage_failed:
            raise OSError(
                "attempt-ledger storage failure latched: all further "
                "GUI side effects for this run are stopped (§7.6); "
                "transient recovery does not clear the boundary")

    # ---- test seam ----

    def fail_storage(self) -> None:
        self._fail_storage = True

    def _write_event(self, event: dict) -> None:
        if self._fail_storage:
            self._storage_failed = True
            raise OSError("simulated storage failure")
        path = self.root / f"{event['eventId']}.json"
        if path.exists():
            raise ValueError(
                f"append-only violation: event {event['eventId']} "
                "already exists")
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(event, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(path)
        except OSError:
            # 真实 IO 异常(2013):底层写盘失败即置位存储锁存 —
            # 即使磁盘瞬时恢复,本 run 的 GUI 副作用全部停止
            # (不确定边界不可清除;不自动恢复/不重试未知动作)。
            self._storage_failed = True
            raise

    # ---- public protocol ----

    def begin_attempt(self, *, run_id: str, step_id: str, attempt_id: str,
                      window_id: str, transition: str,
                      transition_basis: str,
                      previous: Mapping | None = None,
                      authorized_cleanup: bool = False) -> dict:
        """Start an attempt. Refuses when the previous outcome does not
        allow advancing (fail/unknown/skipped) — a next step may not
        bypass a failed predecessor by re-focusing.

        ``authorized_cleanup``(§7.5):显式授权的清理步与主流程前一步
        的关系是"清理触发关联",不是 sequence 推进 — 主流程 fail
        正是运行授权 cleanup 的前提。previous 的稳定关联仍持久
        (run/step/attempt 引用),跨 run 拒绝保持。"""
        if self._fail_storage:
            self._storage_failed = True
            raise OSError("simulated storage failure")
        self._assert_storage_gate()
        previous_data = None
        if previous is not None:
            previous_data = dict(previous)
            prior_outcome = previous_data.get("outcome")
            if not authorized_cleanup and not self.can_advance(prior_outcome):
                raise ValueError(
                    f"previous attempt outcome {prior_outcome!r} does not "
                    "allow advancing; a failed/unknown predecessor blocks "
                    "the next step (§7.6)")
            if previous_data.get("runId") != run_id:
                raise ValueError(
                    "previous attempt belongs to another run; cross-run "
                    "state reuse is forbidden (§7.6)")
        event = {
            "eventId": "EVT-" + uuid.uuid4().hex,
            "kind": "attempt_started",
            # 可检测的持久化顺序(§7.6:游标提交与状态引用按可检测顺序
            # 持久化);单调计数,重建历史按此排序(glob 文件名随机)。
            "sequence": self._next_sequence(),
            "runId": run_id,
            "stepId": step_id,
            "attemptId": attempt_id,
            "windowId": window_id,
            "transitionId": transition,
            "transitionBasis": transition_basis,
            "previousStepId": (previous_data or {}).get("stepId"),
            "previousAttemptId": (previous_data or {}).get("attemptId"),
            "previousWindowId": (previous_data or {}).get("windowId"),
            "outcome": "unknown",
        }
        with self._lock:
            try:
                self._write_event(event)
            except OSError:
                # 真实写盘 IO 异常(2013):任何 begin 路径的写失败都
                # 置位存储锁存 — 磁盘恢复不清除边界(全 run 停止 GUI;
                # 子类覆写 _write_event 的注入同样捕获)。
                self._storage_failed = True
                raise
        return event

    def complete_attempt(self, event_id: str, *, outcome: str,
                         state_before: Mapping | None,
                         state_after: Mapping | None,
                         action_receipt_ref: str | None,
                         assertion_refs: list | None,
                         evidence_refs: list | None,
                         receipt_payload: Mapping | None = None) -> dict:
        """Complete an attempt (append-only completion event). Any storage
        failure AFTER the action marks the attempt unknown — the action
        may have executed, so it is never reported as not-executed."""
        if self._fail_storage:
            self._storage_failed = True
            raise OSError("simulated storage failure")
        self._assert_storage_gate()
        if outcome not in _OUTCOMES:
            raise ValueError(
                f"outcome must be one of {_OUTCOMES}, got {outcome!r}")
        started = self.load_event(event_id)
        if started.get("kind") != "attempt_started":
            raise ValueError(f"{event_id}: not an open attempt")
        for existing in self._events_of(started["runId"]):
            if existing.get("of") == event_id and existing["kind"] in (
                    "attempt_completed", "attempt_boundary_unknown"):
                raise ValueError(
                    f"append-only violation: attempt {event_id} already "
                    f"completed ({existing['eventId']})")
        # 0639 缺口3:assertion/evidence refs 与 receipt 引用**全树**
        # 递归校验(2112:普通持久事件入口统一严格校验 — 不只检查
        # state);失败诊断不含敏感原值。
        for label, payload in (
                ("assertion_refs", list(assertion_refs or [])),
                ("evidence_refs", list(evidence_refs or []))):
            _redact_state(payload, label)
        event = {
            "eventId": "EVT-" + uuid.uuid4().hex,
            "kind": "attempt_completed",
            "of": event_id,
            "sequence": self._next_sequence(),
            "runId": started["runId"],
            "stepId": started["stepId"],
            "attemptId": started["attemptId"],
            "windowId": started["windowId"],
            "transitionId": started["transitionId"],
            "transitionBasis": started["transitionBasis"],
            "previousStepId": started["previousStepId"],
            "previousAttemptId": started["previousAttemptId"],
            "previousWindowId": started["previousWindowId"],
            "stateBeforeRef": _redact_state(state_before, "stateBefore"),
            "stateAfterRef": _redact_state(state_after, "stateAfter"),
            "actionReceiptRef": action_receipt_ref,
            "receiptPayload": (_redact_state(receipt_payload, "receipt")
                               if receipt_payload else None),
            "assertionResultRefs": list(assertion_refs or []),
            "evidenceRefs": list(evidence_refs or []),
            "outcome": outcome,
        }
        with self._lock:
            try:
                self._write_event(event)
            except OSError:
                # 真实写盘 IO 异常(2013):置位存储锁存(磁盘恢复不
                # 清除边界;全 run 停止 GUI)。
                self._storage_failed = True
                # 动作后写盘失败:落一个明确的 unknown 边界标记(若这也
                # 失败则保留原异常 —— 下次重启 rebuild 时按缺失 completion
                # 的 started 事件推断 unknown)。
                try:
                    boundary = {
                        "eventId": "EVT-" + uuid.uuid4().hex,
                        "kind": "attempt_boundary_unknown",
                        "of": event_id,
                        "runId": started["runId"],
                        "stepId": started["stepId"],
                        "attemptId": started["attemptId"],
                        "windowId": started["windowId"],
                        "transitionId": started["transitionId"],
                        "transitionBasis": started["transitionBasis"],
                        "previousStepId": started["previousStepId"],
                        "previousAttemptId": started["previousAttemptId"],
                        "previousWindowId": started["previousWindowId"],
                        "stateBeforeRef": {},
                        "stateAfterRef": {},
                        "actionReceiptRef": None,
                        "assertionResultRefs": [],
                        "evidenceRefs": [],
                        "outcome": "unknown",
                        "reason": "post_action_write_failed",
                    }
                    self._fail_storage = False
                    try:
                        self._write_event(boundary)
                    finally:
                        self._fail_storage = True
                finally:
                    raise
        return event

    def load_event(self, event_id: str) -> dict:
        path = self.root / f"{event_id}.json"
        if not path.exists():
            raise ValueError(f"unknown event {event_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _events_of(self, run_id: str) -> list:
        events = []
        for path in sorted(self.root.glob("EVT-*.json")):
            try:
                event = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if event.get("runId") == run_id:
                events.append(event)
        return events

    def attach_state_before(self, event_id: str, *,
                            state: Mapping) -> dict:
        """为已 begin 的 attempt 补挂不可变脱敏 before-state(0739:
        begin 时真实前态尚未产生的话,由 step preparation 边界在同
        租约内补挂;append-only 稳定事件)。"""
        started = self.load_event(event_id)
        if started.get("kind") != "attempt_started":
            raise ValueError(f"{event_id}: not an open attempt")
        event = {
            "eventId": "EVT-" + uuid.uuid4().hex,
            "kind": "state_before_attached",
            "of": event_id,
            "sequence": self._next_sequence(),
            "runId": started["runId"],
            "stepId": started["stepId"],
            "attemptId": started["attemptId"],
            "windowId": started["windowId"],
            "stateBeforeRef": _redact_state(state, "stateBefore"),
        }
        with self._lock:
            try:
                self._write_event(event)
            except OSError:
                # 真实写盘 IO 异常(2013):任何 begin 路径的写失败都
                # 置位存储锁存 — 磁盘恢复不清除边界(全 run 停止 GUI;
                # 子类覆写 _write_event 的注入同样捕获)。
                self._storage_failed = True
                raise
        return event

    def _next_sequence(self) -> int:
        """Monotonic persistence order across restarts: max(existing)+1."""
        highest = 0
        if self.root.exists():
            for path in self.root.glob("EVT-*.json"):
                try:
                    event = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                try:
                    highest = max(highest, int(event.get("sequence") or 0))
                except (TypeError, ValueError):
                    continue
        return highest + 1

    def rebuild_history(self, *, run_id: str) -> list:
        """Restart recovery: rebuild the run's attempt history from disk.
        A started attempt with no completion (or an explicit unknown
        boundary) resolves to outcome=unknown — the action may have
        executed; it is never reported as not-executed. Events are ordered
        by their recorded persistence sequence (file names are random)."""
        events = []
        if not self.root.exists():
            return events
        for path in sorted(self.root.glob("EVT-*.json")):
            try:
                event = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if event.get("runId") != run_id:
                continue
            events.append(event)
        events.sort(key=lambda e: (int(e.get("sequence") or 0),
                                   e.get("eventId")))
        by_kind: dict[str, dict] = {}
        completed_of = set()
        for event in events:
            if event["kind"] == "attempt_completed":
                completed_of.add(event["of"])
            elif event["kind"] == "attempt_boundary_unknown":
                completed_of.add(event["of"])
            by_kind.setdefault(event["kind"], event)
        history = []
        for event in events:
            if event["kind"] != "attempt_started":
                continue
            if event["eventId"] in completed_of:
                for candidate in events:
                    if (candidate.get("of") == event["eventId"]
                            and candidate["kind"] in (
                                "attempt_completed",
                                "attempt_boundary_unknown")):
                        # 0809 缺口3:重建时解析附挂的真实前态
                        # (state_before_attached 事件),不丢 begin 时
                        # 持久的事实。
                        for attached in events:
                            if (attached.get("of") == event["eventId"]
                                    and attached.get("kind")
                                    == "state_before_attached"):
                                candidate.setdefault(
                                    "stateBeforeRef",
                                    dict(attached.get(
                                        "stateBeforeRef") or {}))
                                break
                        history.append(candidate)
                        break
            else:
                # started 但无 completion → 不确定边界(动作可能已执行)。
                unknown = dict(event)
                unknown["kind"] = "attempt_boundary_unknown"
                unknown["outcome"] = "unknown"
                unknown["reason"] = "completion_event_missing"
                unknown.setdefault("stateBeforeRef", {})
                unknown.setdefault("stateAfterRef", {})
                unknown.setdefault("actionReceiptRef", None)
                unknown.setdefault("assertionResultRefs", [])
                unknown.setdefault("evidenceRefs", [])
                history.append(unknown)
        return history

    def blocked_for_actions(self, run_id: str) -> bool:
        """True when a pre-action write failure has blocked further side
        effects for this run (§7.6: 写入失败停止后续副作用)."""
        return self._fail_storage

    @staticmethod
    def can_advance(outcome: str | None) -> bool:
        """unknown 不能推进为成功;fail/skipped 同样阻断。"""
        return outcome == "pass"
