"""The target-side registry binds the real backend lock by HWND, not title."""

from __future__ import annotations

import os
import json
import sys
from dataclasses import replace
from types import SimpleNamespace
from pathlib import Path

import psutil
import pytest

from target import product_policy
from target.product_profiles import ProductWindowRole, get_capability
from target.windowing.models import WindowIdentity, WindowIdentityError
from target.windowing.runtime import WindowSession
from agent.recording.models import GoldenTrace


class FakeBackend:
    def __init__(self):
        self.windows = [{"handle": 1, "pid": 10, "process_name": "EDRClient.exe",
                         "title": "EDRClient", "rect": {"left": 0, "top": 0,
                         "right": 800, "bottom": 600}}]
        self.lock = None

    def _enumerate_top_windows_windows(self, *, strict=False):
        return list(self.windows)

    def connect(self, **kwargs):
        return {"ok": True}

    def lock_window(self, **kwargs):
        item = self.windows[0]
        self.lock = {"handle": item["handle"], "pid": item["pid"],
                     "snapshot": {"handle": item["handle"], "pid": item["pid"],
                                  "process_name": item["process_name"],
                                  "title": item["title"], "rectangle": item["rect"]}}
        return {"ok": True}

    def get_window_lock(self):
        return {"lock": self.lock}

    def unlock_window(self):
        self.lock = None
        return {"ok": True}


def test_role_focus_binds_native_identity_and_rejects_rebuild():
    product_policy.reset_for_tests()
    cap = get_capability("windows_hisec", "windows_pywinauto")
    run = product_policy.begin_product_run(cap.profile_id, cap.digest)
    try:
        backend = FakeBackend()
        session = WindowSession(run.run_id, "windows_pywinauto", backend)
        identity = session.focus_and_lock_role("main", cap, run.run_token)
        assert identity.native_id == "1"
        assert session.verify_locked().generation == 1
        backend.windows = []
        with pytest.raises(WindowIdentityError, match="window_identity_stale"):
            session.verify_locked()
        backend.windows = [{"handle": 1, "pid": 11, "process_name": "EDRClient.exe",
                            "title": "EDRClient", "rect": {"left": 0, "top": 0,
                            "right": 800, "bottom": 600}}]
        session.refresh()
        assert session.registry.list()[0].generation == 2
    finally:
        product_policy.end_product_run(run.run_token)


def test_same_title_same_process_is_role_ambiguous():
    cap = get_capability("windows_hisec", "windows_pywinauto")
    backend = FakeBackend()
    backend.windows.append({**backend.windows[0], "handle": 2})
    session = WindowSession("s", "windows_pywinauto", backend)
    session.refresh()
    with pytest.raises(WindowIdentityError, match="window_role_ambiguous"):
        session.registry.resolve_unique("main", cap)


def test_windows_inventory_retains_root_uia_id_for_same_title_windows(monkeypatch):
    from target.automation.windows_pywinauto import WindowsPywinautoBackend

    class FakeWindow:
        def __init__(self, handle, root):
            self.handle = handle
            self.element_info = SimpleNamespace(automation_id=root)

        def is_top_level(self):
            return True

        def is_visible(self):
            return True

        def window_text(self):
            return "logo1"

        def process_id(self):
            return os.getpid()

        def rectangle(self):
            return SimpleNamespace(left=0, top=0, right=800, bottom=600,
                                   width=lambda: 800, height=lambda: 600)

    windows = [FakeWindow(101, "MainRoot"), FakeWindow(202, "LogRoot")]
    monkeypatch.setitem(sys.modules, "pywinauto", SimpleNamespace(
        Desktop=lambda backend: SimpleNamespace(windows=lambda: windows),
    ))

    result = WindowsPywinautoBackend._enumerate_top_windows_windows(strict=True)

    assert [(item["handle"], item["root_control_id"]) for item in result] == [
        (101, "MainRoot"), (202, "LogRoot"),
    ]


def test_role_focus_connects_the_exact_native_window_after_root_resolution():
    product_policy.reset_for_tests()
    base = get_capability("windows_hisec", "windows_pywinauto")
    capability = replace(base, window_roles=(
        ProductWindowRole("log", ("EDRClient.exe",), "^logo1$", ("LogRoot",)),
    ))
    run = product_policy.begin_product_run(base.profile_id, base.digest)

    class ExactBackend(FakeBackend):
        def __init__(self):
            super().__init__()
            self.windows = [
                {**self.windows[0], "title": "logo1", "root_control_id": "MainRoot"},
                {**self.windows[0], "handle": 2, "title": "logo1", "root_control_id": "LogRoot"},
            ]
            self.connected_handle = None

        def connect_window_identity(self, handle, pid, timeout=10.0):
            self.connected_handle = handle
            return {"ok": True, "handle": handle, "pid": pid}

        def lock_window(self, **kwargs):
            item = next(w for w in self.windows if w["handle"] == self.connected_handle)
            self.lock = {"handle": item["handle"], "pid": item["pid"],
                         "snapshot": {"handle": item["handle"], "pid": item["pid"],
                                      "process_name": item["process_name"],
                                      "title": item["title"], "rectangle": item["rect"]}}
            return {"ok": True}

    try:
        backend = ExactBackend()
        session = WindowSession(run.run_id, "windows_pywinauto", backend)
        focused = session.focus_and_lock_role("log", capability, run.run_token)
        assert backend.connected_handle == 2
        assert focused.native_id == "2"
        assert focused.root_control_id == "LogRoot"
    finally:
        product_policy.end_product_run(run.run_token)


def test_selector_focus_resolves_registered_same_title_window_by_root():
    product_policy.reset_for_tests()
    capability = get_capability("windows_hisec", "windows_pywinauto")
    run = product_policy.begin_product_run(capability.profile_id, capability.digest)
    backend = FakeBackend()
    backend.windows = [
        {**backend.windows[0], "title": "logo1", "root_control_id": "MainRoot"},
        {**backend.windows[0], "handle": 2, "title": "logo1", "root_control_id": "LogRoot"},
    ]
    selected = []

    def connect_exact(handle, pid, timeout=10.0):
        selected.append(handle)
        return {"ok": True, "handle": handle, "pid": pid}

    def lock_selected(**kwargs):
        item = next(window for window in backend.windows if window["handle"] == selected[-1])
        backend.lock = {"handle": item["handle"], "pid": item["pid"],
                        "snapshot": {"handle": item["handle"], "pid": item["pid"],
                                     "process_name": item["process_name"],
                                     "title": item["title"], "rectangle": item["rect"]}}
        return {"ok": True}

    backend.connect_window_identity = connect_exact
    backend.lock_window = lock_selected
    application = {"path": r"C:\Program Files\HiSec-Endpoint\core\EDRClient.exe"}
    session = WindowSession(run.run_id, "windows_pywinauto", backend)
    try:
        identity = session.focus_and_lock_selector(
            "EDRClient.exe", "^logo1$", "LogRoot", capability, application,
            run.run_token, process_exe=lambda pid: application["path"],
        )
        assert identity.native_id == "2"
        assert selected == [2]
        assert session.verify_locked().root_control_id == "LogRoot"
        assert backend.lock["snapshot"]["root_control_id"] == "LogRoot"
        with pytest.raises(WindowIdentityError, match="window_selector_not_found"):
            session.focus_and_lock_selector(
                "EDRClient.exe", "^logo1$", "LogRoot", capability, application,
                run.run_token, process_exe=lambda pid: r"D:\Other\EDRClient.exe",
            )
        assert selected == [2]
        backend.windows.append({**backend.windows[1], "handle": 3})
        with pytest.raises(WindowIdentityError, match="window_selector_ambiguous"):
            session.focus_and_lock_selector(
                "EDRClient.exe", "^logo1$", "LogRoot", capability, application,
                run.run_token, process_exe=lambda pid: application["path"],
            )
        assert selected == [2]
    finally:
        product_policy.end_product_run(run.run_token)


def test_selector_focus_mcp_tool_uses_run_bound_installed_profile(monkeypatch):
    from target import server

    product_policy.reset_for_tests()
    capability = get_capability("windows_hisec", "windows_pywinauto")
    run = product_policy.begin_product_run(capability.profile_id, capability.digest)
    observed = []

    class Session:
        def focus_and_lock_selector(self, *args):
            observed.append(args)
            return SimpleNamespace(native_id="202", root_control_id="LogRoot")

    monkeypatch.setattr(server, "_backend_kind", "windows_pywinauto")
    monkeypatch.setattr(server, "_window_session", Session())
    monkeypatch.setattr(server, "_product_observation", {"stale": True})
    try:
        result = json.loads(server.focus_window_selector(
            "EDRClient.exe", "^logo1$", "LogRoot", run.run_token,
        ))
        assert result["ok"] is True
        assert result["identity"]["native_id"] == "202"
        assert observed[0][3] == capability
        assert observed[0][4]["path"].endswith("EDRClient.exe")
        assert observed[0][5] == run.run_token
        assert server._product_observation is None
    finally:
        product_policy.end_product_run(run.run_token)


def test_macos_role_uses_pid_process_name_not_localized_cg_owner():
    class LocalizedBackend:
        def _list_windows_cg(self):
            return [{"handle": 88, "pid": os.getpid(), "app_name": "本地化名称",
                     "title": "计算器", "rectangle": {"x": 10, "y": 20,
                                              "w": 100, "h": 200}}]

    session = WindowSession("s", "macos_accessibility", LocalizedBackend())
    identity = session.refresh()[0]
    assert identity.process_name == psutil.Process(os.getpid()).name()
    assert identity.title == "计算器"


def test_macos_focus_routes_through_exact_native_identity(monkeypatch):
    class ExactBackend:
        def __init__(self):
            self.connected = None

        def connect_window_identity(self, handle, pid):
            self.connected = (handle, pid)
            return {"ok": True}

        def connect(self, **_kwargs):
            raise AssertionError("title-only connection must not be used")

        def lock_window(self, **_kwargs):
            return {"ok": True}

    backend = ExactBackend()
    session = WindowSession("s", "macos_accessibility", backend)
    identity = WindowIdentity(
        session_id="s", backend="macos_accessibility", native_id="202",
        process_id=42, process_name="EDRClient", title="日志中心",
        rect=(300, 0, 400, 100),
    )
    monkeypatch.setattr(session, "bind_backend_lock", lambda: identity)
    assert session._focus_and_lock_identity(identity) == identity
    assert backend.connected == (202, 42)


def test_product_window_inventory_is_path_owned_and_sees_unlocked_child():
    cap = get_capability("windows_hisec", "windows_pywinauto")
    backend = FakeBackend()
    backend.windows.extend([
        {"handle": 2, "pid": 10, "process_name": "EDRClient.exe",
         "title": "日志中心", "parent_native_id": "1",
         "rect": {"left": 20, "top": 20, "right": 600, "bottom": 400}},
        {"handle": 3, "pid": 20, "process_name": "EDRClient.exe",
         "title": "日志中心", "parent_native_id": "1",
         "rect": {"left": 20, "top": 20, "right": 600, "bottom": 400}},
    ])
    session = WindowSession("s", "windows_pywinauto", backend)
    application = {"path": r"C:\Program Files\HiSec-Endpoint\core\EDRClient.exe"}
    processes = {
        10: r"C:\Program Files\HiSec-Endpoint\core\EDRClient.exe",
        20: r"D:\Other\EDRClient.exe",
    }

    windows = session.observe_product_windows(
        cap, application, process_exe=lambda pid: processes[pid],
    )

    assert {(item.native_id, item.role) for item in windows} == {
        ("1", "main"), ("2", "log_center"),
    }
    assert session.registry.locked is None


def test_product_window_inventory_rejects_ambiguous_registered_role():
    cap = get_capability("windows_hisec", "windows_pywinauto")
    backend = FakeBackend()
    backend.windows.append({**backend.windows[0], "handle": 2})
    session = WindowSession("s", "windows_pywinauto", backend)

    with pytest.raises(WindowIdentityError, match="window_role_ambiguous"):
        session.observe_product_windows(
            cap, {"path": r"C:\Program Files\HiSec-Endpoint\core\EDRClient.exe"},
            process_exe=lambda pid: r"C:\Program Files\HiSec-Endpoint\core\EDRClient.exe",
        )


def test_product_window_inventory_fails_closed_when_ownership_or_enumeration_is_unknown():
    cap = get_capability("windows_hisec", "windows_pywinauto")
    application = {"path": r"C:\Program Files\HiSec-Endpoint\core\EDRClient.exe"}
    session = WindowSession("s", "windows_pywinauto", FakeBackend())

    with pytest.raises(WindowIdentityError, match="product_window_ownership_unverifiable"):
        session.observe_product_windows(
            cap, application, process_exe=lambda pid: None,
        )

    class FailedEnumeration(FakeBackend):
        def _enumerate_top_windows_windows(self, *, strict=False):
            if strict:
                raise ValueError("window_inventory_unavailable")
            return []

    session = WindowSession("s", "windows_pywinauto", FailedEnumeration())
    with pytest.raises(ValueError, match="window_inventory_unavailable"):
        session.observe_product_windows(
            cap, application,
            process_exe=lambda pid: application["path"],
        )

    backend = FakeBackend()
    backend.windows.append({
        "handle": 3, "pid": 10, "process_name": "EDRClient.exe",
        "title": "未登记的弹窗", "rect": {"left": 20, "top": 20,
                                 "right": 600, "bottom": 400},
    })
    session = WindowSession("s", "windows_pywinauto", backend)
    with pytest.raises(WindowIdentityError, match="product_window_unclassified"):
        session.observe_product_windows(
            cap, application, process_exe=lambda pid: application["path"],
        )


def test_product_window_inventory_mcp_uses_active_run_and_installed_spec(monkeypatch):
    from target import server

    cap = get_capability("windows_hisec", "windows_pywinauto")
    identity = WindowIdentity(
        session_id="run", backend="windows_pywinauto", native_id="42",
        process_id=10, process_name="EDRClient.exe", title="日志中心",
        rect=(0, 0, 800, 600), role="log_center",
    )
    seen = []

    class Session:
        def observe_product_windows(self, capability, application):
            seen.append((capability.profile_id, application["path"]))
            return (identity,)

    monkeypatch.setattr(server, "_backend", object())
    monkeypatch.setattr(server, "_backend_kind", "windows_pywinauto")
    monkeypatch.setattr(server, "_window_session", Session())
    monkeypatch.setattr(server._product_policy, "require_product_run",
                        lambda token: SimpleNamespace(capability=cap) if token == "token" else None)

    result = json.loads(server.observe_product_windows("token"))

    assert result["ok"] is True
    assert result["windows"][0]["native_id"] == "42"
    assert seen == [("windows_hisec",
                     r"C:\Program Files\HiSec-Endpoint\core\EDRClient.exe")]


def test_edrclient_catalog_resolves_window_titles_from_existing_recordings():
    root = Path(__file__).resolve().parents[1] / "recordings"
    recorded = set()
    for name in ("hisec_final", "e2e_hisec_flow"):
        golden = GoldenTrace.from_dict(json.loads(
            (root / name / "golden-trace.json").read_text(encoding="utf-8")
        ))
        recorded.update((step.selector.window.get("processName"),
                         step.selector.window.get("titleRegex"))
                        for step in golden.steps if step.selector is not None)
    assert ("EDRClient.exe", "^logo1$") in recorded
    assert ("HiSecEndpointAgent.exe", "^logo1$") in recorded
    assert ("EDRClient.exe", "^日志中心$") in recorded

    backend = FakeBackend()
    backend.windows = [
        {"handle": 1, "pid": 10, "process_name": "EDRClient.exe", "title": "logo1",
         "rect": {"left": 0, "top": 0, "right": 800, "bottom": 600}},
        {"handle": 2, "pid": 11, "process_name": "HiSecEndpointAgent.exe", "title": "logo1",
         "rect": {"left": 0, "top": 0, "right": 400, "bottom": 300}},
        {"handle": 3, "pid": 10, "process_name": "EDRClient.exe", "title": "日志中心",
         "parent_native_id": "1",
         "rect": {"left": 50, "top": 50, "right": 750, "bottom": 550}},
    ]
    session = WindowSession("run", "windows_pywinauto", backend)
    capability = get_capability("windows_hisec", "windows_pywinauto")
    paths = {
        10: r"C:\Program Files\HiSec-Endpoint\core\EDRClient.exe",
        11: r"C:\Program Files\HiSec-Endpoint\core\safra\HisecEndpointAgent.exe",
    }
    windows = session.observe_product_windows(
        capability, {"path": paths[10], "entry_path": paths[11]},
        process_exe=lambda pid: paths[pid],
    )
    assert {(item.role, item.process_name, item.title) for item in windows} == {
        ("entry", "HiSecEndpointAgent.exe", "logo1"),
        ("main", "EDRClient.exe", "logo1"),
        ("log_center", "EDRClient.exe", "日志中心"),
    }
