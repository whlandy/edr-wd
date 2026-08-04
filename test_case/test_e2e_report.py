import base64
import json

import pytest

from agent.e2e_report import (
    E2EEvidenceLifecycle,
    create_run_dir,
    persist_target_screenshot,
    sanitise_trace_value,
    write_report,
)


PNG_1X1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M/wHwAF/gL+3MxZ5wAAAABJRU5ErkJggg=="
)


def test_persist_target_screenshot_writes_agent_copy(tmp_path):
    run_dir = create_run_dir("win/dev", root=tmp_path)
    assert run_dir.parent == tmp_path
    assert "win-dev" not in run_dir.name
    metadata = persist_target_screenshot(
        {"image_b64": base64.b64encode(PNG_1X1).decode(), "width": 1, "height": 1},
        run_dir,
        "after shot",
    )
    assert (run_dir / metadata["path"]).read_bytes() == PNG_1X1
    assert metadata["path"] == "screenshots/after-shot.png"
    assert metadata["sha256"].startswith("sha256:")


def test_persist_target_screenshot_rejects_missing_payload(tmp_path):
    run_dir = create_run_dir("win-dev", root=tmp_path)
    with pytest.raises(ValueError, match="no base64"):
        persist_target_screenshot({"ok": True, "path": "C:\\remote.png"}, run_dir, "after")


def test_write_report_creates_json_and_markdown(tmp_path):
    run_dir = create_run_dir("win-dev", root=tmp_path)
    report = {
        "target": "win-dev",
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:00:01+00:00",
        "summary": {"passed": 1, "failed": 0, "skipped": 0},
        "tests": [{"nodeid": "x::test_y", "outcome": "passed", "duration_seconds": 0.1}],
        "screenshots": [{
            "path": "screenshots/after.png",
            "sha256": "sha256:abc",
            "evidence_id": "IMG-test",
            "operation_sequence": 1,
        }],
        "operation_trace": [{
            "sequence": 1,
            "started_at": "2026-01-01T00:00:00+00:00",
            "duration_ms": 12,
            "tool": "click",
            "arguments": {"control_id": 7},
            "result": {"ok": True},
        }],
    }
    write_report(run_dir, report)
    assert json.loads((run_dir / "report.json").read_text())["summary"]["passed"] == 1
    markdown = (run_dir / "report.md").read_text()
    assert "1 passed, 0 failed, 0 skipped" in markdown
    assert "screenshots/after.png" in markdown
    html_report = (run_dir / "report.html").read_text()
    assert "EDR-WD E2E Report" in html_report
    assert '<img loading=\'lazy\' src=\'screenshots/after.png\'' in html_report
    assert "1</b>Passed" in html_report
    assert "Operation trace" in html_report
    assert "control_id" in html_report
    assert "Captured here" in html_report
    assert "Trace #1 screenshot" in html_report


def test_sanitise_trace_value_omits_images_and_summarises_large_collections():
    value = sanitise_trace_value({
        "ok": True,
        "image_b64": "A" * 1000,
        "controls": [{"control_id": i} for i in range(10)],
    })
    assert value["image_b64"] == "<omitted base64: 1000 chars>"
    assert value["controls"]["count"] == 10
    assert len(value["controls"]["sample"]) == 3


def test_evidence_lifecycle_reuses_previous_after_as_next_before(tmp_path):
    class FakeClient:
        def __init__(self):
            self.calls = []

        def call_tool(self, tool, arguments):
            self.calls.append((tool, arguments))
            if tool == "screenshot":
                return {
                    "ok": True,
                    "image_b64": base64.b64encode(PNG_1X1).decode(),
                    "width": 1,
                    "height": 1,
                }
            return {"ok": True}

    run_dir = create_run_dir("win-dev", root=tmp_path)
    screenshots = []
    steps = []
    client = FakeClient()
    lifecycle = E2EEvidenceLifecycle(
        run_dir,
        screenshot_sink=screenshots.append,
        step_sink=steps.append,
    )

    baseline = lifecycle.initialize(client, process_name="Initial.exe")
    lifecycle.run_action(
        client,
        step_id="001-click",
        tool="click",
        arguments={"control_id": 1},
        before_process_name="Initial.exe",
        after_process_name="Next.exe",
    )
    lifecycle.run_action(
        client,
        step_id="002-click",
        tool="click",
        arguments={"control_id": 2},
        before_process_name="Next.exe",
        after_process_name="Final.exe",
    )

    assert len(screenshots) == 3  # INIT baseline + one after per action.
    assert [call[0] for call in client.calls].count("screenshot") == 3
    assert steps[0]["before_evidence_id"] == baseline["evidence_id"]
    assert steps[1]["before_evidence_id"] == steps[0]["after_evidence_id"]
    assert steps[1]["after_evidence_id"] == lifecycle.current_evidence_id


def test_evidence_lifecycle_recaptures_before_when_chain_file_is_missing(tmp_path):
    class FakeClient:
        def __init__(self):
            self.screenshot_count = 0

        def call_tool(self, tool, arguments):
            if tool == "screenshot":
                self.screenshot_count += 1
                return {
                    "ok": True,
                    "image_b64": base64.b64encode(PNG_1X1).decode(),
                }
            return {"ok": True}

    run_dir = create_run_dir("win-dev", root=tmp_path)
    screenshots, steps = [], []
    client = FakeClient()
    lifecycle = E2EEvidenceLifecycle(
        run_dir,
        screenshot_sink=screenshots.append,
        step_sink=steps.append,
    )
    baseline = lifecycle.initialize(client, process_name="Initial.exe")
    (run_dir / baseline["path"]).unlink()

    lifecycle.run_action(
        client,
        step_id="001-click",
        tool="click",
        before_process_name="Initial.exe",
        after_process_name="Next.exe",
    )

    assert client.screenshot_count == 3  # original INIT, repaired before, after
    assert steps[0]["before_recaptured"] is True
    assert steps[0]["before_evidence_id"] != baseline["evidence_id"]
