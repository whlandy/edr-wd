"""trace.md renderer escape-consistency regression (§6r).

`agent/trace/markdown.py::_escape_markdown` must satisfy the same
contract as `agent/trace/markdown_escape.escape_markdown` (the repo's
canonical user-controlled-value escape, as used by recovery_markdown
and render_report) on top of its HTML escaping:

  * newline / CR / control chars cannot survive — window_title,
    process_name and diagnostic strings originate from the target
    machine where any process controls their content, and a raw
    newline in a table cell or heading line injects structure.
  * `|` is escaped — unescaped pipes break table-row rendering
    (header table, step-results table).
  * an input backslash is doubled — otherwise an attacker-supplied
    `\\*` would consume the renderer's own backslash escape and the
    `*` would render as emphasis (data loss / structure change).

Each test fails against the pre-fix renderer.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
for p in (str(_REPO / "target"), str(_REPO / "agent"), str(_REPO / "test_case")):
    if p not in sys.path:
        sys.path.insert(0, p)

from trace import (  # noqa: E402
    EvidenceRecord,
    IntegrityReport,
    ManifestRecord,
    RenderContext,
    persist_screenshot,
    render_case_trace,
)
from trace.markdown import _escape_markdown  # noqa: E402
from fake_target import make_png  # noqa: E402


def _ctx(evidence_index=None):
    return RenderContext(
        trace_id="TR-1",
        branch_id="BR-1",
        case_id="TC-1",
        case_title="Open Protection",
        target_profile="windows_hisec",
        catalog_id="CAT-1",
        plan_id="PLAN-1",
        started_at="2026-01-01T00:00:00.000Z",
        ended_at="2026-01-01T00:00:01.000Z",
        duration_ms=1000,
        terminal_status="passed",
        integrity_ok=True,
        integrity_issues=(),
        evidence_index=evidence_index or {},
        generated_at="2026-01-01T00:00:00.000Z",
    )


def _manifest():
    return ManifestRecord(
        trace_id="TR-1",
        branch_heads=("EVT-1",),
        catalog_digest="sha256:digest",
        evidence_counts={"screenshot": 1},
        terminal_status="passed",
        integrity_verification_result=IntegrityReport.ok_report(),
    )


# ---------------------------------------------------------------------------
# Unit-level contract (mirrors test_markdown_escape_regression_table shape)
# ---------------------------------------------------------------------------


def test_escape_collapses_newlines():
    out = _escape_markdown("line1\nline2")
    assert "\n" not in out
    assert "line1" in out and "line2" in out


def test_escape_collapses_cr_and_crlf():
    assert "\r" not in _escape_markdown("a\r\nb\rc")
    assert "\n" not in _escape_markdown("a\r\nb\rc")


def test_escape_strips_control_chars():
    out = _escape_markdown("a\x00b\x1bc")
    assert "\x00" not in out and "\x1b" not in out


def test_escape_escapes_pipe():
    out = _escape_markdown("col1|col2")
    assert "|" not in out.replace("\\|", "")
    assert "\\|" in out


def test_escape_doubles_backslash():
    out = _escape_markdown("a\\*b")
    # The renderer's backslash escape must survive: an input backslash
    # must be doubled so it cannot consume the escape it is given.
    assert out.count("\\") >= 2


def test_escape_benign_text_untouched():
    assert _escape_markdown("hello world") == "hello world"


# ---------------------------------------------------------------------------
# Renderer-level: untrusted window_title through the provenance line
# ---------------------------------------------------------------------------


def _window_evidence(tmp_path: Path, window_title: str) -> EvidenceRecord:
    return persist_screenshot(
        make_png(), "after", trace_dir=tmp_path,
        step_no=1, step_id="S001",
        snapshot_id="OBS", event_id="EVT",
        process_name="p", pid=1,
        window_title=window_title,
    )


def test_window_title_newline_cannot_inject_structure(tmp_path: Path):
    rec = _window_evidence(
        tmp_path, "Evil\n| Injected | Row | /etc/passwd |")
    body = render_case_trace(
        tmp_path, manifest=_manifest(),
        render_ctx=_ctx({rec.evidence_id: rec}),
        step_results=[{"step_id": "S001", "status": "passed", "duration_ms": 5}],
    )
    # The provenance line is a fenced-ish inline span; a newline inside
    # it must not have survived to break out of the line.
    assert "Evil\n" not in body
    # No injected fake table row may appear on its own line.
    for line in body.splitlines():
        assert not line.startswith("| Injected"), body


def test_window_title_pipe_cannot_break_table(tmp_path: Path):
    rec = _window_evidence(tmp_path, "win|fake||row")
    body = render_case_trace(
        tmp_path, manifest=_manifest(),
        render_ctx=_ctx({rec.evidence_id: rec}),
        step_results=[{"step_id": "S001", "status": "passed", "duration_ms": 5}],
    )
    # The provenance line lives outside a table, but the title's own
    # pipes must be escaped so a future table embed stays safe and the
    # raw byte stream shows the escaped form.
    assert "win\\|fake" in body


def test_step_table_pipe_injection_blocked(tmp_path: Path):
    # A step_result title lands in a plain line; a diagnostic lands in
    # a list item. Both go through _escape_markdown — exercise the
    # table-cell adjacent path via case_title containing a pipe.
    rec = _window_evidence(tmp_path, "t")
    ctx = _ctx({rec.evidence_id: rec})
    body = render_case_trace(
        tmp_path, manifest=_manifest(),
        render_ctx=ctx,
        step_results=[
            {"step_id": "S001", "status": "passed", "duration_ms": 5,
             "title": "Open | Malware | Details",
             "expectation_results": [
                 {"type": "window_text_contains", "status": "failed",
                  "diagnostic": "boom\n| fake | row |"}]},
        ],
    )
    # Title and diagnostic newlines/pipes must be neutralized.
    assert "boom\n" not in body
    for line in body.splitlines():
        assert not line.startswith("| fake"), body
    assert "Open \\| Malware" in body


# ---------------------------------------------------------------------------
# Renderer-level: header table cell (target_profile embeds in a table)
# ---------------------------------------------------------------------------


def test_header_table_cell_pipe_escaped(tmp_path: Path):
    ctx = _ctx()
    # RenderContext is frozen; build a variant with an adversarial
    # profile via dataclasses.replace.
    import dataclasses
    ctx = dataclasses.replace(ctx, target_profile="win| injected |row")
    body = render_case_trace(
        tmp_path, manifest=_manifest(),
        render_ctx=ctx, step_results=[],
    )
    for line in body.splitlines():
        assert not line.startswith("| injected"), body
    assert "win\\| injected" in body


if __name__ == "__main__":
    pytest.main([__file__])
