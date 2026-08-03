"""Snapshot builder helpers for P2.1 transition / checkpoint tests.

Avoids copy-pasting target-dict construction in every test. Real
production code uses ``obs.build_snapshot`` from
``target/observations/snapshot.py`` to materialise
``ObservationSnapshot`` instances — tests use the same factory,
just with synthetic target dicts.

Usage:

    from test_case.fixtures.transitions.builder import (
        make_window, make_modal, make_control, snapshot,
    )

    before = snapshot([
        make_window(pid=101, native_window_id="w-1", title="Login",
                    active=True),
    ])
    after = snapshot([
        make_window(pid=101, native_window_id="w-1", title="Dashboard",
                    active=True),
    ])
    assert classify_transition(before, after).kind is TransitionKind.PAGE_NAVIGATION
"""

from __future__ import annotations

from typing import Any

from target.observations import build_snapshot as _build_snapshot
from target.observations.assignment import assign_target_ids
from target.observations.models import ObservationSnapshot


_DEFAULT_PROCESS = "HiSecAgent"
_DEFAULT_BACKEND = "windows_pywinauto"
_DEFAULT_HOST = "fixture-host"
_DEFAULT_CAPTURED_AT = "2026-08-03T00:00:00+00:00"


def make_window(
    *,
    pid: int,
    native_window_id: str,
    title: str = "",
    process_name: str = _DEFAULT_PROCESS,
    control_type: str = "Window",
    active: bool = False,
    rect: tuple[int, int, int, int] | None = None,
) -> dict[str, Any]:
    """Return a window-kind target dict for ``build_snapshot``."""
    d: dict[str, Any] = {
        "kind": "window",
        "process_name": process_name,
        "pid": pid,
        "native_window_id": native_window_id,
        "title": title,
        "control_type": control_type,
    }
    if rect is not None:
        d["rect"] = list(rect)
    # `active` is a builder-local sentinel — not a Target field. We
    # strip it before calling build_snapshot so the factory does not
    # reject it.
    if active:
        d["active"] = True
    return d


def make_modal(
    *,
    pid: int,
    native_window_id: str,
    title: str = "",
    process_name: str = _DEFAULT_PROCESS,
    control_type: str = "Dialog",
    active: bool = False,
) -> dict[str, Any]:
    """Return a modal-kind window dict.

    `kind="window"` + `control_type` in the modal set
    (`Dialog`, `Pane`, `AXDialog`, `AXSheet`, `Sheet`,
    `AXAlert`) is the classifier's heuristic for modal surfaces.
    """
    return make_window(
        pid=pid,
        native_window_id=native_window_id,
        title=title,
        process_name=process_name,
        control_type=control_type,
        active=active,
    )


def make_control(
    *,
    pid: int,
    native_window_id: str,
    title: str = "",
    text: str = "",
    automation_id: str | None = None,
    process_name: str = _DEFAULT_PROCESS,
    control_type: str = "Button",
) -> dict[str, Any]:
    """Return a control-kind target dict (kind=control)."""
    d: dict[str, Any] = {
        "kind": "control",
        "process_name": process_name,
        "pid": pid,
        "native_window_id": native_window_id,
        "title": title,
        "control_type": control_type,
    }
    if text:
        d["text"] = text
    if automation_id is not None:
        d["automation_id"] = automation_id
    return d


def _resolve_active_id(targets: list[dict[str, Any]]) -> str | None:
    """Run deterministic target_id assignment and return the
    `target_id` of the first dict marked ``active=True``.

    ``active`` is a builder-local sentinel — it is not part of the
    `Target` schema — so we look it up by index. The assignment
    function reorders targets by stable key
    `(process_name, pid, native_window_id, native_index)`;
    `native_index` is the original position in the input list, which
    is what we use to find the dict the caller marked active.
    """
    # Build a parallel index map: which dicts in the input list were
    # marked active? Use enumerate to remember their position.
    active_native_indices = [
        i for i, d in enumerate(targets) if d.get("active")
    ]
    if not active_native_indices:
        return None
    # Strip the sentinel before passing to the factory so the dicts
    # remain valid Target inputs.
    stripped = [{k: v for k, v in d.items() if k != "active"}
                for d in targets]
    assigned = assign_target_ids(stripped)
    # Match by `native_index` field added by the assignment fn (if
    # present), else by input position — both stable for our inputs.
    for target_dict in assigned.targets:
        if target_dict.get("native_index") == active_native_indices[0]:
            return target_dict["target_id"]
    # Fallback: the assignment reorders; if we lost the link, return
    # the first assigned target_id (best-effort). This should not
    # happen for stable inputs.
    return assigned.targets[0]["target_id"]


def snapshot(
    targets: list[dict[str, Any]],
    *,
    backend: str = _DEFAULT_BACKEND,
    host: str = _DEFAULT_HOST,
    captured_at: str = _DEFAULT_CAPTURED_AT,
) -> ObservationSnapshot:
    """Build an ``ObservationSnapshot`` and resolve the active window.

    The first dict marked ``active=True`` (if any) becomes the
    snapshot's ``active_window``. ``active`` is stripped before the
    dicts reach the factory.
    """
    active_id = _resolve_active_id(targets)
    # Strip sentinel before delegating to the production factory.
    stripped = [{k: v for k, v in d.items() if k != "active"}
                for d in targets]
    return _build_snapshot(
        targets=stripped,
        backend=backend,
        host=host,
        captured_at=captured_at,
        active_window_target_id=active_id,
    )


__all__ = [
    "make_window",
    "make_modal",
    "make_control",
    "snapshot",
]