"""
ssh_runner.py — Pure SSH/SCP execution for EDR-WD.

Takes a fully-resolved ssh_config dict (host, port, user, auth) and runs
commands or transfers files. Knows nothing about config files or target names.

Interface:
    run_ssh(ssh_config, command, timeout=30) -> (exit_code, stdout_stderr)
    scp_to(ssh_config, local_path, remote_path, timeout=30) -> (exit_code, "")
    scp_from(ssh_config, remote_path, local_path, timeout=30) -> (exit_code, "")

ssh_config shape:
    {
        "host": "170.170.11.26",
        "port": 22,
        "user": "admin",
        "auth": {
            "type": "password",
            "password": "whl@123"
        }
    }
"""

from __future__ import annotations

import subprocess
import os
from typing import Tuple


def _ssh_base_cmd(ssh_config: dict) -> list:
    """Build the base sshpass+ssh command prefix."""
    auth = ssh_config.get("auth", {})
    host = ssh_config["host"]
    port = ssh_config.get("port", 22)
    user = ssh_config.get("user", "admin")

    cmd = ["sshpass", "-p", auth["password"],
           "ssh",
           "-o", "StrictHostKeyChecking=no",
           "-o", "ConnectTimeout=10",
           "-p", str(port),
           f"{user}@{host}"]
    return cmd


def run_ssh(ssh_config: dict, command: str, *, timeout: int = 30) -> Tuple[int, str]:
    """
    Run `command` on the remote host via SSH.
    Returns (exit_code, combined_stdout_stderr).
    """
    cmd = _ssh_base_cmd(ssh_config) + [command]
    try:
        cp = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return cp.returncode, (cp.stdout + cp.stderr).decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return -1, f"SSH command timed out after {timeout}s"
    except FileNotFoundError as e:
        return -1, f"Command not found: {e}"


def scp_to(ssh_config: dict, local_path: str | os.PathLike,
           remote_path: str, *, timeout: int = 30) -> Tuple[int, str]:
    """
    Upload local_path to remote_path on the target.
    Returns (exit_code, "").
    """
    auth = ssh_config.get("auth", {})
    host = ssh_config["host"]
    port = ssh_config.get("port", 22)
    user = ssh_config.get("user", "admin")

    cmd = [
        "sshpass", "-p", auth["password"],
        "scp",
        "-o", "StrictHostKeyChecking=no",
        "-o", f"Port={port}",
        "-o", "ConnectTimeout=10",
        str(local_path),
        f"{user}@{host}:{remote_path}",
    ]
    try:
        cp = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return cp.returncode, (cp.stdout + cp.stderr).decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return -1, f"SCP upload timed out after {timeout}s"
    except FileNotFoundError as e:
        return -1, f"Command not found: {e}"


def scp_from(ssh_config: dict, remote_path: str | os.PathLike,
             local_path: str | os.PathLike, *, timeout: int = 30) -> Tuple[int, str]:
    """
    Download remote_path from the target to local_path.
    Returns (exit_code, "").
    """
    auth = ssh_config.get("auth", {})
    host = ssh_config["host"]
    port = ssh_config.get("port", 22)
    user = ssh_config.get("user", "admin")

    cmd = [
        "sshpass", "-p", auth["password"],
        "scp",
        "-o", "StrictHostKeyChecking=no",
        "-o", f"Port={port}",
        "-o", "ConnectTimeout=10",
        f"{user}@{host}:{remote_path}",
        str(local_path),
    ]
    try:
        cp = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return cp.returncode, (cp.stdout + cp.stderr).decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return -1, f"SCP download timed out after {timeout}s"
    except FileNotFoundError as e:
        return -1, f"Command not found: {e}"
