"""Parent links require OS ownership or a recorded opening action."""

from __future__ import annotations

from .models import WindowIdentity


def link_parent(child: WindowIdentity, candidates: tuple[WindowIdentity, ...],
                cause: str | None = None) -> str | None:
    if child.parent_native_id:
        matches = [c for c in candidates if c.native_id == child.parent_native_id
                   and c.session_id == child.session_id]
        return matches[0].native_id if len(matches) == 1 else None
    if cause and child.opened_by_action_id == cause:
        matches = [c for c in candidates if c.process_id == child.process_id
                   and c.session_id == child.session_id and c.visible]
        return matches[0].native_id if len(matches) == 1 else None
    return None
