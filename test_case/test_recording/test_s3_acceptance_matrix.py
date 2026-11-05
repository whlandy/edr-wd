"""S3 原验收矩阵核对(09 §7/§8;用户指令片1):生产接线诚实性与矩阵覆盖。

核对面(不能用 fake backend 自报 strong 代替生产能力):
- 生产 backend(windows_pywinauto/macos_accessibility)**未实现**
  window_sequence_capability → 真实 begin_product_run 初始化后 facade
  capability 必须 supported=false → bind fail closed(不伪造 ready)。
- 本 run 原生实例跟踪能力:capability 结构化声明的 generation/coverage
  来自 backend 自身事实;丢事件/同 handle 复用/同 title-root 多实例的
  强实例语义由 capability 门控制 — 未声明 strong 即拒绝,不降级。
- 生命周期转换(mark_hidden/mark_lost/recover_hidden)保留 §7.4 语义:
  HIDDEN 经显式 SHOW 事实恢复;LOST 一律拒绝。
- 事件丢失检测(capability coverage 未知/丢事件)→ 副作用前拒绝。

全部计数为本地 fake OS(生产接线检查用真实 backend 类反射,无 GUI)。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.unit

from target import product_policy
from target.product_profiles import get_capability

REAL_DIGEST = get_capability("windows_hisec", "windows_pywinauto").digest


@pytest.fixture(autouse=True)
def _clean_lease():
    product_policy.reset_for_tests()
    import target.server as server
    server._binding_facade_holder["facade"] = None
    yield
    product_policy.reset_for_tests()
    server._binding_facade_holder["facade"] = None


# ---- 生产 capability 接线:fail closed 而非伪造 ----

def test_real_backend_without_capability_fails_closed(monkeypatch):
    """生产 backend 的 capability 实装(0116 任务):中性自报
    (supported=False/coverage=unknown — 由本 run 跟踪事实决定);
    真实初始化入口无事件源订阅实现挂接(Windows 真实 hook 需实机
    验证)时,facade capability 仍 fail closed,bind 副作用前拒绝。
    """
    import target.server as server
    from target.automation.windows_pywinauto import WindowsPywinautoBackend
    # 实装面(0116 缺口1):两方法均存在。
    assert callable(getattr(WindowsPywinautoBackend,
                            "window_sequence_capability", None))
    assert callable(getattr(WindowsPywinautoBackend,
                            "lifecycle_event_stream", None))
    # 中性自报:backend 不自报 strong(跟踪事实才是权威)。
    backend = WindowsPywinautoBackend.__new__(WindowsPywinautoBackend)
    self_report = backend.window_sequence_capability()
    assert self_report.get("supported") is not True, (
        "backend 不得自报 strong(跟踪事实才是权威)")
    # 无事件源挂接(非 Windows/未 hook 实装)→ fail closed。
    monkeypatch.setattr(server, "_backend", backend, raising=False)
    monkeypatch.setattr(server, "_backend_kind", "windows_pywinauto")
    result = json.loads(server.begin_product_run(
        "windows_hisec", REAL_DIGEST, lease_seconds=300))
    assert result.get("ok") is True, result
    facade = server._binding_facade_holder.get("facade")
    assert facade is not None
    cap = facade.capability()
    assert cap["supported"] is not True, (
        f"生产能力未声明时必须 fail closed,实际 {cap}")
    assert cap["coverage"] == "unavailable"
    bind_result = json.loads(server.bind_entry_window(
        "main", dict(facade._declared.get("main", {})),
        result["run_token"]))
    assert bind_result.get("ok") is False, bind_result
    assert bind_result.get("code") == "window_sequence_capability_unavailable"
    try:
        product_policy.end_product_run(result["run_token"])
    except ValueError:
        pass


def test_capability_gate_covers_lost_events_and_reuse(monkeypatch):
    """丢事件/同 handle 复用等强实例语义由 capability 覆盖声明控制:
    coverage 非 strong 或 generation<=0 一律拒绝(不因 self-report
    supported=true 放行)— 已通过三门回归固化,此处核对照射点:
    未声明 strong 的生产初始化拒绝所有绑定。"""
    from test_case.test_recording.test_window_binding_server_wiring import (
        _FakeOSBackend, _real_begin,
    )
    import target.server as server
    for capability in ({"supported": True, "coverage": "event_lossy",
                        "generation": 3},
                       {"supported": True, "coverage": "strong",
                        "generation": -1}):
        backend = _FakeOSBackend({"100": {"process_id": 42,
                                          "title": "logo1",
                                          "root_control_id": "MainRoot"}},
                                 capability=capability)
        run = _real_begin(server, monkeypatch, backend)
        try:
            facade = server._binding_facade_holder.get("facade")
            assert facade.capability()["supported"] is not True, (
                f"{capability} 不得放行")
        finally:
            product_policy.end_product_run(run["run_token"])


def test_caller_show_dict_is_not_native_fact(monkeypatch):
    """recover_hidden 的 show_fact 是观测事实引用而非调用方自报原生
    事实:native/pid 不匹配一律拒绝(§7.4,不得把调用方字典当原生)。"""
    from test_case.test_recording.test_window_binding_facade import (
        _FakeOSBackend as _FacadeFakeOS,
        _facade as _facade_factory,
        _DECLARED,
    )
    from target import product_policy as policy
    from target.windowing.binding import WindowBindingRefusal
    policy.reset_for_tests()
    context = policy.begin_product_run("windows_hisec", REAL_DIGEST,
                                       lease_seconds=300)
    try:
        backend = _FacadeFakeOS(
            {k: dict(v) for k, v in
             {100: {"process_id": 42, "title": "logo1",
                    "root_control_id": "MainRoot"}}.items()})
        facade = _facade_factory(backend, context.run_token)
        first = facade.bind("W1", {**_DECLARED["W1"],
                                   "_recordedInstanceId": "RI-W1"})
        facade.mark_hidden("W1", first["binding_token"])
        # native_id 不匹配 → 拒绝(调用方字典不得指定别人的 HWND)。
        with pytest.raises(WindowBindingRefusal) as excinfo:
            facade.recover_hidden(
                "W1", first["binding_token"],
                show_fact={"native_id": 999, "process_id": 42,
                           "root_control_id": "MainRoot"})
        assert "different HWND" in str(excinfo.value)
        # owner 不匹配 → 拒绝。
        with pytest.raises(WindowBindingRefusal) as excinfo:
            facade.recover_hidden(
                "W1", first["binding_token"],
                show_fact={"native_id": 100, "process_id": 7,
                           "root_control_id": "MainRoot"})
        assert "owner differs" in str(excinfo.value)
    finally:
        policy.reset_for_tests()
