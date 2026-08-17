"""
_strict.py — Minimal strict-mode helpers for P0.3 models.

P0.3 deliberately does NOT import the corresponding helpers from
`protocol_models.models` because those names are private
(`_allow_none`, `_require_str`, `_strict_from_dict_kwargs`). P0.2
review established that private symbols must not leak across
checkpoint boundaries (a downstream consumer could come to depend
on them and break future refactors of P0.2).

We therefore duplicate the minimal helpers here. The `ProtocolModelError`
class IS the public P0.2 surface and remains imported.

If `protocol_models` ever exposes a public strict-helper API,
P0.3 can switch to importing that and delete this file.
"""

from __future__ import annotations

from typing import Any, Mapping

try:
    from ..protocol_models.models import ProtocolModelError
except ImportError:  # target-local deployment
    from protocol_models.models import ProtocolModelError


def _require_str(value: Any, *, path: str, field_name: str) -> str:
    """Strict: require a non-empty string."""
    if not isinstance(value, str):
        raise ProtocolModelError(
            "type_error",
            f"{field_name} must be a string, got {type(value).__name__}",
            path=path,
            value=type(value).__name__,
        )
    if not value:
        raise ProtocolModelError(
            "empty_value",
            f"{field_name} must be a non-empty string",
            path=path,
            value=value,
        )
    return value


def _allow_none_or_str(value: Any, *, path: str, field_name: str) -> Any:
    """Strict: require None or a string (including empty string)."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    raise ProtocolModelError(
        "type_error",
        f"{field_name} must be None or a string, got {type(value).__name__}",
        path=path,
        value=type(value).__name__,
    )


def _require_int_or_none(value: Any, *, path: str, field_name: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int):
        raise ProtocolModelError(
            "type_error",
            f"{field_name} must be an int or None, got {type(value).__name__}",
            path=path,
            value=type(value).__name__,
        )
    return value


def _strict_from_dict_kwargs(
    allowed: frozenset[str], data: Mapping[str, Any], *, path: str,
) -> dict[str, Any]:
    """Strict: reject unknown fields at construction."""
    if not isinstance(data, Mapping):
        raise ProtocolModelError(
            "type_error",
            f"expected mapping, got {type(data).__name__}",
            path=path,
            value=type(data).__name__,
        )
    out: dict[str, Any] = {}
    for k, v in data.items():
        if k not in allowed:
            raise ProtocolModelError(
                "unknown_field",
                f"field {k!r} is not allowed here",
                path=path,
                value=k,
            )
        out[k] = v
    return out


__all__ = [
    "_require_str",
    "_allow_none_or_str",
    "_require_int_or_none",
    "_strict_from_dict_kwargs",
]
