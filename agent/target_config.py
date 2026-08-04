"""
target_config.py — EDR-WD target registry and configuration loader.

Runtime config is loaded from EDR_WD_CONFIG or config/targets.local.json.
targets.example.json is only used by --init / documentation and is NEVER used
for real operations.

Usage:
    from agent.target_config import TargetConfig

    tc = TargetConfig()                    # auto-detect: EDR_WD_CONFIG > targets.local.json
    tc = TargetConfig("config/custom.json")  # explicit path

    # Basic queries
    tc.list_targets()                      # → {"2.26-edr-win26-win11": {...}}
    tc.get_target("2.26-edr-win26-win11")  # → full target config dict
    tc.get_default_target()               # → target name string

    # URL builder
    tc.build_mcp_url("2.26-edr-win26-win11")  # → "http://<TARGET_IP>:8765/mcp"

    # Auth resolver (prefers inline password; password_env remains supported)
    tc.resolve_auth("2.26-edr-win26-win11")  # → resolved Paramiko SSH config

    # CLI
    python -m agent.target_config --list
    python -m agent.target_config --validate
    python -m agent.target_config --init [--force]
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Optional


def _normalize_name_part(value: str, field: str) -> str:
    """Normalize hostname/OS metadata for use in a target config key."""
    normalized = re.sub(r"[^a-z0-9]+", "-", str(value).strip().lower()).strip("-")
    if not normalized:
        raise ValueError(f"{field} must contain at least one letter or digit")
    return normalized


def normalize_os_version(value: str) -> str:
    """Return a major-only OS label such as ``win11`` or ``macos14``."""
    compact = re.sub(r"[^a-z0-9]+", "", str(value).strip().lower())
    match = re.fullmatch(r"(?:win|windows)(\d{1,2})", compact)
    if match:
        return f"win{int(match.group(1))}"
    match = re.fullmatch(r"macos(\d{1,2})", compact)
    if match:
        return f"macos{int(match.group(1))}"
    raise ValueError(
        "os_version must contain only the major OS version "
        "(for example: win11 or macos14)"
    )


def normalize_observed_os_version(platform: str, value: str) -> str:
    """Convert an OS caption/version observed on a target to the config label."""
    if platform == "windows":
        match = re.search(r"windows\s*(\d{1,2})", value, flags=re.IGNORECASE)
        if match:
            return f"win{int(match.group(1))}"
    elif platform == "macos":
        match = re.search(r"(\d{1,2})(?:\.\d+)*", value)
        if match:
            return f"macos{int(match.group(1))}"
    raise ValueError(f"cannot determine {platform} major version from {value!r}")


def build_target_name(ip: str, hostname: str, os_version: str) -> str:
    """Build ``<IP3>.<IP4>-<hostname>-<os-version>`` from target identity."""
    try:
        address = ipaddress.ip_address(str(ip).strip())
    except ValueError as exc:
        raise ValueError(f"target IP must be a valid IPv4 address: {ip!r}") from exc
    if address.version != 4:
        raise ValueError("target naming currently requires an IPv4 address")

    octets = str(address).split(".")
    host_part = _normalize_name_part(hostname, "hostname")
    os_part = normalize_os_version(os_version)
    return f"{octets[2]}.{octets[3]}-{host_part}-{os_part}"


def verify_observed_identity(
    cfg: dict,
    platform: str,
    hostname: str,
    observed_os_version: str,
) -> dict:
    """Compare live target identity with the configured canonical target name."""
    configured_name = cfg.get("_target_name")
    if not cfg.get("_canonical_name") or not configured_name:
        return {
            "ok": True,
            "verified": False,
            "reason": "canonical identity is not configured",
        }

    os_version = normalize_observed_os_version(platform, observed_os_version)
    observed_name = build_target_name(
        cfg.get("ssh", {}).get("host", ""),
        hostname,
        os_version,
    )
    return {
        "ok": observed_name == configured_name,
        "verified": True,
        "configured_name": configured_name,
        "observed_name": observed_name,
        "hostname": _normalize_name_part(hostname, "hostname"),
        "os_version": os_version,
    }

# ── Config discovery ──────────────────────────────────────────────────────────

def _find_config() -> Path | None:
    """
    Find the first existing config file for runtime use, in priority order:

      1. EDR_WD_CONFIG env var (explicit path, if set and exists)
      2. config/targets.local.json (must exist for real operations)

    Do NOT fall back to targets.example.json — it contains placeholder values
    and must not be used as a real target.
    """
    # 1. EDR_WD_CONFIG env var — if set, it is an explicit intent; must exist
    env_path = os.environ.get("EDR_WD_CONFIG")
    if env_path:
        p = Path(env_path)
        if not p.exists():
            raise ConfigNotFound(
                f"EDR_WD_CONFIG is set to '{env_path}' but the file does not exist. "
                f"Please create the file or unset EDR_WD_CONFIG to use config/targets.local.json."
            )
        return p
    # 2. targets.local.json (required for real operations)
    base = Path(__file__).parent.parent
    local = base / "config" / "targets.local.json"
    if local.exists():
        return local
    return None


def _find_example() -> Path | None:
    """
    Find targets.example.json for --list / documentation purposes only.
    NEVER used for real operations.
    """
    base = Path(__file__).parent.parent
    example = base / "config" / "targets.example.json"
    return example if example.exists() else None


def _default_config_path() -> Path:
    base = Path(__file__).parent.parent
    return base / "config" / "targets.local.json"


def _secure_mode(path: Path) -> None:
    """Restrict a credential-bearing config file to the current user on POSIX."""
    if os.name != "nt":
        path.chmod(0o600)


def _atomic_write_json(path: Path, data: dict, *, backup: bool) -> None:
    """Durably replace a JSON config and optionally retain the previous version."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists():
        backup_path = path.with_suffix(path.suffix + ".bak")
        shutil.copy2(path, backup_path)
        _secure_mode(backup_path)

    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        _secure_mode(tmp_path)
        os.replace(tmp_path, path)
        _secure_mode(path)
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


# ── Minimal skeleton (used by --init) ───────────────────────────────────────

SKELETON = {
    "$schema": "./targets.schema.json",
    "default_target": "2.26-edr-win26-win11",
    "targets": {
        "2.26-edr-win26-win11": {
            "description": "",
            "platform": "windows",
            "app_profile": "windows_hisec",
            "identity": {
                "hostname": "edr-win26",
                "os_version": "win11",
            },
            "ssh": {
                "host": "",
                "port": 22,
                "user": "",
                "auth": {"type": "password", "password": ""},
            },
            "mcp": {
                "host": "0.0.0.0",
                "port": 8765,
                "path": "/mcp",
                "connect_mode": "direct",
                "tunnel": {"enabled": False, "local_port": 18765},
            },
            "windows": {
                "python_path": "",
                "target_root": "",
                "task_name": "StartEDRMCP",
                "run_with_highest_privileges": True,
            },
        },
        "2.29-edr-mac29-macos14": {
            "description": "",
            "platform": "macos",
            "app_profile": "macos_generic",
            "identity": {
                "hostname": "edr-mac29",
                "os_version": "macos14",
            },
            "ssh": {
                "host": "",
                "port": 22,
                "user": "",
                "auth": {"type": "password", "password": ""},
            },
            "mcp": {
                "host": "0.0.0.0",
                "port": 8765,
                "path": "/mcp",
                "connect_mode": "direct",
                "tunnel": {"enabled": False, "local_port": 18765},
            },
            "macos": {
                "python_path": "/opt/homebrew/bin/python3",
                "root": "",
                "backend": "macos_accessibility",
                "launch_name": "com.edr-wd.target",
            },
        },
    },
}


class ConfigNotFound(Exception):
    """Raised when no runtime config file can be found."""
    pass


class ConfigError(Exception):
    """Raised when the config file exists but has validation errors."""
    pass


# ── Legacy schema normalizer ──────────────────────────────────────────────────

def _normalize_target(raw: dict) -> dict:
    """
    Convert a pre-2026 targets.json raw target dict to the current schema.

    Legacy fields seen in targets.json:
      server.{python_path, host, port, command}  → mcp (subset)
      connection.{preferred, direct_url, tunnel_url} → mcp.connect_mode / direct_url / tunnel_url
      task.name                                   → windows.task_name
      paths.{target_root, scripts}               → windows.{target_root, scripts}

    Current schema (post-2026) fields preserved as-is:
      ssh, mcp, windows, description, name
    """
    import warnings

    # Detect legacy by presence of 'server' or 'connection' at target level
    has_server = "server" in raw
    has_connection = "connection" in raw
    if not has_server and not has_connection:
        # Already new schema. Fill omitted platform for older local configs:
        # a target with a macos block and no windows block is a macOS target.
        out = dict(raw)
        if "platform" not in out:
            if "macos" in out and "windows" not in out:
                out["platform"] = "macos"
            else:
                out["platform"] = "windows"
        return out

    out = dict(raw)  # shallow copy — don't mutate caller's dict

    # server.{python_path,host,port,command} → mcp
    server = raw.get("server", {})
    mcp = dict(raw.get("mcp", {}))  # preserve any existing mcp fields
    mcp.setdefault("host", server.get("host", "0.0.0.0"))
    mcp.setdefault("port", server.get("port", 8765))
    mcp.setdefault("path", "/mcp")
    mcp.setdefault("connect_mode", "direct")
    mcp.setdefault("tunnel", {"enabled": False, "local_port": 18765})
    out["mcp"] = mcp

    # connection → mcp overrides
    conn = raw.get("connection", {})
    if conn.get("preferred"):
        mcp["connect_mode"] = conn["preferred"]
    if conn.get("direct_url"):
        # [internal legacy-only field] stores the original full URL from legacy
        # connection.direct_url. build_mcp_url() checks this as a fallback.
        # Not part of the public schema — do not use in new configs.
        mcp["_direct_url_override"] = conn["direct_url"]
    if conn.get("tunnel_url"):
        # [internal legacy-only field] stores the original tunnel URL.
        # build_mcp_url() checks this as a fallback for tunnel mode.
        # Not part of the public schema — do not use in new configs.
        mcp["_tunnel_url_override"] = conn["tunnel_url"]

    # task.name → windows.task_name
    task = raw.get("task", {})
    paths = raw.get("paths", {})
    win = dict(raw.get("windows", {}))
    win.setdefault("task_name", task.get("name", "StartEDRMCP"))
    win.setdefault("target_root", paths.get("target_root", ""))
    win.setdefault("scripts", paths.get("scripts", "scripts"))
    win.setdefault("python_path", server.get("python_path", ""))
    win.setdefault("run_with_highest_privileges", True)
    out["windows"] = win

    # ssh.password (legacy) → ssh.auth (new schema)
    ssh_legacy = raw.get("ssh", {})
    if "auth" not in ssh_legacy:
        if ssh_legacy.get("password"):
            out_ssh = dict(out.get("ssh", {}))
            out_ssh["auth"] = {"type": "password", "password": ssh_legacy["password"]}
            out["ssh"] = out_ssh

    # platform default: legacy targets are assumed to be Windows
    # (windows target is the only target type that existed pre-2026).
    out.setdefault("platform", "windows")

    return out


# ── Platform validation helpers ─────────────────────────────────────────────

SUPPORTED_PLATFORMS = ("windows", "macos")
SUPPORTED_PROFILES = {
    "windows": {"windows_hisec"},
    "macos": {"macos_hisec", "macos_generic"},
}


def _validate_platform_specific(t: dict) -> list[str]:
    """
    Platform-specific validation. Returns a list of error messages.

    Behavior:
      - platform=windows (default): requires windows.{target_root, python_path, task_name}
      - platform=macos: requires macos.{root, python_path, backend, launch_name}
    """
    errors: list[str] = []
    platform = t.get("platform", "windows")

    if platform not in SUPPORTED_PLATFORMS:
        errors.append(
            f"platform='{platform}' is not supported. "
            f"Supported: {', '.join(SUPPORTED_PLATFORMS)}"
        )
        return errors

    if platform == "windows":
        win = t.get("windows", {})
        if not win.get("target_root"):
            errors.append("windows.target_root is required for platform=windows")
        if not win.get("python_path"):
            errors.append("windows.python_path is required for platform=windows")
    elif platform == "macos":
        mac = t.get("macos", {})
        if not mac.get("root"):
            errors.append("macos.root is required for platform=macos")
        if not mac.get("python_path"):
            errors.append("macos.python_path is required for platform=macos")
        if not mac.get("backend"):
            errors.append("macos.backend is required for platform=macos")
        if not mac.get("launch_name"):
            errors.append("macos.launch_name is required for platform=macos")

    return errors


def _print_guide() -> None:
    """Print a concise config setup guide."""
    print("EDR-WD config guide")
    print("=" * 60)
    print("1. Generate a skeleton config:")
    print("   edr-wd config --init")
    print("")
    print("2. Edit config/targets.local.json with real values:")
    print("   - default_target")
    print("   - identity.hostname / identity.os_version (major only, e.g. win11)")
    print("   - ssh.host / ssh.user / ssh.auth")
    print("   - ssh.auth.type=password with ssh.auth.password for intranet targets")
    print("   - mcp.host / mcp.port / mcp.path / mcp.connect_mode")
    print("   - windows.* for platform=windows")
    print("   - macos.* for platform=macos")
    print("")
    print("3. Preview or migrate target names:")
    print("   edr-wd config --suggest-names")
    print("   edr-wd config --rename-target <OLD_TARGET_NAME> --dry-run")
    print("   edr-wd config --rename-target <OLD_TARGET_NAME>")
    print("")
    print("4. Validate the file:")
    print("   edr-wd config --validate")
    print("")
    print("5. Inspect targets:")
    print("   edr-wd config --list")
    print("")
    print("6. Use the deployment entrypoints:")
    print("   Windows agent: agent/deploy.ps1")
    print("   Windows target: target/deploy.ps1")
    print("   macOS/Linux agent: agent/edr-wd.sh")
    print("")
    print("Helpful notes:")
    print("   - Set EDR_WD_CONFIG to point at an alternate config file.")
    print("   - Paramiko is used for all SSH/SFTP auth paths.")
    print("   - Inline password auth is preferred; password_env/key auth are compatibility paths.")
    print("   - TODO security hardening: move secrets out of local JSON when needed.")
    print("   - Use scripts/redact_config.py to inspect a config without secrets.")
    print("   - Use edr-wd config --list to verify per-target platform/profile fields.")


# ── TargetConfig class ────────────────────────────────────────────────────────

class TargetConfig:
    """
    Loaded once per instantiation; call reload() to re-read from disk.
    """

    def __init__(self, config_path: str | Path | None = None):
        if config_path:
            self._path = Path(config_path)
        else:
            self._path = _find_config()
        self._data: dict = {}
        self._loaded = False
        if self._path and self._path.exists():
            self.reload()

    # ── I/O ──────────────────────────────────────────────────────────────────

    def reload(self) -> None:
        if self._path and self._path.exists():
            with open(self._path, encoding="utf-8") as f:
                self._data = json.load(f)
        else:
            self._data = {}
        self._loaded = True

    def save(self) -> None:
        if not self._path:
            raise RuntimeError("No config path set")
        _atomic_write_json(self._path, self._data, backup=True)

    @property
    def path(self) -> Path | None:
        return self._path

    # ── Queries ───────────────────────────────────────────────────────────────

    def list_targets(self) -> dict:
        """Return {name: target_dict} for all targets."""
        return dict(self._data.get("targets", {}))

    def get_default_target(self) -> str | None:
        return self._data.get("default_target")

    def get_target(self, name: str | None = None) -> dict:
        """
        Return the normalized config dict for `name`.
        If name is None, uses default_target.
        Raises KeyError if target not found.

        Legacy schema (targets.json, pre-2026): converts server/connection/task/paths
        to the current mcp/ssh/windows structure so callers always get a consistent shape.
        """
        if name is None:
            name = self.get_default_target()
        if not name:
            raise KeyError("No target name and no default_target set")
        targets = self._data.get("targets", {})
        if name not in targets:
            raise KeyError(f"Target '{name}' not found. Available: {list(targets.keys())}")
        raw = dict(targets[name])
        return _normalize_target(raw)

    def has_target(self, name: str) -> bool:
        return name in self._data.get("targets", {})

    # ── Platform / profile queries ───────────────────────────────────────────

    def get_target_platform(self, name: str | None = None) -> str:
        """
        Return the platform for a target: 'windows' or 'macos'.
        Defaults to 'windows' when not specified (legacy behavior).
        """
        t = self.get_target(name)
        return t.get("platform", "windows")

    def get_target_app_profile(self, name: str | None = None) -> str | None:
        """
        Return the app_profile for a target, or None if not set.
        Used to dispatch test suites to the right workflow.
        """
        t = self.get_target(name)
        return t.get("app_profile")

    def get_canonical_target_name(self, name: str | None = None) -> str:
        """Return the target name derived from its IPv4 address and identity."""
        t = self.get_target(name)
        identity = t.get("identity", {})
        return build_target_name(
            t.get("ssh", {}).get("host", ""),
            identity.get("hostname", ""),
            identity.get("os_version", ""),
        )

    # ── MCP URL builder ───────────────────────────────────────────────────────

    def build_mcp_url(self, target_name: str | None = None) -> str:
        """
        Build the MCP HTTP URL for the target.

        connect_mode=direct → http://{ssh.host}:{mcp.port}{mcp.path}
        connect_mode=local  → http://127.0.0.1:{mcp.port}{mcp.path}
        connect_mode=tunnel → http://127.0.0.1:{tunnel.local_port}{mcp.path}

        For legacy normalized configs, _direct_url_override / _tunnel_url_override
        are used when the original connection.{direct_url,tunnel_url} full URL
        was present in the raw config.

        Raises KeyError if ssh.host is empty and mode is direct.
        """
        t = self.get_target(target_name)
        mcp = t.get("mcp", {})
        connect_mode = mcp.get("connect_mode", "direct")

        # Legacy normalized configs carry the original full URL as an override
        if connect_mode == "direct":
            direct_url = mcp.get("_direct_url_override")
            if direct_url:
                return direct_url
            host = t.get("ssh", {}).get("host", "")
            if not host:
                raise KeyError(f"Target '{target_name}': ssh.host is required for direct mode")
            port = mcp.get("port", 8765)
            path = mcp.get("path", "/mcp")
            return f"http://{host}:{port}{path}"
        if connect_mode == "local":
            port = mcp.get("port", 8765)
            path = mcp.get("path", "/mcp")
            return f"http://127.0.0.1:{port}{path}"

        if connect_mode == "tunnel":
            tunnel_url = mcp.get("_tunnel_url_override")
            if tunnel_url:
                return tunnel_url
            local_port = mcp.get("tunnel", {}).get("local_port", 18765)
            path = mcp.get("path", "/mcp")
            return f"http://127.0.0.1:{local_port}{path}"

        raise KeyError(
            f"Target '{target_name}': unsupported mcp.connect_mode={connect_mode!r}; "
            "expected 'direct', 'local', or 'tunnel'"
        )

    # ── Auth resolver ─────────────────────────────────────────────────────────

    def resolve_auth(self, target_name: str | None = None) -> dict:
        """
        Return a copy of the SSH config. Inline auth.password is preferred.
        auth.password_env is still supported for compatibility and TODO
        hardening, but it is no longer required for intranet targets.

        Raises EnvironmentError if password auth is selected and neither
        auth.password nor a resolvable auth.password_env is present.
        """
        t = self.get_target(target_name)
        ssh = dict(t.get("ssh", {}))
        auth = dict(ssh.get("auth", {}))

        if auth.get("type") == "password":
            has_inline = bool(auth.get("password"))
            has_env = bool(auth.get("password_env"))

            if has_inline and has_env:
                raise ConfigError(
                    f"Target '{target_name}': auth.password and auth.password_env "
                    "are mutually exclusive; choose one"
                )

            if has_inline:
                auth = {
                    "type": "password",
                    "password": auth["password"],
                }
            elif has_env:
                penv_name = auth["password_env"]
                value = os.getenv(penv_name)
                if not value:
                    raise ConfigError(
                        f"Target '{target_name}': auth.password_env='{penv_name}' "
                        f"is set but environment variable '{penv_name}' is not defined"
                    )
                auth = {
                    "type": "password",
                    "password": value,
                }
            else:
                raise ConfigError(
                    f"Target '{target_name}': auth.type='password' but "
                    "neither auth.password nor auth.password_env is set"
                )

        ssh["auth"] = auth
        return ssh

    # ── Convenience: full resolved target dict ────────────────────────────────

    def get_resolved_target(self, name: str | None = None) -> dict:
        """
        Return a fully resolved target config:
          - resolves password auth (inline password preferred; password_env supported)
          - adds mcp_url (built from ssh.host + mcp config)
        """
        t = self.get_target(name)
        ssh = self.resolve_auth(name)
        mcp_url = self.build_mcp_url(name)
        try:
            canonical_name = self.get_canonical_target_name(name)
        except ValueError:
            # Keep existing local configs operational while exposing that no
            # canonical identity is available yet.
            canonical_name = None
        return {
            **t,
            "ssh": ssh,
            "_mcp_url": mcp_url,
            "_canonical_name": canonical_name,
        }

    def rename_target(
        self,
        old_name: str,
        new_name: str | None = None,
        *,
        dry_run: bool = False,
    ) -> str:
        """Rename a target key and update default_target, then save the config."""
        targets = self._data.get("targets", {})
        if old_name not in targets:
            raise KeyError(f"Target '{old_name}' not found. Available: {list(targets)}")
        canonical_name = new_name or self.get_canonical_target_name(old_name)
        if canonical_name != old_name and canonical_name in targets:
            raise ConfigError(f"Target '{canonical_name}' already exists")

        if canonical_name != old_name and not dry_run:
            items = []
            for name, target in targets.items():
                items.append((canonical_name if name == old_name else name, target))
            self._data["targets"] = dict(items)
            if self.get_default_target() == old_name:
                self._data["default_target"] = canonical_name
            self.save()
        return canonical_name

    # ── Init ──────────────────────────────────────────────────────────────────

    @classmethod
    def init_config(cls, path: str | Path | None = None, *, force: bool = False) -> Path:
        """
        Generate a config skeleton at `path` (default: targets.local.json).

        If the file already exists and force=False, raises FileExistsError.
        """
        p = Path(path) if path else _default_config_path()
        if p.exists() and not force:
            raise FileExistsError(f"{p} already exists. Use --force to overwrite.")
        _atomic_write_json(p, SKELETON, backup=force and p.exists())
        return p

    # ── Validate ─────────────────────────────────────────────────────────────

    def validate(self) -> list[str]:
        """
        Check the loaded config and return a list of error/warning strings.
        Returns empty list if everything is OK.
        """
        errors: list[str] = []
        targets = self._data.get("targets", {})
        if not targets:
            errors.append("No targets defined")
            return errors

        default = self._data.get("default_target") or self._data.get("default")
        if not default:
            errors.append("default_target is not set")
        elif default not in targets:
            errors.append(f"default_target='{default}' but that target does not exist")

        for name, t in targets.items():
            raw = dict(t)
            is_legacy = "server" in raw or "connection" in raw
            # Check for legacy schema and warn once during validation
            if is_legacy:
                errors.append(
                    f"[{name}] uses legacy pre-2026 schema "
                    "(server/connection/task/paths fields). "
                    "Migrate to mcp/ssh/windows schema. "
                    "Run 'python -m agent.target_config --init' for the new skeleton."
                )

            # Validate the normalized form so both legacy and new schemas are checked
            try:
                t = _normalize_target(raw)
            except Exception as e:
                errors.append(f"[{name}] failed to normalize: {e}")
                continue

            # ssh
            ssh = t.get("ssh", {})
            if not ssh.get("host"):
                errors.append(f"[{name}] ssh.host is required")
            if not ssh.get("user"):
                errors.append(f"[{name}] ssh.user is required")
            ssh_port = ssh.get("port", 22)
            if not isinstance(ssh_port, int) or not (1 <= ssh_port <= 65535):
                errors.append(f"[{name}] ssh.port must be an integer 1-65535")
            auth = ssh.get("auth", {})
            if auth.get("type") == "password":
                if auth.get("password"):
                    pass
                elif not auth.get("password_env"):
                    errors.append(
                        f"[{name}] auth.type='password' requires auth.password "
                        "or auth.password_env"
                    )
                penv = auth.get("password_env")
                if not auth.get("password") and penv and not os.environ.get(penv):
                    errors.append(
                        f"[{name}] auth.password_env='{penv}' is set but "
                        f"env var '{penv}' is not defined"
                    )
            elif auth.get("type") == "key":
                pass
            else:
                errors.append(
                    f"[{name}] ssh.auth.type must be 'password' (preferred) "
                    "or 'key' (compatibility)"
                )

            identity = t.get("identity", {})
            if not is_legacy and not identity.get("hostname"):
                errors.append(f"[{name}] identity.hostname is required")
            if not is_legacy and not identity.get("os_version"):
                errors.append(f"[{name}] identity.os_version is required (for example: win11)")
            if not is_legacy and ssh.get("host") and identity.get("hostname") and identity.get("os_version"):
                try:
                    canonical_name = build_target_name(
                        ssh["host"],
                        identity["hostname"],
                        identity["os_version"],
                    )
                    if name != canonical_name:
                        errors.append(
                            f"[{name}] target name must be '{canonical_name}' "
                            "(<IP3>.<IP4>-<hostname>-<os-version>)"
                        )
                except ValueError as exc:
                    errors.append(f"[{name}] cannot build canonical target name: {exc}")

            # mcp
            mcp = t.get("mcp", {})
            port = mcp.get("port")
            if not isinstance(port, int) or not (1 < port < 65536):
                errors.append(f"[{name}] mcp.port must be an integer 1-65535")
            path = mcp.get("path", "/mcp")
            if not path.startswith("/"):
                errors.append(f"[{name}] mcp.path must start with '/'")
            connect_mode = mcp.get("connect_mode", "direct")
            if connect_mode not in {"direct", "local", "tunnel"}:
                errors.append(
                    f"[{name}] mcp.connect_mode must be 'direct', 'local', or 'tunnel'"
                )
            if connect_mode == "tunnel":
                local_port = mcp.get("tunnel", {}).get("local_port")
                if not isinstance(local_port, int) or not (1 <= local_port <= 65535):
                    errors.append(
                        f"[{name}] mcp.tunnel.local_port must be an integer 1-65535"
                    )

            platform = t.get("platform", "windows")
            profile = t.get("app_profile")
            if profile and profile not in SUPPORTED_PROFILES.get(platform, set()):
                allowed = ", ".join(sorted(SUPPORTED_PROFILES.get(platform, set())))
                errors.append(
                    f"[{name}] app_profile='{profile}' is invalid for platform={platform}; "
                    f"expected one of: {allowed or '(none)'}"
                )

            # Platform-specific required fields
            errors.extend(_validate_platform_specific(t))

        return errors


# ── CLI entry point ─────────────────────────────────────────────────────────

def add_config_arguments(
    parser: argparse.ArgumentParser,
    *,
    include_config_path: bool = True,
) -> None:
    """Register the shared config command arguments on a parser."""
    if include_config_path:
        parser.add_argument("--config", help="Path to config file")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--list", action="store_true", help="List all targets")
    group.add_argument("--validate", action="store_true", help="Validate config")
    group.add_argument("--init", action="store_true", help="Initialize skeleton config")
    group.add_argument("--guide", action="store_true", help="Show a concise config setup guide")
    group.add_argument("--suggest-names", action="store_true", help="Show canonical target-name suggestions")
    group.add_argument("--rename-target", metavar="NAME", help="Rename one target to its canonical name")
    parser.add_argument("--force", action="store_true", help="Overwrite existing config with --init")
    parser.add_argument("--dry-run", action="store_true", help="Preview a config mutation without writing")


def run_config_command(args: argparse.Namespace) -> int:
    """Execute a parsed config command and return its process exit code."""
    if args.guide:
        _print_guide()
        return 0

    if args.init:
        try:
            p = TargetConfig.init_config(args.config, force=args.force)
            print(f"Created: {p}")
            print("Next: run 'edr-wd config --guide' for the setup walkthrough.")
        except FileExistsError as e:
            print(f"SKIP: {e}")
        return 0

    tc = TargetConfig(args.config) if args.config else TargetConfig()

    if args.suggest_names:
        failed = False
        for name in tc.list_targets():
            try:
                print(f"{name} -> {tc.get_canonical_target_name(name)}")
            except ValueError as exc:
                failed = True
                print(f"{name} -> ERROR: {exc}")
        return 1 if failed else 0

    if args.rename_target:
        try:
            new_name = tc.rename_target(args.rename_target, dry_run=args.dry_run)
        except (ConfigError, KeyError, ValueError) as exc:
            print(f"ERROR: {exc}")
            return 1
        verb = "Would rename" if args.dry_run else "Renamed"
        print(f"{verb}: {args.rename_target} -> {new_name}")
        return 0

    if args.list:
        targets = tc.list_targets()
        default = tc.get_default_target()
        if targets:
            for name, t in targets.items():
                marker = " (default)" if name == default else ""
                desc = t.get("description") or "—"
                host = t.get("ssh", {}).get("host") or "—"
                platform = t.get("platform", "windows")
                profile = t.get("app_profile") or "—"
                try:
                    canonical = tc.get_canonical_target_name(name)
                except (KeyError, ValueError):
                    canonical = "invalid identity"
                print(
                    f"  {name}{marker}  platform={platform}  profile={profile} "
                    f"host={host}  canonical={canonical}  desc={desc}"
                )
        else:
            example = _find_example()
            if example:
                print("  (no targets.local.json found — showing targets.example.json for reference)")
                print(f"  Copy '{example}' to 'config/targets.local.json' and edit it.")
                print("  Then run: python -m agent.target_config --list")
        return 0

    if args.validate:
        errs = tc.validate()
        if not errs:
            print("OK: config is valid")
            return 0
        for e in errs:
            print(f"ERROR: {e}")
        return 1

    # No action — show help
    parser.print_help()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="EDR-WD target config tools")
    add_config_arguments(parser)
    args = parser.parse_args()
    return run_config_command(args)


if __name__ == "__main__":
    raise SystemExit(main())
