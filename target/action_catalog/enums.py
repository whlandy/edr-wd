"""
enums.py — V1 catalog enums (architecture §7.1 table).

Single point of truth for the documented enum sets. Imported by
`action_catalog.models` and by tests.

Stability: changing an enum value is a MAJOR bump (architecture §7.2)
because every catalog consumer validates against these constants.
"""

from __future__ import annotations


VALID_CATEGORY: frozenset[str] = frozenset({
    "observation",
    "session",
    "semantic_input",
    "pointer_input",
    "workflow",
})


VALID_SIDE_EFFECT: frozenset[str] = frozenset({
    "none",
    "session_mutation",
    "gui_mutation",
    "system_mutation",
})


VALID_RISK: frozenset[str] = frozenset({
    "low",
    "medium",
    "high",
    "irreversible",
})


VALID_ROLLBACK_CLASS: frozenset[str] = frozenset({
    "reversible",
    "reconstructable",
    "logical_only",
    "irreversible",
})


VALID_DEFAULT_SCREENSHOT: frozenset[str] = frozenset({
    "none",
    "after",
    "before_after",
    "on_failure",
})


VALID_TRANSITION_POLICY: frozenset[str] = frozenset({
    "never",
    "possible",
    "expected",
    "required_checkpoint",
})


VALID_REQUIRES: frozenset[str] = frozenset({
    "connected_window",
    "window_lock",
    "target_ref",
})


VALID_EXECUTION_PROVIDER: frozenset[str] = frozenset({
    # The action is dispatched to a backend method (P1.1 dispatcher).
    "backend",
    # The action is implemented inline by the target server (currently
    # only `restore_edr`). The catalog still owns the entry; the
    # dispatch happens in server.py because no backend implements it.
    # This keeps the catalog the single source of truth.
    "server_inline",
})


VALID_BACKENDS: frozenset[str] = frozenset({
    "windows_pywinauto",
    "macos_accessibility",
})


# Per-backend tool-method gap table.
#
# The V1 catalog declares that an action is "statically supported on
# backend X" via `spec.backends`. In a few cases a backend nominally
# supports the action category but the tool method is genuinely
# absent (no equivalent UIA/AX API on that platform today). To keep
# `status.action_space` byte-identical with the pre-P0.1 hard-coded
# map, those gaps are declared HERE, not in server.py.
#
# Format: { backend_name: { tool_name: disabled_reason } }
#
# P0.1 surfaces these as `enabled: False` in `status.action_space`
# AND in `get_action_catalog(backend=..., include_disabled=True)`.
# P1.1 dispatcher will consult the same table to refuse dispatch
# with `code: "backend_disabled"` and `disabled_reason`.
#
# Adding a new entry here is a minor bump (it changes observable
# status.action_space, but consumers should already treat that map
# as live-state). Removing one is also a minor bump when the
# underlying backend gains the method.
BACKEND_NOT_IMPLEMENTED: dict[str, dict[str, str]] = {
    "macos_accessibility": {
        "type_text": "not implemented by macos_accessibility (no AX setValue path)",
        "select":    "not implemented by macos_accessibility (no AX picker action)",
        "get_text":  "not implemented by macos_accessibility (no AX value readback)",
    },
}


__all__ = [
    "VALID_CATEGORY",
    "VALID_SIDE_EFFECT",
    "VALID_RISK",
    "VALID_ROLLBACK_CLASS",
    "VALID_DEFAULT_SCREENSHOT",
    "VALID_TRANSITION_POLICY",
    "VALID_REQUIRES",
    "VALID_EXECUTION_PROVIDER",
    "VALID_BACKENDS",
    "BACKEND_NOT_IMPLEMENTED",
]