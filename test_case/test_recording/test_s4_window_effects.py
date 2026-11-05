"""S4:appeared/absent 动作前后差分(09 §7.3)失败回归。

驱动真实 target.windowing.binding.WindowBindingFacade(fake OS 层):
- 动作前基线:租约内记录匹配候选与绑定基线(显式 baseline token);
- 动作只执行一次(本切片不负责动作,差分只读轮询由调用方驱动);
- already-visible 旧窗口不满足 appeared;
- 增量恰一个新候选才能绑定;多个新候选拒绝;零候选到期失败;
- HIDDEN 的既有绑定重新显示满足"同实例出现"(不沿用给新实例);
- absent:基线在而动作后消失;
- 差分只枚举(不为验证效果额外激活窗口)。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

from target import product_policy
from target.product_profiles import get_capability
from target.windowing.binding import WindowBindingFacade, WindowBindingRefusal

REAL_DIGEST = get_capability("windows_hisec", "windows_pywinauto").digest
_INSTALLED_PATH = ("C:\\Program Files\\HiSec-Endpoint\\core\\"
                   "EDRClient.exe")


class _EffectOSBackend:
    """替身 OS 事实层:支持窗口出现/消失(差分语义),无授权逻辑。"""

    def __init__(self, windows):
        self._windows = {str(k): dict(v) for k, v in (windows or {}).items()}
        self.focus_calls: list = []
        self.exe_path = _INSTALLED_PATH

    def exe_path_for_pid(self, pid):
        return self.exe_path

    def refresh_inventory(self):
        from types import MappingProxyType
        return [MappingProxyType({**w, "native_id": nid,
                                  "process_name": "EDRClient.exe",
                                  "lost": False,
                                  "generation": 2})
                for nid, w in self._windows.items()]

    def focus_existing_window(self, **kwargs):
        self.focus_calls.append(kwargs)
        return {"ok": True}

    def window_sequence_capability(self):
        return {"supported": True, "coverage": "strong", "generation": 2}

    # 差分测试驱动
    def add_window(self, native_id, window):
        self._windows[str(native_id)] = dict(window)

    def remove_window(self, native_id):
        self._windows.pop(str(native_id), None)


_MAIN = {"process_id": 42, "title": "logo1", "root_control_id": "MainRoot",
         "visible": True, "foreground": True}
_DIALOG = {"process_id": 42, "title": "Log Center",
           "root_control_id": "LogRoot", "visible": True,
           "foreground": False}
_DECLARED = {"W1": {"processName": "EDRClient.exe",
                    "rootAutomationId": "MainRoot"}}


@pytest.fixture()
def lease():
    product_policy.reset_for_tests()
    context = product_policy.begin_product_run("windows_hisec", REAL_DIGEST,
                                               lease_seconds=300)
    yield context
    product_policy.reset_for_tests()


def _facade(backend, run_token):
    from types import MappingProxyType
    session = type("S", (), {})()
    session.backend = backend
    session.refresh = lambda *a, **k: tuple(
        MappingProxyType(item) for item in backend.refresh_inventory())
    return WindowBindingFacade(
        session=session, policy=product_policy, lease_token=run_token,
        declared_windows=_DECLARED, tracked_generation=2, coverage="strong",
        installed_paths=(_INSTALLED_PATH,))


def test_appeared_new_instance_unique_diff_binds(lease):
    """appeared:基线后动作出现恰一个新候选 → 差分绑定成功,且新实例
    与 already-visible 旧窗口区分。"""
    backend = _EffectOSBackend({"100": dict(_MAIN)})
    facade = _facade(backend, lease.run_token)
    baseline = facade.begin_effect_baseline(
        "W-dialog", {"processName": "EDRClient.exe",
                     "titleRegex": "^Log Center$"})
    assert baseline.get("baseline_token")
    assert baseline.get("candidate_count") == 0
    # 动作(模拟)后:出现恰一个新窗口。
    backend.add_window(200, _DIALOG)
    result = facade.observe_effect_after_action(
        "W-dialog", baseline["baseline_token"],
        effect={"kind": "appeared"})
    assert result.get("ok") is True, result
    assert result.get("outcome") == "appeared"
    assert str(result.get("native_id")) == "200"


def test_appeared_already_visible_old_window_never_satisfies(lease):
    """already visible 的同名旧窗口不满足 appeared:基线已有候选时,
    无增量 → 到期失败,不得把旧窗口当 appeared。"""
    backend = _EffectOSBackend({"100": dict(_MAIN), "200": dict(_DIALOG)})
    facade = _facade(backend, lease.run_token)
    baseline = facade.begin_effect_baseline(
        "W-dialog", {"processName": "EDRClient.exe",
                     "titleRegex": "^Log Center$"})
    assert baseline.get("candidate_count") == 1
    result = facade.observe_effect_after_action(
        "W-dialog", baseline["baseline_token"],
        effect={"kind": "appeared"})
    assert result.get("ok") is False
    assert result.get("outcome") == "timeout", result


def test_appeared_multiple_new_candidates_refused(lease):
    """增量多候选拒绝:基线后同时出现两个新窗口 → 结构化拒绝,
    不任意挑选。"""
    backend = _EffectOSBackend({"100": dict(_MAIN)})
    facade = _facade(backend, lease.run_token)
    baseline = facade.begin_effect_baseline(
        "W-dialog", {"processName": "EDRClient.exe",
                     "titleRegex": "^Log Center$"})
    backend.add_window(200, dict(_DIALOG, title="Log Center"))
    backend.add_window(201, dict(_DIALOG, title="Log Center",
                                 process_id=43))
    result = facade.observe_effect_after_action(
        "W-dialog", baseline["baseline_token"],
        effect={"kind": "appeared"})
    assert result.get("ok") is False
    assert result.get("outcome") == "ambiguous", result


def test_appeared_hidden_same_instance_show_satisfies(lease):
    """HIDDEN 的既有绑定重新显示满足"同实例出现",且该 SHOW 不延展给
    新实例 W(§7.3:新实例 W 必须对应新本次实例)。"""
    backend = _EffectOSBackend({"100": dict(_MAIN)})
    facade = _facade(backend, lease.run_token)
    bound = facade.bind("W1", {**_DECLARED["W1"],
                               "_recordedInstanceId": "RI-W1"})
    facade.mark_hidden("W1", bound["binding_token"])
    # 基线期间窗口消失(隐藏),动作后重新显示(同 native)。
    backend.remove_window(100)
    hidden_baseline = facade.begin_effect_baseline(
        "W1", _DECLARED["W1"], binding_token=bound["binding_token"])
    assert hidden_baseline.get("candidate_count") == 0
    backend.add_window(100, _MAIN)
    result = facade.observe_effect_after_action(
        "W1", hidden_baseline["baseline_token"],
        effect={"kind": "appeared"}, binding_token=bound["binding_token"])
    assert result.get("ok") is True, result
    assert result.get("outcome") == "same_instance_shown"
    assert str(result.get("native_id")) == "100"


def test_absent_effect_detected(lease):
    """absent:基线在、动作后消失 → outcome=absent。"""
    backend = _EffectOSBackend({"100": dict(_MAIN), "200": dict(_DIALOG)})
    facade = _facade(backend, lease.run_token)
    baseline = facade.begin_effect_baseline(
        "W-dialog", {"processName": "EDRClient.exe",
                     "titleRegex": "^Log Center$"})
    assert baseline.get("candidate_count") == 1
    backend.remove_window(200)
    result = facade.observe_effect_after_action(
        "W-dialog", baseline["baseline_token"],
        effect={"kind": "absent"})
    assert result.get("ok") is True, result
    assert result.get("outcome") == "absent"


def test_effect_observation_is_read_only(lease):
    """差分只枚举:观察效果不得激活窗口(focus 0 次;§7.2 尾段)。"""
    backend = _EffectOSBackend({"100": dict(_MAIN)})
    facade = _facade(backend, lease.run_token)
    baseline = facade.begin_effect_baseline(
        "W-dialog", {"processName": "EDRClient.exe",
                     "titleRegex": "^Log Center$"})
    backend.add_window(200, _DIALOG)
    facade.observe_effect_after_action(
        "W-dialog", baseline["baseline_token"],
        effect={"kind": "appeared"})
    assert backend.focus_calls == [], (
        "效果观察是只读差分,不得激活窗口")


def test_baseline_without_lease_refused(lease):
    """基线与差分都在租约门禁内:无租约 → 拒绝。"""
    backend = _EffectOSBackend({"100": dict(_MAIN)})
    facade = _facade(backend, lease.run_token)
    product_policy.end_product_run(lease.run_token)
    with pytest.raises(WindowBindingRefusal) as excinfo:
        facade.begin_effect_baseline(
            "W-dialog", {"processName": "EDRClient.exe",
                         "titleRegex": "^Log Center$"})
    assert excinfo.value.code == "window_binding_lease_refused"
