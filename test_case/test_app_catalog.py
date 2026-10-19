"""One app catalog owns default selection, paths, and all declared windows."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.target_config import TargetConfig
from agent import cli
from target import product_policy, server
from target.recording.models import RecordingModelError
from target.product_profiles import (BUILTIN_CAPABILITIES, DEFAULT_APP_ID,
                                     INSTALLED_APPS, default_profile_for_backend,
                                     register_app_catalog)


def test_one_catalog_contains_default_edr_and_each_apps_window_inventory():
    path = Path(__file__).resolve().parents[1] / "target/product_specs/apps.json"
    catalog = json.loads(path.read_text(encoding="utf-8"))
    assert catalog["default_app"] == DEFAULT_APP_ID == "edrclient"
    assert set(INSTALLED_APPS) == {item["app_id"] for item in catalog["apps"]}
    assert default_profile_for_backend("windows_pywinauto") == "windows_hisec"
    assert default_profile_for_backend("macos_accessibility") == "macos_hisec"
    for app in catalog["apps"]:
        for profile in app["profiles"]:
            assert profile["application"]["path"]
            assert profile["windows"]
            assert all(window["name"] and window["window_path"]
                       for window in profile["windows"])
            assert (profile["profile_id"], profile["backend"]) in BUILTIN_CAPABILITIES


def test_product_run_can_start_before_any_recording_session(monkeypatch):
    class NoRecording:
        def status(self, *, heartbeat=False):
            raise RecordingModelError("recording_session_missing", "no recording session exists")

    product_policy.reset_for_tests()
    monkeypatch.setattr(server, "_recording_service", NoRecording())
    backend = server._backend_kind
    profile_id = default_profile_for_backend(backend)
    capability = BUILTIN_CAPABILITIES[(profile_id, backend)]
    started = json.loads(server.begin_product_run(profile_id, capability.digest))
    assert started["ok"] is True
    try:
        assert started["profile_id"] == profile_id
    finally:
        assert json.loads(server.end_product_run(started["run_token"]))["ok"] is True


def test_catalog_rejects_missing_paths_or_window_names_without_partial_registration():
    path = Path(__file__).resolve().parents[1] / "target/product_specs/apps.json"
    source = json.loads(path.read_text(encoding="utf-8"))
    app = source["apps"][1]
    app["app_id"] = "invalid_notepad"
    app["profiles"][0]["profile_id"] = "invalid_notepad"
    catalog = {"schema_version": 1, "default_app": "invalid_notepad", "apps": [app]}
    before = set(BUILTIN_CAPABILITIES)
    catalog["apps"][0]["profiles"][0]["application"].pop("path")
    with pytest.raises(ValueError, match="app_catalog_path_required"):
        register_app_catalog(catalog)
    assert set(BUILTIN_CAPABILITIES) == before
    catalog["apps"][0]["profiles"][0]["application"]["path"] = \
        "C:\\Windows\\System32\\notepad.exe"
    catalog["apps"][0]["profiles"][0]["windows"][0]["name"] = ""
    with pytest.raises(ValueError, match="app_catalog_windows_invalid"):
        register_app_catalog(catalog)
    assert set(BUILTIN_CAPABILITIES) == before
    catalog["apps"][0]["profiles"][0]["windows"][0]["name"] = "记事本主窗口"
    catalog["apps"][0]["profiles"][0]["windows"][0]["window_path"] = "elsewhere"
    with pytest.raises(ValueError, match="app_catalog_window_path_invalid"):
        register_app_catalog(catalog)
    assert set(BUILTIN_CAPABILITIES) == before


def test_target_profile_defaults_to_edr_but_explicit_product_wins():
    config = TargetConfig.__new__(TargetConfig)
    config.get_target = lambda _name=None: {"platform": "windows"}
    assert config.get_target_app_profile("demo") == "windows_hisec"
    config.get_target = lambda _name=None: {"platform": "macos"}
    assert config.get_target_app_profile("demo") == "macos_hisec"
    config.get_target = lambda _name=None: {
        "platform": "windows", "app_profile": "windows_notepad"}
    assert config.get_target_app_profile("demo") == "windows_notepad"


def test_products_command_shows_default_app_paths_and_window_names(capsys):
    assert cli.main(["products"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["default_app"] == "edrclient"
    edr = next(app for app in output["apps"] if app["app_id"] == "edrclient")
    windows = next(profile for profile in edr["profiles"]
                   if profile["backend"] == "windows_pywinauto")
    assert windows["application"]["entry_path"].endswith("HisecEndpointAgent.exe")
    assert {window["role"] for window in windows["windows"]} == {
        "entry", "main", "log_center", "dialog"}
    assert all(window["name"] and window["window_path"]
               for window in windows["windows"])


def test_target_activates_only_locally_registered_path_and_owner(monkeypatch):
    product_policy.reset_for_tests()
    monkeypatch.delenv("EDR_WD_ALLOWED_PROFILES", raising=False)
    calls = []
    class Backend:
        def activate_registered_app(self, name, *, _resolve):
            calls.append((name, _resolve(name)))
            return {"ok": True, "method": "test"}
    monkeypatch.setattr(server, "_backend", Backend())
    monkeypatch.setattr(server, "_backend_kind", "windows_pywinauto")
    result = json.loads(server.activate_product_app("windows_notepad"))
    assert result["ok"] is True
    assert calls == [("notepad.exe", "C:\\Windows\\System32\\notepad.exe")]
    assert json.loads(server.activate_product_app("macos_calculator"))["code"] == \
        "product_profile_unknown"
    run = product_policy.begin_product_run(
        "windows_notepad", BUILTIN_CAPABILITIES[("windows_notepad", "windows_pywinauto")].digest,
        backend_kind="windows_pywinauto")
    try:
        assert json.loads(server.activate_product_app("windows_notepad"))["code"] == \
            "product_run_conflict"
    finally:
        product_policy.end_product_run(run.run_token)


def test_edr_activation_rejects_path_outside_catalog(monkeypatch):
    calls = []
    class Backend:
        def activate_edr(self, **kwargs):
            calls.append(kwargs)
            return {"ok": True}
    monkeypatch.setattr(server, "_backend", Backend())
    monkeypatch.setattr(server, "_backend_kind", "windows_pywinauto")
    refused = json.loads(server.activate_edr(exe_path="C:\\Other\\Unregistered.exe"))
    assert refused["code"] == "product_application_path_unregistered"
    assert calls == []
    accepted = json.loads(server.activate_edr())
    assert accepted["ok"] is True
    assert calls[0]["exe_path"] == \
        "C:\\Program Files\\HiSec-Endpoint\\core\\safra\\HisecEndpointAgent.exe"
