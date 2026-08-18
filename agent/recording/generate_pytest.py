"""Deterministic, intentionally thin pytest projection."""

from __future__ import annotations

import re


def safe_test_name(name: str) -> str:
    value = re.sub(r"[^0-9A-Za-z_]+", "_", name).strip("_").lower()
    if not value or value[0].isdigit():
        value = "recorded_" + value
    return value


def render_pytest_ini() -> str:
    """Make the directory its own pytest root.

    Without it the test inherits whatever marker filter the surrounding
    repository configures — `edr-wd` deselects anything unmarked — and a
    delivered recording silently runs zero tests instead of replaying.
    """
    return (
        "[pytest]\n"
        "# This directory is a standalone deliverable: it must not inherit a\n"
        "# marker filter that would deselect the replay it exists to run.\n"
        "addopts =\n"
    )


def render_conftest() -> str:
    """The fixture the generated test needs, beside the test that needs it.

    A recording directory is the deliverable: it has to be runnable with plain
    `pytest <dir>` and nothing else, so the fixture that builds the replay
    runtime ships with it rather than living in a suite the directory is not
    part of.
    """
    return (
        '"""Target wiring for the generated replay test.\n\n'
        "Run with:  pytest <this directory>\n\n"
        "`edr-wd` must be importable — install it, or set PYTHONPATH to the\n"
        "repository root.\n\n"
        "    EDR_WD_CONFIG   target config path (default config/targets.local.json)\n"
        "    EDR_WD_TARGET   target name        (default: the config's default_target)\n"
        "    EDR_WD_PROFILE  execution profile  (default: the golden trace's own)\n"
        '"""\n\n'
        "import json\n"
        "import os\n"
        "from pathlib import Path\n\n"
        "import pytest\n\n"
        "from agent.recording.replay import load_golden_trace\n"
        "from agent.recording.runtime import build_replay_runtime\n"
        "from agent.subagent.target_agent import TargetSubAgent\n"
        "from agent.target_config import TargetConfig\n\n\n"
        "@pytest.fixture(scope=\"session\")\n"
        "def edr_wd_golden():\n"
        "    return load_golden_trace(Path(__file__).parent / \"golden-trace.json\")\n\n\n"
        "@pytest.fixture(scope=\"session\")\n"
        "def edr_wd_target(edr_wd_golden):\n"
        "    config = TargetConfig(os.environ.get(\"EDR_WD_CONFIG\"))\n"
        "    target = os.environ.get(\"EDR_WD_TARGET\") or config.get_default_target()\n"
        "    agent = TargetSubAgent(target, config=config)\n"
        "    ready = agent.ensure_ready()\n"
        "    if not ready.get(\"ok\"):\n"
        "        pytest.skip(f\"target {target} is not reachable: {ready}\")\n"
        "    profile = (\n"
        "        os.environ.get(\"EDR_WD_PROFILE\")\n"
        "        or edr_wd_golden.environment.get(\"profile\")\n"
        "        or \"default\"\n"
        "    )\n"
        "    return build_replay_runtime(\n"
        "        agent,\n"
        "        edr_wd_golden,\n"
        "        profile=profile,\n"
        "        asset_root=Path(__file__).parent,\n"
        "    )\n"
    )


def render_pytest(name: str) -> str:
    function_name = "test_" + safe_test_name(name)
    return (
        "from agent.recording.replay import replay_golden_trace\n\n\n"
        f"def {function_name}(edr_wd_target, edr_wd_golden):\n"
        "    result = replay_golden_trace(edr_wd_target, edr_wd_golden)\n"
        "    assert result.task_success, result.summary\n"
    )
