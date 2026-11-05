#!/usr/bin/env python3
"""S5 acceptance round driver (09 design doc, §11).

One round = fresh recording -> stop collection -> compile -> replay ->
business assertions -> concise feature report. Evidence lands in
result-report/s5-windows-acceptance/round-N/.

Modes:
  --local-fixture    REAL end-to-end round on the local chain: REAL
                     RecordingService session start -> OS-synthetic capture
                     events through the REAL correlator/session.ingest ->
                     stop persists the raw v2 document -> REAL compiler ->
                     REAL main-chain replay (execute_v3_window_stage_real:
                     real server/policy/binding/dispatcher/ledger) -> real
                     business assertions -> concise feature report. The fake
                     OS backend replaces only enumeration/connect/lock/input
                     injection; authorization, lease, binding and dispatcher
                     are the production code paths. No GUI.
  --live             The real Windows round. Refused unless the supervising
                     chat has explicitly authorized the exact target and
                     actions; the refusal names exactly what is missing.

The driver never fabricates results: each stage writes its own artifact,
and the round summary records exactly what ran, what is missing and which
contract gate stopped the round. Any required stage failing/unknown/skipped
or not run exits non-zero.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
EVIDENCE_ROOT = REPO / "result-report" / "s5-windows-acceptance"

ROUND_STAGES = ("recording", "stop-collection", "compile", "replay",
                "business-assertions", "traceable-report")


def _fail_closed_note() -> dict:
    """Production capability honesty: without a backend-declared strong
    instance capability the round must refuse, not fabricate ready."""
    try:
        from target.automation.windows_pywinauto import WindowsPywinautoBackend
    except ImportError:
        return {"backend_capability_implemented": None,
                "note": "target package unavailable in this environment"}
    probe = getattr(WindowsPywinautoBackend, "window_sequence_capability",
                    None)
    return {"backend_capability_implemented": callable(probe),
            "note": ("real backend must implement reliable per-run "
                     "instance tracking (events/generation/loss "
                     "detection) and declare window_sequence/v1; "
                     "otherwise the S3 gate refuses (fail closed)")}


# ---- fake OS backend (OS fact layer only; same contract as the
# acceptance suite's _FakeOSBackend) ------------------------------------

_DRIVER_PATCH = None


def _make_fake_os_backend(windows):
    from types import SimpleNamespace as _NS

    class _FakeOSBackend:
        """替身 OS 事实层(仅枚举/连接/锁定/输入注入;授权/租约/
        binding/dispatcher 全真实)。"""

        def __init__(self, scroll_fact="scrolled-row",
                     checked_fact=True):
            self._scroll_fact = scroll_fact
            self._checked_fact = checked_fact
            self._windows = {str(k): dict(v) for k, v in windows.items()}
            self.dispatch_calls = []
            self._lock_state = None
            self._gui = _NS(main_window=None)
            self._backend_kind = "windows_pywinauto"

        def _enumerate_top_windows_windows(self, *, strict=False):
            # 对话窗口的父边(真实 UIA 携带 owner/parent;runtime 角色
            # 匹配要求 log_center 指向 main 的边)。
            main_native = next((nid for nid, w in self._windows.items()
                                if "EdrMain" in (w.get("root_control_id")
                                                 or "")), None)
            entries = []
            for nid, w in self._windows.items():
                parent = None
                if "LogCenter" in (w.get("root_control_id") or "") and \
                        main_native is not None:
                    parent = main_native
                entries.append({
                    "handle": int(nid),
                    "pid": w.get("process_id", 42),
                    "process_name": w.get("process_name",
                                          "EDRClient.exe"),
                    "title": w.get("title", ""),
                    "rect": {"left": 0, "top": 0, "right": 800,
                             "bottom": 600},
                    "visible": w.get("visible", True),
                    "foreground": w.get("foreground", True),
                    "root_control_id": w.get("root_control_id", ""),
                    "parent_native_id": parent})
            return entries

        def connect_window_identity(self, handle, pid, timeout=10.0):
            item = self._windows.get(str(handle))
            if item is None or int(item.get("process_id", 42)) != int(pid):
                return {"ok": False, "error": "gone"}
            self._gui.main_window = _NS(_handle=int(handle), _pid=int(pid),
                                        _title=item.get("title", ""))
            return {"ok": True, "pid": int(pid), "handle": int(handle),
                    "title": item.get("title", "")}

        def lock_window(self, title_re=None, process_name=None, pid=None,
                        strict=True, activate=True, **kwargs):
            state = self._connected_window_state()
            if not state.get("ok"):
                return {"ok": False,
                        "code": "window_lock_criteria_mismatch",
                        "error": "no connected window"}
            from target.automation.base import lock_criteria_mismatch
            if lock_criteria_mismatch(state, title_re, process_name, pid):
                return {"ok": False,
                        "code": "window_lock_criteria_mismatch",
                        "error": "criteria mismatch"}
            self._lock_state = {
                "backend": "windows_pywinauto",
                "title_re": title_re or (state.get("title") or ""),
                "process_name": state.get("process_name"),
                "pid": state.get("pid"), "handle": state.get("handle"),
                "strict": bool(strict), "snapshot": state,
                "generation": 2}
            return {"ok": True, "lock": self._lock_state,
                    "verify": {"ok": True, "locked": True,
                               "foreground_verified": True,
                               "degraded": False}}

        def get_window_lock(self):
            return {"ok": True, "locked": self._lock_state is not None,
                    "lock": self._lock_state}

        def verify_window_lock(self, activate=True):
            if self._lock_state is None:
                return {"ok": True, "locked": False}
            state = self._connected_window_state()
            if not state.get("ok") or state.get("pid") != \
                    self._lock_state.get("pid"):
                return {"ok": False, "locked": True,
                        "code": "ownership_mismatch"}
            return {"ok": True, "locked": True, "foreground_verified": True,
                    "degraded": False, "active": state,
                    "lock": self._lock_state}

        def unlock_window(self):
            self._lock_state = None
            return {"ok": True}

        def _connected_window_state(self):
            win = self._gui.main_window
            if win is None:
                return {"ok": False, "error": "Not connected"}
            return {"ok": True, "handle": win._handle,
                    "title": win._title, "pid": win._pid,
                    "process_name": self.process_name_for_pid(win._pid),
                    "rectangle": {"x": 0, "y": 0, "w": 800, "h": 600}}

        def process_name_for_pid(self, pid):
            for w in self._windows.values():
                if int(w.get("process_id", 42)) == int(pid):
                    return w.get("process_name", "EDRClient.exe")
            return None

        def exe_path_for_pid(self, pid):
            return r"C:\Program Files\HiSec-Endpoint\core\EDRClient.exe"

        def dump_tree(self, window_title_re=None, max_depth=10):
            if self._lock_state is None:
                return {"ok": False, "error": "No window connected"}
            nid = str(self._lock_state.get("handle"))
            w = self._windows.get(nid)
            if w is None:
                return {"ok": False, "error": "locked window not found"}
            # 只投影**当前已锁定窗口**的根控件(1059 缺口2:不遍历
            # 全部窗口冒充本窗口观测);root_class_name/root_text 与
            # 录制声明 selector 的 controlType/name 对齐。
            controls = [{"class_name": w.get("root_class_name", "Button"),
                         "text": w.get("root_text", w.get("title", "")),
                         # OS 事实层:控件的 Toggle 状态与可见/启用同为
                         # 真实 UIA 树投影字段(checked_fact 注入可控 —
                         # 求值仍由真实 evaluator 从观测读出)。
                         "checked": w.get("root_checked",
                                          self._checked_fact),
                         "control_id": 1,
                         "automation_id": w.get("root_control_id", ""),
                         "rectangle": {"x": 0, "y": 0, "w": 800, "h": 600},
                         "is_visible": True, "is_enabled": True,
                         "depth": 0}]
            for extra in w.get("extra_controls") or []:
                idx = len(controls)
                controls.append({
                    "class_name": extra.get("class_name", "Button"),
                    "text": extra.get("text", ""),
                    "control_id": len(controls) + 1,
                    "automation_id": extra.get("automation_id", ""),
                    "rectangle": {"x": 10 + 30 * idx, "y": 10 + 25 * idx,
                                  "w": 100, "h": 30},
                    "is_visible": True, "is_enabled": True, "depth": 1})
            return {"ok": True, "title": self._lock_state.get("title"),
                    "window_rectangle": {"x": 0, "y": 0, "w": 800,
                                         "h": 600},
                    "rectangle_mode": "screen", "truncated": False,
                    "complete": True, "controls": controls}

        def click(self, **kwargs):
            self.dispatch_calls.append({"method": "click", **kwargs})
            return {"ok": True, "data": {"clicked": True}}

        def click_at(self, x, y, expected_process_name=None):
            self.dispatch_calls.append({"method": "click_at", "x": x,
                                        "y": y})
            return {"ok": True, "method": "mouse.click"}

        def drag(self, x1, y1, x2, y2, duration=0.25, **kwargs):
            self.dispatch_calls.append({"method": "drag", "x1": x1,
                                        "y1": y1, "x2": x2, "y2": y2})
            return {"ok": True, "method": "dragTo"}

        def scroll(self, clicks, x=None, y=None, **kwargs):
            self.dispatch_calls.append({"method": "scroll",
                                        "clicks": clicks})
            # OS 事实层:滚动改变滚动目标控件(root_text — 滚动语义
            # 的结果事实;未变化对照由注入方通过 _scroll_fact 控制)。
            self._windows["100"]["root_text"] = \
                self._scroll_fact or "Apply"
            return {"ok": True, "method": "scroll",
                    "scrollTargetText": self._windows["100"]["root_text"]}

        def activate_app(self, app_name=None, bundle_id=None):
            self.dispatch_calls.append({"method": "activate_app",
                                        "app_name": app_name})
            return {"ok": True, "method": "activate_registered"}

        def focus_existing_window(self, *, handle, pid, title,
                                  process_name):
            item = self._windows.get(str(handle))
            if item is None or int(item.get("process_id", 42)) != int(pid):
                return {"ok": False, "error": "gone"}
            return {"ok": True, "method": "focus_existing_window",
                    "handle": int(handle), "pid": int(pid)}

        def window_sequence_capability(self):
            return {"supported": True, "coverage": "strong",
                    "generation": 2}

        def lifecycle_event_stream(self):
            from target.windowing import instance_tracker
            backend = self

            class _DynamicStream:
                """动作响应的生命周期事件流(fake OS 事实层):每次
                drain 对照当前可见性差分 emit create/destroy — 与真实
                SetWinEventHook 的 SHOW/HIDE/DESTROY 语义一致。"""

                def __init__(self):
                    self._seen = set()
                    self._pending = []
                    self._healthy = True
                    self.failure = ""

                @property
                def healthy(self):
                    return self._healthy

                def drain_events(self):
                    events = list(self._pending)
                    self._pending.clear()
                    for native_id, w in backend._windows.items():
                        visible = w.get("visible", True) is not False
                        was = native_id in self._seen
                        if visible and not was:
                            events.append({
                                "kind": "create",
                                "native_id": str(native_id),
                                "pid": w.get("process_id", 42),
                                "root_control_id":
                                    w.get("root_control_id", ""),
                                "class_name": "FakeWnd"})
                            self._seen.add(native_id)
                        elif not visible and was:
                            events.append({"kind": "destroy",
                                           "native_id": str(native_id)})
                            self._seen.discard(native_id)
                    return events

            return _DynamicStream()

    return _FakeOSBackend


def _install_fake_os_driver(backend):
    """注入 fake OS 层(真实 server/policy/binding/dispatcher 仅 OS
    事实层替换 — 与验收套件同一契约)。"""
    import pytest as _pytest
    from types import SimpleNamespace as _NS
    import target.server as server
    from target.action_dispatcher import set_backend_resolver
    from target import product_policy
    global _DRIVER_PATCH
    product_policy.reset_for_tests()
    server._binding_facade_holder["facade"] = None
    patch = _pytest.MonkeyPatch()
    patch.setattr(server, "_backend", product_policy.GuardedBackend(
        backend, "windows_pywinauto"))
    patch.setattr(server, "_backend_kind", "windows_pywinauto")
    patch.setattr(server, "_recording_service", _NS(
        status=lambda **kw: {"state": "idle"}))
    set_backend_resolver(lambda: server._backend)
    _DRIVER_PATCH = patch


def _uninstall_fake_os_driver():
    global _DRIVER_PATCH
    if _DRIVER_PATCH is not None:
        _DRIVER_PATCH.undo()
        _DRIVER_PATCH = None


# ---- local-fixture round (real chain, OS-synthetic fact layer) ---------

def run_local_fixture_round(round_no: int, round_dir: Path,
                            *,
                            inject_business_assertion: bool = False,
                            scroll_assertion: bool = False,
                            scroll_fact: str = "scrolled-row",
                            checked_fact=None,
                            drag_assertion: bool = False,
                            drag_fact: str = "dragged-content") -> dict:
    """真实链单轮:真实 RecordingService start→OS 事件捕获源→stop 持久
    raw→真实 compiler→**真实主链回放**(execute_v3_window_stage_real:
    真实 server/policy/binding/dispatcher/账本/业务断言)→精简特征报告。
    不手造 raw/成功回执/passed StepResult;每阶段写自己的 artifact;
    失败 fail-closed。"""
    stages: dict[str, dict] = {}
    from target.recording.service import RecordingService
    from target.recording.source import HookPacket
    from target.recording.session import CaptureScope
    from target.recording.windows import WindowsUIACorrelator

    # ---- 捕获源(OS synthetic 事实层:合成 HookPacket 经真实
    # correlator correlate → session.ingest — 事件协议/归因/范围门
    # 全真实;解析器状态机在 opened 转换后切换前台/控件 — 与 OS
    # 行为一致)。
    scope = CaptureScope(
        "s5-local", "windows_pywinauto", "EDRClient.exe",
        # 容纳本应用全部窗口标题(真实录制 scope 由操作员按应用
        # admitting 配置 — 同应用多窗口域名是既有语义,见
        # "第二进程留在 scope"用例)。
        "^EDRClient$|^logo1$|^Log Center$")

    class _Resolver:
        # 窗口几何(OS 事实层:前台窗口命中 — 与真实 hit test 一致)。
        # 仅可见性变化,不做 state 切换归属:pointer 归属一律由
        # window_at(x,y)/element_at(x,y) 按**包内坐标**几何解析
        # (state 切换会让同一坐标在关联时点前后漂移 — 205/后续
        # 复验已锁:归因是逐包 OS 查询,不是可变预测)。
        # windowTitle 键是 correlator 的窗口身份契约(_with_window
        # 只认 windowTitle;title 是 windows()/seed 枚举的键)。
        _DIALOG = {"handle": 200, "windowTitle": "Log Center", "pid": 42}
        _MAIN = {"handle": 1, "windowTitle": "logo1", "pid": 42}
        # Log Center 对话框覆盖主窗命中区(真实 OS 叠放语义:置顶
        # 对话框截获其矩形内的命中)。
        _DIALOG_RECT = (0, 0, 400, 300)

        def __init__(self):
            self.process_names = ["EDRClient.exe"]
            self.dialog_visible = False
            self.element = {"controlType": "Button",
                            "automationId": "btnApply", "text": "Apply",
                            "rect": [10, 20, 110, 60], "protected": False}

        def windows(self):
            return [{"title": "EDRClient", "pid": 42,
                     "processName": "EDRClient.exe", "handle": 1}]

        def foreground(self):
            return {"processName": "EDRClient.exe",
                    **(self._DIALOG if self.dialog_visible
                       else self._MAIN)}

        def window_at(self, x, y):
            # 命中测试按坐标:对话框可见且坐标落在其矩形内 → W2,
            # 否则 W1(置顶窗口优先 — 真实 OS 叠放语义)。
            if self.dialog_visible:
                l, t, r, b = self._DIALOG_RECT
                if l <= x < r and t <= y < b:
                    return {"processName": "EDRClient.exe",
                            **self._DIALOG}
            return {"processName": "EDRClient.exe", **self._MAIN}

        def cursor_position(self):
            return (50, 40)

        def element_at(self, x, y):
            win = self.window_at(x, y)
            if win.get("handle") == 200:
                return {"controlType": "Row", "automationId": "logRow",
                        "text": "row-1", "rect": [0, 0, 20, 10],
                        "protected": False}
            if 296 <= x < 400 and 96 <= y < 136:
                # dragTarget(主窗内第二控件 — drag 终点;编译器/执行
                # 链都要求双端在同一 drag_commit 窗口内解析)。
                return {"controlType": "Button",
                        "automationId": "dragTarget",
                        "text": "drag-original",
                        "rect": [296, 96, 400, 136], "protected": False}
            return dict(self.element)

        def refresh(self, target):
            return self.element_at(0, 0)

        def focused(self):
            return self.element_at(0, 0)

        def application_process_names(self, process_name):
            return self.process_names

    resolver = _Resolver()
    correlator = WindowsUIACorrelator(resolver)
    correlator.seed_scope(scope)

    def source_factory(_scope, sink):
        class _SyntheticHookSource:
            """OS 事件捕获源替身:合成 HookPacket 经真实 correlator
            correlate → session.ingest(事件协议/归因/范围门/协议真实);
            只 OS 事件层 synthetic,不手造最终 raw。事件在 RECORDING
            状态下经 pump() 送达。真实事件序列:点击(btnApply)→
            opened 转换 → 点击(logRow)。"""

            def __init__(self):
                self.dropped_packets = 0
                self.out_of_scope_events = 0
                self.correlation_errors = []
                # 真实 UIA 树证据(control snapshots — 入口窗口身份)。
                self.control_snapshots = [{
                    "snapshotId": "CS-0001", "capturedAtMs": 90,
                    "reason": "step",
                    "window": {"handle": 1,
                               "processName": "EDRClient.exe",
                               "rootAutomationId": "EdrMainWindow",
                               "title": "logo1"},
                    "contentHash": "sha256:" + "a" * 64,
                    "stateHash": "sha256:" + "b" * 64,
                    "truncated": False, "complete": True,
                    "controlCount": 1,
                    "controls": [{"automationId": "EdrMainWindow",
                                  "controlType": "Window"}]}]
                # session.recording() 从本源读取 correlator 的窗口
                # 注册表/时间线(v2 文档;真实归因事实)。
                self._correlator = correlator
                # 事件单调时间基于本 run 实际时钟(seed 观察携带真实
                # 单调值 — 生命周期区间要求事件事实不早于首次观察)。
                import time as _time
                base = int(_time.monotonic() * 1000)
                # 完整 A→B→A 流(真实捕获语义):click1(btnApply,
                # W1)→ opened 转换(W2 出现)→ click2(logRow,W2 内)
                # → closed 转换(W2 关闭 — 记录的真实关闭事实,经
                # compiler 生成 effects;不是事后硬添)→ click3
                # (btnBack,返回 A)。
                self._packets = [
                    HookPacket(kind="pointer_up", monotonic_ms=base + 100,
                               wall_time="2026-11-05T00:00:01.000Z",
                               screen_point=(50, 40), button="left"),
                    HookPacket(kind="window_transition",
                               monotonic_ms=base + 150,
                               wall_time="2026-11-05T00:00:01.500Z",
                               native={"kind": "opened",
                                       "processName": "EDRClient.exe",
                                       "title": "Log Center", "pid": 42,
                                       "handle": 200}),
                    HookPacket(kind="pointer_up", monotonic_ms=base + 300,
                               wall_time="2026-11-05T00:00:03.000Z",
                               screen_point=(60, 40), button="left"),
                    HookPacket(kind="window_transition",
                               monotonic_ms=base + 400,
                               wall_time="2026-11-05T00:00:04.000Z",
                               native={"kind": "closed",
                                       "processName": "EDRClient.exe",
                                       "title": "Log Center", "pid": 42,
                                       "handle": 200}),
                    HookPacket(kind="pointer_up", monotonic_ms=base + 500,
                               wall_time="2026-11-05T00:00:05.000Z",
                               screen_point=(50, 40), button="left"),
                    *([
                        HookPacket(kind="pointer_down",
                                   monotonic_ms=base + 800,
                                   wall_time="2026-11-05T00:00:08.000Z",
                                   screen_point=(50, 40), button="left"),
                        HookPacket(kind="pointer_up",
                                   monotonic_ms=base + 900,
                                   wall_time="2026-11-05T00:00:09.000Z",
                                   screen_point=(305, 115), button="left"),
                    ] if drag_assertion else []),
                    # scroll 手势放**最后**:scroll_commit 成为最近已录
                    # 动作,其 bindPrevious 断言可折叠(compiler 折叠只认
                    # 前一动作);drag 断言则用 bindCausalId 显式绑定已录
                    # drag 动作(session 校验已录 causal,真实读取不猜)。
                    # 末包 text_activity flush scroll 且不产生事件。
                    *([
                        HookPacket(kind="scroll", monotonic_ms=base + 1000,
                                   wall_time="2026-11-05T00:00:10.000Z",
                                   screen_point=(50, 40),
                                   native={"delta": 120}),
                        HookPacket(kind="scroll", monotonic_ms=base + 1020,
                                   wall_time="2026-11-05T00:00:10.020Z",
                                   screen_point=(50, 40),
                                   native={"delta": 120}),
                        HookPacket(kind="text_activity",
                                   monotonic_ms=base + 1100,
                                   wall_time="2026-11-05T00:00:11.000Z"),
                    ] if scroll_assertion else []),
                ]

            def start(self):
                pass

            def pump(self, max_packets=None):
                """RECORDING 状态下送达合成事件(经真实 correlator/
                session.ingest)。事件序号按到达顺序连续分配(每事件
                恰一次递增;raw v2 要求 1..N)。opened/closed 转换事实
                后指针归属/控件切换(与 OS 前台行为一致 — A→B→A)。"""
                sequence = 0
                fed = 0
                import time as _time
                for packet in self._packets:
                    if max_packets is not None and fed >= max_packets:
                        return
                    # 事件按其记录的单调时刻**真实节奏**送达(OS 事实层
                    # 语义:事件在它发生的时刻到达,不从未来瞬间送完);
                    # 否则随后的管理断言(session._now_ms 真实时钟)会早于
                    # 声称的未来时刻,违反 monotonicMs 非递减不变量。
                    delay_ms = packet.monotonic_ms - int(
                        _time.monotonic() * 1000)
                    if delay_ms > 0:
                        _time.sleep(delay_ms / 1000.0)
                    if packet.kind == "window_transition":
                        kind = (packet.native or {}).get("kind")
                        # opened/closed 转换事实切换对话框可见性(真实
                        # OS 生命周期);指针归属仍由逐包几何命中解析。
                        resolver.dialog_visible = kind == "opened"
                    event = correlator.correlate(packet, scope,
                                                 sequence + 1)
                    fed += 1
                    if event is None:
                        continue
                    for accepted in (event if isinstance(event, tuple)
                                     else (event,)):
                        sequence += 1
                        object.__setattr__(accepted, "sequence", sequence)
                        sink(accepted)

            def pause(self):
                pass

            def resume(self):
                pass

            def stop(self):
                pass

        return _SyntheticHookSource()

    from target.recording.result_store import RecordingResultStore
    # 正常路径也接入既有持久结果链：driver 自配每轮 durable store，stop
    # 把 raw 落成持久 receipt，compile/回放输入从该持久 store 读回 —— 不
    # 只取 stop 内存 dict 手写绕过收集链。
    result_store = RecordingResultStore(round_dir / "result-store")
    service = RecordingService(
        target_name="s5-local", backend="windows_pywinauto",
        source_factory=source_factory, result_store=result_store)

    # ---- stage: recording + stop-collection(真实 service/会话)。
    # RECORDING 会话的 scope 必须与捕获事件的 scope **字符同一**
    # (session.ingest 的既有 scope 全等门:ev.scope == self.scope,
    # 不等即拒;two regex strings 表达同一集合也不相等)。
    started = service.start(name=f"s5-fixture-round-{round_no}",
                            process_name="EDRClient.exe",
                            window_title=scope.window_title,
                            lease_seconds=300)
    if not started.get("ok"):
        stages["recording"] = {"status": "failed",
                               "error": started.get("error")}
        summary = _round_summary(round_no, stages, "recording failed")
        _write_summary(round_dir, summary)
        return summary
    stages["recording"] = {"status": "ran",
                           "sessionId": started.get("sessionId"),
                           "source": "local-fixture (OS events via real "
                                     "correlator/session; not a Windows "
                                     "capture)"}
    # RECORDING 状态下经 pump 送达合成事件(真实 correlator/session.
    # ingest — 归因/scope 门/协议真实;opened 转换后指针归属切换)。
    service.manager.current()._source.pump()
    # 可选独立业务断言(默认关):时间推进后真实时钟已越过末事件预定时刻,
    # 管理断言经真实 session.add_assertion 落在时间线末尾且单调非递减,
    # 经真实 compiler 折叠成与窗口效果分开的 verifier。
    if inject_business_assertion or scroll_assertion or drag_assertion:
        session = service.manager.current()
        if scroll_assertion:
            # scroll 结果 verifier:scroll 是最近已录动作,bindPrevious
            # 因果绑定后经真实 compiler 折入该 scroll_commit 步。期望
            # 值为滚动后应出现的内容(OS 滚动改变 logRow 文本 — fake
            # OS 事实层随滚动更新;求值对**动作后**观测比对)。
            session.add_assertion({
                "assertion": "text_contains",
                "expected": "scrolled-row",
                "automationId": "btnApply",
                "bindPrevious": True,
                "requestId": f"fixture-scroll-{round_no}",
            })
        if inject_business_assertion:
            session.add_assertion({
                "assertion": "checked", "expected": True,
                "automationId": "btnApply",
                "bindPrevious": True,
                "requestId": f"fixture-business-{round_no}",
            })
        if drag_assertion:
            # drag 结果 verifier:从已录会话事件**真实读取** drag_commit
            # 的 causalId 显式绑定(bindCausalId;session 校验必须命中
            # 已录动作)。scroll 放最后成为最近动作,故 drag 不能再用
            # bindPrevious(那会绑到 scroll)。
            drag_causal = next(
                (e.causal_id for e in session._events
                 if e.type == "drag_commit" and e.causal_id), None)
            if drag_causal is None:
                raise RuntimeError(
                    "drag gesture did not produce a drag_commit action")
            session.add_assertion({
                "assertion": "text_contains",
                "expected": "dragged-content",
                "automationId": "dragTarget",
                "bindCausalId": drag_causal,
                "requestId": f"fixture-drag-{round_no}",
            })
    # stop() 内部会调真实捕获源/会话收尾;若源 stop 抛错(如 OSError),
    # service.stop 会传播 —— 必须 here 留存阶段失败并写 summary,不能裸崩。
    try:
        stopped = service.stop()
    except Exception as exc:  # noqa: BLE001 - surfaced as a stage failure
        stages["stop-collection"] = {
            "status": "failed",
            "sessionId": None,
            "error": f"stop raised: {exc}"}
        summary = _round_summary(round_no, stages,
                                 "stop raised (capture/session teardown "
                                 "failed)")
        summary["failures"] = ["stop-collection: stop raised "
                               "unhandled exception"]
        _write_summary(round_dir, summary)
        return summary
    session_id = stopped.get("sessionId")
    # 校验停止**本身**与**持久收集**结果(fail-closed)：stop 未成功，或
    # 配置了 durable store 的停止未把 raw 落成持久 receipt，都不得把
    # "记录停下来了" 冒充 "产物已安全/可编译"。既有服务契约
    # (停止成功/存储失败分开,resultStore.ok=false 上报)不改 —— driver
    # 是校验方。
    if stopped.get("ok") is not True:
        stages["stop-collection"] = {
            "status": "failed", "sessionId": session_id,
            "resultStore": stopped.get("resultStore"),
            "error": stopped.get("error") or "stop did not complete"}
        summary = _round_summary(round_no, stages,
                                 "stop did not complete (ok=false)")
        summary["failures"] = ["stop-collection: stop failed (ok=false)"]
        _write_summary(round_dir, summary)
        return summary
    stop_store = stopped.get("resultStore")
    if stop_store is not None and stop_store.get("ok") is not True:
        stages["stop-collection"] = {
            "status": "failed", "sessionId": session_id,
            "resultStore": stop_store,
            "error": stop_store.get("error") or "durable result store did "
                     "not persist the stopped recording"}
        summary = _round_summary(round_no, stages,
                                 "stop produced no durable result "
                                 "(persist/collection failed)")
        summary["failures"] = ["stop-collection: not collected ("
                               "resultStore.ok=false)"]
        _write_summary(round_dir, summary)
        return summary
    # 从既有持久结果读回 compile 输入(durable 事实源,非内存手写)。读回
    # 失败必须留存阶段错误(non-zero 阻断),不能直接崩溃丢 summary。
    try:
        recording = result_store.load_stopped_recording(session_id)
    except Exception as exc:  # noqa: BLE001 - surfaced as a stage failure
        stages["stop-collection"] = {
            "status": "failed", "sessionId": session_id,
            "resultStore": stop_store,
            "error": f"durable read-back failed for {session_id}: {exc}"}
        summary = _round_summary(round_no, stages,
                                 "durable result could not be read back "
                                 "(collection failed)")
        summary["failures"] = ["stop-collection: durable read-back failed"]
        _write_summary(round_dir, summary)
        return summary
    raw_schema = recording.get("schema")
    raw_path = round_dir / "recording" / "raw.json"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(json.dumps(recording, ensure_ascii=False,
                                   indent=1), encoding="utf-8")
    stages["stop-collection"] = {
        "status": "ran", "artifact": str(raw_path),
        "schema": raw_schema,
        "sessionId": session_id,
        "resultStoreOk": stop_store.get("ok") if stop_store else None,
        "eventCount": len(recording.get("events") or []),
        "windows": [w.get("title") for w in recording.get("windows") or []],
        "coverage": (recording.get("windowTimeline") or {}).get("coverage"),
    }

    # ---- stage: compile(真实编译器;严格 raw v2 reader)。
    from target.recording.models import RawRecording
    if raw_schema != "edr.desktop-recording/v2":
        summary = _round_summary(round_no, stages,
                                 "stop did not produce a raw v2 document")
        _write_summary(round_dir, summary)
        return summary
    raw = RawRecording.from_dict(recording)
    from agent.recording.compiler import compile_recording
    result = compile_recording(raw, profile="windows_hisec")
    golden_dict = result.golden.to_dict()
    golden_path = round_dir / "golden" / "golden.v3.json"
    golden_path.parent.mkdir(parents=True, exist_ok=True)
    golden_path.write_text(json.dumps(golden_dict, ensure_ascii=False,
                                      indent=1), encoding="utf-8")
    (round_dir / "compile-report.json").write_text(
        json.dumps({"status": result.golden.status,
                    "issues": [{"code": i.code, "sequence": i.sequence}
                               for i in result.issues]},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    stages["compile"] = {"status": "ran",
                         "goldenStatus": result.golden.status,
                         "issueCodes": [i.code for i in result.issues]}
    if result.golden.status != "ready":
        summary = _round_summary(round_no, stages,
                                 "compile did not reach ready")
        _write_summary(round_dir, summary)
        return summary

    # ---- stage: replay(真实主链 execute_v3_window_stage_real —
    # 真实 server/policy/binding/dispatcher/账本;OS 事实层 synthetic)。
    from agent.recording import execution_ledger
    from target.windowing import attempt_ledger
    import pytest as _pytest
    from types import SimpleNamespace as _NS

    windows = {
        # 窗口根控件与录制声明控件一致(W1=btnApply/W2=logRow;
        # dump_tree 投影 root_control_id/class/text)。dragTarget 是
        # W1 内第二控件(extra_control)— drag 双端在**同一窗口**
        # 内(编译器 end selector 与执行链端点解析都取 drag_commit
        # 自身窗口的投影控件,独立 W300 不可能编译)。
        "100": {"process_id": 42, "title": "logo1",
                "root_control_id": "btnApply",
                "root_class_name": "Button", "root_text": "Apply",
                "visible": True, "foreground": True,
                "extra_controls": [{"automation_id": "dragTarget",
                                    "class_name": "Button",
                                    "text": "drag-original"}],
                "extra_checked": False},
        "200": {"process_id": 42, "title": "Log Center",
                "root_control_id": "logRow",
                "root_class_name": "Row", "root_text": "row-1",
                "visible": False, "foreground": True,
                "process_name": "EDRClient.exe"},
    }
    FakeOSBackend = _make_fake_os_backend(windows)
    _init_fact = {"scroll": scroll_fact,
                  "checked": checked_fact if checked_fact is not None
                  else True}
    FakeOSBackend = (lambda cls, facts: type(
        cls.__name__, (cls,), {"__init__": lambda s: cls.__init__(
            s, scroll_fact=facts["scroll"],
            checked_fact=facts["checked"])}))(
        FakeOSBackend, _init_fact)
    original_click = FakeOSBackend.click

    def click_applies_flow(self, **kwargs):
        out = original_click(self, **kwargs)
        # 录制流程语义:btnApply 点击打开 Log Center(appeared),
        # logRow 点击(Log Center 内)关闭它(absent)— A→B→A。
        if kwargs.get("automation_id") == "btnApply":
            self._windows["200"]["visible"] = True
        elif kwargs.get("automation_id") == "logRow":
            self._windows["200"]["visible"] = False
        return out

    FakeOSBackend.click = click_applies_flow
    # drag 事实层:fake drag 更新终点控件文本(drag_fact — 拖拽语义
    # 的结果事实;正例默认与断言期望一致;负例注入方经 drag_fact 注入
    # 不同的 OS 事实 — 期望不变、事实变,真实 evaluator 必判 fail)。
    FakeOSBackend._drag_fact = drag_fact

    def drag(self, x1, y1, x2, y2, duration=0.25, **kwargs):
        self.dispatch_calls.append({"method": "drag", "x1": x1, "y1": y1,
                                    "x2": x2, "y2": y2})
        self._windows["100"]["extra_controls"] = [
            {"automation_id": "dragTarget", "class_name": "Button",
             "text": self._drag_fact}]
        return {"ok": True, "method": "dragTo",
                "start": {"x": x1, "y": y1}, "end": {"x": x2, "y": y2}}

    FakeOSBackend.drag = drag
    backend = FakeOSBackend()
    import target.server as _server
    _install_fake_os_driver(backend)
    try:
        ledger = attempt_ledger.AttemptLedger(round_dir / "replay" / "events")
        window_role_map = {}
        for window in golden_dict["windowSequence"]["windows"]:
            regex = (window.get("identity") or {}).get("titleRegex", "")
            # 双重反转义(to_dict 的 JSON 转义 + regex 字面转义)后
            # 比对产品角色标题词;已装角色域校验由适配器真实执行。
            decoded = re.sub(r"\\(.)", r"\1", regex)
            decoded = re.sub(r"\\(.)", r"\1", decoded)
            window_role_map[window["id"]] = (
                "log_center" if re.search("Log Center|日志中心", decoded)
                else "main")  # EDRClient/logo1 均属 main 角色域
        replay_result = execution_ledger.execute_v3_window_stage_real(
            golden=result.golden, ledger=ledger, server_tools=_server,
            window_role_map=window_role_map)
    finally:
        _uninstall_fake_os_driver()
    stages["replay"] = {
        "status": "ran", "runId": replay_result["runId"],
        "serverRunId": replay_result.get("serverRunId"),
        "dispatchInputs": replay_result["dispatchInputs"],
        "latched": replay_result["latched"],
        "blockedAt": replay_result["blockedAt"],
        "source": "execute_v3_window_stage_real (real server/policy/"
                  "binding/dispatcher/ledger; OS facts synthetic; "
                  "local-fixture, not a Windows capture)"}

    # ---- stage: business-assertions(真实求值结果 — 来自真实链的
    # 断言引用;不合成)。
    history = attempt_ledger.AttemptLedger(
        round_dir / "replay" / "events").rebuild_history(
        run_id=replay_result["runId"])
    assertion_rows = [r for e in history
                      for r in (e.get("assertionResultRefs") or [])]
    stages["business-assertions"] = {
        "status": "ran",
        "assertionCount": len(assertion_rows),
        "failed": [r["assertionId"] for r in assertion_rows
                   if r.get("result") != "pass"],
        "note": "业务断言经真实 evaluator 在动作后权威观测求值"}

    # ---- stage: traceable-report(精简特征报告 — A 片投影)。
    report = replay_result["report"]
    report_path = round_dir / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False,
                                      indent=1, default=str),
                           encoding="utf-8")
    stages["traceable-report"] = {
        "status": "ran", "artifact": str(report_path),
        "outcome": report["outcome"],
        "failedStep": (report["failedStep"] or {}).get("stepId"),
        "screenshots": "无图(本地轮不生成图片,不冒充实机;按需引用)"}

    # ---- fail-closed 汇总:任一必要项 fail/unknown/未运行 → 非零。
    failures = []
    for stage_name in ROUND_STAGES:
        stage = stages.get(stage_name) or {}
        if stage.get("status") != "ran":
            failures.append(f"{stage_name}: not ran")
    if replay_result["latched"] or replay_result["blockedAt"]:
        failures.append(f"replay latched at {replay_result['blockedAt']}")
    if report["outcome"] != "pass":
        failures.append(f"report outcome={report['outcome']}")
    if any(r.get("result") != "pass" for r in assertion_rows):
        failures.append("business assertions not all pass")
    if failures:
        summary = _round_summary(round_no, stages, "; ".join(failures))
        summary["failures"] = failures
        _write_summary(round_dir, summary)
        return summary
    summary = _round_summary(round_no, stages, None)
    _write_summary(round_dir, summary)
    return summary


def _round_summary(round_no, stages, failure_note):
    summary = {
        "round": round_no,
        "mode": "local-fixture",
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "stages": stages,
        "capability": _fail_closed_note(),
        "acceptance": {
            "three_rounds_required": True,
            "injected_defects_must_fail_correctly": True,
            "local_fixture_is_not_windows_evidence": True,
        },
    }
    if failure_note:
        summary["failure"] = failure_note
    return summary


def _write_summary(round_dir, summary):
    round_dir.mkdir(parents=True, exist_ok=True)
    (round_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1, default=str),
        encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--round", type=int, default=1)
    parser.add_argument("--local-fixture", action="store_true",
                        help="run the REAL chain end-to-end on the local "
                             "chain (raw v2 -> compile -> main-chain replay "
                             "-> assertions -> report); artifacts labeled "
                             "fixture")
    parser.add_argument("--drag-assertion", action="store_true",
                        help="inject a drag flow + bound result verifier "
                             "(opt-in; default off)")
    parser.add_argument("--live", action="store_true",
                        help="run against the real Windows target; requires "
                             "explicit user authorization in the "
                             "supervising chat (refused otherwise)")
    parser.add_argument("--business-assertion", action="store_true",
                        help="opt-in: inject one independent business "
                             "(non-window control) assertion via the real "
                             "session.add_assertion at stop time; default "
                             "off keeps the locked window-only round")
    parser.add_argument("--scroll-assertion", action="store_true",
                        help="opt-in: synthesize a real scroll gesture "
                             "(wheel packets through the real correlator) "
                             "bound to a text_contains result verifier")
    parser.add_argument("--three-rounds", action="store_true",
                        help="run three fresh local-fixture rounds "
                             "(independent session/run/directory); any "
                             "single-round failure exits non-zero")
    args = parser.parse_args()
    if args.live:
        print(json.dumps({
            "status": "refused",
            "reason": "no live Windows authorization in this session; "
                      "the user must authorize the exact target and "
                      "actions first; additionally the production backend "
                      "must implement reliable per-run instance tracking "
                      "and declare window_sequence/v1 (currently "
                      "unsupported — fail closed)",
        }, ensure_ascii=False))
        return 3
    round_dir = EVIDENCE_ROOT / f"round-{args.round}"
    if round_dir.exists() and any(round_dir.iterdir()):
        # 旧原件不可覆盖(fresh round/run/session/目录)。
        print(json.dumps({
            "status": "refused",
            "reason": f"round-{args.round} directory already has "
                      "artifacts; use a fresh --round number (evidence "
                      "protection)",
        }, ensure_ascii=False))
        return 4
    flags = dict(
        inject_business_assertion=args.business_assertion,
        scroll_assertion=args.scroll_assertion,
        drag_assertion=args.drag_assertion)
    if args.three_rounds:
        # 三轮 fresh 本地 fixture:每轮独立 session/run/目录(目录由
        # run_local_fixture_round 内部编号),单轮失败即整体非零
        # (fail-closed;不重试、不复用)。
        results = []
        for n in (args.round, args.round + 1, args.round + 2):
            rdir = EVIDENCE_ROOT / f"round-{n}"
            if rdir.exists() and any(rdir.iterdir()):
                print(json.dumps({
                    "status": "refused",
                    "reason": f"round-{n} directory already has artifacts; "
                              "use a fresh --round number (evidence "
                              "protection)"}, ensure_ascii=False))
                return 4
            s = run_local_fixture_round(n, rdir, **flags)
            fails = s.get("failures") or ([s["failure"]]
                                          if s.get("failure") else [])
            results.append({"round": n, "failures": fails,
                            "stages": {k: v.get("status")
                                       for k, v in s["stages"].items()}})
            if fails:
                break
        print(json.dumps({"status": ("three-rounds-complete"
                                     if all(not r["failures"]
                                            for r in results)
                                     else "three-rounds-failed"),
                          "rounds": results}, ensure_ascii=False))
        return 0 if all(not r["failures"] for r in results) else 5
    summary = run_local_fixture_round(
        args.round, round_dir, **flags)
    failures = summary.get("failures") or ([summary["failure"]]
                                          if summary.get("failure") else [])
    print(json.dumps({"status": ("local-fixture-complete"
                                 if not failures else
                                 "local-fixture-failed"),
                      "roundDir": str(round_dir),
                      "failures": failures,
                      "stages": {k: v.get("status")
                                 for k, v in summary["stages"].items()}},
                     ensure_ascii=False))
    # 任一必要阶段 fail/unknown/skipped/未运行 → 非零退出(fail-closed)。
    return 0 if not failures else 5


if __name__ == "__main__":
    sys.exit(main())
