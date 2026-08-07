"""
P1-2 (review): MCP-entry contract test for `scroll_region`.

Proves the *real user entry path* is wired to the NEW architecture — that is,
`server.py -> @mcp.tool scroll_region() -> run_scroll_region -> detect ->
plan -> dispatcher -> verify` — and NOT accidentally reverted to the raw
`_backend.scroll()` bypass. It also locks in two production-robustness fixes
this test surfaced:

  * a `scroll_region()` call with no explicit identity must NOT crash with a
    ProtocolModelError from an all-empty prepended window target (f8);
  * expected domain outcomes (no enabled owner / backend unavailable) must be
    honest NOT_DISPATCHED JSON, never a fabricated success.

All global mutation (server._backend, scroll.actions._default_dispatcher) is
done through pytest `monkeypatch` so state is restored after each test.
"""

from __future__ import annotations

import json

# Stub the backend factory BEFORE importing server so server._backend is ours.
import target.automation as _automod


class FakeBackend:
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

_automod.create_backend = lambda: FakeBackend()

import target.server as srv  # noqa: E402  (after backend stub)

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
    restoring both afterwards. Ensures the real default dispatcher (which would
    hit a live backend) is never invoked during tests."""
    monkeypatch.setattr(srv, "_backend", backend)
    monkeypatch.setattr(_acts, "_default_dispatcher", _fake_dispatch_ok)


def test_scroll_region_routes_through_new_chain_happy_path(monkeypatch):
    """The MCP entry dispatches AND detects genuine movement through the new
    chain (ScrollResult shape, not the raw _backend.scroll() bypass)."""
    _install(monkeypatch, FakeBackend())
    payload = json.loads(srv.scroll_region())
    # ScrollResult-shaped contract — the old _backend.scroll() path never
    # produced these keys.
    assert "dispatched" in payload and "moved" in payload and "reason" in payload
    assert payload["ok"] is True
    assert payload["dispatched"] is True
    assert payload["moved"] is True
    assert payload["reason"] == "next_page"


def test_scroll_region_honest_no_fabricated_movement_on_flat(monkeypatch):
    """A FLAT scroll region (no next/load-more owner) legitimately dispatches a
    wheel-scroll, but when the content never changes it must report moved=False
    — never a fabricated success."""
    no_next = [
        {
            "class_name": "Edit",
            "automation_id": "searchBox",
            "text": "type here",
            "is_visible": True,
            "is_enabled": True,
        }
    ]
    _install(monkeypatch, FakeBackend(controls=no_next))
    payload = json.loads(srv.scroll_region())
    assert payload["ok"] is True
    # Honesty invariant: no content change => NOT moved, regardless of whether
    # a wheel-scroll was dispatched.
    assert payload["moved"] is False
    assert payload["success"] is False


def test_scroll_region_honest_backend_unavailable(monkeypatch):
    """Backend unavailable -> honest NOT_DISPATCHED with backend_error, not a
    crash (ProtocolModelError) and not a fabricated result."""
    _install(monkeypatch, FakeBackend(ok=False, error="no window connected"))
    try:
        payload = json.loads(srv.scroll_region())
    except Exception as exc:  # pragma: no cover - fail loudly if regression
        assert False, f"scroll_region should not raise: {exc!r}"
        return
    assert payload["ok"] is True
    assert payload["dispatched"] is False
    assert payload["reason"] == "not_dispatched"
    assert "backend_error" in payload


def test_scroll_region_backend_none_returns_unavailable_json(monkeypatch):
    """server._backend is None -> the tool returns the _backend_unavailable
    JSON without touching the chain."""
    monkeypatch.setattr(srv, "_backend", None)
    payload = json.loads(srv.scroll_region())
    assert payload["ok"] is False
    assert payload.get("tool") == "scroll_region"
    assert "not loaded" in payload.get("error", "").lower()
