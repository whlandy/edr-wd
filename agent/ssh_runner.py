"""
ssh_runner.py — Pure SSH/SCP execution for EDR-WD.

Supports two auth backends:
  - key auth   → OpenSSH (ssh/scp with -i)
  - password auth → Paramiko (pure Python, no sshpass needed)

Password auth never appears in shell commands or logs — credentials are
passed directly to Paramiko's SSHClient.connect().

Interface:
    run_ssh(ssh_config, command, timeout=30) -> (exit_code, stdout_stderr)
    scp_to(ssh_config, local_path, remote_path, timeout=30) -> (exit_code, msg)
    scp_from(ssh_config, remote_path, local_path, timeout=30) -> (exit_code, msg)

ssh_config shape (password auth):
    {
        "host": "<TARGET_IP>",
        "port": 22,
        "user": "<TARGET_USER>",
        "auth": {
            "type": "password",
            "password": "<YOUR_PASSWORD>",          # or
            "password_env": "EDR_WD_TARGET_PASSWORD"  # env var name
        }
    }

ssh_config shape (key auth):
    {
        "host": "<TARGET_IP>",
        "port": 22,
        "user": "<TARGET_USER>",
        "auth": {
            "type": "key",
            "key_path": "~/.ssh/id_edr_wd"
        }
    }
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import Tuple

# Paramiko for password auth (pure Python, no sshpass needed on Windows)
try:
    import paramiko as _paramiko_mod
    paramiko = _paramiko_mod
    _PARAMIKO_AVAILABLE = True
except ImportError:
    paramiko = None
    _PARAMIKO_AVAILABLE = False


class SSHAuthError(ValueError):
    """Raised when ssh_config is missing required auth fields."""
    pass


class UnsupportedAuthType(SSHAuthError):
    """Raised when auth.type is not 'password' or 'key'."""
    pass


class ParamikoNotAvailable(SSHAuthError):
    """Raised when password auth is requested but Paramiko is not installed."""
    pass


def _get_password(ssh_config: dict) -> str:
    """Extract password from ssh_config, checking password_env first."""
    auth = ssh_config.get("auth", {})
    password_env = auth.get("password_env")
    if password_env:
        password = os.environ.get(password_env)
        if not password:
            raise SSHAuthError(
                "auth.password_env is set but the environment variable is not defined"
            )
        return password
    password = auth.get("password")
    if not password:
        raise SSHAuthError(
            "auth.type='password' but no password or password_env is configured"
        )
    return password


def _resolve_key_path(key_path: str) -> Path:
    """Expand ~ and environment variables in the key path."""
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


# ─── Remote path helpers ──────────────────────────────────────────────────────

def _remote_join(base: str, *parts: str) -> str:
    """
    Join path components for a remote SFTP path, always using forward slashes.
    Works for both Unix and Windows remote paths.

    Args:
        base: remote base path, e.g. 'C:\\Users\\admin\\Desktop' or '/home/user'
        *parts: additional path components

    Returns:
        Joined path with forward slashes, e.g. 'C:/Users/admin/Desktop/edr-wd/target'
    """
    # Normalize base: convert backslashes to forward slashes, strip trailing slash
    base = base.replace("\\", "/").rstrip("/")
    for p in parts:
        # Each part: replace backslashes, strip leading/trailing slashes
        segment = str(p).replace("\\", "/").strip("/")
        if segment:
            base = f"{base}/{segment}"
    return base


# ─── Paramiko SFTP helpers ───────────────────────────────────────────────────

def _paramiko_connect(ssh_config: dict, timeout: int = 10) -> paramiko.SSHClient:
    """Establish a Paramiko SSH connection. Caller must call .close()."""
    if not _PARAMIKO_AVAILABLE:
        raise ParamikoNotAvailable(
            "Paramiko is not installed. Install it with: pip install paramiko"
        )
    auth = ssh_config.get("auth", {})
    if auth.get("type") != "password":
        raise UnsupportedAuthType(
            f"Paramiko backend only supports auth.type='password', "
            f"got auth.type='{auth.get('type')}'. Use key auth for OpenSSH."
        )
    password = _get_password(ssh_config)
    host = ssh_config["host"]
    port = ssh_config.get("port", 22)
    user = ssh_config.get("user", "<TARGET_USER>")

    client = paramiko.SSHClient()  # type: ignore[union-attr]
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # type: ignore[union-attr]
    client.connect(
        hostname=host,
        port=port,
        username=user,
        password=password,
        timeout=timeout,
        look_for_keys=False,
        allow_agent=False,
    )
    return client


def _paramiko_run_ssh(ssh_config: dict, command: str, timeout: int = 30) -> Tuple[int, str]:
    """Run a command via Paramiko SSH. Returns (exit_code, combined_output)."""
    try:
        client = _paramiko_connect(ssh_config, timeout=timeout)
    except Exception:
        # Already a clean SSHAuthError — let it propagate
        raise

    try:
        # Use the high-level exec_command — it properly handles stdout/stderr
        # reading and blocks until the command exits.
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        exit_code = stdout.channel.recv_exit_status()
        return exit_code, out + err
    except Exception:
        # Do not leak exception details — they may contain host/user info
        return -1, "Paramiko command execution failed"
    finally:
        client.close()


def _paramiko_scp_to(ssh_config: dict, local_path: str | os.PathLike,
                      remote_path: str, timeout: int = 30) -> Tuple[int, str]:
    """Upload a file or directory to the remote host via SFTP. Returns (exit_code, msg)."""
    local = Path(local_path)
    if not local.exists():
        return -1, f"Local path does not exist: {local}"

    try:
        client = _paramiko_connect(ssh_config, timeout=timeout)
    except Exception as e:
        return -1, f"Paramiko connect failed: {e}"

    try:
        sftp = client.open_sftp()

        # Ensure remote parent directory exists by walking up the path
        # Use _remote_join to handle both Unix and Windows paths with slashes
        parts = remote_path.replace("\\", "/").rstrip("/").split("/")
        for i in range(1, len(parts) + 1):
            remote_dir = "/".join(parts[:i])
            try:
                sftp.stat(remote_dir)
            except IOError:
                try:
                    sftp.mkdir(remote_dir)
                except OSError:
                    pass  # may already exist

        _TEXT_EXTS = {".sh", ".py", ".ps1", ".bat", ".txt", ".json", ".xml", ".plist", ".yaml", ".yml", ".md", ".cfg", ".ini", ".toml"}

        def _should_strip_crlf(path: Path) -> bool:
            return path.suffix.lower() in _TEXT_EXTS

        if local.is_dir():
            # Upload directory recursively
            for item in local.rglob("*"):
                rel = item.relative_to(local)
                remote_item = _remote_join(remote_path, *rel.parts)
                if item.is_dir():
                    try:
                        sftp.mkdir(remote_item)
                    except IOError:
                        pass
                else:
                    if _should_strip_crlf(item):
                        content = item.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
                        with sftp.open(remote_item, "wb") as remote_f:
                            remote_f.write(content.encode("utf-8"))
                    else:
                        sftp.put(str(item), remote_item)
            return 0, "SFTP directory upload completed"
        else:
            # If remote_path is a directory (last component has no ext), append filename
            effective_remote = remote_path.rstrip("/")
            last = effective_remote.split("/")[-1]
            if "." not in last and "/" in effective_remote:
                effective_remote = f"{effective_remote}/{local.name}"
            if _should_strip_crlf(local):
                content = local.read_bytes()
                text_content = content.decode("utf-8", errors="replace").replace("\r\n", "\n")
                with sftp.open(effective_remote, "wb") as remote_f:
                    remote_f.write(text_content.encode("utf-8"))
            else:
                sftp.put(str(local), effective_remote)
            return 0, f"SFTP file uploaded to {effective_remote}"
    except Exception as e:
        # Do not leak local path details in error messages
        return -1, f"SFTP upload failed: {type(e).__name__}: {e}"
    finally:
        client.close()


def _paramiko_scp_from(ssh_config: dict, remote_path: str | os.PathLike,
                       local_path: str | os.PathLike, timeout: int = 30) -> Tuple[int, str]:
    """Download a file from the remote host via SFTP. Returns (exit_code, msg)."""
    local = Path(local_path)
    try:
        client = _paramiko_connect(ssh_config, timeout=timeout)
    except Exception:
        # Clean SSHAuthError — let it propagate
        raise

    try:
        sftp = client.open_sftp()
        try:
            sftp.stat(str(remote_path))
        except IOError:
            return -1, "Remote path not found"

        local.parent.mkdir(parents=True, exist_ok=True)
        sftp.get(str(remote_path), str(local))
        return 0, "SFTP download completed"
    except Exception:
        return -1, "SFTP download failed"
    finally:
        client.close()


# ─── OpenSSH helpers (key auth only) ───────────────────────────────────────

def _openssh_base(ssh_config: dict) -> list:
    """Build base ssh command for key auth. Raises if password auth requested."""
    auth = ssh_config.get("auth", {})
    if auth.get("type") == "password":
        raise UnsupportedAuthType(
            "OpenSSH backend does not support password auth. "
            "Install Paramiko (pip install paramiko) to use password auth, "
            "or switch to key auth."
        )
    if auth.get("type") != "key":
        raise UnsupportedAuthType(
            f"Unsupported auth.type='{auth.get('type')}'. Supported: 'password', 'key'"
        )
    key_path = auth.get("key_path", "~/.ssh/id_rsa")
    resolved = _resolve_key_path(key_path)
    host = ssh_config["host"]
    port = ssh_config.get("port", 22)
    user = ssh_config.get("user", "<TARGET_USER>")
    return [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "ConnectTimeout=10",
        "-i", str(resolved),
        "-p", str(port),
        f"{user}@{host}",
    ]


def _openssh_scp_base(ssh_config: dict) -> list:
    """Build base scp command for key auth."""
    auth = ssh_config.get("auth", {})
    if auth.get("type") != "key":
        raise UnsupportedAuthType(
            "OpenSSH SCP backend does not support password auth. "
            "Use Paramiko (pip install paramiko) for password auth."
        )
    key_path = auth.get("key_path", "~/.ssh/id_rsa")
    resolved = _resolve_key_path(key_path)
    host = ssh_config["host"]
    port = ssh_config.get("port", 22)
    user = ssh_config.get("user", "<TARGET_USER>")
    return [
        "scp",
        "-o", "StrictHostKeyChecking=no",
        "-o", f"Port={port}",
        "-o", "ConnectTimeout=10",
        "-i", str(resolved),
    ]


# ─── Public interface ────────────────────────────────────────────────────────

def run_ssh(ssh_config: dict, command: str, *, timeout: int = 30) -> Tuple[int, str]:
    """
    Run `command` on the remote host via SSH.

    - key auth → OpenSSH subprocess
    - password auth → Paramiko (no sshpass needed)

    Returns (exit_code, combined_stdout_stderr).
    """
    auth = ssh_config.get("auth", {})
    auth_type = auth.get("type")

    if auth_type == "password":
        return _paramiko_run_ssh(ssh_config, command, timeout=timeout)

    # key auth → OpenSSH
    try:
        cmd = _openssh_base(ssh_config) + [command]
    except UnsupportedAuthType:
        raise
    try:
        cp = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return cp.returncode, (cp.stdout + cp.stderr).decode("utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return -1, f"SSH command timed out after {timeout}s"
    except FileNotFoundError as e:
        missing = getattr(e, "filename", None)
        name = Path(missing).name if missing else "unknown"
        hint = " Ensure OpenSSH is installed and in PATH." if name in ("ssh", "scp") else ""
        return -1, f"Command not found: {missing or e}.{hint}"


def scp_to(ssh_config: dict, local_path: str | os.PathLike,
           remote_path: str, *, timeout: int = 30) -> Tuple[int, str]:
    """
    Upload local_path to remote_path on the target via SFTP (password auth)
    or SCP (key auth). Returns (exit_code, message).
    """
    auth = ssh_config.get("auth", {})
    auth_type = auth.get("type")

    if auth_type == "password":
        return _paramiko_scp_to(ssh_config, local_path, remote_path, timeout=timeout)

    # key auth → OpenSSH SCP
    try:
        base = _openssh_scp_base(ssh_config)
    except UnsupportedAuthType:
        raise
    cmd = base + [
        str(local_path),
        f"{ssh_config['user']}@{ssh_config['host']}:{remote_path}",
    ]
    try:
        cp = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if cp.returncode != 0:
            return cp.returncode, (cp.stdout + cp.stderr).decode("utf-8", errors="replace")
        return 0, f"Uploaded {local_path} to {remote_path}"
    except subprocess.TimeoutExpired:
        return -1, f"SCP upload timed out after {timeout}s"
    except FileNotFoundError as e:
        missing = getattr(e, "filename", None)
        name = Path(missing).name if missing else "unknown"
        hint = " Ensure OpenSSH is installed and in PATH." if name in ("ssh", "scp") else ""
        return -1, f"Command not found: {missing or e}.{hint}"


def scp_from(ssh_config: dict, remote_path: str | os.PathLike,
             local_path: str | os.PathLike, *, timeout: int = 30) -> Tuple[int, str]:
    """
    Download remote_path from the target to local_path via SFTP (password auth)
    or SCP (key auth). Returns (exit_code, message).
    """
    auth = ssh_config.get("auth", {})
    auth_type = auth.get("type")

    if auth_type == "password":
        return _paramiko_scp_from(ssh_config, remote_path, local_path, timeout=timeout)

    # key auth → OpenSSH SCP
    try:
        base = _openssh_scp_base(ssh_config)
    except UnsupportedAuthType:
        raise
    cmd = base + [
        f"{ssh_config['user']}@{ssh_config['host']}:{remote_path}",
        str(local_path),
    ]
    try:
        cp = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if cp.returncode != 0:
            return cp.returncode, (cp.stdout + cp.stderr).decode("utf-8", errors="replace")
        return 0, f"Downloaded {remote_path} to {local_path}"
    except subprocess.TimeoutExpired:
        return -1, f"SCP download timed out after {timeout}s"
    except FileNotFoundError as e:
        missing = getattr(e, "filename", None)
        name = Path(missing).name if missing else "unknown"
        hint = " Ensure OpenSSH is installed and in PATH." if name in ("ssh", "scp") else ""
        return -1, f"Command not found: {missing or e}.{hint}"


def scp_dir_to(ssh_config: dict, local_dir: str | os.PathLike,
                remote_dir: str, *, timeout: int = 60,
                tracked_only: bool = True) -> Tuple[int, str]:
    """
    Upload the contents of local_dir/ to remote_dir/, optionally restricting
    to files tracked by git (via ``git ls-files``).

    This prevents accidental upload of untracked debug scripts, __pycache__,
    logs, and local config files.

    Args:
        ssh_config:      SSH connection config
        local_dir:      Absolute path to the local source directory
        remote_dir:     Remote destination directory (e.g. ``C:/Users/foo/edr-wd``)
        timeout:        Per-file upload timeout in seconds
        tracked_only:   If True (default), only upload files returned by
                        ``git ls-files <local_dir>``. If False, upload the
                        entire directory tree.

    Returns (exit_code, message_summary).
    """
    local_dir = Path(local_dir).resolve()

    if tracked_only:
        try:
            cp = subprocess.run(
                ["git", "ls-files", "--", str(local_dir)],
                capture_output=True, timeout=10,
                cwd=local_dir.parent,
            )
            if cp.returncode == 0:
                raw = cp.stdout.decode("utf-8", errors="replace")
                rel_paths = [
                    line.strip()
                    for line in raw.splitlines()
                    if line.strip() and not line.startswith("#")
                ]
            else:
                return -1, f"git ls-files failed (rc={cp.returncode}): {cp.stderr.decode()[:200]}"
        except subprocess.TimeoutExpired:
            return -1, "git ls-files timed out"
        except FileNotFoundError:
            return -1, "git not found in PATH — cannot determine tracked files"

        if not rel_paths:
            return 0, f"No tracked files found under {local_dir}"
    else:
        # Upload everything under local_dir/
        rel_paths = []
        for item in local_dir.rglob("*"):
            if item.is_file():
                rel_paths.append(item.relative_to(local_dir).as_posix())

    # Upload each tracked file individually so one failure doesn't block others.
    # git returns paths like "target/__init__.py"; strip the top-level component
    # (the directory name, e.g. "target") to get the relative path within it.
    local_dir_name = local_dir.name          # e.g. "target"
    repo_root = local_dir.parent            # AGENT_ROOT
    failed = []
    for git_rel in rel_paths:
        # git_rel = "target/server.py"; strip the leading "target/" to get "server.py"
        inner = Path(git_rel)
        if inner.parts[0] != local_dir_name:
            failed.append(f"{git_rel}: does not start with {local_dir_name}/ — skipping")
            continue
        rel_within = "/".join(inner.parts[1:])  # "server.py" or "automation/base.py"
        src = repo_root / git_rel              # absolute local file
        dst = _remote_join(remote_dir, rel_within)
        if ssh_config.get("auth", {}).get("type") == "password":
            rc, msg = _paramiko_scp_to(ssh_config, str(src), dst, timeout=timeout)
        else:
            rc, msg = _scp_file_key(ssh_config, str(src), dst, timeout=timeout)
        if rc != 0:
            failed.append(f"{git_rel} → {dst}: {msg[:80]}")

    if failed:
        return -1, f"Failed to upload {len(failed)} file(s): " + "; ".join(failed[:3])
    return 0, f"Uploaded {len(rel_paths)} tracked file(s) to {remote_dir}"


def _scp_file_key(ssh_config: dict, local_path: str, remote_path: str,
                   timeout: int = 30) -> Tuple[int, str]:
    """Upload a single file via OpenSSH scp (key auth only)."""
    try:
        base = _openssh_scp_base(ssh_config)
    except UnsupportedAuthType:
        raise
    cmd = base + [str(local_path), f"{ssh_config['user']}@{ssh_config['host']}:{remote_path}"]
    try:
        cp = subprocess.run(cmd, capture_output=True, timeout=timeout)
        if cp.returncode != 0:
            return cp.returncode, (cp.stdout + cp.stderr).decode("utf-8", errors="replace")
        return 0, ""
    except subprocess.TimeoutExpired:
        return -1, f"SCP timed out after {timeout}s"
    except FileNotFoundError as e:
        missing = getattr(e, "filename", None)
        return -1, f"scp not found: {missing or e}"
