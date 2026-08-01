"""
models.py — P0.3 wire models (architecture §8.1).

Frozen dataclasses with strict unknown-field rejection at
`from_dict`, mirroring the P0.2 contract. Two models live here:

* `Target` — one observed window/control within a snapshot.
* `ObservationSnapshot` — one observation event, with a deterministic
  list of `Target` objects and a content-addressed `tree_digest`.

Stability (architecture §7.2):

  * Adding a new field → patch bump; consumers using `from_dict`
    will see a strict-mode `unknown_field` error until they adopt
    the new field.
  * Removing a field → major bump.
  * Changing a field's meaning → major bump.

Stable model codes:

  * `unknown_field` — extra key in kwargs.
  * `type_error` — value of wrong type.
  * `empty_value` — required string/sequence is empty.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from protocol_models.models import ProtocolModelError

from ._strict import (
    _allow_none_or_str,
    _require_int_or_none,
    _require_str,
    _strict_from_dict_kwargs,
)
from .enums import TARGET_KIND_CONTROL, TARGET_KIND_WINDOW, VALID_TARGET_KINDS


# Snapshot schema version (architecture §8.1). Independent from the
# catalog schema version (P0.1) and the protocol schema version (P0.2).
OBSERVATION_SCHEMA_VERSION = "1.0.0"


# Re-use ProtocolModelError from P0.2 — keeps error codes consistent.
# `unknown_field`, `type_error`, `enum_error`, `empty_value` are the
# stable codes used at every model layer.


# target_id format check (T0001, T0002, ...).
_TARGET_ID_RE = re.compile(r"^T(\d{4,})$")


def _require_target_id(value: Any, *, path: str) -> str:
    if not isinstance(value, str) or not _TARGET_ID_RE.match(value):
        raise ProtocolModelError(
            "type_error",
            f"target_id must match pattern T####, got {value!r}",
            path=path,
            value=value,
        )
    return value


# ---------------------------------------------------------------------------
# Target — one observed window/control
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """One observed window or control within a snapshot.

    `target_id` is observation-local (architecture §8.1): a `T0001`
    in snapshot A is unrelated to a `T0001` in snapshot B. The
    resolver uses `fingerprint` to detect when the same underlying
    control reappears across snapshots.

    `fingerprint_fields` lists the keys that contributed to the
    fingerprint digest (architecture §8.3): consumers can compare
    this list to debug a stale-target resolution.
    """

    target_id: str
    kind: str
    process_name: str
    pid: int | None
    native_window_id: str
    title: str
    control_type: str | None = None
    automation_id: str | None = None
    text: str | None = None
    rect: tuple[int, int, int, int] | None = None
    fingerprint: str = ""
    fingerprint_fields: tuple[str, ...] = field(default_factory=tuple)

    _ALLOWED: frozenset[str] = frozenset({
        "target_id", "kind", "process_name", "pid", "native_window_id",
        "title", "control_type", "automation_id", "text", "rect",
        "fingerprint", "fingerprint_fields",
    })

    def __post_init__(self) -> None:
        _require_target_id(self.target_id, path="target_id")
        if self.kind not in VALID_TARGET_KINDS:
            raise ProtocolModelError(
                "type_error",
                f"kind must be one of {sorted(VALID_TARGET_KINDS)}, got {self.kind!r}",
                path="kind",
                value=self.kind,
            )
        _require_str(self.process_name, path="process_name",
                     field_name="process_name")
        _require_int_or_none(self.pid, path="pid", field_name="pid")
        _require_str(self.native_window_id, path="native_window_id",
                     field_name="native_window_id")
        if self.title:
            _require_str(self.title, path="title", field_name="title")
        if self.fingerprint:
            _require_str(self.fingerprint, path="fingerprint",
                         field_name="fingerprint")
        if not isinstance(self.fingerprint_fields, tuple):
            raise ProtocolModelError(
                "type_error",
                f"fingerprint_fields must be a tuple, got {type(self.fingerprint_fields).__name__}",
                path="fingerprint_fields",
                value=type(self.fingerprint_fields).__name__,
            )
        if self.rect is not None:
            if (not isinstance(self.rect, tuple)
                or len(self.rect) != 4
                or not all(isinstance(v, int) for v in self.rect)):
                raise ProtocolModelError(
                    "type_error",
                    f"rect must be a 4-tuple of ints, got {self.rect!r}",
                    path="rect",
                    value=self.rect,
                )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Target":
        kwargs = _strict_from_dict_kwargs(
            cls._ALLOWED, data, path="Target",
        )
        # JSON parsing yields lists, not tuples.
        if "rect" in kwargs and kwargs["rect"] is not None:
            kwargs["rect"] = tuple(kwargs["rect"])
        if "fingerprint_fields" in kwargs and kwargs["fingerprint_fields"] is not None:
            kwargs["fingerprint_fields"] = tuple(kwargs["fingerprint_fields"])
        return cls(**kwargs)


# ---------------------------------------------------------------------------
# ObservationSnapshot — one observation event
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObservationSnapshot:
    """One observation event for a connected window.

    `target_ids` is the snapshot's deterministic assignment
    (T0001, T0002, ...). `targets` is in the same order. The list is
    sorted by `(process_name, pid, native_window_id)` per
    architecture §8.1, then by backend's stable traversal order.

    `tree_digest` is `sha256:<hex>` over canonical bytes of every
    target's fingerprint — it changes if and only if any target's
    stable fields change. The resolver compares tree_digest to detect
    stale references (architecture §8.3).

    `screenshot_evidence_id` is optional; P0.3 does not capture
    screenshots. P1.4 will populate it.
    """

    schema_version: str
    snapshot_id: str
    captured_at: str
    backend: str
    host: str
    active_window: Target | None
    targets: tuple[Target, ...]
    tree_digest: str
    screenshot_evidence_id: str | None = None

    _ALLOWED: frozenset[str] = frozenset({
        "schema_version", "snapshot_id", "captured_at", "backend", "host",
        "active_window", "targets", "tree_digest",
        "screenshot_evidence_id",
    })

    def __post_init__(self) -> None:
        _require_str(self.schema_version, path="schema_version",
                     field_name="schema_version")
        _require_str(self.snapshot_id, path="snapshot_id",
                     field_name="snapshot_id")
        _require_str(self.captured_at, path="captured_at",
                     field_name="captured_at")
        _require_str(self.backend, path="backend", field_name="backend")
        _require_str(self.host, path="host", field_name="host")
        _require_str(self.tree_digest, path="tree_digest",
                     field_name="tree_digest")
        if not isinstance(self.targets, tuple):
            raise ProtocolModelError(
                "type_error",
                f"targets must be a tuple, got {type(self.targets).__name__}",
                path="targets",
                value=type(self.targets).__name__,
            )
        if self.active_window is not None and not isinstance(
            self.active_window, Target
        ):
            raise ProtocolModelError(
                "type_error",
                f"active_window must be a Target or None, got {type(self.active_window).__name__}",
                path="active_window",
                value=type(self.active_window).__name__,
            )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ObservationSnapshot":
        kwargs = _strict_from_dict_kwargs(
            cls._ALLOWED, data, path="ObservationSnapshot",
        )
        if "active_window" in kwargs and kwargs["active_window"] is not None:
            kwargs["active_window"] = Target.from_dict(kwargs["active_window"])
        if "targets" in kwargs and kwargs["targets"] is not None:
            kwargs["targets"] = tuple(
                Target.from_dict(t) for t in kwargs["targets"]
            )
        return cls(**kwargs)


__all__ = [
    "OBSERVATION_SCHEMA_VERSION",
    "Target",
    "ObservationSnapshot",
]