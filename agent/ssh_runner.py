"""
ssh_runner.py — Paramiko-based SSH/SFTP execution for EDR-WD.

All SSH command execution and file transfer goes through Paramiko:
  - password auth → Paramiko password
  - key auth      → Paramiko key_filename

Passwords never appear in shell commands or logs — credentials are passed
directly to Paramiko's SSHClient.connect().

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
            "password": "<YOUR_PASSWORD>",          # preferred
            "password_env": "EDR_WD_TARGET_PASSWORD"  # optional fallback
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
import socket
import subprocess
from pathlib import Path
from typing import Tuple

# Paramiko is the single SSH/SFTP transport for agent-target operations.
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
    """Raised when SSH/SFTP is requested but Paramiko is not installed."""
    pass


def _get_password(ssh_config: dict) -> str:
    """Extract password from ssh_config, preferring inline auth.password."""
    auth = ssh_config.get("auth", {})
    password = auth.get("password")
    if password:
        return password
    password_env = auth.get("password_env")
    if password_env:
        password = os.environ.get(password_env)
        if not password:
            raise SSHAuthError(
                "auth.password_env is set but the environment variable is not defined"
            )
        return password
    raise SSHAuthError(
        "auth.type='password' but no password or password_env is configured"
    )


def _get_optional_passphrase(ssh_config: dict) -> str | None:
    """Extract an optional key passphrase from ssh_config."""
    auth = ssh_config.get("auth", {})
    passphrase = auth.get("passphrase")
    if passphrase:
        return passphrase
    passphrase_env = auth.get("passphrase_env")
    if passphrase_env:
        value = os.environ.get(passphrase_env)
        if not value:
            raise SSHAuthError(
                "auth.passphrase_env is set but the environment variable is not defined"
            )
        return value
    return None


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


def _remote_parent(path: str) -> str:
    """Return the remote parent directory, preserving slash-only remote paths."""
    normalized = path.replace("\\", "/").rstrip("/")
    if "/" not in normalized:
        return ""
    return normalized.rsplit("/", 1)[0]


_EXCLUDED_DIRS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "logs",
    "screenshots",
}
_EXCLUDED_NAMES = {
    ".DS_Store",
    "config.json",
    "targets.json",
    "targets.local.json",
    "test_machines.json",
}
_EXCLUDED_SUFFIXES = {".log", ".pyc", ".pyo", ".tmp"}


def _is_safe_fallback_file(path: Path, root: Path) -> bool:
    """Return whether a file is safe to upload when git metadata is unavailable."""
    rel = path.relative_to(root)
    if any(part in _EXCLUDED_DIRS for part in rel.parts):
        return False
    if path.name in _EXCLUDED_NAMES:
        return False
    if path.suffix.lower() in _EXCLUDED_SUFFIXES:
        return False
    return True


def _fallback_rel_paths(local_dir: Path) -> list[str]:
    """Enumerate deployable files without git, applying conservative exclusions."""
    rel_paths: list[str] = []
    for item in local_dir.rglob("*"):
        if item.is_file() and _is_safe_fallback_file(item, local_dir):
            rel_paths.append(item.relative_to(local_dir).as_posix())
    return sorted(rel_paths)


# ─── Paramiko SFTP helpers ───────────────────────────────────────────────────

def _paramiko_connect(ssh_config: dict, timeout: int = 10) -> paramiko.SSHClient:
    """Establish a Paramiko SSH connection. Caller must call .close()."""
    if not _PARAMIKO_AVAILABLE:
        raise ParamikoNotAvailable(
            "Paramiko is not installed. Install it with: pip install paramiko"
        )
    auth = ssh_config.get("auth", {})
    auth_type = auth.get("type")
    if auth_type not in ("password", "key"):
        raise UnsupportedAuthType(
            f"Unsupported auth.type='{auth_type}'. Supported: 'password', 'key'"
        )

    host = ssh_config["host"]
    port = ssh_config.get("port", 22)
    user = ssh_config.get("user", "<TARGET_USER>")
    connect_kwargs = {
        "hostname": host,
        "port": port,
        "username": user,
        "timeout": timeout,
        "look_for_keys": False,
        "allow_agent": False,
    }
    if auth_type == "password":
        connect_kwargs["password"] = _get_password(ssh_config)
    else:
        key_path = auth.get("key_path", "~/.ssh/id_rsa")
        connect_kwargs["key_filename"] = str(_resolve_key_path(key_path))
        passphrase = _get_optional_passphrase(ssh_config)
        if passphrase:
            connect_kwargs["passphrase"] = passphrase

    client = paramiko.SSHClient()  # type: ignore[union-attr]
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())  # type: ignore[union-attr]
    client.connect(**connect_kwargs)
    return client


def _paramiko_run_ssh(ssh_config: dict, command: str, timeout: int = 30) -> Tuple[int, str]:
    """Run a command via Paramiko SSH. Returns (exit_code, combined_output).

    Connection and authentication failures propagate: they are the caller's to
    handle and are already free of credentials. Only command execution is
    reduced to a return value, and it names which kind of failure occurred —
    collapsing a timeout, a dropped channel and a protocol error into one
    opaque string makes an operator debug the wrong thing. The channel carries
    `timeout` (paramiko applies it to reads), so a command that stops producing
    output does return rather than hang; it is an inactivity timeout, not a
    wall-clock deadline, so a command that keeps emitting output can still run
    longer than `timeout`.
    """
    client = _paramiko_connect(ssh_config, timeout=timeout)
    try:
        stdin, stdout, stderr = client.exec_command(command, timeout=timeout)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        exit_code = stdout.channel.recv_exit_status()
        return exit_code, out + err
    except socket.timeout:
        return -1, f"ssh command produced no output for {timeout}s"
    except Exception as exc:
        # The type is diagnostic; the message can carry host/user details.
        return -1, f"ssh command execution failed: {type(exc).__name__}"
    finally:
        client.close()


def _paramiko_scp_to(ssh_config: dict, local_path: str | os.PathLike,
                      remote_path: str, timeout: int = 30,
                      preserve_bytes: bool = False,
                      overwrite: bool = True) -> Tuple[int, str]:
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

        def _ensure_remote_dir(remote_dir: str) -> None:
            remote_dir = remote_dir.replace("\\", "/").rstrip("/")
            if not remote_dir:
                return
            parts = remote_dir.split("/")
            for i in range(1, len(parts) + 1):
                current = "/".join(parts[:i])
                if not current:
                    continue
                try:
                    sftp.stat(current)
                except IOError:
                    try:
                        sftp.mkdir(current)
                    except OSError:
                        pass  # may already exist or be a drive/root component

        def _resolve_remote_file(local_file: Path, remote: str) -> str:
            effective = remote.replace("\\", "/").rstrip("/")
            last = effective.split("/")[-1] if effective else ""
            if remote.endswith(("/", "\\")) or ("." not in last and "/" in effective):
                effective = _remote_join(effective, local_file.name)
            return effective

        def _write_file(local_file: Path, remote_file: str) -> None:
            _ensure_remote_dir(_remote_parent(remote_file))
            if not overwrite:
                try:
                    sftp.stat(remote_file)
                except IOError:
                    pass
                else:
                    raise FileExistsError("Remote destination already exists")
            if not preserve_bytes and _should_strip_crlf(local_file):
                content = local_file.read_bytes()
                text_content = content.decode("utf-8", errors="replace").replace("\r\n", "\n")
                with sftp.open(remote_file, "wb") as remote_f:
                    remote_f.write(text_content.encode("utf-8"))
            else:
                sftp.put(str(local_file), remote_file)

        _TEXT_EXTS = {".sh", ".py", ".ps1", ".bat", ".txt", ".json", ".xml", ".plist", ".yaml", ".yml", ".md", ".cfg", ".ini", ".toml"}

        def _should_strip_crlf(path: Path) -> bool:
            return path.suffix.lower() in _TEXT_EXTS

        if local.is_dir():
            _ensure_remote_dir(remote_path)
            # Upload directory recursively
            for item in local.rglob("*"):
                rel = item.relative_to(local)
                remote_item = _remote_join(remote_path, *rel.parts)
                if item.is_dir():
                    _ensure_remote_dir(remote_item)
                else:
                    _write_file(item, remote_item)
            return 0, "SFTP directory upload completed"
        else:
            effective_remote = _resolve_remote_file(local, remote_path)
            _write_file(local, effective_remote)
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


# ─── Public interface ────────────────────────────────────────────────────────

def run_ssh(ssh_config: dict, command: str, *, timeout: int = 30) -> Tuple[int, str]:
    """
    Run `command` on the remote host via SSH.

    - password auth → Paramiko password
    - key auth → Paramiko key_filename

    Returns (exit_code, combined_stdout_stderr).
    """
    return _paramiko_run_ssh(ssh_config, command, timeout=timeout)


def scp_to(ssh_config: dict, local_path: str | os.PathLike,
           remote_path: str, *, timeout: int = 30,
           preserve_bytes: bool = False,
           overwrite: bool = True) -> Tuple[int, str]:
    """
    Upload local_path to remote_path on the target via Paramiko SFTP.
    Returns (exit_code, message).
    """
    if preserve_bytes or not overwrite:
        return _paramiko_scp_to(
            ssh_config,
            local_path,
            remote_path,
            timeout=timeout,
            preserve_bytes=preserve_bytes,
            overwrite=overwrite,
        )
    return _paramiko_scp_to(ssh_config, local_path, remote_path, timeout=timeout)


def scp_from(ssh_config: dict, remote_path: str | os.PathLike,
             local_path: str | os.PathLike, *, timeout: int = 30) -> Tuple[int, str]:
    """
    Download remote_path from the target to local_path via Paramiko SFTP.
    Returns (exit_code, message).
    """
    return _paramiko_scp_from(ssh_config, remote_path, local_path, timeout=timeout)


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

    used_fallback = False
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
                err = cp.stderr.decode("utf-8", errors="replace")
                if "not a git repository" in err.lower():
                    rel_paths = _fallback_rel_paths(local_dir)
                    used_fallback = True
                else:
                    return -1, f"git ls-files failed (rc={cp.returncode}): {err[:200]}"
        except subprocess.TimeoutExpired:
            return -1, "git ls-files timed out"
        except FileNotFoundError:
            rel_paths = _fallback_rel_paths(local_dir)
            used_fallback = True

        if not rel_paths:
            mode = "fallback files" if used_fallback else "tracked files"
            return 0, f"No {mode} found under {local_dir}"
    else:
        # Upload everything under local_dir/
        rel_paths = []
        for item in local_dir.rglob("*"):
            if item.is_file():
                rel_paths.append(item.relative_to(local_dir).as_posix())

    # Upload each file individually so one failure doesn't block others.
    # git returns paths like "target/__init__.py"; fallback paths are already
    # relative to local_dir, e.g. "__init__.py".
    local_dir_name = local_dir.name          # e.g. "target"
    repo_root = local_dir.parent            # AGENT_ROOT
    failed = []
    for rel_path in rel_paths:
        inner = Path(rel_path)
        if used_fallback or not tracked_only:
            rel_within = inner.as_posix()
            src = local_dir / inner
        else:
            # git_rel = "target/server.py"; strip the leading "target/".
            if not inner.parts or inner.parts[0] != local_dir_name:
                failed.append(f"{rel_path}: does not start with {local_dir_name}/ — skipping")
                continue
            rel_within = "/".join(inner.parts[1:])
            src = repo_root / inner
        dst = _remote_join(remote_dir, rel_within)
        rc, msg = _paramiko_scp_to(ssh_config, str(src), dst, timeout=timeout)
        if rc != 0:
            failed.append(f"{rel_path} → {dst}: {msg[:80]}")

    if failed:
        return -1, f"Failed to upload {len(failed)} file(s): " + "; ".join(failed[:3])
    mode = "fallback" if used_fallback else "tracked"
    return 0, f"Uploaded {len(rel_paths)} {mode} file(s) to {remote_dir}"
