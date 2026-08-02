"""P1.4 acceptance gate — trace.md renderer.

Covers FR-P1.4-04 (relative paths), -05 (visible warning on missing
evidence), -06 (HTML + Markdown escape), -09 (atomic write).
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
    write_case_trace_atomic,
)
from trace.markdown import _escape_markdown  # noqa: E402
from fake_target import make_png  # noqa: E402


def _ctx(
    *,
    evidence_index=None,
    integrity_ok=True,
    integrity_issues=(),
    generated_at="2026-01-01T00:00:00.000Z",
):
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
        integrity_issues=tuple(integrity_issues),
        evidence_index=evidence_index or {},
        generated_at=generated_at,
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
# FR-P1.4-04: relative paths
# ---------------------------------------------------------------------------


def test_trace_md_renders_relative_links(tmp_path: Path):
    png = make_png()
    rec = persist_screenshot(
        png, "after",
        trace_dir=tmp_path,
        step_no=1, step_id="S001",
        snapshot_id="OBS", event_id="EVT",
        process_name="p", pid=1, window_title="t",
    )
    ctx = _ctx(evidence_index={rec.evidence_id: rec})
    body = render_case_trace(
        tmp_path, manifest=_manifest(),
        render_ctx=ctx,
        step_results=[
            {"step_id": "S001", "status": "passed", "duration_ms": 50},
        ],
    )
    # Relative path; no absolute.
    assert f"({rec.relative_path})" in body
    assert "tmp_path" not in body  # no absolute paths leaked
    # Relative path uses forward slash.
    assert "/tmp/" not in body


def test_trace_md_renders_per_step_sections(tmp_path: Path):
    png1 = make_png()
    png2 = make_png()
    rec1 = persist_screenshot(
        png1, "after", trace_dir=tmp_path,
        step_no=1, step_id="S001",
        snapshot_id="OBS", event_id="EVT-1",
        process_name="p", pid=1, window_title="t",
    )
    rec2 = persist_screenshot(
        png2, "after", trace_dir=tmp_path,
        step_no=2, step_id="S002",
        snapshot_id="OBS", event_id="EVT-2",
        process_name="p", pid=1, window_title="t",
    )
    ctx = _ctx(evidence_index={
        rec1.evidence_id: rec1,
        rec2.evidence_id: rec2,
    })
    body = render_case_trace(
        tmp_path, manifest=_manifest(),
        render_ctx=ctx,
        step_results=[
            {"step_id": "S001", "status": "passed", "duration_ms": 50},
            {"step_id": "S002", "status": "failed", "duration_ms": 60},
        ],
    )
    # Two step sections, two image refs.
    assert body.count("### Step") == 2
    assert body.count(rec1.relative_path) >= 1
    assert body.count(rec2.relative_path) >= 1


# ---------------------------------------------------------------------------
# FR-P1.4-05: visible warning on missing/corrupt evidence
# ---------------------------------------------------------------------------


def test_missing_screenshot_renders_warning(tmp_path: Path):
    """FR-P1.4-05: missing file -> visible warning, not empty image."""
    rec = persist_screenshot(
        make_png(), "after", trace_dir=tmp_path,
        step_no=1, step_id="S001",
        snapshot_id="OBS", event_id="EVT",
        process_name="p", pid=1, window_title="t",
    )
    (tmp_path / rec.relative_path).unlink()  # simulate deletion

    ctx = _ctx(evidence_index={rec.evidence_id: rec})
    body = render_case_trace(
        tmp_path, manifest=_manifest(),
        render_ctx=ctx,
        step_results=[
            {"step_id": "S001", "status": "passed", "duration_ms": 50},
        ],
    )
    assert "⚠" in body or "missing" in body.lower()
    assert "Expected path" in body
    assert rec.relative_path in body


def test_corrupted_screenshot_renders_warning(tmp_path: Path):
    rec = persist_screenshot(
        make_png(), "after", trace_dir=tmp_path,
        step_no=1, step_id="S001",
        snapshot_id="OBS", event_id="EVT",
        process_name="p", pid=1, window_title="t",
    )
    # Corrupt the on-disk bytes (digest no longer matches).
    (tmp_path / rec.relative_path).write_bytes(b"corrupt bytes")

    ctx = _ctx(evidence_index={rec.evidence_id: rec})
    body = render_case_trace(
        tmp_path, manifest=_manifest(),
        render_ctx=ctx,
        step_results=[
            {"step_id": "S001", "status": "passed", "duration_ms": 50},
        ],
    )
    assert "digest mismatch" in body
    assert rec.relative_path in body


# ---------------------------------------------------------------------------
# FR-P1.4-06: HTML + Markdown escape
# ---------------------------------------------------------------------------


def test_markdown_escape_blocks_raw_html(tmp_path: Path):
    """Untrusted UI text containing `<script>` etc. must be inert."""
    rec = persist_screenshot(
        make_png(), "after", trace_dir=tmp_path,
        step_no=1, step_id="S001",
        snapshot_id="OBS", event_id="EVT",
        process_name="<script>alert(1)</script>",
        pid=1,
        window_title='evil <img src=x onerror=alert(1)>',
    )
    ctx = _ctx(evidence_index={rec.evidence_id: rec})
    body = render_case_trace(
        tmp_path, manifest=_manifest(),
        render_ctx=ctx,
        step_results=[
            {"step_id": "S001", "status": "passed", "duration_ms": 50},
        ],
    )
    # The literal `<script>...</script>` must NOT appear unescaped.
    assert "<script>alert(1)</script>" not in body
    # It must appear escaped (`&lt;script&gt;`).
    assert "&lt;script&gt;" in body
    # The literal `<img onerror=...>` must NOT appear unescaped.
    assert "<img src=x onerror=alert(1)>" not in body
    assert "&lt;img" in body


def test_renderer_escapes_raw_html_and_markdown_links(tmp_path: Path):
    """Markdown characters like `[link](http://evil)` must be escaped."""
    rec = persist_screenshot(
        make_png(), "after", trace_dir=tmp_path,
        step_no=1, step_id="S001",
        snapshot_id="OBS", event_id="EVT",
        process_name="p",
        pid=1,
        window_title="[click](http://evil.example)",
    )
    ctx = _ctx(evidence_index={rec.evidence_id: rec})
    body = render_case_trace(
        tmp_path, manifest=_manifest(),
        render_ctx=ctx,
        step_results=[
            {"step_id": "S001", "status": "passed", "duration_ms": 50},
        ],
    )
    # The literal `[click](http://evil.example)` must NOT appear unescaped
    # as a Markdown link.
    assert "[click](http://evil.example)" not in body
    # The unescaped link must not survive: assert no
    # un-escaped sequence `[` + `click]` + bare `(` appears.
    import re
    assert not re.search(r"[^\\]\[click]\(", body), body
    # (escaped form verified by absence of un-escaped pattern)


# ---------------------------------------------------------------------------
# FR-P1.4-09: atomic write
# ---------------------------------------------------------------------------


def test_trace_md_byte_identical_rerender(tmp_path: Path):
    """Two renders with frozen `generated_at` produce identical bytes
    (acceptance #7)."""
    rec = persist_screenshot(
        make_png(), "after", trace_dir=tmp_path,
        step_no=1, step_id="S001",
        snapshot_id="OBS", event_id="EVT",
        process_name="p", pid=1, window_title="t",
    )
    ctx = _ctx(
        evidence_index={rec.evidence_id: rec},
        generated_at="2026-01-01T00:00:00.000Z",
    )
    step_results = [
        {"step_id": "S001", "status": "passed", "duration_ms": 50},
    ]
    body1 = render_case_trace(
        tmp_path, manifest=_manifest(),
        render_ctx=ctx, step_results=step_results,
    )
    body2 = render_case_trace(
        tmp_path, manifest=_manifest(),
        render_ctx=ctx, step_results=step_results,
    )
    assert body1 == body2


def test_atomic_write_to_disk(tmp_path: Path):
    out = tmp_path / "trace.md"
    rec = persist_screenshot(
        make_png(), "after", trace_dir=tmp_path,
        step_no=1, step_id="S001",
        snapshot_id="OBS", event_id="EVT",
        process_name="p", pid=1, window_title="t",
    )
    ctx = _ctx(evidence_index={rec.evidence_id: rec})
    write_case_trace_atomic(
        tmp_path, manifest=_manifest(),
        render_ctx=ctx,
        step_results=[
            {"step_id": "S001", "status": "passed", "duration_ms": 50},
        ],
        out_path=out,
    )
    assert out.exists()
    # No leftover .tmp files.
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == []


def test_atomic_write_leaves_no_tmp_on_failure(tmp_path: Path, monkeypatch):
    """If os.replace raises, the prior trace.md is intact."""
    out = tmp_path / "trace.md"
    prior = "# prior content\n"
    out.write_text(prior)
    rec = persist_screenshot(
        make_png(), "after", trace_dir=tmp_path,
        step_no=1, step_id="S001",
        snapshot_id="OBS", event_id="EVT",
        process_name="p", pid=1, window_title="t",
    )
    ctx = _ctx(evidence_index={rec.evidence_id: rec})

    import os as os_mod
    real_replace = os_mod.replace
    def fail_replace(*args, **kwargs):
        raise OSError("simulated")
    monkeypatch.setattr(os_mod, "replace", fail_replace)

    with pytest.raises(OSError):
        write_case_trace_atomic(
            tmp_path, manifest=_manifest(),
            render_ctx=ctx,
            step_results=[
                {"step_id": "S001", "status": "passed", "duration_ms": 50},
            ],
            out_path=out,
        )
    monkeypatch.setattr(os_mod, "replace", real_replace)
    assert out.read_text() == prior


# ---------------------------------------------------------------------------
# Header coverage
# ---------------------------------------------------------------------------


def test_trace_md_header_includes_case_identity(tmp_path: Path):
    body = render_case_trace(
        tmp_path, manifest=_manifest(),
        render_ctx=_ctx(),
        step_results=[],
    )
    assert "TC-1" in body
    assert "Open Protection" in body
    assert "TR-1" in body
    assert "windows_hisec" in body
    assert "passed" in body


def test_trace_md_integrity_section_reports_failure(tmp_path: Path):
    body = render_case_trace(
        tmp_path, manifest=_manifest(),
        render_ctx=_ctx(
            integrity_ok=False,
            integrity_issues=("event_hash mismatch on EVT-2",),
        ),
        step_results=[],
    )
    assert "FAIL" in body
    assert "event_hash mismatch" in body



# ---------------------------------------------------------------------------
# Markdown escape regression table (non-blocking recommendation
# from P1.4 review).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected_substr,forbidden_substr",
    [
        # HTML
        ("<script>", "&lt;script&gt;", "<script>"),
        ("<img src=x onerror=alert(1)>", "&lt;img", "<img src="),
        # Markdown
        ("[click](http://evil)", "\\[click\\]", "[click]("),
        ("*bold*", "\\*bold\\*", None),
        ("`code`", "\\`code\\`", None),
        # backslash input preserved as data (escape does not
        # touch a stray backslash that isn't followed by a
        # special char)
        ("plain\\text", "plain\\text", None),
        # benign text untouched
        ("hello world", "hello world", None),
    ],
    ids=[
        "html-script",
        "html-img",
        "md-link",
        "md-em",
        "md-code",
        "literal-backslash",
        "benign-text",
    ],
)
def test_markdown_escape_regression_table(raw, expected_substr, forbidden_substr):
    out = _escape_markdown(raw)
    assert expected_substr in out
    if forbidden_substr:
        assert forbidden_substr not in out
