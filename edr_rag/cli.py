"""Command line entrypoint for edr-rag.

The implementation is intentionally phased. Only workspace initialisation is
active today; ingestion/search/action logic should be added following
docs/requirements/EDR-RAG-DESIGN.md.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path


DEFAULT_WORKDIR = "~/Desktop/edr-chm-rag-work"
DESIGN_DOC = "docs/requirements/EDR-RAG-DESIGN.md"


def _workdir(value: str | None) -> Path:
    raw = value or os.environ.get("CHM_RAG_WORKDIR") or DEFAULT_WORKDIR
    return Path(raw).expanduser()


def init_workdir(args: argparse.Namespace) -> int:
    workdir = _workdir(args.workdir)
    for child in [
        "input",
        "extracted",
        "normalized",
        "action_catalog",
        "vector_index",
        "logs",
    ]:
        (workdir / child).mkdir(parents=True, exist_ok=True)
    print(f"edr-rag workdir ready: {workdir}")
    return 0


def not_implemented(args: argparse.Namespace) -> int:
    command = getattr(args, "command", "unknown")
    print(
        f"edr-rag command '{command}' is not implemented yet. "
        f"Implement it according to {DESIGN_DOC}."
    )
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="edr-rag",
        description="CHM manual ingestion, retrieval, and action catalog tooling.",
    )
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser("init-workdir", help="Create the edr-rag desktop workdir layout.")
    p_init.add_argument("--workdir", default=None, help="Override CHM_RAG_WORKDIR.")
    p_init.set_defaults(func=init_workdir)

    for name in ["ingest", "extract-procedures", "search", "build-actions", "action-search", "apply-feedback"]:
        p = sub.add_parser(name, help=f"Placeholder for P-level command: {name}.")
        p.add_argument("--workdir", default=None, help="Override CHM_RAG_WORKDIR.")
        p.set_defaults(func=not_implemented)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
