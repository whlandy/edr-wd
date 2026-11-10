"""v3 transport: role-map derivation and MCP carrier parameter plumbing.

Covers the pieces `tmp/v3_replay_driver*.py` used to hand-roll per copy:
`derive_role_map` (W→role authorization from golden identities + installed
roles) and `MCPServerTools` (positional mapping, None passthrough, run_token
capture, facade shim revision read).
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from agent.recording.v3_transport import (  # noqa: E402
    MCPServerTools,
    RoleDerivationError,
    derive_role_map,
)
from target.product_profiles import get_capability  # noqa: E402


def _golden_with(windows):
    return {"windowSequence": {"windows": windows,
                               "steps": [], "cleanup": []}}


def _win(wid, process="EDRClient.exe", title=None, root=None):
    identity = {"processName": process}
    if title:
        identity["titleRegex"] = title
    if root:
        identity["rootAutomationId"] = root
    return {"id": wid, "identity": identity, "origin": "already_open"}


@pytest.fixture
def hisec_capability():
    return get_capability("windows_hisec", "windows_pywinauto")


def test_demo_golden_derives_log_center_and_main(hisec_capability):
    """The demo golden's six windows derive exactly the map the r3 driver
    hand-wrote: W6 (Log Center) → log_center, everything else → main."""
    golden = json.loads(
        (REPO / "result-report/trace-self-correction/20261110-close-race-final/"
         "demo-golden.v3.json").read_text())
    assert derive_role_map(golden, hisec_capability) == {
        "W1": "main", "W2": "main", "W3": "main", "W4": "main",
        "W5": "main", "W6": "log_center",
    }


def test_root_automation_id_is_exact_evidence():
    """A declared rootAutomationId inside the role's root_control_ids wins
    outright — even when the title would not fullmatch. Uses the
    qa_recording_fixture profile, whose installed spec declares roots."""
    from target.product_profiles import BUILTIN_CAPABILITIES
    cap = BUILTIN_CAPABILITIES[("qa_recording_fixture", "windows_pywinauto")]
    golden = _golden_with([_win("W9", root="RecordingFixture",
                                title="^totally-different$")])
    assert derive_role_map(golden, cap) == {"W9": "main"}


def test_ambiguous_title_refuses(hisec_capability):
    """A window whose process is EDRClient and title is logo1 fullmatches
    the main pattern — but a *second* process-matching role with the same
    title core would be ambiguous. Prove the ambiguity refusal with a
    synthetic capability: title matching alone (no root evidence) cannot
    decide between two roles."""
    from target.product_profiles import ProductCapability, ProductWindowRole

    class _Cap:
        window_roles = (
            ProductWindowRole(role="alpha", process_names=("EDRClient.exe",),
                              title_pattern="^(logo1)$"),
            ProductWindowRole(role="beta", process_names=("EDRClient.exe",),
                              title_pattern="^(logo1)$"),
        )

    golden = _golden_with([_win("W1", title="^logo1$")])
    with pytest.raises(RoleDerivationError, match="several roles"):
        derive_role_map(golden, _Cap())


def test_unmatched_identity_refuses(hisec_capability):
    """An identity no role matches refuses — the adapter validates every W
    against its role, so an unmapped W would fail later only after the
    begin/register side effects."""
    golden = _golden_with([_win("W7", process="Notepad.exe",
                                title="^Untitled$")])
    with pytest.raises(RoleDerivationError, match="no installed product role"):
        derive_role_map(golden, hisec_capability)


def test_empty_window_sequence_refuses():
    with pytest.raises(RoleDerivationError, match="no windowSequence"):
        derive_role_map({"windowSequence": {"windows": []}}, None)
    with pytest.raises(RoleDerivationError, match="no windowSequence"):
        derive_role_map({}, None)


class _RecordingTransport:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, name, arguments):
        self.calls.append((name, arguments))
        return self.responses.get(name, json.dumps({"ok": True}))


def test_mcp_carrier_maps_positionals_and_filters_none():
    """Positional args map by the declared order; None values are dropped
    except the whitelist the adapter relies on (control_selector/observe/
    action_id/action_args/request_id/target_ref)."""
    responses = {"begin_product_run": json.dumps(
        {"ok": True, "run_token": "tok-1", "run_id": "r1"})}
    transport = _RecordingTransport(responses)
    tools = MCPServerTools(transport)
    raw = tools.begin_product_run("windows_hisec", "digest", 300)
    assert json.loads(raw)["run_token"] == "tok-1"
    assert tools.run_token == "tok-1"
    name, params = transport.calls[-1]
    assert name == "begin_product_run"
    assert params == {"profile_id": "windows_hisec", "digest": "digest",
                      "lease_seconds": 300}

    tools.focus_bound_window("W1", "WB-x", tools.run_token,
                             action_id=None, observe=True,
                             target_ref=None)
    name, params = transport.calls[-1]
    assert name == "focus_bound_window"
    assert params["action_id"] is None and params["observe"] is True
    assert "target_ref" in params and params["target_ref"] is None
    assert "request_id" not in params  # not passed, not whitelisted


def test_mcp_facade_shim_reads_revision():
    """The binding-facade shim resolves the authoritative revision via
    reverify and refuses when the binding is gone."""
    responses = {
        "reverify_bound_window": json.dumps(
            {"binding_token": "WB-1", "revision": 7}),
    }
    tools = MCPServerTools(_RecordingTransport(responses))
    tools.run_token = "tok-9"
    doc = tools._binding_facade_holder["facade"].step_context(
        run_id="r", step_id="s", attempt_id="a", window_id="W1",
        binding_token="WB-1")
    assert doc == {"bindingRevision": 7}

    responses["reverify_bound_window"] = json.dumps(
        {"ok": False, "code": "window_binding_not_found"})
    with pytest.raises(ValueError, match="reverify refused"):
        tools._binding_facade_holder["facade"].step_context(
            run_id="r", step_id="s", attempt_id="a", window_id="W1",
            binding_token="WB-1")


def test_in_process_carrier_forwards_module():
    """InProcessServerTools forwards attribute access to the server module —
    the same object the in-process tests pass today."""
    import target.server as server
    from agent.recording.v3_transport import InProcessServerTools
    tools = InProcessServerTools(server)
    assert tools.status is server.status
