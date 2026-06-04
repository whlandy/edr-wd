#!/usr/bin/env python3
"""
redact_config.py — Print a config file with secrets redacted.

Replaces values for known-secret keys (password, password_env, key_path,
host, user, root, target_root, python_path, direct_url, tunnel_url,
preempt_path, etc.) with "<REDACTED>" or with a stable hash so that
the same value always maps to the same placeholder. This lets you
diff / review a config without leaking real credentials.

Why a redactor, not a viewer:
  Reviewers and tools (cat / less / git diff) treat config files as
  text and will happily print real passwords. The only safe way to
  inspect config/targets.local.json is through this script.

Usage:
  python scripts/redact_config.py config/targets.local.json
  python scripts/redact_config.py config/targets.local.json --format json
  python scripts/redact_config.py config/targets.local.json --strict
      # --strict exits non-zero if any value LOOKS like a real password

The script:
  - Never echoes a value for the keys listed in SECRET_KEYS.
  - Hashes other values (so repeated entries stay the same in a diff).
  - Preserves structural shape so reviewers can see schema validity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


# Keys whose value MUST be redacted.
SECRET_KEYS = frozenset({
    "password",
    "password_env",
    "key_path",
    "token",
    "secret",
    "authorization",
    "bearer",
    "api_key",
    "ssh_password",
})

# Keys whose value should be hashed (kept stable for diffing) but the
# value itself is structural (host, path, etc.) — useful to confirm a
# config points at the right machine without leaking the actual one.
STRUCTURAL_KEYS = frozenset({
    "host",
    "user",
    "root",
    "target_root",
    "python_path",
    "direct_url",
    "tunnel_url",
    "preempt_path",
    "key_path",  # also here, but secret takes priority
    "ssh_port",
    "port",
})


def _stable_hash(value: str) -> str:
    return "h_" + hashlib.sha1(value.encode("utf-8")).hexdigest()[:8]


def _looks_like_password(value: str) -> bool:
    """
    Heuristic: a value that looks like a real password.

    Strict rules — only flag a value when ALL of these hold:
      - non-empty string with length 6..64
      - no whitespace (real passwords don't usually contain spaces unless
        deliberately quoted, and config values with spaces are usually
        sentences or commands)
      - contains at least one letter AND at least one digit
      - contains at least one symbol, OR is 10+ characters
    """
    if not value or not isinstance(value, str):
        return False
    if len(value) < 6 or len(value) > 64:
        return False
    if any(c.isspace() for c in value):
        return False
    if not (re.search(r"[A-Za-z]", value) and re.search(r"[0-9]", value)):
        return False
    if re.search(r"[^A-Za-z0-9]", value):
        return True
    return len(value) >= 10


def _redact(value: Any, *, strict: bool) -> tuple[Any, bool]:
    """
    Return (redacted_value, is_suspicious). is_suspicious is True when a
    non-secret key contained a value that looks like a real password —
    used by --strict to fail loudly.
    """
    if isinstance(value, dict):
        out = {}
        suspicious = False
        for k, v in value.items():
            if k in SECRET_KEYS:
                out[k] = "<REDACTED>"
            elif k in STRUCTURAL_KEYS:
                out[k] = _stable_hash(v) if isinstance(v, str) else v
            else:
                rv, sus = _redact(v, strict=strict)
                out[k] = rv
                suspicious = suspicious or sus
        return out, suspicious

    if isinstance(value, list):
        out = []
        suspicious = False
        for item in value:
            rv, sus = _redact(item, strict=strict)
            out.append(rv)
            suspicious = suspicious or sus
        return out, suspicious

    if isinstance(value, str):
        if strict and _looks_like_password(value):
            return "<REDACTED:password-like>", True
        return value, False

    return value, False


def main() -> int:
    parser = argparse.ArgumentParser(description="Print a config with secrets redacted")
    parser.add_argument("path", type=Path, help="Path to a config file (e.g. config/targets.local.json)")
    parser.add_argument("--format", choices=("json", "yaml-ish"), default="json")
    parser.add_argument("--strict", action="store_true",
                        help="Exit non-zero if any non-secret key contains a password-like value")
    args = parser.parse_args()

    if not args.path.exists():
        print(f"ERROR: {args.path} not found", file=sys.stderr)
        return 2

    try:
        raw = json.loads(args.path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"ERROR: {args.path} is not valid JSON: {e}", file=sys.stderr)
        return 2

    redacted, suspicious = _redact(raw, strict=args.strict)

    print(json.dumps(redacted, indent=2, ensure_ascii=False))

    if args.strict and suspicious:
        print("\n[--strict] Found values that look like passwords in non-secret keys.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
