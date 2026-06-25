"""Guard against public-Internet probes in tests and health checks."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

CHECKED_SUFFIXES = {
    ".md",
    ".py",
    ".ps1",
    ".sh",
    ".json",
}

SKIP_DIRS = {
    ".git",
    ".pytest_cache",
    ".venv",
    "__pycache__",
    "node_modules",
}

ALLOWLIST_FILES = {
    Path("references/mcp-tools.md"),
    Path("references/testing.md"),
    Path("test_case/test_no_public_connectivity_checks.py"),
}

FORBIDDEN_PATTERNS = {
    "8.8.8.8": "public DNS probe",
    "1.1.1.1": "public DNS probe",
    "google.com": "public Internet probe",
    "baidu.com": "public Internet probe",
    "Test-NetConnection": "generic network probe; use target-local checks",
    "Invoke-WebRequest": "external HTTP probe; use MCP/client checks",
}


def _iter_project_files():
    for path in ROOT.rglob("*"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.relative_to(ROOT) in ALLOWLIST_FILES:
            continue
        if path.is_file() and path.suffix in CHECKED_SUFFIXES:
            yield path


def test_no_public_connectivity_probes_in_project_sources():
    offenders = []
    for path in _iter_project_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern, reason in FORBIDDEN_PATTERNS.items():
            if pattern in text:
                offenders.append(f"{path.relative_to(ROOT)}: {pattern} ({reason})")

    assert not offenders, (
        "Do not make tests or health checks depend on public Internet reachability:\n"
        + "\n".join(offenders)
    )
