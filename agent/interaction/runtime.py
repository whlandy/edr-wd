"""One executor for live actions and product intent assertions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from typing import Any, Mapping

from agent.execution import AtomicExecutor, BackendUnavailable, StepMaterializationError
from agent.execution.confirmation import ExecutionContext
from agent.locator import HybridLocator, LocatorError, LocatorPolicy, LocatorRequest
from agent.products.intent import IntentRequest, validate_intent_request
from agent.recording.models import ReplaySelector
from agent.recording.visual import SafeVisualResolver
from target.protocol_models import AtomicTestStep, Expectation, TargetRef, TestCase


@dataclass(frozen=True)
class IntentRunResult:
    action_result: Any | None
    assertion_result: Any
    skipped_click: bool

    @property
    def passed(self) -> bool:
        return ((self.action_result is None or self.action_result.outcome.value == "passed")
                and self.assertion_result.outcome.value == "passed")


def execute_live_action(request: LocatorRequest, *, executor: AtomicExecutor,
                        profile: str, expectations: tuple[Expectation, ...] = (),
                        locator: HybridLocator | None = None,
                        policy: LocatorPolicy | None = None,
                        visual_candidates: tuple[Mapping, ...] = (),
                        visual_selector: Mapping | None = None,
                        visual_resolver: SafeVisualResolver | None = None) -> Any:
    """Materialize from each step's fresh observation, then use AtomicExecutor."""
    locator = locator or HybridLocator()
    if visual_selector is not None and visual_resolver is None:
        raise ValueError("live_visual_resolver_missing")
    policy = policy or LocatorPolicy(mode="semantic_first" if visual_selector is not None else "semantic_only")
    step = AtomicTestStep(
        step_id="LIVE-1", step_no=1, title=request.action_id,
        action_id=request.action_id, args={}, required=True,
        expectations=expectations or (Expectation(type="action_ok", value=True),),
    )

    def materialize(current: AtomicTestStep, observation: Mapping) -> AtomicTestStep:
        current_visual = visual_candidates
        if visual_selector is not None:
            selector = ReplaySelector(request.window, request.control, visual_selector)
            current_visual = (*current_visual, *visual_resolver.collect_evidence(selector, observation))
        try:
            result = locator.resolve(request, observation, policy,
                                     visual_candidates=tuple(current_visual),
                                     ocr_boxes=tuple(observation.get("ocr_boxes") or ()))
        except LocatorError as exc:
            raise StepMaterializationError(exc.code, str(exc),
                                           details={"candidate_count": exc.candidate_count}) from exc
        if result.candidate is None:
            if result.method != "visual" or result.point is None:
                raise StepMaterializationError("locator_no_semantic_target",
                                               "live action has no verified target")
            window = observation.get("window_identity") or {}
            rect = window.get("rect") or (observation.get("active_window") or {}).get("rect")
            if not isinstance(rect, (list, tuple)) or len(rect) != 4:
                raise StepMaterializationError("visual_window_unverified",
                                               "locked window rectangle is unavailable")
            return replace(current, action_id="pointer.click_window", args={
                "x": result.point[0] - int(rect[0]),
                "y": result.point[1] - int(rect[1]),
                "expected_process_name": str(request.window.get("processName") or ""),
            })
        candidate = result.candidate
        ref = TargetRef(
            snapshot_id=result.observation_id, target_id=candidate.target_id,
            expected_process_name=str(request.window.get("processName") or ""),
            fingerprint=candidate.source.get("fingerprint"),
            selector_hint=dict(request.control),
        )
        args = {}
        if request.action_id in {"gui.click", "gui.click_target"}:
            for source, output in (("automationId", "automation_id"),
                                   ("name", "text"), ("controlType", "control_type")):
                if request.control.get(source):
                    args[output] = request.control[source]
            args["expected_process_name"] = ref.expected_process_name
        return replace(current, target_ref=ref, args=args)

    case = TestCase(case_id="LIVE-" + uuid.uuid4().hex[:12], title=request.action_id,
                    profiles=(profile,), steps=(step,))
    return executor.run_case(case, step_materializer=materialize,
                             execution_context=ExecutionContext(profile=profile))


def execute_intent(request: IntentRequest, *, adapter,
                   profile, executor: AtomicExecutor, observation_provider,
                   registry=None, visual_resolver: SafeVisualResolver | None = None) -> IntentRunResult:
    """Execute a product plan, then separately assert its resulting page."""
    validate_intent_request(request, profile, adapter)
    plan = adapter.resolve_intent(request.intent_id, dict(request.arguments),
                                  registry=registry, profile=profile)
    role_focus = getattr(observation_provider, "focus_role", None)
    if (registry is None and callable(role_focus)
            and plan.main_role != plan.result_role):
        try:
            role_focus(plan.result_role)
        except BackendUnavailable as exc:
            if "window_role_not_found" not in str(exc):
                raise
        else:
            plan = replace(plan, click_required=False)
    main = adapter.resolve_window_role(plan.main_role)
    result_role = adapter.resolve_window_role(plan.result_role)
    action_result = None
    if plan.click_required:
        if callable(role_focus):
            role_focus(plan.main_role)
        else:
            if not main.process_names:
                raise ValueError("product_window_owner_missing")
            observation_provider.focus_window(main.process_names[0],
                                              main.title_pattern or ".*",
                                              main.root_control_ids[0] if main.root_control_ids else "")
        if not main.process_names:
            raise ValueError("product_window_owner_missing")
        action_result = execute_live_action(
            LocatorRequest("gui.click", {"processName": main.process_names[0]},
                           plan.selector),
            executor=executor, profile=profile.profile_id,
            visual_selector=plan.visual, visual_resolver=visual_resolver,
        )
        if action_result.outcome.value != "passed":
            return IntentRunResult(action_result, action_result, False)
    if callable(role_focus):
        role_focus(plan.result_role)
    else:
        if not result_role.process_names:
            raise ValueError("product_window_owner_missing")
        observation_provider.focus_window(
            result_role.process_names[0], result_role.title_pattern or ".*",
            result_role.root_control_ids[0] if result_role.root_control_ids else "",
        )
    assertions = tuple(Expectation(type=item["type"], value=item["value"])
                       for item in plan.result_assertions)
    assertion_step = AtomicTestStep(
        step_id="INTENT-ASSERT", step_no=1, title=f"verify {plan.result_role}",
        action_id="observe.assert", required=True, expectations=assertions,
    )
    assertion_case = TestCase(
        case_id="INTENT-" + uuid.uuid4().hex[:12], title=request.intent_id,
        profiles=(profile.profile_id,), steps=(assertion_step,),
    )
    assertion_result = executor.run_case(
        assertion_case, execution_context=ExecutionContext(profile=profile.profile_id),
    )
    return IntentRunResult(action_result, assertion_result, not plan.click_required)
