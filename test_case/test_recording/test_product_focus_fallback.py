# 产品租约内无 root 聚焦回退与原始错误码保留。
#
# 复现 2026-10-25-0616 独立实机失败：fixture golden（无 rootAutomationId）在
# qa_recording_fixture 租约下，MCPObservationProvider.focus_window 经
# execute_action(session.connect) 聚焦，被 target 产品白名单拒绝
#（fixture allowed 仅 gui.click/gui.type_text），AtomicExecutor 把探测异常
# 吞成全步骤 {"code":"backend_unavailable"}（action=null），伪报后端离线。
#
# 修复目标：
# 1. 诊断性：BackendUnavailable 携带原始 code，run_case 探测失败把原始
#    code/message 写进每个 blocked 步骤（不再伪报后端离线）；
# 2. 聚焦回退：主路径 execute_action(session.*) 保持不变（既有 profile 行为
#    零变化）；被产品策略拒绝且运行时可解析出唯一入口 role 时，回退
#    focus_window_role（产品域设计的聚焦，建立租约会话绑定，不经 allowed
#    白名单，禁走 legacy connect/lock）；回退后校验聚焦窗口身份与 golden
#    作用域一致。
# 全部使用真实 check_product_action 策略与真实 fixture capability。
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.execution.executor import AtomicExecutor, BackendUnavailable
from agent.recording.mcp_runtime import MCPObservationProvider
from agent.recording.replay import load_golden_trace
from target.action_dispatcher.conditions import check_product_action
from target.product_policy import ProductRunContext
from target.product_profiles import get_capability
from agent.trace.store import TraceStore

pytestmark = pytest.mark.unit

TITLE = "EDR-WD Native Recording QA [qa-dsh-fixed-1025]"
TITLE_REGEX = r"^EDR\-WD Native Recording QA \[qa\-dsh\-fixed\-1025\]$"
PROCESS = "powershell.exe"


def _fixture_capability():
    return get_capability("qa_recording_fixture", "windows_pywinauto")


class _FixtureTarget:
    """贴近生产的 target 桩：execute_action 走真实产品策略门控。"""

    def __init__(self, *, allow_session_actions: bool = False,
                 bundle_title: str = TITLE):
        self.calls: list[tuple[str, dict]] = []
        self._allow_session = allow_session_actions
        self._bundle_title = bundle_title
        self.context = ProductRunContext(
            "run-1", "tok-1", _fixture_capability(), time.monotonic() + 600)

    def tools_list(self):
        names = ("begin_product_run", "end_product_run", "observe_window_bundle",
                 "focus_window_role", "focus_window_selector", "execute_action",
                 "connect", "lock_window", "verify_window_lock", "get_window_lock",
                 "dump_tree")
        return {"tools": [{"name": n} for n in names]}

    state = SimpleNamespace(backend_kind="windows_pywinauto")

    def call_tool(self, name, arguments, timeout=None):
        self.calls.append((name, dict(arguments or {})))
        if name == "execute_action":
            action_id = (arguments or {}).get("action_id", "")
            if self._allow_session and action_id.startswith("session."):
                # 模拟 allowed_action_ids 含 session.* 的 profile（notepad/hisec）
                return {"ok": True, "result": {"ok": True}}
            receipt = check_product_action(self.context, action_id)
            if receipt is not None:
                return {"ok": False, "code": receipt.code, "action_id": action_id}
            return {"ok": True, "result": {"ok": True}}
        if name == "focus_window_role":
            if not self._allow_session and (arguments or {}).get("role") != "main":
                return {"ok": False, "code": "unknown_window_role"}
            return {"ok": True, "identity": {"root_control_id": "RecordingFixture"}}
        if name == "observe_window_bundle":
            return {"ok": True, "observation_id": "OBS-0001",
                    "window_identity": {"native_id": "w-1", "generation": 1,
                                        "process_name": PROCESS,
                                        "title": self._bundle_title,
                                        "root_control_id": "RecordingFixture"},
                    "tree": {"controls": [
                        {"automation_id": "RecordingFixture.FixtureSave",
                         "control_type": "Button", "name": "Save sample"},
                    ]}}
        if name in {"connect", "lock_window", "verify_window_lock",
                    "get_window_lock", "dump_tree"}:
            return {"ok": True}
        return {"ok": True}


def _provider(tmp_path, target, *, entry_role=None):
    store = TraceStore(Path(tmp_path) / "traces")
    store.open()
    return MCPObservationProvider(
        target, max_depth=6, trace_store=store, capture_screenshot=False,
        timeout=5, use_bundle=True, run_token="tok-1", include_ocr=False,
        entry_role=entry_role,
    )


def test_unrooted_focus_under_product_run_falls_back_to_entry_role(tmp_path):
    # 主路径 session.connect 被真实产品策略拒绝 → 回退 focus_window_role(main)
    # 成功聚焦；全程禁走 legacy connect/lock_window（产品窗口域）。
    target = _FixtureTarget()
    provider = _provider(tmp_path, target, entry_role="main")
    snapshot_id = provider.focus_window(PROCESS, TITLE_REGEX)
    assert snapshot_id
    executed = [a for n, a in target.calls if n == "execute_action"]
    assert executed and executed[0]["action_id"] == "session.connect"
    assert any(n == "focus_window_role" for n, _ in target.calls)
    assert not any(n in {"connect", "lock_window"} for n, _ in target.calls)


def test_unrooted_focus_without_fallback_keeps_original_refusal_code(tmp_path):
    # 无 entry_role 时不得吞码：BackendUnavailable 必须携带原始拒绝码。
    target = _FixtureTarget()
    provider = _provider(tmp_path, target, entry_role=None)
    with pytest.raises(BackendUnavailable) as excinfo:
        provider.focus_window(PROCESS, TITLE_REGEX)
    assert excinfo.value.code == "product_action_forbidden"
    assert not any(n in {"connect", "lock_window"} for n, _ in target.calls)


def test_fallback_focus_verifies_window_identity(tmp_path):
    # 回退聚焦后必须校验窗口身份与 golden 作用域一致，不得锁错窗口。
    target = _FixtureTarget(bundle_title="A Different Window")
    provider = _provider(tmp_path, target, entry_role="main")
    with pytest.raises(BackendUnavailable) as excinfo:
        provider.focus_window(PROCESS, TITLE_REGEX)
    assert excinfo.value.code == "window_identity_mismatch"


def test_session_allowed_profiles_keep_primary_focus_path(tmp_path):
    # 含 session.* 的 profile（notepad/hisec）行为零变化：主路径直接成功，
    # 不触发 role 回退。
    target = _FixtureTarget(allow_session_actions=True)
    provider = _provider(tmp_path, target, entry_role="main")
    snapshot_id = provider.focus_window(PROCESS, TITLE_REGEX)
    assert snapshot_id
    assert not any(n == "focus_window_role" for n, _ in target.calls)


def test_run_case_preserves_original_probe_code_in_blocked_steps(tmp_path):
    # 0616 实证形状复现：探测失败曾把 3 步全部写成 backend_unavailable；
    # 修复后每个 blocked 步骤携带原始 code 与 message，不再伪报后端离线。
    from target.protocol_models import AtomicTestStep, TestCase

    class _Materializer:
        def __init__(self, provider):
            self._provider = provider

        def prepare_observation_for_step(self, step):
            try:
                self._provider.focus_window(PROCESS, TITLE_REGEX)
            except BackendUnavailable as exc:
                raise BackendUnavailable(str(exc), code=exc.code) from exc

        def recover_pre_action_observation(self, step):
            return False

        def recover_post_action_observation(self, step):
            return False

    target = _FixtureTarget()  # session.* 拒绝、无回退 → 原始码 product_action_forbidden
    provider = _provider(tmp_path, target, entry_role=None)
    steps = tuple(
        AtomicTestStep(step_id=f"step-{i:04d}", action_id="gui.click",
                       args={}, required=True)
        for i in range(1, 4)
    )
    case = TestCase(case_id="case_qa", title="qa", description="",
                    steps=steps)
    executor = AtomicExecutor(
        dispatch=lambda **kwargs: {"ok": True},
        observation_provider=provider,
        confirmation_gate=None,
        risk_lookup=lambda action_id: ("low", ""),
        step_materializer=_Materializer(provider),
    )
    result = executor.run_case(case)
    assert len(result.step_results) == 3
    for sr in result.step_results:
        assert sr.status.value == "blocked"
        assert sr.error["code"] == "product_action_forbidden"
        assert sr.error.get("message")


def test_resolve_entry_role_matches_single_process_role():
    from agent.recording.runtime import _resolve_entry_role
    golden = load_golden_trace(Path(
        "result-report/20261025-0520-replay-profile-binding"
        "/recompile-qa-profile/independent_unique_0426/golden-trace.json"))
    assert _resolve_entry_role(
        "qa_recording_fixture", "windows_pywinauto", PROCESS) == "main"
    # EDR profile 多角色共享进程 → 不解析（回退不可用，主路径不受影响）
    assert _resolve_entry_role(
        "windows_hisec", "windows_pywinauto", "EDRClient.exe") is None
