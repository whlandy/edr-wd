"""Offline planner E2E scenarios for the completed action-sequence TODO.

The suite crosses the real catalog, structured-output parser, observation
resolver, and post-step replan policy. A stub LLM supplies deterministic wire
payloads; no real GUI mutation is performed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from agent.planner import (
    REPLAN_REASON_STALE_TARGET_REF,
    REPLAN_REASON_UNEXPECTED_TRANSITION,
    SCHEMA_VERSION,
    enabled_actions_for,
    needs_replan,
    parse_plan,
)
from target.action_catalog import ACTIONS_V1
from target.observations import (
    CODE_OWNERSHIP_MISMATCH,
    CODE_TARGET_AMBIGUOUS,
    CODE_TARGET_STALE,
    ObservationRef,
    ObservationSnapshot,
    TargetResolutionError,
    build_snapshot,
    invalidate_snapshot,
    mark_live,
    reset_for_tests,
    resolve_target,
)
from target.protocol_models import ActionSequence


@dataclass(frozen=True)
class StubLLM:
    payload: dict[str, Any]

    def complete(self) -> dict[str, Any]:
        return self.payload


@pytest.fixture(autouse=True)
def _reset_observation_registry():
    reset_for_tests()
    yield
    reset_for_tests()


def _snapshot(*, duplicate_ok: bool = False) -> ObservationSnapshot:
    targets: list[dict[str, Any]] = [
        {
            "process_name": "EDRClient.exe",
            "pid": 42,
            "native_window_id": "window-main",
            "title": "Main",
            "kind": "window",
            "rect": (0, 0, 800, 600),
        },
        {
            "process_name": "EDRClient.exe",
            "pid": 42,
            "native_window_id": "window-main",
            "title": "OK",
            "kind": "control",
            "control_type": "Button",
            "automation_id": "btn-ok",
            "rect": (100, 100, 200, 150),
        },
    ]
    if duplicate_ok:
        targets.append(
            {
                "process_name": "EDRClient.exe",
                "pid": 42,
                "native_window_id": "window-main",
                "title": "Another button",
                "kind": "control",
                "control_type": "Button",
                "automation_id": "btn-other",
                "rect": (220, 100, 320, 150),
            }
        )
    snapshot = build_snapshot(
        targets=targets,
        backend="windows_pywinauto",
        host="fake-windows",
        captured_at="2026-08-04T00:00:00Z",
        active_window_target_id="T0001",
    )
    mark_live(snapshot.snapshot_id)
    return snapshot


def _wire_plan(snapshot_id: str, target_ref: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "plan_id": "PLAN-E2E-1",
        "snapshot_id": snapshot_id,
        "expected_actions": [
            {
                "step_id": "S1",
                "action_id": "gui.click",
                "target_ref": target_ref,
                "on_error": "reobserve_replan",
            }
        ],
        "expected_dependencies": {"S1": []},
    }


def _parse_from_stub(stub: StubLLM, snapshot_id: str):
    return parse_plan(
        stub.complete(),
        catalog=enabled_actions_for("windows_pywinauto", "windows_hisec"),
        snapshot_id=snapshot_id,
    )


def _gui_click():
    return next(spec for spec in ACTIONS_V1 if spec.action_id == "gui.click")


def test_valid_stub_llm_plan_resolves_semantic_target_end_to_end():
    snapshot = _snapshot()
    stub = StubLLM(
        _wire_plan(
            snapshot.snapshot_id,
            {
                "snapshot_id": snapshot.snapshot_id,
                "target_id": "T0002",
                "expected_process_name": "EDRClient.exe",
            },
        )
    )

    parsed = _parse_from_stub(stub, snapshot.snapshot_id)
    assert parsed.errors == ()
    wire_ref = parsed.plan["expected_actions"][0]["target_ref"]
    target = resolve_target(ObservationRef(**wire_ref), snapshot, _gui_click())
    assert target.target_id == "T0002"
    assert target.automation_id == "btn-ok"


def test_ambiguous_semantic_target_is_rejected_without_guessing():
    snapshot = _snapshot(duplicate_ok=True)
    stub = StubLLM(
        _wire_plan(
            snapshot.snapshot_id,
            {
                "snapshot_id": snapshot.snapshot_id,
                "target_id": "",
                "expected_process_name": "EDRClient.exe",
                "selector_hint": {"control_type": "Button"},
            },
        )
    )

    parsed = _parse_from_stub(stub, snapshot.snapshot_id)
    assert parsed.errors == ()
    wire_ref = parsed.plan["expected_actions"][0]["target_ref"]
    with pytest.raises(TargetResolutionError) as exc:
        resolve_target(ObservationRef(**wire_ref), snapshot, _gui_click())
    assert exc.value.code == CODE_TARGET_AMBIGUOUS
    assert exc.value.details["candidate_count"] == 2


def test_wrong_window_ownership_is_rejected_before_dispatch():
    snapshot = _snapshot()
    stub = StubLLM(
        _wire_plan(
            snapshot.snapshot_id,
            {
                "snapshot_id": snapshot.snapshot_id,
                "target_id": "T0002",
                "expected_process_name": "WrongProcess.exe",
            },
        )
    )

    parsed = _parse_from_stub(stub, snapshot.snapshot_id)
    assert parsed.errors == ()
    wire_ref = parsed.plan["expected_actions"][0]["target_ref"]
    with pytest.raises(TargetResolutionError) as exc:
        resolve_target(ObservationRef(**wire_ref), snapshot, _gui_click())
    assert exc.value.code == CODE_OWNERSHIP_MISMATCH


def test_invalidated_tree_is_rejected_and_missing_target_triggers_replan():
    snapshot = _snapshot()
    ref = {
        "snapshot_id": snapshot.snapshot_id,
        "target_id": "T0002",
        "expected_process_name": "EDRClient.exe",
    }
    parsed = _parse_from_stub(
        StubLLM(_wire_plan(snapshot.snapshot_id, ref)), snapshot.snapshot_id
    )
    assert parsed.errors == ()

    invalidate_snapshot(snapshot.snapshot_id)
    with pytest.raises(TargetResolutionError) as exc:
        resolve_target(ObservationRef(**ref), snapshot, _gui_click())
    assert exc.value.code == CODE_TARGET_STALE

    plan = ActionSequence.from_dict(
        {
            "plan_id": "PLAN-E2E-1",
            "catalog_version": "1.0.0",
            "catalog_digest": "sha256:test",
            "target": "fake-windows",
            "steps": [
                {
                    "step_id": "S1",
                    "action_id": "gui.click",
                    "args": {},
                    "depends_on": [],
                    "expectations": [],
                    "on_error": "reobserve_replan",
                    "target_ref": ref,
                }
            ],
        }
    )
    fresh = build_snapshot(
        targets=[
            {
                "process_name": "EDRClient.exe",
                "pid": 42,
                "native_window_id": "window-main",
                "title": "Main after navigation",
                "kind": "window",
                "rect": (0, 0, 800, 600),
            }
        ],
        backend="windows_pywinauto",
        host="fake-windows",
        captured_at="2026-08-04T00:00:01Z",
        active_window_target_id="T0001",
    )
    mark_live(fresh.snapshot_id)
    decision = needs_replan(plan, fresh)
    assert decision.needed is True
    assert decision.reason == REPLAN_REASON_STALE_TARGET_REF


def test_unexpected_mid_sequence_dialog_triggers_replan():
    plan = ActionSequence.from_dict(
        {
            "plan_id": "PLAN-DIALOG",
            "catalog_version": "1.0.0",
            "catalog_digest": "sha256:test",
            "target": "fake-windows",
            "steps": [
                {
                    "step_id": "S1",
                    "action_id": "gui.click",
                    "args": {},
                    "depends_on": [],
                    "expectations": [],
                    "on_error": "reobserve_replan",
                    "transition": {
                        "expected": True,
                        "kind": "none",
                        "checkpoint_before": False,
                        "expected_window_owner": None,
                    },
                }
            ],
        }
    )
    before = ObservationSnapshot.from_dict(
        {
            "schema_version": "obs.v1",
            "snapshot_id": "snap-before",
            "captured_at": "2026-08-04T00:00:00Z",
            "backend": "fake",
            "host": "fake-windows",
            "active_window": None,
            "targets": [],
            "tree_digest": "sha256:before",
        }
    )
    after = ObservationSnapshot.from_dict(
        {
            "schema_version": "obs.v1",
            "snapshot_id": "snap-dialog",
            "captured_at": "2026-08-04T00:00:01Z",
            "backend": "fake",
            "host": "fake-windows",
            "active_window": None,
            "targets": [
                {
                    "target_id": "T0001",
                    "kind": "window",
                    "process_name": "EDRClient.exe",
                    "pid": 42,
                    "native_window_id": "unexpected-dialog",
                    "title": "Unexpected confirmation",
                    "fingerprint": "sha256:dialog",
                    "fingerprint_fields": [],
                }
            ],
            "tree_digest": "sha256:dialog",
        }
    )

    decision = needs_replan(plan, after, snapshot_before=before)
    assert decision.needed is True
    assert decision.reason == REPLAN_REASON_UNEXPECTED_TRANSITION
