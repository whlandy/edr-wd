"""
T2 (review): MCP-entry contract test for `page_table`.

Proves the real user entry path is wired for the pagination-only composite:
`server.py -> @mcp.tool page_table() -> run_page_table -> detect -> plan ->
dispatcher -> verify`. It specifically locks in the T2 invariants that
distinguish it from `scroll_region`:

  * a PAGINATED surface dispatches exactly the semantic click chain and
    reports movement only after a genuine content change (`next_page`,
    `moved=True`);
  * a non-paged structure (FLAT) returns honest `not_dispatched` — page_table
    must NEVER wheel-scroll, so it must NOT dispatch anything and must NOT
    fabricate movement;
  * backend unavailable / server._backend None -> honest JSON, no crash.

All global mutation (server._backend, scroll.actions._default_dispatcher) is
done through pytest `monkeypatch` so state is restored after each test.
"""

from __future__ import annotations

import json

# Stub the backend factory BEFORE importing server so server._backend is ours.
import target.automation as _automod


class FakeBackend:
    """Backend whose tree flips the next-button text on the 2nd dump (simulates
    a real page change), so the T2 chain can genuinely observe movement."""

    def __init__(self, controls=None, title="Main Window", ok=True, error=""):
        self._n = 0
        self._controls = controls
        self._title = title
        self._ok = ok
        self._error = error

    def dump_tree(self, window_title_re=None, max_depth=6):
        self._n += 1
        if not self._ok:
            return {"ok": False, "error": self._error}
        if self._controls is None:
            text = "下一页" if self._n == 1 else "第二页"
            controls = [
                {
                    "class_name": "Button",
                    "automation_id": "nextPageButton",
                    "text": text,
                    "is_visible": True,
                    "is_enabled": True,
                }
            ]
        else:
            controls = self._controls
        return {
            "ok": True,
            "title": self._title,
            "window_rectangle": {"x": 0, "y": 0, "w": 800, "h": 600},
            "controls": controls,
        }


NEXT_CONTROLS = [
    {
        "class_name": "Button",
        "automation_id": "nextPageButton",
        "text": "下一页",
        "is_visible": True,
        "is_enabled": True,
    }
]

FLAT_CONTROLS = [
    {
        "class_name": "Edit",
        "automation_id": "searchBox",
        "text": "type here",
        "is_visible": True,
        "is_enabled": True,
    }
]

_automod.create_backend = lambda: FakeBackend()

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
    """Install a fake backend + a controllable default dispatcher for the call,
    restoring both afterwards so the real default dispatcher (which would hit a
    live backend) is never invoked during tests."""
    monkeypatch.setattr(srv, "_backend", backend)
    monkeypatch.setattr(_acts, "_default_dispatcher", _fake_dispatch_ok)


def test_page_table_paginated_happy_path(monkeypatch):
    """A PAGINATED surface routes through the T2 chain: dispatches the semantic
    click and reports movement only after a real content change."""
    _install(monkeypatch, FakeBackend())
    payload = json.loads(srv.page_table(direction="next"))
    assert "dispatched" in payload and "moved" in payload and "reason" in payload
    assert payload["ok"] is True
    assert payload["dispatched"] is True
    assert payload["moved"] is True
    assert payload["reason"] == "next_page"


def test_page_table_flat_never_wheels_or_fabricates(monkeypatch):
    """A non-paged (FLAT) surface must NOT wheel-scroll and must NOT fabricate
    movement — T2 invariant distinguishing it from scroll_region."""
    _install(monkeypatch, FakeBackend(controls=FLAT_CONTROLS))
    payload = json.loads(srv.page_table(direction="next"))
    assert payload["ok"] is True
    # page_table never dispatches for a non-paged surface.
    assert payload["dispatched"] is False
    assert payload["reason"] == "not_dispatched"
    assert payload["moved"] is False
    assert payload["success"] is False


def test_page_table_disabled_next_not_dispatched(monkeypatch):
    """Structurally disabled next (carries a disabled marker) -> NOT_DISPATCHED,
    nothing armed, never moved. `_is_enabled` model derives disabledness from
    text/automation-id markers (project convention), so the marker is the
    controllable signal here — `is_enabled` UI field is not consumed by
    detect.py (see code-review note in the round log)."""
    disabled = [
        {
            "class_name": "Button",
            "automation_id": "nextPageButton",
            "text": "下一页(disabled)",
            "is_visible": True,
            "is_enabled": False,
        }
    ]
    _install(monkeypatch, FakeBackend(controls=disabled))
    payload = json.loads(srv.page_table(direction="next"))
    assert payload["ok"] is True
    assert payload["dispatched"] is False
    assert payload["reason"] == "not_dispatched"
    assert payload["moved"] is False


def test_page_table_backend_unavailable_honest(monkeypatch):
    """Backend unavailable -> honest NOT_DISPATCHED with backend_error."""
    _install(monkeypatch, FakeBackend(ok=False, error="no window connected"))
    try:
        payload = json.loads(srv.page_table(direction="next"))
    except Exception as exc:  # pragma: no cover - fail loudly if regression
        assert False, f"page_table should not raise: {exc!r}"
        return
    assert payload["ok"] is True
    assert payload["dispatched"] is False
    assert payload["reason"] == "not_dispatched"
    assert "backend_error" in payload


def test_page_table_backend_none_returns_unavailable_json(monkeypatch):
    """server._backend is None -> the tool returns the _backend_unavailable
    JSON without touching the chain."""
    monkeypatch.setattr(srv, "_backend", None)
    payload = json.loads(srv.page_table(direction="next"))
    assert payload["ok"] is False
    assert payload.get("tool") == "page_table"
    assert "not loaded" in payload.get("error", "").lower()
