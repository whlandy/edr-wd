"""C11 (design 11 §9): 语言结果闭环——receipt ok 不等于语言业务成功。

结构菜单点击(menu-layout/v1) + 因果绑定 text_equals 语言结果断言在
v3 真实链(真实 server/binding/dispatcher,仅 OS fake)上的正负组合:

正例: 语言菜单点击恰一次注入(Idioma,当前语言项)、currentLanguage
控件实况变为 French → 严格 pass 推进。

负例(本片反例): 动作 receipt ok 但语言未变(currentLanguage 仍是
English)→ control_text_equals failed(expected=French, actual=
English)→ fail 锁存、下一步 0 输入——窗口存在/点击成功不代替业务
结果(§9 C11 行:"独立业务断言失败;窗口存在不代替结果")。

期望值来自录制时的显式声明(事件因果绑定的 assertion.expected),
不从回放实测输出学习。既有 checked 失败阻断语义
(test_business_verifier_false_fails_blocks_next_step)保持;本片锁定
的是语言菜单 + 语言结果断言这一具体正负组合此前无测试覆盖。
"""

from __future__ import annotations

import copy
import dataclasses
import json
import sys
from pathlib import Path

import pytest

from agent.recording.compiler import compile_recording
from agent.recording.models import GoldenTrace
from target.recording.models import (
    CaptureScope,
    ObservedTarget,
    RawCaptureEvent,
    RawRecording,
)

pytestmark = pytest.mark.unit

sys.path.insert(0, str(Path(__file__).parent))
import test_v3_real_server_adapter as adapter  # noqa: E402
import test_localized_menu_replay as menu_fixture  # noqa: E402

CHAIN = [{"controlType": "Window", "name": "EDRClient"},
         {"controlType": "Pane", "name": "Desktop 1"}]


def _language_recording(expected_text="French"):
    """语言菜单点击(弱身份 MenuItem,menu-layout/v1)+ 因果绑定的
    text_equals 断言(currentLanguage 强 ID 控件,期望 expected_text)。"""
    window = {"handle": 7, "title": "logo1",
              "processName": "EDRClient.exe",
              "instanceId": "RI-0001",
              "rootAutomationId": "EdrMainWindow"}
    lang_rect = [1000, 140, 1153, 172]
    click = RawCaptureEvent(
        sequence=1, wall_time="2026-11-06T00:00:00Z", monotonic_ms=200,
        type="pointer_click",
        input={"button": "left", "screenPoint": [1050, 155]},
        observed_target=ObservedTarget(
            "OLD-OBS", "OLD-TARGET", "old-fingerprint", control_type="MenuItem",
            automation_id=None, text="Language", rect=tuple(lang_rect),
            ancestry=tuple(copy.deepcopy(CHAIN))),
        scope=CaptureScope("t", "windows_pywinauto", "EDRClient.exe",
                           "^EDRClient$"),
        evidence={"window": window}, causal_id="C-LANG")
    assertion = RawCaptureEvent(
        sequence=2, wall_time="2026-11-06T00:00:01Z", monotonic_ms=400,
        type="assertion", input={},
        assertion={"type": "text_equals", "expected": expected_text,
                   "timeoutSeconds": 5.0},
        observed_target=ObservedTarget(
            "OLD-OBS2", "OLD-TARGET2", "old-fp2", control_type="Text",
            automation_id="currentLanguage", text=expected_text,
            rect=(100, 20, 220, 40)),
        scope=CaptureScope("t", "windows_pywinauto", "EDRClient.exe",
                           "^EDRClient$"),
        evidence={"window": window}, causal_id="C-LANG")
    frame = menu_fixture.nodes("Language")
    snapshot = {"snapshotId": "CS-1", "capturedAtMs": 190, "reason": "step",
                "window": window, "contentHash": "h", "stateHash": "h",
                "controlCount": len(frame), "complete": True,
                "controls": frame}
    return RawRecording(
        name="c11-language-result", session_id="s-c11",
        events=[click, assertion], control_snapshots=(snapshot,),
        windows=({"instanceId": "RI-0001", "handle": 7, "pid": 42,
                  "processName": "EDRClient.exe", "title": "logo1",
                  "origin": "already_open"},),
        schema="edr.desktop-recording/v2",
        window_timeline={
            "schema": "edr.recorded-window-timeline/v1",
            "events": ({"id": "WE1", "order": 1, "monotonicMs": 10,
                        "instanceId": "RI-0001", "kind": "initial_observed",
                        "source": "initial_inventory"},),
            "coverage": {"destroyEvents": "supported", "droppedEvents": 0}})


def _compiled_golden(expected_text="French"):
    result = compile_recording(
        _language_recording(expected_text), profile="windows_hisec")
    assert result.golden.status == "ready", [i.code for i in result.issues]
    golden = GoldenTrace.from_dict(result.golden.to_dict())
    step = golden.steps[0]
    assert step.selector.control.get("structural", {}).get(
        "kind") == "menu-layout/v1", "前置:结构菜单 selector"
    verifiers = [v for v in step.verifiers
                 if v.get("type") == "text_equals"]
    assert verifiers and verifiers[0]["expected"] == expected_text, \
        "前置:text_equals 断言因果折入菜单步骤"
    assert verifiers[0]["match"] == {"automation_id": "currentLanguage",
                                     "control_type": "Text"}, \
        "断言身份是断言控件自身的强 ID,不是动作 selector"
    return golden


def _fake_os_with_language(monkeypatch, language_text):
    """fake OS:菜单 3 项以平移后当前几何存在(Idioma),currentLanguage
    控件显示 language_text。仅 OS 事实层;链全真实。"""
    backend = adapter._fake_backend_for("same_window")
    rows = menu_fixture.nodes("Idioma", dx=-900, dy=100)
    dump = backend.dump_tree

    def translated_tree(*args, **kwargs):
        result = dump(*args, **kwargs)
        for i, row in enumerate(rows):
            left, top, right, bottom = row["rect"]
            result["controls"].append({
                "class_name": "MenuItem", "text": row["text"],
                "control_id": 20 + i, "automation_id": "",
                "rectangle": {"x": left, "y": top,
                              "w": right - left, "h": bottom - top},
                "is_visible": True, "is_enabled": True, "depth": 1,
                "ancestry": row["ancestry"]})
        result["controls"].append({
            "class_name": "Text", "text": language_text,
            "control_id": 30, "automation_id": "currentLanguage",
            "rectangle": {"x": 100, "y": 20, "w": 120, "h": 20},
            "is_visible": True, "is_enabled": True, "depth": 1})
        return result

    backend.dump_tree = translated_tree
    import target.server as server
    from target import product_policy
    from target.action_dispatcher import set_backend_resolver
    from types import SimpleNamespace
    product_policy.reset_for_tests()
    server._backend = product_policy.GuardedBackend(
        backend, "windows_pywinauto")
    server._backend_kind = "windows_pywinauto"
    server._recording_service = SimpleNamespace(
        status=lambda **kw: {"state": "idle"})
    set_backend_resolver(lambda: server._backend)

    def _restore():
        product_policy.reset_for_tests()
        server._binding_facade_holder["facade"] = None
        set_backend_resolver(lambda: None)

    return backend, _restore


def test_language_changed_after_menu_click_passes(monkeypatch, tmp_path):
    """正例:菜单点击恰一次注入当前语言项(Idioma),语言结果控件实况
    French == 期望 → 严格 pass 推进(latched=False)。"""
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    import target.server as server

    golden = _compiled_golden("French")
    backend, _restore = _fake_os_with_language(monkeypatch, "French")
    try:
        result = execute_v3_window_stage_real(
            golden=golden, ledger=AttemptLedger(tmp_path / "ev"),
            server_tools=server,
            window_role_map=adapter._SAME_WINDOW_ROLE_MAP)
        assert result["latched"] is False, result["steps"]
        assert result["dispatchInputs"] == 1, result["steps"]
        assert result["report"]["outcome"] == "pass", result["report"]
        assert len(backend.dispatch_calls) == 1
        assert backend.dispatch_calls[0]["text"] == "Idioma", \
            "注入的是当前语言菜单项,不是录制 Language"
    finally:
        _restore()


def test_receipt_ok_language_unchanged_fails_and_latches(monkeypatch, tmp_path):
    """反例(本片反例):receipt ok 但语言未变 → control_text_equals
    failed(expected=French, actual=English)→ fail 锁存、durable 断言
    引用记录真实差值;窗口存在不代替结果。"""
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    import target.server as server

    golden = _compiled_golden("French")
    backend, _restore = _fake_os_with_language(monkeypatch, "English")
    try:
        result = execute_v3_window_stage_real(
            golden=golden, ledger=AttemptLedger(tmp_path / "ev"),
            server_tools=server,
            window_role_map=adapter._SAME_WINDOW_ROLE_MAP)
        assert result["latched"] is True, result["steps"]
        assert result["dispatchInputs"] == 1, (
            "动作步恰一次真实输入,但 receipt ok 不推进")
        assert result["report"]["outcome"] == "fail", result["report"]
        refs = (result["steps"][0].get("assertionResultRefs") or [])
        text_results = [r for r in refs
                        if r.get("type") == "control_text_equals"]
        assert text_results, refs
        assert text_results[0]["result"] == "failed", text_results
        assert text_results[0]["expected"] == "French", text_results
        assert text_results[0]["actual"] == "English", text_results
        events = AttemptLedger(tmp_path / "ev").rebuild_history(
            run_id=result["runId"])
        assert [e["outcome"] for e in events] == ["fail"], events
        durable = [r for r in (events[0].get("assertionResultRefs") or [])
                   if r.get("type") == "control_text_equals"]
        assert durable and durable[0]["result"] == "fail", (
            "断言结果持久可重建(重启只读 rebuild 同判)")
        assert durable[0]["expected"] == "French"
        assert durable[0]["actual"] == "English"
    finally:
        _restore()


def test_expected_value_is_recorded_declaration_not_learned(monkeypatch,
                                                            tmp_path):
    """期望值来自录制声明:回放期 currentLanguage 实测是期望值本身
    (French)时 pass;实测非期望值(English)时 fail——同一份 golden、
    期望恒为录制期 French,不随实测输出改变(§9 C11 行:"不根据实测
    输出学习期望")。"""
    from agent.recording.execution_ledger import execute_v3_window_stage_real
    from target.windowing.attempt_ledger import AttemptLedger
    import target.server as server

    golden = _compiled_golden("French")
    backend, _restore = _fake_os_with_language(monkeypatch, "English")
    try:
        result = execute_v3_window_stage_real(
            golden=golden, ledger=AttemptLedger(tmp_path / "ev"),
            server_tools=server,
            window_role_map=adapter._SAME_WINDOW_ROLE_MAP)
        assert result["report"]["outcome"] == "fail"
        step_verifiers = golden.steps[0].verifiers
        assert all(v.get("expected") == "French"
                   for v in step_verifiers
                   if v.get("type") == "text_equals"), step_verifiers
    finally:
        _restore()
