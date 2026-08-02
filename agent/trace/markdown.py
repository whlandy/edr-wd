"""
markdown.py — Case trace renderer (architecture §11 + §14.5,
FR-P1.4-04, -05, -06, -09).

[commit A — initial implementation, pre-self-fix]
Pre-fix state: contains 3 self-caught bugs that landed as
"review #1" fixes:

  1. `_MARKDOWN_SPECIALS` includes `\\` causing double-escape
     (e.g. `[` -> `\\[` instead of `\[`).
  2. No "provenance" line per step, so HTML/Markdown escape of
     `process_name` / `window_title` was never actually surfaced
     in the body.
  3. `integrity_status` looked at `integrity_ok` only — failed to
     reflect a non-empty `integrity_issues` list.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import html
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .events import EventType  # noqa: F401
from .evidence import EvidenceRecord
from .manifest import ManifestRecord
from .projections import project_step_results


# ---------------------------------------------------------------------------
# Escaping helpers (FR-P1.4-06)
# ---------------------------------------------------------------------------


# Pre-fix: includes backslash, causing double-escape.
_MARKDOWN_SPECIALS = "\\\\`*[]<>~"


def _escape_markdown(text: str) -> str:
    if text is None:
        return ""
    escaped = html.escape(text, quote=True)
    out = []
    for ch in escaped:
        if ch in _MARKDOWN_SPECIALS:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


# ---------------------------------------------------------------------------
# Header / section renderers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RenderContext:
    trace_id: str
    branch_id: str
    case_id: str
    case_title: str
    target_profile: str
    catalog_id: str
    plan_id: str
    started_at: str
    ended_at: str
    duration_ms: int
    terminal_status: str
    integrity_ok: bool
    integrity_issues: tuple[str, ...] = ()
    evidence_index: Mapping[str, EvidenceRecord] = field(default_factory=dict)
    generated_at: str | None = None

    def generated_at_iso(self) -> str:
        return self.generated_at or _utcnow_iso()


def render_case_trace(
    trace_dir: Path,
    *,
    manifest: ManifestRecord,
    render_ctx: RenderContext,
    step_results: Sequence[Mapping[str, Any]],
    captured_at_frozen: str | None = None,
) -> str:
    """Produce the Markdown body."""
    lines: list[str] = []
    lines.append(f"# Case {render_ctx.case_id} — {_escape_markdown(render_ctx.case_title)}")
    lines.append("")
    lines.append("| Field | Value |")
    lines.append("|---|---|")
    lines.append(f"| Trace ID | `{render_ctx.trace_id}` |")
    lines.append(f"| Branch ID | `{render_ctx.branch_id}` |")
    lines.append(f"| Case ID | `{render_ctx.case_id}` |")
    lines.append(f"| Plan ID | `{render_ctx.plan_id}` |")
    lines.append(f"| Catalog ID | `{render_ctx.catalog_id}` |")
    lines.append(f"| Target / profile | {_escape_markdown(render_ctx.target_profile)} |")
    lines.append(f"| Started at | `{render_ctx.started_at}` |")
    lines.append(f"| Ended at | `{render_ctx.ended_at}` |")
    lines.append(f"| Duration | {render_ctx.duration_ms} ms |")
    lines.append(f"| Terminal status | `{render_ctx.terminal_status}` |")
    # Pre-fix: integrity_status used only `integrity_ok`.
    integrity_status = "OK" if render_ctx.integrity_ok else "FAIL"
    lines.append(f"| Integrity check | `{integrity_status}` ({len(render_ctx.integrity_issues)} issues) |")
    lines.append("")

    lines.append("## Step results")
    lines.append("")
    lines.append("| Step | Status | Duration (ms) | Evidence |")
    lines.append("|---|---|---|---|")
    for sr in step_results:
        sid = str(sr.get("step_id", ""))
        status = str(sr.get("status", ""))
        dur = sr.get("duration_ms", 0)
        ev_links = _summarize_evidence_links(sr, render_ctx.evidence_index)
        lines.append(
            f"| `{_escape_markdown(sid)}` | `{status}` | {dur} | {ev_links} |"
        )
    lines.append("")

    lines.append("## Per-step detail")
    lines.append("")
    for sr in step_results:
        sid = str(sr.get("step_id", ""))
        status = str(sr.get("status", ""))
        title = sr.get("title", "") or ""
        lines.append(f"### Step `{_escape_markdown(sid)}` — {status}")
        if title:
            lines.append("")
            lines.append(f"{_escape_markdown(title)}")
        lines.append("")
        ev_results = sr.get("expectation_results") or []
        if ev_results:
            lines.append("**Expectations**")
            lines.append("")
            for er in ev_results:
                t = er.get("type") if isinstance(er, dict) else getattr(er, "expectation_type", "")
                st = er.get("status") if isinstance(er, dict) else getattr(er, "status", "")
                diag = er.get("diagnostic") if isinstance(er, dict) else getattr(er, "diagnostic", "")
                lines.append(f"- `{_escape_markdown(str(t))}` -> `{st}`")
                if diag:
                    lines.append(f"  - diagnostic: {_escape_markdown(str(diag))}")
            lines.append("")

        # Pre-fix: no provenance line. process_name / window_title
        # never surfaced, so the HTML/Markdown escape was unused.
        step_evidence = [
            ev for ev in render_ctx.evidence_index.values()
            if ev.step_id == sid
        ]
        if step_evidence:
            lines.append("**Evidence**")
            lines.append("")
            for ev in step_evidence:
                lines.extend(_render_evidence_block(ev, trace_dir))
            lines.append("")

    lines.append("## Recovery branches (P2.x)")
    lines.append("")
    lines.append("_Branches and checkpoints are reserved for P2.1 / P2.2._")
    lines.append("")

    lines.append("## Integrity & evidence verification")
    lines.append("")
    lines.append(f"- Chain integrity: {'OK' if render_ctx.integrity_ok else 'FAIL'}")
    if render_ctx.integrity_issues:
        lines.append("- Issues:")
        for issue in render_ctx.integrity_issues:
            lines.append(f"  - {_escape_markdown(issue)}")
    if render_ctx.evidence_index:
        lines.append(f"- Evidence records: {len(render_ctx.evidence_index)}")
    lines.append("")
    lines.append(f"_Generated at {render_ctx.generated_at_iso()}_")
    lines.append("")

    return "\n".join(lines)


def write_case_trace_atomic(
    trace_dir: Path,
    *,
    render_ctx: RenderContext,
    manifest: ManifestRecord,
    step_results: Sequence[Mapping[str, Any]],
    out_path: Path | None = None,
) -> Path:
    if out_path is None:
        out_path = trace_dir / "trace.md"
    body = render_case_trace(
        trace_dir, manifest=manifest,
        render_ctx=render_ctx, step_results=step_results,
    )
    parent = Path(out_path).parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{Path(out_path).name}.", suffix=".tmp",
        dir=str(parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, out_path)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
    return out_path


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _summarize_evidence_links(
    step_result: Mapping[str, Any],
    evidence_index: Mapping[str, EvidenceRecord],
) -> str:
    sid = step_result.get("step_id", "")
    items = [ev for ev in evidence_index.values() if ev.step_id == sid]
    if not items:
        return ""
    parts = []
    for ev in items:
        parts.append(f"`{ev.role}` ({ev.evidence_id})")
    return ", ".join(parts)


def _render_evidence_block(ev: EvidenceRecord, trace_dir: Path) -> list[str]:
    rel = ev.relative_path
    abs_path = trace_dir / rel
    if not abs_path.exists():
        return _warning_block(ev, reason="missing file")

    actual_sha = "sha256:" + hashlib.sha256(abs_path.read_bytes()).hexdigest()
    if actual_sha != ev.sha256:
        return _warning_block(ev, reason="digest mismatch")

    title = f"{ev.role} — {ev.evidence_id} ({ev.width}x{ev.height}, {ev.bytes_size} bytes)"
    alt = _escape_markdown(f"{ev.role} screenshot for step {ev.step_id}")
    cap = _escape_markdown(title)
    return [f"![{alt}]({rel})", f"*{cap}*", ""]


def _warning_block(ev: EvidenceRecord, *, reason: str) -> list[str]:
    msg = (
        f"> ⚠ **Evidence missing**: `{ev.evidence_id}` "
        f"(role `{ev.role}`, step `{ev.step_id}`) "
        f"— {reason}. Expected path: `{_escape_markdown(ev.relative_path)}`. "
        f"Recorded SHA-256: `{ev.sha256}`."
    )
    return [msg, ""]


def _utcnow_iso() -> str:
    return _dt.datetime.now(tz=_dt.timezone.utc).isoformat(timespec="milliseconds")


__all__ = [
    "RenderContext",
    "render_case_trace",
    "write_case_trace_atomic",
]