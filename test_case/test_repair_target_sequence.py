"""Regression tests for the explicit target repair sequence."""

import json


def test_repair_target_runs_deploy_install_ensure(monkeypatch):
    from agent import target_manager

    calls = []

    class FakeTargetConfig:
        def get_default_target(self):
            return "unit-target"

    monkeypatch.setattr(target_manager, "TargetConfig", FakeTargetConfig)
    monkeypatch.setattr(
        target_manager,
        "deploy_target",
        lambda name: calls.append(("deploy", name)) or {"ok": True, "data": {}},
    )
    monkeypatch.setattr(
        target_manager,
        "install_target_task",
        lambda name: calls.append(("install", name)) or {"ok": True, "data": {}},
    )
    monkeypatch.setattr(
        target_manager,
        "ensure_server_running",
        lambda name, *, repair=False: calls.append(("ensure", name, repair))
        or {"ok": True, "data": {"status": "started"}},
    )

    result = target_manager.repair_target("unit-target", repair=True)

    assert result["ok"] is True
    assert calls == [
        ("deploy", "unit-target"),
        ("install", "unit-target"),
        ("ensure", "unit-target", True),
    ]
    assert result["data"]["repair_actions"] == [
        "deploy_target",
        "install_target_task",
        "ensure_server_running",
    ]
    json.dumps(result)


def test_repair_target_stops_when_install_fails(monkeypatch):
    from agent import target_manager

    ensure_calls = []

    class FakeTargetConfig:
        def get_default_target(self):
            return "unit-target"

    monkeypatch.setattr(target_manager, "TargetConfig", FakeTargetConfig)
    monkeypatch.setattr(
        target_manager, "deploy_target", lambda _name: {"ok": True, "data": {}},
    )
    monkeypatch.setattr(
        target_manager,
        "install_target_task",
        lambda _name: {"ok": False, "code": "install_failed"},
    )
    monkeypatch.setattr(
        target_manager,
        "ensure_server_running",
        lambda *_a, **_k: ensure_calls.append(True) or {"ok": True},
    )

    result = target_manager.repair_target("unit-target", repair=True)

    assert result["ok"] is False
    assert result["code"] == "install_failed"
    assert ensure_calls == []
