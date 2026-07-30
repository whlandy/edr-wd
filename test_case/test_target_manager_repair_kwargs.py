"""Phase 3: repair=False / repair=True plumbing for stop_server / restart_server.

Verifies:
  - repair=False (default) does not cascade to repair_target when the
    lifecycle backend refuses with target_payload_incomplete. Pure
    passthrough — lifecycle error surfaces unchanged.
  - repair=True cascades to repair_target(repair=True) ONLY when the
    lifecycle code is target_payload_incomplete. Other lifecycle errors
    (e.g. SSH failure) are surfaced unchanged.
  - restart_server is a pure passthrough of repair to both stop_server
    and ensure_server_running — no cascade logic of its own.

Phase 3 design rule: cascade logic lives in the leaf entry points
(stop_server, ensure_server_running). restart_server MUST NOT add
its own repair branches.
"""

from __future__ import annotations

import json


# ── stop_server ──────────────────────────────────────────────────────────────


def test_stop_server_repair_false_does_not_cascade(monkeypatch):
    """repair=False: lifecycle payload_incomplete surfaces unchanged;
    repair_target is never called."""
    from agent import target_manager

    repair_calls = []

    class FakeTargetConfig:
        def get_default_target(self):
            return "unit-target"

        def get_resolved_target(self, _name):
            return {
                "platform": "macos",
                "ssh": {"host": "127.0.0.1"},
                "mcp": {"port": 8765, "connect_mode": "tunnel"},
                "macos": {"root": "/Users/admin/edr-wd/target", "launch_name": "com.edr-wd.target"},
            }

    class FakeLifecycle:
        def stop_server(self, _cfg):
            return {
                "ok": False,
                "stage": "stop",
                "code": "target_payload_incomplete",
                "error": "tracked payload missing",
            }

    monkeypatch.setattr(target_manager, "TargetConfig", FakeTargetConfig)
    monkeypatch.setattr(target_manager, "_dispatch_lifecycle", lambda _cfg: (FakeLifecycle(), None))
    monkeypatch.setattr(target_manager, "repair_target", lambda *a, **k: repair_calls.append((a, k)) or {"ok": False})

    result = target_manager.stop_server("unit-target")  # default repair=False

    # Lifecycle error surfaced unchanged
    assert result["ok"] is False
    assert result["code"] == "target_payload_incomplete"
    # CRITICAL: repair_target was NEVER called
    assert repair_calls == [], (
        f"repair=False must not cascade to repair_target. Calls: {repair_calls}"
    )


def test_stop_server_repair_true_cascades_on_incomplete(monkeypatch):
    """repair=True AND lifecycle code=target_payload_incomplete:
    delegate to repair_target and report the cascade."""
    from agent import target_manager

    repair_calls = []

    class FakeTargetConfig:
        def get_default_target(self):
            return "unit-target"

        def get_resolved_target(self, _name):
            return {
                "platform": "macos",
                "ssh": {"host": "127.0.0.1"},
                "mcp": {"port": 8765, "connect_mode": "tunnel"},
                "macos": {"root": "/Users/admin/edr-wd/target", "launch_name": "com.edr-wd.target"},
            }

    class FakeLifecycle:
        calls = 0

        def stop_server(self, _cfg):
            self.calls += 1
            if self.calls == 2:
                return {"ok": True, "stage": "stop", "data": {"port_killed": True}}
            return {
                "ok": False,
                "stage": "stop",
                "code": "target_payload_incomplete",
                "error": "tracked payload missing",
            }

    monkeypatch.setattr(target_manager, "TargetConfig", FakeTargetConfig)
    monkeypatch.setattr(target_manager, "_dispatch_lifecycle", lambda _cfg: (FakeLifecycle(), None))

    def fake_repair_target(name, repair=False):
        repair_calls.append({"name": name, "repair": repair})
        return {
            "ok": True,
            "data": {"repair_actions": ["deploy_target", "install_target_task"]},
        }

    monkeypatch.setattr(target_manager, "repair_target", fake_repair_target)

    result = target_manager.stop_server("unit-target", repair=True)

    # CRITICAL: repair_target called exactly once with repair=True
    assert repair_calls == [{"name": "unit-target", "repair": True}]
    # The repaired payload is retried and the original stop action succeeds.
    assert result["recoverable"] is True
    assert result["ok"] is True
    assert result["data"]["lifecycle_result"]["code"] == "target_payload_incomplete"
    assert result["data"]["repair_actions"] == [
        "deploy_target", "install_target_task",
    ]
    json.dumps(result)


def test_stop_server_repair_true_recoverable_flag_set(monkeypatch):
    """When repair=True causes a cascade, the result MUST carry
    recoverable=True so UI layers can suppress hard-error display.

    This prevents false-positive error popups when the operation
    ultimately succeeded through the repair channel.
    """
    from agent import target_manager

    class FakeTargetConfig:
        def get_default_target(self):
            return "unit-target"

        def get_resolved_target(self, _name):
            return {
                "platform": "macos",
                "ssh": {"host": "127.0.0.1"},
                "mcp": {"port": 8765, "connect_mode": "tunnel"},
                "macos": {"root": "/Users/admin/edr-wd/target", "launch_name": "com.edr-wd.target"},
            }

    class FakeLifecycle:
        calls = 0

        def stop_server(self, _cfg):
            self.calls += 1
            if self.calls == 2:
                return {"ok": True, "stage": "stop", "data": {}}
            return {
                "ok": False,
                "code": "target_payload_incomplete",
            }

    monkeypatch.setattr(target_manager, "TargetConfig", FakeTargetConfig)
    monkeypatch.setattr(target_manager, "_dispatch_lifecycle", lambda _cfg: (FakeLifecycle(), None))
    monkeypatch.setattr(
        target_manager, "repair_target",
        lambda *_a, **_k: {"ok": True, "data": {"repair_actions": ["deploy"]}},
    )

    result = target_manager.stop_server("unit-target", repair=True)

    # The contract: a recoverable cascade is recoverable=True
    assert result.get("recoverable") is True


def test_stop_server_failed_repair_is_not_recoverable(monkeypatch):
    """A failed repair must remain a hard failure."""
    from agent import target_manager

    class FakeTargetConfig:
        def get_default_target(self):
            return "unit-target"

        def get_resolved_target(self, _name):
            return {
                "platform": "macos",
                "ssh": {"host": "127.0.0.1"},
                "mcp": {"port": 8765, "connect_mode": "tunnel"},
                "macos": {
                    "root": "/Users/admin/edr-wd/target",
                    "launch_name": "com.edr-wd.target",
                },
            }

    class FakeLifecycle:
        def stop_server(self, _cfg):
            return {"ok": False, "code": "target_payload_incomplete"}

    monkeypatch.setattr(target_manager, "TargetConfig", FakeTargetConfig)
    monkeypatch.setattr(
        target_manager, "_dispatch_lifecycle", lambda _cfg: (FakeLifecycle(), None),
    )
    monkeypatch.setattr(
        target_manager,
        "repair_target",
        lambda *_a, **_k: {"ok": False, "code": "deploy_failed"},
    )

    result = target_manager.stop_server("unit-target", repair=True)

    assert result["ok"] is False
    assert result["recoverable"] is False
    assert result["code"] == "stop_repair_failed"


def test_stop_server_non_cascade_errors_have_no_recoverable_flag(monkeypatch):
    """Other lifecycle errors (SSH failure etc.) MUST NOT be marked
    recoverable — they did NOT recover, they simply failed.
    """
    from agent import target_manager

    class FakeTargetConfig:
        def get_default_target(self):
            return "unit-target"

        def get_resolved_target(self, _name):
            return {
                "platform": "macos",
                "ssh": {"host": "127.0.0.1"},
                "mcp": {"port": 8765, "connect_mode": "tunnel"},
                "macos": {"root": "/Users/admin/edr-wd/target", "launch_name": "com.edr-wd.target"},
            }

    class FakeLifecycle:
        def stop_server(self, _cfg):
            return {
                "ok": False,
                "code": "target_launchagent_check_failed",
                "error": "SSH timeout",
            }

    monkeypatch.setattr(target_manager, "TargetConfig", FakeTargetConfig)
    monkeypatch.setattr(target_manager, "_dispatch_lifecycle", lambda _cfg: (FakeLifecycle(), None))

    result = target_manager.stop_server("unit-target", repair=True)

    # Non-cascade errors MUST NOT carry recoverable=True
    assert "recoverable" not in result or result["recoverable"] is False


def test_stop_server_repair_true_does_not_cascade_on_other_errors(monkeypatch):
    """repair=True but lifecycle error is SSH failure (not payload_incomplete):
    surface lifecycle error unchanged; do NOT cascade."""
    from agent import target_manager

    repair_calls = []

    class FakeTargetConfig:
        def get_default_target(self):
            return "unit-target"

        def get_resolved_target(self, _name):
            return {
                "platform": "macos",
                "ssh": {"host": "127.0.0.1"},
                "mcp": {"port": 8765, "connect_mode": "tunnel"},
                "macos": {"root": "/Users/admin/edr-wd/target", "launch_name": "com.edr-wd.target"},
            }

    class FakeLifecycle:
        def stop_server(self, _cfg):
            return {
                "ok": False,
                "stage": "stop",
                "code": "target_launchagent_check_failed",
                "error": "SSH probe failed",
            }

    monkeypatch.setattr(target_manager, "TargetConfig", FakeTargetConfig)
    monkeypatch.setattr(target_manager, "_dispatch_lifecycle", lambda _cfg: (FakeLifecycle(), None))
    monkeypatch.setattr(target_manager, "repair_target", lambda *a, **k: repair_calls.append((a, k)) or {"ok": True})

    result = target_manager.stop_server("unit-target", repair=True)

    # Lifecycle error surfaced unchanged (NOT cascaded)
    assert result["code"] == "target_launchagent_check_failed"
    # CRITICAL: repair_target was never called for non-payload errors
    assert repair_calls == [], (
        f"repair=True must only cascade on target_payload_incomplete, "
        f"not on SSH errors. Calls: {repair_calls}"
    )


# ── restart_server ───────────────────────────────────────────────────────────


def test_restart_server_repair_false_forwards_to_both(monkeypatch):
    """restart_server(repair=False) forwards repair=False to both
    stop_server and ensure_server_running. No cascade logic of its own."""
    from agent import target_manager

    forwarded = []

    def fake_stop(name, *, repair=False):
        forwarded.append(("stop", name, repair))
        return {"ok": True, "stage": "stop", "data": {"port_killed": True}}

    def fake_ensure(name, *, repair=False):
        forwarded.append(("ensure", name, repair))
        return {"ok": True, "stage": "ensure", "data": {"status": "started"}}

    monkeypatch.setattr(target_manager, "stop_server", fake_stop)
    monkeypatch.setattr(target_manager, "ensure_server_running", fake_ensure)

    result = target_manager.restart_server("unit-target")  # default repair=False

    assert forwarded == [
        ("stop", "unit-target", False),
        ("ensure", "unit-target", False),
    ]
    assert result["ok"] is True


def test_restart_server_repair_true_forwards_to_both(monkeypatch):
    """restart_server(repair=True) forwards repair=True to both
    stop_server and ensure_server_running."""
    from agent import target_manager

    forwarded = []

    def fake_stop(name, *, repair=False):
        forwarded.append(("stop", name, repair))
        return {"ok": True, "stage": "stop", "data": {"port_killed": True}}

    def fake_ensure(name, *, repair=False):
        forwarded.append(("ensure", name, repair))
        return {"ok": True, "stage": "ensure", "data": {"status": "started"}}

    monkeypatch.setattr(target_manager, "stop_server", fake_stop)
    monkeypatch.setattr(target_manager, "ensure_server_running", fake_ensure)

    result = target_manager.restart_server("unit-target", repair=True)

    assert forwarded == [
        ("stop", "unit-target", True),
        ("ensure", "unit-target", True),
    ]
    assert result["ok"] is True


def test_restart_server_short_circuits_when_stop_fails(monkeypatch):
    """restart_server must short-circuit on stop failure and NOT call
    ensure_server_running."""
    from agent import target_manager

    forwarded = []

    monkeypatch.setattr(
        target_manager, "stop_server",
        lambda name, *, repair=False: forwarded.append(("stop", name, repair)) or {
            "ok": False, "code": "target_payload_incomplete",
        },
    )
    monkeypatch.setattr(
        target_manager, "ensure_server_running",
        lambda name, *, repair=False: forwarded.append(("ensure", name, repair)) or {
            "ok": True,
        },
    )

    result = target_manager.restart_server("unit-target", repair=True)

    assert forwarded == [("stop", "unit-target", True)]  # ensure NOT called
    assert result["code"] == "target_payload_incomplete"


def test_restart_server_continues_after_successful_repair_cascade(monkeypatch):
    """A repaired stop returns ok=True, allowing restart to ensure again."""
    from agent import target_manager

    forwarded = []

    monkeypatch.setattr(
        target_manager,
        "stop_server",
        lambda name, *, repair=False: forwarded.append(("stop", name, repair)) or {
            "ok": True,
            "recoverable": True,
            "stage": "stop",
            "data": {},
        },
    )
    monkeypatch.setattr(
        target_manager,
        "ensure_server_running",
        lambda name, *, repair=False: forwarded.append(("ensure", name, repair)) or {
            "ok": True,
            "stage": "ensure",
        },
    )

    result = target_manager.restart_server("unit-target", repair=True)

    assert result["ok"] is True
    assert forwarded == [
        ("stop", "unit-target", True),
        ("ensure", "unit-target", True),
    ]
