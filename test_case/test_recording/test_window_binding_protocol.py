"""S3:租约内目标实例绑定协议(09 号设计 §7/§8,真实授权边界 + fake OS 层)。

失败回归先行。复验 1227 十反例对应的回归(真实调用链):

- 授权边界走**现有 target.product_policy**(require_product_run/
  authorized_dispatch;租约由真实 begin_product_run 获取),fake 只替代
  底层 OS/backend(focus/inventory),不以 fake 字典替代产品授权;
- capability 结构化协商:supported=false / coverage=unknown 一律副作用前
  拒绝(focus/dispatch 0 次);
- 生命周期:同实例持续 lost 多次重验均拒;同 HWND/pid/root 重现但无明确
  SHOW 事实 → 拒绝;同 HWND **owner 变化** → 新实例语义(lost);明确
  HIDE→SHOW(同 native/owner/身份)恢复原绑定;正常 HIDE/SHOW 对照;
- 完整身份:root+title 同时声明都须满足;focus 成功后 title 漂移 →
  dispatch 0 次;declared_windows 是权威(未知 W/身份冲突拒绝);
- §7.6:首步 foreign run 拒绝;LOST 绑定不产生 same_window_reverified;
  run 关联来自实际绑定。

无 GUI/网络/线程等待;租约用真实 product_policy(内存态)。
"""

from __future__ import annotations

import dataclasses as dc
import runpy
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

from agent.recording.window_binding import (
    WindowBindingProtocol,
    WindowBindingError,
)
from target import product_policy
from target.product_profiles import get_capability

REAL_DIGEST = get_capability('windows_hisec', 'windows_pywinauto').digest


class _FakeOSBackend:
    """替身 OS/backend 层:inventory/focus 只反映窗口事实,无授权逻辑。
    exe_path_for_pid 是产品路径归属缝隙(§7.1):默认返回已装路径。"""

    def __init__(self, windows):
        self._windows = windows        # {native_id: {pid,title,root,...}}
        self.focus_calls: list[str] = []
        self._lost: set[str] = set()
        self._destroyed: set[str] = set()
        self._rebind: dict[str, dict] = {}
        self.exe_path = ("C:\\Program Files\\HiSec-Endpoint\\core\\"
                         "EDRClient.exe")

    def exe_path_for_pid(self, pid):
        return self.exe_path

    def refresh_inventory(self):
        live = []
        for native_id, w in self._windows.items():
            if native_id in self._destroyed:
                continue
            w = {**w, **self._rebind.get(native_id, {})}
            w["native_id"] = native_id
            w["process_name"] = "EDRClient.exe"
            w["lost"] = native_id in self._lost
            live.append(w)
        return live

    def focus(self, native_id):
        if native_id in self._destroyed:
            return {"ok": False, "error": "gone"}
        self.focus_calls.append(native_id)
        return {"ok": True}

    def window_sequence_capability(self):
        return {"supported": True, "coverage": "strong"}

    def dispatch(self, **kwargs):
        # OS 层替身不拥有授权;授权在 protocol 的 authorized_dispatch 内。
        # 这里只记录调用以断言执行次数(0 或 1)。
        self.dispatch_calls = getattr(self, "dispatch_calls", [])
        self.dispatch_calls.append(kwargs)
        return {"ok": True, "receipt": f"R{len(self.dispatch_calls)}"}

    # 测试驱动的事实变更
    def hide(self, native_id):
        self._lost.add(native_id)

    def show(self, native_id):
        self._lost.discard(native_id)

    def destroy(self, native_id):
        self._destroyed.add(native_id)
        self._lost.discard(native_id)

    def drift(self, native_id, **fields):
        self._rebind.setdefault(native_id, {}).update(fields)


_WINDOWS = {"N1": {"process_id": 42, "title": "logo1",
                   "root_control_id": "MainRoot"},
            "N2": {"process_id": 42, "title": "日志中心",
                   "root_control_id": "LogRoot"}}

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


def _protocol(backend, run_token):
    return WindowBindingProtocol(
        backend=backend, policy=product_policy, lease_token=run_token,
        declared_windows=_DECLARED)


def _bound(backend, protocol, window_id="W1"):
    identity = {**_DECLARED[window_id],
                "_recordedInstanceId": f"RI-{window_id}"}
    return protocol.bind(window_id, identity)


# ---- 能力协商(§8) ----

def test_capability_unsupported_refuses_before_any_side_effect(lease):
    backend = _FakeOSBackend(_WINDOWS)
    backend.window_sequence_capability = (
        lambda: {"supported": False, "coverage": "unknown"})
    protocol = _protocol(backend, lease.run_token)
    with pytest.raises(WindowBindingError) as excinfo:
        protocol.bind("W1", {**_DECLARED["W1"],
                             "_recordedInstanceId": "RI-W1"})
    assert excinfo.value.code == "window_sequence_capability_unavailable"
    assert backend.focus_calls == []


def test_capability_unknown_coverage_refuses(lease):
    # coverage=unknown:强实例能力必须拒绝(§8,S1 录制覆盖不替代 run 跟踪)。
    backend = _FakeOSBackend(_WINDOWS)
    backend.window_sequence_capability = (
        lambda: {"supported": True, "coverage": "unknown"})
    protocol = _protocol(backend, lease.run_token)
    with pytest.raises(WindowBindingError) as excinfo:
        protocol.bind("W1", {**_DECLARED["W1"],
                             "_recordedInstanceId": "RI-W1"})
    assert excinfo.value.code == "window_sequence_capability_unavailable"


def test_capability_true_but_unstructured_refuses(lease):
    # truthiness 不算:非结构化响应一律拒。
    backend = _FakeOSBackend(_WINDOWS)
    backend.window_sequence_capability = lambda: True
    protocol = _protocol(backend, lease.run_token)
    with pytest.raises(WindowBindingError):
        protocol.bind("W1", {**_DECLARED["W1"],
                             "_recordedInstanceId": "RI-W1"})


# ---- 首次绑定唯一性 ----

def test_first_bind_unique_and_two_candidates_ambiguous(lease):
    backend = _FakeOSBackend({k: dict(v) for k, v in _WINDOWS.items()})
    protocol = _protocol(backend, lease.run_token)
    first = _bound(backend, protocol)
    assert first["binding_token"].startswith("WB-")
    assert first["bound_native_id"] == "N1"
    # W2 声明 LogRoot,N2 即唯一候选 → 绑定成功(不同 RI 不同 W)。
    second = _bound(backend, protocol, "W2")
    assert second["bound_native_id"] == "N2"
    # W1 已绑定;双候选场景用未绑定 W3(同 root 声明)解析 → ambiguous。
    backend.drift("N2", root_control_id="MainRoot")
    protocol._declared["W3"] = {"processName": "EDRClient.exe",
                                "rootAutomationId": "MainRoot"}
    with pytest.raises(WindowBindingError) as excinfo:
        protocol.bind("W3", {"processName": "EDRClient.exe",
                             "rootAutomationId": "MainRoot",
                             "_recordedInstanceId": "RI-W3"})
    assert excinfo.value.code == "window_binding_ambiguous"


def test_first_bind_zero_candidates_refuses(lease):
    backend = _FakeOSBackend({"N1": {"process_id": 42, "title": "logo1",
                                      "root_control_id": "OtherRoot"}})
    protocol = _protocol(backend, lease.run_token)
    with pytest.raises(WindowBindingError) as excinfo:
        protocol.bind("W1", {**_DECLARED["W1"],
                             "_recordedInstanceId": "RI-W1"})
    assert excinfo.value.code == "window_binding_not_found"


# ---- LOST:持续多次/兄弟不顶替/same-pid 复用 ----

def test_lost_survives_repeated_reverify_and_sibling(lease):
    backend = _FakeOSBackend({k: dict(v) for k, v in _WINDOWS.items()})
    protocol = _protocol(backend, lease.run_token)
    first = _bound(backend, protocol)
    backend.hide("N1")
    with pytest.raises(WindowBindingError):
        protocol.reverify("W1", first["binding_token"])
    # 兄弟出现(同身份)→ 仍拒绝(LOST 不重猜)。
    backend._windows["N3"] = {"process_id": 42, "title": "logo1",
                              "root_control_id": "MainRoot"}
    for _ in range(3):  # 持续多次重验
        with pytest.raises(WindowBindingError) as excinfo:
            protocol.reverify("W1", first["binding_token"])
        assert excinfo.value.code == "window_binding_lost"
    with pytest.raises(WindowBindingError):
        protocol.bind("W1", {**_DECLARED["W1"],
                             "_recordedInstanceId": "RI-W1"})


def test_same_pid_handle_reappearance_without_show_fact_refuses(lease):
    """同 HWND/pid/root 重现但**无明确 SHOW 事实**(窗口持续 lost)→
    拒绝;明确 HIDE→SHOW 对照见下。"""
    backend = _FakeOSBackend({k: dict(v) for k, v in _WINDOWS.items()})
    protocol = _protocol(backend, lease.run_token)
    first = _bound(backend, protocol)
    # HIDE(协议记录):HIDDEN;inventory lost=True 期间 reverify 拒绝
    # (连续性未知不冒充已证明)。
    protocol.mark_hidden("W1", first["binding_token"])
    backend.hide("N1")
    with pytest.raises(WindowBindingError) as excinfo:
        protocol.reverify("W1", first["binding_token"])
    assert excinfo.value.code == "window_binding_lost"
    # SHOW:inventory lost=False(同 HWND/同 owner 重现)→ reverify 恢复
    # 原绑定(recover_hidden 路径:字段全验)。
    backend.show("N1")
    result = protocol.recover_hidden(
        "W1", first["binding_token"],
        show_fact={"native_id": "N1", "process_id": 42,
                   "root_control_id": "MainRoot", "title": "logo1"})
    assert result["bound_native_id"] == "N1"
    assert protocol.reverify("W1", first["binding_token"])["state"] == "BOUND"


def test_destroy_then_same_pid_reappearance_is_lost(lease):
    """同 HWND/pid/root 重现 = 新实例(§7.4):旧 binding LOST,不恢复。"""
    backend = _FakeOSBackend({k: dict(v) for k, v in _WINDOWS.items()})
    protocol = _protocol(backend, lease.run_token)
    first = _bound(backend, protocol)
    backend.destroy("N1")
    # 同 HWND 新实例重新出现(相同属性):不是旧 binding 的恢复。
    backend._windows["N1"] = {"process_id": 42, "title": "logo1",
                              "root_control_id": "MainRoot"}
    with pytest.raises(WindowBindingError):
        protocol.reverify("W1", first["binding_token"])
    with pytest.raises(WindowBindingError):
        protocol.bind("W1", {**_DECLARED["W1"],
                             "_recordedInstanceId": "RI-W1"})


# ---- owner/root/title 变化 ----

def test_owner_change_between_focus_and_dispatch_zero_actions(lease):
    backend = _FakeOSBackend({k: dict(v) for k, v in _WINDOWS.items()})
    protocol = _protocol(backend, lease.run_token)
    first = _bound(backend, protocol)
    original_focus = backend.focus

    def focusing_then_owner_change(native_id):
        result = original_focus(native_id)
        backend.drift(native_id, process_id=99)  # 聚焦成功后 owner 变化
        return result

    backend.focus = focusing_then_owner_change
    with pytest.raises(WindowBindingError) as excinfo:
        protocol.focus_and_execute(
            "W1", first["binding_token"],
            dispatch=backend.dispatch,
            action={"action_id": "gui.click"})
    assert excinfo.value.code == "window_binding_lost"
    assert not backend.dispatch_calls if hasattr(backend, "dispatch_calls") else True


def test_title_drift_between_focus_and_dispatch_zero_actions(lease):
    backend = _FakeOSBackend({k: dict(v) for k, v in _WINDOWS.items()})
    protocol = _protocol(backend, lease.run_token)
    # title-only 声明的 W(titleRegex 路径):声明 ^logo1$。
    declared = {**_DECLARED, "WT": {"processName": "EDRClient.exe",
                                    "titleRegex": "^logo1$"}}
    protocol = WindowBindingProtocol(
        backend=backend, policy=product_policy,
        lease_token=lease.run_token, declared_windows=declared)
    first = protocol.bind("WT", {"titleRegex": "^logo1$",
                                 "_recordedInstanceId": "RI-WT"})
    original_focus = backend.focus

    def focusing_then_title_drift(native_id):
        result = original_focus(native_id)
        backend.drift(native_id, title="NEVER")  # 聚焦成功后标题漂移
        return result

    backend.focus = focusing_then_title_drift
    with pytest.raises(WindowBindingError) as excinfo:
        protocol.focus_and_execute(
            "WT", first["binding_token"],
            dispatch=backend.dispatch,
            action={"action_id": "gui.click"})
    assert excinfo.value.code == "window_binding_lost"
    # root+title 同时声明则都须满足:root 正确 + title 永不匹配 → 拒绝。
    backend2 = _FakeOSBackend({k: dict(v) for k, v in _WINDOWS.items()})
    protocol2 = _protocol(backend2, lease.run_token)
    with pytest.raises(WindowBindingError) as excinfo:
        protocol2.bind("W1", {**_DECLARED["W1"], "titleRegex": "^NEVER$",
                              "_recordedInstanceId": "RI-x"})
    assert excinfo.value.code == "window_binding_not_found"


# ---- 连续同 W 重验 ----

def test_consecutive_same_window_still_reverified(lease):
    backend = _FakeOSBackend({k: dict(v) for k, v in _WINDOWS.items()})
    protocol = _protocol(backend, lease.run_token)
    first = _bound(backend, protocol)
    protocol.focus_and_execute(
        "W1", first["binding_token"],
        dispatch=backend.dispatch,
        action={"action_id": "gui.click"})
    before = len(backend.focus_calls)
    protocol.focus_and_execute(
        "W1", first["binding_token"],
        dispatch=backend.dispatch,
        action={"action_id": "gui.click"})
    assert len(backend.focus_calls) > before, (
        "连续同 W 也必须重验")


# ---- 声明为权威 ----

def test_undeclared_window_and_conflicting_identity_refused(lease):
    backend = _FakeOSBackend({k: dict(v) for k, v in _WINDOWS.items()})
    protocol = _protocol(backend, lease.run_token)
    with pytest.raises(WindowBindingError) as excinfo:
        protocol.bind("UNDECLARED", {"processName": "EDRClient.exe",
                                     "rootAutomationId": "MainRoot"})
    assert excinfo.value.code == "window_binding_not_declared"
    with pytest.raises(WindowBindingError) as excinfo:
        protocol.bind("W1", {"processName": "Other.exe",
                             "rootAutomationId": "MainRoot"})
    assert excinfo.value.code == "window_binding_not_declared"


# ---- 租约(真实 product_policy) ----

def test_lease_expiry_refuses():
    backend = _FakeOSBackend({k: dict(v) for k, v in _WINDOWS.items()})
    context = product_policy.begin_product_run(
        "windows_hisec", REAL_DIGEST, lease_seconds=1)
    protocol = _protocol(backend, context.run_token)
    import time
    time.sleep(1.1)  # 真实到期
    with pytest.raises(WindowBindingError) as excinfo:
        protocol.bind("W1", {**_DECLARED["W1"],
                             "_recordedInstanceId": "RI-W1"})
    assert excinfo.value.code == "window_binding_lease_refused"
    product_policy.reset_for_tests()


def test_lease_competition_refuses():
    # 另一 run 持有租约 → 本协议拒绝,不触碰他人状态。
    backend = _FakeOSBackend({k: dict(v) for k, v in _WINDOWS.items()})
    product_policy.begin_product_run(
        "windows_hisec", REAL_DIGEST, lease_seconds=300)
    protocol = _protocol(backend, "tok-of-someone-else")
    with pytest.raises(WindowBindingError) as excinfo:
        protocol.bind("W1", {**_DECLARED["W1"],
                             "_recordedInstanceId": "RI-W1"})
    assert excinfo.value.code == "window_binding_lease_refused"
    product_policy.reset_for_tests()


# ---- A→B→A 实际聚焦执行 ----

def test_a_b_a_flow_executes_on_each_window(lease):
    backend = _FakeOSBackend({k: dict(v) for k, v in _WINDOWS.items()})
    protocol = _protocol(backend, lease.run_token)
    b1 = _bound(backend, protocol, "W1")
    protocol.focus_and_execute(
        "W1", b1["binding_token"],
        dispatch=backend.dispatch,
        action={"action_id": "gui.click"})
    b2 = _bound(backend, protocol, "W2")
    protocol.focus_and_execute(
        "W2", b2["binding_token"],
        dispatch=backend.dispatch,
        action={"action_id": "gui.click"})
    # 回 A:重验后执行(A 窗口未变)。
    protocol.focus_and_execute(
        "W1", b1["binding_token"],
        dispatch=backend.dispatch,
        action={"action_id": "gui.click"})
    assert backend.focus_calls == ["N1", "N2", "N1"]


# ---- §7.6 基础 ----

def test_foreign_run_context_refused_and_lost_no_reverified(lease):
    backend = _FakeOSBackend({k: dict(v) for k, v in _WINDOWS.items()})
    protocol = _protocol(backend, lease.run_token)
    first = _bound(backend, protocol)
    # 首步 foreign run:调用者自报 run 与实际绑定不符 → 拒绝。
    with pytest.raises(WindowBindingError) as excinfo:
        protocol.step_context(
            run_id="OTHER_RUN", step_id="step-0001", attempt_id="a1",
            window_id="W1", binding_token=first["binding_token"])
    assert excinfo.value.code == "window_binding_run_mismatch"
    # LOST 绑定:reverify 拒绝(lost);step_context 需要传入上一状态的
    # 真实上下文(跨 run 拒绝);transition 如实标记 unverified_state ——
    # 不产生 same_window_reverified(§7.6:转换依据来自实际验证)。
    ctx1 = protocol.step_context(
        run_id=protocol._bindings["W1"].run_id, step_id="step-0001",
        attempt_id="a1", window_id="W1",
        binding_token=first["binding_token"])
    backend.hide("N1")
    with pytest.raises(WindowBindingError):
        protocol.reverify("W1", first["binding_token"])
    actual_run = protocol._bindings["W1"].run_id
    context = protocol.step_context(
        run_id=actual_run, step_id="step-0002", attempt_id="a2",
        window_id="W1", binding_token=first["binding_token"],
        previous=ctx1)
    assert context["transition"] == "unverified_state_lost", (
        f"LOST 绑定不得产生 same_window_reverified,实际 "
        f"{context['transition']}")
    assert context["previousStepId"] == "step-0001"
