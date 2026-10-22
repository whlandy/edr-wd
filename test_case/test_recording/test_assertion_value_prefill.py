"""`record assert` 断言取值预填（选组件→抓内容→再设断言）的回归测试。

对应 design/assertion-value-prefill-design.md：
- ObservedTarget 增可选 `value`（组件当前内容），缺省 None；
- 命中测试（windows._observed）把 value 带进 ObservedTarget；
- 打开编辑器时传给 UI 的 default dict 含 value；
- 编辑器把 value 预填进 expected（可改）；读不到 value 时仍为空（不回归）。
"""

import pytest

from target.recording.models import ObservedTarget
from target.recording.windows import WindowsUIACorrelator
from target.recording.session import _assertion_default
from target.recording.indicator import _expected_default


# ── ObservedTarget 字段 ───────────────────────────────────────────


def test_observed_target_value_defaults_to_none():
    t = ObservedTarget("OBS", "T0001", "sha256:x", control_type="Edit",
                       text="name-field")
    assert t.value is None
    assert t.text == "name-field"


def test_observed_target_carries_value():
    t = ObservedTarget("OBS", "T0001", "sha256:x", control_type="Edit",
                       text="name-field", value="张三")
    assert t.value == "张三"


# ── 命中测试把当前内容带进 ObservedTarget ──────────────────────────
# Windows 与 macOS（MacOSAXCorrelator 复用同一路径）都走 windows._observed。


def test_windows_observed_carries_value_from_target_data():
    observed = WindowsUIACorrelator._observed({
        "controlType": "Edit", "automationId": "field", "text": "姓名", "value": "张三",
    })
    assert observed.value == "张三"
    assert observed.text == "姓名"


def test_windows_observed_value_none_when_absent():
    observed = WindowsUIACorrelator._observed({
        "controlType": "Button", "text": "OK",
    })
    assert observed.value is None


# ── 打开编辑器时传给指示器的 default dict 带 value ──────────────────


def test_assertion_default_includes_value_when_present():
    target = ObservedTarget("OBS", "T0001", "sha256:x", control_type="Edit",
                            text="姓名", value="张三")
    default = _assertion_default(target)
    assert default["value"] == "张三"
    assert default["name"] == "姓名"
    assert default["controlType"] == "Edit"


def test_assertion_default_value_none_when_absent():
    target = ObservedTarget("OBS", "T0001", "sha256:x", control_type="Button",
                            text="OK")
    assert _assertion_default(target).get("value") is None


def test_assertion_default_handles_none_target():
    assert _assertion_default(None) == {}


# ── 编辑器把 value 预填进 expected（可改），读不到则仍空 ─────────────


def test_expected_prefill_uses_captured_value():
    assert _expected_default({"value": "张三"}) == "张三"


def test_expected_prefill_empty_when_no_value():
    assert _expected_default({"name": "OK"}) == ""
    assert _expected_default({}) == ""
