"""A native window ID is scoped to a session and a lifecycle generation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Mapping


class WindowIdentityError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class WindowIdentity:
    session_id: str
    backend: str
    native_id: str
    process_id: int
    process_name: str
    title: str
    rect: tuple[int, int, int, int]
    role: str = ""
    root_control_id: str = ""
    class_name: str = ""
    parent_native_id: str | None = None
    opened_by_action_id: str | None = None
    visible: bool = True
    foreground: bool = False
    generation: int = 1

    @classmethod
    def from_native(cls, snapshot: Mapping, *, session_id: str,
                    backend: str, generation: int = 1) -> "WindowIdentity":
        if not session_id or not backend:
            raise WindowIdentityError("window_identity_invalid")
        native = snapshot.get("handle") or snapshot.get("native_id") or snapshot.get("cg_window_id")
        # AX-only windows require a stable reference; a title is never enough.
        if native is None and backend == "macos_accessibility":
            native = snapshot.get("ax_window_ref")
        if native is None:
            raise WindowIdentityError("window_identity_unavailable")
        pid = snapshot.get("pid", snapshot.get("process_id"))
        if pid is None:
            raise WindowIdentityError("window_identity_unavailable")
        rect = snapshot.get("rect") or snapshot.get("rectangle") or {}
        if isinstance(rect, Mapping):
            left = int(rect.get("left", rect.get("x", 0)))
            top = int(rect.get("top", rect.get("y", 0)))
            right = int(rect.get("right", left + int(rect.get("width", rect.get("w", 0)))))
            bottom = int(rect.get("bottom", top + int(rect.get("height", rect.get("h", 0)))))
            normalized = (left, top, right, bottom)
        elif isinstance(rect, (list, tuple)) and len(rect) == 4:
            normalized = tuple(map(int, rect))
        else:
            raise WindowIdentityError("window_identity_invalid")
        if normalized[2] <= normalized[0] or normalized[3] <= normalized[1]:
            raise WindowIdentityError("window_identity_invalid")
        return cls(
            session_id=session_id, backend=backend, native_id=str(native),
            process_id=int(pid), process_name=str(snapshot.get("process_name") or ""),
            title=str(snapshot.get("title") or ""), rect=normalized,
            role=str(snapshot.get("role") or ""),
            root_control_id=str(snapshot.get("root_control_id") or ""),
            class_name=str(snapshot.get("class_name") or ""),
            parent_native_id=(str(snapshot["parent_native_id"])
                              if snapshot.get("parent_native_id") is not None else None),
            opened_by_action_id=snapshot.get("opened_by_action_id"),
            visible=bool(snapshot.get("visible", True)),
            foreground=bool(snapshot.get("foreground", False)),
            generation=generation,
        )

    def with_generation(self, generation: int) -> "WindowIdentity":
        return replace(self, generation=generation)

    @property
    def binding(self) -> tuple[str, str, int, str, int]:
        return (self.session_id, self.native_id, self.process_id,
                self.root_control_id, self.generation)
