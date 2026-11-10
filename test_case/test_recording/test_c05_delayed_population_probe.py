"""C05 另一向探针（设计11 §9 C05 / §5.2，真实链路，先红后绿）。

场景：菜单 SHOW → window_changed 立即 walk，但菜单项 population 尚未
完成（live 原件 CS-0005/CS-0015 均为 SHOW 后数百 ms 只读到 1 个容器的
实锤）→ walk 读到空树 → ``no_controls`` 结构化跳过是**终态、无重试** →
用户点击菜单项的 step walk 被 1500ms debounce 排到 DESTROY 之后（死
HWND，walk_failed）→ RI-0010（live-REC-647312 第4菜单 handle 6751880）
全程零快照，编译器拿不到任何可绑定菜单帧。

探针A（负例原料，当前代码应红）：真实 WindowsUIACorrelator + 真实
ControlInventory。SHOW walk 空读后菜单 population 完成，DESTROY 未发生
（时序与 live 一致：population < HIDE/DESTROY），一次有界补拍即可保住
本次有效菜单的唯一帧 —— 当前代码没有任何补拍，帧丢失。
探针B（有界+诚实）：population 永不完成（真实空内容），补拍一次仍空
→ no_controls 终态，恰读 2 次（不无限重试，§5.3 不靠无限加队列）。
探针C（step 语义不变）：step reason 的空读不补拍 —— debounce 合并语义
与普通页面 walk 行为保持不变，只有 window_changed（刚进入的窗口/菜单）
获得补拍。

修复语义（最小）：inventory._capture 对 reason=window_changed 的空读
安排一次延迟补拍；补拍仍空保持 no_controls 诚实终态；retried 计数透明
（§5.1 失败回退也要计数与解释）；instance_authority 契约对补拍读到的
内容照常生效（退役未复用授权由 1000 修复提供）。
"""

from __future__ import annotations

import threading
import time

import pytest

from target.recording.inventory import ControlInventory
from target.recording.models import CaptureScope
from target.recording.windows import WindowsUIACorrelator

pytestmark = pytest.mark.unit

SCOPE = CaptureScope("target", "windows_pywinauto", "EDRClient.exe", "^logo1$")

MENU_HANDLE = 6751880  # live-REC-647312 第4菜单的真实 handle
MAIN_HANDLE = 13239958


class _PopulationResolver:
    """菜单 population 竞态 resolver：菜单第1次读空树，之后读完整菜单。

    element_at 提供命中项（ownerPid 对齐输入窗口 pid），pointer_down/up
    两包驱动真实 pending_press → 事件 → step walk 请求链。
    """

    def __init__(self, populate_after: int) -> None:
        # 第 N 次**全库**读起返回完整菜单：seed 先消费 walk #1（主窗口），
        # 菜单 SHOW walk 是 #2 —— populate_after=3 使菜单首读（#2）为空。
        self.populate_after = populate_after
        self.walks: list[int] = []
        self.destroyed = threading.Event()

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
        return {"controlType": "MenuItem", "text": "Idioma 1",
                "automationId": "", "ownerPid": pid,
                "rect": [0, 30, 120, 28],
                "ancestry": [{"control_type": "Menu", "name": "语言菜单"}]}

    def control_tree(self, handle, max_depth=12):
        self.walks.append(handle)
        if self.destroyed.is_set():
            return None  # DESTROY 后：死 HWND
        if handle == MENU_HANDLE and len(self.walks) < self.populate_after:
            return {"ok": True, "title": "EDRClient", "complete": False,
                    "controls": []}  # population 未完成
        return self._tree(handle)

    def _tree(self, handle):
        if handle == MENU_HANDLE:
            return {
                "ok": True, "title": "EDRClient", "complete": True,
                "controls": [
                    {"automation_id": f"M.item{i}", "control_type": "MenuItem",
                     "text": f"Idioma {i}",
                     "rectangle": {"x": 0, "y": i * 30, "w": 120, "h": 28},
                     "depth": 2, "is_visible": True, "is_enabled": True,
                     "ancestry": [{"control_type": "Menu", "name": "语言菜单"}]}
                    for i in range(4)
                ],
            }
        return {
            "ok": True, "title": "logo1", "complete": True,
            "controls": [
                {"automation_id": "logo1.menu", "control_type": "MenuItem",
                 "text": "Language",
                 "rectangle": {"x": 10, "y": 10, "w": 80, "h": 24},
                 "depth": 1, "is_visible": True, "is_enabled": True},
            ],
        }


def _click_packets(ms):
    """真实链：pointer_down + pointer_up 两包（up 产生事件与 step walk）。"""
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


def _run_flow(resolver, inventory, *, include_click):
    """真实 correlator 生产入口，时序对齐 live-REC-647312 第4菜单：
    SHOW（walk 立即空读）→ population 完成 → 用户点击 → 补拍窗口期内
    HIDE → DESTROY。live 里 SHOW→HIDE 有 1.8s，菜单补拍（~500ms）落在
    可见窗口内；探针以 ~750ms 保持同一先后约束且留有余量。"""
    correlator = WindowsUIACorrelator(resolver, inventory=inventory)
    correlator.seed_scope(SCOPE)
    correlator.correlate(_trans("show", 100, MENU_HANDLE), SCOPE, 1)
    time.sleep(0.3)  # population 完成（菜单第2次读起完整）；空读已于 t≈0 落盘
    if include_click:
        down, up = _click_packets(400)
        correlator.correlate(down, SCOPE, 2)
        correlator.correlate(up, SCOPE, 3)
        time.sleep(0.4)  # 补拍（空读+500ms）落在 HIDE 前的真实窗口内
        correlator.correlate(_trans("hide", 800, MENU_HANDLE), SCOPE, 4)
        correlator.correlate(_trans("destroy", 820, MENU_HANDLE), SCOPE, 5)
    else:
        correlator.correlate(_trans("hide", 150, MENU_HANDLE), SCOPE, 2)
        correlator.correlate(_trans("destroy", 170, MENU_HANDLE), SCOPE, 3)
    resolver.destroyed.set()
    return correlator


def _wait(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


# --- 探针A：SHOW walk 空读 + population 完成 → 菜单帧不得丢（预期红） ---

def test_c05_show_walk_empty_read_does_not_lose_the_menu_frame():
    """§5.2：菜单 SHOW 后快速点击/HIDE，SHOW 期 walk 与菜单 population
    竞态读空不得终结该实例的采集 —— population 完成且窗口未销毁时，
    一次有界补拍必须保住本次有效菜单的唯一帧（live RI-0010 全程零快照
    的机制）。"""
    resolver = _PopulationResolver(populate_after=3)  # seed#1 + 菜单首读#2 空
    inventory = ControlInventory(resolver)  # 生产默认 debounce 1500ms
    inventory.start()
    try:
        correlator = _run_flow(resolver, inventory, include_click=True)
        got = _wait(lambda: any(
            s["window"].get("handle") == MENU_HANDLE
            for s in inventory.snapshots))
    finally:
        inventory.stop()

    kinds = [e["kind"] for e in correlator.window_timeline["events"]]
    assert "created" in kinds and "destroyed" in kinds, kinds

    menu_frames = [s for s in inventory.snapshots
                   if s["window"].get("handle") == MENU_HANDLE]
    assert got and len(menu_frames) == 1, (
        f"C05 population 竞态：SHOW walk 空读后菜单帧永久丢失（"
        f"snapshots={len(inventory.snapshots)}, skipped={inventory.skipped}, "
        f"retried={getattr(inventory, 'retried', None)}）。§5.2 要求 "
        f"SHOW→输入→HIDE 与后台发布竞态下不丢当前菜单特征"
    )
    frame = menu_frames[0]
    ri = frame["window"].get("instanceId")
    assert ri and str(ri).startswith("RI-"), "菜单帧必须带实例归属（RI）"
    destroyed_ri = [e.get("instanceId")
                    for e in correlator.window_timeline["events"]
                    if e.get("kind") == "destroyed"]
    assert destroyed_ri and ri == destroyed_ri[-1], (
        "帧的实例必须与 DESTROY 事实命名同一实例（退役未复用授权语义）")
    assert frame["complete"] is True and frame["controlCount"] == 4
    # 点击的 step 请求与补拍同 handle：合并后提前执行（修复的设计意图，
    # 不再落死 HWND），故 walk_failed 可能不出现；无论合并与否菜单帧必须在。
    assert inventory.skipped.get("walk_failed") in (None, 1), (
        f"skipped={inventory.skipped}")


# --- 探针B：补拍有界且诚实（population 永不完成 → 恰读2次） ------------

def test_c05_empty_read_retry_is_bounded_and_stays_honest():
    """§5.3：补拍不得变成无限重试。population 永不完成时恰读 2 次
    （原读+一次补拍），第二次仍空保持 no_controls 诚实终态，retried
    计数透明。"""
    resolver = _PopulationResolver(populate_after=10**9)  # 永远空
    inventory = ControlInventory(resolver)
    inventory.start()
    try:
        correlator = WindowsUIACorrelator(resolver, inventory=inventory)
        correlator.seed_scope(SCOPE)
        correlator.correlate(_trans("show", 100, MENU_HANDLE), SCOPE, 1)
        ok = _wait(lambda: len([h for h in resolver.walks
                                if h == MENU_HANDLE]) >= 2)
        time.sleep(0.25)  # 若实现错误地第三次重试，这里会暴露
        walks_menu = [h for h in resolver.walks if h == MENU_HANDLE]
        skipped = dict(inventory.skipped)
        retried = dict(inventory.retried)
        menu_snapshots = [s for s in inventory.snapshots
                          if s["window"].get("handle") == MENU_HANDLE]
    finally:
        inventory.stop()

    assert ok, f"补拍未发生（walks={resolver.walks}）"
    assert len(walks_menu) == 2, (
        f"补拍必须恰好一次：菜单 handle 实际读了 {len(walks_menu)} 次"
        f"（walks={resolver.walks}）。§5.3 不靠无限重试掩盖空内容")
    # 原读与补拍各自如实计 no_controls（两次结构化 skip，不隐藏任何一次）。
    assert skipped.get("no_controls") == 2, skipped
    assert retried.get("no_controls") == 1, (
        f"补拍发生必须计数透明且恰一次，实际 retried={retried}")
    assert menu_snapshots == [], "population 真实为空时不得伪造菜单帧"
    assert not correlator.scope_seed_error, correlator.scope_seed_error


# --- 探针C：step 空读语义不变（不补拍） --------------------------------

def test_c05_step_empty_read_keeps_existing_semantics():
    """普通 step walk 空读保持 no_controls 终态：debounce 合并语义与
    现有页面 walk 行为不变，补拍仅限 window_changed（刚进入的窗口）。"""
    resolver = _PopulationResolver(populate_after=10**9)
    inventory = ControlInventory(resolver, debounce_ms=60)
    inventory.start()
    try:
        # handle=9999 在 resolver 中无树（control_tree 返回主窗口树——任何
        # handle 都返回非空），改用 menu handle 空树路径检验 step 语义：
        # menu handle 只在 window_changed 下才补拍，step 必须终态。
        inventory.request(handle=MENU_HANDLE, reason="step", title="EDRClient")
        ok = _wait(lambda: inventory.skipped.get("no_controls") == 1)
        time.sleep(0.3)  # 若错误地补拍，这里会暴露
        walks_menu = [h for h in resolver.walks if h == MENU_HANDLE]
        retried = dict(inventory.retried)
    finally:
        inventory.stop()

    assert ok, f"step 空读应立即 no_controls，实际 skipped={inventory.skipped}"
    assert len(walks_menu) == 1, f"step 空读不得补拍，实际读了 {len(walks_menu)} 次"
    assert not retried, f"step 路径不得产生补拍计数，实际 retried={retried}"
