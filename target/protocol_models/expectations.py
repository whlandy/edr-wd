"""
expectations.py — Typed expectation registry (architecture §9.3, FR-P0.2-09).

P0.2 declares the registry of expectation types and the contract that
each evaluator must satisfy. The actual evaluator *implementations*
(how the assertion is computed against an `ObservationSnapshot`) land
in P1.2 alongside the atomic executor.

For P0.2:

    * `_EXPECTATION_TYPE_REGISTRY_RAW` (private dict) is the source of
      truth. It is wrapped at import time in `MappingProxyType` and
      exposed as `EXPECTATION_TYPE_REGISTRY`. The proxy rejects
      `__setitem__` / `__delitem__`, so downstream consumers cannot
      mutate the registry (P0.2 review feedback).
    * `validate_expectation_type(type)` raises
      `ProtocolModelError(code="enum_error")` for unknown types.
    * `iter_expectation_types()` is a stable iteration order for
      trace reports.

This module is importable by the validator (which uses it to assert
the type is registered) and by P1.2 (which adds evaluator callables).

Stability: adding a new expectation type is a MINOR bump. Removing
one is a MAJOR bump.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from .enums import (
    EXPECTATION_ACTION_OK,
    EXPECTATION_ACTIVE_WINDOW_OWNER,
    EXPECTATION_CONTROL_ABSENT,
    EXPECTATION_CONTROL_EXISTS,
    EXPECTATION_CONTROL_TEXT_CONTAINS,
    EXPECTATION_CONTROL_TEXT_EQUALS,
    EXPECTATION_VISUAL_EVIDENCE_CAPTURED,
    EXPECTATION_WINDOW_CLOSED,
    EXPECTATION_WINDOW_OPEN,
    EXPECTATION_WINDOW_TEXT_CONTAINS,
    VALID_EXPECTATION_TYPES,
)
from .models import ProtocolModelError


@dataclass(frozen=True)
class ExpectationTypeSpec:
    """Descriptor for one registered expectation type."""

    type: str
    description: str
    required_fields: tuple[str, ...]


# Private dict; wrapped at import time. Never mutate after module load.
_EXPECTATION_TYPE_REGISTRY_RAW: dict[str, ExpectationTypeSpec] = {
    EXPECTATION_ACTION_OK: ExpectationTypeSpec(
        type=EXPECTATION_ACTION_OK,
        description="The action receipt reports ok=true.",
        required_fields=(),
    ),
    EXPECTATION_WINDOW_OPEN: ExpectationTypeSpec(
        type=EXPECTATION_WINDOW_OPEN,
        description="A window matching the value fields exists.",
        required_fields=("process_name",),
    ),
    EXPECTATION_WINDOW_CLOSED: ExpectationTypeSpec(
        type=EXPECTATION_WINDOW_CLOSED,
        description="No matching window exists.",
        required_fields=("process_name",),
    ),
    EXPECTATION_ACTIVE_WINDOW_OWNER: ExpectationTypeSpec(
        type=EXPECTATION_ACTIVE_WINDOW_OWNER,
        description="Active window belongs to expected process.",
        required_fields=("process_name",),
    ),
    EXPECTATION_CONTROL_EXISTS: ExpectationTypeSpec(
        type=EXPECTATION_CONTROL_EXISTS,
        description="A unique matching control exists in the snapshot.",
        required_fields=("selector",),
    ),
    EXPECTATION_CONTROL_ABSENT: ExpectationTypeSpec(
        type=EXPECTATION_CONTROL_ABSENT,
        description="No matching control exists in the snapshot.",
        required_fields=("selector",),
    ),
    EXPECTATION_CONTROL_TEXT_EQUALS: ExpectationTypeSpec(
        type=EXPECTATION_CONTROL_TEXT_EQUALS,
        description="Normalized control text equals the value.",
        required_fields=("selector", "value"),
    ),
    EXPECTATION_CONTROL_TEXT_CONTAINS: ExpectationTypeSpec(
        type=EXPECTATION_CONTROL_TEXT_CONTAINS,
        description="Normalized control text contains the value.",
        required_fields=("selector", "value"),
    ),
    EXPECTATION_WINDOW_TEXT_CONTAINS: ExpectationTypeSpec(
        type=EXPECTATION_WINDOW_TEXT_CONTAINS,
        description="Observed window/tree text contains the value.",
        required_fields=("value",),
    ),
    EXPECTATION_VISUAL_EVIDENCE_CAPTURED: ExpectationTypeSpec(
        type=EXPECTATION_VISUAL_EVIDENCE_CAPTURED,
        description="Required screenshot was persisted with a matching digest.",
        required_fields=("screenshot_role",),
    ),
}


# Public immutable view. MappingProxyType supports `__contains__`,
# `__getitem__`, `__iter__`, `__len__`, `keys()`, `values()`,
# `items()` — but NOT `__setitem__` / `__delitem__`. Any attempt
# to mutate raises `TypeError`.
EXPECTATION_TYPE_REGISTRY: Mapping[str, ExpectationTypeSpec] = MappingProxyType(
    _EXPECTATION_TYPE_REGISTRY_RAW
)


def validate_expectation_type(type_name: str) -> None:
    """Raise `ProtocolModelError(code='enum_error')` if unknown."""
    if type_name not in VALID_EXPECTATION_TYPES:
        raise ProtocolModelError(
            "enum_error",
            f"expectation type {type_name!r} is not registered; "
            f"known types: {sorted(VALID_EXPECTATION_TYPES)}",
            path="type",
            value=type_name,
        )


def iter_expectation_types() -> tuple[ExpectationTypeSpec, ...]:
    """Stable iteration order for trace reports / docs."""
    return tuple(EXPECTATION_TYPE_REGISTRY[t] for t in sorted(EXPECTATION_TYPE_REGISTRY))


__all__ = [
    "ExpectationTypeSpec",
    "EXPECTATION_TYPE_REGISTRY",
    "validate_expectation_type",
    "iter_expectation_types",
]