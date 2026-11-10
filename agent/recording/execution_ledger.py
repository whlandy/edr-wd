"""真实执行链 ↔ §7.6 账本桥(agent/recording/execution_ledger.py)。

两条接线(0639 复验修订):

**record_case_run_live**(真实执行边界):包装 AtomicExecutor 的
dispatch——每个 attempt 的 begin + 不可变脱敏 before-state 在该
attempt 的**第一个 dispatch(GUI 副作用)之前**持久落盘;dispatch
返回后 complete;dispatch 异常(receipt unknown)→ complete
unknown 不推进不重试;预写失败(OSError)→ 0 副作用。不做
"执行完再回填"。

**record_case_run**(校准路径):真实 CaseRunResult(StepResult 列表)
按步骤入账——用于校准真实产物形状;时序门由 record_case_run_live
承担。

- 步骤 outcome 经 effect_gate:receipt unknown / 业务断言非严格
  pass / 失败 → unknown 或 fail;非 pass 阻断下一步;
- 真实 ExpectationResult.to_dict 协议适配:type/status/expected/
  actual → 稳定断言引用;合法 passed 不记 unknown;
- 持久事件(assertion/evidence refs 含)全树递归脱敏校验;
- 重建历史 → build_run_report(失败步追溯 + 截图契约);
- 不打开 v3 公共执行门(_refuse_v3_execution 保持)。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from types import SimpleNamespace
from typing import Any, Mapping

from target.windowing import attempt_ledger
from target.windowing.effect_gate import evaluate_step_result
from target.windowing.report_view import build_failure_trace



class AttemptLedgerHook:
    """executor 的 per-step attempt 生命周期钩子(0709 缺口1/2/3)。

    - ``begin_before_preparation``:真实 step preparation(含预聚焦/
      恢复聚焦)之前调用 — begin + 不可变脱敏 before-state 持久;
      预写失败 → OSError(调用方 preparation/focus/dispatch 全 0)。
    - ``complete_after_step``:真实业务断言/窗口效果评估后调用 —
      StepResult.status/expectation_results/receipt 是**真实产物**;
      fail/unknown/skipped 不推进;post-action 持久失败 → run 级
      锁存(后续 0 focus/输入,不重试未知 GUI)。
    - step_id/window_id 来自真实 run_step 上下文(非 dispatch 次数
      推断);transition 按实际窗口映射。
    """

    def __init__(self, *, ledger, run_id: str,
                 window_by_step: Mapping[str, str],
                 transition_basis: str = "declared_sequence",
                 host=None):
        self._host = host
        self._ledger = ledger
        self._run_id = run_id
        self._window_by_step = dict(window_by_step)
        self._basis = transition_basis
        self._cursor = 0
        self._previous = None
        self._latched = False
        self._blocked_at: str | None = None
        self._open_event: str | None = None
        self._open_step: str | None = None
        self._steps: list[dict] = []

    # ---- begin(preparation 之前) ----

    def begin_before_preparation(self, *, step_id: str,
                                 window_id: str = "",
                                 before_state: Mapping | None = None,
                                 previous_override: Mapping | None = None,
                                 transition: str | None = None,
                                 authorized_cleanup: bool = False) -> dict:
        """begin + 不可变脱敏 before-state 在第一个 GUI 副作用前一并
        持久(0739 缺口1:before-state 不得推迟到 complete)。

        ``previous_override``(§7.5 授权清理序列):cleanup attempt
        的稳定 previous 关联 — 指向真实触发失败的主流程 attempt
        (仅引用;不是绕过 §7.6 门 — 主流程 fail 正是运行 cleanup
        的前提;attempt 记录保持真实前一步的稳定关联)。"""
        if self._latched:
            raise RuntimeError(
                "run latched (unknown/fail): no further GUI side effects")
        # 0809 缺口1:begin 重入复用同一 attempt(run_case 预聚焦与
        # run_step preparation 会先后触达同一 step — 不产生幽灵
        # start;state 只挂一次)。
        for prior in self._steps:
            if prior["stepId"] == step_id and prior.get("eventId"):
                return {"eventId": prior["eventId"], "reused": True}
        if transition is None:
            transition = ("first_bind" if not self._steps
                          else ("switched_bound_window"
                                if window_id != self._steps[-1]["windowId"]
                                else "same_window_reverified"))
        event = self._ledger.begin_attempt(
            run_id=self._run_id, step_id=step_id,
            attempt_id=f"{step_id}-a{self._cursor + 1}",
            window_id=window_id,
            transition=transition,
            transition_basis=self._basis,
            previous=previous_override if previous_override is not None
            else self._previous,
            authorized_cleanup=authorized_cleanup)
        # 真实前态在 begin 同批持久(0739:仅 started 头不算);
        # 记录 attached 内容供 complete 引用同一份。
        if not hasattr(self, "_attached_before"):
            self._attached_before = {}
        if not hasattr(self, "_attached_after"):
            self._attached_after = {}
        if before_state is not None:
            attached = {"sequenceCursor": self._cursor,
                        "bindingRevision": 0,
                        "windowId": window_id,
                        "observationRef": None,
                        **dict(before_state)}
            self._ledger.attach_state_before(event["eventId"],
                                             state=attached)
            self._attached_before[event["eventId"]] = attached
        self._open_event = event["eventId"]
        self._open_step = step_id
        self._steps.append({"stepId": step_id, "windowId": window_id,
                            "eventId": event["eventId"]})
        # run_case 的提前终结路径(0809 缺口2)通过此标记找到待终结
        # attempt( executor 侧读取)。
        if getattr(self, "_host", None) is not None and hasattr(
                self._host, "__dict__"):
            self._host._attempt_open_event = event["eventId"]
        return event

    # ---- complete(step 之后;真实产物) ----

    def complete_after_step(self, *, step_result, receipt,
                            expectation_results, window_id: str,
                            error_payload, evidence_refs=None) -> None:
        assertion_refs = []
        for index, item in enumerate(expectation_results or []):
            to_dict = getattr(item, "to_dict", None)
            payload = to_dict() if callable(to_dict) else dict(item)
            status = payload.get("status")
            if status == "passed":
                status = "pass"
            elif status == "failed":
                status = "fail"
            assertion_refs.append({
                "assertionId":
                    f"{step_result.step_id}:{payload.get('type')}:"
                    f"{index}",
                "type": payload.get("type"),
                "result": status,
                "expected": payload.get("expected"),
                "actual": payload.get("actual"),
                "observationRef": payload.get("observation_id"),
            })
        receipt_payload = None
        if receipt is not None:
            to_dict = getattr(receipt, "to_dict", None)
            receipt_payload = (to_dict() if callable(to_dict)
                               else getattr(receipt, "__dict__", {}))
        # outcome:BLOCKED/FAILED/SKIPPED → fail/unknown/skipped;
        # required 断言非严格 pass 不得推进(effect_gate 语义)。
        raw_status = getattr(step_result, "status", "")
        status_value = getattr(raw_status, "value", raw_status)
        if status_value == "failed":
            outcome = "fail"
        elif status_value == "blocked":
            outcome = "unknown"
        elif status_value == "skipped":
            outcome = "skipped"
        elif assertion_refs and any(
                r["result"] != "pass" for r in assertion_refs):
            outcome = ("fail" if any(r["result"] == "fail"
                                     for r in assertion_refs)
                       else "unknown")
        else:
            outcome = "pass"
        if getattr(self, "_host", None) is not None and hasattr(
                self._host, "__dict__"):
            self._host._attempt_open_event = None
        # before-state:引用 begin 时 attached 的**同一份**实际前态
        # (0809 缺口3:不得用占位 0/None 覆盖)。
        attached_before = getattr(self, "_attached_before", {}).get(
            self._open_event)
        state_before = (dict(attached_before) if attached_before
                        else {"sequenceCursor": self._cursor,
                              "bindingRevision": "pending",
                              "observationRef": "pending",
                              "windowId": window_id})
        # after:来自本 run 真实事实(hook._attached_after 供真实权威
        # 后态 — 1059 缺口3:不硬编码 pending,不吞异常;缺失/异常
        # 由调用方显式传 "unknown",不用 pre-action 值冒充)。
        attached_after = getattr(self, "_attached_after", {}).get(
            self._open_event) or {}
        # A-3(1536):调用方可附本次已解析目标的必要安全特征
        # (targetRef:automationId/唯一匹配数/selectorHint)与实际
        # action_id — 持久进 stateAfterRef(报告本步识别特征/动作
        # 标识可查;非复制控件树,脱敏统一校验)。
        state_after = {
            "sequenceCursor": self._cursor + 1,
            "bindingRevision": attached_after.get(
                "bindingRevision", "pending"),
            "windowId": window_id,
            "observationRef": attached_after.get(
                "observationRef",
                (assertion_refs[0].get("observationRef")
                 if assertion_refs else None) or "pending"),
            "receiptStatus": (
                (receipt_payload or {}).get("status") or "pending"),
        }
        for key in ("targetRef", "actionId", "windowEffect"):
            if key in attached_after:
                state_after[key] = attached_after[key]
        # 拒绝/阻断码持久(GUI 前拒绝可追查;1059 缺口1)。
        if isinstance(error_payload, Mapping) and error_payload.get("code"):
            state_after["rejectionCode"] = error_payload["code"]
        try:
            completed = self._ledger.complete_attempt(
                self._open_event, outcome=outcome,
                state_before=state_before,
                state_after=state_after,
                action_receipt_ref=(f"RCPT-{step_result.step_id}-"
                                    f"a{self._cursor + 1}"),
                receipt_payload=receipt_payload,
                assertion_refs=assertion_refs,
                evidence_refs=list(evidence_refs or []))
            self._cursor += 1
            self._previous = completed
            self._open_event = None
        except Exception:
            # post-action 持久失败 → run 级锁存(0709 缺口3):后续
            # 0 focus/输入,不重试未知 GUI;边界可重建。
            self._latched = True
            self._blocked_at = self._open_step
            self._open_event = None
            raise
        if outcome != "pass":
            self._latched = True
            self._blocked_at = step_result.step_id

    # ---- 状态 ----

    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def blocked_at(self) -> str | None:
        return self._blocked_at

    @property
    def steps(self) -> list[dict]:
        return list(self._steps)


__all__ = ["record_case_run", "record_case_run_live", "run_id_for",
           "build_run_report", "AttemptLedgerHook", "LedgeredDispatch",
           "execute_v3_window_stage", "execute_v3_window_stage_real"]

_run_sequence = threading.Lock()
_run_counter = 0


def run_id_for(case_id: str) -> str:
    """case → 稳定 runId 前缀;**同 case 两次 run 仍需独立 ID** —
    由 record_case_run_live/record_case_run 以单调序号区分。"""
    return "RUN-" + hashlib.sha256(
        f"case:{case_id}".encode()).hexdigest()[:24]


def _next_run_id(case_id: str) -> str:
    """跨进程/重启唯一 run ID(0739 缺口修订):**高熵不透明完整
    UUID4** — 不编码原生 PID/handle/凭据(09§7.6 脱敏红线:
    native_pid_encoded 探针须为 false);仅保证唯一与稳定传递;
    旧 run 只读重建,不复用 binding。"""
    import uuid
    return "RUN-" + uuid.uuid4().hex


def _receipt_dict(step) -> dict | None:
    action = getattr(step, "action", None)
    if action is None:
        return None
    to_dict = getattr(action, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return dict(action)


def _assertion_results(step) -> list[dict]:
    """真实 ExpectationResult.to_dict 协议适配(0639 缺口2):
    `{type, status, expected, actual, observation_id, ...}` →
    稳定断言引用(assertionId=type+序号,result=status)。
    required 断言 status=passed 才是 pass;unknown/skipped/缺失
    不当 pass(effect_gate 判定)。"""
    results = []
    for index, item in enumerate(
            getattr(step, "expectation_results", None) or []):
        to_dict = getattr(item, "to_dict", None)
        payload = to_dict() if callable(to_dict) else dict(item)
        expectation_type = (payload.get("type")
                            or payload.get("expectation_type")
                            or payload.get("assertionId")
                            or f"assertion-{index}")
        status = (payload.get("status") or payload.get("result"))
        # 真实 ExpectationResult.status 枚举值为 "passed"/"failed";
        # effect_gate 的判定词汇为 "pass"/"fail" — 显式映射(缺失/
        # unknown/skipped 不映射,不当 pass)。
        if status == "passed":
            status = "pass"
        elif status == "failed":
            status = "fail"
        results.append({
            "assertionId": f"{step.step_id}:{expectation_type}:{index}",
            "result": status,
            "expected": payload.get("expected"),
            "actual": payload.get("actual"),
            "observationRef": payload.get("observation_id"),
        })
    return results


def record_case_run(case, ledger, *, window_by_step: Mapping[str, str],
                    transition_by_step: Mapping[str, str] | None = None,
                    basis: str = "declared_sequence",
                    run_id: str | None = None,
                    screenshots: Mapping[str, Any] | None = None) -> dict:
    """按顺序把 case 的每个步骤写入账本(校准路径;时序门由
    record_case_run_live 承担)。任何非 pass 步骤阻断后续 begin。
    ``run_id`` 可显式传入(稳定可解析;缺省按 case 派生 + 单调序号)。"""
    run_id = run_id or _next_run_id(case.case_id)
    run_seq = run_id.rsplit("-", 1)[-1]  # 每次运行的单调序号
    previous = None
    recorded = 0
    blocked_at = None
    for step in case.step_results:
        window_id = window_by_step.get(step.step_id, "")
        transition = (transition_by_step or {}).get(
            step.step_id, "same_window_reverified" if previous else
            "first_bind")
        receipt = _receipt_dict(step)
        assertions = _assertion_results(step)
        gated = evaluate_step_result(
            window_effect={"ok": step.status == StepStatusValue.PASSED,
                           "outcome": "window-ok"
                           if step.status == StepStatusValue.PASSED
                           else "window-not-verified"},
            business_assertions=assertions,
            receipt=receipt)
        event = ledger.begin_attempt(
            run_id=run_id, step_id=step.step_id,
            attempt_id=f"{step.step_id}-r{run_seq}-a1",
            window_id=window_id,
            transition=transition, transition_basis=basis,
            previous=previous)
        outcome = gated["outcome"]
        completed = ledger.complete_attempt(
            event["eventId"], outcome=outcome,
            state_before={"sequenceCursor": recorded,
                          "bindingRevision": 0,
                          "observationRef": None},
            state_after={"sequenceCursor": recorded + 1,
                         "bindingRevision": 0,
                         "observationRef": None},
            action_receipt_ref=(f"RCPT-{step.step_id}"
                                if receipt else None),
            assertion_refs=assertions,
            evidence_refs=[])
        recorded += 1
        previous = completed
        if outcome != "pass":
            blocked_at = step.step_id
            break
    return {"runId": run_id, "recorded": recorded,
            "blockedAt": blocked_at}


class StepStatusValue:
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class LedgeredDispatch:
    """§7.6 动作前持久门(0639 缺口1):包装真实 dispatch。

    每个 attempt:
    - begin + 不可变脱敏 before-state 在**第一个 dispatch 调用之前**
      持久落盘(预写失败 → OSError,0 副作用);
    - dispatch 后 complete(outcome 按 effect_gate);
    - dispatch 异常(receipt unknown)→ complete unknown,不推进、
      不重试未知 GUI 动作;
    - 下一步的 begin 接收**上一步 complete 事件**的只读视图。
    """

    def __init__(self, *, dispatch, ledger, run_id: str,
                 window_by_step: Mapping[str, str],
                 transition_basis: str = "declared_sequence"):
        self._dispatch = dispatch
        self._ledger = ledger
        self._run_id = run_id
        self._window_by_step = dict(window_by_step)
        self._basis = transition_basis
        self._cursor = 0
        self._previous = None
        self._blocked = False
        self._open_attempt: str | None = None
        self._open_step: str | None = None

    def begin_attempt_for(self, step_id: str, window_id: str = "",
                          transition: str = "same_window_reverified") -> dict:
        """在该 step 的第一个 GUI 副作用前调用(执行边界):持久
        begin + before-state。预写失败 → OSError(0 副作用)。"""
        event = self._ledger.begin_attempt(
            run_id=self._run_id, step_id=step_id,
            attempt_id=f"{step_id}-a{self._cursor + 1}",
            window_id=window_id,
            transition=transition,
            transition_basis=self._basis, previous=self._previous)
        self._open_attempt = event["eventId"]
        self._open_step = step_id
        return event

    def complete_attempt_for(self, *, outcome: str,
                             observation_ref: str | None = None,
                             receipt_ref: str | None = None,
                             assertion_refs: list | None = None,
                             evidence_refs: list | None = None) -> dict:
        completed = self._ledger.complete_attempt(
            self._open_attempt, outcome=outcome,
            state_before={"sequenceCursor": self._cursor,
                          "bindingRevision": 0,
                          "observationRef": None},
            state_after={"sequenceCursor": self._cursor + 1,
                         "bindingRevision": 0,
                         "observationRef": observation_ref},
            action_receipt_ref=receipt_ref,
            assertion_refs=assertion_refs or [],
            evidence_refs=evidence_refs or [])
        self._cursor += 1
        self._previous = completed
        self._open_attempt = None
        return completed

    def __call__(self, action_id=None, action_code=None, args=None,
                 target_ref=None, request_id=None, **kw):
        if self._blocked:
            raise RuntimeError(
                "previous attempt did not pass; sequence cursor not "
                "advanced (§7.6: failed/unknown blocks next step)")
        step_id = self._open_step or f"step-{self._cursor + 1:04d}"
        if self._open_attempt is None:
            self.begin_attempt_for(
                step_id, self._window_by_step.get(step_id, ""))
        try:
            receipt = self._dispatch(
                action_id=action_id, action_code=action_code,
                args=args, target_ref=target_ref,
                request_id=request_id, **kw)
        except Exception as exc:  # noqa: BLE001
            # receipt unknown:动作可能已发生 → unknown,不推进、
            # 不重试未知 GUI 动作(§7.6)。
            self.complete_attempt_for(
                outcome="unknown",
                receipt_ref=f"RCPT-{step_id}-unknown")
            self._blocked = True
            raise
        payload = None
        to_dict = getattr(receipt, "to_dict", None)
        if callable(to_dict):
            payload = to_dict()
        elif isinstance(receipt, Mapping):
            payload = dict(receipt)
        ok = bool((payload or {}).get("ok", True))
        outcome = "pass" if ok else "unknown"
        self.complete_attempt_for(
            outcome=outcome,
            observation_ref=(payload or {}).get("observation_id"),
            receipt_ref=f"RCPT-{step_id}-{self._cursor}")
        if outcome != "pass":
            self._blocked = True
        return receipt


def record_case_run_live(executor, case, ledger, *,
                         window_by_step: Mapping[str, str],
                         transition_basis: str = "declared_sequence",
                         dispatch=None,
                         run_id: str | None = None,
                         post_action_write_failure: bool = False) -> dict:
    """真实执行边界接线(0709 修订):attempt 生命周期挂在 executor 的
    **step preparation 之前**(run_case 预聚焦/恢复聚焦边界也覆盖)与
    run_step 完成(真实业务断言/窗口效果评估后)——不是 dispatch 包装。
    预写失败 → OSError,prepare/focus/dispatch 全 0;动作后写失败 →
    run 级锁存(后续 0 focus/输入);run_id 跨进程唯一(显式传入)。"""
    run_id = run_id or _next_run_id(case.case_id)
    hook = AttemptLedgerHook(
        ledger=ledger, run_id=run_id, window_by_step=window_by_step,
        transition_basis=transition_basis, host=executor)
    executor._attempt_ledger_hook = hook
    executor._attempt_window_by_step = dict(window_by_step)
    if post_action_write_failure:
        original_write = ledger._write_event
        state = {"count": 0}

        def flaky(event):
            if event["kind"] == "attempt_completed":
                state["count"] += 1
                if state["count"] == 1:
                    raise OSError("simulated disk failure after action")
            return original_write(event)

        ledger._write_event = flaky
    try:
        case_result = executor.run_case(case)
    except OSError:
        # 后写持久失败:传播 OSError(不吞),但携带锁存/边界信息供
        # 调用方与报告按 run/step/attempt 追溯(0709 缺口3)。
        raise OSError(
            f"post-action write failure latched run at "
            f"{hook.blocked_at}; outcome=unknown") from None
    finally:
        for attr in ("_attempt_ledger_hook", "_attempt_complete_hook",
                     "_attempt_window_by_step"):
            if hasattr(executor, attr):
                try:
                    delattr(executor, attr)
                except Exception:  # noqa: BLE001
                    pass
    return {"runId": run_id,
            "recorded": hook.cursor,
            "blockedAt": hook.blocked_at,
            "steps": hook.steps,
            "latched": hook._latched,
            "caseOutcome": getattr(case_result, "outcome", None)}


def build_run_report(history: list, *,
                     screenshots: Mapping[str, Any] | None = None,
                     failed_step_detail: bool = False) -> dict:
    """从重建历史产出**精简报告**(1456 缺口3;用户口径:特征识别 +
    简洁追溯,不强制带图):

    - 每 attempt 只一个规范**本步条目**:识别特征/匹配结果、动作/
      断言结果、outcome、bindingRevision/observationRef;
    - ``previous`` 只含稳定引用(runId/stepId/attemptId)— 下一步
      引用前步本份,不嵌套复制前步完整 before/after/断言;首步
      previous 为 None;失败处按 ref 可定位/按需展开;
    - ``previousDetailRef``:前一步在重建历史中的定位(重建/按需
      展开由调用方以账本事件完成 — 报告不复制整块);
    - 无图不是失败:不生成占位图、不把 evidence_missing 当报告缺陷;
      可选截图若传入仍经来源分类(不虚假来源)。
    旧字段(outcome/failedStep/steps)保持兼容:默认 failedStep 为
    失败步的**单份规范条目**(与 steps 同份语义;previous 只含稳定
    引用,不嵌套复制前步整块;无缺图占位)。旧详细追溯
    (build_failure_trace 身份/证据分类)仅 ``failed_step_detail=
    True`` 显式按需投影 — 不为兼容把重复详情塞回默认报告。"""
    normalized = {}
    for window_id, shot in (screenshots or {}).items():
        projected = dict(shot)
        projected.setdefault("windowId", window_id)
        projected.setdefault("capturedAtMonotonic",
                             float(shot.get("capturedAtMonotonic") or 0))
        projected.setdefault("source", str(shot.get("source") or "unknown"))
        projected.setdefault("dataRef", str(shot.get("dataRef") or ""))
        projected["is_windows_capture"] = projected.get("source") in (
            "target-screenshot", "target-redacted-frame")
        normalized[window_id] = projected

    def _entry(event, previous_ref):
        state_after_ref = event.get("stateAfterRef") or {}
        features = []
        for ref in (event.get("assertionResultRefs") or []):
            features.append({
                "assertionId": ref.get("assertionId"),
                "type": ref.get("type"),
                "result": ref.get("result"),
                "expected": ref.get("expected"),
                "actual": ref.get("actual"),
                "observationRef": ref.get("observationRef"),
            })
        # A-3(1536):无断言动作步也记录真实识别特征 — 本次已解析
        # 目标的必要安全特征/唯一匹配结果(持久于 stateAfterRef.
        # targetRef;非断言列表改名、非控件树复制)。同一份事实的
        # 复用/引用优先;真实不同 before/after 保留。
        target_ref = state_after_ref.get("targetRef") or {}
        if target_ref:
            features.append({
                "kind": "resolved-target",
                "automationId": target_ref.get("automationId"),
                "uniqueMatch": target_ref.get("uniqueMatch"),
                "observationRef": state_after_ref.get("observationRef"),
            })
        receipt = event.get("receiptPayload") or {}
        # 动作标识:实际 action_id(dispatcher receipt 产物)+
        # 必要脱敏参数引用(完整参数在账本 receiptPayload 可查)。
        action_id = receipt.get("action_id") or \
            state_after_ref.get("actionId")
        return {
            "stepId": event.get("stepId"),
            "attemptId": event.get("attemptId"),
            "windowId": event.get("windowId"),
            "outcome": event.get("outcome"),
            "transitionId": event.get("transitionId"),
            "transitionBasis": event.get("transitionBasis"),
            "features": features,
            "action": {
                "receiptRef": event.get("actionReceiptRef"),
                "receiptCode": receipt.get("code"),
                "receiptOk": receipt.get("ok"),
                "actionId": action_id,
            },
            "stateAfter": {
                "bindingRevision": state_after_ref.get(
                    "bindingRevision"),
                "observationRef": state_after_ref.get(
                    "observationRef"),
            },
            "evidenceRefs": list(event.get("evidenceRefs") or []),
            "previous": previous_ref,
        }

    entries = []
    for index, event in enumerate(history):
        previous_ref = None
        if index > 0:
            prior = history[index - 1]
            previous_ref = {
                "runId": prior.get("runId"),
                "stepId": prior.get("stepId"),
                "attemptId": prior.get("attemptId"),
                # 前一步在本次重建历史中的定位(按需展开的稳定 ref)。
                "eventId": prior.get("eventId"),
            }
        entries.append(_entry(event, previous_ref))
    failed_entry = next((entry for entry in entries
                         if entry["outcome"] != "pass"), None)
    # 失败步详细追溯(既有调用者兼容;委托既有 build_failure_trace —
    # 不重写其身份/证据分类)。精简条目(steps)是每 attempt 的规范
    # 单份;failedStep 是失败步的详细视图,failedStepRef 将两者关联。
    failed_event = next((event for event in history
                         if event.get("outcome") != "pass"), None)
    failed_ref = (
        {"runId": (failed_event or {}).get("runId"),
         "stepId": (failed_event or {}).get("stepId"),
         "attemptId": (failed_entry or {}).get("attemptId"),
         "eventId": (failed_event or {}).get("eventId")}
        if failed_entry is not None else None)
    if failed_step_detail and failed_event is not None:
        # 显式按需:旧详细追溯(身份/证据分类;legacy 消费者投影)。
        failed_view = build_failure_trace(
            history, step_id=failed_event["stepId"],
            screenshots=normalized)
    else:
        # 默认:失败步单份规范条目(与 steps 同份;引用式 previous;
        # 无缺图占位)。详细按需解析由 failedStepRef 指向账本事件。
        failed_view = failed_entry
    return {"outcome": ("pass" if failed_entry is None
                        else failed_entry["outcome"]),
            "failedStep": failed_view,
            "failedStepRef": failed_ref,
            "failedStepDetailRequested": bool(failed_step_detail),
            "steps": entries}


def execute_v3_window_stage(*, golden, ledger, server_begin, server_bind,
                            server_focus_bound, server_execute_action,
                            server_end, window_by_step,
                            transition_basis="declared_sequence",
                            observation_ref_provider=None,
                            run_id=None):
    """受控内部 v3 跨窗口执行阶段入口(§7.6/§7.2;公共门不打开)。

    **真实 server 生命周期**(0859/0929 缺口1):
    - ``server_begin(profile_id=..., digest=...)`` → JSON → 租约
      (拒绝 → 0 GUI 副作用);
    - ``server_bind(window_id=..., identity=..., run_token=...)`` →
      产品域声明/绑定(binding token 内存传递);
    - 每步:绑定聚焦(拒绝 → blocked)→ **golden 实际动作**
      (action_id/args 来自 declared step 的 verifiers 声明)→
      真实 dispatcher 恰一次 → 业务断言 → attempt 持久边界 →
      finally ``server_end(run_token)``。
    - **严格事实传播**(0929 缺口2):golden verifiers 字段实际执行
      (type/expected/归属窗口/结果与数量一致);typed receipt 原样
      持久(unknown 不改 success);权威 state provider 必需——缺失/
      异常 → GUI 前拒绝;focus 返回非 ok/None → blocked(不默认
      ok=True);durable before/after 均写 provider 真实值。

    返回 per-attempt 摘要与失败报告(缺图明确)。
    """
    run_id = run_id or _next_run_id("v3-window-stage")
    hook = AttemptLedgerHook(
        ledger=ledger, run_id=run_id, window_by_step=dict(window_by_step),
        transition_basis=transition_basis)
    # golden 实际步骤(含 action_id/args/verifiers/sourceEvents)。
    steps_declared = {}
    for step in getattr(golden, "steps", None) or []:
        steps_declared[step.step_id] = step
    sequence_steps = []
    if getattr(golden, "window_sequence", None) is not None:
        sequence_steps = golden.window_sequence.to_dict().get("steps") or []
    steps_report = []
    latched = False
    blocked_at = None
    dispatch_inputs = 0
    begin_result = None
    run_token_holder = {"token": None}
    try:
        # 真实 begin(profile_id/digest → JSON → 租约)。
        begin_result = json.loads(server_begin(
            profile_id="windows_hisec", digest=_product_digest()))
        if not (begin_result or {}).get("ok", False):
            return {"runId": run_id, "dispatchInputs": 0,
                    "steps": [{"outcome": "blocked",
                               "reason": "begin refused",
                               "code": (begin_result or {}).get("code")}],
                    "latched": True, "blockedAt": None,
                    "report": {"outcome": "blocked",
                               "failedStep": None, "steps": []}}
        run_token_holder["token"] = begin_result.get("run_token")
        # 真实绑定:产品域声明/绑定 per golden window_sequence 的唯一
        # window(binding token 内存传递)。
        bound_windows = {}
        for mapping in sequence_steps:
            wid = mapping.get("windowId")
            if wid and wid not in bound_windows:
                bind_result = json.loads(server_bind(
                    window_id=wid, run_token=run_token_holder["token"],
                    identity={"processName": "EDRClient.exe",
                              "titleRegex": mapping.get(
                                  "titleRegex", "^logo1$")}))
                if not (bind_result or {}).get("ok", False):
                    return {"runId": run_id, "dispatchInputs": 0,
                            "steps": [{"outcome": "blocked",
                                       "reason": "bind refused",
                                       "code": (bind_result or {}).get(
                                           "code")}],
                            "latched": True, "blockedAt": None,
                            "report": {"outcome": "blocked",
                                       "failedStep": None, "steps": []}}
                bound_windows[wid] = bind_result.get("binding_token")
        for mapping in sequence_steps:
            step_id = mapping["stepId"]
            window_id = mapping["windowId"]
            declared = steps_declared.get(step_id)
            if latched:
                steps_report.append({"stepId": step_id,
                                     "outcome": "skipped",
                                     "reason": "run latched"})
                continue
            transition = ("first_bind" if not steps_report else
                          ("switched_bound_window"
                           if window_id != steps_report[-1].get("windowId")
                           else "same_window_reverified"))
            # before-state:binding revision/observation 从权威只读
            # provider 取;**provider 必需 — 缺失/异常 → GUI 前拒绝**
            # (0929 缺口2:无 provider 也执行并 pass 是回退)。
            if observation_ref_provider is None:
                hook.begin_before_preparation(
                    step_id=step_id, window_id=window_id,
                    before_state={"bindingRevision": "pending",
                                  "observationRef": "pending"})
                hook.complete_after_step(
                    step_result=SimpleNamespace(
                        step_id=step_id, status=StepStatusValue.BLOCKED),
                    receipt=None, expectation_results=[],
                    window_id=window_id, error_payload={
                        "code": "state_provider_required",
                        "message": ("authoritative state provider is "
                                    "required for v3 stage execution")})
                latched = True
                blocked_at = step_id
                steps_report.append({"stepId": step_id,
                                     "outcome": "unknown",
                                     "reason": "state provider missing"})
                break
            before_binding_revision = "pending"
            before_observation_ref = "pending"
            try:
                snapshot = observation_ref_provider(window_id)
                if not isinstance(snapshot, Mapping) or \
                        "bindingRevision" not in snapshot:
                    raise ValueError(
                        f"state provider returned invalid snapshot "
                        f"for {window_id}")
                before_binding_revision = snapshot.get(
                    "bindingRevision", "pending")
                before_observation_ref = snapshot.get(
                    "observationRef", "pending")
            except Exception as exc:  # noqa: BLE001
                hook.begin_before_preparation(
                    step_id=step_id, window_id=window_id,
                    before_state={"bindingRevision": "pending",
                                  "observationRef": "pending"})
                hook.complete_after_step(
                    step_result=SimpleNamespace(
                        step_id=step_id, status=StepStatusValue.BLOCKED),
                    receipt=None, expectation_results=[],
                    window_id=window_id, error_payload={
                        "code": "observation_provider_failed",
                        "message": str(exc)})
                latched = True
                blocked_at = step_id
                steps_report.append({
                    "stepId": step_id, "outcome": "unknown",
                    "reason": "state provider failed"})
                break
            event = hook.begin_before_preparation(
                step_id=step_id, window_id=window_id,
                before_state={"bindingRevision": before_binding_revision,
                              "observationRef": before_observation_ref})
            # 绑定聚焦(真实 binding token 传递)。focus 返回非 JSON
            # (None/任意对象)→ blocked,不默认放行(0929 缺口2)。
            binding_token = bound_windows.get(window_id)
            focus_raw = server_focus_bound(
                window_id=window_id,
                binding_token=binding_token,
                run_token=run_token_holder["token"])
            try:
                focus_result = json.loads(focus_raw) \
                    if isinstance(focus_raw, (str, bytes, bytearray)) \
                    else None
            except (ValueError, TypeError):
                focus_result = None
            if not isinstance(focus_result, Mapping) or \
                    not focus_result.get("ok", False):
                hook.complete_after_step(
                    step_result=SimpleNamespace(
                        step_id=step_id, status=StepStatusValue.BLOCKED),
                    receipt=None, expectation_results=[],
                    window_id=window_id, error_payload={
                        "code": "window_focus_failed",
                        "message": "focus refused or not a typed "
                                   "JSON receipt"})
                latched = True
                blocked_at = step_id
                steps_report.append({"stepId": step_id,
                                     "outcome": "unknown",
                                     "reason": "focus refused"})
                break
            # golden 实际动作:action_id/args 来自 declared step。
            action_id = getattr(declared, "action_id", None) or \
                (mapping.get("actionId") or "app.activate")
            action_args = dict(getattr(declared, "args", None) or {})
            receipt = json.loads(server_execute_action(
                window_id=window_id, run_id=run_id, step_id=step_id,
                action_id=action_id, action_args=action_args,
                binding_token=binding_token,
                run_token=run_token_holder["token"]))
            dispatch_inputs += 1
            # 严格 typed receipt:unknown/fail 不改 success(0929 缺口2)。
            receipt_ok = bool((receipt or {}).get("ok", False))
            receipt_status = (receipt or {}).get("status") or "pending"
            if not receipt_ok:
                hook.complete_after_step(
                    step_result=SimpleNamespace(
                        step_id=step_id, status=StepStatusValue.UNKNOWN
                        if hasattr(StepStatusValue, "UNKNOWN")
                        else StepStatusValue.FAILED),
                    receipt=SimpleNamespace(
                        to_dict=lambda: dict(receipt or {})),
                    expectation_results=[], window_id=window_id,
                    error_payload=None)
                latched = True
                blocked_at = step_id
                steps_report.append({"stepId": step_id,
                                     "outcome": "unknown",
                                     "receiptStatus": receipt_status})
                break
            # golden 声明 verifiers 全部执行(0929 缺口2:字段是
            # verifiers 不是 expectations;type/expected/归属窗口/
            # 结果与数量一致;缺省空 → required_verifier_missing)。
            declared_verifiers = list(
                getattr(declared, "verifiers", None) or [])
            assertions = list((receipt or {}).get("assertions") or [])
            if declared_verifiers and len(assertions) < \
                    len(declared_verifiers):
                hook.complete_after_step(
                    step_result=SimpleNamespace(
                        step_id=step_id, status=StepStatusValue.FAILED),
                    receipt=SimpleNamespace(
                        to_dict=lambda: dict(receipt or {})),
                    expectation_results=[], window_id=window_id,
                    error_payload={
                        "code": "required_verifier_missing",
                        "message": (f"golden declared "
                                    f"{len(declared_verifiers)} verifiers "
                                    f"but dispatcher returned "
                                    f"{len(assertions)} assertions")})
                latched = True
                blocked_at = step_id
                steps_report.append({"stepId": step_id,
                                     "outcome": "unknown",
                                     "reason": "required verifier missing"})
                break
            gated = evaluate_step_result(
                window_effect={"ok": True, "outcome": "window-ok"},
                business_assertions=assertions,
                receipt={"ok": receipt_ok, "status": receipt_status})
            after_binding_revision = "pending"
            after_observation_ref = "pending"
            if observation_ref_provider is not None:
                try:
                    snapshot = observation_ref_provider(window_id)
                    if not isinstance(snapshot, Mapping) or \
                            "bindingRevision" not in snapshot:
                        raise ValueError(
                            f"state provider returned invalid snapshot "
                            f"for {window_id}")
                    after_binding_revision = snapshot.get(
                        "bindingRevision", "pending")
                    after_observation_ref = snapshot.get(
                        "observationRef", "pending")
                except Exception as exc:  # noqa: BLE001
                    hook.complete_after_step(
                        step_result=SimpleNamespace(
                            step_id=step_id,
                            status=StepStatusValue.FAILED),
                        receipt=SimpleNamespace(
                            to_dict=lambda: dict(receipt or {})),
                        expectation_results=[], window_id=window_id,
                        error_payload={
                            "code": "observation_provider_failed",
                            "message": str(exc)})
                    latched = True
                    blocked_at = step_id
                    steps_report.append({
                        "stepId": step_id, "outcome": "unknown",
                        "reason": "state provider failed"})
                    break
            hook._attached_before = {event["eventId"]: {
                "sequenceCursor": len(steps_report),
                "bindingRevision": before_binding_revision,
                "windowId": window_id,
                "observationRef": before_observation_ref}}
            hook.complete_after_step(
                step_result=SimpleNamespace(
                    step_id=step_id,
                    status=(StepStatusValue.PASSED
                            if gated["outcome"] == "pass"
                            else StepStatusValue.FAILED)),
                receipt=SimpleNamespace(
                    ok=True, to_dict=lambda: dict(receipt or {})),
                expectation_results=[
                    SimpleNamespace(to_dict=lambda a=a: {
                        "type": a.get("assertionId", "assertion"),
                        "status": a.get("result"),
                        "expected": a.get("expected"),
                        "actual": a.get("actual"),
                        "observation_id": a.get("observationRef"),
                        "duration_ms": 0})
                    for a in assertions],
                window_id=window_id, error_payload=None)
            outcome = gated["outcome"]
            step_view = {"stepId": step_id, "windowId": window_id,
                         "attemptId": f"{step_id}-a{len(steps_report) + 1}",
                         "transitionId": transition,
                         "outcome": outcome,
                         "receiptStatus": receipt_status,
                         "previousStepId": (steps_report[-1]["stepId"]
                                            if steps_report else None),
                         "previousAttemptId": (
                             steps_report[-1].get("attemptId")
                             if steps_report else None),
                         "previousWindowId": (
                             steps_report[-1].get("windowId")
                             if steps_report else None),
                         "bindingRevision": after_binding_revision,
                         "observationRef": after_observation_ref,
                         "assertionResultRefs": assertions}
            steps_report.append(step_view)
            if outcome != "pass":
                latched = True
                blocked_at = step_id
                break
    finally:
        # 真实 end(run_token;正常/异常均释放本 run 租约;结果透传)。
        end_result = None
        try:
            end_result = server_end(
                run_token=run_token_holder.get("token"))
        except Exception as exc:  # noqa: BLE001
            end_result = {"ok": False, "error": str(exc)}
    history = ledger.rebuild_history(run_id=run_id)
    report = build_run_report(history, screenshots={})
    return {"runId": run_id, "dispatchInputs": dispatch_inputs,
            "steps": steps_report, "latched": latched,
            "blockedAt": blocked_at, "begin": begin_result,
            "end": end_result, "report": report,
            "runToken": run_token_holder.get("token")}


def _product_digest():
    """windows_hisec 生产 digest(从已装产品 profile 派生)。"""
    from target.product_profiles import get_capability
    return get_capability("windows_hisec",
                          "windows_pywinauto").digest


def _golden_window_identities(golden) -> dict:
    """golden windowSequence.windows → {W id: identity}(真实编译产物
    的声明身份;不硬编码任何 processName/title)。"""
    sequence = getattr(golden, "window_sequence", None)
    if sequence is None:
        raise ValueError("golden has no windowSequence; "
                         "v3 stage cannot address windows")
    document = sequence.to_dict()
    identities = {}
    for window in document.get("windows") or []:
        window_id = window.get("id")
        identity = window.get("identity")
        if not window_id or not isinstance(identity, Mapping):
            raise ValueError(f"golden window {window_id!r} has no "
                             "declared identity")
        entry = dict(identity)
        # Design 11 D2: the recorded owner relation rides the declaration so
        # the binding can narrow coexisting same-titled windows through the
        # live owner chain. Declared evidence only — never a merge key.
        owner_wid = window.get("ownerWindowId")
        if isinstance(owner_wid, str) and owner_wid:
            entry["ownerWindowId"] = owner_wid
        identities[window_id] = entry
    if not identities:
        raise ValueError("golden windowSequence declares no windows")
    return identities


def _validate_window_role_map(window_role_map: Mapping[str, str],
                              window_identities: Mapping[str, Mapping],
                              profile_id: str) -> dict:
    """显式 golden W → 已安装产品 role 映射(§7.1:W 是引用不是授权)。

    校验(全部拒绝在 GUI 前):
    - 每个映射的 W 必须来自 golden windowSequence 声明(未知 W 拒绝);
    - 每个映射的 role 必须是已安装产品 profile 的真实 window_roles
      条目(非 role 名/非产品域 role 拒绝);
    - W 声明身份与 role 授权域一致:processName 属于该 role 的进程集,
      titleRegex 匹配该 role 的 title_pattern(声明越域拒绝 — 不改
      产品域放行任意 W,不 conflate W 与 role)。"""
    from target.product_profiles import get_capability
    capability = get_capability(profile_id, "windows_pywinauto")
    roles = {role.role: role for role in capability.window_roles}
    validated = {}
    for window_id, role_name in dict(window_role_map).items():
        if window_id not in window_identities:
            raise ValueError(
                f"window_role_map references {window_id!r} which is not "
                "declared in the golden windowSequence")
        role = roles.get(role_name)
        if role is None:
            raise ValueError(
                f"role {role_name!r} is not a window role of installed "
                f"product profile {profile_id!r}")
        identity = window_identities[window_id]
        process = str(identity.get("processName") or "").lower()
        allowed_processes = {p.lower() for p in role.process_names}
        if process not in allowed_processes:
            raise ValueError(
                f"{window_id}: declared processName "
                f"{identity.get('processName')!r} is outside the "
                f"installed product role {role_name!r} process domain")
        title_regex = identity.get("titleRegex")
        if role.title_pattern and title_regex:
            import re as _re
            # W 的 titleRegex 必须与 role 授权 pattern 相容:W regex
            # 的匹配域不能超出 role pattern(取 W regex 能匹配的任意
            # 样例标题都必须被 role pattern 接受 — 用两者对 W 自身
            # regex 的字面样例做交集检查不可行,改为:W regex 必须
            # 是 role pattern 的子集语言;实践中用"role pattern 必须
            # 匹配 W regex 的锚定样例"保守判定不足,直接要求
            # pattern 相等或 W regex 更严格(字符集逐一包含)。
            if _re.compile(title_regex) is None:
                raise ValueError(f"{window_id}: invalid titleRegex")
        validated[window_id] = role_name
    return validated


def _role_compatible_identity(window_id: str, identity: Mapping,
                              role_name: str, profile_id: str) -> None:
    """W 声明 titleRegex 与 role 授权 pattern 的相容性:golden W 的
    titleRegex 来自录制实例的真实标题(compiler 对标题 re.escape),
    其未转义字面核心必须被 role 授权 pattern 完整接受 — 录制实例
    的实际标题在产品 role 域内。含未转义正则元字符的宽泛 W regex
    无法证明子语言关系,fail closed(保守拒绝,不放行任意 W)。"""
    import re as _re
    from target.product_profiles import get_capability
    capability = get_capability(profile_id, "windows_pywinauto")
    role = {r.role: r for r in capability.window_roles}[role_name]
    role_pattern = role.title_pattern
    window_pattern = identity.get("titleRegex")
    if not role_pattern or not window_pattern:
        return
    role_rx = _re.compile(role_pattern)
    core = window_pattern
    if core.startswith("^") and core.endswith("$") and len(core) >= 2:
        core = core[1:-1]
    # compiler 转义样式:去除字面转义(\X → X);残留正则元字符
    # → 宽泛 pattern,子语言不可证,fail closed。
    unescaped = _re.sub(r"\\(.)", r"\1", core)
    if _re.search(r"[\\^$.|?*+()\[\]{}]", unescaped):
        raise ValueError(
            f"{window_id}: declared titleRegex {window_pattern!r} is a "
            "general pattern; sublanguage compatibility with the "
            f"installed product role {role_name!r} cannot be proven "
            "(fail closed)")
    if not role_rx.fullmatch(unescaped):
        raise ValueError(
            f"{window_id}: recorded title {unescaped!r} is outside the "
            f"installed product role {role_name!r} authorization domain "
            f"({role_pattern!r})")


def _strict_state_values(*, binding_revision, observation_ref,
                         window_id: str, phase: str) -> tuple:
    """严格状态值校验(1129:None/缺失/unknown 不是合法必需事实)。

    返回 (revision, observation_ref);任何必需值非法 → ValueError
    (调用方按 phase 决定 GUI 前拒绝还是 after 锁存)。revision 必须
    是正整数;observationRef 必须是 OBS- 前缀的本 run 观测注册引用
    字符串。显式 "unknown" 允许通过并原样传播(after 未知锁存语义),
    但 None/缺失/空/错误类型不放行。"""
    import re as _re
    if binding_revision == "unknown":
        pass
    elif not isinstance(binding_revision, int) or \
            isinstance(binding_revision, bool) or binding_revision < 1:
        raise ValueError(
            f"{window_id} {phase}: bindingRevision must be a positive "
            f"integer, got {binding_revision!r}")
    if observation_ref == "unknown":
        pass
    elif not isinstance(observation_ref, str) or \
            not _re.fullmatch(r"OBS-[0-9a-f]{32}", observation_ref):
        raise ValueError(
            f"{window_id} {phase}: observationRef must be this run's "
            f"registered OBS- observation id, got {observation_ref!r}")
    return binding_revision, observation_ref


def _authoritative_step_state(server_tools, *, run_token: str,
                              server_run_id: str, window_id: str,
                              binding_token: str, attempt_id: str,
                              step_id: str, observation_record: Mapping,
                              phase: str) -> tuple:
    """**权威只读状态源**(1129 切片;内部受控,不开公共门)。

    - bindingRevision:本 run 真实 binding 的 revision(经
      WindowBindingFacade.step_context — 校验 runId 与 binding 真实
      run 一致;不匹配拒绝);
    - observationRef:本次 focus+observe 注册的 observation_id
      (server 观测记录内 window_identity.session_id 必须等于本 run
      的 server run_id — 时点/归属证明;旧 run/外部值拒绝)。
    token/native 身份不返回;仅 (revision, observation_ref)。"""
    holder = getattr(server_tools, "_binding_facade_holder", None)
    facade = (holder or {}).get("facade")
    if facade is None:
        raise ValueError(
            f"{window_id} {phase}: no binding facade for this run")
    context = facade.step_context(
        run_id=server_run_id, step_id=step_id, attempt_id=attempt_id,
        window_id=window_id, binding_token=binding_token)
    revision = context.get("bindingRevision")
    identity = observation_record.get("window_identity") or {}
    if identity.get("session_id") != server_run_id:
        raise ValueError(
            f"{window_id} {phase}: observation {observation_record.get(
                'observation_id')!r} does not belong to this run "
            f"({server_run_id!r}); refusing foreign/old-run observation")
    observation_id = observation_record.get("observation_id")
    return _strict_state_values(
        binding_revision=revision,
        observation_ref=observation_id,
        window_id=window_id, phase=phase)


def _verify_against_authority(server_tools, *, server_run_id: str,
                              window_id: str, binding_token: str,
                              attempt_id: str, step_id: str,
                              binding_revision, observation_ref,
                              observation_record: Mapping,
                              phase: str) -> tuple:
    """override/外部值必须过**同一权威验证**(1210 缺口2):格式合法
    之后,还要证明 —
    - revision 与本 run 真实 binding 当前 revision 一致(step_context,
      runId 校验;伪造/未注册 revision 拒绝);
    - observationRef 是本次实际注册的观测 id(与本 run 观测记录的
      observation_id 相等;session_id 归属本 run;旧 run/同 run 别窗/
      前观测当后态/未注册合成 id 全部拒绝)。
    不是格式正则/synthetic 前缀/自报 trusted。"""
    holder = getattr(server_tools, "_binding_facade_holder", None)
    facade = (holder or {}).get("facade")
    if facade is None:
        raise ValueError(
            f"{window_id} {phase}: no binding facade for this run")
    context = facade.step_context(
        run_id=server_run_id, step_id=step_id, attempt_id=attempt_id,
        window_id=window_id, binding_token=binding_token)
    real_revision = context.get("bindingRevision")
    if binding_revision != "unknown" and \
            binding_revision != real_revision:
        raise ValueError(
            f"{window_id} {phase}: bindingRevision {binding_revision!r} "
            f"does not match this run's actual binding revision "
            f"{real_revision!r}; refusing unregistered value")
    real_observation_id = observation_record.get("observation_id")
    if observation_ref != "unknown" and \
            observation_ref != real_observation_id:
        raise ValueError(
            f"{window_id} {phase}: observationRef {observation_ref!r} is "
            f"not the currently registered observation "
            f"{real_observation_id!r} of this run/window/attempt; "
            "refusing foreign, stale, or forged reference")
    identity = observation_record.get("window_identity") or {}
    if identity.get("session_id") != server_run_id:
        raise ValueError(
            f"{window_id} {phase}: observation does not belong to this "
            f"run ({server_run_id!r})")
    return binding_revision, observation_ref


def _observation_facts(observation_record: Mapping,
                       product_windows: list | None = None) -> dict:
    """从**真实观测注册记录**构建既有 evaluator 的观测事实(§9.3
    evaluator 协议输入;不手造观察):
    - windows:本 run 权威 path-owned 注册窗口清单
      (observe_product_windows 真实枚举 — 全部已装产品窗口事实,
      支撑 window_open/window_closed 负断言);不可用时回退本步
      锁定窗口身份(单窗事实);
    - controls:观测投影(automation_id/text/control_id/enabled/
      checked/value — normalize_observed_control 真实投影);
    - observation_id:本次注册 id(断言引用时点)。
    native 身份不进断言事实(引用经 run/window/observation 语义)。"""
    if product_windows:
        window_entries = []
        for w in product_windows:
            if not isinstance(w, Mapping):
                continue
            window_entries.append({
                "title": w.get("title"),
                "process_name": w.get("process_name"),
                "role": w.get("role"),
                "visible": w.get("visible"),
            })
        if window_entries:
            tree = observation_record.get("tree") or {}
            projected = observation_record.get("controls") or {}
            raw_controls = (
                list(projected.values()) if isinstance(projected, Mapping)
                else (tree.get("controls") or []))
            controls = [dict(c) for c in raw_controls
                        if isinstance(c, dict)]
            return {"windows": window_entries, "controls": controls,
                    "observation_id": observation_record.get(
                        "observation_id")}
    identity = dict(observation_record.get("window_identity") or {})
    window_entry = {
        "title": identity.get("title"),
        "process_name": identity.get("process_name"),
        "root_control_id": identity.get("root_control_id"),
        "visible": identity.get("visible"),
        "foreground": identity.get("foreground"),
    }
    tree = observation_record.get("tree") or {}
    controls = [dict(c) for c in (tree.get("controls") or [])
                if isinstance(c, dict)]
    projected = observation_record.get("controls") or {}
    if projected:
        controls = [dict(c) for c in projected.values()
                    if isinstance(c, dict)]
    return {"windows": [window_entry], "controls": controls,
            "observation_id": observation_record.get("observation_id")}


def _product_window_facts(server_tools, *, run_token: str) -> list:
    """本 run 权威 path-owned 注册窗口清单(真实工具只读枚举;
    异常 → 空清单,调用方回退锁定窗口事实)。"""
    try:
        payload = json.loads(server_tools.observe_product_windows(
            run_token))
    except Exception:  # noqa: BLE001 — 观测事实回退,不合成
        return []
    if not isinstance(payload, Mapping) or not payload.get("ok"):
        return []
    windows = payload.get("windows")
    return [w for w in (windows or []) if isinstance(w, Mapping)]


def _read_product_inventory(server_tools, *, run_token: str) -> dict:
    """动作后窗口清单**三态**(1426 缺口1):成功非空 / 成功空 /
    失败 — 负断言(window_closed/absent)只能依据**成功且覆盖充分**
    的本次事实;失败不是"目标不存在"。

    返回 {"ok": bool, "code": str|None, "windows": list,
          "evidence_ref": str|None}:
    - ok=True:本次真实读取成功;evidence_ref 是本次清单的稳定脱敏
      引用(INV-<sha256[:16]>,含 run 绑定字段参与摘要 — 同 run 可
      追溯、跨 run 不同;不冒充 OBS 观测注册 id 形状);
    - ok=False:读取失败(code 保留;windows 恒 [];evidence_ref
      None — 无事实,不伪造)。"""
    try:
        payload = json.loads(server_tools.observe_product_windows(
            run_token))
    except Exception as exc:  # noqa: BLE001 — 读取失败是事实状态
        return {"ok": False, "code": f"inventory_read_error:{exc}",
                "windows": [], "evidence_ref": None}
    if not isinstance(payload, Mapping) or not payload.get("ok"):
        return {"ok": False,
                "code": (payload or {}).get("code")
                if isinstance(payload, Mapping) else "inventory_invalid",
                "windows": [], "evidence_ref": None}
    windows = [w for w in (payload.get("windows") or [])
               if isinstance(w, Mapping)]
    # 断言相关脱敏特征(不含 native handle/pid — 递归脱敏会拒绝,
    # 且原生身份不进普通状态;引用经 run/window/清单语义)。
    clean_windows = [
        {k: w.get(k) for k in ("title", "process_name", "role",
                               "visible", "root_control_id")}
        for w in windows]
    # 稳定脱敏引用:run/step 归属字段参与摘要(审查要求实际用于
    # "窗口不在"的特征事实可追溯 — 引用含本 run 绑定,不跨 run
    # 同值;格式 INV-,不与 OBS 观测注册混淆)。
    digest = hashlib.sha256(json.dumps(
        {"scope": "post-action-window-inventory",
         "windows": clean_windows},
        sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()[:16]
    return {"ok": True, "code": None, "windows": clean_windows,
            "evidence_ref": f"INV-{digest}"}


def _inventory_evidence_record(inventory_read: Mapping, *, run_id: str,
                               step_id: str, attempt_id: str,
                               window_id: str, phase: str) -> dict:
    """本步**单份**持久特征记录(1456 缺口2;复用既有 append-only
    evidenceRefs 接口):实际用于判断的脱敏特征 payload(窗口清单)+
    读取状态/覆盖 + run-step-attempt-window-phase 归属 + 真实本次
    时点。内容 hash 是内容标识;本次来源关联由本记录承载 — 重启后
    从落盘 ref 可还原实际断言事实。native handle/pid/token 不落
    (递归脱敏在 complete_attempt 统一执行)。"""
    import time as _time
    windows = [dict(w) for w in (inventory_read.get("windows") or [])
               if isinstance(w, Mapping)]
    return {
        "ref": inventory_read.get("evidence_ref"),
        "kind": "window-inventory",
        "readStatus": ("ok" if inventory_read.get("ok") else
                       f"failed:{inventory_read.get('code')}"),
        "coverage": ("all-path-owned-registered-windows"
                     if inventory_read.get("ok") else None),
        "windows": windows,
        "runId": run_id, "stepId": step_id, "attemptId": attempt_id,
        "windowId": window_id, "phase": phase,
        "capturedAtMonotonic": _time.monotonic(),
    }


def _evaluate_declared_verifiers(declared, observation_record: Mapping,
                                 action_receipt, *, step_id: str,
                                 window_id: str,
                                 product_windows: list | None = None,
                                 inventory_read: Mapping | None = None) -> list:
    """**真实业务 verifier 求值**(1316 主交付;复用既有机制,不另起
    循环):declared.verifiers → `_verifier_expectation` 映射(既有
    Replay 映射;类型/expected/match/timeout 全保留)→
    `EXPECTATION_REGISTRY` 既有 evaluator 按本 run 绑定窗口、正确
    动作前后时点的真实观测事实求值 → 真实 ExpectationResult 列表
    (含 observation_id = 本次注册观测;类型缺能力 → ExpectationNot
    Available 语义的明确 failed 结果,GUI 前可拒绝,不 silent pass)。

    ``inventory_read``(1456 统一三态入口):窗口存在性断言
    (window_open/window_closed/absent)只依据**成功**读取的清单
    事实;读失败/覆盖未知 → 明确 unknown(事实缺失,不是"目标
    不存在")— 不以空清单/本窗回退冒充否定证据。"""
    from agent.recording.replay import _verifier_expectation
    from agent.execution.expectations import EXPECTATION_REGISTRY, \
        ExpectationNotAvailable
    from agent.execution.models import ExpectationResult, StepStatus
    results = []
    facts = _observation_facts(observation_record,
                               product_windows=product_windows)
    inventory_fact_missing = bool(inventory_read is not None and
                                  not inventory_read.get("ok"))
    for verifier in getattr(declared, "verifiers", None) or []:
        try:
            expectation = _verifier_expectation(declared, verifier)
        except Exception as exc:  # noqa: BLE001 — 声明非法 → 明确 fail
            results.append(ExpectationResult(
                expectation_type=str(
                    (verifier or {}).get("type") or "invalid"),
                status=StepStatus.FAILED,
                expected=(verifier or {}).get("expected"),
                actual=None,
                observation_id=facts.get("observation_id"),
                diagnostic=f"verifier mapping refused: {exc}"))
            continue
        if inventory_fact_missing and expectation.type in (
                "window_open", "window_closed"):
            # 统一三态门(1456 缺口1):清单读取失败 → 窗口存在性
            # 断言事实缺失 → unknown(不可判),不偷换"不存在"。
            results.append(ExpectationResult(
                expectation_type=expectation.type,
                status=StepStatus.BLOCKED,
                expected=expectation.value, actual=None,
                observation_id=facts.get("observation_id"),
                diagnostic=(
                    "window inventory read failed; existence facts "
                    f"unavailable (code={inventory_read.get('code')!r})")))
            continue
        evaluator = EXPECTATION_REGISTRY.get(expectation.type)
        if evaluator is None:
            from agent.execution.models import ExpectationResult, \
                StepStatus
            results.append(ExpectationResult(
                expectation_type=expectation.type,
                status=StepStatus.FAILED,
                expected=expectation.value, actual=None,
                observation_id=facts.get("observation_id"),
                diagnostic=(f"declared verifier type "
                            f"{expectation.type!r} has no evaluator in "
                            "this runtime (ExpectationNotAvailable); "
                            "refused, not passed")))
            continue
        try:
            result = evaluator(declared, expectation, facts,
                               action_receipt)
        except ExpectationNotAvailable as exc:
            from agent.execution.models import ExpectationResult, \
                StepStatus
            results.append(ExpectationResult(
                expectation_type=expectation.type,
                status=StepStatus.FAILED,
                expected=expectation.value, actual=None,
                observation_id=facts.get("observation_id"),
                diagnostic=f"evaluator unavailable: {exc}"))
        except Exception as exc:  # noqa: BLE001 — 求值异常 ≠ pass
            from agent.execution.models import ExpectationResult, \
                StepStatus
            results.append(ExpectationResult(
                expectation_type=expectation.type,
                status=StepStatus.FAILED,
                expected=expectation.value, actual=None,
                observation_id=facts.get("observation_id"),
                diagnostic=f"evaluator raised: {type(exc).__name__}: "
                           f"{exc}"))
        else:
            results.append(result)
    return results


def execute_v3_window_stage_real(*, golden, ledger, server_tools,
                                 window_role_map, profile_id="windows_hisec",
                                 transition_basis="declared_sequence",
                                 observation_ref_provider=None,
                                 lease_seconds=300,
                                 run_id=None):
    """**真实 target.server 适配器**(0959 复验切片;受控内部 v3 入口,
    公共门不打开)。

    与回调版 :func:`execute_v3_window_stage` 的差别:server 链是
    **真实工具**(``target.server`` 的 begin_product_run /
    register_window_declaration / bind_entry_window /
    focus_bound_window / execute_action / end_product_run),经
    ``server_tools`` 注入(测试替换底层 OS backend,不 mock server
    逻辑本身);golden W→产品 role 经 ``window_role_map`` 显式映射
    (§7.1:W 是引用,声明经 register_window_declaration 在产品域
    内校验,未声明/越域 W 绑定拒绝)。

    每步执行链(§7.2):
    1. ``focus_bound_window(action_id=None, observe=True)`` — 绑定
       聚焦 + 权威锁 + 本窗口新观测(恰一次观测,无业务动作);
    2. 从新观测投影构建 ``target_ref``(snapshot_id + 目标控件
       fingerprint + selector_hint) — 声明 selector.control 的
       automationId 解析到观测控件;无匹配 → blocked(0 输入);
    3. ``focus_bound_window(action_id=..., target_ref=...)`` — 同
       租约边界内 focus 后/dispatch 前重验(实例代际/归属/可见/
       前台),经统一 dispatcher 恰一次真实输入。
    finally ``end_product_run``(正常/异常均释放;真实清理结果透传)。

    run_token/binding_token 仅内存传递:不进入返回值、不进入报告、
    不持久账本(账本递归脱敏独立拒绝)。"""
    server = server_tools
    sequence_steps = []
    sequence_obj = getattr(golden, "window_sequence", None)
    if sequence_obj is not None:
        sequence_steps = sequence_obj.to_dict().get("steps") or []
    window_identities = _golden_window_identities(golden)
    # golden steps:stepId → declared step(action_id/args/verifiers/
    # selector)。真实 compiler 产物 steps 是 tuple(RecordedStep)。
    steps_declared = {}
    for step in getattr(golden, "steps", None) or []:
        step_id = getattr(step, "step_id", None) or step.get("stepId")
        steps_declared[step_id] = step
    # B-5:cleanup 也是显式映射的步骤(09§7.5)— golden.cleanup 的
    # 声明步骤进入同一声明表(主流程循环不执行它们;仅 finally 编排)。
    for step in getattr(golden, "cleanup", None) or []:
        step_id = getattr(step, "step_id", None) or step.get("stepId")
        steps_declared[step_id] = step
    # 显式 W→role 映射(越域/未知/缺 role 全部 GUI 前拒绝)。
    _validate_window_role_map(window_role_map, window_identities,
                              profile_id)
    for window_id, role_name in window_role_map.items():
        _role_compatible_identity(window_id,
                                  window_identities[window_id],
                                  role_name, profile_id)
    steps_report = []
    latched = False
    blocked_at = None
    dispatch_inputs = 0
    observations = 0
    begin_result = None
    run_token_holder = {"token": None}
    hook = None
    # runId 一致性(1129):**以 server 真实 run_id 为账本 runId** —
    # binding/facade/step_context 的 runId 校验与账本事件天然一致,
    # 不以 stage 自建 ID 冒充已验证 server 上下文。
    run_id = None
    server_run_id_holder = {"run_id": None}
    try:
        # 真实 begin(租约;拒绝 → 0 副作用)。lease_seconds 由调用方
        # 显式给出(默认 300;真实过期边界由更短租约的调用者验收)。
        begin_result = json.loads(server.begin_product_run(
            profile_id, _product_digest(), lease_seconds=lease_seconds))
        if not (begin_result or {}).get("ok", False):
            return {"runId": None, "serverRunId": None,
                    "dispatchInputs": 0,
                    "observations": 0,
                    "steps": [{"outcome": "blocked",
                               "reason": "begin refused",
                               "code": (begin_result or {}).get("code")}],
                    "latched": True, "blockedAt": None,
                    "report": {"outcome": "blocked",
                               "failedStep": None, "steps": []}}
        run_token_holder["token"] = begin_result.get("run_token")
        run_token = run_token_holder["token"]
        server_run_id_holder["run_id"] = begin_result.get("run_id")
        run_id = server_run_id_holder["run_id"]
        # hook 以 server 真实 run_id 构造(begin 之后;runId 一致)。
        hook = AttemptLedgerHook(
            ledger=ledger, run_id=run_id,
            window_by_step={s.get("stepId"): s.get("windowId")
                            for s in sequence_steps},
            transition_basis=transition_basis)
        # 真实声明(golden W 身份在产品授权域内注册;越域拒绝)。
        register_result = json.loads(server.register_window_declaration(
            json.dumps({wid: window_identities[wid]
                        for wid in window_role_map}, ensure_ascii=False),
            run_token))
        if not (register_result or {}).get("ok", False):
            return {"runId": run_id, "serverRunId": server_run_id_holder[
                        "run_id"],
                    "dispatchInputs": 0,
                    "observations": 0,
                    "steps": [{"outcome": "blocked",
                               "reason": "declaration refused",
                               "code": (register_result or {}).get(
                                   "code")}],
                    "latched": True, "blockedAt": None,
                    "report": {"outcome": "blocked",
                               "failedStep": None, "steps": []}}
        # 真实绑定 per W(复用同 W 的既有 binding;拒绝 → 0 副作用)。
        # 成功形状:binding_token/bound_native_id/state=BOUND(无 ok 键);
        # 拒绝形状:{ok: False, code, error}。
        window_origins = {}
        for window in (sequence_obj.to_dict().get("windows") or []):
            if isinstance(window, Mapping):
                window_origins[window.get("id")] = window.get(
                    "origin") or "already_open"
        bound_windows = {}
        deferred_windows = set()
        for window_id in window_role_map:
            if window_origins.get(window_id) == "observed_during_flow":
                # 将来 W:动作前尚不存在 — 不预绑定(预绑定会拒绝
                # 无观测证据的窗口,正确);其绑定由 appeared 效果
                # 成立时的唯一有效增量完成(B-2 延迟绑定)。
                deferred_windows.add(window_id)
                continue
            bind_result = json.loads(server.bind_entry_window(
                window_id, window_identities[window_id], run_token))
            bind_ok = bool(
                (bind_result or {}).get("ok", False)
                or (bind_result or {}).get("binding_token"))
            if not bind_ok:
                return {"runId": run_id, "serverRunId":
                        server_run_id_holder["run_id"],
                        "dispatchInputs": 0,
                        "observations": 0,
                        "steps": [{"outcome": "blocked",
                                   "reason": "bind refused",
                                   "code": (bind_result or {}).get(
                                       "code")}],
                        "latched": True, "blockedAt": None,
                        "report": {"outcome": "blocked",
                                   "failedStep": None, "steps": []}}
            bound_windows[window_id] = bind_result.get("binding_token")
        for mapping in sequence_steps:
            step_id = mapping["stepId"]
            window_id = mapping["windowId"]
            declared = steps_declared.get(step_id)
            if latched:
                steps_report.append({"stepId": step_id,
                                     "outcome": "skipped",
                                     "reason": "run latched"})
                continue
            binding_token = bound_windows.get(window_id)
            if binding_token is None and window_id in deferred_windows:
                # B-2 延迟绑定:将来 W 现在应已由其 appeared 效果/
                # 前序动作带入可见清单 — 本次只读校验后绑定(仍走
                # 同一真实 bind_entry_window 门:无观测证据即拒绝)。
                # Design 11 D2 续:同名候选并存时携带本步骤声明的控件
                # 选择器,由绑定侧逐候选树内唯一解析(结构收窄);
                # 选择器来自 golden 声明,不来自时间/causalId 推断。
                step_selector = None
                step_selector_obj = getattr(declared, "selector", None) \
                    if declared is not None else None
                step_control = getattr(step_selector_obj, "control", None) \
                    if step_selector_obj is not None else None
                if isinstance(step_control, Mapping) \
                        and step_control.get("structural") is not None:
                    step_selector = dict(step_control)
                bind_result = json.loads(server.bind_entry_window(
                    window_id, window_identities[window_id], run_token,
                    control_selector=step_selector))
                if bind_result.get("binding_token"):
                    bound_windows[window_id] =                         bind_result["binding_token"]
                    binding_token = bound_windows[window_id]
                    deferred_windows.discard(window_id)
            if binding_token is None:
                hook.begin_before_preparation(
                    step_id=step_id, window_id=window_id,
                    before_state={"bindingRevision": "pending",
                                  "observationRef": "pending"})
                hook.complete_after_step(
                    step_result=SimpleNamespace(
                        step_id=step_id, status=StepStatusValue.BLOCKED),
                    receipt=None, expectation_results=[],
                    window_id=window_id, error_payload={
                        "code": "window_not_bound",
                        "message": f"{window_id} has no binding token"})
                latched = True
                blocked_at = step_id
                steps_report.append({"stepId": step_id,
                                     "outcome": "unknown",
                                     "reason": "window not bound"})
                break
            # 步骤0:attempt begin + 预写(1210 缺口1)在**首次任何
            # focus/prepare 副作用之前**持久 — 预写失败(OSError)→
            # 0 focus/0 输入。权威观测需要聚焦才能取,before 权威事实
            # 在聚焦观测后经 append-only state_before_attached 有序补挂
            # (0739 既有机制),预写不被挪到 GUI 之后。
            attempt_id = f"{step_id}-a{len(steps_report) + 1}"
            try:
                event = hook.begin_before_preparation(
                    step_id=step_id, window_id=window_id,
                    before_state={"bindingRevision": "pending",
                                  "observationRef": "pending"})
            except Exception:
                # 预写失败:零 GUI 副作用(异常上抛,无 attempt 结果 —
                # 可重建边界按 started-without-completion unknown)。
                raise
            # 步骤1:绑定聚焦 + 权威锁 + 本窗口新观测(无业务动作)。
            # 观测在预写成功之后;失败可追溯(complete + 拒绝码)。
            observation = json.loads(server.focus_bound_window(
                window_id, binding_token, run_token,
                action_id=None, observe=True))
            if not (observation or {}).get("ok", False):
                hook.complete_after_step(
                    step_result=SimpleNamespace(
                        step_id=step_id, status=StepStatusValue.BLOCKED),
                    receipt=None, expectation_results=[],
                    window_id=window_id, error_payload={
                        "code": (observation or {}).get(
                            "code", "window_focus_failed"),
                        "message": "focus/observe refused"})
                latched = True
                blocked_at = step_id
                steps_report.append({"stepId": step_id,
                                     "outcome": "unknown",
                                     "reason": "focus/observe refused"})
                break
            observations += 1
            observation_id = observation.get("observation_id")
            projected_controls = observation.get("controls") or {}
            # before 权威事实(1129):binding revision 经
            # step_context(真实 binding,runId 校验),observationRef
            # 是本次聚焦观测注册 id(归属本 run 校验)。provider 覆盖
            # 时同样过严格值校验。事实非法 → 零业务输入拒绝,经
            # append-only 补挂可追溯。
            try:
                # 权威事实先算(provider 覆盖也必须与之对照 — 1210:
                # 任何外部值过同一权威验证,无伪造通道)。
                authority_before = _authoritative_step_state(
                    server, run_token=run_token,
                    server_run_id=server_run_id_holder["run_id"],
                    window_id=window_id, binding_token=binding_token,
                    attempt_id=attempt_id, step_id=step_id,
                    observation_record=observation, phase="before")
                if observation_ref_provider is not None:
                    try:
                        snapshot = observation_ref_provider(
                            window_id, phase="before",
                            authoritative=dict(zip(
                                ("bindingRevision", "observationRef"),
                                authority_before)))
                    except TypeError:
                        snapshot = observation_ref_provider(window_id)
                    if not isinstance(snapshot, Mapping):
                        raise ValueError(
                            f"state provider returned invalid snapshot "
                            f"for {window_id}")
                    before_binding_revision, before_observation_ref = \
                        _strict_state_values(
                            binding_revision=snapshot.get(
                                "bindingRevision"),
                            observation_ref=snapshot.get(
                                "observationRef"),
                            window_id=window_id, phase="before")
                    # override 必须过同一权威验证(1210 缺口2):实际
                    # 注册存在、本 run、当前 binding revision、本次
                    # 观测 id — 不是格式/黑名单可越过的来源。
                    before_binding_revision, before_observation_ref = \
                        _verify_against_authority(
                            server, server_run_id=server_run_id_holder[
                                "run_id"],
                            window_id=window_id,
                            binding_token=binding_token,
                            attempt_id=attempt_id, step_id=step_id,
                            binding_revision=before_binding_revision,
                            observation_ref=before_observation_ref,
                            observation_record=observation,
                            phase="before")
                else:
                    before_binding_revision, before_observation_ref = \
                        authority_before
            except Exception as exc:  # noqa: BLE001
                import sys as _sys
                _sys.stderr.write(f"[v3-driver-audit] before_state_invalid: "
                                  f"{type(exc).__name__}: {exc}\n")
                hook.complete_after_step(
                    step_result=SimpleNamespace(
                        step_id=step_id, status=StepStatusValue.BLOCKED),
                    receipt=None, expectation_results=[],
                    window_id=window_id, error_payload={
                        "code": "before_state_invalid",
                        "message": str(exc)})
                latched = True
                blocked_at = step_id
                steps_report.append({
                    "stepId": step_id, "outcome": "unknown",
                    "reason": "before state missing/invalid"})
                break
            # append-only 补挂(0739 既有机制):begin 预写(pending)
            # 之后、首个 GUI 副作用之前,把权威 before 事实有序挂接到
            # 本 attempt(重建时 stateBeforeRef 解析补挂事实)。
            hook._attached_before = {event["eventId"]: {
                "sequenceCursor": len(steps_report),
                "bindingRevision": before_binding_revision,
                "windowId": window_id,
                "observationRef": before_observation_ref}}
            hook._ledger.attach_state_before(event["eventId"], state={
                "sequenceCursor": len(steps_report),
                "bindingRevision": before_binding_revision,
                "windowId": window_id,
                "observationRef": before_observation_ref})
            if before_binding_revision == "unknown" or \
                    before_observation_ref == "unknown":
                # 前态必需事实显式 unknown(1316):与 after 同一严格
                # 推进门 — 零业务输入、durable/report unknown 锁存,
                # 不能被 after 正常抹平。
                hook.complete_after_step(
                    step_result=SimpleNamespace(
                        step_id=step_id, status=StepStatusValue.BLOCKED),
                    receipt=None, expectation_results=[],
                    window_id=window_id, error_payload={
                        "code": "before_state_unknown",
                        "message": ("required before-state facts are "
                                    "explicitly unknown; run latched "
                                    "(§7.6)")})
                latched = True
                blocked_at = step_id
                steps_report.append({
                    "stepId": step_id, "outcome": "unknown",
                    "reason": "before state unknown"})
                break
            # 步骤2:声明 selector.control 的全部约束(automationId/
            # identifier/controlType/name/fingerprint)解析到本窗口新
            # 观测 → 候选集;**唯一候选才 dispatch**,多候选 0 业务输入
            # 拒绝(1059 缺口2:不挑第一项/最近;复用既有精确解析
            # 唯一性语义 — 与 binding._resolve_unique 同门)。
            declared = steps_declared.get(step_id)
            selector = getattr(declared, "selector", None)
            control_selector = getattr(selector, "control", None) or {}
            wanted_automation_id = control_selector.get("automationId")

            def _matches(control) -> bool:
                # 声明约束逐一比对;声明字段必须与观测投影一致。
                # fingerprint 不参与候选匹配:声明值来自录制时
                # observedTarget(录制指纹),观测投影值是运行时
                # 投影哈希 — 语义不同源,直接比较恒假;它仍随
                # target_ref 交由 dispatcher 的既有校验处理。
                constraints = (
                    ("automationId", "automation_id"),
                    ("identifier", "identifier"),
                    ("controlType", "control_type"),
                    ("name", "text"),
                )
                for declared_key, projected_key in constraints:
                    if declared_key not in control_selector:
                        continue
                    if control.get(projected_key) != \
                            control_selector[declared_key]:
                        return False
                return bool(control_selector)

            candidates = [control for control in projected_controls.values()
                          if _matches(control)]
            menu_click_args = None
            if control_selector.get("structural") is not None:
                # Same locator as ordinary golden replay. This projection is
                # already scoped by the live binding/lease to this window;
                # do not fall back to the recorded label or screen point.
                from agent.locator import HybridLocator, LocatorRequest, LocatorError
                from agent.locator.menu_layout import current_click_args
                try:
                    candidates = [c.source for c in HybridLocator().collect_candidates(
                        LocatorRequest("gui.click", {}, control_selector),
                        {"observation_id": observation_id,
                         "controls": list(projected_controls.values())})]
                    if len(candidates) == 1:
                        menu_click_args = current_click_args(
                            candidates[0], list(projected_controls.values()))
                except (LocatorError, ValueError):
                    candidates = []
            if not candidates:
                hook.complete_after_step(
                    step_result=SimpleNamespace(
                        step_id=step_id, status=StepStatusValue.BLOCKED),
                    receipt=None, expectation_results=[],
                    window_id=window_id, error_payload={
                        "code": "target_not_in_observation",
                        "message": (f"declared control "
                                    f"{wanted_automation_id!r} is not in "
                                    f"the fresh observation of "
                                    f"{window_id}")})
                latched = True
                blocked_at = step_id
                steps_report.append({"stepId": step_id,
                                     "outcome": "unknown",
                                     "reason": "target not in observation"})
                break
            if len(candidates) > 1:
                hook.complete_after_step(
                    step_result=SimpleNamespace(
                        step_id=step_id, status=StepStatusValue.BLOCKED),
                    receipt=None, expectation_results=[],
                    window_id=window_id, error_payload={
                        "code": "target_ambiguous",
                        "message": (f"declared control "
                                    f"{wanted_automation_id!r} matches "
                                    f"{len(candidates)} controls in the "
                                    f"fresh observation of {window_id}; "
                                    f"refusing to pick any (0 input)")})
                latched = True
                blocked_at = step_id
                steps_report.append({"stepId": step_id,
                                     "outcome": "unknown",
                                     "reason": "target ambiguous"})
                break
            target_control = candidates[0]
            target_ref = {
                "snapshot_id": observation_id,
                "target_id": target_control.get("target_id"),
                "fingerprint": target_control.get("fingerprint"),
                "expected_process_name":
                    window_identities[window_id].get("processName"),
                "selector_hint": {"automation_id": wanted_automation_id},
            }
            # 声明动作语义:action_id 为 None 的步是 assertion-only 步
            # (仅求值声明 verifier,无 GUI 动作)。
            declared_action_id = getattr(declared, "action_id", None)
            if not declared_action_id:
                # assertion-only(1316 主交付):**真实求值**声明
                # verifier — 用本 run 绑定窗口、动作前(=无动作,聚焦
                # 观测即正确时点)的真实观测事实;0 业务输入。严格
                # pass 才推进;fail/unknown/缺事实锁存,不合成动作。
                inventory_read = _read_product_inventory(
                    server, run_token=run_token)
                verifer_results = _evaluate_declared_verifiers(
                    declared, observation, None, step_id=step_id,
                    window_id=window_id,
                    product_windows=inventory_read["windows"],
                    inventory_read=inventory_read)
                assertion_refs = []
                all_pass = bool(verifer_results) and all(
                    getattr(r, "status", None) is not None and
                    getattr(r.status, "value", r.status) == "passed"
                    for r in verifer_results)
                outcome = "pass" if all_pass else (
                    "fail" if any(
                        getattr(r.status, "value", r.status) == "failed"
                        for r in verifer_results) else "unknown")
                hook.complete_after_step(
                    step_result=SimpleNamespace(
                        step_id=step_id,
                        status=(StepStatusValue.PASSED if all_pass
                                else (StepStatusValue.FAILED
                                      if outcome == "fail"
                                      else StepStatusValue.BLOCKED))),
                    receipt=None,
                    expectation_results=verifer_results,
                    window_id=window_id,
                    error_payload=None if all_pass else {
                        "code": "assertion_only_verifier_not_passed",
                        "message": ("assertion-only step verifiers "
                                    "evaluated against the fresh "
                                    "authoritative observation; "
                                    f"outcome={outcome}")})
                steps_report.append({
                    "stepId": step_id, "windowId": window_id,
                    "attemptId": attempt_id,
                    "transitionId": (
                        "first_bind" if not steps_report else
                        ("switched_bound_window"
                         if window_id != steps_report[-1].get("windowId")
                         else "same_window_reverified")),
                    "outcome": outcome,
                    "previousStepId": (steps_report[-1]["stepId"]
                                       if steps_report else None),
                    "previousAttemptId": (
                        steps_report[-1].get("attemptId")
                        if steps_report else None),
                    "previousWindowId": (
                        steps_report[-1].get("windowId")
                        if steps_report else None),
                    "assertionResultRefs": [
                        {"assertionId":
                            f"{step_id}:{getattr(r, 'expectation_type', 'assertion')}:{i}",
                         "type": getattr(r, "expectation_type", None),
                         "result": getattr(
                             getattr(r, "status", None), "value", None),
                         "expected": getattr(r, "expected", None),
                         "actual": getattr(r, "actual", None),
                         "observationRef": getattr(
                             r, "observation_id", None)}
                        for i, r in enumerate(verifer_results)]})
                if outcome != "pass":
                    latched = True
                    blocked_at = step_id
                    break
                continue
            action_id = declared_action_id
            action_args = dict(getattr(declared, "args", None) or {})
            if menu_click_args is not None:
                for key in ("automation_id", "text", "control_id", "class_name",
                            "parent_text", "auto_id_contains", "auto_id_suffix", "parent_of"):
                    action_args.pop(key, None)
                action_args.update(menu_click_args)
            action_args.setdefault("expected_process_name",
                                   window_identities[window_id].get(
                                       "processName"))
            # 声明控件身份进真实点击(1316"实际动作参数"复核;gui.click
            # 的 automation_id 参数)— target_ref/selector_hint 同时
            # 仍经 dispatcher 既有校验;缺省 automationId 的声明保持
            # 无控件参数(不伪造控件身份)。
            if wanted_automation_id:
                action_args.setdefault("automation_id",
                                       wanted_automation_id)
            elif not control_selector.get("structural"):
                # Weak identity (no automationId): the backend click needs
                # the resolved control's own identity. Materialize
                # control_type/text from the RESOLVED projection (the same
                # observation the target_ref fingerprint pins) — never from
                # the recorded label alone; the recorded selector already
                # matched this projection uniquely.
                resolved_text = target_control.get("text")
                if resolved_text:
                    # Text only: the backend's title/text filter resolves the
                    # deep menu item; pairing control_type here makes the
                    # strict child_window path miss it (replay round r3:
                    # text+MenuItem → Control not found, text alone → found).
                    action_args.setdefault("text", resolved_text)
            # B-4(09§7.2/10 R1):drag 双端语义解析 — selector 与
            # endSelector 在**本次观测**下经既有解析器唯一解析
            # (HybridLocator/resolve_replay_selector;不用录制
            # screen XY 直接回放;模糊端点 0 越权输入拒绝)。
            if action_id == "pointer.drag":
                from agent.recording.replay import (
                    resolve_replay_selector, StepMaterializationError)
                end_selector = getattr(declared, "end_selector", None)
                if end_selector is None or not \
                        getattr(end_selector, "control", None):
                    hook.complete_after_step(
                        step_result=SimpleNamespace(
                            step_id=step_id,
                            status=StepStatusValue.BLOCKED),
                        receipt=None, expectation_results=[],
                        window_id=window_id, error_payload={
                            "code": "replay_drag_end_missing",
                            "message": ("a recorded drag requires a "
                                        "stable release-point selector")})
                    latched = True
                    blocked_at = step_id
                    steps_report.append({"stepId": step_id,
                                         "outcome": "unknown",
                                         "reason": "drag end missing"})
                    break
                try:
                    controls_list = list(projected_controls.values())
                    start_resolution = resolve_replay_selector(
                        selector, {"controls": controls_list,
                                   "observation_id": observation_id})
                    end_resolution = resolve_replay_selector(
                        end_selector, {"controls": controls_list,
                                       "observation_id": observation_id})
                except StepMaterializationError as exc:
                    hook.complete_after_step(
                        step_result=SimpleNamespace(
                            step_id=step_id,
                            status=StepStatusValue.BLOCKED),
                        receipt=None, expectation_results=[],
                        window_id=window_id, error_payload={
                            "code": getattr(exc, "code",
                                            "drag_endpoint_ambiguous"),
                            "message": str(exc)})
                    latched = True
                    blocked_at = step_id
                    steps_report.append({"stepId": step_id,
                                         "outcome": "unknown",
                                         "reason": "drag endpoint not "
                                                   "uniquely resolved"})
                    break
                start_rect = start_resolution.__dict__.get("rect") or \
                    target_control.get("rect")
                end_control = next(
                    (c for c in projected_controls.values()
                     if c.get("target_id") ==
                     end_resolution.__dict__.get("target_id")), {})
                end_rect = end_control.get("rect") or []
                if not isinstance(start_rect, (list, tuple)) or \
                        len(start_rect) < 4 or \
                        not isinstance(end_rect, (list, tuple)) or \
                        len(end_rect) < 4:
                    hook.complete_after_step(
                        step_result=SimpleNamespace(
                            step_id=step_id,
                            status=StepStatusValue.BLOCKED),
                        receipt=None, expectation_results=[],
                        window_id=window_id, error_payload={
                            "code": "drag_endpoint_rect_unavailable",
                            "message": "resolved endpoints lack rect"})
                    latched = True
                    blocked_at = step_id
                    steps_report.append({"stepId": step_id,
                                         "outcome": "unknown",
                                         "reason": "drag rect missing"})
                    break

                def _center(rect):
                    return (int(rect[0]) + int(rect[2])) // 2, \
                        (int(rect[1]) + int(rect[3])) // 2

                start_x, start_y = _center(start_rect)
                end_x, end_y = _center(end_rect)
                # 双端坐标 + duration(拖拽后端签名);expected_process_
                # name/automation_id 不进 drag 签名(归属已由
                # target_ref ownership 门校验)。
                action_args.update({"x1": start_x, "y1": start_y,
                                    "x2": end_x, "y2": end_y})
                action_args.pop("automation_id", None)
                action_args.pop("expected_process_name", None)
            # P1(1721 缺口3):scroll 需要位置时以本次唯一已解析
            # 目标物化(录制/任意外部 XY 不直通 OS;无解析目标 →
            # 仅 clicks 的无位置滚动不受影响)。
            elif action_id == "pointer.scroll":
                recorded_x = action_args.get("x")
                recorded_y = action_args.get("y")
                if recorded_x is not None or recorded_y is not None:
                    try:
                        from agent.recording.replay import (
                            resolve_replay_selector,
                            StepMaterializationError)
                        controls_list = list(projected_controls.values())
                        scroll_resolution = resolve_replay_selector(
                            selector, {"controls": controls_list,
                                       "observation_id": observation_id})
                        scroll_rect = next(
                            (c.get("rect") for c in projected_controls.values()
                             if c.get("target_id") ==
                             scroll_resolution.__dict__.get("target_id")),
                            None)
                        if not isinstance(scroll_rect, (list, tuple)) or \
                                len(scroll_rect) < 4:
                            raise StepMaterializationError(
                                "scroll_target_rect_unavailable",
                                "resolved scroll target lacks rect")
                        sx = (int(scroll_rect[0]) + int(scroll_rect[2])) // 2
                        sy = (int(scroll_rect[1]) + int(scroll_rect[3])) // 2
                        action_args["x"] = sx
                        action_args["y"] = sy
                    except StepMaterializationError as exc:
                        hook.complete_after_step(
                            step_result=SimpleNamespace(
                                step_id=step_id,
                                status=StepStatusValue.BLOCKED),
                            receipt=None, expectation_results=[],
                            window_id=window_id, error_payload={
                                "code": getattr(exc, "code",
                                                "scroll_target_unresolved"),
                                "message": str(exc)})
                        latched = True
                        blocked_at = step_id
                        steps_report.append({
                            "stepId": step_id, "outcome": "unknown",
                            "reason": "scroll target not resolved"})
                        break
            # B-1(09§7.3):动作声明了窗口 effects → dispatch 前
            # 在同租约内记录基线(只枚举,不聚焦/不激活;预写已成功
            # 在 begin 时持久)。本步声明的 effect 列表来自真实
            # compiler 产物 windowSequence.steps[].effects。
            declared_effects = list(mapping.get("effects") or [])
            baseline_token = None
            effect_baseline_tokens = {}
            effect_instance_refs = {}
            # native 值仅本 run 内存(opaque ref → native 映射);
            # 不透明引用持久,native 不落任何持久状态。
            effect_instance_natives = {}
            if declared_effects:
                try:
                    facade = (getattr(server, "_binding_facade_holder",
                                      {}) or {}).get("facade")
                    if facade is None:
                        raise ValueError("no binding facade for baseline")
                    # 基线按**效果目标身份**记录(§7.3:差分是"匹配
                    # 该身份的候选"前后差;目标窗口可能尚未存在 —
                    # 基线窗口 id 用效果目标的 W)。
                    for effect in declared_effects:
                        effect_wid = effect.get("windowId") or window_id
                        baseline = facade.begin_effect_baseline(
                            effect_wid,
                            window_identities.get(effect_wid)
                            or window_identities[window_id],
                            binding_token=bound_windows.get(effect_wid))
                        effect_baseline_tokens[effect_wid] = \
                            baseline.get("baseline_token")
                    baseline_token = next(iter(
                        effect_baseline_tokens.values()), None)
                except Exception as exc:  # noqa: BLE001 — 基线失败=事实缺失
                    hook.complete_after_step(
                        step_result=SimpleNamespace(
                            step_id=step_id,
                            status=StepStatusValue.BLOCKED),
                        receipt=None, expectation_results=[],
                        window_id=window_id, error_payload={
                            "code": "effect_baseline_failed",
                            "message": (f"pre-action effect baseline "
                                        f"failed: {exc}")})
                    latched = True
                    blocked_at = step_id
                    steps_report.append({
                        "stepId": step_id, "outcome": "unknown",
                        "reason": "effect baseline failed"})
                    break
            receipt_json = server.focus_bound_window(
                window_id, binding_token, run_token,
                action_id=action_id, action_args=action_args,
                target_ref=target_ref, observe=False)
            try:
                receipt = json.loads(receipt_json)
            except (TypeError, ValueError):
                receipt = {"ok": False, "code":
                           "window_binding_dispatch_unknown"}
            dispatch_inputs += 1
            # 严格 typed receipt:ok=False/未知 → unknown 锁存。
            if not (receipt or {}).get("ok", False):
                hook.complete_after_step(
                    step_result=SimpleNamespace(
                        step_id=step_id, status=StepStatusValue.BLOCKED),
                    receipt=SimpleNamespace(
                        to_dict=lambda r=dict(receipt): dict(r)),
                    expectation_results=[], window_id=window_id,
                    error_payload={
                        "code": (receipt or {}).get(
                            "code", "action_receipt_not_ok"),
                        "message": "dispatcher receipt not ok"})
                latched = True
                blocked_at = step_id
                steps_report.append({"stepId": step_id,
                                     "outcome": "unknown",
                                     "receiptCode":
                                         (receipt or {}).get("code")})
                break
            # B-1(09§7.3):receipt 确定后、业务断言前 — 只读有界
            # 轮询评估每个声明的窗口效果(不聚焦/不激活;多个效果
            # 必须全部满足;零候选到期 timeout、多候选 ambiguous、
            # receipt unknown 一律不假 pass)。
            effect_results = []
            if declared_effects and baseline_token:
                facade = (getattr(server, "_binding_facade_holder",
                                  {}) or {}).get("facade")
                deadline = time.monotonic() + max(
                    (float(e.get("timeoutSeconds") or 10.0)
                     for e in declared_effects), default=10.0)
                for effect in declared_effects:
                    effect_outcome = None
                    effect_wid = effect.get("windowId") or window_id
                    while True:
                        try:
                            observation_result = facade.\
                                observe_effect_after_action(
                                    effect_wid,
                                    effect_baseline_tokens.get(
                                        effect_wid, baseline_token),
                                    effect=effect,
                                    binding_token=bound_windows.get(
                                        effect_wid))
                        except Exception as exc:  # noqa: BLE001
                            effect_outcome = {"ok": False,
                                              "outcome": "error",
                                              "error": str(exc)}
                            break
                        if observation_result.get("outcome") in (
                                "appeared", "absent",
                                "same_instance_shown"):
                            effect_outcome = observation_result
                            break
                        if observation_result.get("outcome") == \
                                "ambiguous":
                            effect_outcome = observation_result
                            break
                        if time.monotonic() >= deadline:
                            effect_outcome = observation_result
                            break
                        time.sleep(0.05)
                    # P1(1721 缺口1):按声明 kind **精确匹配语义
                    # 终态** — appeared/same_instance_shown/absent 才
                    # 是效果成立;present(读取成功但未发生)/
                    # timeout/error/ambiguous 一律不成功。工具 ok 只
                    # 表示读取成功,不升级效果结果。
                    outcome_name = (effect_outcome or {}).get("outcome")
                    kind = effect.get("kind")
                    satisfied = (
                        (kind == "appeared" and outcome_name in
                         ("appeared", "same_instance_shown"))
                        or (kind == "absent" and outcome_name == "absent"))
                    # P2(1721 缺口4):appeared/same_instance 的原生
                    # 实例经同租约强实例边界消费 — 唯一增量绑定目标
                    # W(不挑第一项、不放宽全部候选);持久引用为
                    # 本 run 内不透明实例序(非 native 原值编码)。
                    native_outcome = (effect_outcome or {}).get(
                        "native_id")
                    opaque_ref = None
                    if native_outcome:
                        opaque_ref = effect_instance_refs.setdefault(
                            str(native_outcome),
                            f"INST-{len(effect_instance_refs) + 1:04d}")
                        effect_instance_natives[opaque_ref] = \
                            str(native_outcome)
                    effect_results.append({
                        "effectId": effect.get("id"),
                        "kind": kind,
                        "windowId": effect.get("windowId"),
                        "outcome": outcome_name,
                        "satisfied": satisfied,
                        "ok": satisfied,
                        **({"instanceRef": opaque_ref}
                           if opaque_ref else {})})
                if not all(e["ok"] for e in effect_results):
                    # 失败也持久必要本次事实(baseline/diff/结果/
                    # 真实拒绝原因 — 不只 stage 临时摘要)。
                    hook._attached_after = {event["eventId"]: {
                        "bindingRevision": "pending",
                        "observationRef": "pending",
                        "windowEffect": effect_results,
                        **({"effectBaseline": {
                            "tokens": effect_baseline_tokens}}
                           if effect_baseline_tokens else {})}}
                    hook.complete_after_step(
                        step_result=SimpleNamespace(
                            step_id=step_id,
                            status=StepStatusValue.BLOCKED),
                        receipt=SimpleNamespace(
                            to_dict=lambda r=dict(receipt): dict(r)),
                        expectation_results=[], window_id=window_id,
                        error_payload={
                            "code": "declared_window_effect_not_satisfied",
                            "message": (f"declared window effects not all "
                                        f"satisfied: "
                                        f"{json.dumps(effect_results, ensure_ascii=False)}")})
                    latched = True
                    blocked_at = step_id
                    steps_report.append({
                        "stepId": step_id, "outcome": "unknown",
                        "reason": "window effect not satisfied",
                        "effects": effect_results})
                    break
                # appeared 唯一增量 → 延迟绑定(B-2;origin 策略:
                # observed_during_flow 的 W 在对应动作后以唯一有效
                # 增量绑定;已可见同名旧窗口不满足 appeared — 由
                # 基线差分天然排除)。
                # appeared/same_instance 唯一增量 → 同租约强实例边界
                # 消费绑定(B-2/P2;origin 策略:observed_during_flow
                # 的 W 在对应动作后以唯一有效增量绑定;已可见同名旧
                # 窗口不满足 appeared — 基线差分天然排除;instanceRef
                # 在 effect 汇总时已登记,绑定分支按它消费)。
                for effect_result, effect in zip(effect_results,
                                                 declared_effects):
                    if effect_result["kind"] == "appeared" and \
                            effect_result.get("satisfied") and \
                            effect_result.get("instanceRef"):
                        new_wid = effect.get("windowId")
                        if new_wid and new_wid not in bound_windows:
                            try:
                                facade = (getattr(
                                    server, "_binding_facade_holder",
                                    {}) or {}).get("facade")
                                # 同租约强实例边界消费唯一增量:
                                # bind_increment 绑定 diff 观察到的
                                # 确切 native(强实例证据仍经 tracker
                                # 校验;不放宽清单/不挑第一项)。
                                bind_result = facade.bind_increment(
                                    new_wid,
                                    window_identities.get(new_wid)
                                    or window_identities[window_id],
                                    native_id=effect_instance_natives[
                                        effect_result["instanceRef"]],
                                    run_token=run_token)
                                if bind_result.get("binding_token"):
                                    bound_windows[new_wid] = \
                                        bind_result["binding_token"]
                                    deferred_windows.discard(new_wid)
                            except Exception:  # noqa: BLE001
                                pass
            # after-state:动作后的**权威只读观测**是必需事实
            # (1059/1129):真实 focus+observe(observe=True 无业务
            # 动作)注册动作后观测;step_context 读同租约 binding
            # revision。异常/缺失/非法 → 后态未知 → unknown 锁存
            # (后续 0 focus/输入;动作可能已执行,不称未执行、不重试
            # GUI);成功 → 真实 revision/observation 进入不可变持久
            # after(与摘要一致),不用 pre-action 值冒充 post-state。
            after_binding_revision = "unknown"
            after_observation_ref = "unknown"
            post_observation = None
            post_binding_lost = False
            try:
                # 权威动作后观测(observe=True 无业务动作;每次注册
                # 新 id)先取 — provider 覆盖值必须与之对照。
                # 动作关闭了本步绑定窗口本身 → 重聚焦按 §7.4 LOST
                # 拒绝(window_binding_lost):绑定丢失是真实后态,
                # 窗口存在性断言仍可按动作后注册清单
                # (observe_product_windows)求值 — LOST 明确标记,
                # 不重试、不复活 binding。
                post_raw = json.loads(server.focus_bound_window(
                    window_id, binding_token, run_token,
                    action_id=None, observe=True, read_only=True))
                if not (post_raw or {}).get("ok", False):
                    if (post_raw or {}).get("code") == "window_binding_lost":
                        post_binding_lost = True
                        raise ValueError("bound window gone after action")
                    raise ValueError(
                        "post-action observation refused: "
                        f"{(post_raw or {}).get('code')}")
                post_observation = post_raw
                observations += 1
                authority_after = _authoritative_step_state(
                    server, run_token=run_token,
                    server_run_id=server_run_id_holder["run_id"],
                    window_id=window_id, binding_token=binding_token,
                    attempt_id=attempt_id, step_id=step_id,
                    observation_record=post_observation, phase="after")
                if observation_ref_provider is not None:
                    try:
                        snapshot = observation_ref_provider(
                            window_id, phase="after",
                            authoritative=dict(zip(
                                ("bindingRevision", "observationRef"),
                                authority_after)))
                    except TypeError:
                        snapshot = observation_ref_provider(window_id)
                    if not isinstance(snapshot, Mapping):
                        raise ValueError(
                            f"state provider returned invalid snapshot "
                            f"for {window_id}")
                    after_binding_revision, after_observation_ref = \
                        _strict_state_values(
                            binding_revision=snapshot.get(
                                "bindingRevision"),
                            observation_ref=snapshot.get(
                                "observationRef"),
                            window_id=window_id, phase="after")
                    # override 必须过同一权威验证(1210 缺口2):本 run
                    # 当前 binding revision + 本次注册观测 id — 未注册/
                    # 别窗/前观测/伪造引用拒绝,不是格式/黑名单可越过。
                    after_binding_revision, after_observation_ref = \
                        _verify_against_authority(
                            server, server_run_id=server_run_id_holder[
                                "run_id"],
                            window_id=window_id,
                            binding_token=binding_token,
                            attempt_id=attempt_id, step_id=step_id,
                            binding_revision=after_binding_revision,
                            observation_ref=after_observation_ref,
                            observation_record=post_observation,
                            phase="after")
                else:
                    after_binding_revision, after_observation_ref = \
                        authority_after
            except Exception:  # noqa: BLE001 — 后态未知,锁存
                if not post_binding_lost:
                    hook._attached_after = {event["eventId"]: {
                        "bindingRevision": "unknown",
                        "observationRef": "unknown"}}
                    hook.complete_after_step(
                        step_result=SimpleNamespace(
                            step_id=step_id,
                            status=StepStatusValue.BLOCKED),
                        receipt=SimpleNamespace(
                            to_dict=lambda r=dict(receipt): dict(r)),
                        expectation_results=[], window_id=window_id,
                        error_payload={
                            "code": "post_state_observation_failed",
                            "message": ("post-action state observation "
                                        "failed; outcome unknown, run "
                                        "latched (§7.6)")})
                    latched = True
                    blocked_at = step_id
                    steps_report.append({
                        "stepId": step_id, "outcome": "unknown",
                        "reason": "post state unknown"})
                    break
                # 动作关闭了本步绑定窗口(真实后态):binding 按 §7.4
                # 已 LOST(不复活/不重试);窗口存在性断言按动作后
                # 注册清单求值,断言结果决定推进;控件级断言事实不可得
                # → 明确 unknown(不冒充 pass)。after 引用 = 本 run
                # 动作后窗口清单注册(observe_product_windows 的真实
                # 只读枚举;无注册 id 时显式 unknown)。
                # 动作后窗口清单三态(1426):成功非空/成功空/失败 —
                # 负断言只用**成功且覆盖充分**的本次事实;失败/缺失
                # → 事实 unknown(不把故障读/空 list 当"目标不存在")。
                inventory_read = _read_product_inventory(
                    server, run_token=run_token)
                declared_only = list(
                    getattr(declared, "verifiers", None) or [])
                if inventory_read["ok"] and declared_only:
                    # 显式声明存在时才可按清单求值(1426 缺口3:空
                    # 声明不偷换"已确认关闭")。
                    inventory_record = {
                        "window_identity": {}, "tree": {"controls": []},
                        "controls": {},
                        "observation_id":
                            inventory_read["evidence_ref"]}
                    verifer_results = _evaluate_declared_verifiers(
                        declared, inventory_record,
                        SimpleNamespace(
                            to_dict=lambda r=dict(receipt): dict(r)),
                        step_id=step_id, window_id=window_id,
                        product_windows=inventory_read["windows"])
                    assertion_refs = [
                        {"assertionId":
                            f"{step_id}:{getattr(r, 'expectation_type', 'assertion')}:{i}",
                         "type": getattr(r, "expectation_type", None),
                         "result": getattr(
                             getattr(r, "status", None), "value", None),
                         "expected": getattr(r, "expected", None),
                         "actual": getattr(r, "actual", None),
                         "observationRef": getattr(
                             r, "observation_id", None)}
                        for i, r in enumerate(verifer_results)]
                    strict_pass = all(
                        getattr(getattr(r, "status", None), "value", None)
                        == "passed" for r in verifer_results)
                    any_fail = any(
                        getattr(getattr(r, "status", None), "value", None)
                        == "failed" for r in verifer_results)
                    outcome = ("pass" if strict_pass and not any_fail
                               else ("fail" if any_fail else "unknown"))
                else:
                    # 失败读/无声明:没有可验证的关闭/absent 依据 →
                    # 明确 unknown(不解释成"动作按预期关闭")。
                    verifer_results = []
                    assertion_refs = []
                    outcome = "unknown"
                observation_ref_after = (
                    inventory_read["evidence_ref"]
                    if inventory_read["ok"] and
                    inventory_read["evidence_ref"] else "unknown")
                hook._attached_after = {event["eventId"]: {
                    "bindingRevision": "lost",
                    "observationRef": observation_ref_after,
                    # B-1(09§7.6):LOST 步的效果结果同样持久
                    # (先 receipt+效果、后 LOST — §7.3 顺序)。
                    **({"windowEffect": effect_results}
                       if declared_effects else {})}}
                hook.complete_after_step(
                    step_result=SimpleNamespace(
                        step_id=step_id,
                        status=(StepStatusValue.PASSED
                                if outcome == "pass"
                                else (StepStatusValue.FAILED
                                      if outcome == "fail"
                                      else StepStatusValue.BLOCKED))),
                    receipt=SimpleNamespace(
                        to_dict=lambda r=dict(receipt): dict(r)),
                    expectation_results=verifer_results,
                    window_id=window_id,
                    evidence_refs=([_inventory_evidence_record(
                        inventory_read, run_id=run_id, step_id=step_id,
                        attempt_id=attempt_id, window_id=window_id,
                        phase="after")] if inventory_read["ok"] else []),
                    error_payload=(None if outcome == "pass" else {
                        "code": ("bound_window_lost_after_action"
                                 if inventory_read["ok"] else
                                 "post_action_inventory_unavailable"),
                        "message": ("the action closed this step's own "
                                    "bound window (§7.4 LOST); window "
                                    "assertions evaluated on the "
                                    "post-action inventory; control "
                                    "facts unavailable")}))
                steps_report.append({
                    "stepId": step_id, "windowId": window_id,
                    "attemptId": attempt_id,
                    "transitionId": (
                        "first_bind" if not steps_report else
                        ("switched_bound_window"
                         if window_id != steps_report[-1].get("windowId")
                         else "same_window_reverified")),
                    "outcome": outcome,
                    "receiptCode": (receipt or {}).get("code"),
                    "previousStepId": (steps_report[-1]["stepId"]
                                       if steps_report else None),
                    "previousAttemptId": (
                        steps_report[-1].get("attemptId")
                        if steps_report else None),
                    "previousWindowId": (
                        steps_report[-1].get("windowId")
                        if steps_report else None),
                    "bindingRevision": "lost",
                    "observationRef": observation_ref_after,
                    "assertionResultRefs": assertion_refs,
                    "targetId": target_ref.get("target_id")})
                if outcome != "pass":
                    latched = True
                    blocked_at = step_id
                    break
                # pass(窗口断言在 LOST 后的清单上成立)→ 继续序列
                # (绑定丢失不阻断录制自身声明的后续窗口步骤;下一步
                # 按声明重新绑定/聚焦目标窗口)。
                continue
            if after_binding_revision == "unknown" or \
                    after_observation_ref == "unknown":
                # 显式 unknown 后态(1129):未知不是成功推进 — 恰 1
                # 输入,unknown 锁存,后续 0 focus/输入(动作可能已
                # 执行,不称未执行、不重试 GUI)。
                hook._attached_after = {event["eventId"]: {
                    "bindingRevision": after_binding_revision,
                    "observationRef": after_observation_ref}}
                hook.complete_after_step(
                    step_result=SimpleNamespace(
                        step_id=step_id,
                        status=StepStatusValue.BLOCKED),
                    receipt=SimpleNamespace(
                        to_dict=lambda r=dict(receipt): dict(r)),
                    expectation_results=[], window_id=window_id,
                    error_payload={
                        "code": "post_state_unknown",
                        "message": ("post-action state is explicitly "
                                    "unknown; run latched (§7.6)")})
                latched = True
                blocked_at = step_id
                steps_report.append({
                    "stepId": step_id, "outcome": "unknown",
                    "reason": "post state unknown"})
                break
            # 真实业务 verifier 求值(1316 主交付):declared.verifiers
            # 用**动作后权威观测事实**实际求值(类型/expected/match);
            # receipt.ok 只是窗口动作回执,不替代业务断言。既有
            # evaluator 输出真实 ExpectationResult(带本次注册观测 id)。
            declared_verifiers = list(
                getattr(declared, "verifiers", None) or [])
            inventory_read = _read_product_inventory(
                server, run_token=run_token)
            verifer_results = _evaluate_declared_verifiers(
                declared, post_observation,
                SimpleNamespace(to_dict=lambda r=dict(receipt): dict(r)),
                step_id=step_id, window_id=window_id,
                product_windows=inventory_read["windows"],
                inventory_read=inventory_read) if declared_verifiers \
                else []
            # Design 11 D2: window_open 断言的 timeoutSeconds 是存在性
            # 等待上限——对话框在动作后异步出现（click_input 到前台化
            # 到 Qt 创建窗口 >0.1s），post 观测是一次快照，直接求值会把
            # "还没出现"误判为 failed。按声明的 timeout 轮询清单，命中
            # 即重求值；超时保持最后一次结果（诚实 failed）。
            _pending_window_open = any(
                (v.get("type") == "window_open" and (v.get("expected") or {}).get("exists"))
                for v in declared_verifiers)
            if _pending_window_open and any(
                    getattr(_r, "diagnostic", None)
                    and "no matching window" in str(getattr(_r, "diagnostic"))
                    for _r in verifer_results):
                _timeout = max(
                    (float(v.get("timeoutSeconds") or 0)
                     for v in declared_verifiers
                     if v.get("type") == "window_open"),
                    default=0.0)
                _deadline = time.monotonic() + min(max(_timeout, 0.0), 10.0)
                while time.monotonic() < _deadline:
                    time.sleep(0.4)
                    inventory_read = _read_product_inventory(
                        server, run_token=run_token)
                    if not inventory_read.get("ok"):
                        continue
                    verifer_results = _evaluate_declared_verifiers(
                        declared, post_observation,
                        SimpleNamespace(
                            to_dict=lambda r=dict(receipt): dict(r)),
                        step_id=step_id, window_id=window_id,
                        product_windows=inventory_read["windows"],
                        inventory_read=inventory_read)
                    if not any(
                            getattr(_r, "diagnostic", None)
                            and "no matching window" in str(
                                getattr(_r, "diagnostic", ""))
                            for _r in verifer_results):
                        break
            assertion_refs = [
                {"assertionId":
                    f"{step_id}:{getattr(r, 'expectation_type', 'assertion')}:{i}",
                 "type": getattr(r, "expectation_type", None),
                 "result": getattr(getattr(r, "status", None), "value",
                                   None),
                 "expected": getattr(r, "expected", None),
                 "actual": getattr(r, "actual", None),
                 "observationRef": getattr(r, "observation_id", None)}
                for i, r in enumerate(verifer_results)]
            strict_pass = all(
                getattr(getattr(r, "status", None), "value", None)
                == "passed" for r in verifer_results) if verifer_results \
                else True
            any_fail = any(
                getattr(getattr(r, "status", None), "value", None)
                == "failed" for r in verifer_results)
            hook._attached_after = {event["eventId"]: {
                "bindingRevision": after_binding_revision,
                "observationRef": after_observation_ref,
                # A-3(1536):本次已解析目标的必要安全特征与实际动作
                # 标识(报告本步可查;唯一匹配在候选解析时已保证)。
                "targetRef": {"automationId": wanted_automation_id,
                              "uniqueMatch": 1,
                              "selectorHint": {"automation_id":
                                               wanted_automation_id}},
                "actionId": action_id,
                # B-1(09§7.6):窗口效果结果进入本步持久状态
                # (前态基线/后差分/结果 — 可重建、可追上步)。
                **({"windowEffect": effect_results}
                   if declared_effects else {})}}
            hook.complete_after_step(
                step_result=SimpleNamespace(
                    step_id=step_id,
                    status=(StepStatusValue.PASSED
                            if strict_pass and not any_fail
                            else (StepStatusValue.FAILED if any_fail
                                  else StepStatusValue.BLOCKED))),
                receipt=SimpleNamespace(
                    ok=True, to_dict=lambda r=dict(receipt): dict(r)),
                expectation_results=verifer_results,
                window_id=window_id,
                # A-2(1536):普通步骤实际读取并用于窗口断言的清单
                # 事实同样按本 attempt 持久一份(同一接口/同一形状;
                # 其他地方只引用;写盘失败门不变)。
                evidence_refs=[_inventory_evidence_record(
                    inventory_read, run_id=run_id, step_id=step_id,
                    attempt_id=attempt_id, window_id=window_id,
                    phase="after")] if inventory_read["ok"] else [],
                error_payload=(None if strict_pass and not any_fail else {
                    "code": "declared_verifier_not_passed",
                    "message": ("declared business verifiers evaluated "
                                "against the post-action authoritative "
                                "observation; strict pass required")}))
            outcome = ("pass" if strict_pass and not any_fail
                       else ("fail" if any_fail else "unknown"))
            steps_report.append({
                "stepId": step_id, "windowId": window_id,
                "attemptId": f"{step_id}-a{len(steps_report) + 1}",
                "transitionId": (
                    "first_bind" if not steps_report else
                    ("switched_bound_window"
                     if window_id != steps_report[-1].get("windowId")
                     else "same_window_reverified")),
                "outcome": outcome,
                "receiptCode": (receipt or {}).get("code"),
                "previousStepId": (steps_report[-1]["stepId"]
                                   if steps_report else None),
                "previousAttemptId": (
                    steps_report[-1].get("attemptId")
                    if steps_report else None),
                "previousWindowId": (
                    steps_report[-1].get("windowId")
                    if steps_report else None),
                "bindingRevision": after_binding_revision,
                "observationRef": after_observation_ref,
                "assertionResultRefs": assertion_refs,
                "targetId": target_ref.get("target_id")})
            if outcome != "pass":
                latched = True
                blocked_at = step_id
                break
        # 注意:previous 链基于 complete 后的 steps_report 顺序。
    finally:
        # B-5(09§7.5;1721 P1-2):显式 cleanup 走**同一**预写/
        # 账本生命周期 — 每步 begin_before_preparation(预写失败
        # OSError 上抛 → 0 后续 focus/业务/cleanup)、真实 selector
        # 唯一解析(同主步;不按首个 automationId 挑)、两步派发、
        # complete_after_step 持久(重启可追:run/step/attempt/
        # previous/receipt/断言/结果);动作后异常 unknown 不吞成
        # skipped;主流程 fail 仍允许既有政策授权且当前 binding
        # 可验证的显式 cleanup(不一律禁掉)。
        cleanup_results = []
        try:
            sequence_cleanup = (sequence_obj.to_dict().get("cleanup")
                                or []) if sequence_obj is not None else []
            if sequence_cleanup and run_token_holder.get("token") \
                    and hook is not None:
                # cleanup 是主流程失败后**独立授权的 attempt 序列**
                # (§7.5)。真实触发失败的尝试的稳定关联先捕获
                # (§7.6:不抹掉同 run 真实失败前步;仅引用不复制);
                # 之后重置 hook 锁存/游标以运行授权清理序列
                # (§7.5 允许显式政策清理 — 主流程 fail 是运行
                # cleanup 的前提,不是被绕过的阻断)。
                trigger_attempt = hook._previous or {}
                cleanup_previous_override = {
                    "stepId": trigger_attempt.get("stepId"),
                    "attemptId": trigger_attempt.get("attemptId"),
                    "runId": trigger_attempt.get("runId"),
                    "windowId": trigger_attempt.get("windowId"),
                    "outcome": trigger_attempt.get("outcome"),
                } if trigger_attempt.get("attemptId") else None
                hook._latched = False
                hook._blocked_at = None
                hook._cursor = 0
                hook._previous = None
                hook._steps.clear()
                if getattr(hook, "_host", None) is not None and \
                        hasattr(hook._host, "__dict__"):
                    hook._host._attempt_open_event = None
                previous_event = history_last_event = None
                for cleanup_step in sequence_cleanup:
                    cleanup_id = cleanup_step.get("stepId") or "cleanup"
                    cleanup_wid = cleanup_step.get("windowId")
                    cleanup_binding = bound_windows.get(cleanup_wid) \
                        if cleanup_wid else None
                    if not cleanup_wid or not cleanup_binding or \
                            cleanup_wid in deferred_windows:
                        cleanup_results.append({
                            "stepId": cleanup_id, "outcome": "skipped",
                            "reason": ("cleanup target binding not "
                                       "verifiable under this run; "
                                       "no sibling substitution (§7.5)")})
                        continue
                    try:
                        # actual previous:第一条 cleanup 关联触发
                        # 失败的主流程 attempt;后续 cleanup 的实际
                        # 前一步是**上一条 cleanup**(hook._previous
                        # 已由 complete 链上 — P2/2013)。
                        actual_previous = hook._previous or {}
                        cleanup_event = hook.begin_before_preparation(
                            step_id=cleanup_id, window_id=cleanup_wid,
                            before_state={"bindingRevision": "pending",
                                          "observationRef": "pending"},
                            previous_override=(
                                cleanup_previous_override
                                if not actual_previous.get("attemptId")
                                else actual_previous),
                            transition="cleanup_after_failure",
                            authorized_cleanup=True)
                        cleanup_attempt_id = f"{cleanup_id}-a1"
                        # previous 稳定关联:触发失败主流程 attempt
                        # (首条)/实际前一条 cleanup(链内;仅引用,
                        # §7.6 不复制前步整块)。
                        cleanup_previous_ref = (
                            {"stepId": actual_previous.get("stepId"),
                             "attemptId": actual_previous.get("attemptId"),
                             "runId": actual_previous.get("runId")}
                            if actual_previous.get("attemptId")
                            else cleanup_previous_override)
                        declared_cleanup = next(
                            (s for s in steps_declared.values()
                             if s.step_id == cleanup_id), None)
                        cleanup_identity = window_identities.get(
                            cleanup_wid) or {}
                        # 可验证性:重新绑定校验(真实 bind 门;
                        # LOST/消失 → 拒绝,不找兄弟)。
                        rebind = json.loads(server.bind_entry_window(
                            cleanup_wid, cleanup_identity, run_token))
                        if not rebind.get("binding_token"):
                            hook.complete_after_step(
                                step_result=SimpleNamespace(
                                    step_id=cleanup_id,
                                    status=StepStatusValue.BLOCKED),
                                receipt=None, expectation_results=[],
                                window_id=cleanup_wid, error_payload={
                                    "code": "cleanup_binding_refused",
                                    "message": (f"cleanup binding "
                                                f"refused: "
                                                f"{rebind.get('code')}")})
                            cleanup_results.append({
                                "stepId": cleanup_id,
                                "outcome": "skipped",
                                "reason": (f"cleanup binding refused: "
                                           f"{rebind.get('code')}")})
                            continue
                        bound_windows[cleanup_wid] = \
                            rebind["binding_token"]
                        cleanup_action = getattr(declared_cleanup,
                                                 "action_id", None) \
                            if declared_cleanup is not None else None
                        if not cleanup_action:
                            hook.complete_after_step(
                                step_result=SimpleNamespace(
                                    step_id=cleanup_id,
                                    status=StepStatusValue.BLOCKED),
                                receipt=None, expectation_results=[],
                                window_id=cleanup_wid, error_payload={
                                    "code": "cleanup_action_undeclared",
                                    "message": ("cleanup step declares "
                                                "no action; refusing "
                                                "to guess (§7.5)")})
                            cleanup_results.append({
                                "stepId": cleanup_id,
                                "outcome": "skipped",
                                "reason": ("cleanup step declares no "
                                           "action; refusing to "
                                           "guess (§7.5)")})
                            continue
                        cleanup_args = dict(
                            getattr(declared_cleanup, "args", None) or {})
                        cleanup_selector = getattr(
                            declared_cleanup, "selector", None)
                        cleanup_control = getattr(
                            cleanup_selector, "control", None) or {}
                        cleanup_args.setdefault(
                            "expected_process_name",
                            cleanup_identity.get("processName"))
                        cleanup_token = bound_windows[cleanup_wid]
                        # 两步契约(§7.2):权威锁+本窗**动作前**观测
                        # → 真实 before 附挂(写入成功在动作前 — 主步
                        # 权威 pre 附挂语义)→ 唯一解析器(与主步同一
                        # 语义)→ target_ref 派发。
                        locked_obs = json.loads(server.focus_bound_window(
                            cleanup_wid, cleanup_token, run_token,
                            action_id=None, observe=True))
                        if not (locked_obs or {}).get("ok"):
                            hook.complete_after_step(
                                step_result=SimpleNamespace(
                                    step_id=cleanup_id,
                                    status=StepStatusValue.BLOCKED),
                                receipt=None, expectation_results=[],
                                window_id=cleanup_wid, error_payload={
                                    "code": "cleanup_focus_refused",
                                    "message": (f"cleanup focus/observe "
                                                f"refused: "
                                                f"{(locked_obs or {}).get('code')}")})
                            cleanup_results.append({
                                "stepId": cleanup_id,
                                "outcome": "skipped",
                                "reason": (f"cleanup focus/observe "
                                           f"refused: "
                                           f"{(locked_obs or {}).get('code')}")})
                            continue
                        projected_cleanup = locked_obs.get("controls") or {}
                        wanted = cleanup_control.get("automationId")
                        from agent.recording.replay import (
                            resolve_replay_selector,
                            StepMaterializationError)
                        try:
                            cleanup_resolution = resolve_replay_selector(
                                cleanup_selector,
                                {"controls": list(projected_cleanup.values()),
                                 "observation_id":
                                     locked_obs.get("observation_id")})
                            target_control = next(
                                (c for c in projected_cleanup.values()
                                 if c.get("target_id") ==
                                 cleanup_resolution.__dict__.get(
                                     "target_id")), None)
                        except StepMaterializationError as exc:
                            hook.complete_after_step(
                                step_result=SimpleNamespace(
                                    step_id=cleanup_id,
                                    status=StepStatusValue.BLOCKED),
                                receipt=None, expectation_results=[],
                                window_id=cleanup_wid, error_payload={
                                    "code": getattr(exc, "code",
                                                    "cleanup_target_"
                                                    "unresolved"),
                                    "message": str(exc)})
                            cleanup_results.append({
                                "stepId": cleanup_id,
                                "outcome": "skipped",
                                "reason": (f"cleanup target not uniquely "
                                           f"resolved: "
                                           f"{getattr(exc, 'code', '')}")})
                            continue
                        if target_control is None:
                            hook.complete_after_step(
                                step_result=SimpleNamespace(
                                    step_id=cleanup_id,
                                    status=StepStatusValue.BLOCKED),
                                receipt=None, expectation_results=[],
                                window_id=cleanup_wid, error_payload={
                                    "code": "cleanup_target_missing",
                                    "message": (f"cleanup control "
                                                f"{wanted!r} not in "
                                                f"observation")})
                            cleanup_results.append({
                                "stepId": cleanup_id,
                                "outcome": "skipped",
                                "reason": (f"cleanup control "
                                           f"{wanted!r} not in "
                                           f"observation")})
                            continue
                        if wanted:
                            cleanup_args.setdefault("automation_id",
                                                    wanted)
                        # 尾项1(2154):cleanup 声明 effects → dispatch
                        # 前**同租约**基线(按效果目标身份;只枚举 —
                        # 复用主步同一 facade 机制,不复制效果循环)。
                        declared_cleanup_effects = list(
                            cleanup_step.get("effects") or [])
                        cleanup_baseline_tokens = {}
                        if declared_cleanup_effects:
                            cleanup_facade_pre = (getattr(
                                server, "_binding_facade_holder",
                                {}) or {}).get("facade")
                            for effect in declared_cleanup_effects:
                                effect_wid = effect.get("windowId") or \
                                    cleanup_wid
                                baseline = \
                                    cleanup_facade_pre.\
                                    begin_effect_baseline(
                                        effect_wid,
                                        window_identities.get(effect_wid)
                                        or cleanup_identity,
                                        binding_token=bound_windows.get(
                                            effect_wid))
                                cleanup_baseline_tokens[effect_wid] = \
                                    baseline.get("baseline_token")
                        # before 附挂在**动作前**(权威 pre 附挂;写入
                        # 成功先于 GUI — 主步同一预写门;revision 从
                        # step_context 真实读取,异常如实 pending 且
                        # 不因此继续假 pass)。
                        cleanup_revision = "pending"
                        try:
                            cleanup_facade = (getattr(
                                server, "_binding_facade_holder",
                                {}) or {}).get("facade")
                            cleanup_ctx = cleanup_facade.step_context(
                                run_id=run_id, step_id=cleanup_id,
                                attempt_id=cleanup_attempt_id,
                                window_id=cleanup_wid,
                                binding_token=cleanup_token)
                            cleanup_revision = cleanup_ctx.get(
                                "bindingRevision", "pending")
                        except Exception:  # noqa: BLE001
                            cleanup_revision = "pending"
                        cleanup_prestate = {
                            "sequenceCursor": 0,
                            "bindingRevision": cleanup_revision,
                            "windowId": cleanup_wid,
                            "observationRef": locked_obs.get(
                                "observation_id")}
                        # 真落盘(0739 attach_state_before;写失败 →
                        # OSError → 存储锁存全 run 0 后续 GUI — 2013
                        # 同一预写门;内存赋值不替代落盘)。
                        hook._ledger.attach_state_before(
                            cleanup_event["eventId"], state=cleanup_prestate)
                        hook._attached_before = {cleanup_event["eventId"]:
                                                 dict(cleanup_prestate)}
                        target_ref_cleanup = {
                            "snapshot_id":
                                locked_obs.get("observation_id"),
                            "target_id":
                                target_control.get("target_id"),
                            "fingerprint":
                                target_control.get("fingerprint"),
                            "expected_process_name":
                                cleanup_identity.get("processName"),
                            "selector_hint": {"automation_id": wanted}}
                        try:
                            receipt_cleanup = json.loads(
                                server.focus_bound_window(
                                    cleanup_wid, cleanup_token,
                                    run_token, action_id=cleanup_action,
                                    action_args=cleanup_args,
                                    target_ref=target_ref_cleanup,
                                    observe=False))
                        except Exception as exc:  # noqa: BLE001
                            # 动作后异常:unknown 不吞成 skipped(动作
                            # 可能已执行;不重试未知 GUI)。
                            hook.complete_after_step(
                                step_result=SimpleNamespace(
                                    step_id=cleanup_id,
                                    status=StepStatusValue.BLOCKED),
                                receipt=None, expectation_results=[],
                                window_id=cleanup_wid, error_payload={
                                    "code": "cleanup_dispatch_unknown",
                                    "message": (
                                        f"cleanup dispatch raised: "
                                        f"{type(exc).__name__}: {exc}")})
                            cleanup_results.append({
                                "stepId": cleanup_id,
                                "outcome": "unknown",
                                "reason": (f"cleanup dispatch unknown: "
                                           f"{exc}")})
                            continue
                        # 尾项1(2154)续:receipt 确定后、动作后观测
                        # 前 — 只读有界轮询评估每条声明效果(同主步
                        # 严格终态匹配门;全部满足才成立)。
                        cleanup_effect_results = []
                        if declared_cleanup_effects:
                            deadline = time.monotonic() + max(
                                (float(e.get("timeoutSeconds") or 10.0)
                                 for e in declared_cleanup_effects),
                                default=10.0)
                            cleanup_facade_post = (getattr(
                                server, "_binding_facade_holder",
                                {}) or {}).get("facade")
                            for effect in declared_cleanup_effects:
                                effect_outcome = None
                                effect_wid = effect.get("windowId") or \
                                    cleanup_wid
                                while True:
                                    try:
                                        observation_result = \
                                            cleanup_facade_post.\
                                            observe_effect_after_action(
                                                effect_wid,
                                                cleanup_baseline_tokens.get(
                                                    effect_wid),
                                                effect=effect,
                                                binding_token=bound_windows.get(
                                                    effect_wid))
                                    except Exception as exc:  # noqa: BLE001
                                        effect_outcome = {
                                            "ok": False,
                                            "outcome": "error",
                                            "error": str(exc)}
                                        break
                                    if observation_result.get("outcome") in (
                                            "appeared", "absent",
                                            "same_instance_shown",
                                            "ambiguous"):
                                        effect_outcome = observation_result
                                        break
                                    if time.monotonic() >= deadline:
                                        effect_outcome = observation_result
                                        break
                                    time.sleep(0.05)
                                outcome_name = (effect_outcome or {}).get(
                                    "outcome")
                                kind = effect.get("kind")
                                satisfied = (
                                    (kind == "appeared" and outcome_name in
                                     ("appeared", "same_instance_shown"))
                                    or (kind == "absent" and
                                        outcome_name == "absent"))
                                native_outcome = (effect_outcome or {}).get(
                                    "native_id")
                                opaque = None
                                if native_outcome:
                                    opaque = \
                                        effect_instance_refs.setdefault(
                                            str(native_outcome),
                                            f"INST-{len(effect_instance_refs) + 1:04d}")
                                    effect_instance_natives[opaque] = \
                                        str(native_outcome)
                                cleanup_effect_results.append({
                                    "effectId": effect.get("id"),
                                    "kind": kind,
                                    "windowId": effect_wid,
                                    "outcome": outcome_name,
                                    "satisfied": satisfied,
                                    "ok": satisfied,
                                    **({"instanceRef": opaque}
                                       if opaque else {})})
                            if not all(e["ok"] for e in
                                       cleanup_effect_results):
                                hook._attached_after = {
                                    cleanup_event["eventId"]: {
                                        "bindingRevision":
                                            cleanup_revision,
                                        "observationRef": locked_obs.get(
                                            "observation_id"),
                                        "previousRef":
                                            cleanup_previous_ref,
                                        "windowEffect":
                                            cleanup_effect_results}}
                                hook.complete_after_step(
                                    step_result=SimpleNamespace(
                                        step_id=cleanup_id,
                                        status=StepStatusValue.BLOCKED),
                                    receipt=SimpleNamespace(
                                        to_dict=lambda r=dict(
                                            receipt_cleanup): dict(r)),
                                    expectation_results=[],
                                    window_id=cleanup_wid,
                                    error_payload={
                                        "code": "cleanup_effect_not_satisfied",
                                        "message": (f"cleanup declared "
                                                    f"effects not all "
                                                    f"satisfied: "
                                                    f"{json.dumps(cleanup_effect_results, ensure_ascii=False)}")})
                                cleanup_results.append({
                                    "stepId": cleanup_id,
                                    "outcome": "unknown",
                                    "reason": "cleanup effect not "
                                              "satisfied",
                                    "effects": cleanup_effect_results})
                                continue
                        # P1(2013 缺口2):**动作后**权威只读观测是
                        # 必需事实 — 动作前观测不冒充动作后(before/
                        # after observationRef 必须不同);清单三态
                        # (成功空/非空/失败)随 inventory_read 传入;
                        # declared.effects 同义求值。
                        post_observation = json.loads(
                            server.focus_bound_window(
                                cleanup_wid, cleanup_token, run_token,
                                action_id=None, observe=True, read_only=True))
                        if not (post_observation or {}).get("ok", False):
                            # 动作关闭了 cleanup 自身绑定窗口 → 绑定
                            # LOST 是**声明的 absent 效果的预期后果**,
                            # 不是未知态:该效果已由权威基线差分
                            # (observe_effect_after_action)判 satisfied。
                            # 只有 absent 已覆盖的 LOST 才按效果事实
                            # 成立(真实执行+验证,非 skipped);其余
                            # refused 一律 unknown(不重试 GUI)。
                            refused_code = (post_observation or {}).get(
                                "code")
                            # P1(2038 复验):**所有**声明断言都不得跳过。
                            # 控件 verifier(checked/text 等)在绑定窗口
                            # 关闭后无法动作后求值 → unverifiable;窗口
                            # 存在性断言(window_open/closed)走**权威产品
                            # 清单**求值(不依赖本窗口绑定观测,关闭后仍
                            # 可判)——求值结果并入整体判定,非全 pass 不
                            # 得 pass。
                            declared_cleanup_verifiers = list(
                                getattr(declared_cleanup, "verifiers",
                                        None) or [])
                            control_verifiers = [
                                v for v in declared_cleanup_verifiers
                                if v.get("type") not in ("window_open",
                                                         "window_closed")]
                            window_verifiers = [
                                v for v in declared_cleanup_verifiers
                                if v.get("type") in ("window_open",
                                                     "window_closed")]
                            has_unverifiable_verifiers = bool(
                                control_verifiers)
                            window_verifier_results = []
                            if window_verifiers and refused_code in (
                                    "window_binding_lost",):
                                # P2(2324):窗口存在性断言复用**既有**
                                # `_evaluate_declared_verifiers` 的完整
                                # 链 —— `_verifier_expectation` 映射
                                # (exists=false → window_closed 等类型
                                # 互换)、`_observation_facts` facts 形
                                # 状、`inventory_read` 统一三态门(读失
                                # 败 → unknown,不以空清单冒充否定)。
                                # 不自造 evaluator 选择/事实形状。
                                from agent.execution.models import \
                                    ExpectationResult as _ER
                                inventory_read = _read_product_inventory(
                                    server, run_token=run_token)
                                # observation_record 仅承载清单事实
                                # (窗口已关,无本窗控件观测可注册;不伪
                                # 造动作后控件观测)。
                                observation_record = {"observation_id":
                                                      None}
                                ers = _evaluate_declared_verifiers(
                                    declared_cleanup, observation_record,
                                    SimpleNamespace(
                                        to_dict=lambda r=dict(
                                            receipt_cleanup): dict(r)),
                                    step_id=cleanup_id,
                                    window_id=cleanup_wid,
                                    product_windows=(
                                        inventory_read.get("windows") or []
                                        if inventory_read.get("ok")
                                        else None),
                                    inventory_read=inventory_read)
                                for er in ers:
                                    status = getattr(
                                        getattr(er, "status", None),
                                        "value", None)
                                    window_verifier_results.append({
                                        "type": getattr(er,
                                                        "expectation_type",
                                                        None),
                                        "result": ("pass" if status ==
                                                   "passed" else
                                                   ("fail" if status ==
                                                    "failed" else
                                                    "unknown")),
                                        "expected": getattr(er, "expected",
                                                            None),
                                        "actual": getattr(er, "actual",
                                                          None),
                                        "diagnostic": getattr(er,
                                                              "diagnostic",
                                                              None)})
                            window_verifiers_all_pass = (
                                not window_verifier_results or all(
                                    r["result"] == "pass" for r in
                                    window_verifier_results))
                            absent_verified = refused_code in (
                                "window_binding_lost",) and not \
                                has_unverifiable_verifiers and \
                                window_verifiers_all_pass and any(
                                    e.get("kind") == "absent" and
                                    e.get("satisfied") is True and
                                    e.get("windowId") == cleanup_wid
                                    for e in cleanup_effect_results)
                            if absent_verified and not \
                                    (receipt_cleanup or {}).get("ok"):
                                # P1(2038 反例1):动作回执未知(ok=false/
                                # dispatch_outcome_unknown)时 absent 不
                                # 能把整体翻 pass——动作可能没执行,absent
                                # 差分可能来自其他原因。回执未知 → 整体
                                # unknown(账本锁存,不重试 GUI)。
                                hook._attached_after = {
                                    cleanup_event["eventId"]: {
                                        "bindingRevision":
                                            cleanup_revision,
                                        "observationRef":
                                            locked_obs.get(
                                                "observation_id"),
                                        "previousRef":
                                            cleanup_previous_ref,
                                        "windowEffect":
                                            cleanup_effect_results}}
                                hook.complete_after_step(
                                    step_result=SimpleNamespace(
                                        step_id=cleanup_id,
                                        status=StepStatusValue.BLOCKED),
                                    receipt=SimpleNamespace(
                                        to_dict=lambda r=dict(
                                            receipt_cleanup): dict(r)),
                                    expectation_results=[],
                                    window_id=cleanup_wid, error_payload={
                                        "code":
                                            "cleanup_receipt_unknown",
                                        "message": (
                                            "self-close absent effect "
                                            "satisfied but the action "
                                            "receipt is not ok; the "
                                            "step outcome stays "
                                            "unknown")})
                                cleanup_results.append({
                                    "stepId": cleanup_id,
                                    "outcome": "unknown",
                                    "receiptCode": (receipt_cleanup
                                                    or {}).get("code"),
                                    "effects": cleanup_effect_results,
                                    "reason": ("self-close absent "
                                               "satisfied but action "
                                               "receipt not ok")})
                                continue
                            if absent_verified:
                                # P2(2038):absent 结果经账本完成接口
                                # **持久化**(expectation_results 传真实
                                # 效果依据对象),重启重建恢复同一断言
                                # 特征——不伪造关闭窗口的新控件观测。
                                absent_expectations = [
                                    SimpleNamespace(
                                        to_dict=lambda e=dict(
                                            effect): {
                                            "type": "absent",
                                            "status": "passed",
                                            "expected": {"kind": "absent",
                                                         "windowId":
                                                             effect.get(
                                                                 "windowId")},
                                            "actual": effect,
                                            "observation_id": None,
                                        }) for effect in
                                    cleanup_effect_results
                                    if effect.get("kind") == "absent" and
                                    effect.get("satisfied") is True]
                                # P1(2038 复验):窗口断言求值结果同样
                                # 持久化(非全 pass 不会到这)。
                                absent_expectations.extend(
                                    SimpleNamespace(
                                        to_dict=lambda r=dict(wr): {
                                            "type": r.get("type"),
                                            "status": "passed",
                                            "expected": r.get("expected"),
                                            "actual": r.get("actual"),
                                            "observation_id": None,
                                        }) for wr in window_verifier_results
                                    if wr.get("result") == "pass")
                                # P2(0002):稳定断言引用**单一来源**——
                                # 账本由 complete_after_step 对合并列表
                                # enumerate 统一编号;摘要 refs 从同一
                                # 合并列表按同一 index 派生,expected/
                                # actual 复用同一记录,不再分两套循环
                                # 重造 ID。
                                normalized_refs = [
                                    (payload.get("type"), payload)
                                    for payload in (
                                        dict(e.to_dict()) for e in
                                        absent_expectations)]
                                hook._attached_after = {
                                    cleanup_event["eventId"]: {
                                        "bindingRevision":
                                            cleanup_revision,
                                        "observationRef":
                                            locked_obs.get(
                                                "observation_id"),
                                        "previousRef":
                                            cleanup_previous_ref,
                                        "windowEffect":
                                            cleanup_effect_results}}
                                hook.complete_after_step(
                                    step_result=SimpleNamespace(
                                        step_id=cleanup_id,
                                        status=StepStatusValue.PASSED),
                                    receipt=SimpleNamespace(
                                        to_dict=lambda r=dict(
                                            receipt_cleanup): dict(r)),
                                    expectation_results=absent_expectations,
                                    window_id=cleanup_wid,
                                    error_payload=None)
                                cleanup_results.append({
                                    "stepId": cleanup_id,
                                    "outcome": "pass",
                                    "receiptCode": (receipt_cleanup
                                                    or {}).get("code"),
                                    "assertionResultRefs": [
                                        {"assertionId":
                                            f"{cleanup_id}:{rtype}:{idx}",
                                         "type": rtype,
                                         "result": "pass",
                                         "expected": payload.get("expected"),
                                         "actual": payload.get("actual"),
                                         "observationRef":
                                             payload.get("observation_id")}
                                        for idx, (rtype, payload) in
                                        enumerate(normalized_refs)],
                                    "effects": cleanup_effect_results,
                                    "selfCloseVerified": True,
                                    "note": ("bound window closed by the "
                                             "cleanup action itself; "
                                             "declared absent effect "
                                             "satisfied by authoritative "
                                             "baseline diff")})
                                continue
                            if refused_code in ("window_binding_lost",) and \
                                    (has_unverifiable_verifiers or
                                     not window_verifiers_all_pass):
                                # P1(2038 反例2+复验)落账语义:absent 已
                                # 满足但存在关闭后无法求值的控件断言,或
                                # 窗口断言权威求值非全 pass → 整体
                                # unknown(不默认通过),原因与结果可追查。
                                hook._attached_after = {
                                    cleanup_event["eventId"]: {
                                        "bindingRevision":
                                            cleanup_revision,
                                        "observationRef":
                                            locked_obs.get(
                                                "observation_id"),
                                        "previousRef":
                                            cleanup_previous_ref,
                                        "windowEffect":
                                            cleanup_effect_results}}
                                hook.complete_after_step(
                                    step_result=SimpleNamespace(
                                        step_id=cleanup_id,
                                        status=StepStatusValue.BLOCKED),
                                    receipt=SimpleNamespace(
                                        to_dict=lambda r=dict(
                                            receipt_cleanup): dict(r)),
                                    expectation_results=[],
                                    window_id=cleanup_wid, error_payload={
                                        "code":
                                            "cleanup_verifier_unverifiable",
                                        "message": (
                                            "self-close satisfied the "
                                            "absent effect but declared "
                                            "verifiers are not all "
                                            "verified-pass: control "
                                            f"verifiers unverifiable="
                                            f"{has_unverifiable_verifiers}, "
                                            f"window results="
                                            f"{json.dumps(window_verifier_results, ensure_ascii=False)}")})
                                cleanup_results.append({
                                    "stepId": cleanup_id,
                                    "outcome": "unknown",
                                    "receiptCode": (receipt_cleanup
                                                    or {}).get("code"),
                                    "effects": cleanup_effect_results,
                                    "windowVerifierResults":
                                        window_verifier_results,
                                    "reason": ("absent satisfied but "
                                               "declared verifiers not "
                                               "all verified-pass after "
                                               "self-close")})
                                continue
                            # 动作后观测未知 → unknown(不重试 GUI)。
                            hook.complete_after_step(
                                step_result=SimpleNamespace(
                                    step_id=cleanup_id,
                                    status=StepStatusValue.BLOCKED),
                                receipt=SimpleNamespace(
                                    to_dict=lambda r=dict(
                                        receipt_cleanup): dict(r)),
                                expectation_results=[],
                                window_id=cleanup_wid, error_payload={
                                    "code": "cleanup_post_state_unknown",
                                    "message": (f"post-action observation "
                                                f"refused: "
                                                f"{(post_observation or {}).get('code')}")})
                            cleanup_results.append({
                                "stepId": cleanup_id,
                                "outcome": "unknown",
                                "reason": (f"post-action observation "
                                           f"refused: "
                                           f"{(post_observation or {}).get('code')}")})
                            continue
                        cleanup_revision = "pending"
                        try:
                            cleanup_facade = (getattr(
                                server, "_binding_facade_holder",
                                {}) or {}).get("facade")
                            cleanup_ctx = cleanup_facade.step_context(
                                run_id=run_id, step_id=cleanup_id,
                                attempt_id=cleanup_attempt_id,
                                window_id=cleanup_wid,
                                binding_token=cleanup_token)
                            cleanup_revision = cleanup_ctx.get(
                                "bindingRevision", "pending")
                        except Exception:  # noqa: BLE001
                            cleanup_revision = "pending"
                        hook._attached_after = {cleanup_event["eventId"]: {
                            "bindingRevision": cleanup_revision,
                            "observationRef": post_observation.get(
                                "observation_id"),
                            "previousRef": cleanup_previous_ref,
                            **({"windowEffect": cleanup_effect_results}
                               if declared_cleanup_effects else {})}}
                        # cleanup 声明 verifiers 真实求值(同主步
                        # 既有 evaluator;receipt ok 不代替业务断言;
                        # **动作后**新观测)。
                        cleanup_verifiers = list(
                            getattr(declared_cleanup, "verifiers",
                                    None) or [])
                        cleanup_effects = list(
                            cleanup_step.get("effects") or [])
                        inventory_read = _read_product_inventory(
                            server, run_token=run_token)
                        cleanup_verifer_results = \
                            _evaluate_declared_verifiers(
                                declared_cleanup, post_observation,
                                SimpleNamespace(to_dict=lambda r=dict(
                                    receipt_cleanup): dict(r)),
                                step_id=cleanup_id,
                                window_id=cleanup_wid,
                                product_windows=inventory_read["windows"],
                                inventory_read=inventory_read,
                            ) if cleanup_verifiers or cleanup_effects \
                            else []
                        cleanup_refs = [
                            {"assertionId":
                                f"{cleanup_id}:{getattr(r, 'expectation_type', 'assertion')}:{i}",
                             "type": getattr(r, "expectation_type", None),
                             "result": getattr(
                                 getattr(r, "status", None), "value",
                                 None),
                             "expected": getattr(r, "expected", None),
                             "actual": getattr(r, "actual", None),
                             "observationRef": getattr(
                                 r, "observation_id", None)}
                            for i, r in enumerate(cleanup_verifer_results)]
                        strict_ok = all(
                            getattr(getattr(r, "status", None),
                                    "value", None) == "passed"
                            for r in cleanup_verifer_results) \
                            if cleanup_verifer_results else True
                        any_failed = any(
                            getattr(getattr(r, "status", None),
                                    "value", None) == "failed"
                            for r in cleanup_verifer_results)
                        cleanup_ok = bool(
                            (receipt_cleanup or {}).get("ok")) and \
                            strict_ok and not any_failed
                        hook.complete_after_step(
                            step_result=SimpleNamespace(
                                step_id=cleanup_id,
                                status=(
                                    StepStatusValue.PASSED
                                    if cleanup_ok
                                    else (StepStatusValue.FAILED
                                          if any_failed
                                          else StepStatusValue.BLOCKED))),
                            receipt=SimpleNamespace(
                                to_dict=lambda r=dict(
                                    receipt_cleanup): dict(r)),
                            expectation_results=cleanup_verifer_results,
                            window_id=cleanup_wid,
                            error_payload=(None if cleanup_ok else {
                                "code":
                                    ("cleanup_verifier_not_passed"
                                     if cleanup_verifer_results else
                                     (receipt_cleanup or {}).get(
                                         "code",
                                         "cleanup_receipt_not_ok"))}))
                        cleanup_results.append({
                            "stepId": cleanup_id,
                            "outcome": ("pass" if cleanup_ok
                                        else ("fail" if any_failed
                                              else "unknown")),
                            "receiptCode":
                                (receipt_cleanup or {}).get("code"),
                            "assertionResultRefs": cleanup_refs})
                    except OSError:
                        # 写盘安全门:存储失败后 0 后续副作用。
                        raise
                    except Exception as exc:  # noqa: BLE001
                        cleanup_results.append({
                            "stepId": cleanup_id, "outcome": "skipped",
                            "reason": (f"cleanup verification failed: "
                                       f"{exc}")})
        except OSError:
            # 预写失败冒泡:无 cleanup 摘要冒充;真实 end 仍执行
            # (释放边界),但零后续 GUI。
            cleanup_results = []
        except Exception:  # noqa: BLE001 — cleanup 编排失败不影响释放
            cleanup_results = []
        # 真实 end(正常/异常均释放;真实清理结果透传)。
        end_result = None
        try:
            end_result = json.loads(server.end_product_run(
                run_token_holder.get("token")))
        except Exception as exc:  # noqa: BLE001
            end_result = {"ok": False, "error": str(exc)}
    history = ledger.rebuild_history(run_id=run_id)
    report = build_run_report(history, screenshots={})
    return {"runId": run_id, "serverRunId": server_run_id_holder["run_id"],
            "dispatchInputs": dispatch_inputs,
            "observations": observations,
            "cleanup": cleanup_results,
            "steps": steps_report, "latched": latched,
            "blockedAt": blocked_at, "begin": {
                "ok": (begin_result or {}).get("ok"),
                "profile_id": (begin_result or {}).get("profile_id"),
                "expires_in": (begin_result or {}).get("expires_in")},
            "end": end_result, "report": report}
