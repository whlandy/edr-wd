#!/usr/bin/env python3
"""Paramiko local port forwarding for EDR-WD tunnel mode."""

from __future__ import annotations

import argparse
import contextlib
import http.client
import os
import select
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.ssh_runner import _paramiko_connect
from agent.target_config import TargetConfig


PID_DIR = Path(os.environ.get("EDR_WD_TUNNEL_PID_DIR", "/tmp"))
LOG_DIR = Path(os.environ.get("EDR_WD_TUNNEL_LOG_DIR", "/tmp"))


def _target_name(value: str | None) -> str:
    if value:
        return value
    tc = TargetConfig()
    target = os.environ.get("EDR_WD_TARGET") or os.environ.get("EDR_WD_TARGET_NAME") or tc.get_default_target()
    if not target:
        raise SystemExit("No target provided and no default target configured")
    return target


def _cfg(target: str) -> tuple[dict, int, int]:
    tc = TargetConfig()
    raw = tc.get_target(target)
    ssh_cfg = tc.resolve_auth(target)
    mcp = raw.get("mcp", {})
    tunnel = mcp.get("tunnel", {})
    local_port = int(os.environ.get("EDR_WD_LOCAL_PORT") or tunnel.get("local_port", 18765))
    remote_port = int(os.environ.get("EDR_WD_REMOTE_PORT") or mcp.get("port", 8765))
    return ssh_cfg, local_port, remote_port


def _pid_file(local_port: int) -> Path:
    return PID_DIR / f"edr-wd-tunnel-{local_port}.pid"


def _log_file(local_port: int) -> Path:
    return Path(os.environ.get("EDR_WD_TUNNEL_LOG") or LOG_DIR / f"edr-wd-tunnel-{local_port}.log")


def _port_open(port: int) -> bool:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _forward(client_sock: socket.socket, transport, remote_port: int) -> None:
    try:
        channel = transport.open_channel(
            "direct-tcpip",
            ("127.0.0.1", remote_port),
            client_sock.getsockname(),
        )
    except Exception:
        client_sock.close()
        return
    if channel is None:
        client_sock.close()
        return

    sockets = [client_sock, channel]
    try:
        while True:
            readable, _, _ = select.select(sockets, [], [], 60)
            if not readable:
                continue
            if client_sock in readable:
                data = client_sock.recv(32768)
                if not data:
                    break
                channel.sendall(data)
            if channel in readable:
                data = channel.recv(32768)
                if not data:
                    break
                client_sock.sendall(data)
    finally:
        channel.close()
        client_sock.close()


def serve(args: argparse.Namespace) -> int:
    target = _target_name(args.target)
    ssh_cfg, local_port, remote_port = _cfg(target)
    pid_file = _pid_file(local_port)
    pid_file.write_text(str(os.getpid()), encoding="utf-8")

    client = _paramiko_connect(ssh_cfg, timeout=15)
    transport = client.get_transport()
    if transport is None:
        raise RuntimeError("Paramiko transport unavailable")

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", local_port))
    server.listen(50)

    print(f"Paramiko tunnel serving 127.0.0.1:{local_port} -> 127.0.0.1:{remote_port} ({target})", flush=True)
    try:
        while True:
            client_sock, _addr = server.accept()
            # MCP traffic is low-volume. Handle one accepted connection at a
            # time to keep the tunnel process simple and deterministic.
            _forward(client_sock, transport, remote_port)
    finally:
        server.close()
        client.close()
        with contextlib.suppress(FileNotFoundError):
            pid_file.unlink()


def start(args: argparse.Namespace) -> int:
    target = _target_name(args.target)
    _ssh_cfg, local_port, _remote_port = _cfg(target)
    if _port_open(local_port):
        print(f"Tunnel already running on port {local_port}")
        return 0

    log_file = _log_file(local_port)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "serve",
        "--target",
        target,
    ]
    with log_file.open("ab") as log:
        subprocess.Popen(
            cmd,
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )

    deadline = time.time() + args.timeout
    while time.time() < deadline:
        if _port_open(local_port):
            print(f"Tunnel started on 127.0.0.1:{local_port}")
            return 0
        time.sleep(0.25)

    print(f"Tunnel failed to start. Log: {log_file}", file=sys.stderr)
    return 1


def stop(args: argparse.Namespace) -> int:
    target = _target_name(args.target)
    _ssh_cfg, local_port, _remote_port = _cfg(target)
    pid_file = _pid_file(local_port)
    if not pid_file.exists():
        print(f"Tunnel pid file not found for port {local_port}")
        return 0
    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except ValueError:
        pid_file.unlink(missing_ok=True)
        return 0
    if _process_alive(pid):
        os.kill(pid, signal.SIGTERM)
        time.sleep(0.5)
        if _process_alive(pid):
            os.kill(pid, signal.SIGKILL)
    pid_file.unlink(missing_ok=True)
    print(f"Tunnel stopped on port {local_port}")
    return 0


def status(args: argparse.Namespace) -> int:
    target = _target_name(args.target)
    _ssh_cfg, local_port, _remote_port = _cfg(target)
    pid_file = _pid_file(local_port)
    pid = None
    if pid_file.exists():
        with contextlib.suppress(ValueError):
            pid = int(pid_file.read_text(encoding="utf-8").strip())
    if _port_open(local_port):
        detail = f" pid={pid}" if pid else ""
        print(f"Tunnel running on 127.0.0.1:{local_port}{detail}")
        return 0
    print(f"Tunnel not running on 127.0.0.1:{local_port}")
    return 0


def test(args: argparse.Namespace) -> int:
    target = _target_name(args.target)
    _ssh_cfg, local_port, _remote_port = _cfg(target)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", local_port, timeout=5)
        conn.request("GET", "/mcp")
        resp = conn.getresponse()
        status_code = resp.status
    except OSError:
        status_code = 0
    finally:
        with contextlib.suppress(Exception):
            conn.close()  # type: ignore[name-defined]
    if status_code in (404, 406):
        print(f"MCP server responding (HTTP {status_code})")
        return 0
    if status_code == 0:
        print(f"Cannot connect to 127.0.0.1:{local_port}")
        return 1
    print(f"Unexpected HTTP {status_code}")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("start", "stop", "status", "test", "serve"):
        p = sub.add_parser(name)
        p.add_argument("--target")
        if name == "start":
            p.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args(argv)
    return globals()[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
