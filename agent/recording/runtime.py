"""One way to build a replay runtime, shared by the CLI and generated tests.

A generated pytest and `edr-wd replay` must drive the same runtime. Assembling
it twice is how the two drift: the CLI gained a per-call timeout and per-step
window focus that a second implementation would silently lack, and the
divergence only shows up against a live target.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, UnidentifiedImageError

from agent.execution import AtomicExecutor
from agent.execution.confirmation import ConfirmationGate, ExecutionContext
from agent.products import backend_for_product, get_product_profile
from agent.trace.events import EventType
from agent.trace.store import TraceStore
from target.action_catalog import CATALOG_VERSION, catalog_digest
from target.action_catalog.actions_v1 import ACTIONS_V1
from target.action_catalog.registry import get_spec
from target.recording.models import RecordingModelError

from .mcp_runtime import MCPActionDispatch, MCPObservationProvider
from .models import GoldenTrace
from .pillow_matcher import PillowTemplateMatcher
from .replay import ReplayRuntime, ResolutionEvidenceSink
from .visual import SafeVisualResolver

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReplayScope:
    process_name: str
    title_regex: str
    root_automation_id: str | None = None


#: Structured connect outcomes that may enter entry-window recovery. Only a
#: *not-found* result means a candidate window is plausibly reconstructible;
#: ambiguity, permission, transport, backend and format failures are never
#: masked by an activation attempt (R0 phase 1, "触发与身份" gate).
R_RECOVERABLE_CONNECT = {"connect_target_not_found"}
#: Error raised when recovery is requested but the entry window genuinely
#: does not exist. Distinct from the generic acquisition refusal so callers
#: can tell "absent, did not attempt" apart from "failed to obtain".
ENTRY_WINDOW_ABSENT = "entry_window_absent"


def _resolve_entry_role_from_profile(product_profile: Any, process_name: str | None) -> str | None:
    """The single profile role owning the golden's entry process, if unambiguous.

    Used only as the product-domain focus fallback when the profile's allowed
    action set excludes the session.* focus actions. Profiles whose process is
    shared by several roles (or with no match) resolve to None and keep the
    primary session-based focus path.
    """
    if not process_name:
        return None
    candidates = [role for role in product_profile.window_roles
                  if process_name in role.process_names]
    return candidates[0].role if len(candidates) == 1 else None


def _resolve_entry_role(profile: str, backend: str, process_name: str | None) -> str | None:
    try:
        product_profile = get_product_profile(profile, backend)
    except ValueError:
        return None
    return _resolve_entry_role_from_profile(product_profile, process_name)


def golden_entry_scope(golden: GoldenTrace) -> ReplayScope:
    """The window replay must own before the first step runs.

    The selector's window identity may be its title regex **or** its root
    control identity.  ``selectors.window_for_event`` prefers the root
    automation id (it names the window and does not move, unlike a title that
    one of the processes renames while running) and keeps the title only as a
    fallback when no root is exposed.  So a `ready` golden can legitimately
    carry no title -- replay must still be able to name the entry process and
    the window's root identity, or refuse only when neither is available.
    """
    selectors = [
        step.selector for step in (*golden.steps, *golden.cleanup)
        if step.selector is not None
    ]
    first = selectors[0] if selectors else None
    # The application label identifies the overall recording, not necessarily
    # the process that owns its first window (EDR's entry agent is separate).
    process_name = (
        first.window.get("processName") if first else None
    ) or golden.environment.get("application")
    title_regex = first.window.get("titleRegex") if first else None
    root_automation_id = first.window.get("rootAutomationId") if first else None
    if not process_name or not (title_regex or root_automation_id):
        raise RecordingModelError(
            "replay_scope_missing",
            "golden trace has no application process/window scope",
            path="golden.steps",
        )
    return ReplayScope(
        str(process_name),
        str(title_regex) if title_regex else "",
        root_automation_id=str(root_automation_id) if root_automation_id else None,
    )


def acquire_target_window(
    agent: Any, scope: ReplayScope, *,
    timeout: float | None = None,
    recovery: Any = None,
) -> dict:
    """Connect, lock and verify the entry window; raise on any refusal.

    ``recovery`` is an optional callable invoked only when ``connect``
    returns the *structured* ``connect_target_not_found`` code (R0 gate: only
    a genuine not-found may be reconstructible; ambiguity/permission/transport/
    backend/format failures are never masked by an activation attempt). It
    receives ``(agent, scope, connect_result)`` and must either:

    - return ``{"action": "activate_existing", "result": "ok"}`` — the window
      exists and was activated; the connect->lock->verify chain is retried once;
    - return ``{"action": "none", "result": "entry_window_absent"}`` — the
      window genuinely does not exist; raises ``entry_window_absent`` without
      launching anything (R0 boundary: absence is reported, never "rebuilt");
    - raise — propagates (including untyped transport exceptions).

    ``recovery`` is ``None`` (off) by default so old goldens / non-HiSec /
    non-recovering callers keep the exact current behavior, and the existing
    refusal-semantics tests (bare scope, no recovery) stay green.
    """
    attempts = 0
    while True:
        attempts += 1
        for tool, arguments in (
            ("connect", {
                "process_name": scope.process_name,
                "title_re": scope.title_regex,
                "timeout": 10.0,
            }),
            ("lock_window", {
                "process_name": scope.process_name,
                "title_re": scope.title_regex,
                "strict": True,
                "activate": True,
            }),
            ("verify_window_lock", {"activate": True}),
        ):
            result = agent.call_tool(tool, arguments, timeout=timeout)
            if isinstance(result, dict) and result.get("ok"):
                continue
            # A connect failure is the ONLY recovery eligible step, and only
            # for a structured not-found. Everything else refuses immediately.
            if (
                tool != "connect"
                or recovery is None
                or not isinstance(result, dict)
                or str(result.get("code") or "") not in R_RECOVERABLE_CONNECT
            ):
                raise RecordingModelError(
                    "replay_target_unavailable",
                    f"{tool} failed: "
                    f"{result.get('error') if isinstance(result, dict) else result}",
                    path=f"replay.{tool}",
                )
            # A real not-found: ask the recovery hook whether the window is
            # genuinely reconstructible. It returns a disposition.
            outcome = recovery(agent, scope, result)
            action = outcome.get("action") if isinstance(outcome, dict) else None
            disposition = outcome.get("result") if isinstance(outcome, dict) else None
            if action == "activate_existing":
                if attempts >= 2:
                    raise RecordingModelError(
                        ENTRY_WINDOW_ABSENT,
                        "entry window not found and not activatable after recovery",
                        path="replay.connect",
                    )
                break  # retry the whole connect->lock->verify chain once
            if disposition == "entry_window_absent":
                raise RecordingModelError(
                    ENTRY_WINDOW_ABSENT,
                    "entry window does not exist; no reconstructible candidate",
                    path="replay.connect",
                )
            # A window exists (or its presence is unknown) but this capability
            # cannot reconstruct it: refuse rather than claim recovery.
            raise RecordingModelError(
                "replay_target_unavailable",
                f"connect failed and entry window is not recoverable: {disposition}",
                path="replay.connect",
            )
        else:
            return {"ok": True}



def build_visual_resolver(asset_root: Path) -> SafeVisualResolver:
    matcher = PillowTemplateMatcher()

    def verified_template_path(template: str, visual: dict | None = None):
        template_path = (asset_root / template).resolve()
        if asset_root != template_path and asset_root not in template_path.parents:
            return None
        if visual is not None:
            expected = visual.get("elementSha256")
            if not isinstance(expected, str) or not template_path.is_file():
                return None
            actual = "sha256:" + hashlib.sha256(template_path.read_bytes()).hexdigest()
            if actual != expected:
                return None
        return template_path

    def match_template(template: str, observation: dict):
        template_path = verified_template_path(template)
        if template_path is None:
            return []
        return matcher(str(template_path), observation)

    return SafeVisualResolver(
        match_template,
        template_verifier=lambda template, visual: (
            verified_template_path(template, dict(visual)) is not None
        ),
    )


def _validate_visual_assets(golden: GoldenTrace, asset_root: Path,
                            replay_mode: str) -> None:
    """Reject missing/tampered templates before any target-side action."""
    for step in (*golden.steps, *golden.cleanup):
        selector = step.selector
        visual = selector.visual if selector is not None else None
        if replay_mode == "visual_only" and step.action_id in {"gui.click", "gui.click_target"} and not visual:
            raise RecordingModelError(
                "visual_template_missing", "visual-only click has no template",
                path=f"replay.steps.{step.step_id}.visual",
            )
        if not visual:
            continue
        for path_key, digest_key in (("template", "elementSha256"),
                                     ("contextTemplate", "contextSha256")):
            name = visual.get(path_key)
            if not name:
                continue
            candidate = (asset_root / name).resolve()
            expected = visual.get(digest_key)
            if (candidate != asset_root and asset_root not in candidate.parents) or not candidate.is_file():
                raise RecordingModelError(
                    "visual_template_integrity_failed", "template path is absent or escapes asset root",
                    path=f"replay.steps.{step.step_id}.{path_key}",
                )
            actual = "sha256:" + hashlib.sha256(candidate.read_bytes()).hexdigest()
            if actual != expected:
                raise RecordingModelError(
                    "visual_template_integrity_failed", "template digest differs from golden trace",
                    path=f"replay.steps.{step.step_id}.{digest_key}",
                )
            try:
                with Image.open(candidate) as image:
                    if image.format != "PNG":
                        raise ValueError("visual template is not PNG")
                    image.verify()
            except (OSError, ValueError, UnidentifiedImageError) as exc:
                raise RecordingModelError(
                    "visual_template_integrity_failed", "visual template is not a valid PNG",
                    path=f"replay.steps.{step.step_id}.{path_key}",
                ) from exc


def _risk_lookup(action_id: str) -> tuple[str, str]:
    spec = get_spec(ACTIONS_V1, action_id)
    return (spec.risk, spec.side_effect) if spec is not None else ("low", "none")


def _entry_recovery(scope: ReplayScope):
    """Recovery hook wired when ``entry_recovery`` is enabled (default off).

    R0 phase 1 boundary: this hook NEVER launches a process, never starts a
    service and never clicks a control. Its job on a structured
    ``connect_target_not_found`` is to re-confirm, read-only, whether a
    matching entry window actually exists, then report an honest disposition:

    - window genuinely absent -> ``{"result": "entry_window_absent"}``, and the
      caller raises ``entry_window_absent`` (no "rebuild", no launch).
    - a window exists but is not connectable by this capability -> reports
      ``not_recoverable`` (the lease-authority "activate existing window"
      capability is a later increment; we do not fake it by fabricating an
      activation).
    """
    def hook(agent_for_hook: Any, _scope: ReplayScope, connect_result: dict) -> dict:
        try:
            reconfirm = agent_for_hook.call_tool(
                "is_window_open",
                {"process_name": _scope.process_name, "title_re": _scope.title_regex},
                timeout=_SCOPE_PROBE_TIMEOUT,
            )
        except Exception as exc:
            # Transport failure during re-confirm is "unknown", not "absent":
            # we must not report entry_window_absent on a flaky connection.
            return {
                "action": "none",
                "result": "unknown",
                "reason": f"reconfirm transport error: {type(exc).__name__}",
            }
        if not isinstance(reconfirm, dict):
            return {"action": "none", "result": "unknown",
                    "reason": "reconfirm returned a non-dict result"}
        windows = reconfirm.get("windows") if reconfirm.get("ok") else None
        if not isinstance(windows, list):
            # A non-ok / failed re-enumeration is not proof of absence.
            return {"action": "none", "result": "unknown",
                    "reason": f"reconfirm failed: {reconfirm.get('error')}"}
        if windows:
            return {
                "action": "none",
                "result": "not_recoverable",
                "reason": "matching window exists but no lease-authority activate capability",
                "window_identity": _window_identity(windows[0]),
            }
        return {"action": "none", "result": "entry_window_absent",
                "reason": "no matching window exists"}

    return hook


def _window_identity(window: dict) -> dict:
    return {
        "pid": window.get("process_id"),
        "title": window.get("title"),
        "handle": window.get("handle"),
    }


#: Per-call timeout for the read-only re-confirmation probe.
_SCOPE_PROBE_TIMEOUT = 10.0


def build_replay_runtime(
    agent: Any,
    golden: GoldenTrace,
    *,
    profile: str = "default",
    trace_root: str | Path = "result-report/replay-traces",
    asset_root: str | Path | None = None,
    replay_mode: str = "semantic_only",
    max_depth: int = 12,
    timeout: float | None = 60.0,
    persist_screenshots: bool = False,
    confirm_actions: Iterable[str] = (),
    acquire_window: bool = True,
    entry_recovery: bool = False,
) -> ReplayRuntime:
    """Assemble the runtime that replays `golden` against `agent`."""
    # S2 boundary (design doc 09 §9): a v3 golden requires the window-binding
    # and effects slices (S3/S4), which are not implemented. Refuse BEFORE
    # any target call -- a lease or connect is already a GUI-side-effect
    # gate. Never fall back to executing on raw selectors with the
    # windowSequence dropped.
    if getattr(golden, "window_sequence", None) is not None:
        raise RecordingModelError(
            "window_sequence_execution_unsupported",
            "golden v3 replay requires the window-binding and effects "
            "capabilities (design slices S3/S4), which are not implemented",
            path="replay.windowSequence",
        )
    # Validate local replay prerequisites before any remote lock or GUI lease.
    # Otherwise an invalid asset request can strand an exclusive GUI run.
    bound_profile = golden.environment.get("profile")
    if bound_profile and bound_profile != profile:
        raise RecordingModelError(
            "golden_profile_mismatch",
            "golden trace profile differs from the replay execution profile",
            path="replay.profile",
        )
    if replay_mode not in {"semantic_only", "semantic_first", "visual_only"}:
        raise RecordingModelError(
            "replay_mode_invalid", f"unsupported replay mode {replay_mode!r}",
            path="replay.mode",
        )
    if replay_mode == "semantic_only":
        for step in (*golden.steps, *golden.cleanup):
            if (step.action_id in {"gui.click", "gui.click_target"}
                    and step.selector is not None
                    and not step.selector.control
                    and step.selector.visual is not None):
                raise RecordingModelError(
                    "replay_selector_semantic_missing",
                    "image-only click requires semantic_first or visual_only replay",
                    path=f"replay.steps.{step.step_id}.selector",
                )
    if replay_mode != "semantic_only" and asset_root is None:
        raise RecordingModelError(
            "replay_assets_missing",
            "visual replay needs the directory holding the golden trace",
            path="replay.asset_root",
        )
    if replay_mode != "semantic_only" and not Path(asset_root).expanduser().is_dir():
        raise RecordingModelError(
            "replay_assets_missing",
            "visual replay asset directory does not exist",
            path="replay.asset_root",
        )
    if replay_mode != "semantic_only":
        _validate_visual_assets(golden, Path(asset_root).expanduser().resolve(), replay_mode)
    tool_listing = agent.tools_list() if callable(getattr(agent, "tools_list", None)) else {}
    tool_names = {item.get("name") for item in tool_listing.get("tools", ())
                  if isinstance(item, dict)} if isinstance(tool_listing, dict) else set()
    rooted_steps = [
        step for step in (*golden.steps, *golden.cleanup)
        if step.selector is not None and step.selector.window.get("rootAutomationId")
    ]
    if rooted_steps and not {"begin_product_run", "focus_window_selector"} <= tool_names:
        raise RecordingModelError(
            "product_window_selector_unavailable",
            "root-identified replay needs a target with exact product-window focus",
            path="replay.target_tools",
        )
    if any(verifier.get("type") == "window_open"
           for step in (*golden.steps, *golden.cleanup)
           for verifier in step.verifiers):
        if not {"begin_product_run", "observe_product_windows"} <= tool_names:
            raise RecordingModelError(
                "product_window_inventory_unavailable",
                "window assertions require a product-scoped window inventory on the target",
                path="replay.target_tools",
            )
    first_selector = next((step.selector for step in (*golden.steps, *golden.cleanup)
                           if step.selector is not None), None)
    legacy_lock = acquire_window and not (
        first_selector and first_selector.window.get("rootAutomationId"))
    # R0/R2: with entry_recovery enabled AND a lease-capable target, the entry
    # window is acquired UNDER the product-run lease through the target's
    # controlled focus_entry_window tool (read-only enumeration -> full
    # declared-identity match -> single-match connect+strict-lock+activate),
    # with audit events around it. Applies to BOTH entry shapes: a legacy
    # golden (no root in the first selector) and a v2 golden carrying a
    # declared entryWindow identity (review P1-3: restricting recovery to the
    # legacy shape made the root-identity matching a dead path, since the
    # compiler normally emits rooted selectors). Rooted goldens keep their
    # per-step focus_window_selector afterwards; this acquisition only
    # establishes the entry window first. Default (off) keeps the historical
    # order: legacy acquire, then lease.
    lease_entry_path = (
        entry_recovery and "begin_product_run" in tool_names
        and {"end_product_run", "observe_window_bundle", "focus_window_role"} <= tool_names
        and (legacy_lock or golden.entry_window is not None)
    )
    if legacy_lock and not lease_entry_path:
        recovery = _entry_recovery(golden_entry_scope(golden)) if entry_recovery else None
        acquire_target_window(agent, golden_entry_scope(golden), timeout=timeout,
                              recovery=recovery)

    trace_store = TraceStore(Path(trace_root).expanduser().resolve())
    run_token = None
    try:
        trace_store.open()
        run_token, entry_role = _begin_product_run_if_available(
            agent, profile, tool_names, golden, timeout)
        if lease_entry_path:
            _acquire_entry_window_under_lease(
                agent, golden_entry_scope(golden), run_token,
                timeout=timeout, trace_store=trace_store,
                declared_identity=golden.entry_window)
        runtime = _assemble_replay_runtime(
            agent, profile=profile, trace_store=trace_store,
            asset_root=asset_root, replay_mode=replay_mode, max_depth=max_depth,
            timeout=timeout, persist_screenshots=persist_screenshots,
            confirm_actions=confirm_actions, run_token=run_token,
            entry_role=entry_role)
    except BaseException:
        # On the lease entry path we never took a legacy lock, so an
        # unlock_window against a (possibly other-run-held) lease is an
        # unauthorized operation -- release only via the lease hook.
        _release_acquired_target(agent, run_token, timeout, trace_store,
                                 unlock_window=legacy_lock and not lease_entry_path)
        raise
    return runtime


def _recovery_audit(trace_store: TraceStore, event_type: EventType,
                    payload: dict) -> None:
    """Persist one recovery audit event. Never carries tokens/credentials."""
    try:
        trace_store.append_dict(event_type, payload=payload)
    except Exception:
        # Audit persistence failure stops further side effects by contract
        # (R0 "持久写失败时停止后续副作用"); here the acquisition has already
        # happened or been refused by the target, so the failure is surfaced
        # as an error rather than silently dropped.
        raise RecordingModelError(
            "recovery_audit_write_failed",
            "recovery audit could not be persisted",
            path="replay.recovery_audit",
        )


def _acquire_entry_window_under_lease(
    agent: Any, scope: ReplayScope, run_token: str, *,
    timeout: float | None, trace_store: TraceStore,
    declared_identity: Any = None,
) -> None:
    """Acquire the entry window under the held product-run lease (design 08).

    ``declared_identity`` is the golden's optional v2 ``entryWindow`` record.
    Present -> the target must verify the full declared identity (process,
    title, root) and any refusal there is final: no downgrade to a title-only
    match. Absent (v1 golden) -> scope identity only, phase-1 semantics.
    Every disposition is audited to the trace BEFORE the replay's first step.
    """
    if declared_identity is not None:
        effective_process = declared_identity.process_name
        effective_title = declared_identity.title_regex
        effective_root = declared_identity.root_automation_id
    else:
        effective_process = scope.process_name
        effective_title = scope.title_regex or None
        effective_root = scope.root_automation_id
    scope_identity = {
        "process_name": effective_process,
        "title_regex": effective_title,
        "root_automation_id": effective_root,
        "declared_identity": bool(declared_identity),
    }
    # The request audit MUST land before any target side effect: if it cannot
    # be persisted, nothing has happened yet and we stop here (R0 "持久写失败
    # 时停止后续副作用").
    try:
        _recovery_audit(trace_store, EventType.RECOVERY_REQUESTED, {
            "reason": "entry_window_acquisition",
            "scope": scope_identity,
            "action": "focus_entry_window",
        })
    except Exception:
        raise RecordingModelError(
            "recovery_audit_write_failed",
            "recovery request audit could not be persisted; no target side effect has happened",
            path="replay.recovery_audit",
        )
    # Use the same effective identity for the audit and the target request.
    # A root-identified window legitimately has no title regex.
    arguments: dict[str, Any] = {
        "run_token": run_token,
        "process_name": effective_process,
    }
    if effective_title:
        arguments["title_regex"] = effective_title
    if effective_root:
        arguments["root_automation_id"] = effective_root
    try:
        result = agent.call_tool("focus_entry_window", arguments, timeout=timeout)
    except Exception as exc:
        # Transport loss mid-recovery: the outcome is UNKNOWN, not "not
        # executed" -- the target may have acted. Audit the unknown, then
        # propagate the original exception untyped.
        try:
            _recovery_audit(trace_store, EventType.RECOVERY_RESULT, {
                "result": "unknown",
                "reason": f"transport exception: {type(exc).__name__}",
                "scope": scope_identity,
            })
        except Exception:
            logger.warning("recovery result audit failed after transport exception",
                           exc_info=True)
        raise
    if not isinstance(result, dict):
        try:
            _recovery_audit(trace_store, EventType.RECOVERY_RESULT, {
                "result": "invalid_response", "scope": scope_identity})
            audit_failed = False
        except Exception:
            audit_failed = True
        if audit_failed:
            # The audit failure must NOT mask the acquisition outcome we are
            # reporting; log it and raise the real error.
            logger.warning("recovery result audit failed for invalid response")
        raise RecordingModelError(
            "replay_target_unavailable",
            "focus_entry_window returned a non-dict result",
            path="replay.focus_entry_window",
        )
    disposition = result.get("disposition") or result.get("code")
    try:
        _recovery_audit(trace_store, EventType.RECOVERY_RESULT, {
            "result": disposition,
            "scope": scope_identity,
            "window": result.get("window"),
        })
    except Exception:
        # Same rule: report the audit failure separately; the operator must
        # still see the acquisition's own outcome below, not the audit error.
        logger.warning("recovery result audit failed for disposition %r",
                       disposition, exc_info=True)
    if result.get("ok"):
        return
    raise RecordingModelError(
        str(result.get("code") or "replay_target_unavailable"),
        f"entry window acquisition failed: {result.get('error')}",
        path="replay.focus_entry_window",
    )


def _begin_product_run_if_available(
    agent: Any, profile: str, tool_names: set[str], golden: GoldenTrace,
    timeout: float | None,
) -> tuple[str | None, str | None]:
    if "begin_product_run" not in tool_names:
        return None, None
    if not {"end_product_run", "observe_window_bundle", "focus_window_role"} <= tool_names:
        raise RecordingModelError(
            "product_run_capability_incomplete", "target product-run tools are incomplete",
            path="replay.target_tools",
        )
    state = getattr(agent, "state", None)
    try:
        backend = backend_for_product(
            profile, backend_kind=getattr(state, "backend_kind", None),
            platform=getattr(state, "platform", None),
        )
        product_profile = get_product_profile(profile, backend)
        digest = product_profile.digest
    except ValueError as exc:
        raise RecordingModelError("product_profile_unknown", str(exc),
                                  path="replay.profile") from exc
    entry_role = _resolve_entry_role_from_profile(
        product_profile, golden_entry_scope(golden).process_name)
    started = agent.call_tool("begin_product_run", {"profile_id": profile, "digest": digest,
                                                    "lease_seconds": 3600},
                              timeout=timeout)
    if not isinstance(started, dict) or not started.get("ok") or not started.get("run_token"):
        raise RecordingModelError(
            str(started.get("code") if isinstance(started, dict) else "product_run_conflict"),
            "could not acquire target product GUI lease", path="replay.product_run",
        )
    return started["run_token"], entry_role


def _assemble_replay_runtime(
    agent: Any, *, profile: str, trace_store: TraceStore,
    asset_root: str | Path | None, replay_mode: str, max_depth: int,
    timeout: float | None, persist_screenshots: bool,
    confirm_actions: Iterable[str], run_token: str | None, entry_role: str | None,
) -> ReplayRuntime:
    observations = MCPObservationProvider(
        agent,
        max_depth=max_depth,
        trace_store=trace_store,
        capture_screenshot=(replay_mode != "semantic_only" or persist_screenshots),
        timeout=timeout,
        use_bundle=run_token is not None,
        run_token=run_token,
        include_ocr=run_token is not None and replay_mode == "semantic_first",
        entry_role=entry_role,
    )
    resolver = None
    if replay_mode != "semantic_only":
        resolver = build_visual_resolver(Path(asset_root).expanduser().resolve())

    resolution_evidence = ResolutionEvidenceSink()
    executor = AtomicExecutor(
        dispatch=MCPActionDispatch(agent, trace_store=trace_store, timeout=timeout,
                                   run_token=run_token,
                                   resolution_evidence=resolution_evidence),
        observation_provider=observations,
        confirmation_gate=ConfirmationGate(),
        risk_lookup=_risk_lookup,
    )
    return ReplayRuntime(
        executor=executor,
        catalog_version=CATALOG_VERSION,
        catalog_digest=catalog_digest(),
        execution_context=ExecutionContext(
            profile=profile,
            confirmation_tokens=frozenset(confirm_actions),
        ),
        trace_store=trace_store,
        replay_mode=replay_mode,
        visual_resolver=resolver,
        persist_replay_screenshots=persist_screenshots,
        resolution_evidence=resolution_evidence,
        window_focus=lambda process_name, title_regex, root="": (
            observations.get_snapshot(
                observations.focus_window(process_name, title_regex, root)
            )
        ),
        end_product_run=(lambda: _end_product_run(agent, run_token, timeout)) if run_token else None,
    )


def _release_acquired_target(agent: Any, run_token: str | None, timeout: float | None,
                             trace_store: TraceStore, *, unlock_window: bool) -> None:
    """Best-effort teardown after a failed assembly; never masks the error.

    A product-run lease releases its cleanup hook (which unlocks the backend
    window lock) via `_release_product_run_lease`; a legacy acquire has no
    such hook, so the lock_window from `acquire_target_window` would survive
    the failure unless an explicit unlock_window runs.  `unlock_window=False`
    for the product-run entry path — no legacy lock was taken, and unlock is
    an unauthorized operation against a product-run session.  A trace opened
    before the failure must not read as "in progress" forever.
    """
    try:
        if run_token is not None:
            from .replay import _release_product_run_lease
            _release_product_run_lease(
                lambda: _end_product_run(agent, run_token, timeout))
        elif unlock_window:
            result = agent.call_tool("unlock_window", {}, timeout=timeout)
            if not (isinstance(result, dict) and result.get("ok")):
                logger.warning("legacy window unlock after failed assembly failed: %r",
                               result)
    except Exception:
        logger.warning("window/lease release after failed assembly raised", exc_info=True)
    try:
        trace_store.finalize(
            EventType.TRACE_ABORTED, payload={"reason": "replay_assembly_failed"})
    except Exception:
        pass  # the original assembly error remains the actionable error


def _end_product_run(agent: Any, run_token: str, timeout: float | None) -> None:
    result = agent.call_tool("end_product_run", {"run_token": run_token}, timeout=timeout)
    if not isinstance(result, dict) or not result.get("ok"):
        raise RecordingModelError("product_run_cleanup_failed",
                                  "target product run lease could not be released",
                                  path="replay.product_run")


__all__ = [
    "ReplayScope",
    "acquire_target_window",
    "build_replay_runtime",
    "golden_entry_scope",
]
