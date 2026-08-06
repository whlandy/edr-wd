"""CLI contracts that replace ad-hoc MCP helper scripts."""

import json

from agent import cli


class _FakeAgent:
    def __init__(self):
        self.calls = []

    def ensure_ready(self):
        return {"ok": True}

    def tools_list(self):
        return {"ok": True, "tools": [{"name": "activate_edr"}]}

    def call_tool(self, name, arguments=None, timeout=None):
        self.calls.append((name, arguments or {}, timeout))
        if name == "activate_edr":
            return {"ok": True, "already_open": False}
        if name == "is_window_open":
            return {"ok": True, "found": True, "windows": [{"title": "华为HiSec Endpoint"}]}
        if name == "connect":
            return {"ok": True}
        if name == "screenshot":
            return {"ok": True, "image_b64": "png-data"}
        return {"ok": True}


def test_open_edr_uses_exact_main_window_and_persists_screenshot(monkeypatch, capsys):
    agent = _FakeAgent()
    monkeypatch.setattr(cli, "_target_agent", lambda *_args: agent)
    monkeypatch.setattr(
        cli,
        "_save_screenshot_result",
        lambda _result, _target: {"run_dir": "/tmp/report", "screenshot": {"path": "main.png"}},
    )

    exit_code = cli.main(["--target", "win-dev", "open-edr"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output["ok"] is True
    connect = next(call for call in agent.calls if call[0] == "connect")
    assert connect[1]["process_name"] == "EDRClient.exe"
    assert connect[1]["title_re"] == "^华为HiSec Endpoint$"
    assert output["agent_artifact"]["screenshot"]["path"] == "main.png"


def test_tools_command_returns_normalized_tool_array(monkeypatch, capsys):
    agent = _FakeAgent()
    monkeypatch.setattr(cli, "_target_agent", lambda *_args: agent)

    exit_code = cli.main(["--target", "win-dev", "tools"])
    output = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert output == {"ok": True, "tools": [{"name": "activate_edr"}]}
