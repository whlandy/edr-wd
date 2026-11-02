"""回放入口窗口获取（acquire_target_window）的拒绝语义。

``build_replay_runtime`` 在任何步骤执行前调用 ``acquire_target_window``：
connect → lock_window(strict=True, activate=True) → verify_window_lock。
任一工具返回 ``ok=False`` 都必须抛 ``replay_target_unavailable``，
且后续工具不得再被调用——回放绝不能在窗口锁未确认的情况下继续。

此前这条路径零直接测试。它同时是两类的交集：
- 批次 1（不能误操作同标题窗口）：strict 锁拒绝歧义窗口；
- 批次 5（目标窗口消失/连接失败）：connect 失败必须立即拒绝。

本回归锁定（对 runtime.py，非 WIP 业务文件——只读不改）：
1. lock_window 必须 strict=True（歧义/同标题窗口不得静默命中其一）；
2. 任一步失败时后续工具不调用，错误码 replay_target_unavailable；
3. verify_window_lock 失败同样拒绝（锁到了但未激活≠可回放）；
4. 全绿时三步按序调用且返回 ok。
"""

from __future__ import annotations

import pytest

from agent.recording.models import RecordingModelError
from agent.recording.runtime import ReplayScope, acquire_target_window


class _ScriptedTarget:
    """按脚本应答 call_tool；记录每次调用的 (name, args)。"""

    def __init__(self, script: dict[str, object]):
        self.script = script
        self.calls: list[tuple[str, dict | None]] = []

    def call_tool(self, name, args=None, timeout=None):
        self.calls.append((name, args))
        outcome = self.script.get(name, {"ok": True})
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _scope() -> ReplayScope:
    return ReplayScope("EDRClient.exe", "^logo1$")


def test_lock_window_is_strict_and_activated():
    target = _ScriptedTarget({})
    acquire_target_window(target, _scope())
    lock_args = next(args for name, args in target.calls if name == "lock_window")
    assert lock_args["strict"] is True, (
        "非 strict 锁可能静默命中多个同标题窗口之一 -- 歧义必须显式拒绝"
    )
    assert lock_args["activate"] is True
    assert lock_args["process_name"] == "EDRClient.exe"
    assert lock_args["title_re"] == "^logo1$"


def test_connect_failure_refuses_before_lock():
    target = _ScriptedTarget({"connect": {"ok": False, "error": "process not found"}})
    with pytest.raises(RecordingModelError) as excinfo:
        acquire_target_window(target, _scope())
    assert excinfo.value.code == "replay_target_unavailable"
    assert [name for name, _ in target.calls] == ["connect"], (
        "connect 失败后不得继续 lock_window/verify -- 目标已消失必须立即拒绝"
    )
    assert "connect" in str(excinfo.value)


def test_lock_failure_refuses_before_verify():
    target = _ScriptedTarget({
        "lock_window": {"ok": False, "error": "window_ambiguous"},
    })
    with pytest.raises(RecordingModelError) as excinfo:
        acquire_target_window(target, _scope())
    assert excinfo.value.code == "replay_target_unavailable"
    assert [name for name, _ in target.calls] == ["connect", "lock_window"], (
        "锁失败（如同标题歧义）后不得继续 verify/回放"
    )


def test_verify_failure_refuses_acquisition():
    target = _ScriptedTarget({
        "verify_window_lock": {"ok": False, "error": "lock not activated"},
    })
    with pytest.raises(RecordingModelError) as excinfo:
        acquire_target_window(target, _scope())
    assert excinfo.value.code == "replay_target_unavailable"
    assert [name for name, _ in target.calls] == [
        "connect", "lock_window", "verify_window_lock",
    ]


def test_non_dict_result_refuses_acquisition():
    target = _ScriptedTarget({"lock_window": None})
    with pytest.raises(RecordingModelError) as excinfo:
        acquire_target_window(target, _scope())
    assert excinfo.value.code == "replay_target_unavailable"
    assert [name for name, _ in target.calls] == ["connect", "lock_window"]


def test_transport_exception_propagates_untyped():
    # 连接层异常（隧道断开等）不是模型拒绝 -- 原样上抛，
    # 由上层（CLI/integration）决定恢复语义；不得被吞成 ok=False。
    target = _ScriptedTarget({"connect": ConnectionError("tunnel down")})
    with pytest.raises(ConnectionError, match="tunnel down"):
        acquire_target_window(target, _scope())
    assert [name for name, _ in target.calls] == ["connect"]


# ---- 回放入口窗口受控恢复（第一阶段，默认关闭） ----
#
# R0 门控：仅结构化 connect_target_not_found 才进入恢复评估；歧义/权限/
# 通信/后端/格式错误一律直接失败，不得用激活掩盖。恢复动作必须可盘审计，
# 且报告"缺失"（entry_window_absent）与"存在但不可恢复"（unavailable）分开。

from agent.recording.runtime import (
    R_RECOVERABLE_CONNECT,
    ENTRY_WINDOW_ABSENT,
)


def _absent_conn_result() -> dict:
    return {"ok": False, "code": "connect_target_not_found",
            "error": "no window satisfies every connect criterion"}


def _ambiguous_conn_result() -> dict:
    return {"ok": False, "code": "connect_target_ambiguous",
            "error": "several windows satisfy every connect criterion"}


def _disposition(result_kind: str) -> object:
    """Recovery hook returning a fixed disposition."""
    def hook(_agent, _scope, _result):
        return {"action": "none", "result": result_kind}
    return hook

def test_recovery_not_invoked_by_default():
    # 默认关闭：recovery=None，connect_target_not_found 仍按旧语义拒绝，
    # 绝不调用 is_window_open 重确认。
    target = _ScriptedTarget({"connect": _absent_conn_result()})
    with pytest.raises(RecordingModelError) as excinfo:
        acquire_target_window(target, _scope())
    assert excinfo.value.code == "replay_target_unavailable"
    assert [name for name, _ in target.calls] == ["connect"]


def test_structured_not_found_absent_raises_explicit_code():
    # 仅结构化 not_found 触发恢复；重确认证明窗口不存在 -> entry_window_absent。
    script = {"connect": _absent_conn_result(),
              "is_window_open": {"ok": True, "windows": []}}
    target = _ScriptedTarget(script)
    with pytest.raises(RecordingModelError) as excinfo:
        acquire_target_window(target, _scope(), recovery=_entry_probe())
    assert excinfo.value.code == ENTRY_WINDOW_ABSENT
    # connect 失败 -> 重确认（is_window_open）-> 报告缺失；不 lock/verify。
    assert [name for name, _ in target.calls] == ["connect", "is_window_open"]


def test_ambiguous_not_recoverable_and_probe_not_called():
    # 歧义（多个窗口都匹配）绝不用激活掩盖，也不触发重确认。
    script = {"connect": _ambiguous_conn_result(),
              "is_window_open": {"ok": True, "windows": [{"pid": 1}]}}
    target = _ScriptedTarget(script)
    with pytest.raises(RecordingModelError) as excinfo:
        acquire_target_window(target, _scope(), recovery=_entry_probe())
    assert excinfo.value.code == "replay_target_unavailable"
    assert [name for name, _ in target.calls] == ["connect"]


def test_exists_but_not_recoverable_still_refuses():
    # 窗口存在但当前能力无法租约授权激活：报告不可恢复，不假装重建。
    script = {"connect": _absent_conn_result(),
              "is_window_open": {"ok": True, "windows": [{"pid": 7, "title": "logo1", "handle": 1}]}}
    target = _ScriptedTarget(script)
    with pytest.raises(RecordingModelError) as excinfo:
        acquire_target_window(target, _scope(), recovery=_entry_probe())
    assert excinfo.value.code == "replay_target_unavailable"
    assert [name for name, _ in target.calls] == ["connect", "is_window_open"]


def test_reconfirm_transport_error_is_unknown_not_absent():
    # 重确认阶段通信失败 = 未知，不是缺失；不得把 flaky 当成 absent。
    script = {"connect": _absent_conn_result()}
    target = _ScriptedTarget(script)
    target.script["is_window_open"] = ConnectionError("tunnel down")
    with pytest.raises(RecordingModelError) as excinfo:
        acquire_target_window(target, _scope(), recovery=_direct_disposition("unknown"))
    # 直接 disposition "unknown" -> 拒绝（unavailable），不是 absent。
    assert excinfo.value.code == "replay_target_unavailable"


def test_activate_existing_retries_chain_once():
    # 恢复钩子报告 activate_existing -> 重试整个 connect->lock->verify 链一次。
    class _RecoveringTarget(_ScriptedTarget):
        def call_tool(self, name, args=None, timeout=None):
            if name == "connect":
                # record the call, then fail exactly once before succeeding
                self.calls.append((name, args))
                if getattr(self, "_connect_failed", False):
                    return {"ok": True}
                self._connect_failed = True
                return _absent_conn_result()
            return super().call_tool(name, args, timeout=timeout)

    target = _RecoveringTarget({"lock_window": {"ok": True},
                                "verify_window_lock": {"ok": True}})

    def hook(_agent, _scope, _result):
        return {"action": "activate_existing", "result": "ok"}

    assert acquire_target_window(target, _scope(), recovery=hook)["ok"] is True
    # connect(失败,触发恢复) -> 重试 connect(成功) -> lock -> verify。
    assert target.calls == [
        ("connect", _scope_connect_args()),
        ("connect", _scope_connect_args()),
        ("lock_window", _scope_lock_args()),
        ("verify_window_lock", {"activate": True}),
    ]


def test_activate_existing_gives_up_after_one_retry():
    # 激活后重试仍 not_found -> 停止，不无限重试。
    script = {"connect": _absent_conn_result(),
              "verify_window_lock": {"ok": True}}
    target = _ScriptedTarget(script)

    def hook(_agent, _scope, _result):
        return {"action": "activate_existing", "result": "ok"}

    with pytest.raises(RecordingModelError) as excinfo:
        acquire_target_window(target, _scope(), recovery=hook)
    assert excinfo.value.code == ENTRY_WINDOW_ABSENT


def _scope_connect_args() -> dict:
    return {"process_name": "EDRClient.exe", "title_re": "^logo1$", "timeout": 10.0}


def _scope_lock_args() -> dict:
    return {"process_name": "EDRClient.exe", "title_re": "^logo1$",
            "strict": True, "activate": True}


def _entry_probe() -> object:
    """Recovery hook that answers is_window_open from the scripted target."""
    def hook(_agent, _scope, _result):
        probe = _agent.call_tool("is_window_open",
                                 {"process_name": _scope.process_name,
                                  "title_re": _scope.title_regex},
                                 timeout=10.0)
        if isinstance(probe, dict) and probe.get("ok"):
            windows = probe.get("windows") or []
            if windows:
                return {"action": "none", "result": "not_recoverable"}
            return {"action": "none", "result": "entry_window_absent"}
        return {"action": "none", "result": "unknown"}
    return hook


def _direct_disposition(kind: str) -> object:
    def hook(_agent, _scope, _result):
        return {"action": "none", "result": kind}
    return hook
