"""One way to build a replay runtime, shared by the CLI and generated tests.

A generated pytest and `edr-wd replay` must drive the same runtime. Assembling
it twice is how the two drift: the CLI gained a per-call timeout and per-step
window focus that a second implementation would silently lack, and the
divergence only shows up against a live target.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, UnidentifiedImageError

from agent.execution import AtomicExecutor
from agent.execution.confirmation import ConfirmationGate, ExecutionContext
from agent.products import backend_for_product, get_product_profile
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


@dataclass(frozen=True)
class ReplayScope:
    process_name: str
    title_regex: str


def golden_entry_scope(golden: GoldenTrace) -> ReplayScope:
    """The window replay must own before the first step runs."""
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
    if not process_name or not title_regex:
        raise RecordingModelError(
            "replay_scope_missing",
            "golden trace has no application process/title scope",
            path="golden.steps",
        )
    return ReplayScope(str(process_name), str(title_regex))


def acquire_target_window(agent: Any, scope: ReplayScope, *, timeout: float | None = None) -> dict:
    """Connect, lock and verify the entry window; raise on any refusal."""
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
        if not isinstance(result, dict) or not result.get("ok"):
            raise RecordingModelError(
                "replay_target_unavailable",
                f"{tool} failed: "
                f"{(result or {}).get('error') if isinstance(result, dict) else result}",
                path=f"replay.{tool}",
            )
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
) -> ReplayRuntime:
    """Assemble the runtime that replays `golden` against `agent`."""
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
    if acquire_window and not (first_selector and first_selector.window.get("rootAutomationId")):
        acquire_target_window(agent, golden_entry_scope(golden), timeout=timeout)

    trace_store = TraceStore(Path(trace_root).expanduser().resolve())
    trace_store.open()
    run_token = None
    if "begin_product_run" in tool_names:
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
            digest = get_product_profile(profile, backend).digest
        except ValueError as exc:
            raise RecordingModelError("product_profile_unknown", str(exc),
                                      path="replay.profile") from exc
        started = agent.call_tool("begin_product_run", {"profile_id": profile, "digest": digest,
                                                      "lease_seconds": 3600},
                                  timeout=timeout)
        if not isinstance(started, dict) or not started.get("ok") or not started.get("run_token"):
            raise RecordingModelError(
                str(started.get("code") if isinstance(started, dict) else "product_run_conflict"),
                "could not acquire target product GUI lease", path="replay.product_run",
            )
        run_token = started["run_token"]
    try:
        observations = MCPObservationProvider(
            agent,
            max_depth=max_depth,
            trace_store=trace_store,
            capture_screenshot=(replay_mode != "semantic_only" or persist_screenshots),
            timeout=timeout,
            use_bundle=run_token is not None,
            run_token=run_token,
            include_ocr=run_token is not None and replay_mode == "semantic_first",
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
    except Exception:
        if run_token is not None:
            try:
                _end_product_run(agent, run_token, timeout)
            except Exception:
                pass  # original assembly failure remains the actionable error
        raise


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
