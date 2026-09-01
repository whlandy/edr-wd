"""导出 maa 节点表的自检。

这份产物的价值全在「自动发生」。要是靠人记得跑一条命令，忘了不会报错 ——
只会变成「maa-fw 那边加载了什么都不做」，两边都看不出来。

所以这里守两件事：转换器在时产物必须出现且动作没丢；转换器不在时**编译不能挂**。
"""

import json

import pytest

from agent.recording.maa_export import recorder_home, to_maa_nodes
from agent.recording.models import GOLDEN_TRACE_SCHEMA


def _golden():
    return {
        "schema": GOLDEN_TRACE_SCHEMA, "status": "ready", "name": "case",
        "sourceRecording": {}, "catalog": {}, "environment": {},
        "entry": "step-0001", "cleanup": [],
        "steps": {"step-0001": {
            "stepId": "step-0001", "actionId": "gui.click", "args": {},
            "selector": {"control": {"automationId": "Win.btn"}, "window": {},
                         "visual": None},
            "endSelector": None, "verifiers": [], "required": True,
            "status": "ready", "issues": [], "next": None}},
    }


@pytest.mark.skipif(recorder_home() is None, reason="本机没有 edr-cloud-recorder")
def test_click_survives_the_export():
    """转换后还得是「点一下」。变成 DoNothing 就是静默丢失 ——
    实测未导出时 maa-fw 加载 edr-wd 的原生轨迹就是全变 DoNothing。"""
    trace, note = to_maa_nodes(_golden())
    assert note == "ok"
    assert trace["step_0001"]["action"]["type"] == "Click"
    assert trace["step_0001"]["next"] == []


@pytest.mark.skipif(recorder_home() is None, reason="本机没有 edr-cloud-recorder")
def test_node_ids_avoid_characters_maa_fw_rewrites():
    trace, _ = to_maa_nodes(_golden())
    assert "step_0001" in trace and "step-0001" not in trace


def test_missing_recorder_is_not_a_failure(monkeypatch):
    """录制器不在本机是常态，不能因此让编译挂掉 —— 它是附加产物。"""
    monkeypatch.setenv("EDR_RECORDER_HOME", "/nonexistent")
    trace, note = to_maa_nodes(_golden())
    assert trace is None
    assert "EDR_RECORDER_HOME" in note


@pytest.mark.skipif(recorder_home() is None, reason="本机没有 edr-cloud-recorder")
def test_a_broken_golden_trace_is_reported_not_swallowed(monkeypatch):
    """轨迹本身的问题要说清楚，别和「环境没装」混成一句话。"""
    trace, note = to_maa_nodes({"schema": "something-else", "steps": {}})
    assert trace is None
    assert "转换失败" in note
