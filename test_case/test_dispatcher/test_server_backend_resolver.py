# -*- coding: utf-8 -*-
"""The server must register a backend resolver for the unified dispatcher.

`dispatch()` reaches the backend through a registered factory rather than
importing the server module. Without that registration every `execute_action`
call fails with `dispatch_target_missing` — and since replay dispatches only
through `execute_action`, a live replay cannot execute a single step. Every
offline test supplies its own dispatch callable, so nothing else catches it.
"""

import importlib

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture
def server_module():
    """Import the server the way the target does, leaving the runtime wired."""
    import target.server as server

    return importlib.reload(server)


def test_importing_the_server_registers_a_backend_resolver(server_module):
    from target.action_dispatcher.runtime import get_backend

    # Must not raise BackendNotConfiguredError. On a host with no automation
    # backend the resolved value is None, which is still a wired resolver.
    assert get_backend() is server_module._backend


def test_the_resolver_follows_the_server_backend_rather_than_a_snapshot(
    server_module, monkeypatch,
):
    """A lambda over the module global keeps working if the backend is rebound."""
    from target.action_dispatcher.runtime import get_backend

    sentinel = object()
    monkeypatch.setattr(server_module, "_backend", sentinel)

    assert get_backend() is sentinel


def test_dispatch_reaches_the_backend_instead_of_reporting_it_missing(
    server_module, monkeypatch,
):
    from target.action_dispatcher import dispatch

    class _Backend:
        def __init__(self):
            self.clicked = None

        def click(self, **kwargs):
            self.clicked = kwargs
            return {"ok": True}

    backend = _Backend()
    monkeypatch.setattr(server_module, "_backend", backend)

    receipt = dispatch(
        action_id="gui.click",
        action_code="A020",
        args={"automation_id": "logCenterBtn"},
        target_ref={
            "snapshot_id": "OBS-1", "target_id": "T0001",
            "expected_process_name": "EDRClient.exe",
        },
        request_id="REQ-resolver-1",
    )

    assert receipt.code != "dispatch_target_missing"
