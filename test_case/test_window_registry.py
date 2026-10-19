"""Window identity must survive titles but not handle reuse or role ambiguity."""

from __future__ import annotations

import pytest

from agent.products import get_product_profile
from target.windowing import WindowIdentity, WindowIdentityError, WindowRegistry
from target.windowing.relations import link_parent


def _window(handle=1, pid=10, process="EDRClient.exe", title="EDRClient",
            root="EDRClient", **extra):
    return {"handle": handle, "pid": pid, "process_name": process,
            "title": title, "root_control_id": root,
            "rect": {"left": 0, "top": 0, "right": 800, "bottom": 600}, **extra}


def test_identity_requires_native_id_and_session():
    with pytest.raises(WindowIdentityError, match="window_identity_unavailable"):
        WindowIdentity.from_native({"pid": 1, "rect": [0, 0, 2, 2]},
                                   session_id="s", backend="windows_pywinauto")
    identity = WindowIdentity.from_native(_window(), session_id="s", backend="windows_pywinauto")
    assert identity.binding[0] == "s"


def test_reconcile_rebuild_and_handle_reuse_invalidates_old_lock():
    registry = WindowRegistry("s", "windows_pywinauto")
    assert len(registry.reconcile([_window()]).opened) == 1
    old = registry.focus_and_lock(registry.list()[0])
    assert registry.verify(old)
    assert len(registry.reconcile([]).closed) == 1
    assert not registry.verify(old)
    registry.reconcile([_window(pid=11)])
    assert registry.list()[0].generation == old.generation + 1
    with pytest.raises(WindowIdentityError, match="window_identity_stale"):
        registry.focus_and_lock(old)


def test_handle_reuse_remains_stale_after_long_idle_period():
    registry = WindowRegistry("s", "windows_pywinauto")
    old = registry.reconcile([_window()], now=0).opened[0]
    registry.reconcile([], now=1)
    new = registry.reconcile([_window()], now=10_000).opened[0]
    assert new.generation > old.generation
    assert not registry.verify(old)


def test_role_resolution_rejects_same_title_across_processes_and_duplicates():
    registry = WindowRegistry("s", "windows_pywinauto")
    profile = get_product_profile("windows_hisec", "windows_pywinauto")
    registry.reconcile([_window(), _window(handle=2, process="Other.exe")])
    assert registry.resolve_unique("main", profile).native_id == "1"
    registry.reconcile([_window(), _window(handle=2)])
    with pytest.raises(WindowIdentityError, match="window_role_ambiguous"):
        registry.resolve_unique("main", profile)


def test_event_hints_reconcile_to_enumeration_and_parent_requires_proof():
    registry = WindowRegistry("s", "windows_pywinauto")
    registry.reconcile([_window()])
    registry.apply_event({"kind": "closed", "native_id": "1"})
    registry.apply_event({"kind": "closed", "native_id": "1"})
    registry.reconcile([_window()])
    assert len(registry.list()) == 1
    parent = registry.list()[0]
    child = WindowIdentity.from_native(_window(handle=2, parent_native_id="1"),
                                       session_id="s", backend="windows_pywinauto")
    assert link_parent(child, (parent,)) == "1"
    no_proof = WindowIdentity.from_native(_window(handle=3), session_id="s",
                                          backend="windows_pywinauto")
    assert link_parent(no_proof, (parent,)) is None


def test_broad_dialog_role_requires_verified_parent_window():
    profile = get_product_profile("windows_hisec", "windows_pywinauto")
    registry = WindowRegistry("s", "windows_pywinauto")
    registry.reconcile([
        _window(),
        _window(handle=2, title="Settings", root="", parent_native_id="1"),
        _window(handle=3, title="Other", root=""),
    ])
    assert registry.resolve_unique("dialog", profile).native_id == "2"
    registry.reconcile([_window(), _window(handle=3, title="Other", root="")])
    with pytest.raises(WindowIdentityError, match="window_role_not_found"):
        registry.resolve_unique("dialog", profile)


def test_explicit_foreign_parent_rejects_even_title_matched_log_window():
    profile = get_product_profile("windows_hisec", "windows_pywinauto")
    registry = WindowRegistry("s", "windows_pywinauto")
    registry.reconcile([
        _window(),
        _window(handle=2, title="日志中心", root="logCenter", parent_native_id="999"),
    ])
    with pytest.raises(WindowIdentityError, match="window_role_not_found"):
        registry.resolve_unique("log_center", profile)
