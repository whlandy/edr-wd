"""Component picking for the assertion wizard (U2).

Picking must never actuate the business UI: the operator points at a control,
the wizard reads it, and a confirm key (or the component-list fallback)
freezes the draft.  This controller is the offline-testable state machine on
top of the source's management-path hit test; the platform-specific pointer
hit test itself stays in the backends (windows/macos ``resolve_pick_target``).

Rate/timeout constraints (design §3.2): pointer updates are sampled at most
10 Hz and one hit test may take at most 1 s — enforced here so a slow backend
cannot stall the recording loop.

Overlay drawing and native-key interception are live-desktop concerns; on a
platform where the confirm key cannot be reliably swallowed the wizard falls
back to the component-list entry (the recorder never synthesizes a business
click to pick a control).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from .assertion_draft import build_assertion_draft
from .models import RecordingModelError

PICK_SAMPLE_INTERVAL = 0.1     # 10 Hz ceiling between pointer hit tests
PICK_READ_TIMEOUT = 1.0        # one hit test may take at most 1 s


@dataclass
class _PickState:
    previous_state: str
    started_at: float
    observed: Mapping[str, Any] | None = None
    read_status: str = "unknown"
    read_error: str | None = None
    last_sample: float = 0.0


class PickerController:
    """begin → update_pointer* → confirm/cancel, with the recording session's
    previous RECORDING/PAUSED state restored on exit."""

    def __init__(
        self,
        source,
        previous_state_provider: Callable[[], str],
        restore_state: Callable[[str], None],
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._source = source
        self._previous_state_provider = previous_state_provider
        self._restore_state = restore_state
        self._monotonic = monotonic
        self._state: _PickState | None = None

    @property
    def picking(self) -> bool:
        return self._state is not None

    def begin(self) -> dict[str, Any]:
        if self._state is not None:
            return {"ok": True, "picking": True, "duplicate": True}
        previous = self._previous_state_provider()
        if previous not in {"recording", "paused"}:
            raise RecordingModelError(
                "recording_state_invalid",
                "component picking requires an active recording session",
                path="session.state",
            )
        begin = getattr(self._source, "begin_pick", None)
        if callable(begin):
            begin()
        self._state = _PickState(previous_state=previous, started_at=self._monotonic())
        return {"ok": True, "picking": True, "previousState": previous}

    def update_pointer(self, point: tuple[int, int] | Mapping[str, Any]) -> dict[str, Any]:
        """Sample the component under the pointer, rate-limited to 10 Hz."""
        state = self._require_picking()
        now = self._monotonic()
        if now - state.last_sample < PICK_SAMPLE_INTERVAL:
            return {"ok": True, "sampled": False, "reason": "rate_limited"}
        state.last_sample = now
        inspect = getattr(self._source, "inspect_at", None)
        try:
            observed = (inspect(point) if callable(inspect)
                        else self._source.current_observed_target())
        except Exception as exc:  # noqa: BLE001 - a hit-test error is unknown
            state.observed, state.read_status, state.read_error = None, "error", str(exc)
            return {"ok": True, "sampled": True, "readStatus": "error",
                    "readError": str(exc)}
        if observed is None:
            state.observed, state.read_status = None, "unknown"
            return {"ok": True, "sampled": True, "readStatus": "unknown"}
        state.observed = _observed_dict(observed)
        state.read_status = "ok"
        return {"ok": True, "sampled": True, "readStatus": "ok",
                "observed": state.observed}

    def confirm(self) -> dict[str, Any]:
        """Freeze the last good observation into an assertion draft and exit
        picking.  With no readable observation there is no draft: the wizard
        says so instead of inventing a blank target."""
        state = self._require_picking()
        try:
            if state.observed is None or state.read_status != "ok":
                raise RecordingModelError(
                    "recording_pick_target_unresolved",
                    "没有读到可用组件：请把指针指向组件后重试，或改用组件列表选择",
                    path="picker.observed",
                )
            draft = build_assertion_draft(
                state.observed, "text_equals", str(
                    state.observed.get("value") if state.observed.get("value") is not None
                    else state.observed.get("name") or ""),
                read_status="ok",
                observed_at=None,
            )
            return self._exit({"ok": True, "draft": draft.to_dict()})
        finally:
            pass

    def confirm_with_type(self, assertion_type: str, typed_expected: Any) -> dict[str, Any]:
        """Freeze an observation into a typed draft chosen in the wizard."""
        state = self._require_picking()
        if state.observed is None or state.read_status != "ok":
            raise RecordingModelError(
                "recording_pick_target_unresolved",
                "没有读到可用组件：无法按所选类型生成草稿",
                path="picker.observed",
            )
        draft = build_assertion_draft(
            state.observed, assertion_type, typed_expected, read_status="ok",
        )
        return self._exit({"ok": True, "draft": draft.to_dict()})

    def cancel(self) -> dict[str, Any]:
        self._require_picking()
        return self._exit({"ok": True, "cancelled": True})

    def _exit(self, payload: dict[str, Any]) -> dict[str, Any]:
        state = self._state
        self._state = None
        end = getattr(self._source, "end_pick", None)
        if callable(end):
            end()
        self._restore_state(state.previous_state)
        return {**payload, "previousState": state.previous_state}

    def _require_picking(self) -> _PickState:
        if self._state is None:
            raise RecordingModelError(
                "recording_pick_inactive", "component picking is not active",
                path="picker",
            )
        return self._state


def _observed_dict(observed: Any) -> dict[str, Any]:
    if isinstance(observed, Mapping):
        return dict(observed)
    return {
        "snapshot_id": getattr(observed, "snapshot_id", None),
        "automation_id": getattr(observed, "automation_id", None),
        "control_type": getattr(observed, "control_type", None),
        "text": getattr(observed, "text", None),
        "name": getattr(observed, "text", None),
        "value": getattr(observed, "value", None),
        "protected": getattr(observed, "protected", None),
    }


__all__ = ["PickerController", "PICK_SAMPLE_INTERVAL", "PICK_READ_TIMEOUT"]
