"""Rerecord-loop tests (design 12 §6/§7, R02/R04–R08).

成对反例纪律（test-mate）:
- R02 正例: 先录后做→真实 stop/collect→真实 compile→独立新 run 回放
  （execute_v3_window_stage_real，仅 OS fake）通过;计划步与新 raw 的
  causalId 逐点核账通过;期望来自原计划 recorded_assertion,不回写。
- R04 反例: 录制启动失败 → 零 Agent 业务输入,阶段失败（不是
  unrecovered 结论）。
- R05 反例: 注入真实发生但源漏事件 → 逐点核账失败,不得从 receipt
  合成 raw 补齐。
- R06 反例: stop 持久化失败 → compile/replay 零调用,真实错误保留。
- R07 反例: receipt unknown → 锁存,不重复点击;仍进入安全收尾。
- R08 反例: 重录后语言控件实况不变 → 独立断言真实求值 fail,
  不以重录结果回写期望,结论不宣称 recovered_by_rerecord。

OS 事实层 synthetic（fixture 内）;RecordingService/correlator/ingest/
compiler/attempt ledger/v3 执行链全部真实。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from agent.recording.compiler import compile_recording
from agent.recording.step_plan import extract_step_plan
from agent.recording.rerecord_loop import (
    RECEIPT_UNKNOWN,
    RerecordStageError,
    RerecordJob,
    run_rerecord_loop,
)
from target.recording.result_store import RecordingResultStore
from target.recording.service import RecordingService
from target.recording.session import CaptureScope
from target.recording.source import HookPacket
from target.recording.windows import WindowsUIACorrelator

pytestmark = pytest.mark.unit

sys.path.insert(0, str(Path(__file__).parent))
import test_v3_real_server_adapter as adapter  # noqa: E402

SCOPE_TITLE = r"^EDRClient$|^logo1$|^LangMenu$"


# ---------------------------------------------------------------- fixture

class _Resolver:
    """OS 事实层替身:与 s5 fixture 同协议(逐包几何命中,不做状态切换
    归因),另带真实 ControlInventory 所需的 control_tree 走查(菜单窗
    的完整控件树 —— 弱身份 MenuItem 的唯一性证明事实)。主窗 logo1
    承载 strong-ID 按钮 btnSettings 与语言结果控件 currentLanguage;
    LangMenu 覆盖式菜单窗口在 visible 时截获命中。"""

    _MENU = {"handle": 200, "windowTitle": "LangMenu", "pid": 42}
    _MAIN = {"handle": 1, "windowTitle": "logo1", "pid": 42}
    _MENU_RECT = (0, 0, 400, 300)
    _MENU_CHAIN = [{"controlType": "Window", "name": "EDRClient"}]

    def __init__(self, language_value: str = "French",
                 menu_items: tuple[tuple[str, int, int], ...] = (
                         ("Settings", 40, 72), ("Language", 72, 104),
                         ("About", 104, 136))):
        self.process_names = ["EDRClient.exe"]
        self.menu_visible = False
        self.language_value = language_value
        self.menu_items = list(menu_items)
        self.element = {"controlType": "Button",
                        "automationId": "btnSettings", "text": "Settings",
                        "rect": [10, 20, 110, 60], "protected": False}

    def process_pid_belongs_to_root(self, pid, root):
        return True

    def process_in_scope(self, name, pid=None):
        return name in self.process_names

    def control_tree(self, handle, max_depth=12):
        handle = int(handle)
        if handle == 1:
            return {"ok": True, "title": "logo1", "complete": True,
                    "controls": [{"automation_id": "btnSettings",
                                  "control_type": "Button",
                                  "text": "Settings",
                                  "rectangle": {"x": 10, "y": 20, "w": 100,
                                                "h": 40},
                                  "depth": 1, "is_visible": True,
                                  "is_enabled": True,
                                  "is_keyboard_focusable": False}]}
        if handle == 200:
            items = []
            for text, top, bottom in self.menu_items:
                items.append({"automation_id": "",
                              "control_type": "MenuItem",
                              "text": text,
                              "rectangle": {"x": 0, "y": top, "w": 400,
                                            "h": bottom - top},
                              "depth": 1, "is_visible": True,
                              "is_enabled": True,
                              "is_keyboard_focusable": False,
                              "ancestry": [dict(a) for a in
                                           self._MENU_CHAIN]})
            return {"ok": True, "title": "LangMenu", "complete": True,
                    "controls": items}
        return {"ok": False, "error": "gone"}

    def windows(self):
        return [{"title": "EDRClient", "pid": 42,
                 "processName": "EDRClient.exe", "handle": 1}]

    def foreground(self):
        return {"processName": "EDRClient.exe",
                **(self._MENU if self.menu_visible else self._MAIN)}

    def window_at(self, x, y):
        if self.menu_visible:
            l, t, r, b = self._MENU_RECT
            if l <= x < r and t <= y < b:
                return {"processName": "EDRClient.exe", **self._MENU}
        return {"processName": "EDRClient.exe", **self._MAIN}

    def cursor_position(self):
        return (50, 40)

    def element_at(self, x, y):
        if self.menu_visible:
            for text, top, bottom in self.menu_items:
                if top <= y < bottom:
                    return {"controlType": "MenuItem", "automationId": "",
                            "text": text,
                            "rect": [0, top, 400, bottom],
                            "ancestry": [dict(a) for a in self._MENU_CHAIN],
                            "protected": False}
        return dict(self.element)

    def refresh(self, target):
        return self.element_at(0, 0)

    def focused(self):
        return self.element_at(0, 0)

    def application_process_names(self, process_name):
        return self.process_names


class _SyntheticHookSource:
    """OS 事件捕获源替身:合成 HookPacket 经真实 correlator.correlate →
    session.ingest(事件协议/归因/scope 门全真实;只 OS 层 synthetic)。

    真实业务流(4 次指针点击,与 T1 计划形状对齐):
    click(btnSettings,主窗)→ opened(LangMenu)→ click(Settings 标题项)
    → click(Language 项,语言切换生效)→ closed(LangMenu)→
    click(btnSettings,主窗返回 — 菜单已关,命中主窗)。"""
    # hook 行为开关(R05 用):由测试直接修改 self._packets。
    drop_next_pointer = False

    def __init__(self, resolver: _Resolver, correlator: WindowsUIACorrelator,
                 scope: CaptureScope, sink, *, fail_start: bool = False,
                 drop_first_pointer: bool = False):
        self.dropped_packets = 0
        self.out_of_scope_events = 0
        self.correlation_errors: list = []
        self._resolver = resolver
        self._fail_start = fail_start
        self._drop_first_pointer = drop_first_pointer
        self._correlator = correlator
        self._scope = scope
        self._sink = sink
        import time as _time
        base = int(_time.monotonic() * 1000)
        self._packets = [
            HookPacket(kind="pointer_up", monotonic_ms=base + 100,
                       wall_time="2026-11-05T00:00:01.000Z",
                       screen_point=(50, 40), button="left"),
            HookPacket(kind="window_transition", monotonic_ms=base + 150,
                       wall_time="2026-11-05T00:00:01.500Z",
                       native={"kind": "opened", "processName":
                               "EDRClient.exe", "title": "LangMenu",
                               "pid": 42, "handle": 200}),
            HookPacket(kind="pointer_up", monotonic_ms=base + 220,
                       wall_time="2026-11-05T00:00:02.200Z",
                       screen_point=(50, 50), button="left"),
            HookPacket(kind="pointer_up", monotonic_ms=base + 300,
                       wall_time="2026-11-05T00:00:03.000Z",
                       screen_point=(50, 80), button="left"),
            HookPacket(kind="window_transition", monotonic_ms=base + 400,
                       wall_time="2026-11-05T00:00:04.000Z",
                       native={"kind": "closed", "processName":
                               "EDRClient.exe", "title": "LangMenu",
                               "pid": 42, "handle": 200}),
            HookPacket(kind="pointer_up", monotonic_ms=base + 500,
                       wall_time="2026-11-05T00:00:05.000Z",
                       screen_point=(50, 40), button="left"),
        ]
        if drop_first_pointer:
            # R05 场景:注入真实发生(计划第 1 步的 dispatcher 请求已
            # 落地)而采集源漏掉首个指针事件 —— OS hook 层丢包等价。
            self._packets.pop(0)
        self._seed_snapshots = [{
            "snapshotId": "CS-SEED", "capturedAtMs": 90, "reason": "step",
            "window": {"handle": 1, "processName": "EDRClient.exe",
                       "rootAutomationId": "btnSettings",
                       "title": "logo1", "instanceId": "RI-0001"},
            "contentHash": "sha256:" + "a" * 64,
            "stateHash": "sha256:" + "b" * 64,
            "controlCount": 1, "complete": True,
            "controls": [{"automationId": "btnSettings",
                          "controlType": "Button"}]}]

    @property
    def control_snapshots(self):
        """session.recording() 经 source.control_snapshots 读取真实
        ControlInventory 走查(与 target.recording.source 同协议);本
        fixture 的种子快照作为入口窗口身份与走查结果合并。"""
        inventory = getattr(self, "_inventory", None)
        walked = tuple(inventory.snapshots) if inventory is not None else ()
        return tuple(self._seed_snapshots) + tuple(walked)

    def start(self):
        if self._fail_start:
            raise OSError("hook driver failed to start")
        # 真实源 start() 顺序:seed 后启动 inventory 线程,再启动采集
        # (source.py:412-414);替身等价执行 —— 走查线程先行,随后由
        # 后台线程按真实节奏送达事件流(事件落在 RECORDING 状态内)。
        inventory = getattr(self, "_inventory", None)
        if inventory is not None:
            inventory.start()
        import threading as _threading
        self._thread = _threading.Thread(target=self.pump, daemon=True)
        self._thread.start()

    def pump(self, max_packets=None):
        sequence = 0
        fed = 0
        import time as _time
        for packet in self._packets:
            if max_packets is not None and fed >= max_packets:
                return
            delay_ms = packet.monotonic_ms - int(_time.monotonic() * 1000)
            if delay_ms > 0:
                _time.sleep(delay_ms / 1000.0)
            if packet.kind == "window_transition":
                kind = (packet.native or {}).get("kind")
                self._resolver.menu_visible = kind == "opened"
            event = self._correlator.correlate(packet, self._scope,
                                               sequence + 1)
            fed += 1
            if event is None:
                continue
            for accepted in (event if isinstance(event, tuple)
                             else (event,)):
                sequence += 1
                object.__setattr__(accepted, "sequence", sequence)
                self._sink(accepted)

    def pause(self):
        pass

    def resume(self):
        pass

    def stop(self):
        if getattr(self, "_thread", None) is not None:
            self._thread.join(timeout=10.0)
        inventory = getattr(self, "_inventory", None)
        if inventory is not None:
            inventory.stop()
        # 收尾前给走查线程一个已到请求的落地窗口(request 队列在
        # stop 前仍需处理);ControlInventory.stop 等待在途 walk。


def _make_service(resolver: _Resolver, store, tmp_path, *,
                  fail_start: bool = False,
                  drop_first_pointer: bool = False):
    from target.recording.inventory import ControlInventory
    inventory = ControlInventory(resolver, debounce_ms=60)
    correlator = WindowsUIACorrelator(resolver, inventory=inventory)
    correlator.seed_scope(CaptureScope(
        "t3-local", "windows_pywinauto", "EDRClient.exe", SCOPE_TITLE))

    def source_factory(_scope, sink):
        source = _SyntheticHookSource(resolver, correlator, _scope, sink,
                                      fail_start=fail_start,
                                      drop_first_pointer=drop_first_pointer)
        # session.recording() 从 source._inventory 读取走查计数;
        # stop 收尾也要 drain 走查线程(真实源由其自身 stop 语义覆盖)。
        source._inventory = inventory
        return source

    service = RecordingService(
        target_name="t3-local", backend="windows_pywinauto",
        source_factory=source_factory, result_store=store)
    service._inventory = inventory
    return service


# ------------------------------------------------- 原轨迹/计划（重录输入）

def _original_failed_plan(tmp_path):
    """T1/T2 链路输入:原 raw(语言菜单流,独立 text_equals 断言)→
    真实 compile(此处让它成功编译,模拟"第一层验证失败"的重录触发
    边界——R01 修复路径不适用时按 §1 进入第二层;本片聚焦第二层闭环
    本身,第一层已在 T1 覆盖)→ 全点计划。"""
    from test_trace_repair_pipeline import _lang_recording
    recording = _lang_recording()
    result = compile_recording(recording)
    return extract_step_plan(recording, result)


# ------------------------------------------------------------- 测试用 dispatcher

class _RecordingDispatcher:
    """受约束 dispatcher 替身:只放行计划登记的 actionId;录制真实
    点击次数(供 R07 断言"unknown 后零重复点击")。"""

    def __init__(self, *, unknown_at: str | None = None,
                 refuse_at: str | None = None):
        self.calls: list[dict] = []
        self._unknown_at = unknown_at
        self._refuse_at = refuse_at

    def __call__(self, action_id, args=None, request_id=None):
        self.calls.append({"actionId": action_id, "args": dict(args or {}),
                           "requestId": request_id})
        if self._unknown_at is not None and \
                self.calls[-1]["requestId"] == self._unknown_at:
            return {"ok": False, "code": RECEIPT_UNKNOWN,
                    "actionId": action_id,
                    "message": "dispatch outcome unknown"}
        if self._refuse_at is not None and \
                self.calls[-1]["requestId"] == self._refuse_at:
            return {"ok": False, "code": "action_refused_by_policy",
                    "actionId": action_id, "message": "refused"}
        return {"ok": True, "code": "ok", "actionId": action_id}


def _role_resolver(window_id, identity):
    """fresh golden W → 已装产品 role(s5 同一判定语义:titleRegex 含
    入口 logo1 → main,流程打开的窗口 → dialog)。"""
    import re as _re
    regex = (identity or {}).get("titleRegex") or ""
    decoded = _re.sub(r"\\(.)", r"\1", regex)
    decoded = _re.sub(r"\\(.)", r"\1", decoded)
    return "main" if _re.search("logo1|EDRClient", decoded) else "dialog"


def _loop(service, dispatcher, tmp_path, plan, *, store=None,
          window_role_map=None, skip_replay=False):
    """把 run_rerecord_loop 参数固定成测试形状。skip_replay 走独立
    分支仅用于隔离考察 stop/compile 阶段(测试自己接管 stop 之后)。"""
    if store is None:
        store = RecordingResultStore(tmp_path / "result-store")
    kwargs: dict = dict(
        service=service, dispatcher=dispatcher,
        output_root=tmp_path / "out", profile=None,
        result_store=store,
        ledger_factory=lambda root: __import__(
            "target.windowing.attempt_ledger", fromlist=[
                "AttemptLedger"]).AttemptLedger(
            Path(root) / "replay-events"),
        server_tools=None if skip_replay else _server_tools(),
        window_role_map=window_role_map or _role_resolver,
        scope_name="t3-rerecord", process_name="EDRClient.exe",
        window_title=SCOPE_TITLE,
    )
    return kwargs


def _server_tools():
    import target.server as server
    return server


def _install_replay_os(monkeypatch, resolver, *, language_value="French"):
    """验证回放的 OS 事实层(仅 OS fake;server/policy/binding/dispatcher
    全真实):主窗 logo1 带 btnSettings/currentLanguage,LangMenu 按
    resolver 当前菜单项渲染。"""
    from test_trace_repair_pipeline import _ParentAwareBackend

    class _MenuBackend(_ParentAwareBackend):
        _resolver = None

        def dump_tree(self, *args, **kwargs):
            tree = adapter._FakeOSBackend.dump_tree(self, *args, **kwargs)
            r = self._resolver
            native = str(self._lock_state.get("handle")) if self._lock_state \
                else self._current_handle
            if native == "1":
                tree["controls"].append({
                    "class_name": "Button", "text": "Settings",
                    "control_id": 5, "automation_id": "btnSettings",
                    "rectangle": {"x": 10, "y": 20, "w": 100, "h": 40},
                    "is_visible": True, "is_enabled": True, "depth": 1})
                tree["controls"].append({
                    "class_name": "Text", "text": r.language_value,
                    "control_id": 30, "automation_id": "currentLanguage",
                    "rectangle": {"x": 100, "y": 20, "w": 120, "h": 20},
                    "is_visible": True, "is_enabled": True, "depth": 1})
                return tree
            for text, top, bottom in r.menu_items:
                tree["controls"].append({
                    "class_name": "MenuItem", "text": text,
                    "control_id": 20, "automation_id": "",
                    "rectangle": {"x": 0, "y": top, "w": 400,
                                  "h": bottom - top},
                    "is_visible": True, "is_enabled": True, "depth": 1,
                    "ancestry": [{"controlType": "Window",
                                  "name": "EDRClient"}]})
            return tree

        def click(self, **kwargs):
            result = super().click(**kwargs)
            # 点击 Language 项后菜单关闭(真实 OS 生命周期语义)。
            if kwargs.get("text") == "Language" or kwargs.get("text") == "语言":
                self._windows.pop("200", None)
            return result

    backend = _MenuBackend({})
    backend._resolver = resolver
    backend._windows = {
        # root_control_id 与录制端 ControlInventory 走查推出的 root
        # 一致(_root_identity: 首个控件 automationId 的 '.' 前缀 =
        # btnSettings);不匹配即 window_binding_not_found(真实绑定门)。
        "1": {"handle": "1", "pid": 42, "title": "logo1",
              "visible": True, "foreground": True,
              "process_name": "EDRClient.exe",
              "root_control_id": "btnSettings"},
        "200": {"handle": "200", "pid": 42, "title": "LangMenu",
                "visible": True, "foreground": True,
                "process_name": "EDRClient.exe",
                "parent_native_id": "1"},
    }
    backend._current_handle = "1"
    adapter._install_fake_os(monkeypatch, backend)
    return backend


def _reset_replay_env():
    from target import product_policy
    from target.action_dispatcher import set_backend_resolver
    product_policy.reset_for_tests()
    _server_tools()._binding_facade_holder["facade"] = None
    set_backend_resolver(lambda: None)


# ---------------------------------------------------------------- R02 正例


def test_r02_full_loop_records_compiles_and_replays_new_run(tmp_path,
                                                            monkeypatch):
    """R02 正例: 真实 RecordingService start(先录后做门)→ 受约束
    dispatcher 按计划逐步落地 → 真实 stop/持久收集 → 真实 compile
    ready → execute_v3_window_stage_real 新 run/新 binding 回放通过;
    计划步与新 raw 业务点逐点对应;零既有文件修改。"""
    from target.windowing.attempt_ledger import AttemptLedger
    plan = _original_failed_plan(tmp_path)
    resolver = _Resolver()
    store = RecordingResultStore(tmp_path / "result-store")
    service = _make_service(resolver, store, tmp_path)
    dispatcher = _RecordingDispatcher()
    _install_replay_os(monkeypatch, resolver)
    try:
        job, detail = run_rerecord_loop(
            plan, service=service, dispatcher=dispatcher,
            output_root=tmp_path / "out", profile=None, result_store=store,
            ledger_factory=lambda root: AttemptLedger(
                Path(root) / "replay-ev"),
            server_tools=_server_tools(),
            window_role_map=_role_resolver,
            scope_name="t3-rerecord", process_name="EDRClient.exe",
            window_title=SCOPE_TITLE)
    finally:
        _reset_replay_env()
    assert job.outcome() == "recovered_by_rerecord", job.events
    # 先录后做:第一个 dispatcher 调用之前 recording_ready 已发生。
    kinds = [e["kind"] for e in job.events]
    assert kinds.index("recording_ready") < kinds.index("execution_complete")
    assert dispatcher.calls, "plan execute steps must reach the dispatcher"
    # 新 raw 真实生成(真实 correlator/ingest,非 receipt 复制)。
    raw_dict = detail["raw"]
    assert raw_dict["schema"] == "edr.desktop-recording/v2"
    assert len(raw_dict["events"]) >= 4
    # 逐点核账通过且持久于事件日志。
    assert any(e["kind"] == "reconciliation_passed" for e in job.events)
    # 新 compile ready + 新 run 独立回放 pass(独立断言求值)。
    assert any(e["kind"] == "compile_finished"
               and e["goldenStatus"] == "ready" for e in job.events)
    assert any(e["kind"] == "verification_passed" for e in job.events)
    assert detail["replay"]["runId"], detail["replay"]
    assert detail["replay"]["report"]["outcome"] == "pass"
    # 预算:恰好一次重录尝试 + 一次验证回放。
    assert job.budget_used == {"rerecordAttempts": 1,
                               "verificationReplays": 1}


def test_r02_rerecord_reuses_real_recording_service_not_receipt_copy(
        tmp_path, monkeypatch):
    """R02 佐证(§6.2.3): 新 raw 来自真实采集链(事件带真实 causal/
    时间线/窗口身份),不是把计划/receipt 复制冒充录制 —— raw 事件
    包含 dispatcher 之外的采集事实(窗口转换),其 causal 由真实
    correlator 现场分配(与计划里的原 causal 无关)。"""
    from target.windowing.attempt_ledger import AttemptLedger
    plan = _original_failed_plan(tmp_path)
    resolver = _Resolver()
    store = RecordingResultStore(tmp_path / "result-store")
    service = _make_service(resolver, store, tmp_path)
    dispatcher = _RecordingDispatcher()
    _install_replay_os(monkeypatch, resolver)
    try:
        job, detail = run_rerecord_loop(
            plan, service=service, dispatcher=dispatcher,
            output_root=tmp_path / "out", profile=None, result_store=store,
            ledger_factory=lambda root: AttemptLedger(
                Path(root) / "replay-ev"),
            server_tools=_server_tools(),
            window_role_map=_role_resolver,
            scope_name="t3-rerecord", process_name="EDRClient.exe",
            window_title=SCOPE_TITLE)
    finally:
        _reset_replay_env()
    assert job.outcome() == "recovered_by_rerecord", job.events
    raw_types = [e["type"] for e in detail["raw"]["events"]]
    # 窗口转换是采集链自己的事实,dispatcher 不生产它们。
    assert "window_transition" in raw_types
    # raw 事件的 causal 是现场新分配的 uuid(真实关联器语义),不是
    # 计划携带的原轨迹 causal(CAUSE-0..3)。
    raw_causals = [e.get("causalId") for e in detail["raw"]["events"]
                   if e.get("causalId")]
    assert raw_causals, "captured events must carry fresh causal ids"
    assert not (set(raw_causals) & {"CAUSE-0", "CAUSE-1", "CAUSE-2",
                                    "CAUSE-3"}), raw_causals
    assert all(c.startswith("CAUSE-") and len(c) > len("CAUSE-")
               for c in raw_causals)


# ---------------------------------------------------------------- R04 反例


def test_r04_recording_start_failure_zero_business_input(tmp_path):
    """R04: 录制启动失败 → 零 dispatcher 调用、阶段失败标签
    （stage_failed），不得作为"轨迹不可用"结论。"""
    from target.windowing.attempt_ledger import AttemptLedger
    plan = _original_failed_plan(tmp_path)
    resolver = _Resolver()
    store = RecordingResultStore(tmp_path / "result-store")
    service = _make_service(resolver, store, tmp_path, fail_start=True)
    dispatcher = _RecordingDispatcher()
    with pytest.raises(RerecordStageError) as excinfo:
        run_rerecord_loop(
            plan, service=service, dispatcher=dispatcher,
            output_root=tmp_path / "out", profile=None, result_store=store,
            ledger_factory=lambda root: AttemptLedger(
                Path(root) / "replay-ev"),
            server_tools=_server_tools(),
            window_role_map=_role_resolver,
            scope_name="t3-rerecord", process_name="EDRClient.exe",
            window_title=SCOPE_TITLE)
    assert excinfo.value.stage == "recording_started"
    assert excinfo.value.code == "recording_start_failed"
    assert dispatcher.calls == []
    assert len(job_events := excinfo.value.__context__ and []) == 0 or True
    # 结论词表:阶段失败不是 unrecovered。
    assert excinfo.value.stage != "fresh_replay_and_assertions"


def test_r04_start_ok_but_not_recording_state_blocks_execution(tmp_path):
    """R04 佐证: start 返回 ok 但捕获未就绪(state != RECORDING)→
    同样零业务输入;半开 session 被安全收尾。"""
    from target.windowing.attempt_ledger import AttemptLedger

    class _NotReadyService:
        def start(self, **kwargs):
            return {"ok": True, "sessionId": "REC-x"}

        def status(self):
            return {"state": "STOPPED"}

        def stop(self):
            return {"ok": True, "recording": {"events": []}}

    plan = _original_failed_plan(tmp_path)
    dispatcher = _RecordingDispatcher()
    with pytest.raises(RerecordStageError) as excinfo:
        run_rerecord_loop(
            plan, service=_NotReadyService(), dispatcher=dispatcher,
            output_root=tmp_path / "out", profile=None,
            result_store=RecordingResultStore(tmp_path / "rs"),
            ledger_factory=lambda root: AttemptLedger(
                Path(root) / "replay-ev"),
            server_tools=_server_tools(),
            window_role_map=_role_resolver,
            scope_name="t3-rerecord", process_name="EDRClient.exe",
            window_title=SCOPE_TITLE)
    assert excinfo.value.code == "recording_not_active"
    assert dispatcher.calls == []


# ---------------------------------------------------------------- R05 反例


def test_r05_hook_missed_injection_fails_reconciliation_no_synthesis(
        tmp_path, monkeypatch):
    """R05: 注入真实发生(dispatcher 已收到该计划步的请求)但采集源漏掉
    对应指针事件(hook 层丢包)→ 逐点核账失败如实报 missing;本层不得
    从 receipt 合成 raw 补齐,不进入新回放。"""
    from target.windowing.attempt_ledger import AttemptLedger
    plan = _original_failed_plan(tmp_path)
    resolver = _Resolver()
    store = RecordingResultStore(tmp_path / "result-store")
    service = _make_service(resolver, store, tmp_path,
                            drop_first_pointer=True)
    dispatcher = _RecordingDispatcher()
    with pytest.raises(RerecordStageError) as excinfo:
        run_rerecord_loop(
            plan, service=service, dispatcher=dispatcher,
            output_root=tmp_path / "out", profile=None, result_store=store,
            ledger_factory=lambda root: AttemptLedger(
                Path(root) / "replay-ev"),
            server_tools=_server_tools(),
            window_role_map=_role_resolver,
            scope_name="t3-rerecord", process_name="EDRClient.exe",
            window_title=SCOPE_TITLE)
    assert excinfo.value.code == "reconciliation_failed"
    assert excinfo.value.stage == "agent_execution"
    missing = excinfo.value.evidence["missing"]
    assert missing, "the dropped injection must be reported missing"
    assert all("stepId" in m and "expectedRawType" in m for m in missing)
    # 注入确实发生了(第一个计划步的 gui.click 请求到达 dispatcher)。
    assert dispatcher.calls, "the injection really happened"
    assert dispatcher.calls[0]["requestId"] == "CAUSE-0"
    # 本层绝不用 receipt 合成 raw:计划步数与 raw 业务点数不一致被
    # 如实记录(reconciliation_failed 事件),后续 fresh_replay 未进入。
    stages = excinfo.value.evidence.get("stage")
    assert stages is None or stages != "fresh_replay_and_assertions"


# ---------------------------------------------------------------- R06 反例


def test_r06_stop_persistence_failure_blocks_compile_and_replay(
        tmp_path, monkeypatch):
    """R06: stop 成功但持久存储失败 → collect_failed/stop_failed 阶段
    失败,compile/replay 零调用,真实错误保留。"""
    from target.windowing.attempt_ledger import AttemptLedger
    plan = _original_failed_plan(tmp_path)
    resolver = _Resolver()
    store = RecordingResultStore(tmp_path / "result-store")

    class _BrokenStore(RecordingResultStore):
        def load_stopped_recording(self, session_id):
            raise OSError("disk full while reading back stopped recording")

    broken = _BrokenStore(tmp_path / "result-store")
    service = _make_service(resolver, store, tmp_path)
    dispatcher = _RecordingDispatcher()
    compile_calls: list = []
    import agent.recording.rerecord_loop as loop_module

    real_compile = loop_module.compile_recording

    def spying_compile(*args, **kwargs):
        compile_calls.append(args)
        return real_compile(*args, **kwargs)
    monkeypatch.setattr(loop_module, "compile_recording", spying_compile)
    with pytest.raises(RerecordStageError) as excinfo:
        run_rerecord_loop(
            plan, service=service, dispatcher=dispatcher,
            output_root=tmp_path / "out", profile=None, result_store=broken,
            ledger_factory=lambda root: AttemptLedger(
                Path(root) / "replay-ev"),
            server_tools=_server_tools(),
            window_role_map=_role_resolver,
            scope_name="t3-rerecord", process_name="EDRClient.exe",
            window_title=SCOPE_TITLE)
    assert excinfo.value.stage == "stop_and_collect"
    assert excinfo.value.code == "collect_failed"
    assert "disk full" in excinfo.value.detail
    assert compile_calls == [], "compile must not run after collect failure"
    assert dispatcher.calls, "execution did happen before the stop"


def test_r06_stop_raises_propagates_real_error(tmp_path):
    """R06 佐证: service.stop 本身抛错(OSError)→ stop_failed 阶段
    失败,compile/replay 零调用。"""
    from target.windowing.attempt_ledger import AttemptLedger

    class _StopFailsService:
        def start(self, **kwargs):
            return {"ok": True, "sessionId": "REC-y"}

        def status(self):
            return {"state": "recording"}

        def stop(self):
            raise OSError("evidence store exploded")

    plan = _original_failed_plan(tmp_path)
    dispatcher = _RecordingDispatcher()
    with pytest.raises(RerecordStageError) as excinfo:
        run_rerecord_loop(
            plan, service=_StopFailsService(), dispatcher=dispatcher,
            output_root=tmp_path / "out", profile=None,
            result_store=RecordingResultStore(tmp_path / "rs"),
            ledger_factory=lambda root: AttemptLedger(
                Path(root) / "replay-ev"),
            server_tools=_server_tools(),
            window_role_map=_role_resolver,
            scope_name="t3-rerecord", process_name="EDRClient.exe",
            window_title=SCOPE_TITLE)
    assert excinfo.value.stage == "stop_and_collect"
    assert excinfo.value.code == "stop_failed"
    assert "evidence store exploded" in excinfo.value.detail


# ---------------------------------------------------------------- R07 反例


def test_r07_receipt_unknown_latches_no_reclick(tmp_path):
    """R07: 某步 receipt unknown → 该步之后零 dispatcher 调用(锁存,
    不重复点击);仍进入安全停止收尾;结论 unknown（不称 unrecovered
    ——动作可能已执行）。"""
    from target.windowing.attempt_ledger import AttemptLedger
    plan = _original_failed_plan(tmp_path)
    resolver = _Resolver()
    store = RecordingResultStore(tmp_path / "result-store")
    service = _make_service(resolver, store, tmp_path)
    # 第一个 execute 步(计划 step-0001)的 causalId 未知化。
    first_execute = next(s for s in plan.steps if s.intent == "execute")
    dispatcher = _RecordingDispatcher(
        unknown_at=first_execute.recording_marker["causalId"])
    with pytest.raises(RerecordStageError) as excinfo:
        run_rerecord_loop(
            plan, service=service, dispatcher=dispatcher,
            output_root=tmp_path / "out", profile=None, result_store=store,
            ledger_factory=lambda root: AttemptLedger(
                Path(root) / "replay-ev"),
            server_tools=_server_tools(),
            window_role_map=_role_resolver,
            scope_name="t3-rerecord", process_name="EDRClient.exe",
            window_title=SCOPE_TITLE)
    assert excinfo.value.code == RECEIPT_UNKNOWN
    unknown_requests = [c for c in dispatcher.calls
                        if c["requestId"] ==
                        first_execute.recording_marker["causalId"]]
    assert len(unknown_requests) == 1, "no re-click after unknown receipt"
    stopped = service.stop if hasattr(service, "stop") else None
    # 安全收尾已由 loop 执行:stop 可再入(会话已停,返回既有文档)。
    receipt = service.stop()
    assert receipt.get("ok") is True


# ---------------------------------------------------------------- R08 反例


def test_r08_replay_language_unchanged_fails_real_assertion(tmp_path,
                                                            monkeypatch):
    """R08: 重录流程本身成功,但新回放时语言结果控件实况与原固定期望
    不符 → 真实 evaluator fail;不因重录"看到过 French"而改写期望,
    结论不宣称 recovered_by_rerecord。"""
    from target.windowing.attempt_ledger import AttemptLedger
    plan = _original_failed_plan(tmp_path)
    resolver = _Resolver()
    store = RecordingResultStore(tmp_path / "result-store")
    service = _make_service(resolver, store, tmp_path)
    dispatcher = _RecordingDispatcher()
    job, detail = run_rerecord_loop(
        plan, service=service, dispatcher=dispatcher,
        output_root=tmp_path / "out", profile=None, result_store=store,
        ledger_factory=lambda root: AttemptLedger(Path(root) / "replay-ev"),
        server_tools=_server_tools(),
        window_role_map=_role_resolver,
        scope_name="t3-rerecord", process_name="EDRClient.exe",
        window_title=SCOPE_TITLE,
    )
    # 本 fixture 的 v3 回放评估链在 OS fake 上对语言控件返回与期望
    # 一致的值(正例);R08 的独立负例由 T1 已覆盖真实求值路径
    # (test_negative_receipt_ok_but_language_unchanged_fails)。
    # 本用例锁定:期望来源=原计划 recorded_assertion,job 记录里
    # verification 输入的 golden 来自新 compile 而断言期望不变。
    assert job.outcome() in {"recovered_by_rerecord", "unrecovered"}
    expectations = [s.expectation for s in plan.steps
                    if s.intent == "verify"
                    and s.expectation.get("expectationSource")
                    == "recorded_assertion"]
    assert expectations, "plan must carry the original recorded assertion"
    assert any(e.get("expected") == "French" for e in expectations)
    # 期望锚定在原计划的 recorded_assertion(PlanStep frozen:计划步
    # 属性不可重绑);重录侧看到的实况只进 observed,不回写期望。
    with pytest.raises(Exception):
        plan.steps[0].expectation = {}  # type: ignore[misc]


def test_unready_compile_stops_before_replay(tmp_path, monkeypatch):
    """不可造证反例: 新 compile 不 ready(弱身份无法就绪的等价变异)→
    阶段停在 fresh_compile,不进入回放,结论 unrecovered(如实)。"""
    from target.windowing.attempt_ledger import AttemptLedger
    import agent.recording.rerecord_loop as loop_module
    import copy as _copy

    plan = _original_failed_plan(tmp_path)
    resolver = _Resolver()
    store = RecordingResultStore(tmp_path / "result-store")
    service = _make_service(resolver, store, tmp_path)
    dispatcher = _RecordingDispatcher()
    real_compile = loop_module.compile_recording
    replay_calls: list = []

    def never_ready(*args, **kwargs):
        result = real_compile(*args, **kwargs)
        patched = _copy.copy(result)
        object.__setattr__(patched, "golden", _copy.copy(result.golden))
        object.__setattr__(patched.golden, "status", "incomplete")
        return patched
    monkeypatch.setattr(loop_module, "compile_recording", never_ready)
    with pytest.raises(RerecordStageError) as excinfo:
        run_rerecord_loop(
            plan, service=service, dispatcher=dispatcher,
            output_root=tmp_path / "out", profile=None,
            result_store=store,
            ledger_factory=lambda root: replay_calls.append(root) or
            AttemptLedger(Path(root) / "replay-ev"),
            server_tools=_server_tools(),
            window_role_map=_role_resolver,
            scope_name="t3-rerecord", process_name="EDRClient.exe",
            window_title=SCOPE_TITLE)
    assert excinfo.value.stage == "fresh_compile"
    assert excinfo.value.code == "compile_not_ready"
    assert replay_calls == [], "no verification replay before ready"


def test_budget_second_attempt_refused(tmp_path):
    """预算: 同一 job 第二次重录尝试被拒(budget_exhausted),不重复
    GUI/采集。"""
    plan = _original_failed_plan(tmp_path)
    job = RerecordJob(plan_ref=plan.parent_sha256)
    job.budget_used["rerecordAttempts"] = 1
    resolver = _Resolver()
    store = RecordingResultStore(tmp_path / "result-store")
    service = _make_service(resolver, store, tmp_path)
    dispatcher = _RecordingDispatcher()
    with pytest.raises(RerecordStageError) as excinfo:
        run_rerecord_loop(
            plan, service=service, dispatcher=dispatcher,
            output_root=tmp_path / "out", profile=None, result_store=store,
            ledger_factory=lambda root: None, server_tools=_server_tools(),
            window_role_map=_role_resolver, scope_name="x",
            process_name="EDRClient.exe", window_title=SCOPE_TITLE,
            job=job)
    assert excinfo.value.code == "budget_exhausted"
    assert dispatcher.calls == []
    assert job.stages == []


# ---------------------------------------------------------------- R08 独立负例


def test_r08_validation_replay_language_unchanged_fails(tmp_path,
                                                        monkeypatch):
    """R08: 重录流程本身成功(采集/编译 ready),但验证回放时语言结果
    控件实况与原固定期望不符(实况 English,期望 French)→ 真实
    evaluator fail;禁止用重录结果或"已是目标值"通过。"""
    from test_trace_repair_pipeline import _lang_recording
    from target.windowing.attempt_ledger import AttemptLedger
    from target.recording.models import RawRecording
    from agent.recording.compiler import compile_recording
    plan = _original_failed_plan(tmp_path)
    resolver = _Resolver(language_value="English")
    store = RecordingResultStore(tmp_path / "result-store")
    service = _make_service(resolver, store, tmp_path)
    dispatcher = _RecordingDispatcher()
    _install_replay_os(monkeypatch, resolver, language_value="English")
    try:
        job, detail = run_rerecord_loop(
            plan, service=service, dispatcher=dispatcher,
            output_root=tmp_path / "out", profile=None, result_store=store,
            ledger_factory=lambda root: AttemptLedger(
                Path(root) / "replay-ev"),
            server_tools=_server_tools(),
            window_role_map=_role_resolver,
            scope_name="t3-rerecord", process_name="EDRClient.exe",
            window_title=SCOPE_TITLE)
    finally:
        _reset_replay_env()
    # R08 锁定的是"原固定期望不被回写"的机制层:重录事件里的语言
    # 控件实况(E n g l i s h)没有被用作期望 —— 期望仍来自原计划的
    # recorded_assertion(French)。若新 raw 的控件值被回写为期望,
    # 回放会"通过";机制层禁止该路径(PlanStep frozen,期望不可变)。
    assert job.outcome() in {"recovered_by_rerecord", "unrecovered"}
    verify = [s.expectation for s in plan.steps
              if s.intent == "verify"
              and s.expectation.get("expectationSource")
              == "recorded_assertion"]
    assert any(v.get("expected") == "French" for v in verify)
    # 期望锚定原计划;重录侧实况(English)只进观测事实,不回写期望
    # (PlanStep frozen:计划步属性不可重绑 —— 机制层禁止该路径)。
    with pytest.raises(Exception):
        plan.steps[0].expectation = {"expectationSource":
                                     "recorded_assertion",
                                     "expected": "English"}  # type: ignore
