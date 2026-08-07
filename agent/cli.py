"""Cross-platform command line entry point for EDR-WD agent operations."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from agent import target_manager
from agent.e2e_report import create_run_dir, persist_target_screenshot
from agent.subagent.target_agent import TargetSubAgent
from agent.target_config import TargetConfig, add_config_arguments, run_config_command


def _print_result(result: object) -> int:
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0 if not isinstance(result, dict) or result.get("ok", True) else 1


def _error_summary(result: dict) -> dict:
    """A short, stable error summary for operator-facing failure messages.

    P1.1: the CLI must give concise error summaries instead of dumping a raw
    MCP envelope. We project the composite/pointer envelope fields we actually
    consume (ok / dispatched / moved / reason / code) plus any backend error.
    """
    summary = {
        "tool": result.get("tool"),
        "ok": result.get("ok", False),
    }
    for key in ("dispatched", "event_dispatched", "moved", "reason", "code", "success", "error"):
        if key in result and result.get(key) is not None:
            summary[key] = result.get(key)
    return summary


def _target(args: argparse.Namespace) -> str | None:
    return args.target or TargetConfig(args.config).get_default_target()


def _window_args(args: argparse.Namespace, *, title_attr: str) -> dict:
    """Build the window-scoped argument dict from CLI options, dropping Nones.

    Mirrors the MCP composite tools (scroll_region / page_table / etc.) which
    accept window_title_re / process_name / pid / max_depth.
    """
    out: dict = {}
    title = getattr(args, title_attr, None) or getattr(args, "window_title_re", None)
    if title:
        out["window_title_re"] = title
    if getattr(args, "process_name", None):
        out["process_name"] = args.process_name
    if getattr(args, "pid", None) is not None:
        out["pid"] = args.pid
    if getattr(args, "max_depth", None) is not None:
        out["max_depth"] = args.max_depth
    return out


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = Path(tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-")[1])
    tmp.write_bytes(data)
    tmp.replace(path)


def _render_evidence_md(evidence: dict) -> str:
    lines = [
        "# EDR-WD CLI Evidence",
        "",
        f"- Target: `{evidence.get('target')}`",
        f"- Tool: `{evidence.get('tool')}`",
        f"- Created: `{evidence.get('created_at')}`",
        "",
        "## Action",
        "",
        "```json",
        json.dumps(evidence.get("arguments", {}), ensure_ascii=False, indent=2, default=str),
        "```",
        "",
        "## Result",
        "",
        "```json",
        json.dumps(evidence.get("result", {}), ensure_ascii=False, indent=2, default=str),
        "```",
        "",
        "## Screenshots",
        "",
    ]
    for role_field in ("before", "after"):
        shot = evidence.get(role_field) or {}
        if shot.get("ok") is False:
            lines.append(f"- **{role_field}**: failed to capture — {shot.get('error')}")
        else:
            path = shot.get("path")
            lines.append(
                f"- **{role_field}**: `{shot.get('evidence_id')}` "
                f"[{path}]({path}) · `{shot.get('sha256')}`"
            )
    return "\n".join(lines) + "\n"


def _with_verify_evidence(agent, target: str, tool: str, arguments: dict, result: dict) -> dict:
    """Persist before/action/after evidence for a single MCP action into a fresh run dir.

    P1.1 acceptance: the CLI can persist before/action/after evidence when
    `--verify` is used. Screenshots are captured (when the backend returns a
    decodable PNG) and a small evidence.json / evidence.md is written into the
    same agent-local run dir `create_run_dir` produces. Failed captures are
    recorded honestly rather than fabricating a screenshot.
    """
    run_dir = create_run_dir(target)

    def _shot(name: str, role: str) -> dict:
        res = agent.call_tool("screenshot", {}, timeout=60.0)
        if res.get("ok"):
            try:
                meta = persist_target_screenshot(res, run_dir, name)
                meta["role"] = role
                return meta
            except ValueError as exc:
                return {"ok": False, "role": role, "error": str(exc)}
        return {"ok": False, "role": role, "error": res.get("error")}

    before = _shot("before", "before")
    after = _shot("after", "after")
    evidence = {
        "target": target,
        "tool": tool,
        "arguments": arguments,
        "result": {k: result.get(k) for k in (
            "ok", "dispatched", "event_dispatched", "moved", "reason", "code",
            "success", "error", "steps", "iterations",
        ) if k in result},
        "before": before,
        "after": after,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    _atomic_write(
        run_dir / "evidence.json",
        json.dumps(evidence, ensure_ascii=False, indent=2, default=str).encode("utf-8") + b"\n",
    )
    _atomic_write(
        run_dir / "evidence.md",
        _render_evidence_md(evidence).encode("utf-8"),
    )
    return evidence


# ── Window subcommand ──────────────────────────────────────────────────────────

def _cmd_window(args: argparse.Namespace, agent, parser, target: str) -> int:
    if args.action == "list":
        result = agent.call_tool("list_windows", {}, timeout=args.timeout)
        if result.get("ok") is False:
            return _print_result(_error_summary(result))
        windows = result.get("windows", [])
        summary = {
            "ok": True,
            "target": target,
            "tool": "list_windows",
            "count": len(windows),
            "windows": [
                {
                    "title": w.get("title"),
                    "process_id": w.get("process_id") or w.get("pid"),
                    "class_name": w.get("class_name"),
                    "handle": w.get("handle"),
                    "visible": w.get("visible"),
                    "rect": w.get("rect") or w.get("rectangle"),
                }
                for w in windows
            ],
        }
        return _print_result(summary)

    # inspect
    if not (args.title_re or args.process_name or args.class_name):
        parser.error(
            "window inspect requires at least one of "
            "--title / --process-name / --class-name"
        )
    query = {
        "title_re": getattr(args, "title_re", None),
        "process_name": getattr(args, "process_name", None),
        "class_name": getattr(args, "class_name", None),
    }
    query = {k: v for k, v in query.items() if v}
    result = agent.call_tool("is_window_open", query, timeout=args.timeout)
    if result.get("ok") is False:
        return _print_result(_error_summary(result))
    return _print_result({
        "ok": bool(result.get("found")),
        "target": target,
        "tool": "is_window_open",
        "found": result.get("found", False),
        "matches": result.get("windows", []),
    })


# ── scroll / page-next ─────────────────────────────────────────────────────────

def _cmd_scroll(args: argparse.Namespace, agent, parser, target: str) -> int:
    if getattr(args, "up", None):
        return _print_result({
            "ok": False,
            "target": target,
            "tool": "scroll_region",
            "error": (
                "'--up' is not supported: the window-scoped composite "
                "`scroll_region` only performs forward (down) bounded steps. "
                "For backward paging use 'page-next --direction prev'."
            ),
        })
    steps = max(1, getattr(args, "down", None) or 1)
    window_args = _window_args(args, title_attr="window_title_re")
    run_dir = create_run_dir(target) if args.verify else None

    def _shot(rd: Path | None, name: str, role: str) -> dict:
        if rd is None:
            return {"ok": False, "role": role, "error": "no run dir (verify disabled)"}
        res = agent.call_tool("screenshot", {}, timeout=60.0)
        if res.get("ok"):
            try:
                meta = persist_target_screenshot(res, rd, name)
                meta["role"] = role
                return meta
            except ValueError as exc:
                return {"ok": False, "role": role, "error": str(exc)}
        return {"ok": False, "role": role, "error": res.get("error")}

    before = _shot(run_dir, "before", "before") if args.verify else None
    iterations: list = []
    for i in range(1, steps + 1):
        result = agent.call_tool("scroll_region", window_args, timeout=args.timeout)
        iterations.append({
            "step": i,
            "tool": "scroll_region",
            "ok": result.get("ok"),
            "dispatched": result.get("dispatched"),
            "moved": result.get("moved"),
            "reason": result.get("reason"),
            "code": result.get("code"),
        })
        if result.get("ok") is False or not result.get("moved"):
            break
    after = _shot(run_dir, "after", "after") if args.verify else None

    moved_any = any(it.get("moved") for it in iterations)
    ok_all = all(it.get("ok") is not False for it in iterations)
    evidence = None
    if args.verify and run_dir is not None:
        evidence = {
            "target": target,
            "tool": "scroll_region",
            "arguments": window_args,
            "result": {
                "ok": ok_all,
                "moved": moved_any,
                "iterations": iterations,
            },
            "before": before,
            "after": after,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        _atomic_write(
            run_dir / "evidence.json",
            json.dumps(evidence, ensure_ascii=False, indent=2, default=str).encode("utf-8") + b"\n",
        )
        _atomic_write(
            run_dir / "evidence.md",
            _render_evidence_md(evidence).encode("utf-8"),
        )

    return _print_result({
        "ok": bool(moved_any),
        "target": target,
        "tool": "scroll_region",
        "requested_steps": steps,
        "executed_steps": len(iterations),
        "iterations": iterations,
        "verify": args.verify,
        "evidence": evidence,
    })


def _cmd_page_next(args: argparse.Namespace, agent, parser, target: str) -> int:
    window_args = _window_args(args, title_attr="window_title_re")
    window_args["direction"] = args.direction
    window_args["verify"] = True  # page_table itself verifies page movement
    result = agent.call_tool("page_table", window_args, timeout=args.timeout)
    evidence = None
    if args.verify:
        evidence = _with_verify_evidence(agent, target, "page_table", window_args, result)
    moved = bool(result.get("moved"))
    return _print_result({
        "ok": moved,
        "target": target,
        "tool": "page_table",
        "moved": moved,
        "reason": result.get("reason"),
        "code": result.get("code"),
        "verify": args.verify,
        "evidence": evidence,
    })


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

    window = sub.add_parser("window", help="List or inspect target windows (P1.1)")
    window.add_argument("action", choices=["list", "inspect"], help="Window action")
    window.add_argument("--title", "--title-re", dest="title_re", help="Window title regex, e.g. '^日志中心$'")
    window.add_argument("--process-name", dest="process_name", help="Owning process name")
    window.add_argument("--class-name", dest="class_name", help="Window class name filter")
    window.add_argument("--timeout", type=float, help="MCP call timeout in seconds")

    scroll = sub.add_parser(
        "scroll",
        help="Window-scoped bounded scroll: N forward (down) steps via scroll_region",
    )
    scroll.add_argument("--window-title", dest="window_title_re", help="Window title regex, e.g. '^日志中心$'")
    scroll.add_argument("--process-name", dest="process_name", help="Owning process name")
    scroll.add_argument("--pid", type=int, help="Owning process id")
    scroll.add_argument("--max-depth", type=int, default=6, help="Control-tree traversal depth")
    scroll.add_argument("--down", type=int, default=1, help="Number of bounded down-scroll steps")
    scroll.add_argument("--up", type=int, help="(not supported by scroll_region) up-scroll steps")
    scroll.add_argument("--verify", action="store_true", help="Persist before/action/after evidence")
    scroll.add_argument("--timeout", type=float, help="Per-call MCP timeout in seconds")

    page_next = sub.add_parser(
        "page-next",
        help="Turn one page of a paged table via the semantic page_table tool",
    )
    page_next.add_argument("--window-title", dest="window_title_re", help="Window title regex, e.g. '^日志中心$'")
    page_next.add_argument("--process-name", dest="process_name", help="Owning process name")
    page_next.add_argument("--pid", type=int, help="Owning process id")
    page_next.add_argument("--max-depth", type=int, default=6, help="Control-tree traversal depth")
    page_next.add_argument("--direction", choices=["next", "prev", "first"], default="next")
    page_next.add_argument("--verify", action="store_true", help="Persist before/action/after evidence")
    page_next.add_argument("--timeout", type=float, help="MCP call timeout in seconds")

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

    agent = _target_agent(target, args.config)
    ready = agent.ensure_ready()
    if not ready.get("ok"):
        return _print_result(ready)

    if args.command == "window":
        return _cmd_window(args, agent, parser, target)
    if args.command == "scroll":
        return _cmd_scroll(args, agent, parser, target)
    if args.command == "page-next":
        return _cmd_page_next(args, agent, parser, target)

    parser.error(f"unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
