"""Rerecord loop: record-before-act through a real RecordingService (design 12 §6).

The repair layer (trace_repair.py) rewrites an existing trace; this layer
is the second resort: the plan (step_plan.py) is executed by a constrained
agent **behind a recording that has already started** — the first business
input may only fire after the real RecordingService reports capture ready,
and every side effect lands through the existing dispatcher chain. After
the flow, the loop stops the recording durably, compiles the fresh raw
with the real compiler, and validates with a NEW run and NEW bindings via
the controlled v3 executor — receipt ok alone is never success, and the
expectations come from the original plan's recorded assertions, never
from what the rerecord happened to produce (§6.3 / R08).

Stage events are append-only and every terminal state carries the stage
that actually ran. A recording-start failure is a stage failure (R04),
not an "unrecovered trace" verdict; a stop/collect persistence failure
blocks compile/replay entirely (R06); a dispatch receipt unknown latches
the run — no re-click (R07); and the per-point reconciliation between the
plan and the fresh raw refuses to synthesize missing events (R05).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from agent.recording.compiler import compile_recording
from agent.recording.models import canonical_json
from agent.recording.step_plan import PlanStep, StepPlan
from target.recording.models import RawRecording

STAGE_PREFLIGHT = "preflight"
STAGE_RECORDING_STARTED = "recording_started"
STAGE_AGENT_EXECUTION = "agent_execution"
STAGE_STOP_AND_COLLECT = "stop_and_collect"
STAGE_FRESH_COMPILE = "fresh_compile"
STAGE_FRESH_REPLAY = "fresh_replay_and_assertions"

#: The dispatcher receipt code the unified dispatcher emits when an input
#: may already have been sent but the outcome did not land deterministically.
#: Locking the run there is R07's discipline: the point may have executed,
#: so re-clicking is forbidden — recovery belongs to a separate job that
#: first coordinates a safe initial state.
RECEIPT_UNKNOWN = "dispatch_outcome_unknown"


class RerecordStageError(Exception):
    """A stage failure with its stage and evidence; never rewritten into
    a different stage's outcome."""

    def __init__(self, stage: str, code: str, detail: str,
                 evidence: Mapping[str, Any] | None = None) -> None:
        super().__init__(f"{stage}: {code}: {detail}")
        self.stage = stage
        self.code = code
        self.detail = detail
        self.evidence = dict(evidence or {})


@dataclass
class RerecordJob:
    """Append-only stage/event journal for one bounded rerecord attempt."""
    plan_ref: str
    stages: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    budget_used: dict[str, int] = field(default_factory=lambda: {
        "rerecordAttempts": 0, "verificationReplays": 0})

    def record(self, stage: str, kind: str, payload: Mapping[str, Any]) -> None:
        self.events.append({"stage": stage, "kind": kind, **dict(payload)})

    def enter(self, stage: str) -> None:
        self.stages.append(stage)

    def outcome(self) -> str:
        """The honest terminal label for this job, per §7 vocabulary.

        Only a verified fresh replay is recovered_by_rerecord; a stage
        that did not run is listed, never traded for a failure verdict —
        environment/authorization gaps are not failure counts (§2)."""
        events = {e["kind"] for e in self.events}
        if "verification_passed" in events:
            return "recovered_by_rerecord"
        if "verification_failed" in events:
            return "unrecovered"
        if "reconciliation_failed" in events:
            return "unrecovered"
        if "recording_start_failed" in events:
            return "stage_failed"
        if "stop_failed" in events:
            return "stage_failed"
        if "dispatch_unknown" in events:
            return "unknown"
        if "compile_not_ready" in events:
            return "unrecovered"
        return "incomplete"


def _require_recording_active(service_status: Mapping[str, Any]) -> None:
    # Real session receipts report lowercase state values (session.py
    # SessionReceipt.to_dict → CaptureSessionState.value, "recording").
    if service_status.get("state") != "recording":
        raise RerecordStageError(
            STAGE_RECORDING_STARTED, "recording_not_active",
            f"capture not ready: state={service_status.get('state')!r}",
            evidence={"status": dict(service_status)})


def _execute_step(step: PlanStep, dispatcher: Callable[..., Mapping[str, Any]],
                  job: RerecordJob) -> dict[str, Any]:
    """Land one execute step through the real dispatcher and return the
    receipt record. Gap steps are skipped with their gap — never a guessed
    click; verify steps carry no action at all."""
    action_id = step.action.get("actionId")
    gap = step.action.get("gap")
    if step.intent == "capture-lifecycle":
        return {"stepId": step.step_id, "skipped": "capture_lifecycle"}
    if step.intent == "verify":
        return {"stepId": step.step_id, "skipped": "verify_expectation_only",
                "expectation": dict(step.expectation)}
    if action_id is None:
        return {"stepId": step.step_id, "skipped": "gap",
                "gap": dict(gap or {})}
    args = dict(step.action.get("paramSource") or {})
    causal = step.recording_marker.get("causalId")
    receipt = dispatcher(action_id, args=args, request_id=causal)
    if not isinstance(receipt, Mapping):
        raise RerecordStageError(
            STAGE_AGENT_EXECUTION, "dispatcher_receipt_invalid",
            f"step {step.step_id}: dispatcher returned "
            f"{type(receipt).__name__}")
    record = {"stepId": step.step_id, "actionId": action_id,
              "causalId": causal, "receipt": dict(receipt)}
    job.record(STAGE_AGENT_EXECUTION, "step_dispatched", record)
    code = receipt.get("code")
    if code == RECEIPT_UNKNOWN or receipt.get("ok") is False and code is None:
        raise RerecordStageError(
            STAGE_AGENT_EXECUTION, RECEIPT_UNKNOWN,
            f"step {step.step_id}: receipt outcome unknown; run latched, "
            "no re-click (R07)",
            evidence=record)
    if receipt.get("ok") is False:
        raise RerecordStageError(
            STAGE_AGENT_EXECUTION, "dispatch_refused",
            f"step {step.step_id}: dispatcher refused ({code})",
            evidence=record)
    return record


def reconcile_plan_against_raw(plan: StepPlan,
                               new_raw: RawRecording) -> dict[str, Any]:
    """Per-point accounting between the plan and the fresh raw (§6.2.6).

    The plan's causalIds are the ORIGINAL trace's correlation keys; the
    live correlator assigns its own fresh causal ids to the new capture
    (windows.py ``_causal_id``), so the reconciliation cannot be keyed on
    causal identity — it is an ORDERED alignment: the k-th plan step that
    intends a business action must be answered by the k-th business event
    of a compatible type in the new raw, and every raw business event must
    be claimed by some planned step (one planned gesture may legally fan
    out into several raw events; an unanswered planned step or an
    unclaimed raw event is reported). Window transitions are capture
    facts the flow's own actions produced, not planned inputs, so they
    count as evidence, not as unmatched business points.

    Missing points are reported, never synthesized: if the hook failed to
    capture an injection that really happened (R05), the reconciliation
    fails and no receipt may be promoted into raw."""
    _BUSINESS = {"pointer_click", "pointer_double_click", "text_commit",
                 "selection_change", "toggle_change", "scroll_commit",
                 "drag_commit", "key_command"}
    raw_business = [e for e in new_raw.events if e.type in _BUSINESS]
    raw_by_type: dict[str, list[int]] = {}
    for event in raw_business:
        raw_by_type.setdefault(event.type, []).append(event.sequence)
    planned_execute = [s for s in plan.steps if s.intent == "execute"
                       and s.action.get("actionId") is not None]
    _PLANNED_TYPE = {"gui.click": "pointer_click",
                     "gui.type_text": "text_commit",
                     "gui.select": "selection_change",
                     "pointer.scroll": "scroll_commit",
                     "pointer.drag": "drag_commit",
                     "pointer.double_click": "pointer_double_click",
                     "pointer.right_click": "pointer_click",
                     "pointer.middle_click": "pointer_click"}
    missing: list[dict[str, Any]] = []
    claimed: list[int] = []
    for step in planned_execute:
        raw_type = _PLANNED_TYPE.get(str(step.action.get("actionId")))
        pool = raw_by_type.get(raw_type, []) if raw_type else []
        candidate = next((seq for seq in pool if seq not in claimed), None)
        if candidate is None:
            missing.append({
                "stepId": step.step_id,
                "actionId": step.action.get("actionId"),
                "expectedRawType": raw_type,
                "originalCausalId": step.recording_marker.get("causalId")})
        else:
            claimed.append(candidate)
    unexpected = [seq for seq in range(1, len(new_raw.events) + 1)
                  if seq in {e.sequence for e in raw_business}
                  and seq not in claimed]
    transitions = [e.sequence for e in new_raw.events
                   if e.type == "window_transition"]
    return {"ok": not missing and not unexpected,
            "plannedExecuteSteps": len(planned_execute),
            "rawBusinessEvents": len(raw_business),
            "matched": [(step.step_id, claimed[i]) for i, step in
                        enumerate(planned_execute) if i < len(claimed)
                        and claimed[i] is not None] if claimed else [],
            "rawTransitionSequences": transitions,
            "missing": missing, "unexpected": unexpected}


def run_rerecord_loop(
    plan: StepPlan,
    *,
    service: Any,
    dispatcher: Callable[..., Mapping[str, Any]],
    output_root: Any,
    profile: str | None,
    result_store: Any,
    ledger_factory: Callable[[Any], Any],
    server_tools: Any,
    window_role_map: Mapping[str, str],
    scope_name: str,
    process_name: str,
    window_title: str,
    collect_recording_artifacts: Callable[..., Any] | None = None,
    artifact_agent: Any = None,
    job: RerecordJob | None = None,
) -> tuple[RerecordJob, dict[str, Any]]:
    """One bounded rerecord attempt (§6.2/§6.3): record-before-act, real
    stop/collect, fresh compile, independent new-run replay.

    Raises RerecordStageError at the first failed stage; the returned job
    records every stage that DID run. Zero business inputs precede a
    started recording; zero compile/replay calls follow a failed stop or
    collect."""
    if job is None:
        job = RerecordJob(plan_ref=plan.parent_sha256)
    if job.budget_used.get("rerecordAttempts", 0) >= 1:
        raise RerecordStageError(
            STAGE_PREFLIGHT, "budget_exhausted",
            "this job already consumed its one rerecord attempt")
    job.budget_used["rerecordAttempts"] += 1
    job.enter(STAGE_PREFLIGHT)

    # ---- recording_started: the gate before ANY business input (§6.2.1).
    job.enter(STAGE_RECORDING_STARTED)
    try:
        started = service.start(name=scope_name, process_name=process_name,
                                window_title=window_title)
    except Exception as exc:  # noqa: BLE001 — surfaced as a stage failure
        job.record(STAGE_RECORDING_STARTED, "recording_start_failed",
                   {"error": f"{type(exc).__name__}: {exc}"})
        raise RerecordStageError(
            STAGE_RECORDING_STARTED, "recording_start_failed",
            f"service.start raised: {exc}") from exc
    if not isinstance(started, Mapping) or not started.get("ok"):
        job.record(STAGE_RECORDING_STARTED, "recording_start_failed",
                   {"error": str((started or {}).get("error")
                                 if isinstance(started, Mapping)
                                 else started)})
        raise RerecordStageError(
            STAGE_RECORDING_STARTED, "recording_start_failed",
            "service.start did not confirm capture")
    session_id = started.get("sessionId")
    status = service.status()
    try:
        _require_recording_active(status)
    except RerecordStageError:
        # Leave the half-open session stopped so its partial capture is
        # drained and durable rather than abandoned mid-flight.
        try:
            service.stop()
        except Exception:  # noqa: BLE001 — the original error still surfaces
            pass
        raise
    job.record(STAGE_RECORDING_STARTED, "recording_ready",
               {"sessionId": session_id})

    # ---- agent_execution: constrained plan steps through the dispatcher.
    job.enter(STAGE_AGENT_EXECUTION)
    dispatch_records: list[dict[str, Any]] = []
    try:
        for step in plan.steps:
            record = _execute_step(step, dispatcher, job)
            dispatch_records.append(record)
            if record.get("receipt", {}).get("code") == RECEIPT_UNKNOWN:
                raise RerecordStageError(
                    STAGE_AGENT_EXECUTION, RECEIPT_UNKNOWN,
                    f"step {record.get('stepId')}: outcome unknown; "
                    "stopping further dispatch (R07)")
    except RerecordStageError as exc:
        job.record(STAGE_AGENT_EXECUTION, "dispatch_unknown" if
                   exc.code == RECEIPT_UNKNOWN else "dispatch_refused",
                   {"code": exc.code, "detail": exc.detail,
                    "evidence": exc.evidence,
                    "stepsAttempted": len(dispatch_records),
                    "stepsPlanned": len(plan.steps)})
        # §6.2.5: an agent failure still enters the stop-and-collect path.
        _stop_and_collect(
            job, service=service, result_store=result_store,
            output_root=output_root, profile=profile,
            collect_recording_artifacts=collect_recording_artifacts,
            artifact_agent=artifact_agent, session_id=session_id)
        raise
    job.record(STAGE_AGENT_EXECUTION, "execution_complete",
               {"dispatchedSteps": len(dispatch_records),
                "plannedSteps": len(plan.steps)})

    # ---- stop_and_collect: durable stop, then fresh compile input.
    artifacts, raw_recording, raw_dict = _stop_and_collect(
        job, service=service, result_store=result_store,
        output_root=output_root, profile=profile,
        collect_recording_artifacts=collect_recording_artifacts,
        artifact_agent=artifact_agent, session_id=session_id)

    # ---- per-point reconciliation BEFORE any validation replay (R05).
    reconciliation = reconcile_plan_against_raw(plan, raw_recording)
    if not reconciliation["ok"]:
        job.record(STAGE_AGENT_EXECUTION, "reconciliation_failed",
                   {"missing": reconciliation["missing"],
                    "unexpected": reconciliation["unexpected"]})
        raise RerecordStageError(
            STAGE_AGENT_EXECUTION, "reconciliation_failed",
            "plan/raw per-point accounting failed; raw events are not "
            "synthesizable from receipts (R05)",
            evidence={"missing": reconciliation["missing"],
                      "unexpected": reconciliation["unexpected"]})
    job.record(STAGE_AGENT_EXECUTION, "reconciliation_passed",
               {"plannedExecuteSteps": reconciliation["plannedExecuteSteps"],
                "rawBusinessEvents": reconciliation["rawBusinessEvents"]})

    # ---- fresh_compile: the REAL compiler on the durable raw.
    job.enter(STAGE_FRESH_COMPILE)
    result = compile_recording(raw_recording, profile=profile)
    job.record(STAGE_FRESH_COMPILE, "compile_finished",
               {"goldenStatus": result.golden.status,
                "issueCodes": [i.code for i in result.issues]})
    if result.golden.status != "ready":
        job.record(STAGE_FRESH_COMPILE, "compile_not_ready",
                   {"goldenStatus": result.golden.status})
        raise RerecordStageError(
            STAGE_FRESH_COMPILE, "compile_not_ready",
            f"fresh compile reached {result.golden.status!r}, not ready")
    job.budget_used["verificationReplays"] += 1

    # ---- fresh_replay_and_assertions: NEW run, NEW bindings (§6.3).
    job.enter(STAGE_FRESH_REPLAY)
    from agent.recording.execution_ledger import (
        _golden_window_identities, execute_v3_window_stage_real)
    # W→role is an explicit authorization input (§7.1); accept either a
    # fixed mapping or a resolver over the fresh golden's declared
    # identities (title regex → installed product role), never a guess.
    identities = _golden_window_identities(result.golden)
    resolved_roles = (dict(window_role_map) if isinstance(
        window_role_map, Mapping)
        else {wid: window_role_map(wid, identity)
              for wid, identity in identities.items()})
    ledger = ledger_factory(output_root)
    replay = execute_v3_window_stage_real(
        golden=result.golden, ledger=ledger, server_tools=server_tools,
        window_role_map=resolved_roles)
    assertion_rows = [r for e in ledger.rebuild_history(
        run_id=replay["runId"]) for r in (e.get("assertionResultRefs") or [])]
    failures = [r.get("assertionId") for r in assertion_rows
                if r.get("result") != "pass"]
    job.record(STAGE_FRESH_REPLAY, "verification_finished",
               {"runId": replay["runId"],
                "dispatchInputs": replay["dispatchInputs"],
                "latched": replay["latched"],
                "outcome": replay["report"]["outcome"],
                "assertionFailures": failures})
    if replay["latched"] or replay["report"]["outcome"] != "pass" \
            or failures:
        job.record(STAGE_FRESH_REPLAY, "verification_failed",
                   {"outcome": replay["report"]["outcome"],
                    "blockedAt": replay["blockedAt"],
                    "assertionFailures": failures})
        return job, {"reconciliation": reconciliation, "replay": replay,
                     "raw": raw_dict, "artifacts": artifacts,
                     "sessionId": session_id}
    job.record(STAGE_FRESH_REPLAY, "verification_passed",
               {"runId": replay["runId"]})
    return job, {"reconciliation": reconciliation, "replay": replay,
                 "raw": raw_dict, "artifacts": artifacts,
                 "sessionId": session_id}


def _stop_and_collect(
    job: RerecordJob, *, service: Any, result_store: Any, output_root: Any,
    profile: str | None,
    collect_recording_artifacts: Callable[..., Any] | None,
    artifact_agent: Any, session_id: Any,
) -> tuple[Any, RawRecording, dict[str, Any]]:
    """Real stop + durable read-back + optional artifact collection.

    Stop success and durable persistence are checked separately (R06):
    a stop whose resultStore failed leaves compile/replay uncalled. The
    compile input is the durable read-back, not the in-memory dict."""
    job.enter(STAGE_STOP_AND_COLLECT)
    try:
        stopped = service.stop()
    except Exception as exc:  # noqa: BLE001 — surfaced as a stage failure
        job.record(STAGE_STOP_AND_COLLECT, "stop_failed",
                   {"error": f"{type(exc).__name__}: {exc}"})
        raise RerecordStageError(
            STAGE_STOP_AND_COLLECT, "stop_failed",
            f"service.stop raised: {exc}") from exc
    if not isinstance(stopped, Mapping) or not stopped.get("ok"):
        detail = str((stopped or {}).get("error")
                     if isinstance(stopped, Mapping) else stopped)
        job.record(STAGE_STOP_AND_COLLECT, "stop_failed", {"error": detail})
        raise RerecordStageError(STAGE_STOP_AND_COLLECT, "stop_failed",
                                 f"stop did not complete: {detail}")
    store_info = stopped.get("resultStore")
    if store_info is not None and not store_info.get("ok"):
        job.record(STAGE_STOP_AND_COLLECT, "stop_failed",
                   {"error": "durable persistence failed",
                    "resultStore": dict(store_info)})
        raise RerecordStageError(
            STAGE_STOP_AND_COLLECT, "stop_failed",
            "durable result store did not persist the stopped recording "
            f"({store_info.get('error') or 'captureErrors present'})",
            evidence={"resultStore": dict(store_info)})
    job.record(STAGE_STOP_AND_COLLECT, "stopped",
               {"sessionId": session_id,
                "eventCount": len((stopped.get("recording") or {}).get(
                    "events") or [])})
    # Compile input: the DURABLE read-back (R06 / s5 discipline), not the
    # in-memory dict — a persistence layer that silently dropped the raw
    # must fail here, not compile from memory.
    try:
        raw_dict = result_store.load_stopped_recording(session_id)
    except Exception as exc:  # noqa: BLE001 — surfaced as a stage failure
        job.record(STAGE_STOP_AND_COLLECT, "collect_failed",
                   {"error": f"durable read-back failed: {exc}"})
        raise RerecordStageError(
            STAGE_STOP_AND_COLLECT, "collect_failed",
            f"durable read-back failed for {session_id}: {exc}") from exc
    if not isinstance(raw_dict, dict) or not raw_dict.get("events"):
        job.record(STAGE_STOP_AND_COLLECT, "collect_failed",
                   {"error": "durable read-back produced no raw events"})
        raise RerecordStageError(
            STAGE_STOP_AND_COLLECT, "collect_failed",
            f"durable read-back for {session_id} produced no raw events")
    raw_recording = RawRecording.from_dict(raw_dict)
    artifacts = None
    if collect_recording_artifacts is not None:
        stop_result = dict(stopped)
        artifacts, capture_issues = collect_recording_artifacts(
            artifact_agent, stop_result, output_root, profile,
            session_id=session_id)
        job.record(STAGE_STOP_AND_COLLECT, "collected",
                   {"artifacts": str(getattr(artifacts, "directory", "")),
                    "captureIssues": [dict(i) for i in capture_issues]})
    return artifacts, raw_recording, raw_dict


def plan_parent_digest(plan: StepPlan) -> str:
    """Stable parent reference for the job record (origin artifact hash)."""
    return plan.parent_sha256


def raw_sha256(recording: RawRecording) -> str:
    import hashlib
    return "sha256:" + hashlib.sha256(
        canonical_json(recording.to_dict()).encode()).hexdigest()


__all__ = [
    "RerecordJob",
    "RerecordStageError",
    "RECEIPT_UNKNOWN",
    "reconcile_plan_against_raw",
    "run_rerecord_loop",
    "plan_parent_digest",
    "raw_sha256",
]
