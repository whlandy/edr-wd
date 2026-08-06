"""Cross-platform command line entry point for EDR-WD agent operations."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from agent import target_manager
from agent.e2e_report import create_run_dir, persist_target_screenshot
from agent.subagent.target_agent import TargetSubAgent
from agent.target_config import TargetConfig, add_config_arguments, run_config_command


def _print_result(result: object) -> int:
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0 if not isinstance(result, dict) or result.get("ok", True) else 1


def _target(args: argparse.Namespace) -> str | None:
    return args.target or TargetConfig(args.config).get_default_target()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="edr-wd",
        description="Configure, operate, and test EDR-WD targets.",
    )
    parser.add_argument("--config", help="Target config path (defaults to EDR_WD_CONFIG or config/targets.local.json)")
    parser.add_argument("--target", help="Target name (defaults to default_target)")
    sub = parser.add_subparsers(dest="command", required=True)

    config = sub.add_parser("config", help="Create, inspect, validate, or edit target config")
    add_config_arguments(config, include_config_path=False)

    for name, help_text in (
        ("status", "Probe target and lifecycle status"),
        ("up", "Ensure the target MCP server is running"),
        ("down", "Stop the target MCP server"),
        ("restart", "Restart the target MCP server"),
        ("repair", "Deploy, install, and ensure the target MCP server"),
    ):
        sub.add_parser(name, help=help_text)

    test = sub.add_parser("test", help="Run the profile-aware live target test runner")
    test.add_argument("--profile", help="Override the configured application profile")
    test.add_argument("-v", "--verbose", action="store_true")

    sub.add_parser("tools", help="List MCP tools exposed by the selected target")

    call = sub.add_parser("call", help="Call one MCP tool without writing a helper script")
    call.add_argument("tool", help="MCP tool name, for example list_windows")
    call.add_argument("--args", default="{}", help="Tool arguments as a JSON object")
    call.add_argument("--timeout", type=float)
    call.add_argument("--save-screenshot", action="store_true", help="Persist a screenshot result on the agent host")

    open_edr = sub.add_parser("open-edr", help="Open EDRClient, verify it, and save a local screenshot")
    open_edr.add_argument("--timeout", type=float, default=20.0)
    return parser


def _target_agent(target: str, config_path: str | None) -> TargetSubAgent:
    return TargetSubAgent(target, config=TargetConfig(config_path))


def _parse_json_object(raw: str, parser: argparse.ArgumentParser) -> dict:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        parser.error(f"--args must be valid JSON: {exc}")
    if not isinstance(value, dict):
        parser.error("--args must decode to a JSON object")
    return value


def _save_screenshot_result(result: dict, target: str) -> dict:
    run_dir = create_run_dir(target)
    metadata = persist_target_screenshot(result, run_dir, "manual-screenshot")
    return {"run_dir": str(run_dir), "screenshot": metadata}


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "config":
        return run_config_command(args)

    target = _target(args)
    if not target:
        parser.error("no --target supplied and default_target is not configured")

    if args.command == "status":
        return _print_result(target_manager.probe_target(target))
    if args.command == "up":
        return _print_result(target_manager.ensure_server_running(target))
    if args.command == "down":
        return _print_result(target_manager.stop_server(target))
    if args.command == "restart":
        return _print_result(target_manager.restart_server(target))
    if args.command == "repair":
        return _print_result(target_manager.repair_target(target, repair=True))
    if args.command == "test":
        runner = Path(__file__).resolve().parents[1] / "test_case" / "run_tests.py"
        cmd = [sys.executable, str(runner), "--target", target]
        if args.profile:
            cmd.extend(["--profile", args.profile])
        if args.verbose:
            cmd.append("-v")
        return subprocess.run(cmd, check=False).returncode
    if args.command == "tools":
        agent = _target_agent(target, args.config)
        ready = agent.ensure_ready()
        if not ready.get("ok"):
            return _print_result(ready)
        return _print_result(agent.tools_list())
    if args.command == "call":
        arguments = _parse_json_object(args.args, parser)
        agent = _target_agent(target, args.config)
        ready = agent.ensure_ready()
        if not ready.get("ok"):
            return _print_result(ready)
        result = agent.call_tool(args.tool, arguments, timeout=args.timeout)
        if (args.save_screenshot or args.tool == "screenshot") and result.get("ok"):
            try:
                result = dict(result)
                result["agent_artifact"] = _save_screenshot_result(result, target)
                for key in ("image", "image_b64", "image_base64"):
                    result.pop(key, None)
            except ValueError as exc:
                result = {"ok": False, "error": str(exc), "tool_result": result}
        return _print_result(result)
    if args.command == "open-edr":
        agent = _target_agent(target, args.config)
        ready = agent.ensure_ready()
        if not ready.get("ok"):
            return _print_result(ready)
        activated = agent.call_tool(
            "activate_edr", {"wait": True, "timeout": args.timeout}, timeout=args.timeout + 10,
        )
        if not activated.get("ok"):
            return _print_result(activated)
        edr_window = {
            "process_name": "EDRClient.exe",
            "title_re": "^华为HiSec Endpoint$",
        }
        visible = agent.call_tool("is_window_open", edr_window)
        if not visible.get("ok") or not visible.get("found"):
            return _print_result({
                "ok": False,
                "error": "activate_edr returned successfully but EDRClient.exe is not visible",
                "activate_edr": activated,
                "verification": visible,
            })
        connected = agent.call_tool(
            "connect", {**edr_window, "timeout": 10.0}
        )
        if not connected.get("ok"):
            return _print_result(connected)
        screenshot = agent.call_tool("screenshot", {}, timeout=60.0)
        artifact = _save_screenshot_result(screenshot, target) if screenshot.get("ok") else None
        return _print_result({
            "ok": bool(screenshot.get("ok")),
            "target": target,
            "activated": activated,
            "verification": visible,
            "agent_artifact": artifact,
            "screenshot_error": screenshot.get("error") if not screenshot.get("ok") else None,
        })

    parser.error(f"unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
