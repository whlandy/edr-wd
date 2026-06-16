#!/usr/bin/env python3
"""Regression tests for Paramiko SFTP remote path handling."""

from __future__ import annotations

import io
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import ssh_runner  # noqa: E402


_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  PASS  {name}")
    else:
        msg = f"  FAIL  {name}: {detail}"
        print(msg)
        _failures.append(name)


class _RemoteWriter(io.BytesIO):
    def __init__(self, sftp: "_FakeSFTP", path: str):
        super().__init__()
        self._sftp = sftp
        self._path = path

    def __enter__(self) -> "_RemoteWriter":
        return self

    def __exit__(self, *_exc: object) -> None:
        self._sftp.files[self._path] = self.getvalue()
        self.close()


class _FakeSFTP:
    def __init__(self) -> None:
        self.dirs: set[str] = {"C:", "C:/Users", "C:/Users/admin"}
        self.files: dict[str, bytes] = {}
        self.mkdir_calls: list[str] = []
        self.put_calls: list[tuple[str, str]] = []

    def stat(self, path: str) -> object:
        if path in self.dirs or path in self.files:
            return object()
        raise IOError(path)

    def mkdir(self, path: str) -> None:
        self.mkdir_calls.append(path)
        self.dirs.add(path)

    def open(self, path: str, _mode: str) -> _RemoteWriter:
        return _RemoteWriter(self, path)

    def put(self, local: str, remote: str) -> None:
        self.put_calls.append((local, remote))
        self.files[remote] = Path(local).read_bytes()


class _FakeClient:
    def __init__(self, sftp: _FakeSFTP) -> None:
        self.sftp = sftp

    def open_sftp(self) -> _FakeSFTP:
        return self.sftp

    def close(self) -> None:
        return None


def main() -> int:
    print("=" * 60)
    print("test_sftp_paths.py")
    print("=" * 60)

    fake_sftp = _FakeSFTP()
    original_connect = ssh_runner._paramiko_connect
    ssh_runner._paramiko_connect = lambda *_args, **_kwargs: _FakeClient(fake_sftp)  # type: ignore[assignment]
    try:
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "server.py"
            src.write_text("print('ok')\r\n", encoding="utf-8")

            remote_file = "C:/Users/admin/Desktop/edr-wd/target/server.py"
            rc, msg = ssh_runner.scp_to({}, src, remote_file)

            check("file upload succeeds", rc == 0, msg)
            check(
                "remote file path was not mkdir'ed",
                remote_file not in fake_sftp.mkdir_calls,
                f"mkdir calls: {fake_sftp.mkdir_calls}",
            )
            check(
                "remote parent directory was created",
                "C:/Users/admin/Desktop/edr-wd/target" in fake_sftp.dirs,
                f"dirs: {sorted(fake_sftp.dirs)}",
            )
            check(
                "text file uploaded to exact path",
                fake_sftp.files.get(remote_file) == b"print('ok')\n",
                f"files: {fake_sftp.files}",
            )

        uploaded: list[tuple[str, str]] = []
        original_scp_to = ssh_runner._paramiko_scp_to
        ssh_runner._paramiko_scp_to = (  # type: ignore[assignment]
            lambda _ssh, local, remote, timeout=30: uploaded.append((str(local), remote)) or (0, "ok")
        )
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "target"
                (root / "automation").mkdir(parents=True)
                (root / "logs").mkdir()
                (root / "server.py").write_text("server", encoding="utf-8")
                (root / "automation" / "base.py").write_text("base", encoding="utf-8")
                (root / "config.json").write_text("secret", encoding="utf-8")
                (root / "logs" / "server.log").write_text("log", encoding="utf-8")

                rc, msg = ssh_runner.scp_dir_to({}, root, "C:/remote/target")

                uploaded_remotes = {remote for _local, remote in uploaded}
                check("fallback scp_dir_to succeeds outside git", rc == 0, msg)
                check(
                    "fallback uploads deployable files",
                    uploaded_remotes == {
                        "C:/remote/target/automation/base.py",
                        "C:/remote/target/server.py",
                    },
                    f"uploaded: {sorted(uploaded_remotes)}",
                )
        finally:
            ssh_runner._paramiko_scp_to = original_scp_to  # type: ignore[assignment]
    finally:
        ssh_runner._paramiko_connect = original_connect  # type: ignore[assignment]

    if _failures:
        print("\nFAILED:")
        for failure in _failures:
            print(f"  - {failure}")
        return 1
    print("\nALL OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
