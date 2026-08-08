"""Unit and MCP contract tests for sandboxed target file transfer."""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

from target import file_transfer
from target import server


@pytest.fixture(autouse=True)
def isolated_transfer_root(tmp_path, monkeypatch):
    monkeypatch.setenv("EDR_WD_TRANSFER_DIR", str(tmp_path / "transfers"))


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def test_chunked_round_trip_and_checksum():
    first = file_transfer.upload_chunk(
        "nested/sample.bin", _b64(b"hello "), overwrite=False,
    )
    assert first["ok"] is True
    assert first["next_offset"] == 6

    expected = hashlib.sha256(b"hello world").hexdigest()
    final = file_transfer.upload_chunk(
        "nested/sample.bin",
        _b64(b"world"),
        offset=6,
        final=True,
        expected_sha256=expected,
    )
    assert final["ok"] is True
    assert final["complete"] is True
    assert final["sha256"] == expected

    part1 = file_transfer.download_chunk("nested/sample.bin", max_bytes=4)
    part2 = file_transfer.download_chunk(
        "nested/sample.bin", offset=part1["next_offset"], max_bytes=20,
    )
    assert base64.b64decode(part1["content_base64"]) == b"hell"
    assert base64.b64decode(part2["content_base64"]) == b"o world"
    assert part2["eof"] is True
    assert part2["sha256"] == expected


@pytest.mark.parametrize(
    "path",
    ["../secret.txt", "/etc/passwd", r"C:\\Windows\\win.ini", r"..\\secret.txt"],
)
def test_rejects_paths_outside_transfer_root(path):
    result = file_transfer.upload_chunk(path, _b64(b"x"))
    assert result["ok"] is False
    assert result["error_code"] == "path_outside_transfer_root"


def test_rejects_overwrite_and_non_contiguous_resume():
    assert file_transfer.upload_chunk("a.txt", _b64(b"abc"))["ok"] is True
    exists = file_transfer.upload_chunk("a.txt", _b64(b"new"))
    mismatch = file_transfer.upload_chunk("a.txt", _b64(b"x"), offset=2)
    assert exists["error_code"] == "upload_in_progress"
    assert mismatch["error_code"] == "offset_mismatch"


def test_rejects_invalid_base64_and_oversized_download_request():
    bad = file_transfer.upload_chunk("a.txt", "not base64!")
    too_large = file_transfer.download_chunk(
        "missing.txt", max_bytes=file_transfer.MAX_CHUNK_BYTES + 1,
    )
    assert bad["error_code"] == "invalid_base64"
    assert too_large["error_code"] == "invalid_chunk_size"


def test_checksum_mismatch_is_explicit_and_does_not_publish_file():
    result = file_transfer.upload_chunk(
        "a.txt", _b64(b"payload"), final=True, expected_sha256="0" * 64,
    )
    assert result["ok"] is False
    assert result["error_code"] == "checksum_mismatch"
    stat = file_transfer.stat_file("a.txt")
    assert stat["ok"] is False
    assert stat["error_code"] == "file_not_found"


def test_overwrite_is_atomic_until_final_chunk():
    original = file_transfer.upload_chunk("a.txt", _b64(b"old"), final=True)
    assert original["ok"] is True
    started = file_transfer.upload_chunk(
        "a.txt", _b64(b"new "), overwrite=True,
    )
    assert started["ok"] is True
    assert file_transfer.download_chunk("a.txt")["content_base64"] == _b64(b"old")
    staged = file_transfer.stat_file("a.txt")
    assert staged["complete"] is False
    finished = file_transfer.upload_chunk(
        "a.txt", _b64(b"value"), offset=4, final=True,
    )
    assert finished["ok"] is True
    downloaded = file_transfer.download_chunk("a.txt")
    assert base64.b64decode(downloaded["content_base64"]) == b"new value"


def test_server_tools_return_json_contract():
    upload = json.loads(server.transfer_upload(
        "mcp.txt", _b64(b"through mcp"), final=True,
    ))
    stat = json.loads(server.transfer_stat("mcp.txt"))
    download = json.loads(server.transfer_download("mcp.txt", max_bytes=64))
    assert upload["ok"] is True
    assert stat["sha256"] == upload["sha256"]
    assert base64.b64decode(download["content_base64"]) == b"through mcp"
