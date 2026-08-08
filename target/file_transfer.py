"""Sandboxed, chunked file transfer helpers for target-side MCP tools."""

from __future__ import annotations

import base64
import binascii
import hashlib
import os
import re
from pathlib import Path, PurePosixPath


DEFAULT_CHUNK_BYTES = 256 * 1024
MAX_CHUNK_BYTES = 1024 * 1024
MAX_FILE_BYTES = 100 * 1024 * 1024


class TransferError(ValueError):
    """A stable, user-correctable transfer error."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def transfer_root() -> Path:
    """Return the only directory exposed by MCP file transfer tools."""
    record_root = Path(
        os.environ.get(
            "EDR_WD_RECORD_DIR", Path.home() / "Desktop" / "edr-wd-record"
        )
    )
    root = Path(os.environ.get("EDR_WD_TRANSFER_DIR", record_root / "transfers"))
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _safe_path(relative_path: str) -> tuple[Path, str]:
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise TransferError("invalid_path", "relative_path must be a non-empty string")
    normalized = relative_path.strip().replace("\\", "/")
    if normalized.startswith("/") or normalized.startswith("//"):
        raise TransferError("path_outside_transfer_root", "absolute paths are not allowed")
    if re.match(r"^[A-Za-z]:", normalized):
        raise TransferError("path_outside_transfer_root", "drive-qualified paths are not allowed")
    pure = PurePosixPath(normalized)
    if any(part in ("", ".", "..") for part in pure.parts):
        raise TransferError("path_outside_transfer_root", "path traversal is not allowed")

    root = transfer_root()
    candidate = root.joinpath(*pure.parts)
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise TransferError(
            "path_outside_transfer_root", "path escapes the transfer root"
        ) from exc
    if candidate.is_symlink():
        raise TransferError("symlink_not_allowed", "symbolic-link targets are not allowed")
    return candidate, pure.as_posix()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _staging_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.edr-wd-upload")


def _error(exc: TransferError) -> dict:
    return {"ok": False, "error_code": exc.code, "error": str(exc)}


def upload_chunk(
    relative_path: str,
    content_base64: str,
    *,
    offset: int = 0,
    overwrite: bool = False,
    final: bool = False,
    expected_sha256: str | None = None,
) -> dict:
    """Write one contiguous base64 chunk inside the transfer sandbox."""
    try:
        path, normalized = _safe_path(relative_path)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise TransferError("invalid_offset", "offset must be a non-negative integer")
        if not isinstance(content_base64, str):
            raise TransferError("invalid_base64", "content_base64 must be a string")
        try:
            payload = base64.b64decode(content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise TransferError("invalid_base64", "content_base64 is not valid base64") from exc
        if len(payload) > MAX_CHUNK_BYTES:
            raise TransferError(
                "chunk_too_large", f"decoded chunk exceeds {MAX_CHUNK_BYTES} bytes"
            )
        if offset + len(payload) > MAX_FILE_BYTES:
            raise TransferError(
                "file_too_large", f"file would exceed {MAX_FILE_BYTES} bytes"
            )
        if expected_sha256 is not None and not re.fullmatch(
            r"[0-9a-fA-F]{64}", expected_sha256
        ):
            raise TransferError("invalid_sha256", "expected_sha256 must be 64 hex characters")

        path.parent.mkdir(parents=True, exist_ok=True)
        staging = _staging_path(path)
        if offset == 0:
            if path.exists() and not path.is_file():
                raise TransferError("destination_not_file", "destination is not a regular file")
            if path.exists() and not overwrite:
                raise TransferError(
                    "file_exists", "destination exists; set overwrite=true to replace it"
                )
            if staging.exists() and not overwrite:
                raise TransferError(
                    "upload_in_progress",
                    "an incomplete upload exists; resume at its current size or set overwrite=true",
                )
            if staging.is_symlink():
                staging.unlink()
            mode = "wb"
        else:
            if not staging.is_file() or staging.is_symlink():
                raise TransferError("offset_mismatch", "staged upload does not exist for resume")
            current_size = staging.stat().st_size
            if current_size != offset:
                raise TransferError(
                    "offset_mismatch",
                    f"offset {offset} does not match current size {current_size}",
                )
            mode = "ab"

        with staging.open(mode) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

        size = staging.stat().st_size
        result = {
            "ok": True,
            "relative_path": normalized,
            "bytes_written": len(payload),
            "size": size,
            "next_offset": size,
            "complete": bool(final),
        }
        if final:
            digest = _sha256(staging)
            result["sha256"] = digest
            if expected_sha256 and digest.lower() != expected_sha256.lower():
                staging.unlink(missing_ok=True)
                result.update({
                    "ok": False,
                    "complete": False,
                    "error_code": "checksum_mismatch",
                    "error": "uploaded file SHA-256 does not match expected_sha256",
                })
            else:
                os.replace(staging, path)
        return result
    except TransferError as exc:
        return _error(exc)
    except OSError as exc:
        return {"ok": False, "error_code": "io_error", "error": str(exc)}


def download_chunk(
    relative_path: str,
    *,
    offset: int = 0,
    max_bytes: int = DEFAULT_CHUNK_BYTES,
) -> dict:
    """Read one base64 chunk from a file inside the transfer sandbox."""
    try:
        path, normalized = _safe_path(relative_path)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise TransferError("invalid_offset", "offset must be a non-negative integer")
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes < 1
            or max_bytes > MAX_CHUNK_BYTES
        ):
            raise TransferError(
                "invalid_chunk_size", f"max_bytes must be between 1 and {MAX_CHUNK_BYTES}"
            )
        if not path.is_file() or path.is_symlink():
            raise TransferError("file_not_found", "transfer file does not exist")
        size = path.stat().st_size
        if offset > size:
            raise TransferError(
                "offset_mismatch", f"offset {offset} is greater than file size {size}"
            )
        with path.open("rb") as handle:
            handle.seek(offset)
            payload = handle.read(max_bytes)
        next_offset = offset + len(payload)
        eof = next_offset >= size
        result = {
            "ok": True,
            "relative_path": normalized,
            "offset": offset,
            "next_offset": next_offset,
            "size": size,
            "bytes_read": len(payload),
            "content_base64": base64.b64encode(payload).decode("ascii"),
            "eof": eof,
        }
        if eof:
            result["sha256"] = _sha256(path)
        return result
    except TransferError as exc:
        return _error(exc)
    except OSError as exc:
        return {"ok": False, "error_code": "io_error", "error": str(exc)}


def stat_file(relative_path: str) -> dict:
    """Return resumable-transfer metadata without exposing an absolute path."""
    try:
        path, normalized = _safe_path(relative_path)
        staging = _staging_path(path)
        if staging.is_file() and not staging.is_symlink():
            stat = staging.stat()
            return {
                "ok": True,
                "relative_path": normalized,
                "size": stat.st_size,
                "sha256": _sha256(staging),
                "modified_ns": stat.st_mtime_ns,
                "complete": False,
            }
        if not path.is_file() or path.is_symlink():
            raise TransferError("file_not_found", "transfer file does not exist")
        stat = path.stat()
        return {
            "ok": True,
            "relative_path": normalized,
            "size": stat.st_size,
            "sha256": _sha256(path),
            "modified_ns": stat.st_mtime_ns,
            "complete": True,
        }
    except TransferError as exc:
        return _error(exc)
    except OSError as exc:
        return {"ok": False, "error_code": "io_error", "error": str(exc)}
