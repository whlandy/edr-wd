# QA fixture profile：注册器契约、条目能力、越界拒绝与 runtime 显式绑定。
#
# 背景：fixture golden（powershell.exe + 唯一标题 qa-dsh-fixed-1025 + root
# RecordingFixture）不能用目标默认 windows_hisec（EDR）回放。本轮在 target
# 自有 apps.json 注册 `qa_recording_fixture`（application.path 仅为注册契约
# 必需，回放路径从不调用 activate；intents 为 dict），业务动作仅
# gui.click/gui.type_text，runtime 绑定显式 profile。
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from agent.products import backend_for_product
from agent.products.registry import get_product_profile
from agent.recording.compiler import compile_recording
from agent.recording.runtime import build_replay_runtime
from target.action_dispatcher.conditions import check_product_action
from target.product_policy import ProductRunContext
from target.product_profiles import (
    get_capability,
    register_app_catalog,
    register_product_spec,
)
from target.recording.models import RawRecording

pytestmark = pytest.mark.unit

_POWERSHELL = "C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe"
_TITLE = "EDR-WD Native Recording QA [qa-dsh-fixed-1025]"


def _variant(**overrides) -> dict:
    variant = {
        "profile_id": "qa_counterexample_base",
        "version": "1",
        "backend": "windows_pywinauto",
        "application": {"path": _POWERSHELL},
        "windows": [
            {"role": "main", "name": "base", "names": ["base"],
             "window_path": "main", "process_names": ["powershell.exe"],
             "title_pattern": "^base$"},
        ],
        "allowed_action_ids": ["gui.click"],
        "intents": {},
    }
    variant.update(overrides)
    return variant


def _macos_variant() -> dict:
    return {
        "profile_id": "qa_counterexample_base_macos",
        "version": "1",
        "backend": "macos_accessibility",
        "application": {"path": "/System/Applications/Calculator.app"},
        "windows": [
            {"role": "main", "name": "base", "names": ["Calculator"],
             "window_path": "main", "process_names": ["Calculator"],
             "title_pattern": "^(计算器|Calculator)$"},
        ],
        "allowed_action_ids": ["gui.click"],
        "intents": {},
    }


def _catalog(*apps: dict) -> dict:
    return {
        "schema_version": 1,
        "default_app": "qa_counterexample_base",
        "apps": [
            {"app_id": "qa_counterexample_base", "name": "counterexample base",
             "profiles": [_macos_variant()]},
            *apps,
        ],
    }


def _windows_app(app_id: str, variant: dict) -> dict:
    return {"app_id": app_id, "name": app_id, "profiles": [variant]}


def test_catalog_contract_requires_application_path():
    # 独立复验反例：application={} 不满足目录契约（注册必需 path）。
    bad = _variant(profile_id="qa_bad_example", application={})
    with pytest.raises(ValueError, match="app_catalog_path_required"):
        register_app_catalog(_catalog(_windows_app("qa_bad_example", bad)))


def test_catalog_contract_requires_intents_object():
    # 独立复验反例：intents=[] 不是 dict，规范要求 intents 为对象。
    # register_product_spec 接收目录循环变换后的 prepared 形态
    # （schema_version + window_roles），校验失败先于任何全局注册。
    bad = _variant(profile_id="qa_bad_example", intents=[])
    prepared = {**bad, "schema_version": 1, "window_roles": bad["windows"]}
    with pytest.raises(ValueError, match="product_spec_intents_invalid"):
        register_product_spec(prepared)


def test_qa_fixture_capability_registered_with_minimal_actions():
    capability = get_capability("qa_recording_fixture", "windows_pywinauto")
    assert capability.allowed_action_ids == frozenset({"gui.click", "gui.type_text"})
    role = capability.window_roles[0]
    assert role.role == "main"
    assert role.process_names == ("powershell.exe",)
    assert role.title_pattern == r"^EDR-WD Native Recording QA \[qa-dsh-fixed-1025\]$"
    assert role.root_control_ids == ("RecordingFixture",)


def test_qa_fixture_digest_is_deterministic_and_agent_side_matches():
    capability = get_capability("qa_recording_fixture", "windows_pywinauto")
    assert capability.digest == get_capability(
        "qa_recording_fixture", "windows_pywinauto").digest
    profile = get_product_profile("qa_recording_fixture", "windows_pywinauto")
    assert profile.digest == capability.digest
    assert backend_for_product("qa_recording_fixture") == "windows_pywinauto"


def test_edr_and_existing_entries_survive_fixture_registration():
    hisec = get_capability("windows_hisec", "windows_pywinauto")
    assert any(a.startswith("hisec.") for a in hisec.allowed_action_ids)
    assert get_capability("windows_notepad", "windows_pywinauto")
    catalog = json.loads(
        (Path(__file__).resolve().parents[2] / "target" / "product_specs" / "apps.json")
        .read_text(encoding="utf-8"))
    app_ids = {app["app_id"] for app in catalog["apps"]}
    assert {"edrclient", "notepad", "calculator", "qa_recording_fixture"} <= app_ids


def test_product_run_rejects_actions_outside_fixture_capability():
    capability = get_capability("qa_recording_fixture", "windows_pywinauto")
    context = ProductRunContext("run-1", "tok-1", capability, time.monotonic() + 60)
    assert check_product_action(context, "gui.click") is None
    assert check_product_action(context, "gui.type_text") is None
    for action_id in ("hisec.policy_apply", "observe.control_tree", "gui.click_target"):
        receipt = check_product_action(context, action_id)
        assert receipt is not None and receipt.code == "product_action_forbidden"
        assert receipt.ok is False


# --- runtime 显式绑定 -------------------------------------------------------


def _event(sequence: int, kind: str = "pointer_click") -> dict:
    return {
        "sequence": sequence,
        "wallTime": f"2026-10-25T04:30:0{sequence}Z",
        "monotonicMs": 82254921 + sequence * 500,
        "type": kind,
        "scope": {
            "target": "target",
            "backend": "windows_pywinauto",
            "processName": "powershell.exe",
            "windowTitle": "^EDR-WD Native Recording QA \\[qa-dsh-fixed-1025\\]$",
        },
        "input": {"button": "left", "clickCount": 1, "screenPoint": [397, 335]},
        "observedTarget": {
            "snapshotId": f"OBS-{sequence}", "targetId": "T0001",
            "fingerprint": "sha256:fixture-save", "controlType": "Button",
            "automationId": "FixtureSave", "text": "Save sample",
            "protected": False,
        },
        "evidence": {"window": {"title": _TITLE, "processName": "powershell.exe",
                                "handle": 394122}},
        "causalId": f"cause-{sequence}",
        "assertion": None,
    }


def _qa_bound_golden():
    recording = RawRecording.from_dict({
        "schema": "edr.desktop-recording/v1",
        "sessionId": "REC-QA-BIND",
        "name": "qa binding",
        "captureDiagnostics": {"droppedPackets": 0, "correlationErrorCount": 0},
        "events": [_event(1)],
    })
    result = compile_recording(recording, profile="qa_recording_fixture")
    assert result.golden.status == "ready"
    assert result.golden.environment["profile"] == "qa_recording_fixture"
    return result.golden


class _StubAgent:
    def __init__(self):
        self.calls = []

    def tools_list(self):
        names = ("begin_product_run", "end_product_run", "observe_window_bundle",
                 "focus_window_role", "focus_window_selector")
        return {"tools": [{"name": name} for name in names]}

    def call_tool(self, name, arguments, timeout=None):
        self.calls.append((name, dict(arguments or {})))
        if name == "begin_product_run":
            return {"ok": True, "run_token": "tok-test"}
        return {"ok": True}


def test_runtime_begins_product_run_with_explicit_qa_profile(tmp_path):
    golden = _qa_bound_golden()
    agent = _StubAgent()

    build_replay_runtime(
        agent, golden, profile="qa_recording_fixture",
        trace_root=str(tmp_path / "traces"), asset_root=str(tmp_path),
        replay_mode="semantic_only", max_depth=6, timeout=5,
        persist_screenshots=False, confirm_actions=[],
    )

    begins = [args for name, args in agent.calls if name == "begin_product_run"]
    assert len(begins) == 1
    assert begins[0]["profile_id"] == "qa_recording_fixture"
    capability = get_capability("qa_recording_fixture", "windows_pywinauto")
    assert begins[0]["digest"] == capability.digest


def test_runtime_refuses_default_profile_for_qa_bound_golden(tmp_path):
    golden = _qa_bound_golden()
    agent = _StubAgent()

    with pytest.raises(Exception) as excinfo:
        build_replay_runtime(
            agent, golden, profile="windows_hisec",
            trace_root=str(tmp_path / "traces"), asset_root=str(tmp_path),
            replay_mode="semantic_only", max_depth=6, timeout=5,
            persist_screenshots=False, confirm_actions=[],
        )

    assert getattr(excinfo.value, "code", "") == "golden_profile_mismatch"
    assert not [name for name, _ in agent.calls if name == "begin_product_run"]
