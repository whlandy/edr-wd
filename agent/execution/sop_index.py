"""sop_index.py — P2.1 SOP index loader (architecture §15.2, §16).

Loads transition-relevant metadata from the SOP markdown frontmatter
into a deterministic `SopIndex`. The index is consumed by
`agent.execution.checkpoints.decide_checkpoint` as the third input
merged alongside catalog `transition_policy`, step `transition`
declaration, selector semantics, and current application/session
state.

P2.1 scope (per `docs/requirements/P2-recovery-production.md`):

    * Read `sops/*.md` YAML frontmatter (between the leading `---`
      fences) and extract:

        - sop_id (required)
        - version (optional, int default 1)
        - transition_kind (optional, must be a valid
          `TransitionKind` if present)
        - application_state_hint (optional free-form string)

    * Build `SopIndex(entries=(SopIndexEntry, ...), version=str)`
      sorted by `sop_id` for determinism.

    * Reject malformed frontmatter with a `SopIndexLoadError`
      carrying the offending path and reason. Unknown
      `transition_kind` values fail loudly.

Out of scope (intentional, per P2.1 doc):

    * SOP graph, step dependency, planner integration.
    * Procedure body parsing — only frontmatter is consumed.
    * Cross-document references; the index is file-local.

The loader is read-only: it never mutates the SOPs directory.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from agent.execution.transitions import (
    TransitionKind,
    VALID_TRANSITION_KINDS,
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SopIndexLoadError(Exception):
    """Raised when an SOP file fails to parse.

    `path` is the file path (relative or absolute). `reason` is a
    short, machine-friendly tag (`missing_frontmatter`,
    `invalid_transition_kind`, ...). `detail` is the human-readable
    explanation suitable for logs.
    """

    def __init__(self, path: str, reason: str, detail: str) -> None:
        super().__init__(f"{path}: {reason}: {detail}")
        self.path = path
        self.reason = reason
        self.detail = detail


# ---------------------------------------------------------------------------
# SopIndexEntry — one parsed SOP frontmatter
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SopIndexEntry:
    """Transition-relevant metadata for one SOP.

    `sop_id` and `path` are required. `transition_kind` and
    `application_state_hint` default to None when the SOP has not
    declared them. `version` is the frontmatter `version` field
    coerced to int, defaulting to 1.
    """

    sop_id: str
    path: str
    version: int = 1
    transition_kind: TransitionKind | None = None
    application_state_hint: str | None = None

    _ALLOWED: frozenset[str] = frozenset({
        "sop_id", "path", "version", "transition_kind",
        "application_state_hint",
    })


# ---------------------------------------------------------------------------
# SopIndex — sorted collection of entries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SopIndex:
    """Deterministic view of all SOP frontmatter under a directory.

    `entries` is sorted by `sop_id` for byte-stable iteration
    regardless of the underlying filesystem order. `version` is the
    index schema version (independent of any individual SOP's
    version) — P2.1 ships `1.0.0`.
    """

    entries: tuple[SopIndexEntry, ...] = field(default_factory=tuple)
    version: str = "1.0.0"

    _ALLOWED: frozenset[str] = frozenset({"entries", "version"})

    def get(self, sop_id: str) -> SopIndexEntry | None:
        """Return the entry for `sop_id` or None if not present."""
        for entry in self.entries:
            if entry.sop_id == sop_id:
                return entry
        return None

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self):
        return iter(self.entries)


# ---------------------------------------------------------------------------
# Frontmatter parsing
# ---------------------------------------------------------------------------


# Frontmatter is delimited by `---` on its own line, optionally with
# surrounding whitespace. Body content after the closing `---` is
# ignored by P2.1 (we only consume the metadata block).
_FRONTMATTER_RE = re.compile(
    r"\A\s*---\s*\n(?P<body>.*?)\n---\s*(?:\n|\Z)",
    re.DOTALL,
)


def _parse_frontmatter(text: str) -> Mapping[str, Any]:
    """Extract and parse a YAML frontmatter block.

    Returns the parsed mapping. Raises `ValueError` when the block is
    missing, malformed, or not a mapping.
    """
    m = _FRONTMATTER_RE.match(text)
    if m is None:
        raise ValueError("missing or malformed frontmatter block")
    raw = m.group("body")
    # Use yaml for parsing; stdlib only (PyYAML is an indirect
    # dependency via FastMCP and the project's lockfile).
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError as e:  # pragma: no cover
        raise ValueError("PyYAML not installed") from e
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        # Wrap YAML parse errors so callers see a uniform
        # ``ValueError`` chain (the load_sop_index entry point
        # converts ValueError → SopIndexLoadError).
        raise ValueError(f"invalid YAML in frontmatter: {e}") from e
    if not isinstance(parsed, Mapping):
        raise ValueError(
            f"frontmatter must be a mapping, got {type(parsed).__name__}"
        )
    return parsed


def _coerce_entry(
    parsed: Mapping[str, Any],
    path: str,
) -> SopIndexEntry:
    """Validate and coerce a parsed mapping into a SopIndexEntry."""
    sop_id = parsed.get("sop_id")
    if not isinstance(sop_id, str) or not sop_id:
        raise ValueError("sop_id is required and must be a non-empty string")

    version_raw = parsed.get("version", 1)
    if isinstance(version_raw, int):
        version = version_raw
    elif isinstance(version_raw, str) and version_raw.isdigit():
        version = int(version_raw)
    else:
        raise ValueError(
            f"version must be an int, got {type(version_raw).__name__}"
        )
    if version < 1:
        raise ValueError(f"version must be >= 1, got {version}")

    transition_kind_raw = parsed.get("transition_kind")
    transition_kind: TransitionKind | None = None
    if transition_kind_raw is not None:
        if not isinstance(transition_kind_raw, str):
            raise ValueError(
                f"transition_kind must be a string, got "
                f"{type(transition_kind_raw).__name__}"
            )
        try:
            kind = TransitionKind(transition_kind_raw)
        except ValueError:
            valid = sorted(k.value for k in VALID_TRANSITION_KINDS)
            raise ValueError(
                f"transition_kind={transition_kind_raw!r} not in {valid}"
            )
        transition_kind = kind

    application_state_hint = parsed.get("application_state_hint")
    if application_state_hint is not None and not isinstance(
        application_state_hint, str
    ):
        raise ValueError(
            f"application_state_hint must be a string, got "
            f"{type(application_state_hint).__name__}"
        )

    return SopIndexEntry(
        sop_id=sop_id,
        path=path,
        version=version,
        transition_kind=transition_kind,
        application_state_hint=application_state_hint,
    )


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def _read_text(path: Path) -> str:
    """Read `path` as UTF-8 text. Used by tests to swap in fixtures."""
    return path.read_text(encoding="utf-8")


def load_sop_index(
    sops_dir: str | Path,
    *,
    reader: Any = None,
) -> SopIndex:
    """Load the SOP index from `sops_dir` (read-only).

    `reader(path) -> str` is an injection point for tests; default
    reads the file as UTF-8. All `*.md` files immediately under
    `sops_dir` are considered (subdirectories are skipped — SOPs
    stay flat per `sops/INDEX.md` Directory Contract).
    """
    base = Path(sops_dir)
    if not base.exists():
        raise SopIndexLoadError(
            str(base), "sops_dir_missing",
            f"sops directory does not exist: {base}",
        )
    if not base.is_dir():
        raise SopIndexLoadError(
            str(base), "sops_dir_not_directory",
            f"path is not a directory: {base}",
        )

    reader = reader or _read_text
    entries: list[SopIndexEntry] = []
    # SOPs live flat under `sops/`. INDEX.md is the catalog document
    # and TEMPLATE.md is the authoring template; neither is a runnable
    # SOP, so the loader skips them rather than treating them as
    # malformed entries (per `sops/INDEX.md` Directory Contract).
    skip_filenames: frozenset[str] = frozenset({"INDEX.md", "TEMPLATE.md"})
    for path in sorted(base.glob("*.md")):
        if path.name in skip_filenames:
            continue
        try:
            text = reader(path)
        except OSError as e:
            raise SopIndexLoadError(
                str(path), "read_failed", str(e),
            )
        try:
            parsed = _parse_frontmatter(text)
            entry = _coerce_entry(parsed, str(path))
        except ValueError as e:
            raise SopIndexLoadError(
                str(path), "frontmatter_invalid", str(e),
            )
        entries.append(entry)

    # Deterministic order — secondary sort by path so duplicate
    # sop_ids keep stable order before the duplicate check below
    # raises a load error (fail fast).
    entries.sort(key=lambda e: (e.sop_id, e.path))

    # Duplicate sop_id detection — P2.1 review issue: frontmatter
    # is a wire contract; two SOPs sharing the same sop_id would
    # silently win/lose during `SopIndex.get(sop_id)`, hiding
    # recovery-policy drift between SOPs. Fail fast at load time.
    #
    # Round 3 review: track the first-occurrence path per sop_id
    # so the error message names both files. This collapses debug
    # time when two SOPs clash (common during refactors).
    first_seen: dict[str, str] = {}
    for entry in entries:
        if entry.sop_id in first_seen:
            first_path = first_seen[entry.sop_id]
            raise SopIndexLoadError(
                entry.path, "duplicate_sop_id",
                f"duplicate sop_id={entry.sop_id!r}: "
                f"first={first_path} second={entry.path}",
            )
        first_seen[entry.sop_id] = entry.path
    return SopIndex(entries=tuple(entries), version="1.0.0")


__all__ = [
    "SopIndexLoadError",
    "SopIndexEntry",
    "SopIndex",
    "load_sop_index",
]