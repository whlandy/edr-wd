"""CLI replay error contract: recording-model errors must surface as clean
JSON (ok=false + code), never as a raw traceback (regression: the
``except RecordingModelError`` handler in the replay subcommand referenced a
name that was never imported, so ``build_replay_runtime`` validation errors —
e.g. a visual-only replay whose click carries no template — crashed the CLI
with ``NameError`` and exit code 1 instead of the JSON error contract."""

import json

from agent import cli
from target.action_catalog import CATALOG_VERSION, catalog_digest


class _FakeAgent:
    """Deterministic in-process stand-in for the MCP target agent."""

    def __init__(self):
        self.calls = []

    def ensure_ready(self):
        return {"ok": True}

    def tools_list(self):
        return {"ok": True, "tools": []}

    def call_tool(self, name, arguments=None, timeout=None):
        self.calls.append((name, dict(arguments or {}), timeout))
        return {"ok": True}


def _visual_only_golden_without_template(tmp_path):
    """A ready golden whose gui.click has no visual template."""
    directory = tmp_path / "golden"
    directory.mkdir(parents=True)
    path = directory / "golden-trace.json"
    golden = {
        "schema": "edr.desktop-golden-trace/v1",
        "status": "ready",
        "name": "cli-replay-error-contract",
        "sourceRecording": {
            "schema": "edr.desktop-recording/v1",
            "sha256": "sha256:" + "0" * 64,
        },
        "catalog": {"version": CATALOG_VERSION, "digest": catalog_digest()},
        "environment": {
            "application": "EDRClient.exe",
            "backend": "windows_pywinauto",
            "target": "target",
        },
        "entry": "step-0001",
        "steps": {
            "step-0001": {
                "stepId": "step-0001",
                "actionId": "gui.click",
                "args": {},
                "selector": {
                    "control": {
                        "automationId": "EdrMainWindow.baseWidget.logCenterBtn",
                        "controlType": "Button",
                        "fingerprint": "sha256:" + "1" * 64,
                        "name": "Log Center",
                    },
                    "visual": None,
                    "window": {
                        "processName": "EDRClient.exe",
                        "rootAutomationId": "EdrMainWindow",
                    },
                },
                "verifiers": [],
                "required": True,
                "status": "ready",
                "issues": [],
                "endSelector": None,
                "next": None,
            }
        },
        "cleanup": [],
    }
    path.write_text(json.dumps(golden), encoding="utf-8")
    return path


def test_visual_only_missing_template_reports_clean_json(
    monkeypatch, capsys, tmp_path
):
    agent = _FakeAgent()
    monkeypatch.setattr(cli, "_target_agent", lambda *_args: agent)
    golden = _visual_only_golden_without_template(tmp_path)

    exit_code = cli.main([
        "--target", "win-dev",
        "replay", str(golden),
        "--replay-mode", "visual_only",
        "--trace-root", str(tmp_path / "replay-traces"),
    ])

    out = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert out["ok"] is False
    assert out["code"] == "visual_template_missing"
    # No remote action may have been dispatched for a local validation error;
    # only the pre-flight recording_status probe is allowed.
    assert [name for name, _, _ in agent.calls] == ["recording_status"]
