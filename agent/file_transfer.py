"""Agent-side file transfer with MCP primary and Paramiko SFTP fallback."""

from __future__ import annotations

import base64
import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath

from agent.ssh_runner import scp_from, scp_to
from agent.subagent.target_agent import TargetSubAgent
from agent.target_config import TargetConfig


DEFAULT_CHUNK_BYTES = 256 * 1024
MAX_CHUNK_BYTES = 1024 * 1024


def _relative_path(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("remote relative path must be a non-empty string")
    normalized = value.strip().replace("\\", "/")
    if normalized.startswith("/") or normalized.startswith("//") or re.match(
        r"^[A-Za-z]:", normalized
    ):
        raise ValueError("remote path must be relative to the transfer root")
    pure = PurePosixPath(normalized)
    if any(part in ("", ".", "..") for part in pure.parts):
        raise ValueError("remote path traversal is not allowed")
    return pure.as_posix()


def _remote_root(cfg: dict) -> str:
    explicit = cfg.get("file_transfer", {}).get("remote_root")
    if explicit:
        return str(explicit).replace("\\", "/").rstrip("/")
    user = str(cfg.get("ssh", {}).get("user", ""))
    if not user or any(char in user for char in "/\\"):
        raise ValueError(
            "file_transfer.remote_root is required when ssh.user cannot define a home directory"
        )
    if cfg.get("platform", "windows") == "windows":
        return f"C:/Users/{user}/Desktop/edr-wd-record/transfers"
    return f"/Users/{user}/Desktop/edr-wd-record/transfers"


def _remote_path(cfg: dict, relative_path: str) -> str:
    return f"{_remote_root(cfg)}/{_relative_path(relative_path)}"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _fallback_reason(result: dict) -> str | None:
    """Return a reason only for transport/tool availability failures."""
    if result.get("ok"):
        return None
    if result.get("error_code"):
        return None
    text = str(result.get("error") or result).lower()
    markers = (
        "mcp initialize failed",
        "method not found",
        "tool not found",
        "unknown tool",
        "session",
        "connection",
        "timeout",
        "timed out",
        "transport",
    )
    return text[:300] if any(marker in text for marker in markers) else None


def _scp_upload(
    cfg: dict,
    ssh_config: dict,
    local_path: Path,
    relative_path: str,
    *,
    timeout: int,
    fallback_reason: str,
    overwrite: bool,
) -> dict:
    remote = _remote_path(cfg, relative_path)
    remote_parent, remote_name = remote.rsplit("/", 1)
    with tempfile.TemporaryDirectory(prefix="edr-wd-transfer-") as temp_dir:
        staged = Path(temp_dir) / remote_name
        shutil.copy2(local_path, staged)
        rc, message = scp_to(
            ssh_config,
            staged,
            remote_parent + "/",
            timeout=timeout,
            preserve_bytes=True,
            overwrite=overwrite,
        )
    result = {
        "ok": rc == 0,
        "transport": "scp",
        "fallback_reason": fallback_reason,
        "relative_path": _relative_path(relative_path),
        "size": local_path.stat().st_size,
        "sha256": _sha256(local_path) if rc == 0 else None,
        "error": None if rc == 0 else message,
    }
    if rc != 0 and "FileExistsError" in message:
        result["error_code"] = "file_exists"
    return result


def upload_file(
    target: str,
    local_path: str | os.PathLike,
    relative_path: str,
    *,
    config: TargetConfig | None = None,
    overwrite: bool = False,
    allow_scp_fallback: bool = True,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    timeout: int = 60,
) -> dict:
    """Upload a local file over MCP, falling back to SCP/SFTP if unavailable."""
    local = Path(local_path)
    if not local.is_file():
        return {"ok": False, "error_code": "local_file_not_found", "error": str(local)}
    try:
        relative = _relative_path(relative_path)
    except ValueError as exc:
        return {"ok": False, "error_code": "invalid_path", "error": str(exc)}
    if not 1 <= chunk_bytes <= MAX_CHUNK_BYTES:
        return {"ok": False, "error_code": "invalid_chunk_size", "error": f"chunk_bytes must be 1..{MAX_CHUNK_BYTES}"}

    tc = config or TargetConfig()
    cfg = tc.get_target(target)
    agent = TargetSubAgent(target, config=tc)
    ready = agent.ensure_ready()
    if not ready.get("ok"):
        reason = _fallback_reason(ready)
        if not allow_scp_fallback or reason is None:
            return ready
        return _scp_upload(
            cfg, tc.resolve_auth(target), local, relative,
            timeout=timeout, fallback_reason=reason, overwrite=overwrite,
        )

    digest = _sha256(local)
    size = local.stat().st_size
    offset = 0
    first = True
    with local.open("rb") as handle:
        while first or offset < size:
            payload = handle.read(chunk_bytes)
            final = offset + len(payload) >= size
            result = agent.call_tool(
                "transfer_upload",
                {
                    "relative_path": relative,
                    "content_base64": base64.b64encode(payload).decode("ascii"),
                    "offset": offset,
                    "overwrite": overwrite if first else False,
                    "final": final,
                    "expected_sha256": digest if final else None,
                },
                timeout=timeout,
            )
            if not result.get("ok"):
                reason = _fallback_reason(result)
                if not allow_scp_fallback or reason is None:
                    return result
                return _scp_upload(
                    cfg, tc.resolve_auth(target), local, relative,
                    timeout=timeout, fallback_reason=reason, overwrite=overwrite,
                )
            offset = int(result.get("next_offset", offset + len(payload)))
            first = False
    return {
        "ok": True,
        "transport": "mcp",
        "relative_path": relative,
        "size": size,
        "sha256": digest,
    }


def _scp_download(
    cfg: dict,
    ssh_config: dict,
    relative_path: str,
    local_path: Path,
    *,
    timeout: int,
    fallback_reason: str,
) -> dict:
    remote = _remote_path(cfg, relative_path)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    rc, message = scp_from(ssh_config, remote, local_path, timeout=timeout)
    return {
        "ok": rc == 0,
        "transport": "scp",
        "fallback_reason": fallback_reason,
        "relative_path": _relative_path(relative_path),
        "local_path": str(local_path),
        "size": local_path.stat().st_size if rc == 0 else None,
        "sha256": _sha256(local_path) if rc == 0 else None,
        "error": None if rc == 0 else message,
    }


def download_file(
    target: str,
    relative_path: str,
    local_path: str | os.PathLike,
    *,
    config: TargetConfig | None = None,
    overwrite: bool = False,
    allow_scp_fallback: bool = True,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    timeout: int = 60,
) -> dict:
    """Download a target transfer file over MCP with SCP/SFTP fallback."""
    local = Path(local_path)
    if local.exists() and not overwrite:
        return {"ok": False, "error_code": "local_file_exists", "error": str(local)}
    try:
        relative = _relative_path(relative_path)
    except ValueError as exc:
        return {"ok": False, "error_code": "invalid_path", "error": str(exc)}
    if not 1 <= chunk_bytes <= MAX_CHUNK_BYTES:
        return {"ok": False, "error_code": "invalid_chunk_size", "error": f"chunk_bytes must be 1..{MAX_CHUNK_BYTES}"}

    tc = config or TargetConfig()
    cfg = tc.get_target(target)
    agent = TargetSubAgent(target, config=tc)
    ready = agent.ensure_ready()
    if not ready.get("ok"):
        reason = _fallback_reason(ready)
        if not allow_scp_fallback or reason is None:
            return ready
        return _scp_download(
            cfg, tc.resolve_auth(target), relative, local,
            timeout=timeout, fallback_reason=reason,
        )

    local.parent.mkdir(parents=True, exist_ok=True)
    staging = local.with_name(f".{local.name}.edr-wd-download")
    offset = 0
    expected_sha256 = None
    try:
        with staging.open("wb") as handle:
            while True:
                result = agent.call_tool(
                    "transfer_download",
                    {"relative_path": relative, "offset": offset, "max_bytes": chunk_bytes},
                    timeout=timeout,
                )
                if not result.get("ok"):
                    reason = _fallback_reason(result)
                    if not allow_scp_fallback or reason is None:
                        return result
                    handle.close()
                    staging.unlink(missing_ok=True)
                    return _scp_download(
                        cfg, tc.resolve_auth(target), relative, local,
                        timeout=timeout, fallback_reason=reason,
                    )
                payload = base64.b64decode(result["content_base64"], validate=True)
                handle.write(payload)
                offset = int(result["next_offset"])
                if result.get("eof"):
                    expected_sha256 = result.get("sha256")
                    break
            handle.flush()
            os.fsync(handle.fileno())
        digest = _sha256(staging)
        if expected_sha256 and digest.lower() != str(expected_sha256).lower():
            staging.unlink(missing_ok=True)
            return {"ok": False, "error_code": "checksum_mismatch", "error": "downloaded file SHA-256 mismatch"}
        os.replace(staging, local)
        return {
            "ok": True,
            "transport": "mcp",
            "relative_path": relative,
            "local_path": str(local),
            "size": offset,
            "sha256": digest,
        }
    finally:
        if staging.exists():
            staging.unlink()
