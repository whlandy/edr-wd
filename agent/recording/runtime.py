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

from agent.execution import AtomicExecutor
from agent.execution.confirmation import ConfirmationGate, ExecutionContext
from agent.trace.store import TraceStore
from target.action_catalog import CATALOG_VERSION, catalog_digest
from target.action_catalog.actions_v1 import ACTIONS_V1
from target.action_catalog.registry import get_spec
from target.recording.models import RecordingModelError

from .mcp_runtime import MCPActionDispatch, MCPObservationProvider
from .models import GoldenTrace
from .pillow_matcher import PillowTemplateMatcher
from .replay import ReplayRuntime
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
    process_name = golden.environment.get("application") or (
        first.window.get("processName") if first else None
    )
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


def _visual_resolver(asset_root: Path) -> SafeVisualResolver:
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
    if acquire_window:
        acquire_target_window(agent, golden_entry_scope(golden), timeout=timeout)

    trace_store = TraceStore(Path(trace_root).expanduser().resolve())
    trace_store.open()
    observations = MCPObservationProvider(
        agent,
        max_depth=max_depth,
        trace_store=trace_store,
        capture_screenshot=(replay_mode != "semantic_only" or persist_screenshots),
        timeout=timeout,
    )
    resolver = None
    if replay_mode != "semantic_only":
        if asset_root is None:
            raise RecordingModelError(
                "replay_assets_missing",
                "visual replay needs the directory holding the golden trace",
                path="replay.asset_root",
            )
        resolver = _visual_resolver(Path(asset_root).expanduser().resolve())

    executor = AtomicExecutor(
        dispatch=MCPActionDispatch(agent, trace_store=trace_store, timeout=timeout),
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
        window_focus=lambda process_name, title_regex: observations.get_snapshot(
            observations.focus_window(process_name, title_regex)
        ),
    )


__all__ = [
    "ReplayScope",
    "acquire_target_window",
    "build_replay_runtime",
    "golden_entry_scope",
]
