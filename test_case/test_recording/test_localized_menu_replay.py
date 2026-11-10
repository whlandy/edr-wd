"""A translated menu uses a real current control in a bounded menu layout."""
import copy

import pytest

from agent.locator.hybrid import HybridLocator, LocatorError, LocatorRequest
from agent.locator.policy import LocatorPolicy
from agent.recording.compiler import compile_recording
from agent.recording.models import ReplaySelector
from agent.recording.replay import resolve_replay_selector, StepMaterializationError
from target.recording.models import CaptureScope, ObservedTarget, RawCaptureEvent, RawRecording

pytestmark = pytest.mark.unit
CHAIN = [{"controlType": "Window", "name": "EDRClient"},
         {"controlType": "Pane", "name": "Desktop 1"}]


def nodes(label="Language", *, dx=0, dy=0, scale=1, width=153):
    out = []
    for i, text in enumerate(("Refresh", label, "Exit")):
        rect = [1000, 100 + i * 40, 1000 + width, 132 + i * 40]
        rect = [v * scale + (dx if j % 2 == 0 else dy)
                for j, v in enumerate(rect)]
        out.append({"controlType": "MenuItem", "text": text,
                    "ancestry": copy.deepcopy(CHAIN), "rect": rect})
    return out


def recording(*, complete=True, kind="MenuItem", strong=False):
    frame = nodes()
    for node in frame:
        node["controlType"] = kind
    if strong:
        frame[1]["automationId"] = "menu.language"
    window = {"handle": 7, "title": "EDRClient", "processName": "EDRClient.exe"}
    event = RawCaptureEvent(
        sequence=1, wall_time="2026-11-06T00:00:00Z", monotonic_ms=200,
        type="pointer_click", input={"button": "left", "screenPoint": [1050, 155]},
        observed_target=ObservedTarget(
            "OLD-OBS", "OLD-TARGET", "old-fingerprint", control_type=kind,
            automation_id="menu.language" if strong else None,
            text="Language", rect=tuple(frame[1]["rect"]),
            ancestry=tuple(copy.deepcopy(CHAIN))),
        scope=CaptureScope("t", "windows_pywinauto", "EDRClient.exe", "^EDRClient$"),
        evidence={"window": window})
    snapshot = {"snapshotId": "CS-1", "capturedAtMs": 190, "reason": "step",
                "window": window, "contentHash": "h", "stateHash": "h",
                "controlCount": len(frame), "complete": complete, "controls": frame}
    return RawRecording(name="localized-menu", session_id="s-menu", events=[event],
                        control_snapshots=(snapshot,))


def selector():
    result = compile_recording(recording())
    assert result.golden.status == "ready", result.issues
    return result.golden.steps[0].selector


def observation(items):
    controls = [{**node, "control_type": node["controlType"],
                 "target_id": f"CURRENT-{i}", "fingerprint": f"current-{i}",
                 "process_name": "EDRClient.exe", "window_title": "EDRClient"}
                for i, node in enumerate(items)]
    return {"observation_id": "CURRENT-OBS", "controls": controls,
            "active_window": {"process_name": "EDRClient.exe", "title": "EDRClient"},
            "window_identity": {"native_id": "CURRENT-MENU"}}


@pytest.mark.parametrize("label", ["Language", "语言设置", "Idioma", "Langue"])
@pytest.mark.parametrize("scale", [1, 1.5])
def test_translated_moved_scaled_menu_resolves_current_target(label, scale):
    selected = selector()
    obs = observation(nodes(label, dx=-300, dy=90, scale=scale, width=156))
    ref = resolve_replay_selector(selected, obs)
    assert ref.snapshot_id == "CURRENT-OBS" and ref.target_id == "CURRENT-1"
    assert ref.fingerprint == "current-1"
    resolved = HybridLocator().resolve(
        LocatorRequest("gui.click", selected.window, selected.control), obs, LocatorPolicy())
    assert resolved.method == "menu_structural"
    assert resolved.evidence_types == ("structure", "geometry")
    # Contract survives model serialization; no stale target/coordinate persisted.
    roundtrip = ReplaySelector.from_dict(selected.to_dict())
    assert roundtrip == selected


@pytest.mark.parametrize("mutation", ["extra", "overlap", "missing_rect", "ancestry",
                                      "process", "title", "layout"])
def test_changed_or_ambiguous_menu_refuses_instead_of_blind_click(mutation):
    items = nodes("Idioma")
    if mutation == "extra":
        items.append({**items[-1], "rect": [1000, 220, 1153, 252]})
    elif mutation == "overlap":
        items[0]["rect"] = items[1]["rect"]
    elif mutation == "missing_rect":
        items[1].pop("rect")
    elif mutation == "ancestry":
        for n in items:
            n["ancestry"] = [{"controlType": "Window", "name": "Other"}]
    elif mutation == "layout":
        items[1]["rect"] = [1000, 145, 1153, 155]
    obs = observation(items)
    if mutation in {"process", "title"}:
        for n in obs["controls"]:
            n["process_name" if mutation == "process" else "window_title"] = "Other"
    with pytest.raises(StepMaterializationError):
        resolve_replay_selector(selector(), obs)


def test_incomplete_recording_does_not_earn_structural_identity():
    result = compile_recording(recording(complete=False))
    assert result.golden.status == "incomplete"
    assert "structural" not in result.golden.steps[0].selector.control


@pytest.mark.parametrize("kwargs", [{"kind": "Button"}, {"strong": True}])
def test_no_broad_weak_identity_or_strong_id_override(kwargs):
    result = compile_recording(recording(**kwargs))
    assert "structural" not in result.golden.steps[0].selector.control


def test_corrupt_structural_contract_is_not_a_fallback_escape():
    control = dict(selector().control)
    control["structural"] = {**control["structural"], "index": 999}
    with pytest.raises(LocatorError, match="locator_evidence_invalid"):
        HybridLocator().collect_candidates(
            LocatorRequest("gui.click", selector().window, control), observation(nodes()))


def test_standard_replay_materializes_current_translated_label():
    from agent.recording.replay import GoldenStepMaterializer, _recorded_to_atomic
    golden = compile_recording(recording()).golden
    step = golden.steps[0]
    materialized = GoldenStepMaterializer(golden)(
        _recorded_to_atomic(step, 1), observation(nodes("Idioma")))
    assert materialized.args["text"] == "Idioma"
    # Text-only materialization (menu_layout r3 fix): control_type absent.
    assert "control_type" not in materialized.args
    assert materialized.args["parent_fallback"] is False
    assert materialized.target_ref.target_id == "CURRENT-1"


@pytest.mark.parametrize("changed_layout", [False, True])
def test_real_bound_window_chain_dispatches_translated_menu_or_zero_input(
        monkeypatch, tmp_path, changed_layout):
    import dataclasses
    import target.server as server
    from target import product_policy
    from target.windowing.attempt_ledger import AttemptLedger
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from test_case.test_recording import test_v3_real_server_adapter as adapter

    golden, _, _ = adapter._compile_golden("same_window")
    selected = selector()
    selected = dataclasses.replace(selected, window=golden.steps[0].selector.window)
    first = dataclasses.replace(golden.steps[0], selector=selected)
    golden = dataclasses.replace(
        golden, steps=(first,), window_sequence=dataclasses.replace(
            golden.window_sequence, steps=(golden.window_sequence.steps[0],)))
    backend = adapter._checkbox_backend(False)
    dump = backend.dump_tree

    def translated_tree(*args, **kwargs):
        result = dump(*args, **kwargs)
        rows = nodes("Idioma", dx=-900, dy=100)
        if changed_layout:
            rows.pop()
        for i, row in enumerate(rows):
            left, top, right, bottom = row["rect"]
            result["controls"].append({
                "class_name": "MenuItem", "text": row["text"],
                "control_id": 20 + i, "automation_id": "",
                "rectangle": {"x": left, "y": top, "w": right-left, "h": bottom-top},
                "is_visible": True, "is_enabled": True, "depth": 1,
                "ancestry": row["ancestry"]})
        return result

    backend.dump_tree = translated_tree
    product_policy.reset_for_tests()
    adapter._install_fake_os(monkeypatch, backend)
    try:
        result = execute_v3_window_stage_real(
            golden=golden, ledger=AttemptLedger(tmp_path / "events"),
            server_tools=server, window_role_map=adapter._SAME_WINDOW_ROLE_MAP)
        if changed_layout:
            assert result["dispatchInputs"] == 0, result
            assert result["report"]["outcome"] != "pass", result
        else:
            assert result["dispatchInputs"] == 1, result
            assert result["report"]["outcome"] == "pass", result
            assert len(backend.dispatch_calls) == 1
            assert backend.dispatch_calls[0]["text"] == "Idioma"
            # Text-only materialization (menu_layout r3 fix): control_type is
            # deliberately absent from backend args — pairing it made
            # the strict child_window path miss deep Qt MenuItems.
            assert "control_type" not in backend.dispatch_calls[0]
    finally:
        product_policy.reset_for_tests()
        server._binding_facade_holder["facade"] = None
