"""批次 5 恢复轮：收集链路断连恢复回归。

背景（2026-10-30 审计）：``run_recording_ui_bridge`` 的重连预算只包住
``recording_status`` 心跳。一旦隧道在**收集链路**（``get_recording_result`` /
``get_recording_capture``）上抖动，异常直接冒出 ``run_recording_ui_bridge``，
桥死亡，操作者拿不到产物路径——而同一类故障在心跳路径上设计为可恢复。
目标端会话数据仍在（pending 行/租约看门狗兜底），但 Agent 侧的既有恢复语义
应当一致：短暂抖动重试，预算耗尽才放弃。

契约：
- 收集链路出现 ≤ reconnect_tries 次瞬态异常 → 重试后成功收集，结果与
  无抖动时一致。
- 收集链路持续失败 → 抛 UIBridgeError（预算语义与心跳路径一致），
  不无限重试、不静默吞掉。
- 成功收集后 mark_recording_collected 仍然只在无取证缺口时调用
  （重试不得改变收集门槛语义）。
"""

import json
from pathlib import Path

import pytest

from agent.recording.ui_bridge import UIBridgeError, run_recording_ui_bridge

REPO = Path(__file__).resolve().parents[2]

RECORDING_DOC = json.loads(
    (REPO / "recordings" / "endcloud_demo_20261022_r2" / "recording.json")
    .read_text(encoding="utf-8"))


class _CollectFlakyAgent:
    """open/status 正常，收集链路前 N 次 raise，之后放行。"""

    def __init__(self, session_id, fail_tools, fail_times):
        self.session_id = session_id
        self.recording_doc = RECORDING_DOC
        self.fail_tools = fail_tools
        self.fail_times = fail_times
        self.calls: list[tuple[str, dict]] = []
        self.status_calls = 0

    def call_tool(self, name, arguments=None, timeout=None):
        self.calls.append((name, dict(arguments or {})))
        if name == "open_recording_ui":
            return {"ok": True}
        if name == "recording_status":
            self.status_calls += 1
            if self.status_calls == 1:
                return {"ok": True, "sessionId": self.session_id,
                        "state": "recording", "actionCount": 2}
            return {"ok": True, "sessionId": self.session_id, "state": "stopped"}
        if name in self.fail_tools and len(
                [c for c in self.calls if c[0] == name]) <= self.fail_times:
            raise ConnectionError("tunnel blip during collection")
        if name == "get_recording_result":
            return {"ok": True, "sessionId": self.session_id,
                    "recording": self.recording_doc}
        if name == "get_recording_capture":
            return {"ok": False, "code": "recording_capture_missing"}
        if name == "list_recording_results":
            return {"ok": True, "pending": []}
        if name == "mark_recording_collected":
            return {"ok": True}
        raise AssertionError(f"unexpected tool {name}")


def _status_call_count(agent):
    return sum(1 for name, _ in agent.calls if name == "recording_status")


def test_collection_transient_failure_recovers_and_collects(tmp_path):
    agent = _CollectFlakyAgent(
        RECORDING_DOC["sessionId"],
        fail_tools={"get_recording_result", "get_recording_capture"},
        fail_times=2,
    )
    result = run_recording_ui_bridge(
        agent, output_root=tmp_path, profile=None, interval=0.01,
        reconnect_tries=10, log=lambda m: None)
    assert result["ok"] is True
    assert result["sessionId"] == RECORDING_DOC["sessionId"]
    assert Path(result["directory"]) / "golden-trace.json"
    # 重试确实发生了（收集链路重试过，而不是一次通过）
    assert sum(1 for n, _ in agent.calls if n == "get_recording_result") >= 3


def test_collection_persistent_failure_raises_budget_error(tmp_path):
    agent = _CollectFlakyAgent(
        RECORDING_DOC["sessionId"],
        fail_tools={"get_recording_result"},
        fail_times=10_000,
    )
    with pytest.raises(UIBridgeError):
        run_recording_ui_bridge(
            agent, output_root=tmp_path, profile=None, interval=0.01,
            reconnect_tries=3, log=lambda m: None)
    # 预算语义：有限次尝试后放弃，不是无限重试
    attempts = sum(1 for n, _ in agent.calls if n == "get_recording_result")
    assert 3 <= attempts <= 8


def test_recovery_does_not_mark_with_capture_issues(tmp_path):
    """恢复路径同样走完整取证门槛：capture 全缺失 → 不 mark。"""
    agent = _CollectFlakyAgent(
        RECORDING_DOC["sessionId"],
        fail_tools={"get_recording_result"},
        fail_times=1,
    )
    result = run_recording_ui_bridge(
        agent, output_root=tmp_path, profile=None, interval=0.01,
        reconnect_tries=10, log=lambda m: None)
    assert result["ok"] is True
    assert result["captureIssues"], "fixture 依赖 capture 缺失产生取证缺口"
    assert not any(n == "mark_recording_collected" for n, _ in agent.calls)
