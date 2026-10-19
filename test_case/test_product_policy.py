"""Product capability and target lease acceptance checks."""

from __future__ import annotations

import pytest
from types import SimpleNamespace

from agent.products import get_product_adapter, get_product_profile, register_product_adapter
from agent.planner.catalog_view import enabled_actions_for
from target import product_policy
from target.action_dispatcher import cache_clear, dispatch, set_backend_resolver
from target.product_profiles import (BUILTIN_CAPABILITIES, ProductCapability,
                                     ProductWindowRole, get_capability,
                                     register_product_spec, INSTALLED_PRODUCT_SPECS)


@pytest.fixture(autouse=True)
def reset_policy():
    product_policy.reset_for_tests()
    cache_clear()
    yield
    product_policy.reset_for_tests()
    cache_clear()


def test_profile_validation_and_role_resolution():
    for profile_id, backend in (
        ("windows_hisec", "windows_pywinauto"),
        ("macos_hisec", "macos_accessibility"),
        ("macos_generic", "macos_accessibility"),
    ):
        profile = get_product_profile(profile_id, backend)
        profile.validate()
        assert profile.digest == get_capability(profile_id, backend).digest
    with pytest.raises(ValueError, match="product_profile_unknown"):
        get_product_profile("unknown", "windows_pywinauto")
    with pytest.raises(ValueError, match="product_profile_unknown"):
        get_product_profile("windows_hisec", "macos_accessibility")


def test_generic_hides_hisec_and_target_capability_matches_planner():
    for profile_id, backend in (
        ("windows_hisec", "windows_pywinauto"),
        ("macos_hisec", "macos_accessibility"),
        ("macos_generic", "macos_accessibility"),
    ):
        planned = {spec.action_id for spec in enabled_actions_for(backend, profile_id)}
        assert planned == get_capability(profile_id, backend).allowed_action_ids
    assert not any(s.action_id.startswith("hisec.") for s in enabled_actions_for(
        "macos_accessibility", "macos_generic"))


def test_non_edr_products_are_selectable_without_a_custom_python_adapter():
    from agent.target_config import supported_profiles
    for profile_id, backend, platform, process in (
        ("windows_notepad", "windows_pywinauto", "windows", "Notepad.exe"),
        ("macos_calculator", "macos_accessibility", "macos", "Calculator"),
    ):
        profile = get_product_profile(profile_id, backend)
        adapter = get_product_adapter(profile_id, backend)
        assert adapter.resolve_window_role("main").process_names == (process,)
        assert profile.digest == get_capability(profile_id, backend).digest
        assert profile_id in supported_profiles(platform)
        assert {item.action_id for item in enabled_actions_for(backend, profile_id)} == \
            get_capability(profile_id, backend).allowed_action_ids
        assert not any(action.startswith("hisec.") for action in profile.allowed_action_ids)


def test_generic_product_runner_and_backend_check_do_not_default_to_edr():
    from agent.subagent.target_agent import TargetSubAgent
    from test_case.test_profiles import resolve_runner
    agent = TargetSubAgent.__new__(TargetSubAgent)
    agent.state = SimpleNamespace(backend_kind="uia")
    assert agent.profile_backend_conflict("windows_notepad") is False
    assert agent.profile_backend_conflict("macos_calculator") is True
    assert resolve_runner("windows_notepad").__name__ == "run_registered_product_tests"
    assert resolve_runner("not_installed") is None

    class Client:
        def __init__(self):
            self.calls = []
        def call_tool(self, name, args):
            self.calls.append(name)
            if name == "status":
                return {"ok": True, "backend": "macos_accessibility"}
            if name == "begin_product_run":
                return {"ok": True, "run_token": "t"}
            if name == "observe_window_bundle":
                return {"ok": True, "window_identity": {"process_name": "Calculator"}}
            return {"ok": True}
    client = Client()
    passed, failed, skipped, errors, ok = resolve_runner("macos_calculator")(client)
    assert (passed, failed, skipped, errors, ok) == (6, 0, 0, [], True)
    assert client.calls == ["status", "activate_product_app", "begin_product_run",
                            "focus_window_role", "observe_window_bundle", "end_product_run"]


def test_declarative_product_intent_and_target_lease_share_one_descriptor():
    spec = {
        "schema_version": 1, "profile_id": "example_editor", "version": "1",
        "backend": "windows_pywinauto",
        "application": {"app_name": "ExampleEditor.exe"},
        "window_roles": [
            {"role": "main", "process_names": ["ExampleEditor.exe"],
             "title_pattern": "^Example Editor$"},
            {"role": "about", "process_names": ["ExampleEditor.exe"],
             "title_pattern": "^About Example Editor$", "parent_role": "main"},
        ],
        "allowed_action_ids": ["gui.click", "observe.control_tree"],
        "intents": {"example_editor.about.open": {
            "source_role": "main", "result_role": "about",
            "selector": {"name": "About"},
            "assertions": [
                {"type": "active_window_owner", "value": "ExampleEditor.exe"},
                {"type": "control_exists", "value": {"text": "About Example Editor"}},
            ],
        }},
    }
    try:
        capability = register_product_spec(spec)
        adapter = get_product_adapter("example_editor", "windows_pywinauto")
        plan = adapter.resolve_intent("example_editor.about.open", {})
        assert (plan.main_role, plan.result_role, plan.selector) == \
            ("main", "about", {"name": "About"})
        assert adapter.expected_result("example_editor.about.open", {}) == tuple(
            spec["intents"]["example_editor.about.open"]["assertions"])
        assert adapter.activate(lambda name, params: {"ok": name == "activate_app",
                                                     "params": params})["params"] == \
            {"app_name": "ExampleEditor.exe"}
        run = product_policy.begin_product_run("example_editor", capability.digest,
                                               backend_kind="windows_pywinauto")
        product_policy.end_product_run(run.run_token)
    finally:
        BUILTIN_CAPABILITIES.pop(("example_editor", "windows_pywinauto"), None)
        INSTALLED_PRODUCT_SPECS.pop(("example_editor", "windows_pywinauto"), None)


def test_declarative_product_rejects_unowned_windows_and_unknown_actions():
    base = {"schema_version": 1, "profile_id": "bad_product", "version": "1",
            "backend": "windows_pywinauto", "window_roles": [
                {"role": "main", "process_names": []}],
            "allowed_action_ids": ["gui.click"]}
    with pytest.raises(ValueError, match="product_spec_ownership_required"):
        register_product_spec(base)
    base["window_roles"][0]["process_names"] = ["Other.exe"]
    base["allowed_action_ids"] = ["unknown.action"]
    with pytest.raises(ValueError, match="product_profile_actions_invalid"):
        register_product_spec(base)
    assert ("bad_product", "windows_pywinauto") not in BUILTIN_CAPABILITIES


def test_locally_installed_fake_product_uses_only_profile_adapter_boundary():
    from agent.products import registry

    class FakeAdapter:
        def __init__(self, backend):
            self.backend = backend
        def resolve_window_role(self, role):
            return role
        def resolve_intent(self, intent_id, args):
            return intent_id
        def expected_result(self, intent_id, args):
            return ({"type": "action_ok", "value": True},)

    capability = ProductCapability(
        "fake_product", "1", "macos_accessibility",
        frozenset({"gui.click"}), (ProductWindowRole("main", ("FakeApp",)),),
    )
    try:
        register_product_adapter("fake_product", FakeAdapter, capability)
        profile = get_product_profile("fake_product", "macos_accessibility")
        assert profile.digest == capability.digest
        assert get_product_adapter("fake_product", "macos_accessibility").backend == "macos_accessibility"
        from agent.target_config import supported_profiles
        assert "fake_product" in supported_profiles("macos")
        assert "fake_product" not in supported_profiles("windows")
        run = product_policy.begin_product_run("fake_product", capability.digest,
                                               backend_kind="macos_accessibility")
        product_policy.end_product_run(run.run_token)
    finally:
        registry._adapters.pop("fake_product", None)
        BUILTIN_CAPABILITIES.pop(("fake_product", "macos_accessibility"), None)


def test_manifest_rejects_unknown_and_bad_digest(monkeypatch):
    monkeypatch.delenv("EDR_WD_ALLOWED_PROFILES", raising=False)
    cap = get_capability("windows_hisec", "windows_pywinauto")
    assert product_policy.load_allowed_profiles({"windows_hisec": cap.digest})
    with pytest.raises(ValueError):
        product_policy.load_allowed_profiles({"windows_hisec": "bad"})
    with pytest.raises(ValueError):
        product_policy.load_allowed_profiles({"not_registered": "bad"})


def test_lease_is_exclusive_and_legacy_backend_is_fenced():
    cap = get_capability("macos_generic", "macos_accessibility")
    class Backend:
        backend_kind = "macos_accessibility"
        calls = 0
        def click_at(self, x, y):
            self.calls += 1
            return {"ok": True}
    backend = Backend()
    wrapped = product_policy.GuardedBackend(backend)
    run = product_policy.begin_product_run(cap.profile_id, cap.digest)
    with pytest.raises(ValueError, match="product_run_conflict"):
        product_policy.begin_product_run(cap.profile_id, cap.digest)
    assert wrapped.click_at(1, 2)["code"] == "product_run_conflict"
    with product_policy.authorized_dispatch(run.run_token):
        assert wrapped.click_at(1, 2)["ok"] is True
    assert backend.calls == 1
    product_policy.end_product_run(run.run_token)
    assert wrapped.click_at(1, 2)["ok"] is True
    with pytest.raises(ValueError, match="product_run_expired"):
        product_policy.require_product_run(run.run_token)


def test_backend_proxy_exposes_selected_platform_to_dispatcher():
    class Backend:
        pass

    wrapped = product_policy.GuardedBackend(Backend(), "windows_pywinauto")
    assert wrapped.backend_kind == "windows_pywinauto"


def test_dispatch_token_checked_before_cache_and_scoped_between_runs():
    cap = get_capability("macos_generic", "macos_accessibility")
    class Backend:
        backend_kind = "macos_accessibility"
        calls = 0
        def get_window_lock(self):
            return {"ok": True, "process_name": "Demo"}
        def click(self, **kwargs):
            self.calls += 1
            return {"ok": True}
    backend = Backend()
    set_backend_resolver(lambda: product_policy.GuardedBackend(backend))
    args = dict(action_id="gui.click", args={"control_id": 1},
                target_ref={"selector_hint": {"control_id": 1}}, request_id="same")
    first = product_policy.begin_product_run(cap.profile_id, cap.digest)
    assert dispatch(run_token=first.run_token, **args).ok
    assert dispatch(run_token=first.run_token, **args).ok
    assert backend.calls == 1
    assert dispatch(run_token="invalid", **args).code == "product_run_conflict"
    product_policy.end_product_run(first.run_token)
    assert dispatch(run_token=first.run_token, **args).code == "product_run_expired"
    second = product_policy.begin_product_run(cap.profile_id, cap.digest)
    assert dispatch(run_token=second.run_token, **args).ok
    assert backend.calls == 2
    product_policy.end_product_run(second.run_token)
