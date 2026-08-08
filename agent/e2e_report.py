"""Local report persistence for live E2E runs.

Target screenshots are returned over MCP as base64.  This module persists the
returned bytes on the agent host and writes small JSON/Markdown run reports.
Runtime output lives under
``~/Desktop/edr-wd-record/result-report/<UTC timestamp>`` by default.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any


DEFAULT_RECORD_ROOT = Path.home() / "Desktop" / "edr-wd-record"
DEFAULT_REPORT_ROOT = DEFAULT_RECORD_ROOT / "result-report"


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-._")
    return cleaned or "unknown-target"


def create_run_dir(target: str, *, root: Path | None = None) -> Path:
    """Create one collision-safe, agent-local directory for an E2E run."""
    record_root = Path(os.environ.get("EDR_WD_RECORD_DIR", DEFAULT_RECORD_ROOT))
    output_root = Path(
        os.environ.get(
            "EDR_WD_E2E_REPORT_ROOT", root or record_root / "result-report"
        )
    )
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S-%f")
    run_dir = output_root / stamp
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "screenshots").mkdir()
    return run_dir


def persist_target_screenshot(result: dict[str, Any], run_dir: Path, name: str) -> dict[str, Any]:
    """Decode an MCP screenshot result and persist it on the agent host."""
    encoded = result.get("image_b64") or result.get("image_base64") or result.get("image")
    if not isinstance(encoded, str) or not encoded:
        raise ValueError("target screenshot response has no base64 image payload")
    if encoded.startswith("data:"):
        _, _, encoded = encoded.partition(",")
    try:
        png_bytes = base64.b64decode(encoded, validate=True)
    except Exception as exc:
        raise ValueError("target screenshot payload is not valid base64") from exc
    if not png_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("target screenshot payload is not a PNG")

    filename = f"{_safe_name(name)}.png"
    path = Path(run_dir) / "screenshots" / filename
    _atomic_write(path, png_bytes)
    return {
        "evidence_id": f"IMG-{uuid.uuid4().hex[:16]}",
        "path": path.relative_to(run_dir).as_posix(),
        "sha256": "sha256:" + hashlib.sha256(png_bytes).hexdigest(),
        "bytes": len(png_bytes),
        "width": result.get("width"),
        "height": result.get("height"),
        "target_path": result.get("path") or result.get("saved_to"),
    }


class E2EEvidenceLifecycle:
    """Automatic baseline/after screenshot chain for live E2E actions.

    One physical baseline is captured at INIT.  Every successful action then
    captures one physical ``after`` image; that evidence id becomes the next
    action's ``before_evidence_id``.  A fresh before image is only needed when
    the chain has no usable current image.
    """

    def __init__(self, run_dir: Path, *, screenshot_sink, step_sink) -> None:
        self.run_dir = Path(run_dir)
        self._screenshot_sink = screenshot_sink
        self._step_sink = step_sink
        self._current: dict[str, Any] | None = None

    @property
    def current_evidence_id(self) -> str | None:
        return self._current.get("evidence_id") if self._current else None

    def initialize(self, client, *, process_name: str) -> dict[str, Any]:
        """INIT capture. Calling it again reuses the existing baseline."""
        if self._current_is_valid():
            return self._current
        self._current = None
        self._connect(client, process_name)
        metadata = self._capture(client, "000-init-baseline", role="baseline")
        self._current = metadata
        return metadata

    def run_action(
        self,
        client,
        *,
        step_id: str,
        tool: str,
        arguments: dict[str, Any] | None = None,
        before_process_name: str | None = None,
        after_process_name: str,
    ) -> dict[str, Any]:
        """Execute one action and automatically record its evidence lifecycle."""
        before_recaptured = not self._current_is_valid()
        if before_recaptured:
            self._current = None
            if not before_process_name:
                raise RuntimeError(
                    f"step {step_id!r} has no evidence baseline and no before_process_name"
                )
            self.initialize(client, process_name=before_process_name)

        before_id = self.current_evidence_id
        result = client.call_tool(tool, arguments or {})
        role = "after" if result.get("ok") is not False else "failure"

        # Connect is evidence plumbing, not a user test step. It makes the
        # screenshot target explicit and prevents a stale window capture.
        try:
            self._connect(client, after_process_name)
        except AssertionError:
            if role != "failure" or not before_process_name:
                raise
            # A failed action may never create its expected target window.
            # Capture failure evidence from the last known window instead.
            self._connect(client, before_process_name)
        metadata = self._capture(client, f"{step_id}-{role}", role=role)
        step_record = {
            "step_id": step_id,
            "action": {"tool": tool, "arguments": arguments or {}},
            "status": "passed" if result.get("ok") is not False else "failed",
            "before_evidence_id": before_id,
            "after_evidence_id": metadata["evidence_id"],
            "capture_role": role,
            "before_recaptured": before_recaptured,
        }
        self._step_sink(step_record)
        if role == "after":
            self._current = metadata
        return result

    def _current_is_valid(self) -> bool:
        if self._current is None:
            return False
        path = self.run_dir / str(self._current.get("path", ""))
        expected = self._current.get("sha256")
        if not path.is_file() or not isinstance(expected, str):
            return False
        actual = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        return actual == expected

    @staticmethod
    def _connect(client, process_name: str) -> None:
        result = client.call_tool(
            "connect", {"process_name": process_name, "timeout": 10.0}
        )
        if result.get("ok") is not True:
            raise AssertionError(f"evidence connect failed for {process_name}: {result}")

    def _capture(self, client, name: str, *, role: str) -> dict[str, Any]:
        result = client.call_tool("screenshot", {})
        if result.get("ok") is not True:
            raise AssertionError(f"automatic {role} screenshot failed: {result}")
        metadata = persist_target_screenshot(result, self.run_dir, name)
        metadata["role"] = role
        metadata["operation_sequence"] = getattr(
            client, "last_operation_sequence", None
        )
        self._screenshot_sink(metadata)
        return metadata


def write_report(run_dir: Path, report: dict[str, Any]) -> None:
    """Atomically write JSON, Markdown, and browser-friendly HTML reports."""
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    _atomic_write(Path(run_dir) / "report.json", payload)

    summary = report["summary"]
    lines = [
        "# EDR-WD E2E Report",
        "",
        f"- Target: `{report['target']}`",
        f"- Started: `{report['started_at']}`",
        f"- Finished: `{report['finished_at']}`",
        f"- Result: **{summary['passed']} passed, {summary['failed']} failed, {summary['skipped']} skipped**",
        "",
        "## Test cases",
        "",
        "| Status | Test | Duration (s) |",
        "|---|---|---:|",
    ]
    for case in report["tests"]:
        lines.append(f"| {case['outcome']} | `{case['nodeid']}` | {case['duration_seconds']:.3f} |")
    if report.get("screenshots"):
        lines.extend(["", "## Screenshots", ""])
        for screenshot in report["screenshots"]:
            lines.append(f"- [{screenshot['path']}]({screenshot['path']}) — `{screenshot['sha256']}`")
    if report.get("evidence_steps"):
        lines.extend([
            "",
            "## Evidence lifecycle",
            "",
            "| Step | Action | Before evidence | After evidence | Status |",
            "|---|---|---|---|---|",
        ])
        for step in report["evidence_steps"]:
            lines.append(
                f"| `{step['step_id']}` | `{step['action']['tool']}` | "
                f"`{step['before_evidence_id']}` | `{step['after_evidence_id']}` | "
                f"{step['status']} |"
            )
    if report.get("operation_trace"):
        lines.extend([
            "",
            "## Operation trace",
            "",
            "| # | Time | Tool | Duration (ms) | Result |",
            "|---:|---|---|---:|---|",
        ])
        for entry in report["operation_trace"]:
            result = entry.get("result", {})
            result_text = result.get("error") or f"ok={result.get('ok')}"
            lines.append(
                f"| {entry['sequence']} | `{entry['started_at']}` | "
                f"`{entry['tool']}` | {entry['duration_ms']} | {result_text} |"
            )
    _atomic_write(Path(run_dir) / "report.md", ("\n".join(lines) + "\n").encode("utf-8"))
    _atomic_write(
        Path(run_dir) / "report.html",
        _render_html_report(report).encode("utf-8"),
    )


def _render_html_report(report: dict[str, Any]) -> str:
    esc = lambda value: html.escape(str(value), quote=True)
    summary = report["summary"]
    test_rows = "".join(
        "<tr>"
        f"<td><span class='status {esc(case['outcome'])}'>{esc(case['outcome'])}</span></td>"
        f"<td><code>{esc(case['nodeid'])}</code></td>"
        f"<td>{float(case['duration_seconds']):.3f}</td>"
        "</tr>"
        for case in report["tests"]
    )
    evidence_rows = "".join(
        "<tr>"
        f"<td><code>{esc(step['step_id'])}</code></td>"
        f"<td><code>{esc(step['action']['tool'])}</code></td>"
        f"<td><code>{esc(step['before_evidence_id'])}</code></td>"
        f"<td><code>{esc(step['after_evidence_id'])}</code></td>"
        f"<td><span class='status {esc(step['status'])}'>{esc(step['status'])}</span></td>"
        "</tr>"
        for step in report.get("evidence_steps", [])
    )
    cards = "".join(
        "<figure>"
        f"<a href='{esc(shot['path'])}'><img loading='lazy' src='{esc(shot['path'])}' "
        f"alt='{esc(shot.get('role', 'screenshot'))}'></a>"
        f"<figcaption><strong>{esc(shot.get('role', 'screenshot'))}</strong> · "
        f"<code>{esc(shot.get('evidence_id', ''))}</code><br>"
        f"{esc(shot.get('width'))}×{esc(shot.get('height'))} · "
        f"<code>{esc(shot['sha256'])}</code></figcaption>"
        "</figure>"
        for shot in report.get("screenshots", [])
    )
    screenshots_by_sequence = {
        shot.get("operation_sequence"): shot
        for shot in report.get("screenshots", [])
        if shot.get("operation_sequence") is not None
    }
    trace_row_parts = []
    for entry in report.get("operation_trace", []):
        shot = screenshots_by_sequence.get(entry["sequence"])
        evidence = ""
        if shot:
            evidence = (
                "<div class='trace-shot'>"
                f"<a href='{esc(shot['path'])}'><img src='{esc(shot['path'])}' "
                f"alt='Trace #{entry['sequence']} screenshot'></a>"
                f"<div>Captured here · <code>{esc(shot['evidence_id'])}</code></div>"
                "</div>"
            )
        trace_row_parts.append(
            "<tr>"
            f"<td>{entry['sequence']}</td>"
            f"<td><code>{esc(entry['started_at'])}</code></td>"
            f"<td><code>{esc(entry['tool'])}</code>{evidence}</td>"
            f"<td>{entry['duration_ms']}</td>"
            "<td><details><summary>查看参数与结果</summary>"
            f"<h4>Arguments</h4><pre>{esc(json.dumps(entry.get('arguments', {}), ensure_ascii=False, indent=2))}</pre>"
            f"<h4>Result</h4><pre>{esc(json.dumps(entry.get('result', {}), ensure_ascii=False, indent=2))}</pre>"
            "</details></td></tr>"
        )
    trace_rows = "".join(trace_row_parts)
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>EDR-WD E2E Report · {esc(report['target'])}</title>
<style>
:root {{ color-scheme: light; font-family: Inter, ui-sans-serif, system-ui, sans-serif; }}
body {{ margin: 0; background: #f5f7fb; color: #172033; }}
main {{ max-width: 1180px; margin: 0 auto; padding: 32px 20px 64px; }}
h1 {{ margin-bottom: 8px; }} h2 {{ margin-top: 34px; }}
.meta {{ color: #526079; line-height: 1.7; }}
.summary {{ display: flex; gap: 12px; flex-wrap: wrap; margin: 24px 0; }}
.metric {{ background: white; border: 1px solid #dfe5ef; border-radius: 10px; padding: 14px 20px; min-width: 110px; }}
.metric b {{ display: block; font-size: 28px; }}
table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 10px; overflow: hidden; }}
th,td {{ padding: 10px 12px; border-bottom: 1px solid #e8ecf3; text-align: left; vertical-align: top; }}
th {{ background: #eef2f8; }} code {{ overflow-wrap: anywhere; }}
.status {{ font-weight: 650; }} .passed {{ color: #137333; }} .failed {{ color: #b3261e; }} .skipped {{ color: #806000; }}
.gallery {{ display: grid; grid-template-columns: repeat(auto-fit,minmax(360px,1fr)); gap: 18px; }}
figure {{ margin: 0; padding: 12px; background: white; border: 1px solid #dfe5ef; border-radius: 10px; }}
img {{ display: block; width: 100%; height: auto; border: 1px solid #e5e9f0; }}
figcaption {{ margin-top: 10px; color: #526079; line-height: 1.5; }}
pre {{ white-space: pre-wrap; word-break: break-word; background: #f6f8fa; padding: 10px; border-radius: 6px; }}
details summary {{ cursor: pointer; color: #2457a6; }}
.trace-shot {{ margin-top: 8px; max-width: 260px; color: #526079; font-size: 12px; }}
.trace-shot img {{ margin-bottom: 5px; border-radius: 4px; }}
</style>
</head>
<body><main>
<h1>EDR-WD E2E Report</h1>
<div class="meta">Target: <code>{esc(report['target'])}</code><br>
Started: <code>{esc(report['started_at'])}</code><br>
Finished: <code>{esc(report['finished_at'])}</code></div>
<section class="summary">
<div class="metric"><b>{summary['passed']}</b>Passed</div>
<div class="metric"><b>{summary['failed']}</b>Failed</div>
<div class="metric"><b>{summary['skipped']}</b>Skipped</div>
</section>
<h2>Evidence lifecycle</h2>
<table><thead><tr><th>Step</th><th>Action</th><th>Before</th><th>After</th><th>Status</th></tr></thead><tbody>{evidence_rows}</tbody></table>
<h2>Screenshots</h2><section class="gallery">{cards}</section>
<h2>Operation trace</h2>
<table><thead><tr><th>#</th><th>Time</th><th>Tool</th><th>Duration (ms)</th><th>Detail</th></tr></thead><tbody>{trace_rows}</tbody></table>
<h2>Test cases</h2>
<table><thead><tr><th>Status</th><th>Test</th><th>Duration (s)</th></tr></thead><tbody>{test_rows}</tbody></table>
</main></body></html>\n"""


def sanitise_trace_value(value: Any, *, depth: int = 0) -> Any:
    """Keep operation traces readable without embedding images or huge trees."""
    if depth > 5:
        return "<max-depth>"
    if isinstance(value, dict):
        output = {}
        for key, item in value.items():
            if key in {"image", "image_b64", "image_base64"}:
                size = len(item) if isinstance(item, str) else "unknown"
                output[key] = f"<omitted base64: {size} chars>"
            elif key in {"controls", "windows"} and isinstance(item, list):
                output[key] = {
                    "count": len(item),
                    "sample": [sanitise_trace_value(v, depth=depth + 1) for v in item[:3]],
                }
            else:
                output[key] = sanitise_trace_value(item, depth=depth + 1)
        return output
    if isinstance(value, list):
        trimmed = [sanitise_trace_value(v, depth=depth + 1) for v in value[:20]]
        if len(value) > 20:
            trimmed.append(f"<omitted {len(value) - 20} items>")
        return trimmed
    if isinstance(value, str) and len(value) > 2000:
        return value[:2000] + f"… <omitted {len(value) - 2000} chars>"
    return value


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
