"""Cross-platform command line entry point for EDR-WD agent operations."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

from agent import target_manager
from agent.e2e_report import create_run_dir, persist_target_screenshot
from agent.file_transfer import download_file, upload_file
from agent.execution import AtomicExecutor
from agent.execution.confirmation import ConfirmationGate, ExecutionContext
from agent.trace.store import TraceStore
from agent.recording.artifacts import write_compilation_artifacts
from agent.recording.compiler import compile_recording
from agent.recording.mcp_runtime import MCPActionDispatch, MCPObservationProvider
from agent.recording.replay import ReplayRuntime, load_golden_trace, replay_golden_trace
from agent.recording.pillow_matcher import PillowTemplateMatcher
from agent.recording.visual import SafeVisualResolver
from agent.subagent.target_agent import TargetSubAgent
from agent.target_config import TargetConfig, add_config_arguments, run_config_command
from target.recording.models import RawRecording
from target.action_catalog import ACTIONS_V1, CATALOG_VERSION, catalog_digest, get_spec


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


def _decision_block(
    *,
    tool: str,
    window_args: dict,
    result: dict | None = None,
    iterations: list | None = None,
) -> dict:
    """Record the ownership/coordinate decision context for an evidence record.

    P1.3 acceptance: "记录精确 action 参数与 ownership/coordinate 决策". The
    window-scoped composite tools (scroll_region / page_table) resolve a
    single connected window and dispatch window-relative coordinates that are
    converted to screen space, so the decision context is:

      * coordinate_space — the semantic space the action targeted ("window").
      * ownership       — the expected owning process/pid the backend used
        for the candidate control / ownership check (only the keys actually
        supplied in the selection args are recorded; nothing is fabricated).
      * verdict         — the stable ownership/coordinate resolution codes
        the backend returned per step (e.g. WHEEL_MOVED / NEXT_PAGE for a
        clean dispatch, or target_occluded / target_ambiguous /
        point_outside_window / target_not_found when resolution failed).
    """
    ownership = {}
    if window_args.get("process_name"):
        ownership["process_name"] = window_args["process_name"]
    if window_args.get("pid") is not None:
        ownership["pid"] = window_args["pid"]

    codes = []
    source = iterations if iterations is not None else ([result] if result else [])
    for item in source:
        code = (item or {}).get("code")
        if code:
            codes.append(code)
    if not codes and result:
        code = result.get("code")
        if code:
            codes.append(code)

    return {
        "coordinate_space": "window",
        "owner": ownership,
        "verdict_codes": codes,
        "tool_domain": tool,
    }


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
        "## Decision (ownership/coordinate)",
        "",
        "```json",
        json.dumps(evidence.get("decision", {}), ensure_ascii=False, indent=2, default=str),
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
        "decision": _decision_block(tool=tool, window_args=arguments, result=result),
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
            "decision": _decision_block(
                tool="scroll_region",
                window_args=window_args,
                iterations=iterations,
            ),
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

    upload = sub.add_parser("file-upload", help="Upload a file through MCP with optional SCP fallback")
    upload.add_argument("local_path")
    upload.add_argument("relative_path", help="Destination relative to the target transfer root")
    upload.add_argument("--overwrite", action="store_true")
    upload.add_argument("--no-scp-fallback", action="store_true")

    download = sub.add_parser("file-download", help="Download a file through MCP with optional SCP fallback")
    download.add_argument("relative_path", help="Source relative to the target transfer root")
    download.add_argument("local_path")
    download.add_argument("--overwrite", action="store_true")
    download.add_argument("--no-scp-fallback", action="store_true")

    record = sub.add_parser("record", help="Compile or manage a scoped desktop recording")
    record.add_argument("action", choices=["compile", "start", "status", "pause", "resume", "assert", "stop"])
    record.add_argument("recording", nargs="?", help="recording.json for the compile action")
    record.add_argument("--output-root", help="Recording artifact root (compile and stop)")
    record.add_argument("--name", help="Capture name (start only)")
    record.add_argument("--process-name", help="Locked application process (start only)")
    record.add_argument("--window-title", help="Locked application window title regex (start only)")
    record.add_argument("--lease-seconds", type=float, default=300.0, help="Target-local activity lease (start only)")
    record.add_argument("--profile", help="Bind generated golden trace to this app profile (compile and stop)")
    record.add_argument("--timeout", type=float, help="Per-call MCP timeout in seconds")
    record.add_argument("--no-heartbeat", action="store_true", help="Read status without renewing the recording lease")

    replay = sub.add_parser("replay", help="Replay a ready golden trace through the atomic executor")
    replay.add_argument("golden_trace")
    replay.add_argument("--profile", default="default")
    replay.add_argument("--confirm-action", action="append", default=[], help="Out-of-band confirmation token scoped to one action ID")
    replay.add_argument("--max-depth", type=int, default=12, help="Fresh control-tree observation depth")
    replay.add_argument("--trace-root", default="result-report/replay-traces", help="Agent-local append-only replay trace root")
    replay.add_argument("--replay-mode", choices=["semantic_only", "semantic_first", "visual_only"], default="semantic_only")
    replay.add_argument(
        "--timeout", type=float, default=60.0,
        help="Per-call MCP timeout for replay observation and dispatch",
    )
    replay.add_argument(
        "--persist-screenshots",
        action="store_true",
        help="Write target-side source-redacted runtime frames into the replay trace",
    )
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


def _cmd_record(
    args: argparse.Namespace,
    agent: TargetSubAgent,
    parser: argparse.ArgumentParser,
    *,
    profile: str | None = None,
) -> int:
    """Manage target-local recording and persist stopped captures locally."""
    if args.action == "start":
        missing = [
            option
            for option, value in (
                ("--name", args.name),
                ("--process-name", args.process_name),
                ("--window-title", args.window_title),
            )
            if not value
        ]
        if missing:
            parser.error(f"record start requires {', '.join(missing)}")

        active = agent.call_tool(
            "recording_status", {"heartbeat": False}, timeout=args.timeout,
        )
        if active.get("ok") and active.get("state") in {
            "starting", "recording", "paused", "stopping",
        }:
            return _print_result({
                "ok": False,
                "code": "recording_session_active",
                "error": "another target-local recording session is active",
                "state": active.get("state"),
            })

        # Recording is allowed only after the existing target ownership path
        # has resolved and locked one matching application window.
        connected = agent.call_tool(
            "connect",
            {
                "process_name": args.process_name,
                "title_re": args.window_title,
                "timeout": args.timeout or 10.0,
            },
            timeout=args.timeout,
        )
        if not connected.get("ok"):
            return _print_result(connected)
        locked = agent.call_tool(
            "lock_window",
            {
                "process_name": args.process_name,
                "title_re": args.window_title,
                "activate": True,
            },
            timeout=args.timeout,
        )
        if not locked.get("ok"):
            return _print_result(locked)
        verified = agent.call_tool(
            "verify_window_lock",
            {"activate": True},
            timeout=args.timeout,
        )
        if not verified.get("ok"):
            return _print_result(verified)
        result = agent.call_tool(
            "start_recording",
            {
                "name": args.name,
                "process_name": args.process_name,
                "window_title": args.window_title,
                "lease_seconds": args.lease_seconds,
            },
            timeout=args.timeout,
        )
        return _print_result(result)

    tool_by_action = {
        "status": "recording_status",
        "pause": "pause_recording",
        "resume": "resume_recording",
        "stop": "stop_recording",
    }
    if args.action == "assert":
        return _print_result(agent.call_tool(
            "add_recording_assertion", {}, timeout=args.timeout,
        ))

    tool = tool_by_action[args.action]
    arguments = {"heartbeat": not args.no_heartbeat} if args.action == "status" else {}
    result = agent.call_tool(tool, arguments, timeout=args.timeout)
    if args.action != "stop" or not result.get("ok"):
        return _print_result(result)

    try:
        recording = RawRecording.from_dict(result["recording"])
        compilation = compile_recording(recording, profile=profile)
        captures: dict[str, bytes] = {}
        seen_capture_ids: set[str] = set()
        capture_issues: list[dict] = []
        for event in recording.events:
            for evidence_key in ("beforeCapture", "capture"):
                metadata = event.evidence.get(evidence_key)
                capture_id = metadata.get("id") if isinstance(metadata, dict) else None
                if not isinstance(capture_id, str) or capture_id in seen_capture_ids:
                    continue
                seen_capture_ids.add(capture_id)
                fetched = agent.call_tool(
                    "get_recording_capture", {"capture_id": capture_id}, timeout=args.timeout,
                )
                encoded = fetched.get("image_b64") if isinstance(fetched, dict) else None
                try:
                    payload = base64.b64decode(encoded, validate=True) if isinstance(encoded, str) else None
                except (binascii.Error, ValueError):
                    payload = None
                digest = (
                    "sha256:" + hashlib.sha256(payload).hexdigest()
                    if payload is not None else None
                )
                if (
                    not isinstance(fetched, dict)
                    or not fetched.get("ok")
                    or payload is None
                    or digest != metadata.get("sha256")
                    or digest != fetched.get("sha256")
                ):
                    capture_issues.append({
                        "code": "recording_capture_fetch_failed",
                        "sequence": event.sequence,
                        "evidence": evidence_key,
                        "captureId": capture_id,
                    })
                    continue
                captures[capture_id] = payload
        output_root = Path(args.output_root).expanduser() if args.output_root else Path("recordings")
        artifacts = write_compilation_artifacts(
            output_root,
            recording,
            compilation,
            captures=captures,
            artifact_issues=capture_issues,
        )
    except (KeyError, OSError, ValueError) as exc:
        return _print_result({
            "ok": False,
            "code": "recording_persistence_failed",
            "error": str(exc),
            "target_result": result,
        })
    return _print_result({
        **result,
        "status": compilation.golden.status,
        "directory": str(artifacts.directory),
        "issues": [issue.to_dict() for issue in compilation.issues],
        "captureIssues": capture_issues,
    })


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "config":
        return run_config_command(args)

    if args.command == "record" and args.action == "compile":
        if not args.recording:
            parser.error("record compile requires recording.json")
        source = Path(args.recording).expanduser().resolve()
        recording = RawRecording.from_dict(json.loads(source.read_text(encoding="utf-8")))
        result = compile_recording(recording, profile=args.profile)
        output_root = Path(args.output_root).expanduser() if args.output_root else source.parent
        artifacts = write_compilation_artifacts(output_root, recording, result)
        return _print_result({
            "ok": result.golden.status == "ready",
            "status": result.golden.status,
            "directory": str(artifacts.directory),
            "issues": [issue.to_dict() for issue in result.issues],
        })

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
    if args.command == "file-upload":
        return _print_result(upload_file(
            target,
            args.local_path,
            args.relative_path,
            config=TargetConfig(args.config),
            overwrite=args.overwrite,
            allow_scp_fallback=not args.no_scp_fallback,
        ))
    if args.command == "file-download":
        return _print_result(download_file(
            target,
            args.relative_path,
            args.local_path,
            config=TargetConfig(args.config),
            overwrite=args.overwrite,
            allow_scp_fallback=not args.no_scp_fallback,
        ))
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
    if args.command == "record":
        profile = args.profile
        if profile is None:
            try:
                profile = TargetConfig(args.config).get_target_app_profile(target)
            except KeyError:
                profile = None
        return _cmd_record(args, agent, parser, profile=profile)
    if args.command == "replay":
        try:
            golden = load_golden_trace(args.golden_trace)
        except (OSError, ValueError) as exc:
            return _print_result({
                "ok": False,
                "code": getattr(exc, "code", "golden_trace_invalid"),
                "error": str(exc),
                "path": getattr(exc, "path", ""),
            })
        if golden.status != "ready":
            return _print_result({
                "ok": False,
                "code": "golden_trace_incomplete",
                "error": "only ready golden traces can replay",
            })
        if (
            golden.catalog.get("version") != CATALOG_VERSION
            or golden.catalog.get("digest") != catalog_digest()
        ):
            return _print_result({
                "ok": False,
                "code": "golden_catalog_mismatch",
                "error": "golden trace catalog binding differs from the replay runtime",
            })
        expected_profile = golden.environment.get("profile")
        if expected_profile and expected_profile != args.profile:
            return _print_result({
                "ok": False,
                "code": "golden_profile_mismatch",
                "error": "golden trace profile differs from the replay execution profile",
            })
        active_recording = agent.call_tool("recording_status", {"heartbeat": False})
        if active_recording.get("ok") and active_recording.get("state") in {"recording", "paused"}:
            return _print_result({
                "ok": False,
                "code": "recording_session_active",
                "error": "capture and replay cannot run concurrently on the same target",
            })
        selectors = [
            step.selector for step in (*golden.steps, *golden.cleanup)
            if step.selector is not None
        ]
        first_selector = selectors[0] if selectors else None
        process_name = golden.environment.get("application") or (
            first_selector.window.get("processName") if first_selector else None
        )
        title_re = first_selector.window.get("titleRegex") if first_selector else None
        if not process_name or not title_re:
            return _print_result({
                "ok": False,
                "code": "replay_scope_missing",
                "error": "golden trace has no application process/title scope",
            })
        connected = agent.call_tool("connect", {
            "process_name": process_name, "title_re": title_re, "timeout": 10.0,
        })
        if not connected.get("ok"):
            return _print_result(connected)
        locked = agent.call_tool("lock_window", {
            "process_name": process_name, "title_re": title_re,
            "strict": True, "activate": True,
        })
        if not locked.get("ok"):
            return _print_result(locked)
        verified = agent.call_tool("verify_window_lock", {"activate": True})
        if not verified.get("ok"):
            return _print_result(verified)

        trace_store = TraceStore(Path(args.trace_root).expanduser().resolve())
        trace_store.open()
        observations = MCPObservationProvider(
            agent,
            max_depth=args.max_depth,
            trace_store=trace_store,
            capture_screenshot=(
                args.replay_mode != "semantic_only" or args.persist_screenshots
            ),
            timeout=args.timeout,
        )
        visual_resolver = None
        if args.replay_mode != "semantic_only":
            matcher = PillowTemplateMatcher()
            asset_root = Path(args.golden_trace).expanduser().resolve().parent

            def verified_template_path(template: str, visual: dict | None = None):
                template_path = (asset_root / template).resolve()
                if asset_root != template_path and asset_root not in template_path.parents:
                    return None
                if visual is not None:
                    expected = visual.get("elementSha256")
                    if not isinstance(expected, str) or not template_path.is_file():
                        return None
                    actual = "sha256:" + hashlib.sha256(template_path.read_bytes()).hexdigest()
                    if actual != expected:
                        return None
                return template_path

            def match_template(template: str, observation: dict):
                template_path = verified_template_path(template)
                if template_path is None:
                    return []
                return matcher(str(template_path), observation)

            visual_resolver = SafeVisualResolver(
                match_template,
                template_verifier=lambda template, visual: (
                    verified_template_path(template, dict(visual)) is not None
                ),
            )

        def risk_lookup(action_id: str) -> tuple[str, str]:
            spec = get_spec(ACTIONS_V1, action_id)
            return (spec.risk, spec.side_effect) if spec is not None else ("low", "none")

        executor = AtomicExecutor(
            dispatch=MCPActionDispatch(agent, trace_store=trace_store, timeout=args.timeout),
            observation_provider=observations,
            confirmation_gate=ConfirmationGate(),
            risk_lookup=risk_lookup,
        )
        run = replay_golden_trace(
            ReplayRuntime(
                executor=executor,
                catalog_version=CATALOG_VERSION,
                catalog_digest=catalog_digest(),
                execution_context=ExecutionContext(
                    profile=args.profile,
                    confirmation_tokens=frozenset(args.confirm_action),
                ),
                trace_store=trace_store,
                replay_mode=args.replay_mode,
                visual_resolver=visual_resolver,
                persist_replay_screenshots=args.persist_screenshots,
                window_focus=lambda process_name, title_regex: observations.get_snapshot(
                    observations.focus_window(process_name, title_regex)
                ),
            ),
            golden,
        )
        return _print_result({
            "ok": run.evaluation.task_success,
            "case": run.case_result.to_dict(),
            "cleanup": run.cleanup_result.to_dict() if run.cleanup_result else None,
            "evaluation": run.evaluation.to_dict(),
            "trace_directory": str(run.trace_root) if run.trace_root else None,
            "evaluation_path": str(run.evaluation_path) if run.evaluation_path else None,
        })
    if args.command == "scroll":
        return _cmd_scroll(args, agent, parser, target)
    if args.command == "page-next":
        return _cmd_page_next(args, agent, parser, target)

    parser.error(f"unsupported command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
