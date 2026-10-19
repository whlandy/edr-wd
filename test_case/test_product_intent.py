"""EDRClient log-center intent is explicit, idempotent and page-asserted."""

from __future__ import annotations

import pytest

from agent.products import get_product_profile
from agent.products.edrclient import EDRClientAdapter
from agent.products.generic import GenericProductAdapter
from agent.products.intent import IntentRequest, validate_intent_request
from agent.interaction.runtime import execute_intent
from agent.execution import BackendUnavailable
from target.product_profiles import (BUILTIN_CAPABILITIES, INSTALLED_PRODUCT_SPECS,
                                     register_product_spec)
from target.windowing import WindowRegistry


def _request(**changes):
    values = {"profile_id": "windows_hisec",
              "intent_id": "edrclient.log_center.open",
              "arguments": {}, "expected_result": {"page": "log_center"}}
    values.update(changes)
    return IntentRequest(**values)


def _window(handle, process, title, root=""):
    return {"handle": handle, "pid": handle, "process_name": process,
            "title": title, "root_control_id": root,
            "rect": [0, 0, 800, 600]}


def test_intent_validation_rejects_unknown_or_unasserted_requests():
    profile = get_product_profile("windows_hisec", "windows_pywinauto")
    validate_intent_request(_request(), profile)
    with pytest.raises(ValueError, match="product_intent_unknown"):
        validate_intent_request(_request(intent_id="unknown"), profile)
    with pytest.raises(ValueError, match="product_intent_expected_result_missing"):
        validate_intent_request(_request(expected_result={}), profile)


def test_generic_activation_preserves_backend_failure_code():
    adapter = GenericProductAdapter("macos_calculator", "macos_accessibility")
    result = adapter.activate(
        lambda name, args: {"ok": False, "code": "product_window_missing",
                            "error": "no owned window"}
    )
    assert result == {"ok": False, "code": "product_window_missing",
                      "error": "no owned window"}


def test_log_center_plan_skips_click_only_for_verified_product_role():
    profile = get_product_profile("windows_hisec", "windows_pywinauto")
    adapter = EDRClientAdapter("windows_pywinauto")
    registry = WindowRegistry("s", "windows_pywinauto")
    registry.reconcile([_window(1, "EDRClient.exe", "EDRClient", "EDRClient")])
    closed = adapter.resolve_intent("edrclient.log_center.open", {}, registry=registry,
                                    profile=profile)
    assert closed.click_required is True
    registry.reconcile([
        _window(1, "EDRClient.exe", "EDRClient", "EDRClient"),
        _window(2, "EDRClient.exe", "日志中心", "logCenter"),
    ])
    opened = adapter.resolve_intent("edrclient.log_center.open", {}, registry=registry,
                                    profile=profile)
    assert opened.click_required is False
    assert any(item["type"] == "control_exists" for item in opened.result_assertions)
    registry.reconcile([
        _window(1, "EDRClient.exe", "EDRClient", "EDRClient"),
        _window(3, "Other.exe", "日志中心", "logCenter"),
    ])
    spoof = adapter.resolve_intent("edrclient.log_center.open", {}, registry=registry,
                                   profile=profile)
    assert spoof.click_required is True


def test_generic_product_intent_executes_with_its_own_roles_and_assertions(monkeypatch):
    spec = {
        "schema_version": 1, "profile_id": "sample_editor", "version": "1",
        "backend": "windows_pywinauto",
        "window_roles": [
            {"role": "main", "process_names": ["SampleEditor.exe"],
             "title_pattern": "^Sample Editor$"},
            {"role": "about", "process_names": ["SampleEditor.exe"],
             "title_pattern": "^About Sample Editor$", "parent_role": "main"},
        ],
        "allowed_action_ids": ["gui.click", "observe.control_tree"],
        "intents": {"sample_editor.about.open": {
            "source_role": "main", "result_role": "about",
            "selector": {"name": "About"},
            "assertions": [
                {"type": "active_window_owner", "value": "SampleEditor.exe"},
                {"type": "control_exists", "value": {"text": "Sample Editor"}},
            ],
        }},
    }
    class Passed:
        outcome = type("Outcome", (), {"value": "passed"})()
    class Observation:
        def __init__(self):
            self.roles = []
        def focus_role(self, role):
            self.roles.append(role)
            if len(self.roles) == 1:
                raise BackendUnavailable("window_role_not_found")
    class Executor:
        def __init__(self):
            self.case = None
        def run_case(self, case, **kwargs):
            self.case = case
            return Passed()
    try:
        register_product_spec(spec)
        from agent.products import get_product_adapter
        from agent.interaction import runtime
        profile = get_product_profile("sample_editor", "windows_pywinauto")
        adapter = get_product_adapter("sample_editor", "windows_pywinauto")
        observation = Observation()
        executor = Executor()
        seen = []
        def live_action(request, **kwargs):
            seen.append(request)
            return Passed()
        monkeypatch.setattr(runtime, "execute_live_action", live_action)
        result = execute_intent(
            IntentRequest("sample_editor", "sample_editor.about.open", {}, {"page": "about"}),
            adapter=adapter, profile=profile, executor=executor,
            observation_provider=observation,
        )
        assert result.passed is True
        assert observation.roles == ["about", "main", "about"]
        assert seen[0].window["processName"] == "SampleEditor.exe"
        assert seen[0].control == {"name": "About"}
        assert executor.case.steps[0].expectations[0].value == "SampleEditor.exe"
    finally:
        BUILTIN_CAPABILITIES.pop(("sample_editor", "windows_pywinauto"), None)
        INSTALLED_PRODUCT_SPECS.pop(("sample_editor", "windows_pywinauto"), None)
