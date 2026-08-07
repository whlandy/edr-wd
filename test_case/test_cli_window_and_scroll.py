"""P1.1 CLI entry-point contracts: window / scroll / page-next subcommands."""

import json

import pytest

from agent import cli


class _FakeAgent:
    """Deterministic in-process stand-in for the MCP target agent."""

    def __init__(self):
        self.calls = []
        self.responses = {}

    def ensure_ready(self):
        return {"ok": True}

    def tools_list(self):
        return {"ok": True, "tools": []}

    def call_tool(self, name, arguments=None, timeout=None):
        self.calls.append((name, dict(arguments or {}), timeout))
        response = self.responses.get(name)
        if callable(response):
            return response(name, arguments, timeout)
        if response is not None:
            return response
        return {"ok": True}


@pytest.fixture
def fake_agent(monkeypatch, tmp_path):
    agent = _FakeAgent()
    monkeypatch.setattr(cli, "_target_agent", lambda *_args: agent)

    # Keep evidence run dirs out of the repo and record writes.
    def _create_run_dir(target, *, root=None):
        d = tmp_path / "runs" / target
        d.mkdir(parents=True, exist_ok=True)
        return d

    written = []

    def _atomic_write(path, data):
        written.append((path, data))

    monkeypatch.setattr(cli, "create_run_dir", _create_run_dir)
    monkeypatch.setattr(cli, "_atomic_write", _atomic_write)
    return agent, written


def test_window_list_unpacks_windows(monkeypatch, capsys):
    agent = _FakeAgent()
    monkeypatch.setattr(cli, "_target_agent", lambda *_args: agent)
    agent.responses["list_windows"] = {
        "ok": True,
        "windows": [
            {"title": "日志中心", "process_id": 1234, "visible": True},
            {"title": "Other", "process_id": 99, "visible": True},
        ],
    }
    exit_code = cli.main(["--target", "win-dev", "window", "list"])
    out = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert out["ok"] is True
    assert out["count"] == 2
    assert out["windows"][0]["title"] == "日志中心"
    assert [c[0] for c in agent.calls] == ["list_windows"]


def test_window_inspect_passes_title_and_reports_found(monkeypatch, capsys):
    agent = _FakeAgent()
    monkeypatch.setattr(cli, "_target_agent", lambda *_args: agent)
    agent.responses["is_window_open"] = {
        "ok": True, "found": True, "windows": [{"title": "日志中心"}],
    }
    exit_code = cli.main(["--target", "win-dev", "window", "inspect", "--title", "^日志中心$"])
    out = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert out["found"] is True
    name, arguments, _ = agent.calls[0]
    assert name == "is_window_open"
    assert arguments == {"title_re": "^日志中心$"}


def test_window_inspect_requires_filter(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_target_agent", lambda *_args: _FakeAgent())
    with pytest.raises(SystemExit):
        cli.main(["--target", "win-dev", "window", "inspect"])
    err = capsys.readouterr().err
    assert "at least one of" in err


def test_scroll_down_loops_and_stops_on_no_movement(monkeypatch, capsys):
    agent = _FakeAgent()
    monkeypatch.setattr(cli, "_target_agent", lambda *_args: agent)

    def _scroll_response(name, arguments=None, timeout=None):
        assert name == "scroll_region"
        assert arguments["window_title_re"] == "^日志中心$"
        if len(agent.calls) == 1:
            return {"ok": True, "dispatched": True, "moved": True, "reason": "wheel_moved", "code": "WHEEL_MOVED"}
        # second bounded step finds nothing to move -> stop early
        return {"ok": True, "dispatched": False, "moved": False, "reason": "not_dispatched", "code": "NOT_DISPATCHED"}

    agent.responses["scroll_region"] = _scroll_response
    exit_code = cli.main([
        "--target", "win-dev", "scroll",
        "--window-title", "^日志中心$", "--down", "2",
    ])
    out = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert out["ok"] is True
    assert out["executed_steps"] == 2
    assert out["iterations"][0]["moved"] is True
    assert out["iterations"][1]["moved"] is False


def test_scroll_up_reports_unsupported_honestly(monkeypatch, capsys):
    agent = _FakeAgent()
    monkeypatch.setattr(cli, "_target_agent", lambda *_args: agent)
    exit_code = cli.main([
        "--target", "win-dev", "scroll",
        "--window-title", "^日志中心$", "--up", "3",
    ])
    out = json.loads(capsys.readouterr().out)
    assert exit_code != 0
    assert out["ok"] is False
    assert "not supported" in out["error"]
    assert agent.calls == []  # no scroll_region call was made


def test_scroll_verify_persists_before_after_evidence(fake_agent, capsys):
    agent, written = fake_agent

    def _scroll_response(name, arguments=None, timeout=None):
        return {"ok": True, "dispatched": True, "moved": True, "reason": "wheel_moved", "code": "WHEEL_MOVED"}

    agent.responses["scroll_region"] = _scroll_response
    agent.responses["screenshot"] = {"ok": False, "error": "no live backend"}

    exit_code = cli.main([
        "--target", "win-dev", "scroll",
        "--window-title", "^日志中心$", "--down", "1", "--verify",
    ])
    out = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    evidence = out["evidence"]
    assert evidence["tool"] == "scroll_region"
    assert evidence["before"]["ok"] is False
    assert evidence["after"]["ok"] is False
    # gitignore-friendly run dir was used and evidence was written
    assert any(path.name == "evidence.json" for path, _ in written)
    assert any(path.name == "evidence.md" for path, _ in written)
    # screenshot captured before the action and after it
    tools_called = [c[0] for c in agent.calls]
    assert tools_called.count("screenshot") == 2
    # decision context records ownership/coordinate verdicts (P1.3); only
    # the selection keys actually supplied are recorded (here just a title)
    decision = evidence["decision"]
    assert decision["coordinate_space"] == "window"
    assert decision["owner"] == {}
    assert "WHEEL_MOVED" in decision["verdict_codes"]


def test_scroll_verify_records_occlusion_decision(fake_agent, capsys):
    """A window-scoped scroll that fails ownership/occlusion must record the
    stable verdict code in the evidence decision block (P1.3)."""
    agent, written = fake_agent

    def _scroll_response(name, arguments=None, timeout=None):
        return {
            "ok": True,
            "dispatched": False,
            "moved": False,
            "reason": "occluded",
            "code": "target_occluded",
        }

    agent.responses["scroll_region"] = _scroll_response
    agent.responses["screenshot"] = {"ok": False, "error": "no live backend"}

    exit_code = cli.main([
        "--target", "win-dev", "scroll",
        "--window-title", "^日志中心$", "--process-name", "EDRClient.exe",
        "--pid", "6752", "--down", "1", "--verify",
    ])
    out = json.loads(capsys.readouterr().out)
    evidence = out["evidence"]
    decision = evidence["decision"]
    assert decision["coordinate_space"] == "window"
    assert decision["owner"] == {"process_name": "EDRClient.exe", "pid": 6752}
    assert decision["verdict_codes"] == ["target_occluded"]
    # the decision block is rendered into the markdown evidence file
    md = next(data.decode("utf-8") for path, data in written if path.name == "evidence.md")
    assert "## Decision (ownership/coordinate)" in md
    assert "target_occluded" in md


def test_page_next_verify_calls_page_table_and_persists(fake_agent, capsys):
    agent, written = fake_agent

    def _page_next_response(name, arguments=None, timeout=None):
        assert name == "page_table"
        assert arguments["window_title_re"] == "^日志中心$"
        assert arguments["direction"] == "next"
        assert arguments["verify"] is True
        return {"ok": True, "dispatched": True, "moved": True, "reason": "next_page", "code": "NEXT_PAGE"}

    agent.responses["page_table"] = _page_next_response
    agent.responses["screenshot"] = {"ok": False, "error": "no live backend"}

    exit_code = cli.main([
        "--target", "win-dev", "page-next",
        "--window-title", "^日志中心$", "--verify",
    ])
    out = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert out["moved"] is True
    assert out["evidence"]["tool"] == "page_table"
    assert out["evidence"]["decision"]["coordinate_space"] == "window"
    assert "NEXT_PAGE" in out["evidence"]["decision"]["verdict_codes"]
    assert any(path.name == "evidence.json" for path, _ in written)
