#!/usr/bin/env python3
"""
test_paramiko_login.py — Verify username+password SSH login via Paramiko.

Password is read ONLY from environment variable EDR_WD_TARGET_PASSWORD.
Host and user can be passed via args or env vars, but are never printed.

Usage:
    EDR_WD_TARGET_HOST=<TARGET_IP> \
    EDR_WD_TARGET_USER=<TARGET_USER> \
    EDR_WD_TARGET_PASSWORD=<PASSWORD> \
        python scripts/test_paramiko_login.py

    # Or with args (host/user from env, password from env):
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Test Paramiko SSH login")
    parser.add_argument("--port", dest="port", type=int, default=22,
                        help="SSH port (default: 22)")
    # host and user must come from env vars (not CLI) to avoid leaking
    args = parser.parse_args()

    host = os.environ.get("EDR_WD_TARGET_HOST")
    user = os.environ.get("EDR_WD_TARGET_USER")
    password = os.environ.get("EDR_WD_TARGET_PASSWORD")

    if not host or not user or not password:
        print(
            "Usage:\n"
            "  export EDR_WD_TARGET_HOST=<TARGET_IP>\n"
            "  export EDR_WD_TARGET_USER=<TARGET_USER>\n"
            "  export EDR_WD_TARGET_PASSWORD=<PASSWORD>\n"
            "  python scripts/test_paramiko_login.py [--port 22]\n"
            "\n"
            "All three env vars are required. Password is never printed or logged.",
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

    print("[INFO] Attempting Paramiko SSH login to <TARGET_IP>:<PORT> ...")
    exit_code, output = run_ssh(ssh_config, "hostname", timeout=15)

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
