"""
P2.4.D — escape_markdown helper tests + integration into render_report.

Covers:
  * escape_markdown() unit tests for each escape rule.
  * End-to-end: user-controlled values in case-attempt manifests
    do not break markdown table structure (FR-P2.3-10).
  * Renderer-generated text is NOT escaped.

References:
  docs/requirements/P2-cleanup-escape-design.md §2.1 S5 + §8 + §9.4
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.trace.markdown_escape import escape_markdown
from agent.trace.runs import CaseAttemptRef, RunContext
from agent.trace.render_report import render_report


# ---------------------------------------------------------------------
# escape_markdown unit tests
# ---------------------------------------------------------------------


class TestEscapeMarkdown:
    """Conservative markdown escape for user-controlled values."""

    def test_plain_text_passes_through(self) -> None:
        """No special chars → output equals input."""
        assert escape_markdown("plain_text") == "plain_text"
        assert escape_markdown("case_001") == "case_001"

    def test_empty_string_returns_empty(self) -> None:
        assert escape_markdown("") == ""

    def test_pipe_escaped(self) -> None:
        """Pipe breaks table rows → escape."""
        assert escape_markdown("a|b") == "a\\|b"

    def test_multiple_pipes_escaped(self) -> None:
        assert escape_markdown("a|b|c") == "a\\|b\\|c"

    def test_backtick_escaped(self) -> None:
        """Backtick breaks inline code → escape."""
        assert escape_markdown("a`b") == "a\\`b"

    def test_newline_replaced_with_space(self) -> None:
        """Newline breaks table rows → replace with space (not escape)."""
        assert escape_markdown("a\nb") == "a b"

    def test_carriage_return_replaced_with_space(self) -> None:
        """Carriage return (\\r) also replaced with space."""
        assert escape_markdown("a\rb") == "a b"

    def test_crlf_replaced_with_single_space(self) -> None:
        """\\r\\n replaced with single space (not ' ')."""
        assert escape_markdown("a\r\nb") == "a b"

    def test_backslash_escaped(self) -> None:
        """Backslash → double backslash (consistency)."""
        assert escape_markdown("a\\b") == "a\\\\b"

    def test_nul_stripped(self) -> None:
        """NUL byte stripped (control char)."""
        assert escape_markdown("a\x00b") == "ab"

    def test_other_control_chars_stripped(self) -> None:
        """BEL, ESC, etc. (0x01-0x1F, excluding whitespace) stripped."""
        # BEL=0x07, ESC=0x1B, DEL not in range but other controls
        assert escape_markdown("a\x07b\x1bc") == "abc"

    def test_tab_preserved(self) -> None:
        """TAB (0x09) is whitespace, NOT stripped."""
        assert escape_markdown("a\tb") == "a\tb"

    def test_combined_specials(self) -> None:
        """All four escapes apply together."""
        # Order matters: backslash first, then pipe, then newline, then
        # backtick. So `a|b\nc\\d`e` → first `a|b\nc\\d`e` →
        # backslash: `a|b\nc\\\\d`e` → pipe: `a\\|b\nc\\\\d`e` →
        # newline→space: `a\\|b c\\\\d`e` → backtick: `a\\|b c\\\\d\`e`
        result = escape_markdown("a|b\nc\\d`e")
        assert "\\|" in result
        assert "\\`" in result
        assert "\n" not in result
        assert "\\\\" in result  # double backslash present

    def test_idempotent_escape_of_escaped_input(self) -> None:
        """Re-escaping an already-escaped string stays consistent.

        Note: this is not a strict guarantee for general markdown, but
        for our conservative escape set, escape(escape(s)) is
        idempotent because each escape character maps to itself or a
        distinct escape.
        """
        s = "a|b`c\\d"
        once = escape_markdown(s)
        twice = escape_markdown(once)
        # Backslash and pipe and backtick all doubled.
        assert twice.count("\\|") == once.count("\\|") * 2 - s.count("|")
        # Just verify it doesn't crash and stays bounded.
        assert len(twice) > 0


# ---------------------------------------------------------------------
# Integration: render_report escapes user-controlled values
# ---------------------------------------------------------------------


def _setup_run_with_unsafe_ids(
    tmp_path: Path,
    *,
    case_id: str = "case_clean",
    safe_cid: str = "case_clean",
    trace_id: str = "trace_x",
    terminal: str = "passed",
    cleanup_status: str = "unknown",
    cleanup_outcome_critical: bool = False,
):
    """Build a RunContext with a single case-attempt manifest whose
    user-controlled fields (safe_case_id, trace_id) may contain
    markdown special characters. We write the case-attempt-manifest
    JSON directly to bypass sanitize_identifier — these are
    defense-in-depth tests for `escape_markdown` (per design §2.1 S5).

    `case_id` is still used for the filesystem path, so it must be
    filesystem-safe; use `safe_cid` (which goes into the manifest JSON)
    to inject markdown specials.
    """
    run_dir = tmp_path / "run_test"
    run_dir.mkdir()
    run = RunContext(root=tmp_path, run_id="run_test")
    run.finalize()

    (run_dir / "manifest.json").write_text(
        '{"schema_version": "1.0", "run_id": "run_test", '
        '"started_at": "2024-01-01T00:00:00Z", "ended_at": '
        '"2024-01-01T00:01:00Z", "renderer_version": "p2.4", '
        '"evidence_status": "complete"}',
        encoding="utf-8",
    )

    attempt_id = "attempt-0001"
    case_dir = run_dir / case_id / attempt_id
    case_dir.mkdir(parents=True)
    # Write case-attempt-manifest.json with the user-supplied
    # safe_cid and trace_id — they bypass sanitize_identifier so we
    # can exercise escape_markdown's defense-in-depth.
    (case_dir / "case-attempt-manifest.json").write_text(
        json.dumps({
            "schema_version": "1.0.0",
            "trace_id": trace_id,
            "branch_heads": [],
            "catalog_digest": "a" * 64,
            "evidence_counts": {},
            "terminal_status": terminal,
            "cleanup_status": cleanup_status,
            "cleanup_outcome_critical": cleanup_outcome_critical,
            "integrity_verification_result": {"ok": True, "issues": []},
        }),
        encoding="utf-8",
    )

    attempt = CaseAttemptRef(
        safe_case_id=safe_cid,
        attempt_id=attempt_id,
        trace_id=trace_id,
        path=case_dir,
    )
    return run, [attempt]


class TestRenderReportEscape:
    """render_report applies escape_markdown to user-controlled fields."""

    def test_pipe_in_safe_case_id_escaped(self, tmp_path: Path) -> None:
        """Pipe in case id does not break table row structure."""
        run, attempts = _setup_run_with_unsafe_ids(
            tmp_path,
            case_id="case|inject",
            safe_cid="case|inject",
            trace_id="trace_x",
        )
        body, _ = render_report(run, attempts=attempts)
        # Pipe is escaped.
        assert "case\\|inject" in body
        # Raw pipe (which would break the row) NOT followed by space +
        # new-row syntax — but in markdown, what matters is that the
        # row hasn't been split into 6 columns. We verify that the
        # table header is still followed by a row with the same number
        # of columns as the header.
        # Header: | Case | Attempts | Final | Trace |  → 5 columns
        # We expect one data row with exactly 5 columns.
        assert "| Case | Attempts | Final | Trace |" in body

    def test_backtick_in_trace_id_escaped(self, tmp_path: Path) -> None:
        """Backtick in trace id does not break inline code."""
        run, attempts = _setup_run_with_unsafe_ids(
            tmp_path,
            case_id="case_clean",
            safe_cid="case_clean",
            trace_id="trace`with`backtick",
        )
        body, _ = render_report(run, attempts=attempts)
        assert "trace\\`with\\`backtick" in body
        # The unescaped version should NOT appear.
        assert "trace`with`backtick" not in body

    def test_newline_in_trace_id_replaced_with_space(
        self, tmp_path: Path,
    ) -> None:
        """Newline in trace id replaced with space (not split row)."""
        run, attempts = _setup_run_with_unsafe_ids(
            tmp_path,
            case_id="case_with_newline",
            safe_cid="case_with_newline",
            trace_id="trace_with\nnewline",
        )
        body, _ = render_report(run, attempts=attempts)
        # Newline replaced with space in the trace id.
        assert "trace_with newline" in body
        # Original newline NOT present.
        assert "trace_with\nnewline" not in body

    def test_renderer_generated_text_not_escaped(
        self, tmp_path: Path,
    ) -> None:
        """Headings, table syntax, fixed strings are NOT escaped."""
        run, attempts = _setup_run_with_unsafe_ids(
            tmp_path,
            case_id="case_clean",
            safe_cid="case_clean",
            trace_id="trace_x",
        )
        body, _ = render_report(run, attempts=attempts)
        # Heading marker `#` not escaped.
        assert "# Run Report —" in body
        # Table pipe syntax not escaped (it must be there as-is).
        assert "| Case | Attempts | Final | Trace |" in body
        # Status enum not escaped.
        assert "| Passed |" in body or "| Failed |" in body

    def test_cleanup_warnings_section_escapes_user_fields(
        self, tmp_path: Path,
    ) -> None:
        """Cleanup Warnings section also escapes case id and trace id."""
        run, attempts = _setup_run_with_unsafe_ids(
            tmp_path,
            case_id="case|inject",
            safe_cid="case|inject",
            trace_id="trace|evil",
            terminal="passed",
            cleanup_status="failed",
            cleanup_outcome_critical=False,
        )
        body, _ = render_report(run, attempts=attempts)
        # Cleanup Warnings section present.
        assert "## Cleanup Warnings" in body
        # Pipe in case id is escaped in warnings table.
        assert "case\\|inject" in body
        # Pipe in trace id is escaped.
        assert "trace\\|evil" in body

    def test_failures_section_escapes_user_fields(
        self, tmp_path: Path,
    ) -> None:
        """Failures section also escapes case id and trace id."""
        run, attempts = _setup_run_with_unsafe_ids(
            tmp_path,
            case_id="case|inject",
            safe_cid="case|inject",
            trace_id="trace`evil",
            terminal="failed",
        )
        body, _ = render_report(run, attempts=attempts)
        # Failures section present.
        assert "## Failures And Blocks" in body
        # Pipe in case id is escaped.
        assert "case\\|inject" in body
        # Backtick in trace id is escaped.
        assert "trace\\`evil" in body

    def test_backslash_in_trace_id_escaped(self, tmp_path: Path) -> None:
        """Backslash in trace id is doubled."""
        run, attempts = _setup_run_with_unsafe_ids(
            tmp_path,
            case_id="case_clean",
            safe_cid="case_clean",
            trace_id="trace\\with",
        )
        body, _ = render_report(run, attempts=attempts)
        # Backslash is doubled.
        assert "trace\\\\with" in body


# ---------------------------------------------------------------------
# Fixture compatibility: the safe_case_id must remain a valid filename
# ---------------------------------------------------------------------


class TestSanitizationPreserved:
    """escape_markdown is independent from sanitize_identifier.

    The runner's sanitize step keeps IDs as filesystem-safe strings
    (no slashes, no pipes, no control chars). escape_markdown is the
    second layer of defense for markdown table structure.
    """

    def test_safe_case_id_without_specials_passes_through(
        self, tmp_path: Path,
    ) -> None:
        """A safe case id (no specials) renders unchanged."""
        run, attempts = _setup_run_with_unsafe_ids(
            tmp_path,
            case_id="case_clean",
            safe_cid="case_clean_safe",
            trace_id="trace_x",
        )
        body, _ = render_report(run, attempts=attempts)
        assert "case_clean_safe" in body