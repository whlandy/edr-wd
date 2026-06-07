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
    tc.list_targets()                      # → {"win-dev": {...}, "win-prod": {...}}
    tc.get_target("win-dev")              # → full target config dict
    tc.get_default_target()               # → target name string

    # URL builder
    tc.build_mcp_url("win-dev")          # → "http://<TARGET_IP>:8765/mcp"

    # Auth resolver (replaces password_env with actual password from env)
    tc.resolve_auth("win-dev")            # → {"host": ..., "user": ..., "auth": {"type": "password", "password": "***"}}

    # CLI
    python -m agent.target_config --list
    python -m agent.target_config --validate
    python -m agent.target_config --init [--force]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional

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


# ── Minimal skeleton (used by --init) ───────────────────────────────────────

SKELETON = {
    "default_target": "win-dev",
    "targets": {
        "win-dev": {
            "description": "",
            "platform": "windows",
            "app_profile": "windows_hisec",
            "ssh": {
                "host": "",
                "port": 22,
                "user": "",
                "auth": {"type": "password", "password_env": "EDR_WD_WIN_DEV_PASSWORD"},
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
        "mac-dev": {
            "description": "",
            "platform": "macos",
            "app_profile": "macos_generic",
            "ssh": {
                "host": "",
                "port": 22,
                "user": "",
                "auth": {"type": "password", "password_env": "EDR_WD_TARGET_PASSWORD"},
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
        # Already new schema — return as-is
        return raw

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
    print("   python -m agent.target_config --init")
    print("")
    print("2. Edit config/targets.local.json with real values:")
    print("   - default_target")
    print("   - ssh.host / ssh.user / ssh.auth")
    print("   - mcp.host / mcp.port / mcp.path / mcp.connect_mode")
    print("   - windows.* for platform=windows")
    print("   - macos.* for platform=macos")
    print("")
    print("3. Validate the file:")
    print("   python -m agent.target_config --validate")
    print("")
    print("4. Inspect targets:")
    print("   python -m agent.target_config --list")
    print("")
    print("5. Use the deployment entrypoints:")
    print("   Windows agent: agent/deploy.ps1")
    print("   Windows target: target/deploy.ps1")
    print("   macOS/Linux agent: agent/edr-wd.sh")
    print("")
    print("Helpful notes:")
    print("   - Set EDR_WD_CONFIG to point at an alternate config file.")
    print("   - Use scripts/redact_config.py to inspect a config without secrets.")
    print("   - Use agent.target_config --list to verify per-target platform/profile fields.")


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
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)

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

    # ── MCP URL builder ───────────────────────────────────────────────────────

    def build_mcp_url(self, target_name: str | None = None) -> str:
        """
        Build the MCP HTTP URL for the target.

        connect_mode=direct  → http://{ssh.host}:{mcp.port}{mcp.path}
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
        else:  # tunnel
            tunnel_url = mcp.get("_tunnel_url_override")
            if tunnel_url:
                return tunnel_url
            local_port = mcp.get("tunnel", {}).get("local_port", 18765)
            path = mcp.get("path", "/mcp")
            return f"http://127.0.0.1:{local_port}{path}"

    # ── Auth resolver ─────────────────────────────────────────────────────────

    def resolve_auth(self, target_name: str | None = None) -> dict:
        """
        Return a copy of the target config with password_env replaced by the
        actual password from the environment.

        Raises EnvironmentError if password_env is set but the env var is missing.
        """
        t = self.get_target(target_name)
        ssh = dict(t.get("ssh", {}))
        auth = dict(ssh.get("auth", {}))

        if auth.get("type") == "password":
            penv = auth.get("password_env")
            if penv:
                password = os.environ.get(penv)
                if not password:
                    raise EnvironmentError(
                        f"Target '{target_name}': auth.password_env='{penv}' is set "
                        f"but environment variable '{penv}' is not defined"
                    )
                auth["password"] = password
                # remove the env reference — caller gets the actual password
                auth.pop("password_env", None)

        ssh["auth"] = auth
        return ssh

    # ── Convenience: full resolved target dict ────────────────────────────────

    def get_resolved_target(self, name: str | None = None) -> dict:
        """
        Return a fully resolved target config:
          - replaces password_env with actual password from env
          - adds mcp_url (built from ssh.host + mcp config)
        """
        t = self.get_target(name)
        ssh = self.resolve_auth(name)
        mcp_url = self.build_mcp_url(name)
        return {**t, "ssh": ssh, "_mcp_url": mcp_url}

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
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(SKELETON, f, indent=2, ensure_ascii=False)
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
            # Check for legacy schema and warn once during validation
            if "server" in raw or "connection" in raw:
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
            auth = ssh.get("auth", {})
            if auth.get("type") == "password":
                penv = auth.get("password_env")
                if penv and not os.environ.get(penv):
                    errors.append(
                        f"[{name}] auth.password_env='{penv}' is set but "
                        f"env var '{penv}' is not defined"
                    )

            # mcp
            mcp = t.get("mcp", {})
            port = mcp.get("port")
            if not isinstance(port, int) or not (1 < port < 65536):
                errors.append(f"[{name}] mcp.port must be an integer 1-65535")
            path = mcp.get("path", "/mcp")
            if not path.startswith("/"):
                errors.append(f"[{name}] mcp.path must start with '/'")

            # Platform-specific required fields
            errors.extend(_validate_platform_specific(t))

        return errors


# ── CLI entry point ─────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="EDR-WD target config tools")
    parser.add_argument("--config", help="Path to config file")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--list", action="store_true", help="List all targets")
    group.add_argument("--validate", action="store_true", help="Validate config")
    group.add_argument("--init", action="store_true", help="Initialize skeleton config")
    group.add_argument("--guide", action="store_true", help="Show a concise config setup guide")
    parser.add_argument("--force", action="store_true", help="Overwrite existing config with --init")
    args = parser.parse_args()

    tc = TargetConfig(args.config) if args.config else TargetConfig()

    if args.init:
        try:
            p = tc.init_config(args.config, force=args.force)
            print(f"Created: {p}")
            print("Next: run 'python -m agent.target_config --guide' for the setup walkthrough.")
        except FileExistsError as e:
            print(f"SKIP: {e}")
        sys.exit(0)

    if args.guide:
        _print_guide()
        sys.exit(0)

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
                print(f"  {name}{marker}  platform={platform}  profile={profile}  host={host}  desc={desc}")
        else:
            example = _find_example()
            if example:
                print("  (no targets.local.json found — showing targets.example.json for reference)")
                print(f"  Copy '{example}' to 'config/targets.local.json' and edit it.")
                print("  Then run: python -m agent.target_config --list")
        sys.exit(0)

    if args.validate:
        errs = tc.validate()
        if not errs:
            print("OK: config is valid")
            sys.exit(0)
        for e in errs:
            print(f"ERROR: {e}")
        sys.exit(1)

    # No action — show help
    parser.print_help()


if __name__ == "__main__":
    main()
