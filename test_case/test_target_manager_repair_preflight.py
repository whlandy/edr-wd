"""A repair with an unavailable target runtime must not mutate the target."""

import pytest

from agent import target_manager


pytestmark = pytest.mark.unit


def test_repair_probes_before_deploy_install_or_restart(monkeypatch):
    calls = []

    class Config:
        def get_default_target(self):
            return "11.26"

    monkeypatch.setattr(target_manager, "TargetConfig", Config)

    def probe(name):
        calls.append(("probe", name))
        return {"ok": False, "stage": "probe", "code": "python_not_found",
                "error": "configured Python is absent"}

    def forbidden(*_args, **_kwargs):
        pytest.fail("repair must not change the target after failed preflight")

    monkeypatch.setattr(target_manager, "probe_target", probe)
    monkeypatch.setattr(target_manager, "deploy_target", forbidden)
    monkeypatch.setattr(target_manager, "install_target_task", forbidden)
    monkeypatch.setattr(target_manager, "ensure_server_running", forbidden)

    result = target_manager.repair_target("11.26", repair=True)

    assert result["code"] == "python_not_found"
    assert calls == [("probe", "11.26")]


def test_repair_runs_all_steps_after_successful_preflight(monkeypatch):
    calls = []

    class Config:
        def get_default_target(self):
            return "11.26"

    monkeypatch.setattr(target_manager, "TargetConfig", Config)

    def success(stage):
        def run(name, *args, **kwargs):
            calls.append(stage)
            return {"ok": True, "stage": stage, "data": {}}
        return run

    monkeypatch.setattr(target_manager, "probe_target", success("probe"))
    monkeypatch.setattr(target_manager, "deploy_target", success("deploy"))
    monkeypatch.setattr(target_manager, "install_target_task", success("install"))
    monkeypatch.setattr(target_manager, "ensure_server_running", success("ensure"))

    result = target_manager.repair_target("11.26", repair=True)

    assert result["ok"] is True
    assert calls == ["probe", "deploy", "install", "ensure"]
    assert result["data"]["repair_actions"] == [
        "probe_target", "deploy_target", "install_target_task",
        "ensure_server_running",
    ]
