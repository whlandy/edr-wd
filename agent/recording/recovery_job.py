"""Recovery job layer (design 12 §3/§7/§10): persistence, budget across
wake-ups, exclusive lease, crash rebuild, and the honest outcome synthesis.

The repair layer (trace_repair.py) derives bounded candidates and the
rerecord loop (rerecord_loop.py) runs the record-before-act flow — but
neither survives a process restart, and neither can refuse a second
controller on the same tree. This layer is the *job* around both: its
state is a durable JSON document with an append-only event list, the
budget counters live in that document (a 20-minute scheduled wake-up
that re-enters with the same recoveryJobId resumes the same budget,
never a fresh one), and a fresh lease declines any other controller.

The terminal label is synthesized from BOTH layers' actual evidence
(§7): ``unrecovered`` requires real two-layer failure evidence — a
repair layer that produced candidates that failed validation or a
provable no-candidate analysis, AND a rerecord/replay that failed at a
concrete point. A layer that never ran is listed as never run, never
traded for a failure verdict; blocked / awaiting_authorization / unknown
stay what they are (§2: environment, authorization, and receipt-unknown
latches are not attempt failures), and an interrupted in-flight rerecord
is ``unknown`` — its inputs may have landed, so it is never re-fired.
"""
from __future__ import annotations

import json
import time
from typing import Any, Callable, Mapping

from agent.recording.rerecord_loop import (
    RerecordJob,
    RerecordStageError,
    run_rerecord_loop,
)

#: A lease older than this is stale: the controller that took it is
#: presumed gone (crashed), and the job may be re-entered (design §10:
#: the fresh-mark window for the same tree).
LEASE_FRESH_SECONDS = 40 * 60

#: Budget ceilings per recovery job (design §1/§3): one bounded repair
#: search (candidates themselves bounded by trace_repair.CANDIDATE_LIMIT),
#: one full agent rerecord, one verification replay of the fresh
#: recording. The counters are persisted so a scheduled re-wake cannot
#: spend them twice.
BUDGET_LIMITS = {"repairSearches": 1, "rerecordAttempts": 1,
                 "verificationReplays": 1}

JOB_OUTCOME_RECOVERED_REPAIR = "recovered_by_repair"
JOB_OUTCOME_RECOVERED_RERECORD = "recovered_by_rerecord"
JOB_OUTCOME_UNRECOVERED = "unrecovered"
JOB_OUTCOME_BLOCKED = "blocked"
JOB_OUTCOME_AWAITING_AUTHORIZATION = "awaiting_authorization"
JOB_OUTCOME_UNKNOWN = "unknown"
JOB_OUTCOME_DECLINED = "declined_busy"


class RecoveryJobError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> float:
    return time.time()


class RecoveryJob:
    """One recovery job: durable state + append-only events + lease.

    The state document is the ONLY authority on what has run. Rebuilding
    from it after a crash replays nothing: completed stages are skipped,
    budget counters keep their persisted values, and an in-flight stage
    that never reached its done event is handled per its semantics (an
    interrupted rerecord is ``unknown``, never silently re-fired)."""

    def __init__(self, state_path, *, recovery_job_id: str,
                 origin: Mapping[str, Any] | None = None,
                 failure_category: str = "trace_defect",
                 failure_reason: str = "") -> None:
        self.state_path = json.loads(json.dumps(str(state_path)))
        self.recovery_job_id = recovery_job_id
        doc = self._load_or_init(origin or {}, failure_category,
                                 failure_reason)
        self._doc = doc

    # ------------------------------------------------------------ document

    def _load_or_init(self, origin: Mapping[str, Any], failure_category: str,
                      failure_reason: str) -> dict[str, Any]:
        from pathlib import Path
        path = Path(self.state_path)
        if path.exists():
            doc = json.loads(path.read_text(encoding="utf-8"))
            if doc.get("recoveryJobId") != self.recovery_job_id:
                raise RecoveryJobError(
                    "job_id_mismatch",
                    f"state file holds job {doc.get('recoveryJobId')!r}, "
                    f"not {self.recovery_job_id!r}")
            doc.setdefault("events", [])
            return doc
        doc = {
            "recoveryJobId": self.recovery_job_id,
            "originArtifactRef": dict(origin or {}),
            "failureCategory": failure_category,
            "failureReason": failure_reason,
            "stage": "created",
            "budgetUsed": {k: 0 for k in BUDGET_LIMITS},
            "repairCandidateRefs": [],
            "planRef": None,
            "newRecordingRef": None,
            "validationRunRef": None,
            "lease": None,
            "events": [],
            "outcome": None,
        }
        self._persist(doc)
        return doc

    def _persist(self, doc: dict[str, Any]) -> None:
        from pathlib import Path
        path = Path(self.state_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1,
                                  sort_keys=True), encoding="utf-8")
        tmp.replace(path)

    # ------------------------------------------------------------- events

    def record_event(self, stage: str, kind: str,
                     payload: Mapping[str, Any] | None = None) -> None:
        self._doc["events"].append({
            "stage": stage, "kind": kind,
            "at": round(_now(), 3), **dict(payload or {})})
        self._persist(self._doc)

    def stage_done(self, stage: str, refs: Mapping[str, Any] | None = None
                   ) -> None:
        self._doc["stage"] = stage
        self.record_event(stage, "stage_done", dict(refs or {}))

    def has_event(self, kind: str, stage: str | None = None) -> bool:
        return any(e["kind"] == kind and (stage is None or
                                          e["stage"] == stage)
                   for e in self._doc["events"])

    # -------------------------------------------------------------- lease

    def acquire_lease(self, owner: str, round_id: str) -> bool:
        """Exclusive control for the same tree (§10). A fresh lease held
        by another controller declines this entry — recorded, zero GUI
        contact. Our own fresh lease re-enters (same controller); a stale
        lease (holder presumed dead) is retaken with a note."""
        lease = self._doc.get("lease")
        if lease is not None:
            age = _now() - float(lease.get("acquiredAt", 0))
            if age < LEASE_FRESH_SECONDS:
                if lease.get("owner") != owner:
                    self.record_event(
                        "lease", "lease_declined",
                        {"by": owner, "roundId": round_id,
                         "heldBy": lease.get("owner"),
                         "heldForSeconds": round(age, 1)})
                    return False
                return True
            self.record_event(
                "lease", "lease_stale_retaken",
                {"previousOwner": lease.get("owner"),
                 "ageSeconds": round(age, 1)})
        self._doc["lease"] = {"owner": owner, "roundId": round_id,
                              "acquiredAt": _now()}
        self.record_event("lease", "lease_acquired",
                          {"owner": owner, "roundId": round_id})
        return True

    def release_lease(self) -> None:
        if self._doc.get("lease") is not None:
            self.record_event("lease", "lease_released", {})
            self._doc["lease"] = None
            self._persist(self._doc)

    # ------------------------------------------------------------- budget

    def spend(self, key: str) -> None:
        if key not in BUDGET_LIMITS:
            raise RecoveryJobError("unknown_budget_key", key)
        used = int(self._doc["budgetUsed"].get(key, 0))
        if used >= BUDGET_LIMITS[key]:
            raise RecoveryJobError(
                "budget_exhausted",
                f"{key} already spent {used}/{BUDGET_LIMITS[key]} on job "
                f"{self.recovery_job_id}")
        self._doc["budgetUsed"][key] = used + 1
        self._persist(self._doc)

    def budget_used(self) -> dict[str, int]:
        return dict(self._doc["budgetUsed"])

    def sync_budget(self, values: Mapping[str, int]) -> None:
        """Persist observed budget facts from a layer's own accounting
        (e.g. the rerecord loop's verificationReplays)."""
        for k, v in values.items():
            if k in BUDGET_LIMITS:
                self._doc["budgetUsed"][k] = max(
                    int(self._doc["budgetUsed"].get(k, 0)), int(v))
        self._persist(self._doc)

    # -------------------------------------------------------------- refs

    def set_ref(self, key: str, value: Any) -> None:
        if key not in {"repairCandidateRefs", "planRef", "newRecordingRef",
                       "validationRunRef"}:
            raise RecoveryJobError("unknown_ref_key", key)
        self._doc[key] = value
        self._persist(self._doc)

    def ref(self, key: str) -> Any:
        return self._doc.get(key)

    # ----------------------------------------------------------- outcome

    def set_outcome(self, outcome: str, reason: str,
                    refs: Mapping[str, Any] | None = None) -> None:
        self._doc["outcome"] = {"label": outcome, "reason": reason,
                                "refs": dict(refs or {})}
        self.record_event("outcome", "outcome_set",
                          {"label": outcome, "reason": reason})

    def outcome(self) -> str | None:
        oc = self._doc.get("outcome")
        return oc.get("label") if isinstance(oc, Mapping) else None


def synthesize_outcome(
    *, repair_attempted: bool,
    repair_candidates: int,
    repair_refusals: int,
    repair_validated: bool | None,
    rerecord_outcome: str | None,
    blocked_reason: str | None = None,
) -> tuple[str, str]:
    """Honest job-level label from both layers' actual evidence (§7).

    unrecovered needs BOTH: evidence-bounded repair work (candidates
    that failed real validation, or an explicit no-provable-candidate
    analysis) AND a rerecord/replay that failed at a concrete point.
    Environment and authorization gaps cannot make up failure counts
    (§2/R10); a layer that never ran is reported as never run (R09
    counter-case); a receipt-unknown latch is unknown, not unrecovered."""
    if blocked_reason:
        lowered = blocked_reason.lower()
        if "authorization" in lowered or "授权" in blocked_reason:
            return JOB_OUTCOME_AWAITING_AUTHORIZATION, blocked_reason
        if "busy" in lowered or "忙" in blocked_reason:
            return JOB_OUTCOME_BLOCKED, blocked_reason
        return JOB_OUTCOME_UNKNOWN, blocked_reason
    if not repair_attempted and rerecord_outcome is None:
        return JOB_OUTCOME_BLOCKED, "neither layer ran"
    if rerecord_outcome == "recovered_by_rerecord":
        return JOB_OUTCOME_RECOVERED_RERECORD, "validation replay passed"
    if rerecord_outcome == "unknown":
        return JOB_OUTCOME_UNKNOWN, (
            "rerecord outcome unknown (receipt latch); recovery belongs "
            "to a separate job after a safe initial state (R07)")
    if rerecord_outcome in {"unrecovered", "stage_failed", "incomplete"}:
        if not repair_attempted:
            return JOB_OUTCOME_BLOCKED, (
                "rerecord layer failed but the repair layer never ran; "
                "two-layer failure evidence is incomplete (R09)")
        if repair_candidates == 0 and repair_refusals > 0:
            return JOB_OUTCOME_UNRECOVERED, (
                "both layers failed with evidence: repair layer has a "
                "no-provable-candidate analysis; rerecord layer "
                f"{rerecord_outcome}")
        if repair_candidates > 0 and repair_validated is False:
            return JOB_OUTCOME_UNRECOVERED, (
                "both layers failed with evidence: repair candidates "
                "failed real validation; rerecord layer "
                f"{rerecord_outcome}")
        if repair_candidates > 0 and repair_validated is None:
            return JOB_OUTCOME_UNRECOVERED, (
                "repair candidates generated but real validation not "
                f"performed (listed as gap); rerecord layer "
                f"{rerecord_outcome}")
        return JOB_OUTCOME_UNKNOWN, (
            "rerecord layer failed; repair layer ran without a failure "
            "verdict on record")
    if repair_validated is False:
        return JOB_OUTCOME_BLOCKED, (
            "repair candidates failed real validation; rerecord layer "
            "never ran — job stops here, rerecord still available")
    return JOB_OUTCOME_BLOCKED, "no terminal evidence from either layer"


def execute_recovery(
    job: RecoveryJob,
    *,
    recording: Any,
    compile_result: Any,
    owner: str,
    round_id: str,
    propose_repairs_fn: Callable[..., Any],
    validate_candidate: Callable[[Any], bool] | None = None,
    build_plan_fn: Callable[..., Any],
    rerecord_fn: Callable[..., Any] = run_rerecord_loop,
    rerecord_kwargs: Mapping[str, Any] | None = None,
    blocked_reason: str | None = None,
) -> tuple[RecoveryJob, dict[str, Any]]:
    """Drive one recovery job through both layers (§1 order), with
    crash-safe re-entry: a job that already has an outcome is returned
    untouched; an interrupted rerecord is unknown, never re-fired; a
    fresh lease held elsewhere declines the entry (R10/R11)."""
    rerecord_kwargs = dict(rerecord_kwargs or {})
    if job.outcome() is not None:
        return job, {"alreadyTerminal": True}
    if not job.acquire_lease(owner, round_id):
        job.set_outcome(
            JOB_OUTCOME_DECLINED,
            "another controller holds a fresh lease on this job/tree")
        return job, {"declined": True}
    try:
        if blocked_reason:
            label, reason = synthesize_outcome(
                repair_attempted=False, repair_candidates=0,
                repair_refusals=0, repair_validated=None,
                rerecord_outcome=None, blocked_reason=blocked_reason)
            job.stage_done("blocked", {"reason": reason})
            job.set_outcome(label, reason)
            return job, {"blocked": reason}

        # An in-flight rerecord from a previous process: its inputs may
        # have landed — unknown, manual safe-state coordination first.
        if job.has_event("rerecord_started") and \
                not job.has_event("stage_done", "rerecord"):
            job.set_outcome(
                JOB_OUTCOME_UNKNOWN,
                "previous run was interrupted mid-rerecord; inputs may "
                "have landed — coordinate a safe initial state in a new "
                "job before any re-dispatch (R07/R11)")
            return job, {"interrupted": True}

        # ---- layer 1: bounded repair search (static derivation here;
        # real replay validation via validate_candidate when provided).
        if not job.has_event("stage_done", "repair"):
            if job.budget_used().get("repairSearches", 0) == 0:
                job.spend("repairSearches")
            proposal = propose_repairs_fn(
                recording, compile_result.golden, compile_result.diagnostics)
            candidates = list(getattr(proposal, "candidates", ()) or [])
            refusals = list(getattr(proposal, "refusals", ()) or [])
            job.set_ref("repairCandidateRefs", [
                {"ruleId": c.rule_id, "parentSha256": c.parent_sha256,
                 "semanticsChanged": c.semantics_changed}
                for c in candidates])
            job.record_event("repair", "repair_proposal", {
                "candidates": len(candidates), "refusals": len(refusals),
                "refusalReasons": [r.reason for r in refusals]})
            repair_validated: bool | None = None
            if candidates:
                if validate_candidate is None:
                    # ready-by-compile alone is never success (§4):
                    # validation not performed — recorded as a gap.
                    job.record_event("repair", "validation_not_performed", {
                        "candidates": len(candidates)})
                else:
                    repair_validated = bool(validate_candidate(candidates[0]))
                    job.record_event("repair", "candidate_validated", {
                        "validated": repair_validated})
            job.stage_done("repair", {"candidates": len(candidates),
                                      "refusals": len(refusals),
                                      "validated": repair_validated})
            candidates_n, refusals_n = len(candidates), len(refusals)
            if repair_validated is True:
                job.stage_done("verification", {"via": "repair_candidate"})
                job.set_outcome(
                    JOB_OUTCOME_RECOVERED_REPAIR,
                    "repair candidate passed real replay validation")
                return job, {"recoveredBy": "repair"}
        else:
            done = [e for e in job._doc["events"]
                    if e["kind"] == "stage_done" and e["stage"] == "repair"]
            last = done[-1] if done else {}
            candidates_n = int(last.get("candidates", 0))
            refusals_n = int(last.get("refusals", 0))
            repair_validated = last.get("validated")
            proposal = None  # rebuilt from events; no re-derivation
            if repair_validated is True:
                job.set_outcome(
                    JOB_OUTCOME_RECOVERED_REPAIR,
                    "repair candidate passed real replay validation")
                return job, {"recoveredBy": "repair"}
        repair_attempted = True

        # ---- layer 2 preparation: full-point step plan (§1: 修复不成→
        # 梳理每个操作点；不重复已完成的提取).
        if not job.has_event("stage_done", "step_plan"):
            from agent.recording.step_plan import extract_step_plan
            plan = build_plan_fn(recording, compile_result)
            job.set_ref("planRef", {
                "parentSha256": plan.parent_sha256,
                "steps": len(plan.steps), "gaps": len(plan.gaps),
                "uncompiledPoints": plan.uncompiled_points})
            job.stage_done("step_plan", {"steps": len(plan.steps),
                                         "gaps": len(plan.gaps)})
        else:
            plan_ref = job.ref("planRef") or {}
            plan = None

        # ---- layer 2: the rerecord (never re-fired once started).
        if not job.has_event("rerecord_started"):
            if job.budget_used().get("rerecordAttempts", 0) == 0:
                job.spend("rerecordAttempts")
            job.record_event("rerecord", "rerecord_started", {})
            rerecord_job = RerecordJob(
                plan_ref=(job.ref("planRef") or {}).get("parentSha256", ""))
            try:
                _, detail = rerecord_fn(plan, job=rerecord_job,
                                        **rerecord_kwargs)
            except RerecordStageError as exc:
                job.sync_budget(rerecord_job.budget_used)
                job.record_event("rerecord", "rerecord_failed", {
                    "stage": exc.stage, "code": exc.code,
                    "detail": exc.detail})
                job.stage_done("rerecord", {"outcome":
                                            rerecord_job.outcome()})
                label, reason = synthesize_outcome(
                    repair_attempted=repair_attempted,
                    repair_candidates=candidates_n,
                    repair_refusals=refusals_n,
                    repair_validated=repair_validated,
                    rerecord_outcome=rerecord_job.outcome())
                job.set_outcome(label, reason)
                return job, {"rerecordError": {"stage": exc.stage,
                                               "code": exc.code}}
            job.sync_budget(rerecord_job.budget_used)
            job.set_ref("newRecordingRef", {
                "sessionId": detail.get("sessionId"),
                "rawSha256": _raw_digest(detail)})
            job.set_ref("validationRunRef", {
                "runId": (detail.get("replay") or {}).get("runId")})
            outcome = rerecord_job.outcome()
            job.stage_done("rerecord", {"outcome": outcome})
            label, reason = synthesize_outcome(
                repair_attempted=repair_attempted,
                repair_candidates=candidates_n, repair_refusals=refusals_n,
                repair_validated=repair_validated,
                rerecord_outcome=outcome)
            job.set_outcome(label, reason)
            return job, {"rerecord": {"outcome": outcome}}
        return job, {"unexpectedState": True}
    finally:
        if job.outcome() is not None:
            job.release_lease()


def _raw_digest(detail: Mapping[str, Any]) -> str | None:
    raw = detail.get("raw")
    if isinstance(raw, Mapping) and raw.get("events") is not None:
        from agent.recording.models import canonical_json
        import hashlib
        return "sha256:" + hashlib.sha256(
            canonical_json(raw).encode()).hexdigest()
    return None


def recovery_report(state: Mapping[str, Any]) -> dict[str, Any]:
    """The full recovery report (§7): per-stage run/not-run, budget
    table, stable refs — no nested history dumps, no claiming what did
    not run."""
    events = list(state.get("events") or [])
    stages_run: list[str] = []
    for e in events:
        if e["kind"] == "stage_done" and e["stage"] not in stages_run:
            stages_run.append(e["stage"])
    not_run = [s for s in ("repair", "step_plan", "rerecord",
                           "verification") if s not in stages_run]
    oc = state.get("outcome") or {}
    return {
        "recoveryJobId": state.get("recoveryJobId"),
        "failureCategory": state.get("failureCategory"),
        "failureReason": state.get("failureReason"),
        "outcome": oc.get("label"),
        "outcomeReason": oc.get("reason"),
        "budgetUsed": dict(state.get("budgetUsed") or {}),
        "budgetLimits": dict(BUDGET_LIMITS),
        "stagesRun": stages_run,
        "stagesNotRun": not_run,
        "refs": {
            "originArtifactRef": state.get("originArtifactRef"),
            "repairCandidateRefs": state.get("repairCandidateRefs"),
            "planRef": state.get("planRef"),
            "newRecordingRef": state.get("newRecordingRef"),
            "validationRunRef": state.get("validationRunRef"),
        },
    }


__all__ = [
    "BUDGET_LIMITS",
    "LEASE_FRESH_SECONDS",
    "RecoveryJob",
    "RecoveryJobError",
    "execute_recovery",
    "recovery_report",
    "synthesize_outcome",
]
