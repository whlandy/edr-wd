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


def test_mcp_facade_shim_forwards_effect_surface():
    """The shim forwards the S4 effect surface (begin_effect_baseline /
    observe_effect_after_action / bind_increment) to the MCP tools —
    key-by-key, with run_token injected where the adapter's controlled
    chain relies on the MCP carrier to supply it.  The MCP carrier must
    not fabricate baseline tokens locally: they live only in the target
    process's facade memory and round-trip through tool responses."""
    responses = {
        "begin_effect_baseline": json.dumps(
            {"baseline_token": "EB-1", "candidate_count": 0}),
        "observe_effect_after_action": json.dumps(
            {"ok": True, "outcome": "appeared", "native_id": "200"}),
        "bind_increment": json.dumps(
            {"binding_token": "WB-2", "revision": 3, "state": "BOUND"}),
    }
    transport = _RecordingTransport(responses)
    tools = MCPServerTools(transport)
    tools.run_token = "tok-9"
    facade = tools._binding_facade_holder["facade"]

    doc = facade.begin_effect_baseline(
        "W2", {"processName": "EDRClient.exe", "titleRegex": "^Log Center$"},
        binding_token="WB-2")
    assert doc == {"baseline_token": "EB-1", "candidate_count": 0}
    name, params = transport.calls[-1]
    assert name == "begin_effect_baseline"
    assert params == {
        "window_id": "W2",
        "identity": {"processName": "EDRClient.exe",
                     "titleRegex": "^Log Center$"},
        "binding_token": "WB-2",
        "run_token": "tok-9",
    }

    doc = facade.observe_effect_after_action(
        "W2", "EB-1", effect={"kind": "appeared"}, binding_token="WB-2")
    assert doc == {"ok": True, "outcome": "appeared", "native_id": "200"}
    name, params = transport.calls[-1]
    assert name == "observe_effect_after_action"
    assert params == {"window_id": "W2", "baseline_token": "EB-1",
                      "effect": {"kind": "appeared"},
                      "binding_token": "WB-2", "run_token": "tok-9"}

    doc = facade.bind_increment(
        "W2", {"processName": "EDRClient.exe"},
        native_id="200", run_token="tok-9")
    assert doc == {"binding_token": "WB-2", "revision": 3, "state": "BOUND"}
    name, params = transport.calls[-1]
    assert name == "bind_increment"
    assert params == {"window_id": "W2",
                      "identity": {"processName": "EDRClient.exe"},
                      "native_id": "200", "run_token": "tok-9"}


def test_mcp_facade_shim_effect_failures_refuse_like_step_context():
    """An MCP refusal envelope (ok=False + code) must raise — the adapter's
    fail-closed handling catches exceptions, not ok=False JSON, so the
    shim keeps the same contract as step_context.  Business outcomes of
    the effect diff (timeout/ambiguous: ok=False + outcome, no code) pass
    through untouched — relabeling them would corrupt the evidence."""
    responses = {
        "begin_effect_baseline": json.dumps(
            {"ok": False, "code": "window_binding_lease_refused"}),
        "observe_effect_after_action": json.dumps(
            {"ok": False, "code": "window_effect_baseline_unknown"}),
        "bind_increment": json.dumps(
            {"ok": False, "code": "window_binding_shared"}),
    }
    tools = MCPServerTools(_RecordingTransport(responses))
    tools.run_token = "tok-9"
    facade = tools._binding_facade_holder["facade"]
    with pytest.raises(ValueError):
        facade.begin_effect_baseline("W2", {"titleRegex": "^x$"})
    with pytest.raises(ValueError):
        facade.observe_effect_after_action(
            "W2", "EB-x", effect={"kind": "appeared"})
    with pytest.raises(ValueError):
        facade.bind_increment("W2", {"titleRegex": "^x$"}, native_id="9")

    responses["observe_effect_after_action"] = json.dumps(
        {"ok": False, "outcome": "timeout", "appeared": []})
    doc = facade.observe_effect_after_action(
        "W2", "EB-1", effect={"kind": "appeared"})
    assert doc == {"ok": False, "outcome": "timeout", "appeared": []}


def test_in_process_carrier_forwards_module():
    """InProcessServerTools forwards attribute access to the server module —
    the same object the in-process tests pass today."""
    import target.server as server
    from agent.recording.v3_transport import InProcessServerTools
    tools = InProcessServerTools(server)
    assert tools.status is server.status
