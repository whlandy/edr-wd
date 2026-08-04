from __future__ import annotations

import json
import os

import pytest

from agent.cli import build_parser
from agent.target_config import TargetConfig


def _config(name: str = "2.26-edr-win26-win11") -> dict:
    return {
        "$schema": "./targets.schema.json",
        "default_target": name,
        "targets": {
            name: {
                "platform": "windows",
                "app_profile": "windows_hisec",
                "identity": {"hostname": "edr-win26", "os_version": "win11"},
                "ssh": {
                    "host": "192.0.2.26",
                    "port": 22,
                    "user": "tester",
                    "auth": {"type": "password", "password": "secret"},
                },
                "mcp": {
                    "host": "0.0.0.0",
                    "port": 8765,
                    "path": "/mcp",
                    "connect_mode": "direct",
                    "tunnel": {"enabled": False, "local_port": 18765},
                },
                "windows": {
                    "python_path": "C:/Python/python.exe",
                    "target_root": "C:/edr-wd/target",
                    "task_name": "StartEDRMCP",
                },
            }
        },
    }


def test_save_is_atomic_secure_and_keeps_backup(tmp_path):
    path = tmp_path / "targets.local.json"
    path.write_text(json.dumps(_config()), encoding="utf-8")
    config = TargetConfig(path)
    config._data["targets"]["2.26-edr-win26-win11"]["description"] = "updated"

    config.save()

    assert json.loads(path.read_text(encoding="utf-8"))["targets"][
        "2.26-edr-win26-win11"
    ]["description"] == "updated"
    assert path.with_suffix(".json.bak").exists()
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.with_suffix(".json.bak").stat().st_mode & 0o777 == 0o600


def test_rename_dry_run_does_not_write(tmp_path):
    path = tmp_path / "targets.local.json"
    original = _config("legacy")
    path.write_text(json.dumps(original), encoding="utf-8")
    config = TargetConfig(path)

    proposed = config.rename_target("legacy", dry_run=True)

    assert proposed == "2.26-edr-win26-win11"
    assert json.loads(path.read_text(encoding="utf-8")) == original


def test_cli_exposes_cross_platform_commands():
    parser = build_parser()
    for command in ("status", "up", "down", "restart", "repair", "test"):
        assert parser.parse_args([command]).command == command
    assert parser.parse_args(["config", "--guide"]).guide is True


def test_schema_is_valid_json():
    schema_path = os.path.join(os.path.dirname(__file__), "..", "config", "targets.schema.json")
    schema = json.loads(open(schema_path, encoding="utf-8").read())
    assert schema["$schema"].endswith("2020-12/schema")
    assert "target" in schema["$defs"]
