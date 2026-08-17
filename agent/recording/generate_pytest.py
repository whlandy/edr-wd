"""Deterministic, intentionally thin pytest projection."""

from __future__ import annotations

import re


def safe_test_name(name: str) -> str:
    value = re.sub(r"[^0-9A-Za-z_]+", "_", name).strip("_").lower()
    if not value or value[0].isdigit():
        value = "recorded_" + value
    return value


def render_pytest(name: str) -> str:
    function_name = "test_" + safe_test_name(name)
    return (
        "from pathlib import Path\n\n"
        "from agent.recording.replay import load_golden_trace, replay_golden_trace\n\n\n"
        f"def {function_name}(edr_wd_target):\n"
        "    case_dir = Path(__file__).parent\n"
        "    golden = load_golden_trace(case_dir / \"golden-trace.json\")\n"
        "    result = replay_golden_trace(edr_wd_target, golden)\n"
        "    assert result.task_success, result.summary\n"
    )
