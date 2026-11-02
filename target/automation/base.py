"""
base.py — AutomationBackend abstract interface.

A backend owns the question: "given a target GUI session, what can I see,
what can I click, and what is the screen doing right now?"

Implementations are platform-specific (pywinauto, AppleScript, Quartz,
etc.) and are constructed by create_backend(name).

Contract:
    - All public methods return a dict of shape:
        {"ok": bool, "error"?: str, ...}
      so MCP tools can return it directly as JSON.
    - Methods that operate on a connected app (click, dump_tree, type_text,
      get_text, etc.) require a prior `connect()` call. If not connected,
      they return {"ok": False, "error": "Not connected"}.

Minimum viable interface (M4/M5):
    list_windows() -> dict
    is_window_open(process_name=, title_re=, class_name=) -> dict
    wait_window(...) -> dict
    activate_app(app_name=, bundle_id=) -> dict
    screenshot(path=None) -> dict
    click_at(x, y) -> dict
    connect(process_name=, title_re=, app_name=, bundle_id=,
            pid=, timeout=, auto_activate=False) -> dict

Discovery and connected-window interaction APIs:
    dump_tree(max_depth=10)
    click(automation_id, control_id, text, class_name, ...)
    click_target(...)
    click_window_at(...)
    type_text(...)
    select(...)
    get_text(...)
    activate_edr(...)   — HiSec EDR specific
    restore_edr()       — HiSec EDR specific

Windows backends usually implement the full selector-driven surface.
macOS backends may implement only a subset and return
{"ok": False, "error": "not supported on this backend"} for the rest.
The MCP tool wrappers handle the "unsupported on this backend" case
uniformly.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable, Any, Optional


@runtime_checkable
class AutomationBackend(Protocol):
    """Protocol for GUI automation backends."""

    @property
    def backend(self) -> str:
        """Return a short identifier (e.g. 'uia', 'macos_accessibility')."""
        ...

    # ── Always-available ─────────────────────────────────────────────────────

    def list_windows(self) -> dict: ...
    def is_window_open(
        self,
        title_re: Optional[str] = None,
        process_name: Optional[str] = None,
        class_name: Optional[str] = None,
    ) -> dict: ...
    def wait_window(
        self,
        title_re: Optional[str] = None,
        process_name: Optional[str] = None,
        class_name: Optional[str] = None,
        timeout: float = 10.0,
        interval: float = 0.5,
    ) -> dict: ...
    def activate_app(
        self,
        app_name: Optional[str] = None,
        bundle_id: Optional[str] = None,
    ) -> dict: ...
    def screenshot(self, path: Optional[str] = None) -> dict: ...
    def click_at(self, x: int, y: int, expected_process_name: Optional[str] = None) -> dict: ...
    def double_click_at(self, x: int, y: int, expected_process_name: Optional[str] = None) -> dict: ...
    def right_click_at(self, x: int, y: int, expected_process_name: Optional[str] = None) -> dict: ...
    def middle_click_at(self, x: int, y: int, expected_process_name: Optional[str] = None) -> dict: ...
    def hover_at(self, x: int, y: int) -> dict: ...
    def drag(
        self,
        x1: int,
        y1: int,
        x2: int,
        y2: int,
        duration: float = 0.25,
    ) -> dict: ...
    def scroll(
        self,
        clicks: int,
        x: Optional[int] = None,
        y: Optional[int] = None,
    ) -> dict: ...
    def scroll_window(
        self,
        clicks: int,
        x: int,
        y: int,
        window_title_re: Optional[str] = None,
        expected_process_name: Optional[str] = None,
        expected_pid: Optional[int] = None,
    ) -> dict: ...
    def lock_window(
        self,
        title_re: Optional[str] = None,
        process_name: Optional[str] = None,
        pid: Optional[int] = None,
        strict: bool = True,
        activate: bool = True,
    ) -> dict: ...
    def unlock_window(self) -> dict: ...
    def get_window_lock(self) -> dict: ...
    def verify_window_lock(self, activate: bool = True) -> dict: ...

    # ── Connect-required ─────────────────────────────────────────────────────

    def connect(
        self,
        title_re: Optional[str] = None,
        process_name: Optional[str] = None,
        pid: Optional[int] = None,
        app_name: Optional[str] = None,
        bundle_id: Optional[str] = None,
        timeout: float = 10.0,
        auto_activate: bool = False,
    ) -> dict: ...
    def dump_tree(
        self, window_title_re: Optional[str] = None, max_depth: int = 10
    ) -> dict: ...
    def find_control(
        self,
        text: Optional[str] = None,
        role: Optional[str] = None,
        identifier: Optional[str] = None,
        title_re: Optional[str] = None,
        max_depth: int = 10,
    ) -> dict: ...
    def click(
        self,
        control_id: Optional[int] = None,
        text: Optional[str] = None,
        class_name: Optional[str] = None,
        parent_text: Optional[str] = None,
        automation_id: Optional[str] = None,
        auto_id_contains: Optional[str] = None,
        auto_id_suffix: Optional[str] = None,
        parent_of: Optional[str] = None,
        control_type: Optional[str] = None,
        parent_fallback: bool = True,
        expected_process_name: Optional[str] = None,
        timeout: float = 5.0,
    ) -> dict: ...
    def click_target(
        self,
        control_id: Optional[int] = None,
        text: Optional[str] = None,
        class_name: Optional[str] = None,
        parent_text: Optional[str] = None,
        automation_id: Optional[str] = None,
        auto_id_contains: Optional[str] = None,
        auto_id_suffix: Optional[str] = None,
        parent_of: Optional[str] = None,
        control_type: Optional[str] = None,
        x_offset: int = 0,
        y_offset: int = 0,
        parent_fallback: bool = True,
        expected_process_name: Optional[str] = None,
        timeout: float = 5.0,
    ) -> dict: ...
    def click_window_at(
        self,
        x: int,
        y: int,
        window_title_re: Optional[str] = None,
        expected_process_name: Optional[str] = None,
    ) -> dict: ...
    def type_text(
        self,
        text: str,
        control_id: Optional[int] = None,
        class_name: Optional[str] = None,
        string: Optional[str] = None,
        automation_id: Optional[str] = None,
        control_type: Optional[str] = None,
    ) -> dict: ...
    def select(
        self,
        item: Optional[str] = None,
        index: Optional[int] = None,
        control_id: Optional[int] = None,
        text: Optional[str] = None,
        class_name: Optional[str] = None,
    ) -> dict: ...
    def get_text(
        self,
        control_id: Optional[int] = None,
        text: Optional[str] = None,
        class_name: Optional[str] = None,
    ) -> dict: ...

    # ── Region / container-row observation (region_query assertions) ────────
    #
    # Read-only sampling for region/multi-component assertions (06 design §5).
    # Backends that cannot answer return the structured "unavailable" shape;
    # callers treat that as undetermined evidence, never as False.

    def query_container_rows(
        self,
        container: Optional[dict] = None,
        fields: Optional[list] = None,
    ) -> dict:
        """Read the current rows of one container (stable identity in ``container``).

        Expected shape: ``{"ok": True, "rows": [{"rowIndex", <field>: value, ...}]}``
        or ``{"ok": False, "error": str}``. One call returns one same-frame
        snapshot; the caller re-samples per observation tick.
        """
        return {"ok": False, "error": "query_container_rows not supported on this backend"}

    def read_component_values(
        self,
        components: Optional[list] = None,
    ) -> dict:
        """Read the current value of each fixed component ({"field", "identity"})."""
        return {"ok": False, "error": "read_component_values not supported on this backend"}


def lock_criteria_mismatch(
    state: dict,
    title_re: Optional[str] = None,
    process_name: Optional[str] = None,
    pid: Optional[int] = None,
) -> list[str]:
    """Name the lock criteria the window in ``state`` does not satisfy.

    ``lock_window`` selects nothing: it locks whichever window ``connect()``
    already resolved.  Its ``title_re`` / ``process_name`` / ``pid`` arguments
    therefore describe what the caller *believes* is connected, and a lock that
    silently stores those beliefs next to a different window's handle sends
    later pointer actions to the wrong application.  Backends call this to
    refuse that lock instead of recording it.

    Only criteria whose live counterpart is actually observable are compared —
    an unknown process name or PID degrades to "cannot contradict", so RDP and
    other degraded-observation paths keep working.
    """
    import re as _re

    mismatch: list[str] = []
    if title_re:
        title = state.get("title")
        if title is not None:
            try:
                if not _re.search(title_re, str(title), _re.IGNORECASE):
                    mismatch.append("title_re")
            except _re.error:
                mismatch.append("title_re")
    if process_name:
        actual = state.get("process_name")
        if actual:
            normalized = str(actual).lower().removesuffix(".exe")
            if normalized != str(process_name).lower().removesuffix(".exe"):
                mismatch.append("process_name")
    if pid is not None and state.get("pid") is not None:
        try:
            if int(state["pid"]) != int(pid):
                mismatch.append("pid")
        except (TypeError, ValueError):
            mismatch.append("pid")
    return mismatch


def release_window_lock_if_window_changed(backend) -> Optional[dict]:
    """Drop a session's window lock the connected window no longer matches.

    Some entry points rebind the session's connected window without going
    through ``connect()`` — ``activate_edr`` rebinds inside the GUI layer on
    both backends (the agent fallback / client hand-off / macOS connected
    state rewrite), and the Windows legacy ``connect_by_title`` /
    ``connect_by_process`` / ``connect_by_pid`` aliases rebind directly.  A
    window lock left over from before such a rebind describes the OLD
    window: ``get_window_lock`` then reports ``locked=True`` to the new
    session with the old identity, and a later verify can resurrect a lock
    this session never confirmed.

    Semantics follow ``lock_criteria_mismatch``: only observable fields are
    compared — pid, handle, title.  A state that cannot be observed
    (``ok=False``) cannot contradict the lock and keeps it; a match keeps
    the lock so an already-open path does not break a live ownership chain.
    Dropping the lock only REMOVES authorization (pointer injection is then
    refused at ``_ensure_window_lock``); it never widens it.

    Returns the released lock's identity for audit, or None when the lock
    stayed (no lock, still matching, or unobservable state).
    """
    lock = getattr(backend, "_window_lock", None)
    if not isinstance(lock, dict):
        return None
    try:
        state = backend._connected_window_state()
    except Exception:
        return None
    if not state.get("ok"):
        return None
    mismatch: list[str] = []
    if lock.get("pid") is not None and state.get("pid") is not None:
        if int(state["pid"]) != int(lock["pid"]):
            mismatch.append("pid")
    if lock.get("handle") is not None and state.get("handle") is not None:
        if int(state["handle"]) != int(lock["handle"]):
            mismatch.append("handle")
    if lock.get("title_re") and state.get("title") is not None:
        import re as _re
        try:
            if not _re.search(str(lock["title_re"]), str(state["title"]),
                              _re.IGNORECASE):
                mismatch.append("title_re")
        except _re.error:
            mismatch.append("title_re")
    if not mismatch:
        return None
    backend._window_lock = None
    return {key: lock.get(key) for key in ("handle", "pid", "title_re", "process_name")}
