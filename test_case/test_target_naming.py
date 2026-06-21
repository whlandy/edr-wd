"""Contract tests for canonical EDR-WD target names."""

import json

import pytest

from agent.target_config import (
    TargetConfig,
    build_target_name,
    normalize_observed_os_version,
    normalize_os_version,
    verify_observed_identity,
)


def test_build_target_name_uses_last_two_ip_octets():
    assert (
        build_target_name("192.0.2.26", "EDR-WIN26", "win11")
        == "2.26-edr-win26-win11"
    )


def test_build_target_name_normalizes_components():
    assert (
        build_target_name("10.20.3.9", "Mac Lab_01", "macOS 14")
        == "3.9-mac-lab-01-macos14"
    )


@pytest.mark.parametrize("ip", ["target.local", "192.0.2", "2001:db8::1"])
def test_build_target_name_rejects_non_ipv4(ip):
    with pytest.raises(ValueError):
        build_target_name(ip, "host", "win11")


@pytest.mark.parametrize("value", ["Windows 11 23H2", "win11-build22631", "macOS 14.5"])
def test_os_version_rejects_release_and_build_details(value):
    with pytest.raises(ValueError):
        normalize_os_version(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("Windows 11", "win11"), ("windows11", "win11"), ("macOS 14", "macos14")],
)
def test_os_version_accepts_major_only(value, expected):
    assert normalize_os_version(value) == expected


def test_observed_os_version_reduces_platform_output_to_major():
    assert normalize_observed_os_version("windows", "Microsoft Windows 11 Pro") == "win11"
    assert normalize_observed_os_version("macos", "14.5.1") == "macos14"


def _write_windows_config(tmp_path, name):
    config_path = tmp_path / "targets.json"
    config_path.write_text(
        json.dumps(
            {
                "default_target": name,
                "targets": {
                    name: {
                        "platform": "windows",
                        "app_profile": "windows_hisec",
                        "identity": {
                            "hostname": "EDR-WIN26",
                            "os_version": "win11",
                        },
                        "ssh": {
                            "host": "192.0.2.26",
                            "port": 22,
                            "user": "admin",
                            "auth": {"type": "password", "password": "test-only"},
                        },
                        "mcp": {
                            "host": "0.0.0.0",
                            "port": 8765,
                            "path": "/mcp",
                            "connect_mode": "direct",
                        },
                        "windows": {
                            "python_path": "python",
                            "target_root": "C:\\edr-wd\\target",
                            "task_name": "StartEDRMCP",
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return config_path


def test_target_config_resolves_canonical_name(tmp_path):
    name = "2.26-edr-win26-win11"
    config = TargetConfig(_write_windows_config(tmp_path, name))

    assert config.get_canonical_target_name() == name
    assert config.get_resolved_target()["_canonical_name"] == name
    assert config.validate() == []


def test_validation_reports_noncanonical_key(tmp_path):
    config = TargetConfig(_write_windows_config(tmp_path, "win-dev"))
    errors = config.validate()

    assert any("2.26-edr-win26-win11" in error for error in errors)


def test_rename_target_updates_key_and_default(tmp_path):
    config_path = _write_windows_config(tmp_path, "win-dev")
    config = TargetConfig(config_path)

    assert config.rename_target("win-dev") == "2.26-edr-win26-win11"

    reloaded = TargetConfig(config_path)
    assert reloaded.get_default_target() == "2.26-edr-win26-win11"
    assert reloaded.has_target("2.26-edr-win26-win11")
    assert not reloaded.has_target("win-dev")


def test_live_identity_mismatch_is_reported(tmp_path):
    name = "2.26-edr-win26-win11"
    config = TargetConfig(_write_windows_config(tmp_path, name))
    cfg = config.get_resolved_target(name)
    cfg["_target_name"] = name

    result = verify_observed_identity(
        cfg, "windows", "unexpected-host", "Microsoft Windows 11 Pro"
    )

    assert result["ok"] is False
    assert result["observed_name"] == "2.26-unexpected-host-win11"
