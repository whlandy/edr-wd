"""
Backend-adapter test (f3 / P0-3 review: "real MCP entry + main chain switch").

Proves the production bridge between the pure scroll package and a real
AutomationBackend's `dump_tree`:

  * `_shape_control` / `_shape_rect` map backend control dicts into
    `build_snapshot`-compatible target dicts (kind=control), so
    `Target.from_dict` accepts them (contract test at the model boundary).
  * `ScrollBackendSource.targets()` prepends the window target and raises
    `BackendUnavailableError` when `dump_tree` reports `{"ok": False}` —
    no fabricated snapshot.
  * `run_scroll_region` composes the whole chain against a fake backend
    whose dump_tree emits real-shaped control dicts, and returns a
    `ScrollResult`-shaped dict through the REAL dispatcher (default
    `DispatcherAdapter`). A backend-bound action with no live target returns
    NOT_DISPATCHED / no-effect — never a fabricated success.

No live Windows backend is required; the fake backend's dump_tree shape is
taken from `target/automation/windows_pywinauto.py::dump_tree`.
"""

from __future__ import annotations

from scroll.backend import (
    BackendUnavailableError,
    ScrollBackendSource,
    _shape_control,
    _shape_rect,
    run_scroll_region,
)


def _fake_backend(controls=None, ok=True, error=None):
    """A fake AutomationBackend: dump_tree returns the documented shape."""

    class _FakeBackend:
        def dump_tree(self, window_title_re=None, max_depth=10):
            if not ok:
                return {"ok": False, "error": error or "backend unavailable"}
            return {"ok": True, "controls": controls or []}

    return _FakeBackend()


# --- _shape_control / _shape_rect ------------------------------------------

def test_shape_control_maps_backend_keys():
    d = _shape_control(
        {
            "text": "下一页",
            "title": "Page Next",
            "class_name": "Button",
            "automation_id": "nextPageButton",
            "control_type": "Button",
            "rect": {"x": 10, "y": 20, "width": 40, "height": 300},
        }
    )
    assert d["kind"] == "control"
    assert d["text"] == "下一页"
    assert d["control_type"] == "Button"
    assert d["automation_id"] == "nextPageButton"
    assert d["rect"] == (10, 20, 50, 320)


def test_shape_control_target_is_buildable():
    """The mapped dict satisfies the production snapshot build (assign_target_ids
    assigns target_id; Target.from_dict then accepts it)."""
    from observations.snapshot import build_snapshot

    d = _shape_control({"text": "更多", "class_name": "Button", "control_type": "Button"})
    d.update({"process_name": "EDRClient.exe", "pid": 100, "native_window_id": "w1", "title": "win"})
    snap = build_snapshot(
        targets=[d],
        backend="windows_pywinauto",
        host="test",
        captured_at="2026-08-01T00:00:00Z",
    )
    assert snap.targets[0].kind == "control"
    assert snap.targets[0].text == "更多"


def test_shape_rect_list_vs_mapping():
    assert _shape_rect([1, 2, 3, 4]) == {"rect": (1, 2, 3, 4)}
    assert _shape_rect({"x": 0, "y": 5, "width": 10, "height": 7}) == {"rect": (0, 5, 10, 12)}
    assert _shape_rect(None) == {}


# --- ScrollBackendSource.targets ------------------------------------------

def test_targets_prepends_window_and_shapes_controls():
    src = ScrollBackendSource(
        _fake_backend(
            controls=[
                {"text": "下一页", "class_name": "Button", "automation_id": "nextPageButton"},
                {"text": "上一页", "class_name": "Button", "automation_id": "prevPageButton"},
            ]
        ),
        window_title_re="EDR Client",
        process_name="EDRClient.exe",
        pid=100,
        native_window_id="win-1",
    )
    ts = src.targets()
    assert ts[0]["kind"] == "window"
    assert ts[0]["native_window_id"] == "win-1"
    assert ts[1]["kind"] == "control"
    assert ts[1]["automation_id"] == "nextPageButton"
    # Window target comes first; controls follow in backend traversal order.
    assert [t["kind"] for t in ts] == ["window", "control", "control"]


def test_targets_raises_on_backend_unavailable():
    src = ScrollBackendSource(_fake_backend(ok=False, error="no backend"), window_title_re="x")
    try:
        src.targets()
        assert False, "should raise"
    except BackendUnavailableError as e:
        assert "no backend" in str(e)


def test_targets_window_only_is_valid_snapshot():
    """A window with zero controls still builds a valid (window-only) snapshot
    — the window target always anchors it (Observations build_snapshot
    requires >=1 target)."""
    from observations.snapshot import build_snapshot
    import datetime, uuid

    src = ScrollBackendSource(
        _fake_backend(controls=[]),
        window_title_re="EDR Client",
        process_name="EDRClient.exe",
        pid=100,
        native_window_id="win-1",
    )
    ts = src.targets()
    assert [t["kind"] for t in ts] == ["window"]
    snap = build_snapshot(
        targets=ts,
        backend="windows_pywinauto",
        host="test",
        captured_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        snapshot_id=f"snap-{uuid.uuid4().hex[:8]}",
    )
    assert snap.targets[0].kind == "window"


# --- run_scroll_region (full chain through REAL dispatcher) ---------------

def test_run_scroll_region_returns_result_dict_and_never_fabricates_success():
    """A paginated surface with a next button: dispatch through the real
    dispatcher returns NOT_DISPATCHED (live backend absent -> target missing),
    never a fabricated moved=True."""
    src = ScrollBackendSource(
        _fake_backend(
            controls=[
                {"text": "下一页", "class_name": "Button", "automation_id": "nextPageButton"},
                {"text": "上一页", "class_name": "Button", "automation_id": "prevPageButton"},
            ]
        ),
        window_title_re="EDR Client",
        process_name="EDRClient.exe",
        pid=100,
        native_window_id="win-1",
    )
    out = run_scroll_region(src)
    assert isinstance(out, dict)
    assert "dispatched" in out and "moved" in out and "reason" in out
    # No live target for the backend-bound click -> not a success.
    assert out["success"] is False
    if out["moved"]:
        assert out["dispatched"] is True  # ScrollResult invariant


# --- default window-meta robustness (f8 / review: ProtocolModelError) -------

def test_targets_default_identity_drives_window_meta_from_dump_tree():
    """A `scroll_region()` call with NO explicit identity (process_name/pid/
    native_window_id/window_title all unset) must still build a valid snapshot.
    The window target identity is derived from the backend dump_tree's own
    top-level metadata; without this fix build_snapshot raised
    ProtocolModelError (empty process_name / native_window_id)."""

    class _TreeBackend:
        def dump_tree(self, window_title_re=None, max_depth=10):
            return {
                "ok": True,
                "title": "Asset Management",
                "window_rectangle": {"x": 0, "y": 0, "w": 800, "h": 600},
                "controls": [
                    {"text": "下一页", "class_name": "Button",
                     "automation_id": "nextPageButton", "is_enabled": True},
                ],
            }

    from observations.snapshot import build_snapshot
    import datetime, uuid

    src = ScrollBackendSource(_TreeBackend(), max_depth=6)  # all identity unset
    ts = src.targets()
    # Window meta driven from dump_tree top-level title, not blank defaults.
    assert ts[0]["kind"] == "window"
    assert ts[0]["title"] == "Asset Management"
    assert ts[0]["native_window_id"]  # non-empty (win:... fallback)
    assert ts[0]["process_name"]  # non-empty (title fallback)
    # Controls inherit window identity so they too are buildable.
    assert ts[1]["process_name"] == "Asset Management"
    snap = build_snapshot(
        targets=ts,
        backend="windows_pywinauto",
        host="test",
        captured_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        snapshot_id=f"snap-{uuid.uuid4().hex[:8]}",
    )
    assert snap.targets[0].kind == "window"
    assert snap.targets[1].kind == "control"


def test_run_scroll_region_backend_unavailable_is_honest():
    src = ScrollBackendSource(_fake_backend(ok=False, error="down"), window_title_re="x")
    out = run_scroll_region(src)
    assert out["dispatched"] is False
    assert out["moved"] is False
    assert out["success"] is False
    assert "backend_error" in out
