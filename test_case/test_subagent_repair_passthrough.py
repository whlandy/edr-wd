"""Phase 3: TargetSubAgent passthrough of repair to target_manager.

Design rule: subagent MUST NOT branch on repair — it is a pure passthrough.
This keeps the cascade logic in the leaf entry points
(target_manager.ensure_server_running / stop_server) where it belongs.
"""

from __future__ import annotations


def _make_agent(monkeypatch, target_manager):
    """Build a TargetSubAgent instance backed by a fake config; returns the agent.

    The subagent must work without a real TargetConfig, so we replace the
    `config` attribute directly with a stub that exposes build_mcp_url.
    """
    from agent.subagent.target_agent import TargetSubAgent

    class StubConfig:
        def build_mcp_url(self, name):
            return f"http://stub/{name}"

    # Avoid __init__'s TargetConfig() call by going through __new__
    agent = TargetSubAgent.__new__(TargetSubAgent)
    agent.target = "unit-target"
    agent.config = StubConfig()
    agent.state = type("S", (), {})()
    agent.state.server_running = False
    agent.state.mcp_url = None
    agent.state.ready_level = "tcp_only"
    agent.state.backend_kind = None
    agent.state.health = None
    agent.state.last_error = None
    agent.state.session_id = None
    # _lock is used by ensure_running / ensure_ready / initialize_mcp;
    # ensure_ready calls ensure_running and then initialize_mcp on the
    # SAME thread, so we need an RLock to allow reentrant acquisition.
    import threading
    agent._lock = threading.RLock()
    return agent


def test_ensure_running_default_repair_false_passes_through(monkeypatch):
    """ensure_running() called without repair kwarg forwards repair=False
    to target_manager.ensure_server_running."""
    from agent.subagent import target_agent

    captured = []

    def fake_ensure(name, *, repair=False):
        captured.append((name, repair))
        return {"ok": True, "stage": "ensure", "data": {"status": "started"}}

    monkeypatch.setattr(target_agent.target_manager, "ensure_server_running", fake_ensure)

    agent = _make_agent(monkeypatch, target_agent.target_manager)
    result = agent.ensure_running()

    assert captured == [("unit-target", False)]
    assert result["ok"] is True


def test_ensure_running_repair_true_forwarded(monkeypatch):
    """ensure_running(repair=True) forwards repair=True."""
    from agent.subagent import target_agent

    captured = []

    def fake_ensure(name, *, repair=False):
        captured.append((name, repair))
        return {"ok": True, "stage": "ensure", "data": {"status": "started"}}

    monkeypatch.setattr(target_agent.target_manager, "ensure_server_running", fake_ensure)

    agent = _make_agent(monkeypatch, target_agent.target_manager)
    result = agent.ensure_running(repair=True)

    assert captured == [("unit-target", True)]
    assert result["ok"] is True


def test_ensure_ready_default_repair_false_passes_through(monkeypatch):
    """ensure_ready() called without repair kwarg forwards repair=False
    to ensure_running.  We use an ok=False result to short-circuit
    before initialize_mcp, avoiding a pre-existing refresh_status /
    initialize_mcp recursion in target_agent (not our concern here).
    """
    from agent.subagent import target_agent

    captured = []

    def fake_ensure(name, *, repair=False):
        captured.append((name, repair))
        return {"ok": False, "stage": "ensure", "code": "target_payload_incomplete"}

    monkeypatch.setattr(target_agent.target_manager, "ensure_server_running", fake_ensure)

    agent = _make_agent(monkeypatch, target_agent.target_manager)
    result = agent.ensure_ready()

    assert captured == [("unit-target", False)]
    assert result["code"] == "target_payload_incomplete"


def test_ensure_ready_repair_true_forwarded(monkeypatch):
    """ensure_ready(repair=True) forwards repair=True to ensure_running."""
    from agent.subagent import target_agent

    captured = []

    def fake_ensure(name, *, repair=False):
        captured.append((name, repair))
        return {"ok": False, "stage": "ensure", "code": "target_payload_incomplete"}

    monkeypatch.setattr(target_agent.target_manager, "ensure_server_running", fake_ensure)

    agent = _make_agent(monkeypatch, target_agent.target_manager)
    result = agent.ensure_ready(repair=True)

    assert captured == [("unit-target", True)]
    assert result["code"] == "target_payload_incomplete"


def test_ensure_running_does_not_branch_on_repair_locally(monkeypatch):
    """The subagent must not introduce its own repair cascade logic.
    If ensure_server_running returns ok=False with repair=False, the
    subagent surfaces that error unchanged — no local deploy/install."""
    from agent.subagent import target_agent

    deploy_calls = []
    install_calls = []

    def fake_ensure(name, *, repair=False):
        return {
            "ok": False,
            "stage": "ensure",
            "code": "target_payload_incomplete",
            "error": "missing",
        }

    monkeypatch.setattr(target_agent.target_manager, "ensure_server_running", fake_ensure)
    monkeypatch.setattr(target_agent.target_manager, "deploy_target", lambda *a, **k: deploy_calls.append((a, k)))
    monkeypatch.setattr(target_agent.target_manager, "install_target_task", lambda *a, **k: install_calls.append((a, k)))

    agent = _make_agent(monkeypatch, target_agent.target_manager)
    result = agent.ensure_running(repair=True)

    # The error surfaces unchanged
    assert result["code"] == "target_payload_incomplete"
    # CRITICAL: the subagent did NOT call deploy_target or install_target_task
    assert deploy_calls == []
    assert install_calls == []