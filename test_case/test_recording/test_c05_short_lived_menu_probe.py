"""C05 成对探针（设计11 §9 C05 / §5.2，真实链路，先红后绿）。

场景：第4次菜单 SHOW → window_changed 立即 walk 在途 → 快速点击 + HIDE
→ DESTROY 退役实例 → walk 完成时 correlate 权威索引已查不到该 handle
→ instance_superseded 丢弃 → 本次有效菜单的唯一 complete 帧丢失
（live-REC-647312 第4次点击"末次缺事件绑定完整菜单树"的采集侧机制）。

探针A（负例原料，当前代码应红）：真实 WindowsUIACorrelator + 真实
ControlInventory，walk 在途期间 handle 实例退役（未复用），walk 结果
被丢弃 → 菜单快照丢失，编译器拿不到 complete 菜单帧。
探针B（应绿的最小行为面）：退役且未复用的 handle，authority 必须报告
退役实例 id（walk 在途只能读过它；死 HWND walk 必失败，不可能拿到
新树），丢弃是过度保守。复用（新实例接管 handle）后仍丢弃——0517
P1-2 契约保持；从未见过的 handle 仍丢弃。
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


class _Resolver:
    """最小 resolver：注入在途延迟，返回完整菜单树（complete=True）。"""

    def __init__(self) -> None:
        self.walk_delay = 0.0
        self.trees: dict[int, dict] = {}
        self.walks: list[int] = []

    def windows(self):
        return [{"title": "logo1", "pid": 42, "processName": "EDRClient.exe",
                 "handle": 13239958}]

    def process_pid_belongs_to_root(self, pid, root):
        return True

    def process_in_scope(self, name, pid=None):
        return True

    def foreground(self):
        return {"windowTitle": "logo1", "processName": "EDRClient.exe",
                "pid": 42, "handle": 13239958}

    def control_tree(self, handle, max_depth=12):
        self.walks.append(handle)
        if self.walk_delay:
            time.sleep(self.walk_delay)
        return self.trees.get(handle)


def _menu_tree(handle):
    return {
        "ok": True,
        "title": "EDRClient",
        "complete": True,
        "controls": [
            {"automation_id": f"M.item{i}", "control_type": "MenuItem",
             "text": f"Idioma {i}", "rectangle": {"x": 0, "y": i * 30, "w": 120, "h": 28},
             "depth": 2, "is_visible": True, "is_enabled": True,
             "ancestry": [{"control_type": "Menu", "name": "语言菜单"}]}
            for i in range(4)
        ],
    }


def _drain(inventory, expected, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(inventory.snapshots) >= expected:
            return True
        time.sleep(0.01)
    return False


def _run_short_lived_menu_flow(resolver, inventory, menu_handle=6751880):
    """真实 correlator：SHOW→(walk 在途)→点击→HIDE→DESTROY，无 handle 复用。

    全部走 correlate()/seed_scope() 生产入口，不走测试替身的私有方法。
    """
    from target.recording.source import HookPacket

    correlator = WindowsUIACorrelator(resolver, inventory=inventory)
    correlator.seed_scope(SCOPE)

    def trans(kind, ms, handle, title="EDRClient"):
        return HookPacket(
            kind="window_transition", monotonic_ms=ms,
            wall_time="2026-11-06T00:00:00.000Z",
            native={"kind": kind, "processName": "EDRClient.exe",
                    "title": title, "pid": 42, "handle": handle},
        )

    packets = [
        trans("show", 100, menu_handle),      # 菜单出现 → window_changed 立即 walk
        HookPacket(                            # 快速点击（pointer_down+up 分开发）
            kind="pointer_click", monotonic_ms=140,
            wall_time="2026-11-06T00:00:00.000Z",
            screen_point=(60, 45),
            native={"kind": "pointer_down", "button": "left",
                    "screen_point": (60, 45), "handle": 13239958,
                    "processName": "EDRClient.exe", "pid": 42},
        ),
        trans("hide", 150, menu_handle),       # 菜单立即隐藏
        trans("destroy", 170, menu_handle),    # DESTROY：实例退役，无复用
    ]
    for i, p in enumerate(packets, start=1):
        correlator.correlate(p, SCOPE, i)
    return correlator


# --- 探针A：当前代码，walk 在途退役 → 菜单帧丢失（预期红） ----------------

def test_c05_short_lived_menu_frame_survives_destroy_without_reuse():
    """§5.2：点菜单后菜单立刻隐藏，SHOW→输入→HIDE 与后台发布的竞态
    不得丢掉本次有效菜单特征。退役且未复用时 walk 结果不得被
    instance_superseded 结构化丢弃。"""
    resolver = _Resolver()
    resolver.trees[6751880] = _menu_tree(6751880)
    # 主窗口也给出树，使 walk_failed 不成为噪音：唯一 skip 信号应指向
    # instance_superseded（目标条件），与主窗口采集无关。
    resolver.trees[13239958] = {
        "ok": True, "title": "logo1", "complete": True,
        "controls": [
            {"automation_id": "logo1.menu", "control_type": "MenuItem",
             "text": "Language", "rectangle": {"x": 10, "y": 10, "w": 80, "h": 24},
             "depth": 1, "is_visible": True, "is_enabled": True},
        ],
    }
    # walk 慢于 DESTROY（真实 UIA walk 0.4-1.2s，竞态在此）。
    resolver.walk_delay = 0.25

    inventory = ControlInventory(resolver, debounce_ms=0, stop_timeout=3.0)
    inventory.start()
    try:
        correlator = _run_short_lived_menu_flow(resolver, inventory)
        got = _drain(inventory, expected=1)
    finally:
        inventory.stop()

    # 时间线确认竞态前提真实形成：菜单实例 created + destroyed 都落盘。
    kinds = [e["kind"] for e in correlator.window_timeline["events"]]
    assert "created" in kinds and "destroyed" in kinds, kinds

    menu_frames = [s for s in inventory.snapshots
                   if s["window"].get("handle") == 6751880]
    assert got and len(menu_frames) == 1, (
        f"C05 竞态：walk 在途退役未复用，本次有效菜单帧被丢（"
        f"snapshots={len(inventory.snapshots)}, skipped={inventory.skipped}）。"
        f"§5.2 要求 SHOW→输入→HIDE 与后台发布竞态下不丢当前菜单特征"
    )
    assert menu_frames[0]["window"].get("instanceId"), (
        "菜单帧必须带实例归属（RI），供编译器事件绑定")
    assert menu_frames[0]["complete"] is True


# --- 探针B：最小行为面（authority 语义） ---------------------------------

class _AuthorityRecorder:
    """记录 authority 对每类 handle 的回答，用于固定语义边界。"""

    def __init__(self) -> None:
        self.calls: list[tuple[int, object]] = []


def test_c05_authority_reports_retired_unreused_instance():
    """最小行为面：authority 对'退役且未复用'的 handle 必须报告退役
    实例 id —— walk 在途只能读过它；死 HWND 不可能 walk 出新树，丢弃
    无法阻止任何伪造。复用后报告 None（0517 P1-2 保持）；从未见过的
    handle 报告 None（不能给噪音窗口背书）。"""
    resolver = _Resolver()
    correlator = WindowsUIACorrelator(resolver, inventory=ControlInventory(resolver))
    correlator.seed_scope(SCOPE)
    authority = correlator._inventory.instance_authority
    assert callable(authority), "seed_scope 必须把 correlator 权威索引接线给 inventory"

    from target.recording.source import HookPacket

    def trans(kind, ms, handle, title="EDRClient"):
        return HookPacket(
            kind="window_transition", monotonic_ms=ms,
            wall_time="2026-11-06T00:00:00.000Z",
            native={"kind": kind, "processName": "EDRClient.exe",
                    "title": title, "pid": 42, "handle": handle},
        )

    # 777: created → destroyed（退役未复用）；888: 复用；999: 从未见过。
    correlator.correlate(trans("show", 100, 777), SCOPE, 1)
    correlator.correlate(trans("destroy", 150, 777), SCOPE, 2)
    correlator.correlate(trans("show", 200, 888, "旧窗口"), SCOPE, 3)
    correlator.correlate(trans("destroy", 250, 888), SCOPE, 4)
    correlator.correlate(trans("show", 300, 888, "新窗口"), SCOPE, 5)

    retired = authority(777)
    assert retired is not None and isinstance(retired, str) and retired.startswith("RI-"), (
        f"退役未复用 handle 的 authority 必须报告退役实例 id，实际 {retired!r}")

    reused = authority(888)
    assert reused is not None and isinstance(reused, str), (
        f"复用 handle 的 authority 必须报告当前实例，实际 {reused!r}")
    assert retired != reused, "复用后新旧实例 id 必须不同"

    assert authority(999) is None, (
        "从未见过的 handle 不得获得实例背书")
