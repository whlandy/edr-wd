"""
fake_backend.py — Stub backend and observation provider for P1.2 tests.

P1.2 acceptance requires exercising the executor end-to-end against
something that is *not* the real macOS/Windows GUI backend. This
fixture supplies:

  * `FakeBackend` — counts calls, returns canned receipts.
  * `FakeObservationProvider` — emits a single snapshot whose
    `windows`/`controls` lists match the expectations tests need to
    evaluate.

Both are deliberately minimal. P1.2 does not need concurrency, real
GUI hooks, or a real MCP transport. The only contract we care about
is `dispatch()` -> ActionReceipt and `refresh()` -> snapshot_id.

Layering:

    test_case/fake_target/      — test-only fixtures (P1.2)
    target/action_dispatcher    — ActionReceipt (P1.1)
    agent/execution             — AtomicExecutor (P1.2)
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Mapping

from action_dispatcher import ActionReceipt

from execution import BackendUnavailable, ObservationProvider


@dataclass
class FakeBackend:
    """Counts backend calls and yields canned receipts.

    The default factory returns `ok=True`. Tests can configure
    behaviour in three ways (most-precedence first):

      * `responses`: an ordered queue of pre-built ActionReceipt
        objects (P1.2 review #1 Blocker 4 — deterministic, no
        state-toggle churn).
      * `receipt_overrides`: per-action-id canned result payloads
        (legacy; kept for tests that do not care about exact
        ordering).
      * `force_ok`: a global boolean (default True).

    `responses` is consumed in FIFO order; if exhausted, the
    backend falls back to `receipt_overrides` then `force_ok`.
    """

    call_log: list[tuple[str, dict]] = field(default_factory=list)
    call_count: dict[str, int] = field(default_factory=dict)
    responses: list[ActionReceipt] = field(default_factory=list)
    receipt_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    force_ok: bool = True
    raise_on: set[str] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def dispatch(
        self,
        *,
        action_id: str,
        action_code: str | None = None,
        args: dict | None = None,
        target_ref: dict | None = None,
        request_id: str | None = None,
    ) -> ActionReceipt:
        with self.lock:
            self.call_log.append((action_id, dict(args or {})))
            self.call_count[action_id] = self.call_count.get(action_id, 0) + 1
            if action_id in self.raise_on:
                raise RuntimeError(f"backend refuses {action_id}")
            if self.responses:
                # FIFO pop; caller is responsible for queueing the
                # right number of responses.
                return self.responses.pop(0)
            override = self.receipt_overrides.get(action_id, {})
            ok = override.get("ok", self.force_ok)
            result = override.get("result", {"data": {"ok": ok}})
            return ActionReceipt(
                code="ok" if ok else "backend_error",
                ok=ok,
                action_id=action_id,
                action_code=action_code or "A000",
                request_id=request_id,
                result=result,
                server_instance_id="inst-fake",
            )

    def reset(self) -> None:
        with self.lock:
            self.call_log.clear()
            self.call_count.clear()
            self.responses.clear()
            self.receipt_overrides.clear()
            self.raise_on.clear()
            self.force_ok = True

    # --- Response queue helpers (P1.2 review #1 Blocker 4) ---

    def queue_receipts(self, *receipts: ActionReceipt) -> None:
        """Append pre-built receipts to the queue in order."""
        self.responses.extend(receipts)

    def queue_failure(self, action_id: str = "gui.click", *,
                     message: str = "boom") -> None:
        """Convenience: queue a single failure receipt for the next call."""
        self.queue_receipts(_failed_receipt(action_id, message))

    def queue_success(self, action_id: str = "gui.click", *,
                      data: dict | None = None) -> None:
        """Convenience: queue a single success receipt."""
        self.queue_receipts(_ok_receipt(action_id, data))


def _ok_receipt(action_id: str = "gui.click",
                data: dict | None = None) -> ActionReceipt:
    return ActionReceipt(
        code="ok",
        ok=True,
        action_id=action_id,
        action_code="A020",
        request_id="R-queued",
        server_instance_id="inst-fake",
        result={"data": data or {"ok": True}},
    )


def _failed_receipt(action_id: str = "gui.click",
                    message: str = "boom") -> ActionReceipt:
    return ActionReceipt(
        code="backend_error",
        ok=False,
        action_id=action_id,
        action_code="A020",
        request_id="R-queued",
        server_instance_id="inst-fake",
        result={"error": message},
    )


@dataclass
class FakeObservationProvider(ObservationProvider):
    """Single-snapshot provider. `refresh()` returns the same id."""

    snapshot_id: str = "snap-fake"
    snapshot: dict[str, Any] = field(default_factory=lambda: {
        "observation_id": "snap-fake",
        "windows": [],
        "controls": [],
    })
    refresh_count: int = 0
    raise_on_refresh: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)

    def latest_snapshot_id(self) -> str | None:
        return self.snapshot_id

    def get_snapshot(self, snapshot_id: str) -> Mapping[str, Any] | None:
        if snapshot_id != self.snapshot_id:
            return None
        return self.snapshot

    def refresh(self) -> str:
        with self.lock:
            self.refresh_count += 1
            if self.raise_on_refresh:
                raise BackendUnavailable("backend_unavailable")
            return self.snapshot_id

    def set_snapshot(self, payload: dict[str, Any]) -> None:
        self.snapshot = payload
        self.snapshot.setdefault("observation_id", self.snapshot_id)


__all__ = ["FakeBackend", "FakeObservationProvider", "_ok_receipt", "_failed_receipt"]