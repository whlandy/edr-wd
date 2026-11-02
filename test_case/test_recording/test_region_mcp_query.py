# 区域断言 MCP 通路离线测试（batch 2）。
#
# 覆盖：target/server.py 的 observe_container_rows / observe_component_values
# （窗口锁链、字段白名单、脱敏边界、稳定身份服务端再拒一层）与
# MCPObservationProvider.query_container_rows / read_component_values
# （match→稳定身份翻译、run_token 透传、稳定错误码），以及
# evaluate_region_query ← provider ← call_tool 的端到端离线回路。
# 真机联调（pywinauto 实读）留给部署阶段——见 batch1 报告 §4。
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agent.execution.expectations import (
    evaluate_region_query, region_clock, region_row_provider,
)
from agent.execution.models import StepStatus
from agent.recording.mcp_runtime import MCPObservationProvider
from target.protocol_models import Expectation
from target import server as target_server

pytestmark = pytest.mark.unit

_AWARE_NOW = datetime(2026, 10, 25, 12, 0, 3, tzinfo=timezone.utc)
_IN_WINDOW_TIME = "2026-10-25T20:00:02+08:00"


# ---------------------------------------------------------------------------
# Server-side: observe_container_rows / observe_component_values
# ---------------------------------------------------------------------------


class _RowsBackend:
    """观察后端桩：verify_window_lock + 行/组件查询，记录收到的实参。"""

    def __init__(self, *, locked=True, rows=None, error=None,
                 component_error=None, values=None):
        self.locked = locked
        self.rows = rows if rows is not None else []
        self.error = error
        self.component_error = component_error
        self.values = values if values is not None else []
        self.row_calls: list[tuple[dict, list]] = []
        self.value_calls: list[list] = []

    def verify_window_lock(self, activate=False):
        return {"ok": self.locked, "locked": self.locked}

    def query_container_rows(self, container, fields):
        self.row_calls.append((dict(container), list(fields)))
        if self.error:
            return {"ok": False, "error": self.error}
        return {"ok": True, "rows": [dict(r) for r in self.rows],
                "container": dict(container)}

    def read_component_values(self, components):
        self.value_calls.append([dict(c) for c in components])
        if self.component_error:
            return {"ok": False, "error": self.component_error}
        return {"ok": True, "values": [dict(v) for v in self.values]}


def _rows_response(backend, **kwargs):
    return json.loads(target_server.observe_container_rows(**kwargs))


def _values_response(backend, **kwargs):
    return json.loads(target_server.observe_component_values(**kwargs))


def test_container_rows_without_window_lock_are_refused(monkeypatch):
    # 沿锁链（connect→lock_window→verify_window_lock）是前置条件：
    # 没有验证通过的锁就没有行查询，绝不绕锁。
    backend = _RowsBackend(locked=False, rows=[{"rowIndex": 0, "name": "x"}])
    monkeypatch.setattr(target_server, "_backend", backend)
    response = _rows_response(backend, container={"automationId": "logTable"})
    assert response["ok"] is False
    assert response["code"] == "window_lock_required"
    assert backend.row_calls == []  # 拒绝发生在读之前


def test_container_rows_happy_path_passes_whitelisted_fields(monkeypatch):
    backend = _RowsBackend(rows=[
        {"rowIndex": 0, "name": "目标内容", "text": "其他", "value": None,
         "checked": True, "enabled": True, "time": _IN_WINDOW_TIME,
         "controlType": "ListItem"},
    ])
    monkeypatch.setattr(target_server, "_backend", backend)
    response = _rows_response(
        backend,
        container={"control_type": "Table", "automation_id": "logTable"},
        fields=["name", "checked", "time"],
    )
    assert response["ok"] is True
    assert response["sampling"] == "locked_window_rows"
    assert response["rows"][0]["name"] == "目标内容"
    assert response["rows"][0]["checked"] is True
    assert response["rows"][0]["time"] == _IN_WINDOW_TIME
    # snake_case 入参统一为 draft 身份拼写；白名单字段原样下达。
    assert backend.row_calls[0][0] == {
        "controlType": "Table", "automationId": "logTable",
        "identifier": None, "name": None,
    }
    assert backend.row_calls[0][1] == ["name", "checked", "time"]


def test_container_rows_field_whitelist_and_identity_gate(monkeypatch):
    backend = _RowsBackend()
    monkeypatch.setattr(target_server, "_backend", backend)
    # 白名单外字段拒绝（不做任意读取）。
    response = _rows_response(
        backend, container={"automationId": "logTable"}, fields=["handle"])
    assert response["code"] == "field_not_supported"
    # 只有 controlType 无 name：编译侧已挡，服务端再挡一层。
    response = _rows_response(backend, container={"controlType": "Pane"})
    assert response["code"] == "container_identity_unstable"
    assert backend.row_calls == []


def test_container_rows_backend_refusal_keeps_stable_code(monkeypatch):
    backend = _RowsBackend(error="control_not_found")
    monkeypatch.setattr(target_server, "_backend", backend)
    response = _rows_response(backend, container={"automationId": "logTable"})
    assert response["ok"] is False
    assert response["code"] == "control_not_found"
    # 不支持该能力的后端：稳定码，不回传原始句子。
    backend2 = _RowsBackend(error="query_container_rows not supported here")
    monkeypatch.setattr(target_server, "_backend", backend2)
    response = _rows_response(backend2, container={"automationId": "logTable"})
    assert response["code"] == "region_query_unsupported"


def test_container_rows_redact_protected_values(monkeypatch):
    backend = _RowsBackend(rows=[
        {"rowIndex": 0, "name": "密码框", "text": "secret", "value": "secret",
         "checked": None, "enabled": True, "time": None, "protected": True},
    ])
    monkeypatch.setattr(target_server, "_backend", backend)
    response = _rows_response(backend, container={"automationId": "pwdTable"})
    row = response["rows"][0]
    assert row["protected"] is True
    assert row["value"] is None and row["text"] is None
    assert row["name"] == "密码框" and row["enabled"] is True


class _WindowSession:
    def __init__(self, binding="b-1", native_id="4", generation=1):
        self.binding = binding
        self.native_id = native_id
        self.generation = generation
        self.calls = 0

    def verify_locked(self):
        self.calls += 1
        return SimpleNamespace(
            binding=self.binding, native_id=self.native_id,
            generation=self.generation, rect=(0, 0, 10, 10))


def test_container_rows_product_run_rechecks_registry(monkeypatch):
    backend = _RowsBackend(rows=[{"rowIndex": 0, "name": "r"}])
    session = _WindowSession()
    context = SimpleNamespace(run_id="run-1")
    monkeypatch.setattr(target_server, "_backend", backend)
    monkeypatch.setattr(target_server, "_window_session", session)
    monkeypatch.setattr(
        target_server._product_policy, "require_product_run",
        lambda token: context if token == "tok-1" else None,
    )
    response = _rows_response(backend, run_token="tok-1",
                              container={"automationId": "logTable"})
    assert response["ok"] is True
    assert session.calls == 2  # 查询前后各一次同窗口复核

    # 行查询期间窗口漂移：整包作废。
    backend2 = _RowsBackend(rows=[{"rowIndex": 0, "name": "r"}])
    session2 = _WindowSession()
    state = {"count": 0}

    def drift():
        state["count"] += 1
        return SimpleNamespace(
            binding=f"b-{state['count']}", native_id="4", generation=1,
            rect=(0, 0, 10, 10))

    session2.verify_locked = drift
    monkeypatch.setattr(target_server, "_backend", backend2)
    monkeypatch.setattr(target_server, "_window_session", session2)
    response = _rows_response(backend2, run_token="tok-1",
                              container={"automationId": "logTable"})
    assert response["code"] == "observation_window_changed"


def test_container_rows_product_run_without_session_is_stale(monkeypatch):
    backend = _RowsBackend()
    monkeypatch.setattr(target_server, "_backend", backend)
    monkeypatch.setattr(target_server, "_window_session", None)
    monkeypatch.setattr(
        target_server._product_policy, "require_product_run",
        lambda token: SimpleNamespace(run_id="run-1"),
    )
    response = _rows_response(backend, run_token="tok-1",
                              container={"automationId": "logTable"})
    assert response["code"] == "window_identity_stale"


def test_component_values_read_and_redact(monkeypatch):
    backend = _RowsBackend(values=[
        {"field": "text", "value": "10:10"},
        {"field": "value", "value": "secret", "protected": True},
        {"field": "name", "value": None, "reason": "control_not_found"},
    ])
    monkeypatch.setattr(target_server, "_backend", backend)
    response = _values_response(backend, components=[
        {"field": "text", "identity": {"automationId": "logTime"}},
        {"field": "value", "identity": {"automationId": "pwdBox"}},
        {"field": "name", "identity": {"automationId": "missing"}},
    ])
    assert response["ok"] is True
    assert response["sampling"] == "locked_window_values"
    assert response["values"][0] == {"field": "text", "value": "10:10"}
    assert response["values"][1] == {"field": "value", "value": None,
                                     "protected": True}
    assert response["values"][2]["reason"] == "control_not_found"
    # 下达到后端的是稳定身份规格。
    assert backend.value_calls[0][0]["identity"] == {
        "controlType": None, "automationId": "logTime",
        "identifier": None, "name": None,
    }
    # 身份不稳的组件在服务端被拒。
    response = _values_response(backend, components=[
        {"field": "name", "identity": {"controlType": "Edit"}}])
    assert response["code"] == "component_identity_unstable"


def test_keyed_component_values_cross_the_real_server_boundary(monkeypatch):
    specs = [
        {"key": "a", "field": "text", "identity": {"automationId": "one"}},
        {"key": "b", "field": "text", "identity": {"automationId": "two"}},
    ]
    backend = _RowsBackend(values=[
        {"key": "a", "field": "text", "value": "A"},
        {"key": "b", "field": "text", "value": "secret", "protected": True},
    ])
    monkeypatch.setattr(target_server, "_backend", backend)
    response = _values_response(backend, components=specs)
    assert response["ok"] is True
    assert [item["key"] for item in backend.value_calls[0]] == ["a", "b"]
    assert response["values"] == [
        {"key": "a", "field": "text", "value": "A"},
        {"key": "b", "field": "text", "value": None, "protected": True},
    ]


@pytest.mark.parametrize("values", [
    [{"key": "a", "field": "text", "value": "A"}],
    [{"key": "a", "field": "text", "value": "A"},
     {"key": "a", "field": "text", "value": "B"}],
    [{"key": "a", "field": "text", "value": "A"},
     {"key": "wrong", "field": "text", "value": "B"}],
    [{"key": "a", "field": "text", "value": "A"},
     {"key": "b", "field": "name", "value": "B"}],
])
def test_keyed_component_values_reject_bad_backend_responses(monkeypatch, values):
    backend = _RowsBackend(values=values)
    monkeypatch.setattr(target_server, "_backend", backend)
    response = _values_response(backend, components=[
        {"key": "a", "field": "text", "identity": {"automationId": "one"}},
        {"key": "b", "field": "text", "identity": {"automationId": "two"}},
    ])
    assert response["ok"] is False
    assert response["code"] == "component_values_key_mismatch"
    assert "values" not in response


def test_component_values_backend_refusal(monkeypatch):
    backend = _RowsBackend(component_error="read_component_values failed: x")
    monkeypatch.setattr(target_server, "_backend", backend)
    response = _values_response(backend, components=[
        {"field": "name", "identity": {"automationId": "logTime"}}])
    assert response["ok"] is False
    assert response["code"] == "read_component_values failed"


# ---------------------------------------------------------------------------
# Provider-side: MCPObservationProvider
# ---------------------------------------------------------------------------


class _MCPAgent:
    """call_tool 桩：记录实参，按脚本返回。"""

    def __init__(self, responses=None):
        self.calls: list[tuple[str, dict]] = []
        self.responses = responses or {}

    def call_tool(self, name, arguments, timeout=None):
        self.calls.append((name, dict(arguments or {})))
        return self.responses[name]


def test_provider_query_container_rows_translates_match_identity():
    agent = _MCPAgent({"observe_container_rows": {
        "ok": True, "rows": [{"rowIndex": 0, "name": "目标内容"}],
        "container": {"automationId": "logTable"},
    }})
    provider = MCPObservationProvider(agent, run_token="tok-1")
    rows = provider.query_container_rows(
        {"automation_id": "logTable", "control_type": "Table"},
        ["name", "checked"],
    )
    assert rows["ok"] is True
    assert rows["rows"] == [{"rowIndex": 0, "name": "目标内容"}]
    name, payload = agent.calls[0]
    assert name == "observe_container_rows"
    assert payload["container"] == {
        "controlType": "Table", "automationId": "logTable",
        "identifier": None, "name": None,
    }
    assert payload["fields"] == ["name", "checked"]
    assert payload["run_token"] == "tok-1"


def test_provider_maps_tool_refusal_to_unreadable_payload():
    agent = _MCPAgent({"observe_container_rows": {
        "ok": False, "code": "window_lock_required", "error": "window_lock_required",
    }})
    provider = MCPObservationProvider(agent)
    rows = provider.query_container_rows({"automation_id": "logTable"})
    assert rows == {"ok": False, "error": "window_lock_required"}
    # legacy 会话不带 run_token。
    assert "run_token" not in agent.calls[0][1]


def test_provider_rejects_malformed_rows_payload():
    agent = _MCPAgent({"observe_container_rows": {"ok": True, "rows": "nope"}})
    provider = MCPObservationProvider(agent)
    rows = provider.query_container_rows({"automation_id": "logTable"})
    assert rows == {"ok": False, "error": "container_rows_payload_invalid"}


def test_provider_read_component_values_strips_ui_extras():
    agent = _MCPAgent({"observe_component_values": {
        "ok": True, "values": [{"field": "text", "value": "10:10"}],
    }})
    provider = MCPObservationProvider(agent, run_token="tok-1")
    values = provider.read_component_values([
        {"field": "text", "identity": {"automationId": "logTime"},
         "sampleValue": "10:10"},
        "junk",
    ])
    assert values["ok"] is True
    assert agent.calls[0][1]["components"] == [
        {"field": "text", "identity": {"automationId": "logTime"}}]


# ---------------------------------------------------------------------------
# End-to-end offline: evaluate_region_query ← provider ← call_tool
# ---------------------------------------------------------------------------


_CONDITIONS = {"op": "all", "children": [
    {"field": "name", "operator": "contains", "expected": "目标内容"},
    {"field": "checked", "operator": "bool_equals", "expected": True},
    {"field": "time", "operator": "time_recent"},
]}


def _region_value(observation=None, deadline=1.0):
    return {
        "match": {"automation_id": "logTable", "control_type": "Table"},
        "target": {"mode": "container_rows", "components": []},
        "quantifier": "any",
        "conditions": _CONDITIONS,
        "contentTime": {"anchor": "each_check_now", "recentSeconds": 5.0,
                        "enabled": True},
        "observation": observation or {
            "mode": "first_match", "deadlineSeconds": deadline,
            "start": "after_bound_action"},
    }


def _evaluate(value, observation=None, timeout=1.0):
    with region_clock(lambda: _AWARE_NOW):
        return evaluate_region_query(
            None, Expectation(type="region_query", value=value,
                              timeout_seconds=timeout),
            observation or {}, None)


def test_region_query_passes_through_mcp_rows():
    agent = _MCPAgent({"observe_container_rows": {
        "ok": True, "rows": [
            {"rowIndex": 0, "name": "无关", "checked": False,
             "time": _IN_WINDOW_TIME},
            {"rowIndex": 1, "name": "x目标内容y", "checked": True,
             "time": _IN_WINDOW_TIME},
        ],
    }})
    provider = MCPObservationProvider(agent, run_token="tok-1")
    with region_row_provider(provider):
        result = _evaluate(_region_value(deadline=1.0))
    assert result.status is StepStatus.PASSED


def test_fixed_component_query_runs_through_server_and_mcp_provider(monkeypatch):
    backend = _RowsBackend(values=[
        {"key": "a", "field": "text", "value": "A"},
        {"key": "b", "field": "text", "value": "B"},
    ])
    monkeypatch.setattr(target_server, "_backend", backend)

    class LocalMCP:
        def call_tool(self, name, arguments, timeout=None):
            assert name == "observe_component_values"
            return json.loads(target_server.observe_component_values(**arguments))

    provider = MCPObservationProvider(LocalMCP())
    value = _region_value()
    value["target"] = {"mode": "fixed_components", "components": [
        {"key": "a", "field": "text", "identity": {"automationId": "one"}},
        {"key": "b", "field": "text", "identity": {"automationId": "two"}},
    ]}
    value["conditions"] = {"op": "all", "children": [
        {"componentKey": "a", "field": "text", "operator": "equals", "expected": "A"},
        {"componentKey": "b", "field": "text", "operator": "equals", "expected": "B"},
    ]}
    with region_row_provider(provider):
        result = _evaluate(value)
    assert result.status is StepStatus.PASSED
    assert result.actual["samples"][-1]["matchedRows"][0]["rowSnapshot"]["componentValues"] == {
        "a": "A", "b": "B"}

    # The same two values on opposite components must not satisfy the query.
    backend.values[0]["value"], backend.values[1]["value"] = "B", "A"
    with region_row_provider(provider):
        mismatch = _evaluate(value, timeout=0.01)
    assert mismatch.status is StepStatus.FAILED
    assert result.actual["sampling"] == "provider"
    assert result.actual["samples"][-1]["matchCount"] == 1


def test_region_query_mcp_refusal_is_undetermined_not_false():
    # 冻结语义：不可读（含锁链拒绝）≠ false —— honest undetermined。
    agent = _MCPAgent({"observe_container_rows": {
        "ok": False, "code": "window_lock_required",
        "error": "window_lock_required",
    }})
    provider = MCPObservationProvider(agent, run_token="tok-1")
    with region_row_provider(provider):
        result = _evaluate(_region_value(deadline=0.5))
    assert result.status is StepStatus.FAILED
    assert result.diagnostic == "region_query_undetermined"
    assert result.actual["samples"][-1]["unreadable"] == "window_lock_required"


def test_region_query_absent_mode_over_mcp_requires_full_deadline():
    agent = _MCPAgent({"observe_container_rows": {
        "ok": True, "rows": [
            {"rowIndex": 0, "name": "无关行", "checked": False,
             "time": _IN_WINDOW_TIME},
        ],
    }})
    provider = MCPObservationProvider(agent, run_token="tok-1")
    with region_row_provider(provider):
        result = _evaluate(_region_value(
            observation={"mode": "absent_full_period",
                         "deadlineSeconds": 1.0,
                         "start": "after_bound_action"}))
    assert result.status is StepStatus.PASSED
    assert len(agent.calls) >= 2  # 整个观察窗内持续采样，而不是首帧即判
