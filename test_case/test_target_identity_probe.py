"""Lifecycle probe tests for live canonical target identity verification."""

from unittest.mock import patch

from agent.lifecycle.macos import MacOSLifecycle
from agent.lifecycle.windows import WindowsLifecycle


def test_macos_probe_verifies_live_identity():
    cfg = {
        "_target_name": "2.29-edr-mac29-macos14",
        "_canonical_name": "2.29-edr-mac29-macos14",
        "ssh": {"host": "192.0.2.29"},
        "macos": {"python_path": "python3"},
    }
    responses = [
        (0, "edr-mac29\nuser\n"),
        (0, "edr-mac29\n14.5.1\n"),
        (0, "3.13.0\n"),
    ]

    with patch("agent.lifecycle.macos.run_ssh", side_effect=responses):
        result = MacOSLifecycle().probe(cfg)

    assert result["ok"] is True
    assert result["data"]["identity"]["verified"] is True
    assert result["data"]["identity"]["observed_name"] == cfg["_target_name"]


def test_windows_probe_verifies_live_identity():
    cfg = {
        "_target_name": "2.26-edr-win26-win11",
        "_canonical_name": "2.26-edr-win26-win11",
        "ssh": {"host": "192.0.2.26"},
        "windows": {"python_path": "python"},
    }
    responses = [
        (0, "EDR-WIN26\nadmin\n"),
        (0, "3.13.0\n"),
        (0, "windows target deps ok\n"),
        (0, "7.5.0\n"),
        (0, "EDR-WIN26\nMicrosoft Windows 11 Enterprise\n"),
    ]

    with patch("agent.lifecycle.windows.run_ssh", side_effect=responses):
        result = WindowsLifecycle().probe(cfg)

    assert result["ok"] is True
    assert result["data"]["identity"]["verified"] is True
    assert result["data"]["identity"]["observed_name"] == cfg["_target_name"]
