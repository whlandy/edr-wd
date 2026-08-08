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
_RECORD_ROOT = Path(
    os.environ.get(
        "EDR_WD_RECORD_DIR", Path.home() / "Desktop" / "edr-wd-record"
    )
)
LOG_DIR = Path(os.environ.get("EDR_WD_TUNNEL_LOG_DIR", _RECORD_ROOT / "logs"))


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


def _read_pid(local_port: int) -> int | None:
    pid_file = _pid_file(local_port)
    if not pid_file.exists():
        return None
    try:
        return int(pid_file.read_text(encoding="utf-8").strip())
    except ValueError:
        pid_file.unlink(missing_ok=True)
        return None


def _owned_tunnel_pid(local_port: int) -> int | None:
    pid = _read_pid(local_port)
    if pid and _process_alive(pid):
        return pid
    if pid:
        _pid_file(local_port).unlink(missing_ok=True)
    return None


def _terminate_owned_tunnel(local_port: int) -> int | None:
    """Stop only the EDR-WD process recorded for ``local_port``."""
    pid = _read_pid(local_port)
    if not pid:
        return None
    if _process_alive(pid):
        os.kill(pid, signal.SIGTERM)
        deadline = time.time() + 2.0
        while _process_alive(pid) and time.time() < deadline:
            time.sleep(0.05)
        if _process_alive(pid):
            os.kill(pid, signal.SIGKILL)
    _pid_file(local_port).unlink(missing_ok=True)
    return pid


def _mcp_status(local_port: int) -> int:
    try:
        conn = http.client.HTTPConnection("127.0.0.1", local_port, timeout=5)
        conn.request("GET", "/mcp")
        resp = conn.getresponse()
        return resp.status
    except Exception:
        return 0
    finally:
        with contextlib.suppress(Exception):
            conn.close()  # type: ignore[name-defined]


def _mcp_responding(local_port: int) -> bool:
    return _mcp_status(local_port) in (404, 406)


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
            try:
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
            except OSError:
                break
    finally:
        channel.close()
        client_sock.close()


def serve(args: argparse.Namespace) -> int:
    target = _target_name(args.target)
    ssh_cfg, local_port, remote_port = _cfg(target)
    pid_file = _pid_file(local_port)

    client = _paramiko_connect(ssh_cfg, timeout=15)
    transport = client.get_transport()
    if transport is None:
        raise RuntimeError("Paramiko transport unavailable")

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", local_port))
    server.listen(50)
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()), encoding="utf-8")

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


def ensure_tunnel(target: str | None = None, *, timeout: float = 10.0) -> dict:
    """Ensure the configured tunnel is healthy, replacing an owned stale one.

    A listening local socket is not sufficient: the SSH transport can die while
    the forwarding process remains alive.  In that state the old implementation
    returned an error and required a manual stop/start cycle.
    """
    target = _target_name(target)
    _ssh_cfg, local_port, _remote_port = _cfg(target)
    if _port_open(local_port):
        pid = _owned_tunnel_pid(local_port)
        if not pid:
            return {
                "ok": False,
                "target": target,
                "error": f"Port {local_port} is already in use by a non-EDR-WD process",
            }
        if _mcp_responding(local_port):
            return {
                "ok": True,
                "target": target,
                "status": "already_running",
                "local_port": local_port,
                "pid": pid,
            }
        _terminate_owned_tunnel(local_port)

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

    deadline = time.time() + timeout
    while time.time() < deadline:
        if _owned_tunnel_pid(local_port) and _mcp_responding(local_port):
            return {
                "ok": True,
                "target": target,
                "status": "started",
                "local_port": local_port,
                "pid": _owned_tunnel_pid(local_port),
            }
        time.sleep(0.25)

    return {
        "ok": False,
        "target": target,
        "error": f"Tunnel failed to start. Log: {log_file}",
        "local_port": local_port,
    }


def start(args: argparse.Namespace) -> int:
    result = ensure_tunnel(args.target, timeout=args.timeout)
    if result.get("ok"):
        print(
            f"Tunnel {result['status']} on 127.0.0.1:{result['local_port']} "
            f"pid={result.get('pid')}"
        )
        return 0
    print(result.get("error", "Tunnel failed to start"), file=sys.stderr)
    return 1


def stop(args: argparse.Namespace) -> int:
    target = _target_name(args.target)
    _ssh_cfg, local_port, _remote_port = _cfg(target)
    pid_file = _pid_file(local_port)
    pid = _read_pid(local_port)
    if not pid:
        print(f"Tunnel pid file not found for port {local_port}")
        return 0
    _terminate_owned_tunnel(local_port)
    print(f"Tunnel stopped on port {local_port}")
    return 0


def status(args: argparse.Namespace) -> int:
    target = _target_name(args.target)
    _ssh_cfg, local_port, _remote_port = _cfg(target)
    pid = _owned_tunnel_pid(local_port)
    if _port_open(local_port):
        if not pid:
            print(f"Port {local_port} is open, but not owned by EDR-WD tunnel")
            return 1
        status_code = _mcp_status(local_port)
        if status_code in (404, 406):
            print(f"Tunnel running on 127.0.0.1:{local_port} pid={pid}; MCP responding HTTP {status_code}")
            return 0
        print(f"Tunnel pid={pid} is running on 127.0.0.1:{local_port}, but MCP returned HTTP {status_code}")
        return 1
    print(f"Tunnel not running on 127.0.0.1:{local_port}")
    return 0


def test(args: argparse.Namespace) -> int:
    target = _target_name(args.target)
    _ssh_cfg, local_port, _remote_port = _cfg(target)
    status_code = _mcp_status(local_port)
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
