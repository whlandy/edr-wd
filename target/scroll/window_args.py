"""
window_args.py — Unified Window Argument Schema (P1.2).

Normalizes the many legacy window-selection spellings scattered across the
MCP tools (``window_title_re`` / ``expected_process_name`` / ``expected_pid``
/ positional ``process_name`` / ``pid``) into ONE canonical ``window`` object
so agents can target a window without reading server.py:

    {
      "title_re": "^日志中心$",
      "process_name": "EDRClient.exe",
      "pid": 6752,
      "handle": 66336
    }

Design rules (P1.2 acceptance):

* New composite tools accept ``window: dict = None``. When present it is the
  primary window selector.
* Legacy parameters remain fully compatible. Every field falls back
  independently: ``window`` wins, otherwise the legacy alias (below) is used.
* ``handle`` feeds ``native_window_id`` on the resolved window meta (and the
  backend's ``dump_tree`` where it accepts a handle).
* An unknown key inside ``window`` is a caller typo (e.g. ``title`` instead of
  ``title_re``) and must fail loudly rather than be silently ignored — that is
  exactly the class of bug that makes agents debug against server.py.

Legacy alias map (canonical field -> each tool's legacy spelling):

    title_re      -> window_title_re
    process_name  -> process_name  (also expected_process_name on pointer tools)
    pid           -> pid           (also expected_pid on pointer tools)
    handle        -> native_window_id

Pure module — no MCP transport. Importable from server.py and the test suite.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

# The canonical window selector field names, in documentation order.
WINDOW_FIELDS = ("title_re", "process_name", "pid", "handle")


def validate_window(window: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    """Validate a user-supplied ``window`` selector mapping.

    Returns it unchanged on success; raises ``ValueError`` on any unknown key
    so a caller typo is caught at the tool boundary instead of silently lost.
    An empty/dictless ``None`` passes through (no window selection).
    """
    if window is None:
        return {}
    if not isinstance(window, Mapping):
        raise ValueError(
            f"window must be a JSON object with fields "
            f"{list(WINDOW_FIELDS)}, got {type(window).__name__}"
        )
    unknown = [k for k in window if k not in WINDOW_FIELDS]
    if unknown:
        raise ValueError(
            f"unknown window field(s) {unknown}; expected only "
            f"{list(WINDOW_FIELDS)}"
        )
    return window


def _first(*values: Any) -> Any:
    """First non-None value, else None."""
    for v in values:
        if v is not None:
            return v
    return None


def resolve_window(
    window: Optional[Mapping[str, Any]] = None,
    *,
    window_title_re: Optional[str] = None,
    process_name: Optional[str] = None,
    pid: Optional[int] = None,
    expected_process_name: Optional[str] = None,
    expected_pid: Optional[int] = None,
) -> dict:
    """Merge a canonical ``window`` selector over the legacy aliases.

    Returns a dict with the canonical four keys (plus ``native_window_id``
    derived from ``handle``) ready to feed a ``ScrollBackendSource`` or the
    backend pointer tools. ``window`` takes precedence field-by-field; each
    legacy alias stands alone so no cross-field coupling is introduced.

    raise_window: raises ValueError on unknown window keys (via validate).
    """
    w = validate_window(window)
    title = _first(w.get("title_re"), window_title_re)
    proc = _first(w.get("process_name"), process_name, expected_process_name)
    p = _first(w.get("pid"), pid, expected_pid)
    handle = w.get("handle")
    return {
        "window_title_re": title,
        "window_title": title,
        "process_name": proc,
        "pid": p,
        "native_window_id": _first(handle, None),
    }


def window_doc(clause: str = "Selects the target window.") -> str:
    """A ready-to-embed docstring fragment documenting the ``window`` object.

    ``clause`` is a one-line sentence placed before the JSON example. The
    example is an executable JSON selector (not prose) so agents can copy it
    directly without inspecting server.py (P1.2 acceptance #3).
    """
    return (
        f"{clause} Prefer a single ``window`` object of the form "
        '``{"title_re": "^日志中心$", "process_name": "EDRClient.exe", '
        '"pid": 6752, "handle": 66336}`` — all fields optional, at least one '
        "should be set. Legacy per-field parameters (window_title_re / "
        "process_name / pid / expected_process_name / expected_pid) remain "
        "supported and are overridden field-by-field by ``window`` when both are given."
    )
