"""Schemas for CHM-derived manual knowledge artifacts.

These dataclasses are the on-disk contract for the phased edr-rag work:

- P0 writes ManualSection and AssetRef JSONL.
- P1 writes Procedure and RetrievalChunk JSONL.
- P2 writes ActionCatalogEntry YAML/JSONL.
- P3 writes FeedbackItem JSONL.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


JsonDict = dict[str, Any]


@dataclass(slots=True)
class SourceRef:
    """A stable reference back to the extracted manual source."""

    file: str
    section_id: str | None = None
    procedure_id: str | None = None
    anchor: str | None = None

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(slots=True)
class AssetRef:
    """An image or other asset extracted from a CHM manual."""

    id: str
    manual_id: str
    type: str
    path: str
    absolute_path: str | None = None
    referenced_by: list[str] = field(default_factory=list)
    description: str | None = None

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(slots=True)
class ManualSection:
    """Normalized section parsed from CHM HTML and TOC structure."""

    id: str
    manual_id: str
    product: str
    title: str
    section_path: list[str]
    source_file: str
    content: str
    anchor: str | None = None
    headings: list[str] = field(default_factory=list)
    links: list[JsonDict] = field(default_factory=list)
    images: list[JsonDict] = field(default_factory=list)
    metadata: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(slots=True)
class ProcedureStep:
    """One ordered instruction extracted from a manual procedure."""

    order: int
    instruction: str
    target: str | None = None
    action_hint: str | None = None

    def to_dict(self) -> JsonDict:
        return asdict(self)


@dataclass(slots=True)
class Procedure:
    """A structured SOP candidate extracted from manual sections."""

    id: str
    manual_id: str
    product: str
    operation: str
    title: str
    section_path: list[str]
    source: SourceRef
    steps: list[ProcedureStep]
    preconditions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    recovery: list[str] = field(default_factory=list)
    confidence: float | None = None
    status: str = "candidate"

    def to_dict(self) -> JsonDict:
        data = asdict(self)
        data["source"] = self.source.to_dict()
        data["steps"] = [step.to_dict() for step in self.steps]
        return data


@dataclass(slots=True)
class RetrievalChunk:
    """Searchable chunk generated from sections, procedures, or actions."""

    id: str
    manual_id: str
    type: str
    text: str
    source: SourceRef
    metadata: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        data = asdict(self)
        data["source"] = self.source.to_dict()
        return data


@dataclass(slots=True)
class ActionCatalogEntry:
    """Reviewable action candidate derived from a Procedure."""

    action_id: str
    product: str
    module: str
    title: str
    source: SourceRef
    procedure: list[JsonDict]
    status: str = "candidate"
    confidence: float | None = None
    risk: str = "unknown"
    requires_confirmation: bool = True
    preconditions: list[str] = field(default_factory=list)
    success_criteria: list[JsonDict] = field(default_factory=list)
    recovery: list[str] = field(default_factory=list)
    validation: JsonDict = field(default_factory=dict)

    def to_dict(self) -> JsonDict:
        data = asdict(self)
        data["source"] = self.source.to_dict()
        return data


@dataclass(slots=True)
class FeedbackItem:
    """Execution feedback used by P3 to improve catalog and retrieval data."""

    feedback_id: str
    manual_id: str
    failure_type: str
    action_id: str | None = None
    procedure_id: str | None = None
    expected: str | None = None
    observed: str | None = None
    evidence: JsonDict = field(default_factory=dict)
    suggested_patch: JsonDict = field(default_factory=dict)
    status: str = "needs_review"
    created_at: str | None = None

    def to_dict(self) -> JsonDict:
        return asdict(self)
