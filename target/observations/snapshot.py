"""
snapshot.py — Build an ObservationSnapshot from pre-collected data
(architecture §8.1).

P0.3 does NOT call any backend. `build_snapshot` consumes a list of
target dicts (already in the backend's stable traversal order)
and returns a fully-populated ObservationSnapshot. The actual
backend integration lives in `target/server.py` (out of scope for
this module; P0.3 leaves the wiring of dump_tree/list_windows to a
later commit if needed, per FR-P0.3-08).

Usage (synthetic, e.g. in tests):

    snapshot = build_snapshot(
        targets=[
            {"process_name": "EDRClient.exe", "pid": 1234,
             "native_window_id": "win-1", "title": "Main",
             "control_type": "Window", "kind": "window"},
            ...
        ],
        backend="windows_pywinauto",
        host="win26",
        captured_at="2026-08-01T10:00:00Z",
    )
"""

from __future__ import annotations

from .assignment import assign_target_ids
from .ids import new_snapshot_id
from .models import OBSERVATION_SCHEMA_VERSION, ObservationSnapshot, Target
from protocol_models.models import ProtocolModelError


def build_snapshot(
    *,
    targets: list[dict],
    backend: str,
    host: str,
    captured_at: str,
    active_window_target_id: str | None = None,
    screenshot_evidence_id: str | None = None,
    snapshot_id: str | None = None,
) -> ObservationSnapshot:
    """Construct an ObservationSnapshot from pre-collected data.

    Steps:
      1. Run `assign_target_ids` to compute target_id / fingerprint /
         tree_digest.
      2. Materialise each dict into a `Target` (validates fields).
      3. Identify `active_window` by target_id (if given).
      4. Build the ObservationSnapshot envelope.

    Args:
        targets: list of target dicts (one per observed window/
            control). Each dict carries at minimum `process_name`,
            `pid`, `native_window_id`. Other fields are optional.
        backend: backend name (e.g. `windows_pywinauto`).
        host: hostname (or any stable host identifier).
        captured_at: ISO-8601 timestamp string.
        active_window_target_id: target_id of the active window (T####
            format). None means no active window reported.
        screenshot_evidence_id: optional EVID-... reference; P0.3
            leaves it None.
        snapshot_id: optional explicit snapshot_id; defaults to a
            new P0.2 sortable id.

    Raises:
        ProtocolModelError: if any target dict fails strict-mode
            construction (e.g. invalid `kind`).
    """
    if not targets:
        raise ProtocolModelError(
            "type_error",
            "build_snapshot requires at least one target",
            path="targets",
            value=len(targets),
        )
    assignment = assign_target_ids(targets)
    target_models = tuple(Target.from_dict(t) for t in assignment.targets)

    active_window: Target | None = None
    if active_window_target_id is not None:
        for t in target_models:
            if t.target_id == active_window_target_id:
                active_window = t
                break
        else:
            raise ProtocolModelError(
                "type_error",
                f"active_window_target_id {active_window_target_id!r} "
                f"is not present in the snapshot",
                path="active_window_target_id",
                value=active_window_target_id,
            )

    return ObservationSnapshot(
        schema_version=OBSERVATION_SCHEMA_VERSION,
        snapshot_id=snapshot_id or new_snapshot_id(),
        captured_at=captured_at,
        backend=backend,
        host=host,
        active_window=active_window,
        targets=target_models,
        tree_digest=assignment.tree_digest,
        screenshot_evidence_id=screenshot_evidence_id,
    )


__all__ = ["build_snapshot"]