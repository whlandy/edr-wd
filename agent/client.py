#!/usr/bin/env python3
"""
client.py — Mac/Windows 端 SSH Tunnel 工具
==========================================

用于建立到 Windows EDR MCP Server 的 SSH tunnel。
支持两种模式：
  1. SSH tunnel（本地 port forward）：Mac -> SSH -> Windows MCP Server
  2. 直连 HTTP（如果 Windows 暴露了端口）

Usage:
    # 方式1: SSH tunnel（推荐）
    python client.py --ssh-host <WINDOWS_IP> --ssh-user <USER> --tunnel-port 18765

    # 方式2: 直连 HTTP
    python client.py --http-url http://<WINDOWS_IP>:8765

    # 测试连接
    python client.py --test --tunnel-port 18765
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.request
import urllib.error


def check_port(port: int, timeout: float = 2.0) -> bool:
    """检查本地端口是否可连接（HEAD request，避免浪费资源）"""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/")
        req.get_method = lambda: "HEAD"
        urllib.request.urlopen(req, timeout=timeout)
        return True
    except Exception:
        return False


def start_ssh_tunnel(ssh_host: str, ssh_user: str, remote_port: int = 8765,
                      local_port: int = 18765) -> subprocess.Popen:
    """
    启动 SSH tunnel (LocalForward).
    返回 Popen 进程对象。
    """
    cmd = [
        "ssh",
        "-N",                      # 不执行远程命令
        "-f",                      # 后台运行
        "-o", "StrictHostKeyChecking=no",
        "-o", "ServerAliveInterval=60",
        "-L", f"{local_port}:127.0.0.1:{remote_port}",
        f"{ssh_user}@{ssh_host}",
    ]
    print(f"[SSH] Running: {' '.join(cmd)}")
    proc = subprocess.Popen(cmd)
    return proc


def wait_for_port(port: int, timeout: float = 30.0) -> bool:
    """等待端口变为可用"""
    start = time.time()
    while time.time() - start < timeout:
        if check_port(port):
            return True
        time.sleep(1)
    return False


def test_mcp_server(port: int) -> dict:
    """测试 MCP server 是否可用"""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/tools")
        resp = urllib.request.urlopen(req, timeout=5)
        data = json.loads(resp.read())
        return {"ok": True, "tools": [t["name"] for t in data.get("tools", [])]}
    except urllib.error.HTTPError as e:
        # MCP HTTP server 可能返回 404，尝试 list_tools
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/list_tools")
            resp = urllib.request.urlopen(req, timeout=5)
            return {"ok": True, "raw": True}
        except Exception as e2:
            return {"ok": False, "error": f"HTTP {e.code}: {e2}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def main():
    parser = argparse.ArgumentParser(description="EDR-WD SSH Tunnel & Connection Tool")
    parser.add_argument("--ssh-host", help="Windows IP address")
    parser.add_argument("--ssh-user", help="SSH username")
    parser.add_argument("--remote-port", type=int, default=8765, help="Remote MCP server port on Windows")
    parser.add_argument("--tunnel-port", type=int, default=18765, help="Local tunnel port")
    parser.add_argument("--http-url", help="Direct HTTP URL (skip SSH tunnel)")
    parser.add_argument("--test", action="store_true", help="Test existing tunnel")
    parser.add_argument("--timeout", type=int, default=30, help="Port wait timeout")
    args = parser.parse_args()

    local_port = args.tunnel_port

    # 测试模式
    if args.test:
        print(f"[Test] Checking local port {local_port}...")
        if check_port(local_port):
            print(f"[OK] Port {local_port} is open")
            result = test_mcp_server(local_port)
            print(f"[MCP] {result}")
        else:
            print(f"[FAIL] Port {local_port} is not reachable")
            sys.exit(1)
        return

    # 直连 HTTP 模式
    if args.http_url:
        print(f"[HTTP] Connecting directly to {args.http_url}")
        try:
            req = urllib.request.Request(args.http_url)
            resp = urllib.request.urlopen(req, timeout=5)
            print(f"[OK] Connected: {resp.status}")
        except Exception as e:
            print(f"[FAIL] {e}")
            sys.exit(1)
        return

    # SSH tunnel 模式
    if not args.ssh_host or not args.ssh_user:
        parser.print_help()
        sys.exit(1)

    print(f"[SSH] Starting tunnel: localhost:{local_port} -> {args.ssh_host}:{args.remote_port}")

    proc = start_ssh_tunnel(args.ssh_host, args.ssh_user, args.remote_port, local_port)

    print(f"[SSH] Waiting for tunnel (timeout={args.timeout}s)...")
    if wait_for_port(local_port, args.timeout):
        print(f"[OK] Tunnel established on localhost:{local_port}")
        print(f"[OK] Run 'python client.py --test --tunnel-port {local_port}' to verify")
    else:
        print(f"[FAIL] Tunnel failed to establish within {args.timeout}s")
        proc.terminate()
        sys.exit(1)

    print("[Info] Keeping tunnel alive... (Ctrl+C to stop)")
    try:
        proc.wait()
    except KeyboardInterrupt:
        print("\n[Info] Stopping tunnel...")
        proc.terminate()


if __name__ == "__main__":
    main()
