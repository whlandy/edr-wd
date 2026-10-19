"""Agent-side product semantics; no target connection credentials."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from target.action_catalog import VALID_BACKENDS


@dataclass(frozen=True)
class WindowRole:
    role: str
    process_names: tuple[str, ...]
    title_pattern: str = ""
    root_control_ids: tuple[str, ...] = ()
    parent_role: str | None = None


@dataclass(frozen=True)
class ProductProfile:
    profile_id: str
    profile_version: str
    supported_backends: tuple[str, ...]
    window_roles: tuple[WindowRole, ...]
    allowed_action_ids: frozenset[str]
    digest: str

    def validate(self) -> None:
        if not self.profile_id or not self.profile_version or not self.digest:
            raise ValueError("profile identity, version and digest are required")
        if not self.supported_backends or any(
            backend not in VALID_BACKENDS for backend in self.supported_backends
        ):
            raise ValueError("unknown or missing product backend")
        roles = [item.role for item in self.window_roles]
        if not roles or len(roles) != len(set(roles)):
            raise ValueError("window roles must be present and unique")
        if not self.allowed_action_ids:
            raise ValueError("product has no allowed actions")


class ProductAdapter(Protocol):
    """Product-only role and business semantics; OS input stays in backends."""

    def resolve_window_role(self, role: str) -> WindowRole: ...

    def resolve_intent(self, intent_id: str, args: Mapping[str, Any],
                       **kwargs: Any) -> Any: ...

    def expected_result(self, intent_id: str,
                        args: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]: ...
