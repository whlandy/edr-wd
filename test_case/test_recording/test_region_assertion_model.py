"""Region / multi-component assertion drafts and session persistence (batch 1).

Covers the frozen RegionAssertionDraft v2 contract (06 design §3/§5): typed
validation, the shared Chinese summary, persistence stripping, and the
session's begin/add/cancel management path including its window identity
rules and requestId idempotency.
"""

from __future__ import annotations

import pytest

from target.recording.models import (
    ACTION_EVENT_TYPES,
    CaptureScope,
    ObservedTarget,
    RawCaptureEvent,
    RecordingModelError,
)
from target.recording.region_assertion import (
    RegionDraftError,
    build_region_draft,
    draft_for_persistence,
    evaluate_region_conditions,
    render_region_summary,
    time_recent_matches,
    validate_region_draft,
)
from target.recording.session import CaptureSessionState, RecordingSession

pytestmark = pytest.mark.unit

SCOPE = CaptureScope("target", "fake", "EDRClient.exe", "EDRClient")

WINDOW = {"processName": "EDRClient.exe", "title": "日志中心", "handle": 42, "pid": 7}

CONTAINER = {"controlType": "Table", "automationId": "logTable", "identifier": None, "name": None}

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
        "window": dict(WINDOW),
        "target_mode": "container_rows",
        "container": dict(CONTAINER),
        "quantifier": "any",
        "conditions": CONDITIONS,
        "observation": {"mode": "first_match", "deadlineSeconds": 60.0,
                        "start": "after_bound_action"},
        "binding": {"sequence": 2, "causalId": "CAUSE-scroll"},
    }
    payload.update(overrides)
    return build_region_draft(**payload)


def _click(sequence=1, causal_id=None):
    return RawCaptureEvent(
        sequence, "2026-10-25T00:00:00Z", sequence * 10, "pointer_click", SCOPE,
        {"button": "left"},
        ObservedTarget("OBS", "T0001", "sha256:x", "Button", "btnApply",
                       protected=False),
        evidence={"window": {"title": "日志中心", "processName": "EDRClient.exe",
                             "handle": 42, "rootAutomationId": "EdrMainWindow.logPane"}},
        causal_id=causal_id or f"CAUSE-{sequence}",
    )


# ---------------------------------------------------------------------------
# Draft validation (types the replay evaluator must understand)
# ---------------------------------------------------------------------------


def test_build_region_draft_produces_normalized_v2_with_summary():
    draft = _draft()
    assert draft["schemaVersion"] == 2
    assert draft["kind"] == "region_assertion"
    assert draft["target"]["container"]["automationId"] == "logTable"
    assert draft["contentTime"]["anchor"] == "each_check_now"
    assert draft["contentTime"]["recentSeconds"] == 5.0
    assert draft["observation"]["deadlineSeconds"] == 60.0
    assert draft["binding"] == {"sequence": 2, "causalId": "CAUSE-scroll"}
    # 06 §4 摘要口径：观察时长与内容时间阈值分别出现，不写死 10s。
    assert "最多观察60秒" in draft["summaryZh"]
    assert "最近5秒" in draft["summaryZh"]
    assert "任意一行" in draft["summaryZh"]
    assert render_region_summary(draft) == draft["summaryZh"]


def test_text_expectation_must_stay_a_string():
    with pytest.raises(RegionDraftError):
        _draft(conditions={"op": "all", "children": [
            {"field": "name", "operator": "equals", "expected": 123},
        ]})
    # 文本 123（字符串）合法：绝不静默转型。
    draft = _draft(conditions={"op": "all", "children": [
        {"field": "name", "operator": "equals", "expected": "123"},
    ]})
    assert draft["conditions"]["children"][0]["expected"] == "123"


def test_boolean_expectation_must_be_a_real_boolean():
    with pytest.raises(RegionDraftError):
        _draft(conditions={"op": "all", "children": [
            {"field": "checked", "operator": "bool_equals", "expected": "true"},
        ]})
    with pytest.raises(RegionDraftError):
        _draft(conditions={"op": "all", "children": [
            {"field": "checked", "operator": "bool_equals", "expected": 1},
        ]})


def test_count_expectation_must_be_an_integer_and_stay_at_root():
    with pytest.raises(RegionDraftError):
        _draft(conditions={"op": "all", "children": [
            {"operator": "count", "count": {"op": "at_least", "value": 2.5}},
        ]})
    with pytest.raises(RegionDraftError):
        # count 叶子只能挂在根上：子树里的 count 语义不明。
        _draft(conditions={"op": "all", "children": [
            {"field": "name", "operator": "contains", "expected": "x"},
            {"op": "all", "children": [
                {"operator": "count", "count": {"op": "at_least", "value": 1}},
            ]},
        ]})
    draft = _draft(conditions={"op": "all", "children": [
        {"field": "name", "operator": "contains", "expected": "x"},
        {"operator": "count", "count": {"op": "at_most", "value": 3}},
    ]})
    count_leaf = draft["conditions"]["children"][1]
    assert count_leaf["count"] == {"op": "at_most", "value": 3}
    assert count_leaf["field"] == "rows"


def test_count_condition_forces_quantifier_any():
    with pytest.raises(RegionDraftError):
        _draft(quantifier="all", conditions={"op": "all", "children": [
            {"operator": "count", "count": {"op": "equals", "value": 0}},
        ]})


def test_time_threshold_must_be_positive_and_bounded():
    with pytest.raises(RegionDraftError):
        _draft(content_time={"anchor": "each_check_now", "recentSeconds": 0,
                             "enabled": True})
    with pytest.raises(RegionDraftError):
        _draft(content_time={"anchor": "each_check_now", "recentSeconds": -5,
                             "enabled": True})
    with pytest.raises(RegionDraftError):
        _draft(content_time={"anchor": "each_check_now", "recentSeconds": 99999,
                             "enabled": True})


def test_time_recent_requires_enabled_content_time():
    with pytest.raises(RegionDraftError):
        _draft(content_time={"anchor": "each_check_now", "recentSeconds": 5.0,
                             "enabled": False})


def test_non_each_check_now_anchor_is_rejected_in_v2():
    with pytest.raises(RegionDraftError):
        _draft(content_time={"anchor": "action_time", "recentSeconds": 5.0,
                             "enabled": True})


def test_after_bound_action_requires_a_binding():
    with pytest.raises(RegionDraftError):
        _draft(binding=None,
               observation={"mode": "first_match", "deadlineSeconds": 60.0,
                            "start": "after_bound_action"})


def test_deadline_must_be_positive_and_bounded():
    with pytest.raises(RegionDraftError):
        _draft(observation={"mode": "first_match", "deadlineSeconds": 0,
                            "start": "immediate"})
    with pytest.raises(RegionDraftError):
        _draft(observation={"mode": "first_match", "deadlineSeconds": 301,
                            "start": "immediate"})


def test_condition_tree_rejects_unknown_fields_and_operators():
    with pytest.raises(RegionDraftError):
        _draft(conditions={"op": "all", "children": [
            {"field": "name", "operator": "fuzzy", "expected": "x"},
        ]})
    with pytest.raises(RegionDraftError):
        _draft(conditions={"op": "all", "children": [
            {"field": "name", "operator": "equals", "expected": "x", "weight": 2},
        ]})
    with pytest.raises(RegionDraftError):
        _draft(conditions={"op": "xor", "children": [
            {"field": "name", "operator": "equals", "expected": "x"},
        ]})


def test_fixed_components_same_field_distinct_identities_allowed():
    """P2d（审查复验）：同 field 不同身份=合法——组件唯一性由 identity
    校验保证，多组件可共享同一读取属性并分别评估；旧草稿（字段互异）
    仍兼容=宽松超集。"""
    payload = {
        "window": dict(WINDOW),
        "target_mode": "fixed_components",
        "container": None,
        "components": [
            {"field": "text", "identity": {"controlType": "Button", "name": "A"}},
            {"field": "text", "identity": {"controlType": "Button", "name": "B"}},
        ],
        "quantifier": "any",
        "conditions": {"op": "all", "children": [
            {"field": "text", "operator": "contains", "expected": "x"},
        ]},
        "observation": {"mode": "first_match", "deadlineSeconds": 60.0,
                        "start": "immediate"},
        "binding": None,
    }
    build_region_draft(**payload)  # 不抛=通过

def test_container_rows_mode_requires_a_stable_container_identity():
    with pytest.raises(RegionDraftError):
        _draft(container={"controlType": "Table", "name": None})


def test_draft_for_persistence_strips_window_and_ui_only_fields():
    draft = _draft()
    draft["observedAt"] = "2026-10-25T23:00:00+08:00"
    draft["readStatus"] = "ok"
    draft["readError"] = None
    persisted = draft_for_persistence(draft)
    assert "window" not in persisted
    assert "summaryZh" not in persisted
    assert "observedAt" not in persisted
    assert "readStatus" not in persisted
    # 语义字段完整保留，回放可以独立重建求值器输入。
    assert persisted["conditions"] == draft["conditions"]
    assert persisted["contentTime"] == draft["contentTime"]
    assert persisted["observation"] == draft["observation"]


# ---------------------------------------------------------------------------
# Shared time comparator (06 §4 semantics)
# ---------------------------------------------------------------------------


def _aware(minute=0, second=0):
    from datetime import datetime, timezone
    return datetime(2026, 10, 25, 12, minute, second, tzinfo=timezone.utc)


def test_time_recent_accepts_aware_wall_clock_inside_window():
    status, detail = time_recent_matches("2026-10-25T20:00:03+08:00", _aware(second=3), 5.0)
    assert status == "pass"
    assert detail["parsed"].endswith("+00:00")


def test_time_recent_recomputes_window_per_check():
    # 同一文本在两个检查时刻落在不同窗口：锚点是每次检查当下。
    ok_now, _ = time_recent_matches("2026-10-25T12:00:00+00:00", _aware(second=2), 5.0)
    later, _ = time_recent_matches("2026-10-25T12:00:00+00:00", _aware(minute=1), 5.0)
    assert ok_now == "pass"
    assert later == "fail"


def test_time_recent_rejects_future_without_implicit_tolerance():
    status, detail = time_recent_matches("2026-10-25T12:00:10+00:00", _aware(), 5.0)
    assert status == "fail"
    assert detail["reason"] == "future_time_rejected"


def test_time_recent_treats_naive_and_missing_as_undetermined():
    assert time_recent_matches("2026-10-25 12:00:00", _aware(), 5.0)[0] == "undetermined"
    assert time_recent_matches("12:00:00", _aware(), 5.0)[0] == "undetermined"
    assert time_recent_matches("not-a-time", _aware(), 5.0)[0] == "undetermined"
    assert time_recent_matches(None, _aware(), 5.0)[0] == "undetermined"


# ---------------------------------------------------------------------------
# Shared row-level conjunction evaluation
# ---------------------------------------------------------------------------


def test_second_row_match_passes_where_first_row_fails():
    now = _aware(second=3)
    rows = [
        {"rowIndex": 0, "name": "其他", "checked": True,
         "time": "2026-10-25T20:00:00+08:00"},   # 12:00:00Z — 窗口外
        {"rowIndex": 1, "name": "x目标内容y", "checked": True,
         "time": "2026-10-25T20:00:02+08:00"},   # 12:00:02Z — 窗口内
    ]
    outcome = evaluate_region_conditions(CONDITIONS, rows, now, 5.0, quantifier="any")
    assert outcome["result"] == "pass"
    assert outcome["matchCount"] == 1
    assert outcome["matchedRows"][0]["rowIndex"] == 1


def test_cross_row_splicing_never_passes():
    # 第一行名称满足、第二行状态/时间满足：没有任何一行同帧满足全部条件。
    now = _aware(second=3)
    rows = [
        {"rowIndex": 0, "name": "目标内容", "checked": False,
         "time": "2026-10-25T20:00:02+08:00"},
        {"rowIndex": 1, "name": "无关", "checked": True,
         "time": "2026-10-25T20:00:02+08:00"},
    ]
    outcome = evaluate_region_conditions(CONDITIONS, rows, now, 5.0, quantifier="any")
    assert outcome["result"] == "fail"
    assert outcome["matchCount"] == 0


def test_unreadable_value_is_undetermined_not_false():
    now = _aware(second=3)
    rows = [{"rowIndex": 0, "name": None, "checked": None,
             "time": "2026-10-25T20:00:02+08:00"}]
    outcome = evaluate_region_conditions(CONDITIONS, rows, now, 5.0, quantifier="any")
    assert outcome["result"] == "undetermined"
    row = outcome["rowEvidences"][0]
    assert row["conditions"][0]["result"] == "undetermined"
    assert row["conditions"][0]["reason"] == "value_unreadable"


def test_unreadable_container_is_undetermined_with_evidence():
    outcome = evaluate_region_conditions(
        CONDITIONS, {"ok": False, "error": "container_not_found"}, _aware(), 5.0)
    assert outcome["result"] == "undetermined"
    assert outcome["unreadable"] == "container_not_found"


def test_empty_readable_list_is_a_determinate_no_match():
    outcome = evaluate_region_conditions(CONDITIONS, [], _aware(), 5.0, quantifier="any")
    assert outcome["result"] == "fail"
    assert outcome["rowCount"] == 0


def test_quantifier_all_requires_every_accessible_row():
    now = _aware(second=3)
    rows = [
        {"rowIndex": 0, "name": "目标内容", "checked": True,
         "time": "2026-10-25T20:00:02+08:00"},
        {"rowIndex": 1, "name": "目标内容", "checked": False,
         "time": "2026-10-25T20:00:02+08:00"},
    ]
    all_conditions = {"op": "all", "children": [
        {"field": "name", "operator": "contains", "expected": "目标内容"},
    ]}
    # 第二行名称不同：quantifier=all 下不满足 → 失败；只看第一行 → 通过。
    rows[1]["name"] = "无关"
    assert evaluate_region_conditions(
        all_conditions, rows, now, 5.0, quantifier="all")["result"] == "fail"
    assert evaluate_region_conditions(
        all_conditions, rows[:1], now, 5.0, quantifier="all")["result"] == "pass"


def test_count_leaf_counts_rows_matching_the_predicate():
    now = _aware(second=3)
    rows = [
        {"rowIndex": 0, "name": "目标内容-1"},
        {"rowIndex": 1, "name": "目标内容-2"},
        {"rowIndex": 2, "name": "无关"},
    ]
    conditions = {"op": "all", "children": [
        {"field": "name", "operator": "contains", "expected": "目标内容"},
        {"operator": "count", "count": {"op": "at_least", "value": 2}},
    ]}
    assert evaluate_region_conditions(conditions, rows, now, 5.0)["result"] == "pass"
    conditions["children"][1]["count"]["value"] = 3
    assert evaluate_region_conditions(conditions, rows, now, 5.0)["result"] == "fail"


def test_count_only_condition_counts_all_accessible_rows():
    conditions = {"op": "all", "children": [
        {"operator": "count", "count": {"op": "equals", "value": 2}},
    ]}
    rows = [{"rowIndex": 0}, {"rowIndex": 1}]
    assert evaluate_region_conditions(conditions, rows, _aware(), 5.0)["result"] == "pass"


@pytest.mark.parametrize(("op", "threshold", "expected"), [
    ("at_most", 0, "undetermined"),
    ("at_most", 1, "pass"),
    ("at_least", 1, "undetermined"),
    ("at_least", 2, "fail"),
    ("equals", 0, "undetermined"),
    ("equals", 1, "undetermined"),
    ("equals", 2, "fail"),
])
def test_count_with_unreadable_row_uses_possible_match_bounds(op, threshold, expected):
    conditions = {"op": "all", "children": [
        {"field": "text", "operator": "equals", "expected": "X"},
        {"field": "rows", "operator": "count",
         "count": {"op": op, "value": threshold}},
    ]}
    result = evaluate_region_conditions(
        conditions, [{"values": {"text": None}}], _aware(), 5.0)
    assert result["rowEvidences"][0]["result"] == "undetermined"
    assert result["result"] == expected


def test_count_known_matches_can_decide_despite_unreadable_row():
    conditions = {"op": "all", "children": [
        {"field": "text", "operator": "equals", "expected": "X"},
        {"field": "rows", "operator": "count",
         "count": {"op": "at_most", "value": 0}},
    ]}
    rows = [{"values": {"text": "X"}}, {"values": {"text": None}}]
    assert evaluate_region_conditions(conditions, rows, _aware(), 5.0)["result"] == "fail"
    conditions["children"][1]["count"] = {"op": "at_least", "value": 1}
    assert evaluate_region_conditions(conditions, rows, _aware(), 5.0)["result"] == "pass"


# ---------------------------------------------------------------------------
# Session management path
# ---------------------------------------------------------------------------


def _recording_session_with_action():
    session = RecordingSession("REC", "flow", SCOPE)
    session.start()
    session.ingest(_click(1, "CAUSE-scroll"))
    return session


def test_begin_region_assertion_stamps_window_and_state():
    session = _recording_session_with_action()
    ack = session.begin_region_assertion()
    assert ack["ok"] is True and ack["state"] == "recording"
    # A 轨契约：返回体带 assertionWindow（与 window 同值）。
    assert ack["assertionWindow"]["title"] == "日志中心"
    assert ack["window"] == ack["assertionWindow"]
    assert session._assertion_window["title"] == "日志中心"


def test_begin_region_assertion_walks_back_over_stampless_transitions():
    # 实机缺陷回归（REC-c1721cc9903f4141be94473eeed48a90）：动作之后夹杂
    # 无窗口戳的 window_transition，_events[-1] 单点取样必然误拒 —— 现在
    # 从最新事件向历史回溯，取第一个带可识别窗口的事件。
    session = _recording_session_with_action()
    session.ingest(RawCaptureEvent(
        2, "2026-10-25T00:00:02Z", 30, "window_transition", SCOPE,
        {"kind": "hidden", "title": None, "processName": "EDRClient.exe"},
        evidence={},
    ))
    ack = session.begin_region_assertion()
    assert ack["ok"] is True
    assert ack["assertionWindow"]["title"] == "日志中心"
    assert ack["assertionWindow"]["handle"] == 42


def test_begin_region_assertion_prefers_explicit_stamp_over_history_walk():
    session = _recording_session_with_action()
    session.ingest(RawCaptureEvent(
        2, "2026-10-25T00:00:02Z", 30, "window_transition", SCOPE,
        {"kind": "hidden", "title": None, "processName": "EDRClient.exe"},
        evidence={},
    ))
    session._assertion_window = {"title": "显式窗口", "processName": "EDRClient.exe"}
    ack = session.begin_region_assertion()
    assert ack["assertionWindow"]["title"] == "显式窗口"


def test_begin_region_assertion_refuses_when_no_event_carries_a_window():
    session = RecordingSession("REC", "flow", SCOPE)
    session.start()
    session.ingest(RawCaptureEvent(
        1, "2026-10-25T00:00:00Z", 10, "window_transition", SCOPE,
        {"kind": "hidden", "title": None, "processName": "EDRClient.exe"},
        evidence={},
    ))
    with pytest.raises(RecordingModelError) as exc:
        session.begin_region_assertion()
    assert exc.value.code == "recording_assertion_window_unknown"


def test_begin_region_assertion_requires_an_active_session():
    session = RecordingSession("REC", "flow", SCOPE)
    with pytest.raises(RecordingModelError) as exc:
        session.begin_region_assertion()
    assert exc.value.code == "recording_state_invalid"


def test_add_region_assertion_persists_stripped_draft_event():
    session = _recording_session_with_action()
    session.begin_region_assertion()
    draft = _draft()
    saved = session.add_region_assertion(draft)
    assert saved["ok"] is True
    events = session.recording().events
    assert events[1].type == "assertion"
    assertion = events[1].assertion
    assert assertion["type"] == "region_query"
    assert assertion["timeoutSeconds"] == 60.0
    # draft 落盘去掉纯 UI 字段与窗口身份（窗口只在 evidence.window）。
    assert "window" not in assertion["draft"]
    assert "summaryZh" not in assertion["draft"]
    assert assertion["draft"]["conditions"] == draft["conditions"]
    # observed_target 承载容器稳定身份。
    assert events[1].observed_target.automation_id == "logTable"
    assert events[1].observed_target.control_type == "Table"
    # evidence.window 只留 title/processName/handle（wire 校验不含 pid）。
    assert events[1].evidence["window"]["handle"] == 42
    assert "pid" not in events[1].evidence["window"]
    # 显式绑定折叠进动作步。
    assert events[1].causal_id == "CAUSE-scroll"


def test_add_region_assertion_without_binding_is_an_independent_step():
    session = _recording_session_with_action()
    draft = _draft(binding=None,
                   observation={"mode": "first_match", "deadlineSeconds": 30.0,
                                "start": "immediate"})
    saved = session.add_region_assertion(draft)
    assert saved["ok"] is True
    assert session.recording().events[1].causal_id is None


def test_add_region_assertion_deduplicates_by_request_id():
    session = _recording_session_with_action()
    draft = _draft()
    first = session.add_region_assertion(draft)
    second = session.add_region_assertion(draft)
    assert second["duplicate"] is True
    assert second["sequence"] == first["sequence"]
    assert len(session.recording().events) == 2


def test_add_region_assertion_refuses_foreign_binding():
    session = RecordingSession("REC", "flow", SCOPE)
    session.start()
    with pytest.raises(RecordingModelError) as exc:
        session.add_region_assertion(_draft(binding={"sequence": 9, "causalId": "CAUSE-nope"}))
    assert exc.value.code == "recording_assertion_unbound"


def test_add_region_assertion_refuses_invalid_draft_without_appending():
    session = _recording_session_with_action()
    with pytest.raises(RecordingModelError) as exc:
        session.add_region_assertion({"draftId": "x", "schemaVersion": 2,
                                      "kind": "region_assertion"})
    assert exc.value.code == "recording_assertion_invalid"
    assert len(session.recording().events) == 1


def test_add_region_assertion_requires_active_session():
    session = RecordingSession("REC", "flow", SCOPE)
    with pytest.raises(RecordingModelError) as exc:
        session.add_region_assertion(_draft())
    assert exc.value.code == "recording_state_invalid"


def test_cancel_region_assertion_restores_window_without_event():
    session = _recording_session_with_action()
    before = session._assertion_window
    session.begin_region_assertion()
    assert session._assertion_window is not None
    result = session.cancel_region_assertion()
    assert result == {"ok": True, "state": "recording"}
    assert session._assertion_window == before
    assert len(session.recording().events) == 1  # 不落事件
    assert session.state is CaptureSessionState.RECORDING


def test_saved_region_assertion_survives_raw_wire_roundtrip():
    from target.recording.models import RawRecording

    session = _recording_session_with_action()
    session.add_region_assertion(_draft())
    raw = session.recording()
    document = RawRecording.from_dict(raw.to_dict())
    assertion = document.events[1].assertion
    assert assertion["type"] == "region_query"
    validate_region_draft({
        **assertion["draft"],
        "window": dict(WINDOW),
        "summaryZh": render_region_summary(assertion["draft"]),
    })


# ---------------------------------------------------------------------------
# A 轨契约对齐：浮窗编排 / rect 透传 / 最近动作候选
# ---------------------------------------------------------------------------


class _RegionIndicator:
    """带区域编排钩子的浮窗桩（生产侧由 A 轨 Tk 浮窗实现）。"""

    def __init__(self):
        self.events = []
    def start(self): pass
    def pause(self): pass
    def resume(self): pass
    def stop(self): pass
    def update_count(self, count): pass
    def open_assertion_editor(self, draft=None): pass
    def hide_for_region_selection(self):
        self.events.append("hide")
    def restore_after_region_selection(self):
        self.events.append("restore")


def test_region_session_hides_indicator_on_begin_and_restores_afterwards():
    indicator = _RegionIndicator()
    session = RecordingSession("REC", "flow", SCOPE)
    session.attach_indicator(indicator)
    session.start()
    session.ingest(_click(1, "CAUSE-scroll"))
    session.begin_region_assertion()
    assert indicator.events == ["hide"]
    session.cancel_region_assertion()
    assert indicator.events == ["hide", "restore"]


def test_saving_region_assertion_also_restores_the_indicator():
    indicator = _RegionIndicator()
    session = RecordingSession("REC", "flow", SCOPE)
    session.attach_indicator(indicator)
    session.start()
    session.ingest(_click(1, "CAUSE-scroll"))
    session.begin_region_assertion()
    session.add_region_assertion(_draft())
    assert indicator.events == ["hide", "restore"]


def test_control_inventory_entries_carry_rect_for_highlight():
    class _Source:
        control_snapshots = [{
            "snapshotId": "SNAP-1",
            "window": {"title": "日志中心", "processName": "EDRClient.exe"},
            "controls": [{
                "controlType": "Table", "automationId": "logTable",
                "name": "日志表格", "value": None,
                "rect": [12, 34, 560, 400],
            }],
        }]

    session = RecordingSession("REC", "flow", SCOPE, source=_Source())
    entries = session.control_inventory()
    assert len(entries) == 1
    # rect 只用于实机框选高亮；身份白名单仍不含 handle/pid/rect。
    assert entries[0]["rect"] == [12, 34, 560, 400]
    assert "rect" not in entries[0]["component"]
    assert "handle" not in entries[0]["window"] and "pid" not in entries[0]["window"]


def test_recent_actions_lists_recorded_actions_for_wizard_binding():
    session = RecordingSession("REC", "flow", SCOPE)
    session.start()
    session.ingest(_click(1, "CAUSE-scroll"))
    session.ingest(_click(2, "CAUSE-open"))
    actions = session.recent_actions(limit=2)
    assert [a["sequence"] for a in actions] == [2, 1]  # 最近优先
    assert actions[0]["causalId"] == "CAUSE-open"
    assert actions[0]["controlType"] == "Button"
    assert actions[0]["automationId"] == "btnApply"
    assert actions[0]["windowTitle"] == "日志中心"
    assert session.recent_actions(limit=5) == session.recent_actions()
    with pytest.raises(RecordingModelError):
        session.recent_actions(limit=0)


# ---------------------------------------------------------------------------
# 旧入口移除（06 §1：Ctrl+Alt+Q 是唯一断言入口）
# ---------------------------------------------------------------------------


def test_shift_ctrl_a_hotkey_no_longer_opens_the_editor():
    class _Indicator:
        def __init__(self):
            self.opened = []
            self.counts = []
        def start(self): pass
        def pause(self): pass
        def resume(self): pass
        def stop(self): pass
        def open_assertion_editor(self, draft=None):
            self.opened.append(draft)
        def update_count(self, count): self.counts.append(count)

    indicator = _Indicator()
    session = RecordingSession("REC", "flow", SCOPE)
    session.attach_indicator(indicator)
    session.start()
    accepted = session.ingest(RawCaptureEvent(
        1, "2026-10-25T00:00:00Z", 10, "key_command", SCOPE,
        {"key": "A", "modifiers": ["CTRL", "SHIFT"]},
    ))
    # 不再作为管理输入消费：按普通业务按键进入时间线，编辑器不被打开。
    assert accepted is True
    assert indicator.opened == []
    assert [e.type for e in session.recording().events] == ["key_command"]


def test_same_field_distinct_identity_evaluated_separately():
    """P2e 生产链（审查条款）：同 field 双身份分别评估——a.text=A 且
    b.text=B 同时满足=通过；对调值/缺失=失败；叶无 key 旧草稿=兼容。"""
    from datetime import datetime, timedelta, timezone
    from recording.region_assertion import evaluate_region_conditions
    now_wall = datetime(2026, 10, 27, 16, 0, 0, tzinfo=timezone.utc)
    recent = 5.0
    row = {"rowIndex": 0,
           "values": {"text": "B"},  # 旧通道（覆盖值）
           "componentValues": {"a": "A", "b": "B"}}
    def leaf(key, expected):
        return {"componentKey": key, "field": "text",
                "operator": "equals", "expected": expected}
    # a.text=A 且 b.text=B 同时满足 → pass（旧 values 覆盖不再生效）
    assert evaluate_region_conditions(
        {"op": "all", "children": [leaf("a", "A"), leaf("b", "B")]},
        [row], now_wall, recent)["result"] == "pass"
    # 对调值 → fail
    assert evaluate_region_conditions(
        {"op": "all", "children": [leaf("a", "B"), leaf("b", "A")]},
        [row], now_wall, recent)["result"] == "fail"
    # 缺失键 → fail（样本缺=不可判定按实现，至少不 pass）
    r = evaluate_region_conditions(
        {"op": "all", "children": [leaf("a", "A"), leaf("zz", "B")]},
        [row], now_wall, recent)
    assert r["result"] in ("fail", "undetermined")
    # 叶无 componentKey → 旧草稿回退 values[field]（兼容）
    legacy = [{"field": "text", "operator": "equals", "expected": "B"}]
    assert evaluate_region_conditions(
        {"op": "all", "children": legacy}, [row], now_wall, recent)["result"] == "pass"
    # 5 个同 text 组件分别断言
    row5 = {"rowIndex": 0, "values": {"text": "x"},
            "componentValues": {f"c{i}": f"v{i}" for i in range(5)}}
    leaves5 = [leaf(f"c{i}", f"v{i}") for i in range(5)]
    assert evaluate_region_conditions(
        {"op": "all", "children": leaves5},
        [row5], now_wall, recent)["result"] == "pass"


def test_component_key_survives_validation_pipeline():
    """P2f（审查实锤）：validate_condition_leaf 丢 componentKey=raw pass/
    validate 后 fail。回归：raw 叶→validate→evaluate 全程 key 保留。"""
    from recording.region_assertion import (
        evaluate_region_conditions, validate_condition_leaf)
    from datetime import datetime, timezone
    raw = {"componentKey": "a", "field": "text",
           "operator": "equals", "expected": "A"}
    norm = validate_condition_leaf(raw, "leaf")
    assert norm.get("componentKey") == "a"  # 归一化不丢
    row = {"rowIndex": 0, "values": {"text": "B"},
           "componentValues": {"a": "A"}}
    assert evaluate_region_conditions(
        {"op": "all", "children": [norm]}, [row],
        datetime(2026, 10, 27, 16, 0, tzinfo=timezone.utc), 5.0)["result"] == "pass"
    # 空串 key=拒绝
    from recording.region_assertion import RegionDraftError
    with pytest.raises(RegionDraftError):
        validate_condition_leaf(
            {"componentKey": " ", "field": "text",
             "operator": "equals", "expected": "A"}, "leaf")


def test_not_exists_missing_key_is_undetermined():
    """P2h（审查实锤）：缺失键/None 被 not_exists 误过=缺失身份不能误过。
    回归：componentValues 空/None → exists 与 not_exists 均 undetermined。"""
    from datetime import datetime, timezone
    from recording.region_assertion import evaluate_region_conditions
    now = datetime(2026, 10, 27, 17, 0, tzinfo=timezone.utc)
    def run(op, row):
        return evaluate_region_conditions(
            {"op": "all", "children": [
                {"componentKey": "a", "field": "text",
                 "operator": op, "expected": None}]},
            [row], now, 5.0)["result"]
    for row in ({"rowIndex": 0, "componentValues": {}},
                {"rowIndex": 0, "componentValues": {"a": None}}):
        assert run("not_exists", row) == "undetermined"
        assert run("exists", row) == "undetermined"
    # 嵌套叶 key 绑定校验（子树内错键=拒绝）
    from recording.region_assertion import build_region_draft
    with pytest.raises(Exception):
        build_region_draft(
            window=dict(WINDOW), target_mode="fixed_components",
            container=None,
            components=[{"key": "a", "field": "text",
                         "identity": {"controlType": "Button", "name": "A"}}],
            quantifier="any",
            conditions={"op": "all", "children": [
                {"op": "all", "children": [
                    {"componentKey": "zz", "field": "text",
                     "operator": "contains", "expected": "x"}]}]},
            observation={"mode": "first_match", "deadlineSeconds": 60.0,
                         "start": "immediate"},
            binding=None)
