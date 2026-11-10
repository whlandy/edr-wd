"""106/107 轮：方案 C 收窄版——进程级显式物理激活声明（真实方法回归）。

契约（协调者收窄 + 107 轮审查修正）：
- 配置 env `EDR_WD_PHYSICAL_ACTIVATION`：多条规则以 ';' 分隔；每条为
  `process_name|window_title|automation_id` 三段竖线分隔，三者必须同时
  精确匹配（归一：strip+casefold）。**通配符/suffix 无特殊语义：按字面
  字符参与精确匹配**（即配置 "Fixture*" 只匹配字面 "Fixture*"，不匹配
  "FixtureSave"）——收窄语义即"只认精确三元组"，本 docstring 即契约标注；
- 配置缺失/为空 → 默认行为零变化（语义激活照旧）；
- 配置格式非法 → PhysicalActivationConfigError，WindowsGUI.__init__ 启动即
  明确失败，不静默忽略；
- 命中控件跳过 UIA Invoke → 落既有控件定位 + pointer_guard + checked SendInput
  物理路径；原生输入提交失败如实失败（SendInput, ok=False），
  绝不以坐标 fallback 冒充通过。

测试策略（107 轮审查修正：真实 WindowsGUI 方法、零 xfail、mac 可执行）：
- 通过向 sys.modules 注入最小 pywinauto 桩模块（仅类型引用与 mouse 记录器），
  真实导入 target.pywinauto_client 并运行**真实** WindowsGUI.__init__ /
  _semantic_activate / click 方法体；psutil.Process 打桩返回可控进程名；
  _find_control 以实例属性桩返回受控控件（click 其余逻辑全部真实执行）。
  不操作真实 GUI、不部署产品、不重启。
- Part A（纯配置解析/匹配函数）保留为独立契约单测。
"""
from __future__ import annotations

import importlib
import sys
import types

import pytest

from target.automation.physical_activation import (
    PhysicalActivationConfigError,
    parse_physical_activation_spec,
    physical_activation_required,
)

ENV_VAR = "EDR_WD_PHYSICAL_ACTIVATION"
QA_TITLE = "EDR-WD Native Recording QA [qa-dsh-fixed-1025]"
QA_RULE = f"powershell.exe|{QA_TITLE}|FixtureSave"

# ---------------------------------------------------------------- Part A
# 纯配置解析/匹配（无 pywinauto 依赖，任何平台可执行）


def test_missing_config_means_no_rules_and_no_match():
    assert parse_physical_activation_spec(None) == ()
    assert parse_physical_activation_spec("") == ()
    assert parse_physical_activation_spec("   ") == ()
    assert physical_activation_required((), "powershell.exe", "Any", "FixtureSave") is False


def test_parse_single_rule_is_exact_triple():
    rules = parse_physical_activation_spec(QA_RULE)
    assert len(rules) == 1
    rule = rules[0]
    assert rule.process_name == "powershell.exe"
    assert rule.window_title == QA_TITLE
    assert rule.automation_id == "FixtureSave"


def test_parse_multiple_rules_semicolon_separated():
    rules = parse_physical_activation_spec(
        QA_RULE + ";explorer.exe|Task Manager|SomeId")
    assert len(rules) == 2
    assert rules[1].process_name == "explorer.exe"


def test_parse_invalid_config_fails_loudly():
    for bad in (
        "powershell.exe|EDR-WD Native Recording QA [qa-dsh-fixed-1025]",  # 缺 automation_id
        "powershell.exe||FixtureSave",                                    # 空 title
        "|EDR-WD Native Recording QA [qa-dsh-fixed-1025]|FixtureSave",     # 空 process
        "powershell.exe|EDR-WD Native Recording QA [qa-dsh-fixed-1025]|",  # 空 automation_id
        "a|b|c|d",                                                        # 段数过多
        "a|b|c;;|x|y",                                                    # 空规则段
    ):
        with pytest.raises(PhysicalActivationConfigError):
            parse_physical_activation_spec(bad)


def test_match_requires_all_three_fields():
    rules = parse_physical_activation_spec(QA_RULE)
    assert physical_activation_required(rules, "powershell.exe", QA_TITLE, "FixtureSave") is True
    # 大小写归一仍精确（仅大小写差异）
    assert physical_activation_required(rules, "PowerShell.EXE", QA_TITLE, "fixturesave") is True


def test_mismatched_process_is_negative():
    rules = parse_physical_activation_spec(QA_RULE)
    # 异进程 + 同 ID：仅 ID 全局匹配被禁止
    assert physical_activation_required(rules, "notepad.exe", QA_TITLE, "FixtureSave") is False


def test_mismatched_title_is_negative():
    rules = parse_physical_activation_spec(QA_RULE)
    assert physical_activation_required(
        rules, "powershell.exe", "EDR-WD Native Recording QA [other]", "FixtureSave") is False
    # 子串/前缀 title 不算完整匹配
    assert physical_activation_required(
        rules, "powershell.exe", "EDR-WD Native Recording QA", "FixtureSave") is False


def test_mismatched_automation_id_is_negative():
    rules = parse_physical_activation_spec(QA_RULE)
    assert physical_activation_required(
        rules, "powershell.exe", QA_TITLE, "FixtureOther") is False


def test_suffix_and_wildcards_have_no_special_meaning():
    rules = parse_physical_activation_spec("powershell.exe|T|Fix")
    # suffix 形式不匹配完整 ID
    assert physical_activation_required(rules, "powershell.exe", "T", "FixtureSave") is False
    # 通配符按字面字符处理
    rules_wild = parse_physical_activation_spec("powershell.exe|T|Fixture*")
    assert physical_activation_required(rules_wild, "powershell.exe", "T", "FixtureSave") is False
    assert physical_activation_required(rules_wild, "powershell.exe", "T", "Fixture*") is True


# ---------------------------------------------------------------- Part B
# 真实 WindowsGUI 方法回归（pywinauto 桩隔离 + psutil 受控桩；mac 可执行，
# Windows 上同一测试走真实依赖。零 xfail——产品失败不会被标记吞掉。）


class _FakeMouseModule(types.ModuleType):
    """记录坐标点击，供'零坐标动作'断言。"""

    def __init__(self):
        super().__init__("pywinauto.mouse")
        self.clicks: list[tuple] = []

    def click(self, button=None, coords=None):
        self.clicks.append((button, coords))


def _install_fake_pywinauto(monkeypatch):
    """注入最小 pywinauto 桩模块，使 target.pywinauto_client 可真实导入。"""
    pkg = types.ModuleType("pywinauto")
    pkg.Application = object
    pkg.timings = types.ModuleType("pywinauto.timings")
    fake_mouse = _FakeMouseModule()
    pkg.mouse = fake_mouse
    pkg.__path__ = []  # 标记为包，允许子模块导入
    monkeypatch.setitem(sys.modules, "pywinauto", pkg)
    monkeypatch.setitem(sys.modules, "pywinauto.mouse", fake_mouse)
    monkeypatch.delitem(sys.modules, "target.pywinauto_client", raising=False)  # 强制重导
    module = importlib.import_module("target.pywinauto_client")
    monkeypatch.setattr(module, "mouse", fake_mouse, raising=False)
    # The client now uses checked native submission, not click_input. Exercise
    # that real helper with a fake OS API (no Windows DLL on this host).
    from test_case.test_recording.test_native_pointer_submission import API
    from target.automation.native_pointer import send_left_click
    api = API(point=(100, 32))
    fake_mouse.native_api = api
    monkeypatch.setattr(module, "send_left_click", lambda x, y: send_left_click(
        x, y, api=api, sleep=lambda _: None, last_error=lambda: 5))
    return module, fake_mouse


class _FakeProcess:
    def __init__(self, name: str) -> None:
        self._name = name

    def name(self):
        return self._name


class _FakeMainWindow:
    def __init__(self, title: str, process_name: str) -> None:
        self._title = title
        self._process_name = process_name

    def window_text(self):
        return self._title

    def process_id(self):
        return 4242


class _FakeRect:
    """pywinauto RECT 风格：left/top 为属性，width/height 为方法。"""

    left = 20
    top = 20

    def width(self):
        return 160

    def height(self):
        return 24


class _FakeCtrl:
    """受控控件桩：Invoke/点击/矩形均可观测。"""

    def __init__(self, automation_id: str, *, click_fails: bool = False) -> None:
        self._aid = automation_id
        self._click_fails = click_fails
        self.invoked = False
        self.click_input_calls = 0

    def control_type(self):
        return "Button"

    def class_name(self):
        return "Button"

    def friendly_class_name(self):
        return "Button"  # 非 leaf text → 不触发 parent 重定向

    def automation_id(self):
        return self._aid

    def invoke(self):
        self.invoked = True

    def toggle(self):
        raise AssertionError("toggle 不应被调用")

    def rectangle(self):
        return _FakeRect()

    def click_input(self):
        self.click_input_calls += 1
        if self._click_fails:
            raise RuntimeError("simulated click_input failure")


@pytest.fixture
def real_gui(monkeypatch, request):
    """真实 WindowsGUI（真实 __init__ 解析 env）；返回 (gui, fake_mouse, guard_calls)。"""
    env_value = getattr(request, "param", None)
    module, fake_mouse = _install_fake_pywinauto(monkeypatch)
    if env_value is None:
        monkeypatch.delenv(ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(ENV_VAR, env_value)
    gui = module.WindowsGUI()
    monkeypatch.setattr("psutil.Process", lambda pid: _FakeProcess("powershell.exe"))
    gui.main_window = _FakeMainWindow(title=QA_TITLE, process_name="powershell.exe")
    guard_calls: list[tuple] = []

    def _guard(x, y):
        guard_calls.append((x, y))
        return None  # 放行

    gui.pointer_guard = _guard
    return gui, fake_mouse, guard_calls


@pytest.mark.parametrize("real_gui", [QA_RULE], indirect=True)
def test_matched_control_clicks_physically_zero_invoke(real_gui):
    gui, fake_mouse, guard_calls = real_gui
    ctrl = _FakeCtrl("FixtureSave")
    gui._find_control = lambda **kwargs: ctrl
    result = gui.click(automation_id="FixtureSave", control_type="Button")
    assert result["ok"] is True and result["method"] == "SendInput", result
    assert ctrl.invoked is False, "物理声明控件不得走 UIA Invoke"
    assert ctrl.click_input_calls == 0
    assert fake_mouse.native_api.flags == [2, 4]
    assert result['effect_verified'] is False
    assert fake_mouse.clicks == [], "物理点击不得有坐标动作"
    assert guard_calls, "pointer_guard 必须在物理路径上被调用"


@pytest.mark.parametrize("real_gui", [QA_RULE], indirect=True)
def test_guard_rejection_performs_zero_input(real_gui):
    gui, fake_mouse, guard_calls = real_gui
    ctrl = _FakeCtrl("FixtureSave")

    def _reject(x, y):
        guard_calls.append((x, y))
        return {"ok": False, "error": "occluded"}

    gui.pointer_guard = _reject
    gui._find_control = lambda **kwargs: ctrl
    result = gui.click(automation_id="FixtureSave", control_type="Button")
    assert result == {"ok": False, "error": "occluded"}
    assert ctrl.invoked is False, "guard 拒绝后不得有任何 Invoke"
    assert ctrl.click_input_calls == 0, "guard 拒绝后不得有任何点击"
    assert fake_mouse.clicks == [], "guard 拒绝后不得有任何坐标动作"
    assert fake_mouse.native_api.flags == []


@pytest.mark.parametrize("real_gui", [QA_RULE], indirect=True)
def test_click_input_failure_is_not_masked_by_coordinates(real_gui):
    gui, fake_mouse, guard_calls = real_gui
    ctrl = _FakeCtrl("FixtureSave", click_fails=True)
    fake_mouse.native_api.counts = iter((1, 0))
    gui._find_control = lambda **kwargs: ctrl
    result = gui.click(automation_id="FixtureSave", control_type="Button")
    assert result["ok"] is False, result
    assert result["method"] == "SendInput"
    assert result['event_dispatched'] is True and result['outcome'] == 'unknown'
    assert fake_mouse.clicks == [], "click_input 失败不得以坐标 fallback 冒充通过"
    assert ctrl.click_input_calls == 0
    assert fake_mouse.native_api.flags == [2, 4]
    assert guard_calls, "失败发生在 guard 坐标计算与原生提交之后"


@pytest.mark.parametrize("real_gui", [None], indirect=True)
def test_default_env_keeps_uia_invoke(real_gui):
    gui, fake_mouse, _ = real_gui
    ctrl = _FakeCtrl("FixtureSave")
    gui._find_control = lambda **kwargs: ctrl
    result = gui.click(automation_id="FixtureSave", control_type="Button")
    assert result["ok"] is True and result["method"] == "uia_invoke", result
    assert ctrl.invoked is True


@pytest.mark.parametrize("real_gui", [QA_RULE], indirect=True)
def test_other_title_keeps_uia_invoke(real_gui):
    gui, _, _ = real_gui
    gui.main_window = _FakeMainWindow(title="EDR-WD Native Recording QA [other]",
                                      process_name="powershell.exe")
    ctrl = _FakeCtrl("FixtureSave")
    gui._find_control = lambda **kwargs: ctrl
    result = gui.click(automation_id="FixtureSave", control_type="Button")
    assert result["ok"] is True and result["method"] == "uia_invoke", result
    assert ctrl.invoked is True


@pytest.mark.parametrize("real_gui", [QA_RULE], indirect=True)
def test_mismatched_automation_id_keeps_uia_invoke(real_gui):
    gui, _, _ = real_gui
    ctrl = _FakeCtrl("FixtureOther")
    gui._find_control = lambda **kwargs: ctrl
    result = gui.click(automation_id="FixtureOther", control_type="Button")
    assert result["ok"] is True and result["method"] == "uia_invoke", result
    assert ctrl.invoked is True


def test_invalid_config_fails_at_real_init(monkeypatch):
    """真实 __init__（不触 pywinauto 连接）对非法配置启动即明确失败。"""
    module, _ = _install_fake_pywinauto(monkeypatch)
    monkeypatch.setenv(ENV_VAR, "only-two|segments")
    # 用被测模块绑定的异常类：pywinauto_client 按远端布局以裸 `automation`
    # 包导入，与测试进程的 `target.automation` 可能是不同模块对象。
    with pytest.raises(module.PhysicalActivationConfigError):
        module.WindowsGUI()
