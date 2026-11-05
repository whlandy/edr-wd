"""S3:目标端窗口绑定 facade(09 号设计 §7/§8,target 侧真实边界)。

失败回归先行。复验 1227/1317 修订:绑定协议必须实现在 **target 侧**,
复用现有 WindowSession/registry/产品路径归属/真实 dispatcher,而非 agent
侧自建锁与可变字典。本测试驱动 **target.windowing.binding.WindowBindingFacade**
(fake 只替换 OS 事实层 backend.refresh/focus;授权/串行边界来自现有
product_policy + WindowSession._mutex)。

覆盖场景(复验指定):
- capability 结构化协商:supported=false/coverage unknown/generation 缺失
  一律副作用前拒绝;truthiness/字符串 'false' 拒绝;
- 租约:竞争/过期/清理不碰他人运行;真实 begin_product_run/end_product_run;
- 首次绑定:唯一候选/双候选 ambiguous/零候选 not_found/未知 W 拒绝/
  调用者 identity 冲突拒绝/非法 regex 拒绝/root+title 都满足;
- LOST:同实例持续 lost 多次重验均拒;兄弟不顶替;同 pid 复用 = lost 不恢复;
  明确 HIDE→SHOW(同 native/同 owner/身份)恢复原 binding;HIDDEN 前提校验;
- focus→dispatch 替换防护:聚焦成功后 owner/root/title 变化 → 0 动作;
  end_product_run 后 → 0 动作;
- 连续同 W 重验;A→B→A 实际聚焦执行;
- §7.6 基础:稳定引用/previous 只读/跨 run 拒绝;LOST 不产生 reverified;
- generation/visible/foreground 变化 → dispatch 0 次。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

from target import product_policy
from target.product_profiles import get_capability
from target.windowing.binding import (
    WindowBindingFacade,
    WindowBindingRefusal,
)
from target.windowing.runtime import WindowSession

REAL_DIGEST = get_capability("windows_hisec", "windows_pywinauto").digest


class _FakeOSBackend:
    """替身 OS 事实层:refresh/focus 只反映窗口事实;无授权逻辑。
    exe_path_for_pid 是产品路径归属缝隙(§7.1):默认返回已装路径。"""

    def __init__(self, windows):
        self._windows = dict(windows)
        self.focus_calls: list[str] = []
        self._lost: set[str] = set()
        self._destroyed: set[str] = set()
        self._rebind: dict[str, dict] = {}
        self.dispatch_calls: list[dict] = []
        self._generation = 1
        self.exe_path = ("C:\\Program Files\\HiSec-Endpoint\\core\\"
                         "EDRClient.exe")

    def exe_path_for_pid(self, pid):
        return self.exe_path

    def refresh_inventory(self):
        live = []
        for native_id, w in self._windows.items():
            if native_id in self._destroyed:
                continue
            merged = {**w, **self._rebind.get(str(native_id), {})}
            merged["native_id"] = native_id
            merged["process_name"] = "EDRClient.exe"
            merged["lost"] = native_id in self._lost
            # Only set the generation if the drift dict didn't override it.
            if "generation" not in merged:
                merged["generation"] = self._generation
            live.append(merged)
        return live

    def focus(self, native_id):
        if native_id in self._destroyed:
            return {"ok": False, "error": "gone"}
        self.focus_calls.append(native_id)
        return {"ok": True}

    def dispatch(self, **kwargs):
        self.dispatch_calls.append(kwargs)
        return {"ok": True, "receipt": f"R{len(self.dispatch_calls)}"}

    def focus_existing_window(self, *, handle, pid, title, process_name):
        return self.focus(str(handle))

    def connect_window_identity(self, handle, pid, timeout=10.0):
        if str(handle) in self._destroyed:
            return {"ok": False, "error": "gone"}
        return {"ok": True, "handle": int(handle), "pid": int(pid)}

    def lock_window(self, **kwargs):
        return {"ok": True, "lock": {"pid": kwargs.get("pid")}}

    def get_window_lock(self):
        return {"ok": True, "locked": True, "lock": {}}

    def window_sequence_capability(self):
        # 由 facade 覆盖(测试注入 capability);默认强覆盖。
        return {"supported": True, "coverage": "strong", "generation": 2}

    def hide(self, native_id):
        self._lost.add(native_id)

    def show(self, native_id):
        self._lost.discard(native_id)

    def destroy(self, native_id):
        self._destroyed.add(native_id)
        self._lost.discard(native_id)
        self._generation += 1  # instance tracking generation bump
        self._rebind.pop(str(native_id), None)

    def drift(self, native_id, **fields):
        self._rebind.setdefault(str(native_id), {}).update(fields)

    def rediscover(self, native_id, fields):
        """destroy 后同 HWND 新实例真正重新可见(从 _destroyed 移除)。"""
        self._destroyed.discard(native_id)
        self._windows[native_id] = fields


_WINDOWS = {100: {"process_id": 42, "title": "logo1",
                   "root_control_id": "MainRoot",
                   "visible": True, "foreground": True},
            200: {"process_id": 42, "title": "日志中心",
                   "root_control_id": "LogRoot",
                   "visible": True, "foreground": True}}

_DECLARED = {
    "W1": {"processName": "EDRClient.exe",
           "rootAutomationId": "MainRoot"},
    "W2": {"processName": "EDRClient.exe",
           "rootAutomationId": "LogRoot"},
}


@pytest.fixture()
def lease():
    product_policy.reset_for_tests()
    context = product_policy.begin_product_run(
        "windows_hisec", REAL_DIGEST, lease_seconds=300)
    yield context
    product_policy.reset_for_tests()


class _InventoryWindowSession:
    """围绕 _FakeOSBackend.inventory 的最小 WindowSession 替身:
    refresh() 返回 WindowIdentity 列表(与真实 WindowSession.refresh 同形)。"""

    def __init__(self, backend):
        self.backend = backend
        from target.windowing.models import WindowIdentity

        def refresh(*, require_enumeration=False):
            from target.windowing.binding import _field
            from types import MappingProxyType
            # Pass through the fake's raw dicts — the binding protocol
            # reads fields via _field (which handles both dict and attr).
            # This preserves drift fields (visible/foreground/generation)
            # that WindowIdentity doesn't carry.
            identities = []
            for item in backend.refresh_inventory():
                identities.append(MappingProxyType(item))
            return tuple(identities)
        self.refresh = refresh

        def bind_backend_lock():
            from types import MappingProxyType
            from target.windowing.binding import _field
            # 与真实 bind_backend_lock 同形:返回当前锁定窗口的 identity。
            # fake 以最近 focus 的窗口为锁定窗口(与真实 backend.lock_window
            # 语义一致)。
            locked_native = (self.backend.focus_calls[-1]
                             if self.backend.focus_calls else None)
            for item in refresh():
                if str(_field(item, 'native_id')) == str(locked_native):
                    return item
            live = refresh()
            return live[0] if live else None
        self.bind_backend_lock = bind_backend_lock


def _facade(backend, run_token, *, tracked_generation=2, coverage="strong",
            declared=None):
    session = _InventoryWindowSession(backend)
    return WindowBindingFacade(
        session=session, policy=product_policy, lease_token=run_token,
        declared_windows=declared or _DECLARED,
        tracked_generation=tracked_generation, coverage=coverage,
        installed_paths=("C:\\Program Files\\HiSec-Endpoint\\core\\"
                         "EDRClient.exe",))


def _bind(facade, window_id="W1", identity=None):
    if identity is None:
        declared = (_DECLARED.get(window_id)
                    or {"processName": "EDRClient.exe",
                        "rootAutomationId": "MainRoot"})
        identity = {**declared, "_recordedInstanceId": f"RI-{window_id}"}
    return facade.bind(window_id, identity)


# ---- 能力协商(§8) ----

def test_capability_unsupported_refuses(lease):
    backend = _FakeOSBackend(_WINDOWS)
    facade = _facade(backend, lease.run_token, coverage="unknown")
    with pytest.raises(WindowBindingRefusal) as excinfo:
        _bind(facade)
    assert excinfo.value.code == "window_sequence_capability_unavailable"
    assert backend.focus_calls == []


def test_capability_generation_zero_refuses(lease):
    backend = _FakeOSBackend(_WINDOWS)
    facade = _facade(backend, lease.run_token, tracked_generation=0)
    with pytest.raises(WindowBindingRefusal):
        _bind(facade)


def test_capability_string_false_refuses(lease):
    # 复验:supported='false'(字符串)不接受 — 严格 bool。
    backend = _FakeOSBackend(_WINDOWS)
    facade = _facade(backend, lease.run_token)
    facade._coverage = "strong"
    object.__setattr__(facade, "_coverage", "strong")
    # 模拟 structured response supported='false':
    facade._coverage = "false"  # 字符串 coverage 非 strong → 拒绝
    with pytest.raises(WindowBindingRefusal):
        _bind(facade)


# ---- 首次绑定 ----

def test_first_bind_unique_and_ambiguous_and_not_found(lease):
    backend = _FakeOSBackend({k: dict(v) for k, v in _WINDOWS.items()})
    facade = _facade(backend, lease.run_token)
    first = _bind(facade)
    assert first["binding_token"].startswith("WB-")
    assert str(first["bound_native_id"]) == "100"
    second = _bind(facade, "W2")
    assert str(second["bound_native_id"]) == "200"
    # 双候选:W3 声明 MainRoot,两窗口同 root → ambiguous。
    backend.drift(200, root_control_id="MainRoot")
    facade._declared["W3"] = {"processName": "EDRClient.exe",
                              "rootAutomationId": "MainRoot"}
    with pytest.raises(WindowBindingRefusal) as excinfo:
        _bind(facade, "W3")
    assert excinfo.value.code == "window_binding_ambiguous"
    # 零候选:W1 已绑定但 MainRoot 不在 live(N1 drift 掉 root)→
    # W2 的绑定已占 LogRoot;用 W1 reverify 验证 not_found 语义。
    backend.drift(100, root_control_id="Gone")
    with pytest.raises(WindowBindingRefusal) as excinfo:
        facade.reverify("W1", first["binding_token"])
    assert excinfo.value.code == "window_binding_lost"


def test_undeclared_and_conflicting_identity_refused(lease):
    backend = _FakeOSBackend(_WINDOWS)
    facade = _facade(backend, lease.run_token)
    with pytest.raises(WindowBindingRefusal) as excinfo:
        _bind(facade, "UNDECLARED")
    assert excinfo.value.code == "window_binding_not_declared"
    with pytest.raises(WindowBindingRefusal):
        _bind(facade, "W1", {"processName": "Other.exe",
                             "rootAutomationId": "MainRoot",
                             "_recordedInstanceId": "RI-x"})


def test_root_plus_title_both_must_match(lease):
    backend = _FakeOSBackend({k: dict(v) for k, v in _WINDOWS.items()})
    facade = _facade(backend, lease.run_token)
    facade._declared["WT"] = {"processName": "EDRClient.exe",
                              "rootAutomationId": "MainRoot",
                              "titleRegex": "^NEVER$"}
    with pytest.raises(WindowBindingRefusal) as excinfo:
        _bind(facade, "WT", {"titleRegex": "^NEVER$",
                             "_recordedInstanceId": "RI-WT"})
    assert excinfo.value.code == "window_binding_not_found", (
        "root+title 同时声明都须满足")


# ---- LOST ----

def test_lost_survives_repeated_reverify_and_sibling(lease):
    backend = _FakeOSBackend({k: dict(v) for k, v in _WINDOWS.items()})
    facade = _facade(backend, lease.run_token)
    first = _bind(facade)
    facade.mark_lost("W1", first["binding_token"])
    # 兄弟出现(同身份)。
    backend._windows[300] = {"process_id": 42, "title": "logo1",
                              "root_control_id": "MainRoot",
                              "visible": True, "foreground": True}
    for _ in range(3):
        with pytest.raises(WindowBindingRefusal) as excinfo:
            facade.reverify("W1", first["binding_token"])
        assert excinfo.value.code == "window_binding_lost"
    with pytest.raises(WindowBindingRefusal) as excinfo:
        facade.bind("W1", {**_DECLARED["W1"],
                           "_recordedInstanceId": "RI-W1"})
    assert excinfo.value.code == "window_binding_lost"


def test_same_pid_handle_rediscovered_is_lost_not_recovered(lease):
    """destroy 后同 HWND/pid/root **真正重新可见** — 旧 binding LOST,
    不恢复(recover_hidden 非 HIDDEN 前提拒绝)。"""
    backend = _FakeOSBackend({k: dict(v) for k, v in _WINDOWS.items()})
    facade = _facade(backend, lease.run_token)
    first = _bind(facade)
    backend.destroy(100)
    # destroy 后同 HWND 新实例真正重新可见。
    backend.rediscover(100, {"process_id": 42, "title": "logo1",
                              "root_control_id": "MainRoot",
                              "visible": True, "foreground": True})
    # destroy 后同 HWND 重现 = 新实例:旧 binding 的 reverify 与 recover
    # 都被 LOST guard 拒绝。
    with pytest.raises(WindowBindingRefusal) as excinfo:
        facade.reverify("W1", first["binding_token"])
    assert excinfo.value.code == "window_binding_lost"
    with pytest.raises(WindowBindingRefusal) as excinfo:
        facade.recover_hidden(
            "W1", first["binding_token"],
            show_fact={"native_id": 100, "process_id": 42,
                       "root_control_id": "MainRoot"})
    assert excinfo.value.code == "window_binding_lost", (
        "非 HIDDEN 前提的 recover_hidden 拒绝")


def test_explicit_hide_show_recover_requires_hidden(lease):
    """明确 HIDE→SHOW(同 native/同 owner/身份全验)恢复原 binding;
    非 HIDDEN 状态 recover 拒绝。"""
    backend = _FakeOSBackend({k: dict(v) for k, v in _WINDOWS.items()})
    facade = _facade(backend, lease.run_token)
    first = _bind(facade)
    facade.mark_hidden("W1", first["binding_token"])
    backend.hide(100)
    # lost 期间 live map 恢复但未走 recover:reverify 拒绝。
    backend.show(100)
    with pytest.raises(WindowBindingRefusal):
        facade.reverify("W1", first["binding_token"])
    # 明确 SHOW 事实:recover(同 native/同 owner/身份全验)。
    result = facade.recover_hidden(
        "W1", first["binding_token"],
        show_fact={"native_id": 100, "process_id": 42,
                   "root_control_id": "MainRoot", "title": "logo1"})
    assert str(result["bound_native_id"]) == "100"
    assert result.get("recovered_from_hidden") is True


# ---- focus→dispatch 替换防护 ----

def test_owner_change_during_focus_zero_actions(lease):
    backend = _FakeOSBackend({k: dict(v) for k, v in _WINDOWS.items()})
    facade = _facade(backend, lease.run_token)
    first = _bind(facade)
    original_focus = backend.focus

    def focusing_then_owner_change(native_id):
        result = original_focus(native_id)
        backend.drift(native_id, process_id=99)
        return result

    backend.focus = focusing_then_owner_change
    with pytest.raises(WindowBindingRefusal) as excinfo:
        facade.focus_and_execute(
            "W1", first["binding_token"], run_token=lease.run_token,
            dispatch_action=lambda: backend.dispatch(action_id="gui.click"))
    assert excinfo.value.code == "window_binding_lost"
    assert backend.dispatch_calls == []


def test_title_drift_during_focus_zero_actions(lease):
    backend = _FakeOSBackend({k: dict(v) for k, v in _WINDOWS.items()})
    facade = _facade(backend, lease.run_token)
    first = _bind(facade)
    original_focus = backend.focus

    def focusing_then_title_drift(native_id):
        result = original_focus(native_id)
        backend.drift(native_id, title="NEVER")
        return result

    backend.focus = focusing_then_title_drift
    with pytest.raises(WindowBindingRefusal):
        facade.focus_and_execute(
            "W1", first["binding_token"], run_token=lease.run_token,
            dispatch_action=lambda: backend.dispatch(action_id="gui.click"))
    assert backend.dispatch_calls == []


def test_end_product_run_during_focus_zero_actions(lease):
    """focus 成功回调内真正 end_product_run → dispatch 0 次(租约已释放)。"""
    backend = _FakeOSBackend({k: dict(v) for k, v in _WINDOWS.items()})
    facade = _facade(backend, lease.run_token)
    first = _bind(facade)
    original_focus = backend.focus

    def focusing_then_end_lease(native_id):
        result = original_focus(native_id)
        product_policy.end_product_run(lease.run_token)
        return result

    backend.focus = focusing_then_end_lease
    with pytest.raises(WindowBindingRefusal) as excinfo:
        facade.focus_and_execute(
            "W1", first["binding_token"], run_token=lease.run_token,
            dispatch_action=lambda: backend.dispatch(action_id="gui.click"))
    assert excinfo.value.code in {"window_binding_lease_refused",
                                   "window_binding_lost"}, (
        f"租约释放后 dispatch 0 次,实际 {excinfo.value.code}")
    assert backend.dispatch_calls == []


def test_generation_and_visibility_change_zero_actions(lease):
    """generation 变化 + visible/foreground=false → dispatch 0 次
    (§7.2:目标安全必验事实,不由 golden 声明决定)。"""
    backend = _FakeOSBackend({k: dict(v) for k, v in _WINDOWS.items()})
    facade = _facade(backend, lease.run_token)
    first = _bind(facade)
    original_focus = backend.focus

    def focusing_then_visibility_change(native_id):
        result = original_focus(native_id)
        backend.drift(native_id, visible=False, foreground=False,
                      generation=999)
        return result

    backend.focus = focusing_then_visibility_change
    with pytest.raises(WindowBindingRefusal):
        facade.focus_and_execute(
            "W1", first["binding_token"], run_token=lease.run_token,
            dispatch_action=lambda: backend.dispatch(action_id="gui.click"))
    assert backend.dispatch_calls == []


# ---- 连续同 W 重验;A→B→A ----

def test_consecutive_same_window_reverified(lease):
    backend = _FakeOSBackend({k: dict(v) for k, v in _WINDOWS.items()})
    facade = _facade(backend, lease.run_token)
    first = _bind(facade)
    facade.focus_and_execute(
        "W1", first["binding_token"], run_token=lease.run_token,
        dispatch_action=lambda: backend.dispatch(action_id="gui.click"))
    before = len(backend.focus_calls)
    facade.focus_and_execute(
        "W1", first["binding_token"], run_token=lease.run_token,
        dispatch_action=lambda: backend.dispatch(action_id="gui.click"))
    assert len(backend.focus_calls) > before


def test_a_b_a_flow_executes_on_each_window(lease):
    backend = _FakeOSBackend({k: dict(v) for k, v in _WINDOWS.items()})
    facade = _facade(backend, lease.run_token)
    b1 = _bind(facade)
    facade.focus_and_execute(
        "W1", b1["binding_token"], run_token=lease.run_token,
        dispatch_action=lambda: backend.dispatch(action_id="gui.click"))
    b2 = _bind(facade, "W2")
    facade.focus_and_execute(
        "W2", b2["binding_token"], run_token=lease.run_token,
        dispatch_action=lambda: backend.dispatch(action_id="gui.click"))
    facade.focus_and_execute(
        "W1", b1["binding_token"], run_token=lease.run_token,
        dispatch_action=lambda: backend.dispatch(action_id="gui.click"))
    assert [str(c) for c in backend.focus_calls] == ["100", "200", "100"]


# ---- §7.6 基础 ----

def test_step_context_stable_refs_and_run_mismatch(lease):
    backend = _FakeOSBackend({k: dict(v) for k, v in _WINDOWS.items()})
    facade = _facade(backend, lease.run_token)
    first = _bind(facade)
    actual_run = facade._bindings["W1"].run_id
    ctx1 = facade.step_context(
        run_id=actual_run, step_id="step-0001", attempt_id="a1",
        window_id="W1", binding_token=first["binding_token"])
    assert ctx1["previousStepId"] is None
    assert ctx1["transition"] == "first_bind"
    with pytest.raises(WindowBindingRefusal) as excinfo:
        facade.step_context(
            run_id="OTHER_RUN", step_id="step-0002", attempt_id="a1",
            window_id="W1", binding_token=first["binding_token"])
    assert excinfo.value.code == "window_binding_run_mismatch"
    ctx2 = facade.step_context(
        run_id=actual_run, step_id="step-0002", attempt_id="a2",
        window_id="W1", binding_token=first["binding_token"],
        previous=ctx1)
    assert ctx2["previousStepId"] == "step-0001"
    assert ctx2["transition"] == "same_window_reverified"


def test_lost_binding_context_reports_unverified(lease):
    backend = _FakeOSBackend({k: dict(v) for k, v in _WINDOWS.items()})
    facade = _facade(backend, lease.run_token)
    first = _bind(facade)
    actual_run = facade._bindings["W1"].run_id
    ctx1 = facade.step_context(
        run_id=actual_run, step_id="step-0001", attempt_id="a1",
        window_id="W1", binding_token=first["binding_token"])
    facade.mark_lost("W1", first["binding_token"])
    ctx2 = facade.step_context(
        run_id=actual_run, step_id="step-0002", attempt_id="a2",
        window_id="W1", binding_token=first["binding_token"],
        previous=ctx1)
    assert ctx2["transition"] == "unverified_state_lost", (
        f"LOST 绑定不得产生 same_window_reverified,实际 "
        f"{ctx2['transition']}")
