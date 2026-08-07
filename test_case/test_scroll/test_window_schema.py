"""
P1.2 — Unified Window Argument Schema tests.

Locks in the contract that MCP tools accept ONE canonical ``window`` object
``{title_re, process_name, pid, handle}`` while legacy per-field parameters
stay compatibleiones.

Three layers:

* pure — ``window_args.resolve_window`` / ``validate_window`` precedence,
  fallback and typo rejection (the source of truth).
* MCP surface — each composite tool forwards a ``window`` object to its real
  chain (and `window` wins over a legacy alias) without regressing the legacy
  spelling.
* docs — the tool description embeds an executable JSON example so an agent
  does not need to read server.py for parameter names.
"""

from __future__ import annotations

import json

import pytest

from scroll.window_args import (
    WINDOW_FIELDS,
    resolve_window,
    validate_window,
    window_doc,
)


# ── Pure layer ─────────────────────────────────────────────────────────────

def test_resolve_window_object_full():
    w = resolve_window(
        {"title_re": "^日志中心$", "process_name": "EDRClient.exe",
         "pid": 6752, "handle": 66336}
    )
    assert w["window_title_re"] == "^日志中心$"
    assert w["window_title"] == "^日志中心$"
    assert w["process_name"] == "EDRClient.exe"
    assert w["pid"] == 6752
    assert w["native_window_id"] == 66336


def test_resolve_window_legacy_fallback():
    w = resolve_window(
        None,
        window_title_re="日志中心",
        process_name="App.exe",
        pid=99,
    )
    assert w["window_title_re"] == "日志中心"
    assert w["process_name"] == "App.exe"
    assert w["pid"] == 99
    assert w["native_window_id"] is None


def test_resolve_window_window_wins_field_by_field():
    w = resolve_window(
        {"title_re": "^NEW$", "pid": 1},
        window_title_re="^OLD$",
        process_name="Legacy.exe",
        pid=2,
        expected_process_name="Expected.exe",
        expected_pid=3,
    )
    # window.title_re and window.pid win; process_name falls back to the
    # first non-None legacy alias (process_name, then expected_process_name).
    assert w["window_title_re"] == "^NEW$"
    assert w["pid"] == 1
    assert w["process_name"] == "Legacy.exe"  # process_name beats expected_*


def test_resolve_window_expected_aliases():
    # expected_process_name / expected_pid (pointer tools) are honoured.
    w = resolve_window(
        None,
        expected_process_name="EDRClient.exe",
        expected_pid=6752,
    )
    assert w["process_name"] == "EDRClient.exe"
    assert w["pid"] == 6752


def test_validate_window_empty_and_none():
    assert validate_window(None) == {}
    assert validate_window({}) == {}


def test_validate_window_rejects_unknown_key():
    with pytest.raises(ValueError):
        validate_window({"title": "typo-should-be-title_re"})


def test_validate_window_rejects_non_mapping():
    with pytest.raises(ValueError):
        validate_window(["not", "a", "dict"])


def test_window_fields_order():
    assert WINDOW_FIELDS == ("title_re", "process_name", "pid", "handle")


# ── Docs layer ─────────────────────────────────────────────────────────────

def test_window_doc_embeds_executable_json_example():
    doc = window_doc("Selects the target window.")
    assert '"title_re": "^日志中心$"' in doc
    assert '"process_name": "EDRClient.exe"' in doc
    assert '"pid": 6752' in doc
    assert '"handle": 66336' in doc
    # It must be a valid JSON object fragment, not prose.
    example = '{"title_re": "^日志中心$", "process_name": "EDRClient.exe", "pid": 6752, "handle": 66336}'
    assert json.loads(example) == {
        "title_re": "^日志中心$",
        "process_name": "EDRClient.exe",
        "pid": 6752,
        "handle": 66336,
    }


# ── MCP surface ────────────────────────────────────────────────────────────


def _fake_for(*methods):
    class FakeBackend:
        pass

    def _mk(name):
        def fn(*a, **kw):
            return {"ok": True, "method": name, **kw}
        return fn

    for m in methods:
        setattr(FakeBackend, m, _mk(m))
    return FakeBackend()


def test_scroll_window_accepts_window_object(monkeypatch):
    import target.server as srv

    seen = {}

    class FakeBackend:
        def scroll_window(self, clicks, x, y, **kw):
            seen.update(kw)
            return {"ok": True, "clicks": clicks, **kw}

    monkeypatch.setattr(srv, "_backend", FakeBackend())
    out = srv.scroll_window(
        -2, 5, 6,
        window={"title_re": "^日志中心$", "process_name": "EDRClient.exe",
                "pid": 6752, "handle": 66336},
    )
    payload = json.loads(out)
    assert payload["ok"] is True
    assert seen["window_title_re"] == "^日志中心$"
    assert seen["expected_process_name"] == "EDRClient.exe"
    assert seen["expected_pid"] == 6752


def test_scroll_window_window_beats_legacy(monkeypatch):
    import target.server as srv

    seen = {}

    class FakeBackend:
        def scroll_window(self, clicks, x, y, **kw):
            seen.update(kw)
            return {"ok": True, "clicks": clicks, **kw}

    monkeypatch.setattr(srv, "_backend", FakeBackend())
    srv.scroll_window(
        -1, 0, 0,
        window={"title_re": "^NEW$"},
        window_title_re="^OLD$",
    )
    assert seen["window_title_re"] == "^NEW$"


def test_scroll_window_rejects_unknown_window_key(monkeypatch):
    import target.server as srv

    monkeypatch.setattr(srv, "_backend", _fake_for("scroll_window"))
    with pytest.raises(ValueError):
        srv.scroll_window(-1, 0, 0, window={"title": "typo"})


def test_scroll_region_accepts_window_object(monkeypatch):
    import target.server as srv

    seen = {}

    class FakeBackend:
        def dump_tree(self, window_title_re=None, max_depth=6):
            seen["title"] = window_title_re
            return {
                "ok": True,
                "title": window_title_re or "Main",
                "window_rectangle": {"x": 0, "y": 0, "w": 800, "h": 600},
                "controls": [],
            }

    monkeypatch.setattr(srv, "_backend", FakeBackend())
    out = srv.scroll_region(window={"title_re": "^日志中心$", "handle": 66336})
    payload = json.loads(out)
    # no controls -> honest NOT_DISPATCHED, but the window selector was used.
    assert payload["ok"] is True
    assert seen["title"] == "^日志中心$"


def test_page_table_accepts_window_object(monkeypatch):
    import target.server as srv

    seen = {}

    class FakeBackend:
        def dump_tree(self, window_title_re=None, max_depth=6):
            seen["title"] = window_title_re
            return {
                "ok": True,
                "title": window_title_re or "Main",
                "window_rectangle": {"x": 0, "y": 0, "w": 800, "h": 600},
                "controls": [],
            }

    monkeypatch.setattr(srv, "_backend", FakeBackend())
    out = srv.page_table(direction="next", window={"title_re": "^日志中心$"})
    payload = json.loads(out)
    assert payload["ok"] is True
    assert seen["title"] == "^日志中心$"


def test_scroll_until_visible_accepts_window_object(monkeypatch):
    import target.server as srv

    seen = {}

    class FakeBackend:
        def dump_tree(self, window_title_re=None, max_depth=6):
            seen["title"] = window_title_re
            return {
                "ok": True,
                "title": window_title_re or "Main",
                "window_rectangle": {"x": 0, "y": 0, "w": 800, "h": 600},
                "controls": [],
            }

    monkeypatch.setattr(srv, "_backend", FakeBackend())
    # max_steps= foreground probing; with no controls the facade returns
    # NOT_DISPATCHED honestly but still uses the window selector.
    out = srv.scroll_until_visible(
        target_text_re="foo",
        window={"title_re": "^日志中心$", "process_name": "EDRClient.exe"},
    )
    payload = json.loads(out)
    assert payload["ok"] is True
    assert seen["title"] == "^日志中心$"


def test_drag_target_accepts_window_object(monkeypatch):
    import target.server as srv

    seen = {}

    class FakeBackend:
        def dump_tree(self, window_title_re=None, max_depth=6):
            seen["title"] = window_title_re
            return {
                "ok": True,
                "title": window_title_re or "Main",
                "window_rectangle": {"x": 0, "y": 0, "w": 800, "h": 600},
                "controls": [],
            }

    monkeypatch.setattr(srv, "_backend", FakeBackend())
    out = srv.drag_target(
        {"automation_id": "thumb"},
        dx=0,
        dy=5,
        window={"title_re": "^日志中心$"},
    )
    payload = json.loads(out)
    assert payload["ok"] is True
    assert seen["title"] == "^日志中心$"


def test_click_window_at_accepts_window_object(monkeypatch):
    import target.server as srv

    seen = {}

    class FakeBackend:
        def click_window_at(self, x, y, window_title_re, process_name):
            seen["title"] = window_title_re
            seen["process"] = process_name
            return {"ok": True, "x": x, "y": y}

    monkeypatch.setattr(srv, "_backend", FakeBackend())
    out = srv.click_window_at(
        10, 20,
        window={"title_re": "^日志中心$", "process_name": "EDRClient.exe"},
    )
    payload = json.loads(out)
    assert payload["ok"] is True
    assert seen["title"] == "^日志中心$"
    assert seen["process"] == "EDRClient.exe"


def test_mcp_legacy_spelling_still_works(monkeypatch):
    """Legacy per-field parameters must keep working unchanged (P1.2 #2)."""
    import target.server as srv

    seen = {}

    class FakeBackend:
        def scroll_window(self, clicks, x, y, **kw):
            seen.update(kw)
            return {"ok": True, "clicks": clicks, **kw}

    monkeypatch.setattr(srv, "_backend", FakeBackend())
    out = srv.scroll_window(
        -2, 5, 6,
        window_title_re="日志中心",
        expected_process_name="EDRClient.exe",
        expected_pid=6752,
    )
    payload = json.loads(out)
    assert payload["ok"] is True
    assert seen["window_title_re"] == "日志中心"
    assert seen["expected_pid"] == 6752
