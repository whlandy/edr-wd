"""Opt-in live acceptance for target-local desktop recording lifecycle.

Set ``EDR_WD_RECORDING_E2E=1`` only after the current target payload has been
deployed.  The test opens/foregrounds HiSec, shows the recorder indicator, and
installs OS input hooks, so it must never run as part of the default suite.
"""

from __future__ import annotations

import base64
import hashlib
import os

import pytest

from test_case.conftest import is_server_online, live_mcp_client_or_skip


pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.environ.get("EDR_WD_RECORDING_E2E") != "1",
        reason="set EDR_WD_RECORDING_E2E=1 for live recorder acceptance",
    ),
    pytest.mark.skipif(not is_server_online(), reason="MCP server not reachable"),
]


@pytest.fixture(scope="module")
def client():
    return live_mcp_client_or_skip()


def test_live_recording_lifecycle_and_permissions(client):
    tool_result = client.tools_list()
    tools = {
        item["name"] for item in tool_result.get("result", {}).get("tools", [])
    }
    required = {
        "activate_edr", "connect", "lock_window",
        "start_recording", "recording_status", "pause_recording",
        "resume_recording", "stop_recording", "get_recording_capture",
    }
    assert not (required - tools), f"missing recording tools: {sorted(required - tools)}"

    status = client.call_tool("status", {})
    backend = status.get("backend_kind") or status.get("backend")
    if backend == "windows_pywinauto":
        process_name = os.environ.get("EDR_WD_RECORDING_PROCESS", "EDRClient.exe")
    elif backend == "macos_accessibility":
        process_name = os.environ.get("EDR_WD_RECORDING_PROCESS", "EDRClient")
    else:
        pytest.skip(f"unsupported live recording backend: {backend!r}")
    title_re = os.environ.get("EDR_WD_RECORDING_TITLE_RE", "^华为HiSec Endpoint$")

    activated = client.call_tool("activate_edr", {"wait": True, "timeout": 20.0})
    assert activated.get("ok") is True, activated
    connected = client.call_tool("connect", {
        "process_name": process_name, "title_re": title_re, "timeout": 15.0,
    })
    assert connected.get("ok") is True, connected
    locked = client.call_tool("lock_window", {
        "process_name": process_name, "title_re": title_re,
        "strict": True, "activate": True,
    })
    assert locked.get("ok") is True, locked

    started = client.call_tool("start_recording", {
        "name": "live-recorder-lifecycle",
        "process_name": process_name,
        "window_title": title_re,
        "lease_seconds": 30.0,
    })
    assert started.get("ok") is True, started
    assert started.get("evidenceInitialization", {}).get("ok") is True, started
    initial_capture = started["evidenceInitialization"]["capture"]
    fetched_initial = client.call_tool(
        "get_recording_capture", {"capture_id": initial_capture["id"]},
    )
    assert fetched_initial.get("ok") is True, fetched_initial
    initial_png = base64.b64decode(
        fetched_initial["image_b64"], validate=True,
    )
    assert initial_png.startswith(b"\x89PNG\r\n\x1a\n")
    initial_digest = "sha256:" + hashlib.sha256(initial_png).hexdigest()
    assert initial_digest == initial_capture["sha256"]
    assert initial_digest == fetched_initial["sha256"]
    try:
        current = client.call_tool("recording_status", {"heartbeat": True})
        assert current.get("state") == "recording", current
        paused = client.call_tool("pause_recording", {})
        assert paused.get("state") == "paused", paused
        resumed = client.call_tool("resume_recording", {})
        assert resumed.get("state") == "recording", resumed
    finally:
        stopped = client.call_tool("stop_recording", {})

    assert stopped.get("ok") is True, stopped
    assert stopped.get("state") == "stopped"
    assert stopped["recording"]["schema"] == "edr.desktop-recording/v1"
    assert stopped["recording"]["captureDiagnostics"] == {
        "droppedPackets": 0,
        "correlationErrorCount": 0,
    }
