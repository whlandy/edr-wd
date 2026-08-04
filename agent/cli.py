"""Cross-platform command line entry point for EDR-WD agent operations."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from agent import target_manager
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
    return parser


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

    parser.error(f"unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
