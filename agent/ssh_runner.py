"""
ssh_runner.py — Pure SSH/SCP execution for EDR-WD.

Takes a fully-resolved ssh_config dict (host, port, user, auth) and runs
commands or transfers files. Knows nothing about config files or target names.

Interface:
    run_ssh(ssh_config, command, timeout=30) -> (exit_code, stdout_stderr)
    scp_to(ssh_config, local_path, remote_path, timeout=30) -> (exit_code, "")
    scp_from(ssh_config, remote_path, local_path, timeout=30) -> (exit_code, "")

ssh_config shape (password auth):
    {
        "host": "170.170.11.26",
        "port": 22,
        "user": "admin",
        "auth": {
            "type": "password",
            "password": "whl@123"
        }
    }

ssh_config shape (key auth):
    {
        "host": "170.170.11.26",
        "port": 22,
        "user": "admin",
        "auth": {
            "type": "key",
            "key_path": "~/.ssh/id_edr_wd"
        }
    }

Supported auth.type: "password", "key"
If auth.type is unsupported, all functions raise ValueError.
If auth.password is missing (password auth), raises ValueError.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Tuple


class SSHAuthError(ValueError):
    """Raised when ssh_config is missing required auth fields."""
    pass


class UnsupportedAuthType(SSHAuthError):
    """Raised when auth.type is not 'password' or 'key'."""
    pass


def _resolve_key_path(key_path: str) -> Path:
    # Expand ~ and environment variables ($VAR, ${VAR}, %VAR%) in the key path.
    # Supports both Unix and Windows formats.
    def _expand_once(p: str) -> str:
        def _replace(m: re.Match) -> str:
            name = m.group(1) or ""
            return os.environ.get(name, m.group(0)) or m.group(0)
        p = re.sub(r"%([^%]+)%", _replace, p)
        return os.path.expandvars(p)

    expanded = _expand_once(key_path)
    p = Path(expanded).expanduser()
    if not p.exists():
        raise SSHAuthError(f"SSH key not found: {p}")
    return p


def _build_ssh_base(ssh_config: dict) -> list:
    """Build the base ssh command prefix (no auth type check — caller decides)."""
    auth = ssh_config.get("auth", {})
    host = ssh_config["host"]
    port = ssh_config.get("port", 22)
    user = ssh_config.get("user", "admin")
    auth_type = auth.get("type")

    if auth_type == "password":
        password = auth.get("password")
        if not password:
            raise SSHAuthError(
                f"auth.type='password' but auth.password is missing for {user}@{host}"
            )
        return [
            "sshpass", "-p", password,
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            "-p", str(port),
            f"{user}@{host}",
        ]
    elif auth_type == "key":
        key_path = auth.get("key_path", "~/.ssh/id_rsa")
        resolved = _resolve_key_path(key_path)
        return [
            "ssh",
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=10",
            "-i", str(resolved),
            "-p", str(port),
            f"{user}@{host}",
        ]
    else:
        raise UnsupportedAuthType(
            f"Unsupported auth.type='{auth_type}'. Supported: 'password', 'key'"
        )


def _build_scp_base(ssh_config: dict, direction: str) -> list:
    """Build the base scp command prefix."""
    auth = ssh_config.get("auth", {})
    host = ssh_config["host"]
    port = ssh_config.get("port", 22)
    user = ssh_config.get("user", "admin")
    auth_type = auth.get("type")

    if auth_type == "password":
        password = auth.get("password")
        if not password:
            raise SSHAuthError(
                f"auth.type='password' but auth.password is missing for {user}@{host}"
            )
        base = ["sshpass", "-p", password, "scp"]
    elif auth_type == "key":
        key_path = auth.get("key_path", "~/.ssh/id_rsa")
        resolved = _resolve_key_path(key_path)
        base = ["scp", "-i", str(resolved)]
    else:
        raise UnsupportedAuthType(
            f"Unsupported auth.type='{auth_type}'. Supported: 'password', 'key'"
        )

    return base + [
        "-o", "StrictHostKeyChecking=no",
        "-o", f"Port={port}",
        "-o", "ConnectTimeout=10",
    ]


def run_ssh(ssh_config: dict, command: str, *, timeout: int = 30) -> Tuple[int, str]:
    """Run `command` on the remote host via SSH. Returns (exit_code, combined_output)."""
    try:
        cmd = _build_ssh_base(ssh_config) + [command]
    except SSHAuthError:
        raise
    try:
        cp = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return cp.returncode, (cp.stdout + cp.stderr).decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return -1, f"SSH command timed out after {timeout}s"
    except FileNotFoundError as e:
        # e.filename is the missing command, e.g. 'sshpass' or '/usr/bin/ssh'
        missing = getattr(e, "filename", None)
        name = Path(missing).name if missing else "unknown"
        hint = ""
        if name == "sshpass":
            if os.name == "nt":
                hint = " sshpass is not available on Windows agent. Use key auth instead (set auth.type='key' in config)."
            else:
                hint = " Install sshpass: brew install sshpass (macOS) or apt install sshpass (Ubuntu/Debian)."
        elif name in ("ssh", "scp"):
            hint = " Ensure OpenSSH is installed and in PATH."
        return -1, f"Command not found: {missing or e}.{hint}"


def scp_to(ssh_config: dict, local_path: str | os.PathLike,
           remote_path: str, *, timeout: int = 30) -> Tuple[int, str]:
    """Upload local_path to remote_path on the target. Returns (exit_code, "")."""
    try:
        cmd = _build_scp_base(ssh_config, "to") + [
            str(local_path),
            f"{ssh_config['user']}@{ssh_config['host']}:{remote_path}",
        ]
    except SSHAuthError:
        raise
    try:
        cp = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return cp.returncode, (cp.stdout + cp.stderr).decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return -1, f"SCP upload timed out after {timeout}s"
    except FileNotFoundError as e:
        # e.filename is the missing command, e.g. 'sshpass' or '/usr/bin/ssh'
        missing = getattr(e, "filename", None)
        name = Path(missing).name if missing else "unknown"
        hint = ""
        if name == "sshpass":
            if os.name == "nt":
                hint = " sshpass is not available on Windows agent. Use key auth instead (set auth.type='key' in config)."
            else:
                hint = " Install sshpass: brew install sshpass (macOS) or apt install sshpass (Ubuntu/Debian)."
        elif name in ("ssh", "scp"):
            hint = " Ensure OpenSSH is installed and in PATH."
        return -1, f"Command not found: {missing or e}.{hint}"


def scp_from(ssh_config: dict, remote_path: str | os.PathLike,
             local_path: str | os.PathLike, *, timeout: int = 30) -> Tuple[int, str]:
    """Download remote_path from the target to local_path. Returns (exit_code, "")."""
    try:
        cmd = _build_scp_base(ssh_config, "from") + [
            f"{ssh_config['user']}@{ssh_config['host']}:{remote_path}",
            str(local_path),
        ]
    except SSHAuthError:
        raise
    try:
        cp = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return cp.returncode, (cp.stdout + cp.stderr).decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return -1, f"SCP download timed out after {timeout}s"
    except FileNotFoundError as e:
        # e.filename is the missing command, e.g. 'sshpass' or '/usr/bin/ssh'
        missing = getattr(e, "filename", None)
        name = Path(missing).name if missing else "unknown"
        hint = ""
        if name == "sshpass":
            if os.name == "nt":
                hint = " sshpass is not available on Windows agent. Use key auth instead (set auth.type='key' in config)."
            else:
                hint = " Install sshpass: brew install sshpass (macOS) or apt install sshpass (Ubuntu/Debian)."
        elif name in ("ssh", "scp"):
            hint = " Ensure OpenSSH is installed and in PATH."
        return -1, f"Command not found: {missing or e}.{hint}"
