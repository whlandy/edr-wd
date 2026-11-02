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
from agent.file_transfer import download_file, upload_file
from agent.execution import AtomicExecutor
from agent.execution.confirmation import ConfirmationGate, ExecutionContext
from agent.trace.store import TraceStore
from agent.recording.artifacts import write_compilation_artifacts
from agent.recording.collection import (
    CollectionRetryError,
    RetryingCollectionAgent,
    collect_recording_artifacts,
)
from agent.recording.compiler import compile_recording
from agent.recording.mcp_runtime import MCPActionDispatch, MCPObservationProvider
from agent.recording.replay import load_golden_trace, replay_golden_trace
from agent.recording.runtime import build_replay_runtime, build_visual_resolver
from agent.recording.pillow_matcher import PillowTemplateMatcher
from agent.recording.visual import SafeVisualResolver
from agent.products import backend_for_product, get_product_adapter, get_product_profile
from agent.products.edrclient import EDRClientAdapter
from agent.products.intent import IntentRequest
from agent.interaction.runtime import execute_intent
from agent.trace.events import EventType
from agent.subagent.target_agent import TargetSubAgent
from agent.target_config import TargetConfig, add_config_arguments, run_config_command
from target.recording.models import RawRecording, RecordingModelError
from target.action_catalog import ACTIONS_V1, CATALOG_VERSION, catalog_digest, get_spec
from target.product_profiles import BUILTIN_CAPABILITIES, DEFAULT_APP_ID, INSTALLED_APPS


def recording_stop_advisories(result: dict, issues: list[dict],
                              golden_status: str) -> list[str]:
    """`record stop` 输出的人话指引：空录与编译缺口的分类建议。

    空录/租约过期以前要等操作者在落盘后自己发现；verifier 缺失以前只报 code
    不说下一步。指引按真实 issue 分类：**只有 `compile_*_verifier_required`
    才指引 record assert 补结果断言**；选择器歧义 / 窗口绑定各有各的修法；
    未登记码注明以上游为准，不臆造修法、不放宽 ready 门槛。
    """
    advisories: list[str] = []
    action_count = result.get("actionCount", 0)
    reason = str(result.get("reason") or "")
    if action_count == 0:
        if "lease" in reason:
            advisories.append(
                f"这轮没有录到任何动作：录制租约在停止前已过期（reason={reason}）。")
        else:
            advisories.append("这轮没有录到任何动作（actionCount=0）。")
        advisories.append(
            "重录：压缩 record start → 界面操作 → record stop 的时间窗；"
            "操作期间可随时跑 record status 心跳续租"
            "（输出含 leaseSecondsRemaining，可看租约剩余秒数）。")
        return advisories
    if golden_status != "incomplete":
        return advisories
    verifier = [i for i in issues
                if str(i.get("code", "")).endswith("_verifier_required")]
    if verifier:
        seqs = ", ".join(str(i.get("sequence")) for i in verifier)
        advisories.append(
            f"golden 未就绪：第 {seqs} 步（scroll/drag）缺绑定的结果 verifier"
            "（无法证明移动改变了状态）。用 record assert 在该步之后补一条"
            "内容/位置/结果断言，再跑 record compile 重编译 —— 不必整条重录。")
    ambiguous = [i for i in issues if i.get("code") in {
        "compile_selector_ambiguous", "compile_assertion_selector_ambiguous"}]
    if ambiguous:
        seqs = ", ".join(str(i.get("sequence")) for i in ambiguous)
        advisories.append(
            f"golden 未就绪：第 {seqs} 步缺稳定语义选择器；"
            "为该动作补语义选择器后 record compile 重编译。")
    weak_ambiguous = [i for i in issues if i.get("code")
                      == "compile_weak_or_ambiguous_identity"]
    if weak_ambiguous:
        seqs = ", ".join(str(i.get("sequence")) for i in weak_ambiguous)
        advisories.append(
            f"golden 未就绪：第 {seqs} 步是无稳定 automation id 的弱身份目标，"
            "且整窗候选不止一个，无法保证回放唯一；请为该动作录制/确认一个"
            "语义唯一的目标（稳定行键/行内容）后再 compile。")
    weak_unresolved = [i for i in issues if i.get("code")
                       == "compile_weak_identity_unresolved"]
    if weak_unresolved:
        seqs = ", ".join(str(i.get("sequence")) for i in weak_unresolved)
        advisories.append(
            f"golden 未就绪：第 {seqs} 步是无稳定 automation id 的弱身份目标，"
            "且录制未提供可证明唯一的完整控件树；无法保证回放唯一。"
            "请为动作捕获一个稳定 automation id / 语义身份后再 compile。")
    transition = [i for i in issues if i.get("code") == "compile_transition_unbound"]
    if transition:
        seqs = ", ".join(str(i.get("sequence")) for i in transition)
        advisories.append(
            f"golden 未就绪：第 {seqs} 步窗口切换未绑定到前置动作；"
            "补上因果绑定后 record compile 重编译。")
    tree_undercount = [i for i in issues
                       if i.get("code") == "compile_weak_identity_tree_undercount"]
    if tree_undercount:
        seqs = ", ".join(str(i.get("sequence")) for i in tree_undercount)
        advisories.append(
            f"golden 未就绪：第 {seqs} 步录到的控件树低估了同名候选（虚拟化表格"
            "可能没加载全部行），唯一性无法证明；把表格完整加载后重录该动作，"
            "或捕获稳定行键/行内容、确认语义唯一身份后再 compile。")
    volatile = [i for i in issues
                if i.get("code") == "compile_weak_identity_volatile_label"]
    if volatile:
        seqs = ", ".join(str(i.get("sequence")) for i in volatile)
        advisories.append(
            f"golden 未就绪：第 {seqs} 步的选择器靠控件文本身份解析，而同一位置"
            "在别的录制帧里显示过不同标签——回放时该标签会找不到。请捕获稳定的"
            " automation id，或锚定到录制与回放之间不会变化的内容，再 compile。")
    recorder_ui = [i for i in issues
                   if i.get("code") == "recording_recorder_ui_events"]
    if recorder_ui:
        counts = ", ".join(
            f"第 {i.get('sequence')} 步" if i.get("sequence")
            else "录制会话" for i in recorder_ui)
        advisories.append(
            f"golden 未就绪：{counts} 前后录制面板（指示器/断言编辑器）吞掉了"
            "本应发给目标窗口的输入；录制时不要 connect/lock 录制器自身 UI，"
            "重录该段动作后再 compile。")
    unidentified = [i for i in issues
                    if i.get("code") == "recording_unidentified_target_events"]
    if unidentified:
        seqs = ", ".join(str(i.get("sequence")) for i in unidentified)
        advisories.append(
            f"golden 未就绪：第 {seqs} 步有点击落在应用无法命名的目标上"
            "（如空白区域）；请点击可命名的控件后重录该动作再 compile。")
    missing_expected = [i for i in issues
                        if i.get("code") == "compile_assertion_missing_expected"]
    if missing_expected:
        seqs = ", ".join(str(i.get("sequence")) for i in missing_expected)
        advisories.append(
            f"golden 未就绪：第 {seqs} 步断言缺 expected 值；"
            "在断言编辑器里补上期望值后 compile。")
    unsupported = [i for i in issues
                   if i.get("code") == "compile_action_unsupported"]
    if unsupported:
        seqs = ", ".join(str(i.get("sequence")) for i in unsupported)
        advisories.append(
            f"golden 未就绪：第 {seqs} 步是不支持回放的事件类型；"
            "该动作不在当前动作目录内，无法通过补断言修复，需以目录能力为准重录。")
    region_container = [i for i in issues
                        if i.get("code") == "compile_region_container_unresolved"]
    if region_container:
        seqs = ", ".join(str(i.get("sequence")) for i in region_container)
        advisories.append(
            f"golden 未就绪：第 {seqs} 步区域断言的容器没有稳定身份"
            "（不会降级为坐标）；为容器补稳定身份后 compile。")
    window_identity = [i for i in issues
                       if i.get("code") == "compile_window_identity_ambiguous"]
    if window_identity:
        seqs = ", ".join(str(i.get("sequence")) for i in window_identity)
        advisories.append(
            f"golden 未就绪：第 {seqs} 步前后录到过两个同标题/同身份的共存窗口，"
            "回放只能锁住其中一个，无法证明步骤落在录制时的那个窗口里；"
            "请在每步操作前用稳定窗口根身份（automation id）重新锁定窗口，"
            "或避免流程跨越两个同标题窗口后 compile。")
    missing_evidence = [i for i in issues
                        if i.get("code") == "recording_capture_evidence_unavailable"]
    if missing_evidence:
        seqs = ", ".join(str(i.get("sequence")) for i in missing_evidence)
        advisories.append(
            f"golden 未就绪：第 {seqs} 步有生命周期截图未能捕获；"
            "证据不完整，无法证明步骤在录制时真实发生，请重录该段动作后 compile。")
    secret_review = [i for i in issues
                     if i.get("code") == "recording_secret_review_required"]
    if secret_review:
        seqs = ", ".join(str(i.get("sequence")) for i in secret_review)
        advisories.append(
            f"golden 未就绪：第 {seqs} 步涉及受保护文本（密码/凭据）且未通过"
            "环境变量 textSource 提供内容，或无法确认该输入框是否敏感；"
            "明文不落盘，请在断言编辑器里改用环境变量来源后 compile。")
    packets = [i for i in issues if i.get("code") == "recording_packets_dropped"]
    if packets:
        advisories.append(
            "录制会话曾丢弃原生输入包（capture queue 溢出）；"
            "录到的动作序列可能不完整，请减轻同机负载后重录再 compile。")
    correlation = [i for i in issues
                   if i.get("code") == "recording_correlation_errors"]
    if correlation:
        advisories.append(
            "录制捕获层报告了相关联错误（correlation）；动作与证据的对应关系"
            "可能不完整，请重录该段动作后再 compile。")
    empty_recording = [i for i in issues
                       if i.get("code") == "compile_recording_empty"]
    if empty_recording:
        advisories.append(
            "录制里没有任何被接受的动作事件；确认操作发生在锁定窗口内、"
            "捕获源正常后重录。")
    other = sorted({str(i["code"]) for i in issues
                    if i.get("code") not in _STOP_ADVISORY_KNOWN_CODES})
    if other:
        advisories.append(
            "golden 未就绪：还有未登记的编译缺口 "
            f"{', '.join(other)} —— 修复方式以上游编译器口径为准。")
    return advisories


_STOP_ADVISORY_KNOWN_CODES = {
    "compile_scroll_verifier_required", "compile_drag_verifier_required",
    "compile_selector_ambiguous", "compile_assertion_selector_ambiguous",
    "compile_transition_unbound", "compile_recording_empty",
    "recording_packets_dropped", "recording_correlation_errors",
    "recording_secret_review_required", "compile_window_identity_ambiguous",
    "recording_capture_evidence_unavailable",
    "compile_weak_or_ambiguous_identity", "compile_weak_identity_unresolved",
    "compile_weak_identity_tree_undercount",
    "compile_weak_identity_volatile_label",
    "recording_recorder_ui_events", "recording_unidentified_target_events",
    "compile_assertion_missing_expected", "compile_action_unsupported",
    "compile_region_container_unresolved",
}


def _stop_advisory_known_codes() -> frozenset[str]:
    """known 集合的只读视图：parity 回归据此要求「登记即有指引」，防止
    新码只登记不给分支（§7n 轮 window_identity 的沉寂缺口）。"""
    return frozenset(_STOP_ADVISORY_KNOWN_CODES)


def with_lease_warning(result: dict) -> dict:
    """`record status` 的租约剩余不足 60s 时追加人话警告（纯展示，不改字段语义）。"""
    remaining = result.get("leaseSecondsRemaining")
    if (result.get("state") == "recording"
            and isinstance(remaining, (int, float)) and remaining < 60):
        return {"warnings": [
            f"录制租约仅剩 {remaining:.0f}s：跑一次 record status（默认心跳续租），"
            "否则到点会静默停止、之后的操作不会被录制。"], **result}
    return result


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

    sub.add_parser("products", help="List locally installed product profiles")

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

    open_app = sub.add_parser("open-app", help="Activate and focus the selected product's main window")
    open_app.add_argument("--profile", help="Product profile; defaults to the target config")
    open_app.add_argument("--timeout", type=float, default=30.0)

    intent = sub.add_parser("intent", help="Execute a registered product business intent")
    intent.add_argument("intent_id", help="Intent registered by the selected product adapter")
    intent.add_argument("--profile", help="Product profile; defaults to the target config")
    intent.add_argument("--trace-root", default="result-report/intent-traces")
    intent.add_argument("--asset-root", default=".", help="Root for reviewed, redacted visual templates")
    intent.add_argument("--timeout", type=float, default=60.0)

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
    record.add_argument("action", choices=["compile", "start", "status", "pause", "resume", "assert", "stop", "ui"])
    record.add_argument("recording", nargs="?", help="recording.json for the compile action")
    record.add_argument("--output-root", help="Recording artifact root (compile and stop)")
    record.add_argument("--name", help="Capture name (start only)")
    record.add_argument("--process-name", help="Locked application process (start only)")
    record.add_argument("--window-title", help="Locked application window title regex (start only)")
    record.add_argument("--lease-seconds", type=float, default=300.0, help="Target-local activity lease (start only)")
    record.add_argument("--profile", help="Bind generated golden trace to this app profile (compile and stop)")
    record.add_argument("--timeout", type=float, help="Per-call MCP timeout in seconds")
    record.add_argument("--no-heartbeat", action="store_true", help="Read status without renewing the recording lease")
    record.add_argument("--ui-interval", type=float, default=2.0, help="Recorder UI bridge poll interval seconds (ui only)")

    replay = sub.add_parser("replay", help="Replay a ready golden trace through the atomic executor")
    replay.add_argument("golden_trace")
    replay.add_argument("--profile", help="Product profile; defaults to the target config")
    replay.add_argument("--confirm-action", action="append", default=[], help="Out-of-band confirmation token scoped to one action ID")
    replay.add_argument("--max-depth", type=int, default=12, help="Fresh control-tree observation depth")
    replay.add_argument("--trace-root", default="result-report/replay-traces", help="Agent-local append-only replay trace root")
    replay.add_argument("--replay-mode", choices=["semantic_only", "semantic_first", "visual_only"], default="semantic_only")
    replay.add_argument(
        "--entry-recovery",
        action="store_true",
        help="Enable controlled entry-window recovery (R0 phase 1): on a structured "
             "connect_target_not_found, re-confirm read-only whether the entry window "
             "exists and report entry_window_absent instead of a generic refusal. "
             "Never launches a process, service or click. Default off.",
    )
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
    if args.action == "ui":
        from agent.recording.ui_bridge import run_recording_ui_bridge
        return run_recording_ui_bridge(
            agent, output_root=args.output_root, profile=profile,
            interval=max(0.5, args.ui_interval), timeout=args.timeout,
        )
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
    if args.action == "status" and result.get("ok"):
        result = with_lease_warning(result)
    if args.action != "stop" or not result.get("ok"):
        return _print_result(result)

    try:
        # 收集链路与 UI 桥（§6y）同一断连恢复语义：瞬态抖动重试，
        # 预算耗尽才按持久化失败报告。
        retrying = RetryingCollectionAgent(agent, interval=1.0, retries=3)
        artifacts, capture_issues = collect_recording_artifacts(
            retrying, result, args.output_root, profile, timeout=args.timeout,
        )
    except (KeyError, OSError, ValueError, CollectionRetryError) as exc:
        return _print_result({
            "ok": False,
            "code": "recording_persistence_failed",
            "error": str(exc),
            "target_result": result,
        })
    compiled_issues = [issue.to_dict() for issue in artifacts.compilation.issues]
    advisories = recording_stop_advisories(
        result, compiled_issues, artifacts.compilation.golden.status)
    payload = {
        **result,
        "status": artifacts.compilation.golden.status,
        "directory": str(artifacts.directory),
        "issues": compiled_issues,
        "captureIssues": capture_issues,
    }
    if advisories:
        payload = {"warnings": advisories, **payload}
    return _print_result(payload)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "config":
        return run_config_command(args)
    if args.command == "products":
        return _print_result({"ok": True, "default_app": DEFAULT_APP_ID,
                              "apps": [
            {"app_id": app_id, "name": app["name"], "default": app_id == DEFAULT_APP_ID,
             "profiles": [
                 {"profile_id": profile["profile_id"], "backend": profile["backend"],
                  "application": profile["application"],
                  "application_path": profile["application"]["path"],
                  "windows": profile["windows"]}
                 for profile in app["profiles"]]}
            for app_id, app in sorted(INSTALLED_APPS.items())],
                              "products": [
            {"profile_id": capability.profile_id, "backend": capability.backend,
             "version": capability.version, "digest": capability.digest}
            for _, capability in sorted(BUILTIN_CAPABILITIES.items())
        ]})

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
        adapter = EDRClientAdapter("windows_pywinauto")
        activated = adapter.activate(
            lambda name, params: agent.call_tool(name, params, timeout=args.timeout + 10)
        )
        if not activated.get("ok"):
            return _print_result(activated)
        main_role = adapter.resolve_window_role("main")
        edr_window = {"process_name": main_role.process_names[0],
                      "title_re": adapter.activation_window_title_pattern}
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

    if args.command == "open-app":
        target_config = TargetConfig(args.config)
        profile_id = args.profile or target_config.get_target_app_profile(target)
        if not profile_id:
            return _print_result({"ok": False, "code": "product_profile_unknown"})
        try:
            backend = backend_for_product(
                profile_id, backend_kind=getattr(agent.state, "backend_kind", None),
                platform=target_config.get_target_platform(target),
            )
            profile_obj = get_product_profile(profile_id, backend)
            adapter = get_product_adapter(profile_id, backend)
            adapter.resolve_window_role("main")
        except ValueError as exc:
            return _print_result({"ok": False, "code": "product_profile_unknown", "error": str(exc)})
        activate = getattr(adapter, "activate", None)
        if callable(activate):
            activated = activate(lambda name, params: agent.call_tool(
                name, params, timeout=args.timeout))
            if not activated.get("ok"):
                return _print_result(activated)
        else:
            activated = {"ok": True, "code": "activation_not_configured"}
        started = agent.call_tool("begin_product_run", {
            "profile_id": profile_id, "digest": profile_obj.digest,
        }, timeout=args.timeout)
        if not started.get("ok"):
            return _print_result(started)
        run_token = started["run_token"]
        try:
            focused = agent.call_tool("focus_window_role", {
                "role": "main", "run_token": run_token,
            }, timeout=args.timeout)
            if not focused.get("ok"):
                output = focused
            else:
                observation = agent.call_tool("observe_window_bundle", {
                    "run_token": run_token, "capture_image": True,
                    "include_ocr": False,
                }, timeout=args.timeout)
                if observation.get("ok") and observation.get("screenshot_redacted") is True:
                    artifact = _save_screenshot_result(
                        {"image_b64": observation.get("screenshot_b64")}, target)
                    output = {"ok": True, "profile_id": profile_id,
                              "activated": activated, "focused": focused,
                              "observation_id": observation.get("observation_id"),
                              "agent_artifact": artifact}
                else:
                    output = {"ok": False, "code": observation.get("code")
                              or "observation_capture_unredacted"}
        except (ValueError, KeyError) as exc:
            output = {"ok": False, "code": "product_capture_failed", "error": str(exc)}
        finally:
            ended = agent.call_tool("end_product_run", {"run_token": run_token},
                                    timeout=args.timeout)
        if not ended.get("ok"):
            output = {"ok": False, "code": "product_run_cleanup_failed"}
        return _print_result(output)

    if args.command == "window":
        return _cmd_window(args, agent, parser, target)
    if args.command == "intent":
        profile_id = args.profile or TargetConfig(args.config).get_target_app_profile(target)
        if not profile_id:
            return _print_result({"ok": False, "code": "product_profile_unknown"})
        platform = TargetConfig(args.config).get_target_platform(target)
        try:
            backend = backend_for_product(
                profile_id, backend_kind=getattr(agent.state, "backend_kind", None),
                platform=platform,
            )
            profile_obj = get_product_profile(profile_id, backend)
            adapter = get_product_adapter(profile_id, backend)
            intent_plan = adapter.resolve_intent(args.intent_id, {})
        except ValueError as exc:
            return _print_result({"ok": False, "code": "product_profile_unknown", "error": str(exc)})
        if callable(getattr(adapter, "activate", None)):
            activated = adapter.activate(
                lambda name, params: agent.call_tool(name, params, timeout=args.timeout)
            )
            if not activated.get("ok"):
                return _print_result(activated)
        started = agent.call_tool("begin_product_run", {
            "profile_id": profile_id, "digest": profile_obj.digest,
        }, timeout=args.timeout)
        if not started.get("ok"):
            return _print_result(started)
        run_token = started["run_token"]
        trace = TraceStore(Path(args.trace_root).expanduser().resolve())
        trace.open()
        try:
            observation = MCPObservationProvider(
                agent, trace_store=trace, timeout=args.timeout,
                capture_screenshot=True, use_bundle=True, run_token=run_token,
                include_ocr=True,
            )
            executor = AtomicExecutor(
                dispatch=MCPActionDispatch(agent, trace_store=trace, timeout=args.timeout,
                                           run_token=run_token),
                observation_provider=observation,
                confirmation_gate=ConfirmationGate(),
                risk_lookup=lambda action_id: (
                    (spec.risk, spec.side_effect) if (spec := get_spec(ACTIONS_V1, action_id))
                    else ("low", "none")
                ),
            )
            result = execute_intent(
                IntentRequest(profile_id, args.intent_id, {}, {"page": intent_plan.result_role}),
                adapter=adapter, profile=profile_obj, executor=executor,
                observation_provider=observation,
                visual_resolver=(build_visual_resolver(Path(args.asset_root).expanduser().resolve())
                                 if intent_plan.visual else None),
            )
            trace.finalize(EventType.TRACE_COMPLETED if result.passed else EventType.TRACE_ABORTED,
                           payload={"intent_id": args.intent_id, "passed": result.passed})
            output = {"ok": result.passed, "intent_id": args.intent_id,
                      "skipped_click": result.skipped_click,
                      "action": result.action_result.to_dict() if result.action_result else None,
                      "assertion": result.assertion_result.to_dict(),
                      "trace_directory": str(trace.root)}
        except Exception as exc:
            output = {"ok": False, "code": getattr(exc, "code", "intent_execution_failed"),
                      "error": str(exc), "trace_directory": str(trace.root)}
        finally:
            ended = agent.call_tool("end_product_run", {"run_token": run_token},
                                    timeout=args.timeout)
            if not ended.get("ok"):
                output = {"ok": False, "code": "product_run_cleanup_failed",
                          "trace_directory": str(trace.root)}
        return _print_result(output)
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
        replay_profile = args.profile
        if replay_profile is None:
            try:
                replay_profile = TargetConfig(args.config).get_target_app_profile(target)
            except KeyError:
                # Keep an unregistered legacy test target usable. A registered
                # target always takes its app profile from the shared catalog.
                replay_profile = "default"
        expected_profile = golden.environment.get("profile")
        if expected_profile and expected_profile != replay_profile:
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
        try:
            runtime = build_replay_runtime(
                agent,
                golden,
                profile=replay_profile,
                trace_root=args.trace_root,
                asset_root=Path(args.golden_trace).expanduser().resolve().parent,
                replay_mode=args.replay_mode,
                max_depth=args.max_depth,
                timeout=args.timeout,
                persist_screenshots=args.persist_screenshots,
                confirm_actions=args.confirm_action,
                entry_recovery=args.entry_recovery,
            )
        except RecordingModelError as exc:
            return _print_result({
                "ok": False, "code": exc.code, "error": str(exc), "path": exc.path,
            })
        run = replay_golden_trace(runtime, golden)
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
