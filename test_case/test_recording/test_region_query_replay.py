"""Region query 编译 → golden → 回放期望 → 求值器（batch 1）。

覆盖冻结契约的回放半边：region_query 断言事件编译为带容器稳定 match 的
verifier（无稳定身份拒绝编译、绝不降级坐标）、golden 往返、
_verifier_expectation 新分支，以及求值器的单调观察期/同帧行级合取/
内容时间窗口语义（stub provider，参照既有 expectation 测试风格）。
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agent.execution.expectations import (
    EXPECTATION_REGISTRY,
    evaluate_region_query,
    region_clock,
    region_row_provider,
)
from agent.execution.models import StepStatus
from agent.recording.compiler import compile_recording
from agent.recording.models import GoldenTrace
from agent.recording.replay import golden_to_test_case
from target.protocol_models import Expectation
from target.recording.models import RawRecording
from target.recording.region_assertion import (
    build_region_draft,
    draft_for_persistence,
)

from test_phase0_recording import _event, _recording

pytestmark = pytest.mark.unit

CONDITIONS = {
    "op": "all",
    "children": [
        {"field": "name", "operator": "contains", "expected": "目标内容"},
        {"field": "checked", "operator": "bool_equals", "expected": True},
        {"field": "time", "operator": "time_recent"},
    ],
}


def _draft(**overrides):
    payload = {
        "window": {"processName": "EDRClient.exe", "title": "日志中心",
                   "handle": 42, "pid": 7},
        "target_mode": "container_rows",
        "container": {"controlType": "Table", "automationId": "logTable",
                      "identifier": None, "name": None},
        "quantifier": "any",
        "conditions": CONDITIONS,
        "observation": {"mode": "first_match", "deadlineSeconds": 60.0,
                        "start": "after_bound_action"},
        "binding": {"sequence": 2, "causalId": "cause-scroll"},
    }
    payload.update(overrides)
    return build_region_draft(**payload)


RECORDED_WINDOW = {"title": "日志中心", "processName": "EDRClient.exe",
                   "handle": 42}


def _region_assertion_event(sequence, *, causal_id=None, container=None,
                            observation=None, conditions=CONDITIONS,
                            quantifier="any", binding="auto"):
    draft = _draft(
        container=container if container is not None else
        {"controlType": "Table", "automationId": "logTable"},
        observation=observation or
        {"mode": "first_match", "deadlineSeconds": 60.0, "start": "after_bound_action"},
        conditions=conditions,
        quantifier=quantifier,
        binding=({"sequence": max(sequence - 1, 1),
                  "causalId": causal_id or "cause-scroll"}
                 if binding == "auto" else binding),
    )
    persisted = draft_for_persistence(draft)
    identity = draft["target"]["container"]
    observed_target = {
        "snapshotId": f"OBS-{sequence}", "targetId": f"T{sequence:04d}",
        "fingerprint": "sha256:container", "controlType": identity.get("controlType"),
        "automationId": identity.get("automationId"),
        "identifier": identity.get("identifier"), "text": identity.get("name"),
        "protected": False,
    }
    return _event(
        sequence, "assertion",
        assertion={"type": "region_query", "draft": persisted,
                   "timeoutSeconds": draft["observation"]["deadlineSeconds"]},
        causal_id=causal_id or "cause-scroll",
    ) | {"observedTarget": observed_target,
         "evidence": {"window": dict(RECORDED_WINDOW)}}


# ---------------------------------------------------------------------------
# 编译：region_query → verifier（match=容器稳定身份）
# ---------------------------------------------------------------------------


def test_bound_region_assertion_compiles_into_action_verifier():
    scroll = _event(1, "scroll_commit", causal_id="cause-scroll")
    scroll["evidence"] = {"window": dict(RECORDED_WINDOW)}
    events = [
        scroll,
        _region_assertion_event(2, causal_id="cause-scroll"),
    ]
    result = compile_recording(_recording(*events))
    assert result.golden.status == "ready", [i.code for i in result.issues]
    step = result.golden.steps[0]
    verifier = step.verifiers[0]
    assert verifier["type"] == "region_query"
    # 容器身份走既有 expectation_match 机制：稳定字段，无坐标/handle/pid。
    assert verifier["match"] == {"automation_id": "logTable", "control_type": "Table"}
    assert verifier["quantifier"] == "any"
    assert verifier["conditions"] == CONDITIONS
    assert verifier["contentTime"]["recentSeconds"] == 5.0
    assert verifier["observation"]["mode"] == "first_match"
    assert verifier["timeoutSeconds"] == 60.0
    assert "expected" not in verifier


def test_unbound_region_assertion_keeps_its_own_observe_step():
    event = _region_assertion_event(
        1, causal_id=None, binding=None,
        observation={"mode": "first_match", "deadlineSeconds": 30.0,
                     "start": "immediate"},
    )
    event["causalId"] = None
    result = compile_recording(_recording(event))
    observe_step = result.golden.steps[0]
    assert observe_step.action_id is None
    assert observe_step.verifiers[0]["type"] == "region_query"
    assert observe_step.verifiers[0]["timeoutSeconds"] == 30.0
    # 窗口身份进 selector.window（titleRegex 回退），坐标不出现。
    window = observe_step.selector.window
    assert window["processName"] == "EDRClient.exe"


def test_region_container_without_stable_identity_refuses_to_compile():
    # 会话/线格式校验都会先拒绝无稳定身份的容器；这里直接测编译器的纵深
    # 防御（外部工具拼装 raw 时不得降级为坐标）。
    from agent.recording.compiler import _REGION_CONTAINER_ISSUE, _region_query_verifier
    from agent.recording.selectors import assertion_match
    from target.recording.models import CaptureScope, ObservedTarget, RawCaptureEvent

    persisted = draft_for_persistence(_draft())
    base = RawCaptureEvent(
        sequence=1, wall_time="2026-10-25T00:00:00Z", monotonic_ms=10,
        type="assertion", scope=CaptureScope("win", "windows_pywinauto",
                                             "EDRClient.exe", "EDRClient"),
        input={}, observed_target=ObservedTarget(
            snapshot_id="OBS-1", target_id="T0001", fingerprint="sha256:c",
            control_type="Pane", automation_id=None, text=None, protected=False,
        ),
        evidence={"window": {"title": "日志中心", "processName": "EDRClient.exe"}},
        assertion={"type": "region_query", "draft": persisted,
                   "timeoutSeconds": 60.0},
        causal_id="cause-scroll",
    )
    # 绕过线格式校验模拟外部写入：容器身份只有 controlType。
    event = object.__new__(RawCaptureEvent)
    for key, value in base.__dict__.items():
        object.__setattr__(event, key, value)
    object.__setattr__(event, "assertion", {
        "type": "region_query",
        "draft": {"target": {"mode": "container_rows",
                             "container": {"controlType": "Pane"}}},
        "timeoutSeconds": 60.0,
    })
    assert assertion_match(event) is None
    assert _region_query_verifier(event) is None
    assert _REGION_CONTAINER_ISSUE[0] == "compile_region_container_unresolved"


def test_region_golden_roundtrip_and_replay_expectation():
    events = [
        _event(1, "scroll_commit", causal_id="cause-scroll"),
        _region_assertion_event(2, causal_id="cause-scroll"),
    ]
    golden_dict = compile_recording(_recording(*events)).golden.to_dict()
    golden = GoldenTrace.from_dict(golden_dict)  # agent 侧校验放行 region_query
    case = golden_to_test_case(golden)
    expectations = [
        expectation for step in case.steps for expectation in step.expectations
    ]
    region = [e for e in expectations if e.type == "region_query"]
    assert len(region) == 1
    value = region[0].value
    assert set(value) == {"match", "target", "quantifier", "conditions",
                          "contentTime", "observation"}
    assert value["match"] == {"automation_id": "logTable", "control_type": "Table"}
    assert region[0].timeout_seconds == 60.0


def test_old_goldens_without_region_query_keep_their_expectations():
    events = [_event(1, "text_commit", input_={"value": "ok"}),
              _event(2, "assertion",
                     assertion={"type": "text_contains", "expected": "ok",
                                "timeoutSeconds": 5.0},
                     causal_id="cause-1")]
    golden = compile_recording(_recording(*events)).golden
    case = golden_to_test_case(GoldenTrace.from_dict(golden.to_dict()))
    types = [e.type for step in case.steps for e in step.expectations]
    # 旧 verifier 类型映射不受 region_query 增量影响。
    assert "action_ok" in types and "control_text_contains" in types
    assert "region_query" not in types


# ---------------------------------------------------------------------------
# 求值器：stub provider + 固定时区时钟（离线语义闭环）
# ---------------------------------------------------------------------------


class RowProvider:
    """按次返回预置行快照的 stub provider（记录每次查询参数）。"""

    def __init__(self, samples):
        self.samples = list(samples)
        self.calls = []

    def query_container_rows(self, match, fields):
        self.calls.append((dict(match), tuple(fields)))
        if len(self.samples) > 1:
            return self.samples.pop(0)
        return self.samples[0]


_AWARE_NOW = datetime(2026, 10, 25, 12, 0, 3, tzinfo=timezone.utc)

_IN_WINDOW_TIME = "2026-10-25T20:00:02+08:00"   # == 12:00:02Z，窗口内
_OUT_WINDOW_TIME = "2026-10-25T11:00:02+00:00"  # 窗口外


def _row(index, name="目标内容-1", checked=True, time=_IN_WINDOW_TIME):
    return {"rowIndex": index, "name": name, "checked": checked, "time": time}


def _expectation(value, timeout=1.0):
    return Expectation(type="region_query", value=value, timeout_seconds=timeout)


def _region_value(**overrides):
    value = {
        "match": {"automation_id": "logTable", "control_type": "Table"},
        "target": {"mode": "container_rows", "components": []},
        "quantifier": "any",
        "conditions": CONDITIONS,
        "contentTime": {"anchor": "each_check_now", "recentSeconds": 5.0,
                        "enabled": True},
        "observation": {"mode": "first_match", "deadlineSeconds": 1.0,
                        "start": "after_bound_action"},
    }
    value.update(overrides)
    return value


def _evaluate(value, observation=None, timeout=1.0):
    with region_clock(lambda: _AWARE_NOW):
        return evaluate_region_query(
            None, _expectation(value, timeout), observation or {}, None)


def test_fixed_component_query_evaluates_the_provider_sample_as_one_row():
    class ComponentProvider:
        def read_component_values(self, components):
            return {"ok": True, "values": [
                {"key": "a", "field": "text", "value": "A"},
                {"key": "b", "field": "text", "value": "B"},
            ]}

    value = _region_value(
        target={"mode": "fixed_components", "components": [
            {"key": "a", "field": "text", "identity": {"automationId": "one"}},
            {"key": "b", "field": "text", "identity": {"automationId": "two"}},
        ]},
        conditions={"op": "all", "children": [
            {"componentKey": "a", "field": "text", "operator": "equals", "expected": "A"},
            {"componentKey": "b", "field": "text", "operator": "equals", "expected": "B"},
        ]},
    )
    with region_row_provider(ComponentProvider()):
        result = _evaluate(value)
    assert result.status is StepStatus.PASSED
    assert result.actual["samples"][-1]["matchedRows"][0]["rowIndex"] == 0


def test_fixed_component_query_preserves_provider_failure_as_undetermined():
    class FailedProvider:
        def read_component_values(self, components):
            return {"ok": False, "error": "window_lock_required"}

    value = _region_value(
        target={"mode": "fixed_components", "components": []},
        conditions={"op": "all", "children": [
            {"field": "text", "operator": "equals", "expected": "A"},
        ]},
    )
    with region_row_provider(FailedProvider()):
        result = _evaluate(value, timeout=0.01)
    assert result.status is StepStatus.FAILED
    assert result.actual["samples"][-1]["unreadable"] == "window_lock_required"


def test_registry_and_first_match_on_second_row():
    assert EXPECTATION_REGISTRY["region_query"] is evaluate_region_query
    provider = RowProvider([{"ok": True, "rows": [
        _row(0, name="其他"),
        _row(1, name="x目标内容y"),
    ]}])
    with region_row_provider(provider):
        result = _evaluate(_region_value())
    assert result.status is StepStatus.PASSED
    assert result.diagnostic == ""
    samples = result.actual["samples"]
    assert samples[-1]["matchCount"] == 1
    assert samples[-1]["matchedRows"][0]["rowIndex"] == 1
    # provider 收到容器 match 与条件所需字段。
    match, fields = provider.calls[0]
    assert match == {"automation_id": "logTable", "control_type": "Table"}
    assert set(fields) == {"checked", "name", "time"}


def test_first_match_passes_on_a_later_sample_not_the_first_frame():
    provider = RowProvider([
        {"ok": True, "rows": [_row(0, name="其他")]},
        {"ok": True, "rows": [_row(0, name="其他")]},
        {"ok": True, "rows": [_row(0, name="x目标内容y")]},
    ])
    with region_row_provider(provider):
        result = _evaluate(_region_value(), timeout=5.0)
    assert result.status is StepStatus.PASSED
    assert len(provider.calls) == 3


def test_first_match_fails_after_deadline_without_match():
    provider = RowProvider([{"ok": True, "rows": [_row(0, name="其他")]}])
    with region_row_provider(provider):
        result = _evaluate(_region_value(timeout=1.0), timeout=1.0)
    assert result.status is StepStatus.FAILED
    assert result.diagnostic == "region_query_not_satisfied"
    assert result.actual["sampling"] == "provider"


def test_cross_row_splicing_fails_through_the_evaluator():
    provider = RowProvider([{"ok": True, "rows": [
        _row(0, name="目标内容", checked=False),
        _row(1, name="无关", checked=True),
    ]}])
    with region_row_provider(provider):
        result = _evaluate(_region_value())
    assert result.status is StepStatus.FAILED
    assert result.diagnostic == "region_query_not_satisfied"


def test_absent_full_period_passes_only_at_deadline():
    provider = RowProvider([{"ok": True, "rows": [_row(0, name="其他")]}])
    with region_row_provider(provider):
        result = _evaluate(_region_value(
            observation={"mode": "absent_full_period", "deadlineSeconds": 1.0,
                         "start": "after_bound_action"}))
    assert result.status is StepStatus.PASSED
    assert len(provider.calls) >= 2       # 不允许首帧通过
    assert result.actual["compactSamples"][-1]["result"] == "fail"


def test_absent_full_period_fails_when_a_sample_matches():
    provider = RowProvider([
        {"ok": True, "rows": [_row(0, name="其他")]},
        {"ok": True, "rows": [_row(0, name="x目标内容y")]},
    ])
    with region_row_provider(provider):
        result = _evaluate(_region_value(
            observation={"mode": "absent_full_period", "deadlineSeconds": 1.0,
                         "start": "after_bound_action"}), timeout=2.0)
    assert result.status is StepStatus.FAILED
    assert result.diagnostic == "region_query_matched_during_absent_window"


def test_absent_full_period_with_unreadable_samples_is_undetermined():
    provider = RowProvider([{"ok": False, "error": "container_not_found"}])
    with region_row_provider(provider):
        result = _evaluate(_region_value(
            observation={"mode": "absent_full_period", "deadlineSeconds": 1.0,
                         "start": "after_bound_action"}))
    assert result.status is StepStatus.FAILED
    assert result.diagnostic == "region_query_undetermined"
    assert result.actual["reason"] == "undetermined_samples"


def test_unreadable_container_is_undetermined_never_false():
    provider = RowProvider([{"ok": False, "error": "container_not_found"}])
    with region_row_provider(provider):
        result = _evaluate(_region_value(timeout=1.0))
    assert result.status is StepStatus.FAILED
    assert result.diagnostic == "region_query_undetermined"
    assert result.actual["compactSamples"][0]["unreadable"] == "container_not_found"


def test_quantifier_all_through_the_evaluator():
    provider = RowProvider([{"ok": True, "rows": [
        _row(0), _row(1, name="无关"),
    ]}])
    with region_row_provider(provider):
        result = _evaluate(_region_value(quantifier="all", timeout=1.0))
    assert result.status is StepStatus.FAILED
    provider2 = RowProvider([{"ok": True, "rows": [_row(0), _row(1)]}])
    with region_row_provider(provider2):
        passed = _evaluate(_region_value(quantifier="all", timeout=1.0))
    assert passed.status is StepStatus.PASSED


def test_count_leaf_through_the_evaluator():
    conditions = {"op": "all", "children": [
        {"field": "name", "operator": "contains", "expected": "目标内容"},
        {"operator": "count", "count": {"op": "at_least", "value": 2}},
    ]}
    provider = RowProvider([{"ok": True, "rows": [
        _row(0), _row(1, name="目标内容-2"), _row(2, name="无关"),
    ]}])
    with region_row_provider(provider):
        result = _evaluate(_region_value(conditions=conditions, timeout=1.0))
    assert result.status is StepStatus.PASSED
    assert result.actual["samples"][-1]["matchCount"] == 2


def test_content_time_window_is_recomputed_against_the_evaluation_clock():
    provider = RowProvider([{"ok": True, "rows": [
        _row(0, time=_OUT_WINDOW_TIME),
        _row(1, time=_IN_WINDOW_TIME),
    ]}])
    with region_row_provider(provider):
        result = _evaluate(_region_value(timeout=1.0))
    assert result.status is StepStatus.PASSED
    # 时间解析失败 → 该条件不可判定 → 无匹配且不可判定。
    provider2 = RowProvider([{"ok": True, "rows": [
        _row(0, name="x目标内容y", time="2026-10-25 20:00:02"),  # 无时区
    ]}])
    with region_row_provider(provider2):
        undetermined = _evaluate(_region_value(timeout=1.0))
    assert undetermined.status is StepStatus.FAILED
    assert undetermined.diagnostic == "region_query_undetermined"
    # 未来时间被拒绝（无隐式时钟容差）。
    provider3 = RowProvider([{"ok": True, "rows": [
        _row(0, name="x目标内容y", time="2026-10-25T20:00:10+08:00"),
    ]}])
    with region_row_provider(provider3):
        future = _evaluate(_region_value(timeout=1.0))
    assert future.status is StepStatus.FAILED
    row_conditions = future.actual["samples"][-1]["rowEvidences"][0]["conditions"]
    assert row_conditions[-1]["reason"] == "future_time_rejected"


def test_naive_evaluation_clock_is_rejected_not_silently_accepted():
    from datetime import timezone as _tz
    naive = datetime(2026, 10, 25, 12, 0, 3)  # 无时区：内容时间不可判定
    with region_clock(lambda: naive.replace(tzinfo=None)):
        result = evaluate_region_query(
            None, _expectation(_region_value()), {}, None)
    assert result.status is StepStatus.FAILED
    assert result.diagnostic.startswith("region_query_invalid")


_NO_TIME_CONDITIONS = {"op": "all", "children": [
    {"field": "name", "operator": "contains", "expected": "目标内容"},
    {"field": "checked", "operator": "bool_equals", "expected": True},
]}


def test_no_provider_falls_back_to_one_observation_snapshot():
    # 单快照回退没有独立的时间单元格：时间条件应通过 provider 采样路径验证。
    observation = {"controls": [
        {"automation_id": "logTable", "control_type": "Table", "text": "",
         "ancestry": []},
        {"automation_id": "", "control_type": "ListItem", "text": "其他",
         "checked": False, "enabled": True, "value": None,
         "ancestry": [{"automationId": "logTable", "controlType": "Table"}]},
        {"automation_id": "", "control_type": "ListItem", "text": "x目标内容y",
         "checked": True, "enabled": True, "value": None,
         "ancestry": [{"automationId": "logTable", "controlType": "Table"}]},
    ]}
    with region_clock(lambda: _AWARE_NOW):
        result = evaluate_region_query(
            None, _expectation(_region_value(conditions=_NO_TIME_CONDITIONS,
                                             timeout=1.0)),
            observation, None)
    assert result.status is StepStatus.PASSED
    assert result.actual["sampling"] == "single_observation_snapshot"
    assert result.actual["singleSample"] is True
    assert result.actual["samples"][-1]["matchedRows"][0]["rowIndex"] == 1


def test_absent_mode_without_provider_reports_undetermined():
    observation = {"controls": []}
    with region_clock(lambda: _AWARE_NOW):
        result = evaluate_region_query(
            None,
            _expectation(_region_value(conditions=_NO_TIME_CONDITIONS,
                                       observation={
                "mode": "absent_full_period", "deadlineSeconds": 1.0,
                "start": "after_bound_action"})),
            observation, None)
    assert result.status is StepStatus.FAILED
    assert result.diagnostic == "region_query_undetermined"
    assert result.actual["reason"] == "single_snapshot_cannot_prove_full_period_absence"


# ---------------------------------------------------------------------------
# 执行器接线：provider 从 observation provider 暴露给求值器
# ---------------------------------------------------------------------------


def test_executor_wires_row_provider_for_region_expectations():
    from agent.execution import AtomicExecutor, CaseOutcome, ExecutorConfig
    from target.protocol_models import AtomicTestStep, TestCase

    class ObservingBackend:
        def __init__(self):
            self.calls = []
        def dispatch(self, action_id, args, **kwargs):
            self.calls.append(action_id)
            from target.action_dispatcher.receipts import ActionReceipt
            return ActionReceipt(code="ok", ok=True, action_id=action_id,
                                 message="ok")

    class ObservingProvider:
        def __init__(self):
            self.queries = []
        def latest_snapshot_id(self):
            return "snap-1"
        def get_snapshot(self, snapshot_id):
            return {"snapshot_id": snapshot_id, "windows": [], "controls": []}
        def refresh(self):
            return "snap-1"
        def query_container_rows(self, match, fields):
            self.queries.append((dict(match), tuple(fields)))
            return {"ok": True, "rows": [_row(0, name="x目标内容y")]}

    backend, provider = ObservingBackend(), ObservingProvider()
    executor = AtomicExecutor(
        dispatch=backend.dispatch, observation_provider=provider,
        config=ExecutorConfig(step_timeout_seconds=5.0),
    )
    step = AtomicTestStep(
        step_id="S001", step_no=1, action_id="observe.assert", args={},
        expectations=(
            Expectation(type="region_query", value=_region_value(),
                        timeout_seconds=2.0),
        ),
    )
    with region_clock(lambda: _AWARE_NOW):
        result = executor.run_case(TestCase(case_id="TC-R", title="region",
                                            steps=(step,), timeout_seconds=30))
    assert result.outcome is CaseOutcome.PASSED
    assert provider.queries and provider.queries[0][0] == {
        "automation_id": "logTable", "control_type": "Table"}
