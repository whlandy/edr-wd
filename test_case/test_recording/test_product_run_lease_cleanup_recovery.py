"""product-run 租约清理的恢复语义（批次 5 恢复轮续作）。

``replay_golden_trace`` 的 ``finally`` 块调用 ``end_product_run`` 释放目标端
GUI 租约。目标端有惰性过期兜底（product_policy._expire_if_needed），但
Agent 侧调用是裸的：隧道抖动一次，清理异常会

1. 掩盖回放的真实错误（清理异常替换原始异常），
2. 把已经全部通过的回放报告成失败。

这与 ui_bridge 心跳/收集链路（test_collection_reconnect_recovery.py）同类的
"裸调用无恢复语义"缺口。本回归锁定：清理调用享有与收集链路同语义的瞬态
重试预算；预算耗尽后清理失败被记录、不掩盖原始结果。
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from action_dispatcher import ActionReceipt
from agent.execution import AtomicExecutor
from agent.recording.models import GoldenTrace, RecordedStep, ReplaySelector
from agent.recording.replay import ReplayRuntime, replay_golden_trace
from agent.recording.runtime import build_replay_runtime


def _selector():
    return ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^logo1$"},
        control={"automationId": "btnApply"},
    )


def _golden():
    from target.action_catalog import CATALOG_VERSION, catalog_digest
    return GoldenTrace(
        name="lease cleanup",
        source_recording={"schema": "edr.desktop-recording/v1", "sha256": "sha256:raw"},
        catalog={"version": CATALOG_VERSION, "digest": catalog_digest()},
        environment={"backend": "windows_pywinauto", "target": "win-dev"},
        steps=(RecordedStep("step-0001", "gui.click", {}, _selector()),),
        status="ready",
        cleanup=(),
    )


class _FlakyTarget:
    """Minimal target: begin/end product run, then observe for materialization."""

    def __init__(self, *, end_failures: int = 0, end_always_raises: Exception | None = None):
        self.state = SimpleNamespace(backend_kind="windows_pywinauto",
                                     platform="windows")
        self.begin_calls = 0
        self.end_calls = 0
        self._end_failures = end_failures
        self._end_always = end_always_raises

    def tools_list(self):
        return {"tools": [{"name": name} for name in (
            "begin_product_run", "end_product_run", "observe_window_bundle",
            "focus_window_role", "focus_window_selector",
        )]}

    def call_tool(self, name, args=None, timeout=None):
        if name in ("connect", "lock_window", "verify_window_lock"):
            return {"ok": True}
        if name == "observe_window_bundle":
            return {"ok": True, "observation_id": "OBS-BUNDLE",
                    "window_identity": {
                        "native_id": "W-1", "generation": 1,
                        "process_name": "EDRClient.exe", "title": "logo1",
                        "rect": [0, 0, 800, 600],
                    },
                    "tree": {"controls": [{
                        "process_name": "EDRClient.exe",
                        "window_title": "logo1",
                        "automation_id": "btnApply",
                        "control_type": "Button",
                        "text": "应用",
                    }]}}
        if name == "execute_action":
            return {"ok": True, "request_id": args.get("request_id", "R"),
                    "result": {"ok": True}}
        if name == "begin_product_run":
            self.begin_calls += 1
            return {"ok": True, "run_token": "TOKEN"}
        if name == "end_product_run":
            self.end_calls += 1
            if self._end_always is not None:
                raise self._end_always
            if self.end_calls <= self._end_failures:
                raise ConnectionError("tunnel blip")
            return {"ok": True}
        raise AssertionError(f"unexpected tool call {name}")


class _Controls:
    def refresh(self):
        return "OBS"

    def latest_snapshot_id(self):
        return "OBS"

    def get_snapshot(self, snapshot_id):
        return {
            "snapshot_id": snapshot_id,
            "window_identity": {
                "native_id": "W-1", "generation": 1,
                "process_name": "EDRClient.exe", "title": "logo1",
                "rect": [0, 0, 800, 600],
            },
            "controls": [{
                "target_id": "T0001",
                "process_name": "EDRClient.exe",
                "window_title": "logo1",
                "automation_id": "btnApply",
                "control_type": "Button",
                "text": "应用",
            }],
            "windows": [{"process_name": "EDRClient.exe", "title": "logo1"}],
            "active_window": {"process_name": "EDRClient.exe", "title": "logo1",
                              "rect": [0, 0, 800, 600]},
        }


def _runtime(target, tmp_path, **kwargs):
    runtime = build_replay_runtime(target, _golden(), profile="windows_hisec",
                                   trace_root=tmp_path, **kwargs)
    executor = AtomicExecutor(
        dispatch=lambda **kw: ActionReceipt.from_ok(
            action_id=kw["action_id"], action_code=kw["action_code"],
            request_id=kw["request_id"], result={"ok": True}),
        observation_provider=_Controls(),
    )
    return replace(runtime, executor=executor)


def test_transient_end_product_run_failure_is_retried_and_replay_succeeds(tmp_path):
    target = _FlakyTarget(end_failures=2)
    runtime = _runtime(target, tmp_path)

    result = replay_golden_trace(runtime, _golden())

    assert result.task_success, "a tunnel blip during cleanup must not fail the replay"
    assert target.end_calls == 3, "transient failures retried within budget"


def test_exhausted_end_product_run_budget_does_not_mask_success(tmp_path):
    target = _FlakyTarget(end_always_raises=ConnectionError("tunnel gone"))
    runtime = _runtime(target, tmp_path)

    result = replay_golden_trace(runtime, _golden())

    assert result.task_success, (
        "cleanup failure after a fully passed replay must not flip task_success -- "
        "the target lease expires lazily; masking the green result is the defect"
    )
    assert target.end_calls > 1, "retries happened before giving up"


def test_exhausted_end_product_run_budget_does_not_mask_original_error(tmp_path):
    target = _FlakyTarget(end_always_raises=ConnectionError("tunnel gone"))
    runtime = _runtime(target, tmp_path)

    def disk_full(*args, **kwargs):
        raise OSError("evaluation.json persistence failed")

    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr("agent.recording.replay.tempfile.mkstemp", disk_full)
        with pytest.raises(OSError, match="evaluation.json"):
            replay_golden_trace(runtime, _golden())
    assert target.end_calls > 1, "cleanup still retried, but the original error surfaces"
