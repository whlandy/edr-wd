"""focus_entry_window 目标端副作用面(R0 验收:权限与副作用)。

R0 边界:恢复只允许"激活已有窗口" —— 绝不启动进程、绝不操作服务、绝不点击
控件。本回归用调用白名单锁定该工具的行为面:fake backend 记录全部被调用的
后端方法,断言任何处置路径(缺失/歧义/成功/连接失败/锁定失败/绑定失败)下,
被调用的方法都在安全白名单内;任何 activate_edr/_start_service/launch/click
类方法被触达即红。

同时锁定结构化处置:absent/ambiguous/probe 失败不产生任何变更;成功路径
返回 acquired 且窗口身份与枚举一致。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from target import product_policy, server
from target.product_profiles import get_capability

pytestmark = pytest.mark.unit


class RecordingBackend:
    """记录每个被调用的方法名;白名单外的方法一旦被调,测试即红。"""

    def __init__(self, windows):
        self.windows = windows
        self.called = []

    def _record(self, name):
        self.called.append(name)
        return self

    # 只读枚举
    def is_window_open(self, title_re=None, process_name=None, class_name=None):
        self._record("is_window_open")
        return {"ok": True, "windows": self.windows}

    # 精确连接(不解析不启动)
    def connect_window_identity(self, handle, pid, timeout=10.0):
        self._record("connect_window_identity")
        return {"ok": True}

    # 严格锁 + 激活(restore+set_focus)
    def lock_window(self, **kwargs):
        self._record("lock_window")
        return {"ok": True, "lock": {"handle": 5, "pid": 9, "snapshot": {}}}

    def unlock_window(self):
        self._record("unlock_window")
        return {"ok": True}

    def get_window_lock(self):
        self._record("get_window_lock")
        return {"ok": True, "locked": True,
                "lock": {"handle": 5, "pid": 9, "snapshot": {}}}

    # 危险方法:只要被触达,白名单断言即红
    def activate_edr(self, *args, **kwargs):
        self._record("!!activate_edr")
        return {"ok": True}

    def activate_registered_app(self, *args, **kwargs):
        self._record("!!activate_registered_app")
        return {"ok": True}

    def _start_service(self, *args, **kwargs):
        self._record("!!_start_service")
        return True, None


ALLOWED = {"is_window_open", "connect_window_identity", "lock_window",
           "unlock_window", "get_window_lock"}


@pytest.fixture()
def lease():
    product_policy.reset_for_tests()
    capability = get_capability("windows_hisec", "windows_pywinauto")
    run = product_policy.begin_product_run(capability.profile_id, capability.digest)
    yield run
    product_policy.reset_for_tests()


def _run_tool(monkeypatch, backend, windows):
    session = SimpleNamespace(bind_backend_lock=lambda: None)
    monkeypatch.setattr(server, "_backend_kind", "windows_pywinauto")
    monkeypatch.setattr(server, "_backend", backend)
    monkeypatch.setattr(server, "_window_session", session)
    return json.loads(server.focus_entry_window(
        "EDRClient.exe", "^logo1$", lease_run_token[0],
    ))


lease_run_token = [None]


@pytest.fixture(autouse=True)
def _capture_token(lease):
    lease_run_token[0] = lease.run_token
    yield


def test_absent_is_read_only(monkeypatch):
    backend = RecordingBackend([])
    result = _run_tool(monkeypatch, backend, [])
    assert result["code"] == "entry_window_absent"
    assert backend.called == ["is_window_open"], "缺失处置必须只读,零变更方法"


def test_ambiguous_is_read_only(monkeypatch):
    windows = [{"handle": 5, "process_id": 9, "title": "logo1"},
               {"handle": 6, "process_id": 9, "title": "logo1"}]
    backend = RecordingBackend(windows)
    result = _run_tool(monkeypatch, backend, windows)
    assert result["code"] == "entry_window_ambiguous"
    assert result["candidates"][0]["handle"] == 5
    assert backend.called == ["is_window_open"], "歧义处置必须只读,绝不猜一个去锁"


def test_success_calls_only_whitelisted_methods(monkeypatch):
    windows = [{"handle": 5, "process_id": 9, "title": "logo1"}]
    backend = RecordingBackend(windows)
    result = _run_tool(monkeypatch, backend, windows)
    assert result["ok"] is True and result["disposition"] == "acquired"
    assert result["window"] == {"pid": 9, "title": "logo1", "handle": 5}
    assert set(backend.called) <= ALLOWED, (
        f"行为面越权: {sorted(set(backend.called) - ALLOWED)}")
    assert not [c for c in backend.called if c.startswith("!!")], (
        "activate_edr / 启动应用 / 服务操作 / 任何危险方法被触达")


def test_connect_failure_stops_before_lock(monkeypatch):
    windows = [{"handle": 5, "process_id": 9, "title": "logo1"}]
    backend = RecordingBackend(windows)

    def refusing_connect(handle, pid, timeout=10.0):
        backend.called.append("connect_window_identity")
        return {"ok": False, "error": "window gone"}

    backend.connect_window_identity = refusing_connect
    result = _run_tool(monkeypatch, backend, windows)
    assert result["code"] == "entry_window_connect_failed"
    assert "lock_window" not in backend.called, "连接失败不得继续锁定"


def test_lock_failure_reports_code(monkeypatch):
    windows = [{"handle": 5, "process_id": 9, "title": "logo1"}]
    backend = RecordingBackend(windows)

    def refusing_lock(**kwargs):
        backend.called.append("lock_window")
        return {"ok": False, "code": "window_lock_criteria_mismatch",
                "error": "mismatch"}

    backend.lock_window = refusing_lock
    result = _run_tool(monkeypatch, backend, windows)
    assert result["code"] == "window_lock_criteria_mismatch"


def test_bind_failure_releases_lock(monkeypatch):
    windows = [{"handle": 5, "process_id": 9, "title": "logo1"}]
    backend = RecordingBackend(windows)

    class Session:
        def bind_backend_lock(self):
            raise RuntimeError("window_identity_stale")

    monkeypatch.setattr(server, "_backend_kind", "windows_pywinauto")
    monkeypatch.setattr(server, "_backend", backend)
    monkeypatch.setattr(server, "_window_session", Session())
    result = json.loads(server.focus_entry_window(
        "EDRClient.exe", "^logo1$", lease_run_token[0]))
    assert result["code"] == "entry_window_bind_failed"
    assert "unlock_window" in backend.called, "注册表绑定失败必须释放已取得的锁"


def test_expired_token_refuses(monkeypatch):
    backend = RecordingBackend([])
    monkeypatch.setattr(server, "_backend_kind", "windows_pywinauto")
    monkeypatch.setattr(server, "_backend", backend)
    result = json.loads(server.focus_entry_window(
        "EDRClient.exe", "^logo1$", "not-a-valid-token"))
    assert result["ok"] is False
    assert backend.called == [], "无效租约下零方法调用"
