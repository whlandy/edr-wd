"""Deterministic Agent-side bridge for the target-local recorder panel.

``record ui`` runs this loop instead of a model conversation: open the panel,
keep the lease alive while the operator works in it, and the moment the
recording stops, fetch the durable result and compile artifacts locally.  The
loop is a plain while — no LLM turns, and it keeps running even when the
conversation that started it is gone.

Lease rule (design §5): the bridge renews only while it is alive and the
session id is unchanged.  A new recording (different sessionId) resets the
tracking; a session that stopped is collected, never resumed.
"""

from __future__ import annotations

import time
from typing import Any, Callable

from agent.recording.collection import collect_recording_artifacts


class UIBridgeError(RuntimeError):
    """The bridge could not reach the target or could not collect results."""


class _RetryingAgent:
    """收集链路的瞬态断连重试：与心跳循环同一预算语义。

    心跳循环只保护 ``recording_status``；收集链路（拉结果、逐帧取证）在
    循环外执行，隧道抖动会把整个桥打死。这里给每个 call_tool 一次独立的
    连续失败预算：预算内重试，耗尽后按心跳路径同样的方式抛 UIBridgeError。
    """

    def __init__(self, agent, *, interval, reconnect_tries, log):
        self._agent = agent
        self._interval = interval
        self._budget = reconnect_tries
        self._log = log

    def call_tool(self, name, arguments=None, timeout=None):
        misses = 0
        while True:
            try:
                return self._agent.call_tool(name, arguments, timeout=timeout)
            except Exception as exc:  # noqa: BLE001 - same class as heartbeat path
                misses += 1
                if misses > self._budget:
                    raise UIBridgeError(
                        f"target connection lost during collection after "
                        f"{misses} attempts: {exc}"
                    ) from exc
                self._log(f"收集链路连接中断，重试 {misses}/{self._budget} …")
                time.sleep(self._interval)


def run_recording_ui_bridge(
    agent,
    *,
    output_root=None,
    profile: str | None = None,
    interval: float = 2.0,
    reconnect_tries: int = 10,
    timeout: float | None = None,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    opened = agent.call_tool("open_recording_ui", {}, timeout=timeout)
    if not opened.get("ok"):
        return {"ok": False, "stage": "open", "error": opened.get("error") or opened}
    log("录制面板已打开（在目标机交互桌面上操作；本窗口保持运行直到录制停止并收集完成）")
    # 断连期间目标端攒下的未收集结果先收一轮——新录制不得被旧结果堵住。
    retrying = _RetryingAgent(agent, interval=interval, reconnect_tries=reconnect_tries,
                              log=log)
    for stale_id in _pending_session_ids(agent, timeout):
        log(f"发现断连期间停止的录制 {stale_id[:19]}…，先收集 …")
        # 历史 pending 同样走完整取证门槛：成功才 mark，缺口保留 pending。
        _collect_one(retrying, stale_id, output_root, profile, timeout, log, mark=True)

    tracked: str | None = None
    misses = 0
    while True:
        try:
            status = agent.call_tool("recording_status", {"heartbeat": True}, timeout=timeout)
            misses = 0
        except Exception as exc:  # noqa: BLE001 - reconnect instead of dying
            misses += 1
            if misses > reconnect_tries:
                raise UIBridgeError(
                    f"target connection lost after {misses} attempts: {exc}"
                ) from exc
            log(f"目标连接中断，重试 {misses}/{reconnect_tries} …")
            time.sleep(interval)
            continue

        if not status.get("ok"):
            # No session yet: the operator has not pressed 开始录制.  Idle is
            # normal; keep waiting with the panel open.
            time.sleep(interval)
            continue

        session_id = status.get("sessionId")
        state = status.get("state")
        if state in {"recording", "paused"}:
            if session_id != tracked:
                tracked = session_id
                log(f"录制中（会话 {str(session_id)[:19]}…，动作 {status.get('actionCount', 0)} 个）")
            time.sleep(interval)
            continue
        if state == "stopped" and session_id:
            log("录制已停止，开始收集产物 …")
            return _collect_one(retrying, session_id, output_root, profile, timeout, log,
                                mark=True)
        # STOPPING or other transient states: keep waiting.
        time.sleep(interval)


def _pending_session_ids(agent, timeout) -> list[str]:
    try:
        listed = agent.call_tool("list_recording_results", {}, timeout=timeout)
        return [s for s in (listed.get("pending") or []) if s]
    except Exception:  # noqa: BLE001 - listing is best-effort
        return []


def _collect_one(agent, session_id: str, output_root, profile, timeout, log,
                 *, mark: bool = False) -> dict[str, Any]:
    stored = agent.call_tool("get_recording_result", {"session_id": session_id}, timeout=timeout)
    if not stored.get("ok"):
        raise UIBridgeError(
            f"could not fetch the stopped recording ({session_id}): "
            f"{stored.get('error') or stored}"
        )
    result = {"ok": True, "recording": stored["recording"], "sessionId": session_id}
    artifacts, capture_issues = collect_recording_artifacts(
        agent, result, output_root, profile, timeout=timeout,
    )
    # 收集确认是门槛不是提示：截图取证有缺口（captureIssues 非空）时结果
    # 必须留在 pending 里——缺证据的结果一旦 mark 就从待收集清单消失。
    if mark and not capture_issues:
        try:
            agent.call_tool("mark_recording_collected", {"session_id": session_id},
                            timeout=timeout)
        except Exception:  # noqa: BLE001 - collection succeeded; marking is a hint
            pass
    elif mark and capture_issues:
        log("存在取证缺口，结果保留在待收集清单（mark_recording_collected 未调用）")
    compiled_issues = [issue.to_dict() for issue in artifacts.compilation.issues]
    directory = str(artifacts.directory)
    log(f"产物已保存：{directory}")
    if compiled_issues:
        codes = sorted({str(i.get("code")) for i in compiled_issues})
        log(f"编译状态 {artifacts.compilation.golden.status}，缺口：{', '.join(codes)}")
    for issue in capture_issues:
        log(f"截图取证失败：sequence {issue.get('sequence')} ({issue.get('captureId')})")
    return {
        "ok": True,
        "sessionId": session_id,
        "directory": directory,
        "status": artifacts.compilation.golden.status,
        "issues": compiled_issues,
        "captureIssues": capture_issues,
    }


def _collect(agent, session_id: str, output_root, profile, timeout, log) -> dict[str, Any]:
    stored = agent.call_tool("get_recording_result", {"session_id": session_id}, timeout=timeout)
    if not stored.get("ok"):
        raise UIBridgeError(
            f"could not fetch the stopped recording ({session_id}): "
            f"{stored.get('error') or stored}"
        )
    result = {"ok": True, "recording": stored["recording"], "sessionId": session_id}
    artifacts, capture_issues = collect_recording_artifacts(
        agent, result, output_root, profile, timeout=timeout,
    )
    compiled_issues = [issue.to_dict() for issue in artifacts.compilation.issues]
    directory = str(artifacts.directory)
    log(f"产物已保存：{directory}")
    if compiled_issues:
        codes = sorted({str(i.get("code")) for i in compiled_issues})
        log(f"编译状态 {artifacts.compilation.golden.status}，缺口：{', '.join(codes)}")
    for issue in capture_issues:
        log(f"截图取证失败：sequence {issue.get('sequence')} ({issue.get('captureId')})")
    return {
        "ok": True,
        "sessionId": session_id,
        "directory": directory,
        "status": artifacts.compilation.golden.status,
        "issues": compiled_issues,
        "captureIssues": capture_issues,
    }
