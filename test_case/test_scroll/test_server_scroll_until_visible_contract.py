"""
T3: MCP-entry contract test for `scroll_until_visible`.

Proves the real user entry path is wired: `server.py -> @mcp.tool
scroll_until_visible() -> run_scroll_until_visible -> snapshot -> detect ->
PageController plan -> dispatcher -> probe`.

Locks in the T3 invariants that distinguish it from `scroll_region` /
`page_table`:

  * probe BEFORE the first dispatch — an already-visible target performs ZERO
    input and reports `reason=target_visible`, `dispatched=False`;
  * a list that grows each dump (simulated real scrolling) yields a
    `target_visible` result with `moved=True` after exactly N advances;
  * a target that never appears terminates honestly with
    `reason=target_not_found` within `max_steps` — never an infinite spin;
  * backend unavailable / server._backend None -> honest JSON, no crash.

All global mutation (server._backend, scroll.actions._default_dispatcher) is
done through pytest `monkeypatch` so state is restored after each test.
"""

from __future__ import annotations

import json

# Stub the backend factory BEFORE importing server so server._backend is ours.
import target.automation as _automod


class GrowingListBackend:
    """Backend that simulates a scrolling list: each dump returns a *shifted*
    set of rows (row text depends on the current dump count, so the tree digest
    changes every step — genuine movement), and an optional target row whose
    text only appears once the dump count crosses its `appear_on` threshold."""

    def __init__(self, rows=(), title="Main Window", ok=True, error="",
                 base_rows=3):
        self._n = 0
        self.rows = rows            # (target_text, appear_on_dump)
        self._title = title
        self._ok = ok
        self._error = error
        self._base_rows = base_rows

    def dump_tree(self, window_title_re=None, max_depth=6):
        self._n += 1
        if not self._ok:
            return {"ok": False, "error": self._error}
        offset = self._n  # simulated scroll offset -> rows shift every dump
        controls = []
        for i in range(self._base_rows):
            controls.append({
                "class_name": "ListItem",
                "automation_id": f"row{offset + i}",
                "text": f"row{offset + i}",
                "is_visible": True,
                "is_enabled": True,
            })
        for text, appear_on in self.rows:
            if self._n >= appear_on:
                controls.append({
                    "class_name": "ListItem",
                    "automation_id": f"target_{self._n}",
                    "text": text,
                    "is_visible": True,
                    "is_enabled": True,
                })
        return {
            "ok": True,
            "title": self._title,
            "window_rectangle": {"x": 0, "y": 0, "w": 800, "h": 600},
            "controls": controls,
        }


_automod.create_backend = lambda: GrowingListBackend()

import target.server as srv  # noqa: E402

import scroll.actions as _acts  # noqa: E402
from action_dispatcher.receipts import ActionReceipt  # noqa: E402


def _fake_dispatch_ok(action_id, **kwargs):
    return ActionReceipt.from_ok(
        action_id=action_id,
        action_code=kwargs.get("action_code"),
        request_id=None,
        result=kwargs.get("args"),
    )


def _install(monkeypatch, backend):
    """Install the fake backend + a controllable default dispatcher, restoring
    both afterwards so the real dispatcher (which would hit a live window) is
    never invoked during tests."""
    monkeypatch.setattr(srv, "_backend", backend)
    monkeypatch.setattr(_acts, "_default_dispatcher", _fake_dispatch_ok)


def test_backend_unavailable_returns_honest_json(monkeypatch):
    monkeypatch.setattr(srv, "_backend", None)
    payload = json.loads(srv.scroll_until_visible(target_text_re="目标"))
    # Matches the honest-error contract of _backend_unavailable: ok=False and
    # no fabricated dispatched/moved fields.
    assert payload["ok"] is False
    assert payload["tool"] == "scroll_until_visible"
    assert "error" in payload
    assert "dispatched" not in payload or payload["dispatched"] is not True


def test_target_already_visible_performs_zero_dispatch(monkeypatch):
    """Probe-before-dispatch: an already-visible target drives zero input."""
    _install(monkeypatch, GrowingListBackend(rows=(("防护中心", 1),)))
    counter = _count_dispatches(monkeypatch)
    payload = json.loads(srv.scroll_until_visible(target_text_re="防护"))
    assert payload["ok"] is True
    assert payload["reason"] == "target_visible"
    assert payload["dispatched"] is False
    assert payload["moved"] is False
    assert counter.calls == 0


def test_target_appears_after_two_advances(monkeypatch):
    """down on an INFINITE list: target on dump 3 -> 2 advances then found, with
    moved=True and a negative wheel (A029) dispatched each step."""
    _install(monkeypatch, GrowingListBackend(rows=(("目标行", 3),)))
    counter = _count_dispatches(monkeypatch)
    payload = json.loads(srv.scroll_until_visible(
        target_text_re="目标行", direction="down", max_steps=8))
    assert payload["ok"] is True
    assert payload["reason"] == "target_visible"
    assert payload["dispatched"] is True
    assert payload["moved"] is True
    assert counter.calls == 2  # exactly 2 advances, never more


def test_never_found_terminates_within_max_steps(monkeypatch):
    """A target that never appears stops honestly at target_not_found instead
    of spinning forever."""
    _install(monkeypatch, GrowingListBackend(rows=(("NOPE", 999),)))
    counter = _count_dispatches(monkeypatch)
    payload = json.loads(srv.scroll_until_visible(
        target_text_re="NOPE", direction="down", max_steps=3))
    assert payload["ok"] is True
    assert payload["reason"] == "target_not_found"
    assert payload["moved"] is False
    # bounded: never exceeds max_steps dispatches
    assert counter.calls <= 3


def test_verify_false_preserves_honest_unverified_movement(monkeypatch):
    """verify=False keeps moved=False even though dispatch happened (movement
    was not verified)."""
    _install(monkeypatch, GrowingListBackend(rows=(("目标行", 2),)))
    counter = _count_dispatches(monkeypatch)
    payload = json.loads(srv.scroll_until_visible(
        target_text_re="目标行", direction="down", verify=False,
        max_steps=8))
    assert payload["ok"] is True
    assert payload["reason"] == "target_visible"
    assert payload["dispatched"] is True
    assert payload["moved"] is False  # never moved=True under verify=False
    assert counter.calls >= 1


class _Counter:
    def __init__(self):
        self.calls = 0


def _count_dispatches(monkeypatch):
    """Wrap the default dispatcher with a call counter (returns the counter so
    tests can assert how many dispatch rounds one server call generated)."""

    counter = _Counter()
    orig = getattr(_acts, "_default_dispatcher", None)

    def wrapped(action_id, **kwargs):
        counter.calls += 1
        return _fake_dispatch_ok(action_id, **kwargs)

    monkeypatch.setattr(_acts, "_default_dispatcher", wrapped)
    return counter
