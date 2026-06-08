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

Windows UIA-specific primitives:
    dump_tree(max_depth=10)
    click(automation_id, control_id, text, class_name, ...)
    click_target(...)
    click_window_at(...)
    type_text(...)
    select(...)
    get_text(...)
    restore_edr()       — HiSec EDR specific

Cross-backend HiSec-specific:
    activate_edr(...)   — implemented separately by Windows and macOS

These methods are still part of the abstract interface, but macOS
implementations may return {"ok": False, "error": "not supported on
this platform"} for them. The MCP tool wrappers handle the
"unsupported on this backend" case uniformly.
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
    def click_at(self, x: int, y: int) -> dict: ...

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
        timeout: float = 5.0,
    ) -> dict: ...
    def click_target(
        self,
        control_id: Optional[int] = None,
        text: Optional[str] = None,
        class_name: Optional[str] = None,
        parent_text: Optional[str] = None,
        automation_id: Optional[str] = None,
        timeout: float = 5.0,
    ) -> dict: ...
    def click_window_at(self, x: int, y: int, window_title_re: Optional[str] = None) -> dict: ...
    def type_text(
        self,
        text: str,
        control_id: Optional[int] = None,
        class_name: Optional[str] = None,
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
