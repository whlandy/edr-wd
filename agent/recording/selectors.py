"""Stable replay-selector synthesis."""

from __future__ import annotations

from typing import Any, Mapping

from target.recording.models import ObservedTarget, RawCaptureEvent

from .models import ReplaySelector


def _window(event: RawCaptureEvent) -> dict[str, Any]:
    return {
        "processName": event.scope.process_name,
        "titleRegex": event.scope.window_title,
    }


def _control(identity: Mapping[str, Any]) -> dict[str, Any] | None:
    control = {
        "automationId": identity.get("automationId"),
        "identifier": identity.get("identifier"),
        "controlType": identity.get("controlType"),
        "name": identity.get("text"),
        "fingerprint": identity.get("fingerprint"),
    }
    control = {k: v for k, v in control.items() if v not in (None, [], "")}
    strong_identity = identity.get("automationId") or identity.get("identifier")
    if not strong_identity and not (
        identity.get("controlType") and identity.get("text")
    ):
        return None
    # Ancestry only earns its place when the control cannot identify itself.
    # It is captured by walking the UIA tree, which is not how the replay-time
    # observation reports it, so requiring it to match narrows the selector to
    # nothing while adding no discriminating power a unique id does not have.
    if not strong_identity:
        ancestry = [dict(x) for x in (identity.get("ancestry") or [])]
        if ancestry:
            control["ancestry"] = ancestry
    return control


def _identity(target: ObservedTarget) -> dict[str, Any]:
    return {
        "automationId": target.automation_id,
        "identifier": target.identifier,
        "controlType": target.control_type,
        "text": target.text,
        "ancestry": [dict(x) for x in target.ancestry],
        "fingerprint": target.fingerprint,
    }


def _anchor(point: Any, rect: Any) -> dict[str, Any] | None:
    """Project a recorded screen point onto a relative point inside its control.

    Replay recomputes the absolute point from the control's *current*
    rectangle, so a moved or resized window still drags the same grab handle.
    """
    if (
        not isinstance(point, (list, tuple)) or len(point) != 2
        or not isinstance(rect, (list, tuple)) or len(rect) != 4
    ):
        return None
    left, top, right, bottom = (int(value) for value in rect)
    width = right - left
    height = bottom - top
    if width <= 0 or height <= 0:
        return None
    relative_x = (int(point[0]) - left) / width
    relative_y = (int(point[1]) - top) / height
    if not (0 <= relative_x <= 1 and 0 <= relative_y <= 1):
        return None
    return {"relativePoint": [round(relative_x, 6), round(relative_y, 6)]}


#: Ancestors that can own a scroll. A wheel gesture acts on the scrollable
#: container, so these are searched outward from the hit-tested control.
_SCROLLABLE_CONTROL_TYPES = (
    "Table", "List", "Tree", "DataGrid", "Document", "Pane", "ScrollArea",
)


def _scroll_container(target: ObservedTarget) -> dict[str, Any] | None:
    """The nearest ancestor a scroll gesture actually acts on.

    Hit testing during a scroll returns whichever row sits under the pointer,
    whose only identity is its own text — text that is different on the next
    run, so the step would resolve to nothing. The container that scrolls has a
    stable automation id and is what the gesture was aimed at.
    """
    for ancestor in target.ancestry:
        control_type = str(ancestor.get("controlType") or "")
        automation_id = ancestor.get("automationId")
        if not automation_id:
            continue
        if control_type in _SCROLLABLE_CONTROL_TYPES:
            return {
                "automationId": automation_id,
                "controlType": control_type,
            }
    return None


def synthesize_selector(event: RawCaptureEvent) -> ReplaySelector | None:
    target = event.observed_target
    window = _window(event)
    if target is None and event.type == "assertion" and (event.assertion or {}).get("type") in {
        "window_open", "window_text_contains", "window_text_contains_time",
    }:
        # Window-scoped assertions carry no control, by design.
        return ReplaySelector(window=window, control={})
    if target is None:
        return None
    if event.type == "scroll_commit":
        container = _scroll_container(target)
        if container is not None:
            return ReplaySelector(window=window, control=container)
    control = _control(_identity(target))
    if control is None:
        return None
    if event.type == "drag_commit":
        anchor = _anchor(event.input.get("screenPoint"), target.rect)
        if anchor is None:
            return None
        control["anchor"] = anchor
    return ReplaySelector(window=window, control=control)


def synthesize_drag_end_selector(event: RawCaptureEvent) -> ReplaySelector | None:
    """Synthesize the drag release target from its own semantic identity.

    A drag whose release point cannot be tied to a stable control stays
    unresolved: the compiler then leaves the step incomplete rather than
    replaying a recorded screen coordinate.
    """
    identity = event.input.get("endTarget")
    if not isinstance(identity, Mapping):
        return None
    control = _control(identity)
    if control is None:
        return None
    anchor = _anchor(event.input.get("endPoint"), identity.get("rect"))
    if anchor is None:
        return None
    control["anchor"] = anchor
    return ReplaySelector(window=_window(event), control=control)
