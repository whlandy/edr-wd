#!/usr/bin/env python3
"""
test_paramiko_login.py — Verify username+password SSH login via Paramiko.

Preferred usage reads SSH host/user/password from config/targets.local.json by
target name. Environment variables remain as a compatibility fallback.
Real host/user/password values are never printed.

Usage:
    python scripts/test_paramiko_login.py --target win-dev

    # Or fallback env mode:
    EDR_WD_TARGET_HOST=<TARGET_IP> \
    EDR_WD_TARGET_USER=<TARGET_USER> \
    EDR_WD_TARGET_PASSWORD=<PASSWORD> \
        python scripts/test_paramiko_login.py

    # Env mode with a custom port:
    EDR_WD_TARGET_HOST=<TARGET_IP> \
    EDR_WD_TARGET_USER=<TARGET_USER> \
    EDR_WD_TARGET_PASSWORD=<PASSWORD> \
        python scripts/test_paramiko_login.py --port 22
"""

import argparse
import os
import sys
from pathlib import Path

# Add project root to path so we can import ssh_runner
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.ssh_runner import run_ssh, SSHAuthError
from agent.target_config import TargetConfig


def main() -> int:
    parser = argparse.ArgumentParser(description="Test Paramiko SSH login")
    parser.add_argument("--target", help="Target name from config/targets.local.json")
    parser.add_argument("--port", dest="port", type=int, default=22,
                        help="SSH port for env fallback mode (default: 22)")
    args = parser.parse_args()

    try:
        if args.target:
            ssh_config = TargetConfig().resolve_auth(args.target)
        else:
            host = os.environ.get("EDR_WD_TARGET_HOST")
            user = os.environ.get("EDR_WD_TARGET_USER")
            password = os.environ.get("EDR_WD_TARGET_PASSWORD")
            if not host or not user or not password:
                print(
                    "Usage:\n"
                    "  python scripts/test_paramiko_login.py --target <target-name>\n"
                    "\n"
                    "Fallback env mode:\n"
                    "  export EDR_WD_TARGET_HOST=<TARGET_IP>\n"
                    "  export EDR_WD_TARGET_USER=<TARGET_USER>\n"
                    "  export EDR_WD_TARGET_PASSWORD=<PASSWORD>\n"
                    "  python scripts/test_paramiko_login.py [--port 22]\n"
                    "\n"
                    "Passwords are never printed or logged.",
                    file=sys.stderr,
                )
                return 1
            ssh_config = {
                "host": host,
                "port": args.port,
                "user": user,
                "auth": {
                    "type": "password",
                    "password": password,
                },
            }
    except (KeyError, EnvironmentError, SSHAuthError) as e:
        print(f"[FAIL] Could not load SSH config: {e}", file=sys.stderr)
        return 1

    print("[INFO] Attempting Paramiko SSH login to <TARGET_IP>:<PORT> ...")
    try:
        exit_code, output = run_ssh(ssh_config, "hostname", timeout=15)
    except SSHAuthError as e:
        print(f"[FAIL] SSH auth config error: {e}", file=sys.stderr)
        return 1

    if exit_code == 0:
        # Don't print hostname — just confirm command ran
        output_lines = output.strip().splitlines()
        print(f"[OK] Login succeeded. Command output: {len(output_lines)} line(s).")
        return 0
    else:
        # Error output from ssh_runner is already redacted; just show it
        print(f"[FAIL] SSH login failed (exit {exit_code}).", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
