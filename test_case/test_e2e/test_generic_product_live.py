"""Opt-in local macOS smoke for the registered non-EDR product path."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from agent.products import get_product_adapter, get_product_profile
from target import server


pytestmark = pytest.mark.integration


def test_macos_calculator_activation_lock_and_observation():
    if os.environ.get("EDR_WD_GENERIC_PRODUCT_E2E") != "1":
        pytest.skip("set EDR_WD_GENERIC_PRODUCT_E2E=1 for local GUI smoke")
    if sys.platform != "darwin" or not Path("/System/Applications/Calculator.app").is_dir():
        pytest.skip("Calculator.app is unavailable on this host")
    assert server._backend_kind == "macos_accessibility"
    profile_id = "macos_calculator"
    adapter = get_product_adapter(profile_id, "macos_accessibility")
    profile = get_product_profile(profile_id, "macos_accessibility")
    activated = adapter.activate(
        lambda name, args: json.loads(getattr(server, name)(**args))
    )
    assert activated.get("ok") is True, activated
    started = json.loads(server.begin_product_run(profile_id, profile.digest))
    assert started.get("ok") is True, started
    token = started["run_token"]
    try:
        focused = json.loads(server.focus_window_role("main", token))
        assert focused.get("ok") is True, focused
        bundle = json.loads(server.observe_window_bundle(token, capture_image=False))
        assert bundle.get("ok") is True, bundle
        identity = bundle["window_identity"]
        assert identity["process_name"] in adapter.resolve_window_role("main").process_names
        assert identity["native_id"]
        assert bundle["tree"]["controls"]
        inventory = json.loads(server.observe_product_windows(token))
        assert inventory.get("ok") is True, inventory
        assert any(window["native_id"] == identity["native_id"]
                   and window["role"] == "main"
                   for window in inventory["windows"])
    finally:
        ended = json.loads(server.end_product_run(token))
        assert ended.get("ok") is True, ended
