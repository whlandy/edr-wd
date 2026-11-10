"""C05 误挂向探针（设计11 §9 C05 / §5.2，真实链路，先红后绿）。

账本 C05 成功条件第二向："不误挂新RI"；§5.2："发布前校验实例仍属于
原可见段；失效结果不挂到新菜单"。

场景：同一 handle 两代菜单。
- 第一代 RI-A（live-REC-647312 第4菜单 handle 6751880，4 项）：SHOW →
  walk 在途 → DESTROY 退役未复用。1000 轮宽限语义（C05 探针A）下该
  walk 结果发布为 complete 帧，带 RI-A —— 这是**已退役实例的失效帧**，
  采集侧保它是为了"特征不丢"；但它属于原可见段。
- 第二代 RI-B（同 handle 复用，3 项，布局不同）：SHOW，用户点击。
  点击事件 evidence.window.instanceId = RI-B。

当前编译帧选择 `_select_event_snapshot`/`_frame_window_matches` 按
handle 匹配（compiler.py:572 只比 handle/process/root，不比
instanceId）——RI-A 帧的 capturedAtMs 在点击时刻之前，被选为 RI-B
事件的绑定帧；menu_layout.from_recording 按该帧构建结构化契约，
新菜单挂上前一代的 4 行布局。§5.2"失效结果不挂到新菜单"被违反。

探针A（负例原料，当前代码应红）：真实 correlator+inventory+compile
全链。两代菜单特征不同（4项 vs 3项），断言新实例事件的结构化契约
不得来自退役实例的帧 —— 帧必须属于事件自己实例的可见段。
探针B（无歧义对照）：同 handle 两代菜单特征相同时（内容未变），
误挂不可观察 —— 该形态不在本片判定范围（帧不可区分时无挂错可言），
仅固定旧帧被 dedup 语义排除后新帧仍发布的既有行为不回退。
"""

from __future__ import annotations

import time

import pytest

from target.recording.inventory import ControlInventory
from target.recording.models import CaptureScope
from target.recording.windows import WindowsUIACorrelator

pytestmark = pytest.mark.unit

SCOPE = CaptureScope("target", "windows_pywinauto", "EDRClient.exe", "^logo1$")

MENU_HANDLE = 6751880  # live-REC-647312 第4菜单的真实 handle
MAIN_HANDLE = 13239958


def _menu_tree(items, label="Idioma"):
    return {
        "ok": True, "title": "EDRClient", "complete": True,
        "controls": [
            {"automation_id": "", "control_type": "MenuItem",
             "text": f"{label} {i}",
             "rectangle": {"x": 0, "y": i * 30, "w": 120, "h": 28},
             "depth": 2, "is_visible": True, "is_enabled": True,
             "ancestry": [{"control_type": "Menu", "name": "语言菜单"}]}
            for i in range(items)
        ],
    }


class _ReuseResolver:
    """同 handle 两代菜单：generation 决定树内容；DESTROY 后死 HWND。

    element_at 命中项跟随 generation（弱身份 MenuItem，无 automationId，
    使 menu_layout 结构化路径成为编译时的判定者）。
    """

    def __init__(self) -> None:
        self.generation = 0  # 0=主窗口期, 1=第一代菜单(4项), 2=第二代菜单(3项)
        self.walks: list[tuple[int, int]] = []  # (generation, handle)
        self.destroyed = False
        self.walk_delay = 0.0
        # 第二代菜单标签（探针B用不同标签测 volatility 误挂路径）。
        self.gen2_label = "Idioma"

    def windows(self):
        return [{"title": "logo1", "pid": 42, "processName": "EDRClient.exe",
                 "handle": MAIN_HANDLE}]

    def process_pid_belongs_to_root(self, pid, root):
        return True

    def process_in_scope(self, name, pid=None):
        return True

    def foreground(self):
        return {"windowTitle": "logo1", "processName": "EDRClient.exe",
                "pid": 42, "handle": MAIN_HANDLE}

    def window_from_native(self, native):
        if not isinstance(native, dict):
            return None
        return {**native, "processName": "EDRClient.exe"}

    def element_at(self, x, y, pid=None):
        items = 4 if self.generation == 1 else 3
        slot = min(int(y) // 30, items - 1)
        return {"controlType": "MenuItem", "text": f"Idioma {slot}",
                "automationId": "", "ownerPid": pid,
                # [x0, y0, x1, y1]，与快照投影（x+w, y+h）同语义，使
                # from_recording 的 rect 精确命中可成立。
                "rect": [0, slot * 30, 120, slot * 30 + 28],
                "ancestry": [{"control_type": "Menu", "name": "语言菜单"}]}

    def control_tree(self, handle, max_depth=12):
        gen = self.generation  # walk 开始时快照：在途 walk 读的是当时活着的树
        self.walks.append((gen, handle))
        if self.walk_delay:
            time.sleep(self.walk_delay)
        if gen == 1:
            # 第一代菜单在途 walk：树在 walk 开始时已读到（真实 UIA walk
            # 持有已解析的控件引用，DESTROY 发生在 walk 完成之后）。
            return _menu_tree(4)
        if gen == 2:
            return _menu_tree(3, label=self.gen2_label)
        return {
            "ok": True, "title": "logo1", "complete": True,
            "controls": [
                {"automation_id": "logo1.menu", "control_type": "MenuItem",
                 "text": "Language",
                 "rectangle": {"x": 10, "y": 10, "w": 80, "h": 24},
                 "depth": 1, "is_visible": True, "is_enabled": True},
            ],
        }


def _now_ms():
    # 包时间与 capturedAtMs 同源（真实 monotonic clock）：编译帧选择按
    # capturedAtMs <= 事件时刻选帧，量纲必须一致 —— 固定小值会使时间
    # 过滤失效，测不到目标分支。
    return int(time.monotonic() * 1000)


def _click_packets(ms):
    from target.recording.source import HookPacket
    native_window = {"handle": MENU_HANDLE, "pid": 42,
                     "windowTitle": "EDRClient"}
    down = HookPacket(
        kind="pointer_down", monotonic_ms=ms,
        wall_time="2026-11-06T00:00:00.000Z",
        screen_point=(60, 45), button="left",
        native={"kind": "pointer_down", "button": "left",
                "screen_point": (60, 45), "handle": MENU_HANDLE,
                "processName": "EDRClient.exe", "pid": 42,
                "window": native_window},
    )
    up = HookPacket(
        kind="pointer_up", monotonic_ms=ms + 10,
        wall_time="2026-11-06T00:00:00.000Z",
        screen_point=(60, 45), button="left",
        native={"kind": "pointer_up", "button": "left",
                "screen_point": (60, 45), "handle": MENU_HANDLE,
                "processName": "EDRClient.exe", "pid": 42,
                "window": native_window},
    )
    return down, up


def _trans(kind, ms, handle, title="EDRClient"):
    from target.recording.source import HookPacket
    return HookPacket(
        kind="window_transition", monotonic_ms=ms,
        wall_time="2026-11-06T00:00:00.000Z",
        native={"kind": kind, "processName": "EDRClient.exe",
                "title": title, "pid": 42, "handle": handle},
    )


def _wait(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _drain(source, timeout=5.0):
    """真实队列 worker 需要 wall-clock 出队；轮询至队列清空。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if source.diagnostics()["queueDepth"] == 0:
            break
        time.sleep(0.01)
    time.sleep(0.05)


def _run_two_generation_flow(resolver, inventory):
    """真实生产入口，时序：RI-A SHOW→walk 在途→DESTROY（walk 带宽限发布）
    → handle 复用 RI-B SHOW→点击。返回 correlator。"""
    correlator = WindowsUIACorrelator(resolver, inventory=inventory)
    correlator.seed_scope(SCOPE)
    # 第一代：SHOW（window_changed 立即 walk，慢于后续 DESTROY）
    resolver.generation = 1
    resolver.walk_delay = 0.25
    t0 = _now_ms()
    correlator.correlate(_trans("show", t0, MENU_HANDLE), SCOPE, 1)
    # 快速 HIDE→DESTROY（walk 仍在途）——1000 轮宽限：walk 发布带 RI-A
    correlator.correlate(_trans("hide", t0 + 50, MENU_HANDLE), SCOPE, 2)
    resolver.walk_delay = 0.0
    correlator.correlate(_trans("destroy", t0 + 70, MENU_HANDLE), SCOPE, 3)
    resolver.destroyed = True
    # 等宽限 walk 发布（RetiredRI-A complete 帧）
    _wait(lambda: any(
        s["window"].get("handle") == MENU_HANDLE for s in inventory.snapshots))
    # handle 复用：第二代 SHOW（新实例 RI-B）
    resolver.destroyed = False
    resolver.generation = 2
    correlator.correlate(_trans("show", t0 + 900, MENU_HANDLE), SCOPE, 4)
    # 用户点击第二代菜单项（事件带 RI-B）
    down, up = _click_packets(t0 + 940)
    correlator.correlate(down, SCOPE, 5)
    correlator.correlate(up, SCOPE, 6)
    time.sleep(0.05)
    return correlator


def test_c05_stale_frame_of_destroyed_instance_not_bound_to_new_instance_click():
    """§5.2：退役实例的失效帧不得挂到同 handle 新实例的点击事件。

    可观察判定（不测内部状态）：编译 RI-B 点击步的结构化契约不得来自
    RI-A 的 4 行布局。RI-B 菜单 3 项、第一项 rect=[0,0,120,28] 与 RI-A
    第一项相同 —— from_recording 在错误帧上会命中 slot 0、以 4 行布局
    生成契约；正确行为下 RI-B 自身帧（3行）才是判定者，或无 RI-B 帧时
    structural 必须为 None（不得借退役帧）。
    """
    from agent.recording.compiler import compile_recording
    from target.recording.session import RecordingSession
    from target.recording.source import QueuedCaptureSource

    class _NoopDriver:
        def start(self, emit): ...
        def stop(self): ...

    resolver = _ReuseResolver()
    inventory = ControlInventory(resolver)  # 生产默认 debounce 1500ms
    correlator = WindowsUIACorrelator(resolver, inventory=inventory)
    session = RecordingSession("REC-C05G", "review", SCOPE)
    source = QueuedCaptureSource(
        scope=SCOPE, sink=session.ingest, driver=_NoopDriver(),
        correlator=correlator, inventory=inventory)
    session._source = source
    session.start()  # source.start(): seed_scope → inventory.start → worker
    try:
        resolver.generation = 1
        resolver.walk_delay = 0.25
        t0 = _now_ms()
        source._enqueue(_trans("show", t0, MENU_HANDLE))
        source._enqueue(_trans("hide", t0 + 50, MENU_HANDLE))
        resolver.walk_delay = 0.0
        source._enqueue(_trans("destroy", t0 + 70, MENU_HANDLE))
        resolver.destroyed = True
        got_retired_frame = _wait(lambda: any(
            s["window"].get("handle") == MENU_HANDLE
            for s in inventory.snapshots))
        resolver.destroyed = False
        resolver.generation = 2
        # 第二代 SHOW walk 滞后超过点击间隔（真实 UIA walk 0.4-1.2s，live
        # RI-0010 SHOW walk 与 population 竞态）：点击事件处理完成时新实例
        # 自身帧尚未发布 —— 唯一早于点击的候选帧是退役 RI-A 帧，误挂只在
        # 此形态下实测。walk_delay 在 SHOW2 walk 完成前不得重置。
        resolver.walk_delay = 1.2
        source._enqueue(_trans("show", t0 + 900, MENU_HANDLE))
        for p in _click_packets(t0 + 940):
            source._enqueue(p)
        _drain(source, timeout=8.0)
        resolver.walk_delay = 0.0
    finally:
        session.stop()  # drain → inventory.stop

    assert got_retired_frame, (
        f"前置：退役宽限 walk 必须先发布 RI-A complete 帧，实际 "
        f"snapshots={len(inventory.snapshots)}, skipped={inventory.skipped}")
    recording = session.recording()
    # 前置：点击事件带第二代实例 id（S1 戳），且时间线确认两代实例不同。
    clicks = [e for e in recording.events if e.type == "pointer_click"]
    assert len(clicks) == 1, [e.type for e in recording.events]
    click_ri = (clicks[0].evidence.get("window") or {}).get("instanceId")
    assert click_ri and str(click_ri).startswith("RI-"), clicks[0].evidence
    retired_ri = [
        f.get("instanceId")
        for f in recording.window_timeline["events"]
        if f.get("kind") == "destroyed"]
    assert retired_ri and click_ri not in retired_ri, (
        f"前置破坏：点击事件实例 {click_ri} 属于退役集 {retired_ri}")
    # 帧账本：RI-A 帧（退役实例）在库，capturedAtMs 早于点击时刻。
    menu_frames = [s for s in recording.control_snapshots
                   if s["window"].get("handle") == MENU_HANDLE]
    retired_frames = [s for s in menu_frames
                      if s["window"].get("instanceId") == retired_ri[-1]]
    assert retired_frames, (
        f"前置：退役帧必须在库（1000轮宽限语义），menu_frames={menu_frames}")
    assert retired_frames[0]["window"].get("instanceId") != click_ri

    result = compile_recording(recording)
    click_steps = [s for s in result.case.steps if s.action_id == "gui.click"]
    assert click_steps, [s.action_id for s in result.case.steps]
    control = click_steps[0].selector.control
    structural = control.get("structural")
    # 判定：结构化契约不得来自退役实例的 4 行布局。
    if structural is not None:
        rows = structural["layout"]
        assert len(rows) == 3, (
            f"C05 误挂实锤：RI-B 点击事件的结构化契约来自退役 RI-A 帧"
            f"（{len(rows)} 行布局），§5.2 失效结果不得挂到新菜单；"
            f"structural={structural}")
        assert structural["index"] < 3
    else:
        # 无结构化契约时，弱身份唯一性计数同样不得借退役帧判定 ready。
        # 3 项菜单在 RI-B 帧缺失时无法证明唯一 → 不得 ready。
        assert click_steps[0].status == "incomplete", (
            f"RI-B 自身帧缺失时弱身份不得借退役帧证 ready，实际 "
            f"status={click_steps[0].status}, issues={click_steps[0].issues}")


def test_c05_stale_frame_not_used_as_volatile_label_evidence_for_new_instance():
    """探针B:退役帧也不得作为新实例的 volatility 证据。

    同名位置在退役 RI-A 帧中显示不同标签(4项菜单的 slot 3 "Idioma 3"),
    新实例 RI-B 无自身帧 —— volatility 判定若借退役帧会把 RI-B 的弱身份
    误标 compile_weak_identity_volatile_label。§5.2:失效结果不参与新
    实例的任何判定(既不证 ready,也不证 volatile)。
    """
    from agent.recording.compiler import compile_recording
    from target.recording.session import RecordingSession
    from target.recording.source import QueuedCaptureSource

    class _NoopDriver:
        def start(self, emit): ...
        def stop(self): ...

    class _VolatileResolver(_ReuseResolver):
        # 第二代菜单项标签与第一代不同("Idioma i" → "Sprache i"):若借
        # 退役 RI-A 帧判 volatility,同位置不同标签即误报。RI-B 帧含 1 个
        # MenuItem(被点击项)+ 2 个 Separator(真实菜单分隔条):peers<2 使
        # structural=None,判定落到弱身份计数(=1)+ volatility 扫描;
        # RI-B 帧内与点击项同位置的只有它自己(text 相同),不构成 volatility
        # 信号 —— 修复前扫描含退役 RI-A 帧(4 项 MenuItem 同 ancestry,
        # text 全不同)即误报 volatile。
        def __init__(self) -> None:
            super().__init__()
            self.gen2_label = "Sprache"

        def element_at(self, x, y, pid=None):
            return {"controlType": "MenuItem", "text": f"{self.gen2_label} 1",
                    "automationId": "", "ownerPid": pid,
                    "rect": [0, 31, 120, 59],
                    "ancestry": [{"control_type": "Menu", "name": "语言菜单"}]}

        def control_tree(self, handle, max_depth=12):
            gen = self.generation
            self.walks.append((gen, handle))
            if self.walk_delay:
                time.sleep(self.walk_delay)
            if gen == 1:
                return _menu_tree(4)
            if gen == 2:
                base = _menu_tree(3, label=self.gen2_label)["controls"]
                # slot 1 是 MenuItem,slot 0/2 换成分隔条(真实菜单形态)。
                for i in (0, 2):
                    base[i] = {"automation_id": "",
                               "control_type": "Separator",
                               "text": "", "rectangle":
                               {"x": 0, "y": i * 30, "w": 120, "h": 4},
                               "depth": 2, "is_visible": True,
                               "is_enabled": True,
                               "ancestry": [{"control_type": "Menu",
                                             "name": "语言菜单"}]}
                return {"ok": True, "title": "EDRClient", "complete": True,
                        "controls": base}
            return {
                "ok": True, "title": "logo1", "complete": True,
                "controls": [
                    {"automation_id": "logo1.menu", "control_type": "MenuItem",
                     "text": "Language",
                     "rectangle": {"x": 10, "y": 10, "w": 80, "h": 24},
                     "depth": 1, "is_visible": True, "is_enabled": True},
                ],
            }

    resolver = _VolatileResolver()
    # debounce_ms=0：点击的 step walk 立即执行，RI-B 自身帧在库 ——
    # candidate_count 在 RI-B 帧内证唯一（1 候选），判定落到 volatility
    # 扫描：修复前扫到退役 RI-A 帧同位置不同标签误报 volatile。
    inventory = ControlInventory(resolver, debounce_ms=0)
    correlator = WindowsUIACorrelator(resolver, inventory=inventory)
    session = RecordingSession("REC-C05V", "review", SCOPE)
    source = QueuedCaptureSource(
        scope=SCOPE, sink=session.ingest, driver=_NoopDriver(),
        correlator=correlator, inventory=inventory)
    session._source = source
    session.start()
    try:
        resolver.generation = 1
        resolver.walk_delay = 0.25
        t0 = _now_ms()
        source._enqueue(_trans("show", t0, MENU_HANDLE))
        source._enqueue(_trans("hide", t0 + 50, MENU_HANDLE))
        resolver.walk_delay = 0.0
        source._enqueue(_trans("destroy", t0 + 70, MENU_HANDLE))
        resolver.destroyed = True
        got_retired_frame = _wait(lambda: any(
            s["window"].get("handle") == MENU_HANDLE
            for s in inventory.snapshots))
        resolver.destroyed = False
        resolver.generation = 2
        source._enqueue(_trans("show", t0 + 900, MENU_HANDLE))
        for p in _click_packets(t0 + 940):
            source._enqueue(p)
        _drain(source)
    finally:
        session.stop()

    assert got_retired_frame, f"前置破坏:退役帧未发布 skipped={inventory.skipped}"
    recording = session.recording()
    clicks = [e for e in recording.events if e.type == "pointer_click"]
    assert len(clicks) == 1, [e.type for e in recording.events]
    result = compile_recording(recording)
    click_steps = [s for s in result.case.steps if s.action_id == "gui.click"]
    assert click_steps, [s.action_id for s in result.case.steps]
    issues = click_steps[0].issues
    assert "compile_weak_identity_volatile_label" not in issues, (
        f"C05 volatility 误挂实锤:退役 RI-A 帧被当作 RI-B 的标签易变证据"
        f"(issues={issues}),§5.2 失效结果不参与新实例的任何判定")
