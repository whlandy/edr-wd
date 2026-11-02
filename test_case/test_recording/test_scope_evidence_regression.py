"""F54 scope-evidence regressions — 独立审查会话独占文件（主会话勿改）.

Derives hard failing regressions from the round-1 audit
(result-report/20261027-scope-independent-audit/report.md), which proved by
driving the real ``WindowsUIACorrelator.correlate()`` branches offline:

* ``foreign_app_events`` must count each out-of-scope packet exactly once.
  The F54c ``id(packet)`` dedup (windows.py ``_note_rejection``) only guards
  the ``rejections`` list — it sits *after* the foreign early-return, so the
  outer ``no_event_produced`` note (``_correlate_inner``) still double-counts
  foreign ``pointer_up``/``scroll`` packets.  These tests fail until the
  counter itself is per-packet.  Plain assertions, no xfail.
* A foreign total must coexist with successfully recorded in-scope actions
  (guards against "fixing" the counter by suppressing recorded actions).
* Pins the shared time comparator's inclusive [C−recentSeconds, C] boundary
  (06 设计 §4/§6「边界…有证据」) — no existing test pins the exact edges.
* Round 3 (F57 dedup): the rejection dedup must be PER-CORRELATOR-INSTANCE.
  The original implementation stored ``_rej_noted`` on the packet object
  itself (frozen HookPacket mutated via ``object.__setattr__``), so two
  independent correlators — or a new generation after restart/replay —
  silently lost diagnostics for a shared packet.
* Round 4 (supervisor review): the address-reuse demonstration is RETIRED
  from the test file. A correct per-instance dedup that retains the first
  packet makes address reuse impossible, so forcing reuse as a precondition
  would misjudge a correct fix (pytest.fail on "never reused"). The reuse
  swallow of the legacy ``_last_rej_packet`` bare-id comparison remains
  archived experimental evidence against the old/intermediate
  implementations (round-3 raw output, windows 0cab81c5 / 0271e8bc) — see
  result-report/.../20261027-0235-rej-dedup-f56-teardown and the round-4
  report. The pinned contracts here are purely semantic:
  distinct packets each listed; same packet once per instance; two
  correlators and new generations each count independently.
* Round 5 (F64b wndproc probe): REAL ``WndProcReadonlyProbe`` driven
  through a fake native user32 (existing ``object.__new__`` shim tests
  bypass the real constructor/native flow, so they cannot catch restore
  swallowing). Pins: restore failure must be detectable (raise / explicit
  failure), and on failure the ctypes callback must stay strongly
  retained after the probe object is dropped — the OS stores only the raw
  function pointer, never a Python reference, so releasing the callback
  while the HWND is still subclassed is a dangling-call hazard.

Scope/identity tuples are taken verbatim from the f52i capture original
(``REC-7a3965d00a114ef2bc9a72cc8d222b32``): fixture window
``EDR-WD Native Recording QA [region01]`` hosted by ``powershell.exe``
(pid 1396, hwnd 1508278), clicks at (595, 360) and (700, 450).
"""

from __future__ import annotations

import pytest

from target.recording.models import CaptureScope
from target.recording.source import HookPacket
from target.recording.windows import WindowsUIACorrelator

pytestmark = pytest.mark.unit

# --- f52i original identity (verbatim) -------------------------------------
FIXTURE_WINDOW = {
    "windowTitle": "EDR-WD Native Recording QA [region01]",
    "processName": "powershell.exe",
    "handle": 1508278,
    "pid": 1396,
}
FOREIGN_WINDOW = {
    "windowTitle": "Untitled - Notepad",
    "processName": "notepad.exe",
    "handle": 999001,
    "pid": 4242,
}
FIXTURE_ROOT = {
    "controlType": "Window",
    "automationId": "RecordingFixture",
    "identifier": None,
    "text": "EDR-WD Native Recording QA [region01]",
    "rect": [520, 234, 1160, 654],
    "ownerPid": 1396,
    "ancestry": [{"controlType": "Pane", "name": "Desktop 1"}],
}
SCOPE = CaptureScope(
    target="target",
    backend="windows_pywinauto",
    process_name="powershell.exe",
    window_title=r"^EDR-WD Native Recording QA \[region01\]$",
)
FIXTURE_RECT = (520, 234, 1160, 654)
CLICK_POINTS = [(595, 360), (700, 450)]


class _ScriptedResolver:
    """Resolver stand-in exposing the surface the correlator consumes."""

    strict_point_ownership = False

    def __init__(self, fixture_visible: bool = True,
                 element_visible: bool = True) -> None:
        self.fixture_visible = fixture_visible
        self.element_visible = element_visible

    def window_at(self, x, y):
        x0, y0, x1, y1 = FIXTURE_RECT
        if self.fixture_visible and x0 <= x < x1 and y0 <= y < y1:
            return dict(FIXTURE_WINDOW)
        return dict(FOREIGN_WINDOW)

    def foreground(self):
        return dict(FOREIGN_WINDOW)

    def focused(self):
        return dict(FOREIGN_WINDOW)

    def element_at(self, x, y, pid=None):
        x0, y0, x1, y1 = FIXTURE_RECT
        if self.element_visible and self.fixture_visible \
                and x0 <= x < x1 and y0 <= y < y1:
            return dict(FIXTURE_ROOT)
        return None


class _Clock:
    def __init__(self, start: int = 225085500) -> None:
        self.ms = start

    def tick(self, step: int = 7) -> int:
        self.ms += step
        return self.ms


def _correlator(fixture_visible: bool = True) -> tuple[WindowsUIACorrelator, _Clock]:
    return (
        WindowsUIACorrelator(
            _ScriptedResolver(fixture_visible=fixture_visible),
            double_click_ms=500,
            drag_threshold=(4, 4),
        ),
        _Clock(),
    )


def _pkt(clock: _Clock, kind: str, **kw) -> HookPacket:
    payload = {
        "monotonic_ms": clock.tick(),
        "wall_time": "2026-10-26T12:07:51.921Z",
        "native": {},
    }
    payload.update(kw)
    return HookPacket(kind=kind, **payload)


def _feed_click(correlator, clock, seq, point, *, in_scope: bool):
    """One down+up pair; returns the produced events (may be empty)."""
    resolver_hit = point if in_scope else (10, 10)  # (10,10) resolves foreign
    produced = []
    for kind in ("pointer_down", "pointer_up"):
        out = correlator.correlate(
            _pkt(clock, kind, screen_point=resolver_hit, button="left"),
            SCOPE, seq)
        seq += 1
        produced += list(out) if isinstance(out, tuple) else ([out] if out else [])
    return produced, seq


def _actions(events):
    return [e for e in events if e.type == "pointer_click"]


# ---------------------------------------------------------------------------
# 1) 外域 pointer_up 每包只计一次（F54c 反例：当前实现 down=1, up=2）
# ---------------------------------------------------------------------------


def test_foreign_pointer_up_counts_packet_once():
    correlator, clock = _correlator(fixture_visible=False)
    produced, _ = _feed_click(correlator, clock, 1, (10, 10), in_scope=False)
    assert produced == [], "外域点击不得产出任何事件"
    # 2 个包（down+up），每个只允许计数一次。
    assert correlator.out_of_scope_events == 2, (
        "out_of_scope_events 必须按真实包数计数")
    assert correlator.foreign_app_events == 2, (
        "foreign_app_events 每包只能 +1：外域 pointer_up 被内层 out_of_scope 与"
        "外层 no_event_produced 双重记账即失败（当前实现为 3）")


# ---------------------------------------------------------------------------
# 2) 外域 scroll 每包只计一次（同一双记账机制）
# ---------------------------------------------------------------------------


def test_foreign_scroll_counts_packet_once():
    correlator, clock = _correlator(fixture_visible=False)
    out = correlator.correlate(
        _pkt(clock, "scroll", screen_point=(10, 10), native={"delta": -120}),
        SCOPE, 1)
    assert out is None, "外域 scroll 不得产出任何事件"
    assert correlator.out_of_scope_events == 1
    assert correlator.foreign_app_events == 1, (
        "外域 scroll 同样只允许每包计数一次（当前实现 out_of_scope+"
        "no_event_produced 记 2 次）")


# ---------------------------------------------------------------------------
# 3) 外域拒收不得影响 in-scope 点击的录制
# ---------------------------------------------------------------------------


def test_in_scope_click_recorded_despite_foreign_drops():
    correlator, clock = _correlator(fixture_visible=True)
    seq = 1
    for _ in range(3):
        out = correlator.correlate(_pkt(clock, "key_command", key="X"),
                                   SCOPE, seq)
        seq += 1
        assert out is None
    produced, _ = _feed_click(correlator, clock, seq, CLICK_POINTS[0],
                              in_scope=True)
    actions = _actions(produced)
    assert len(actions) == 1, "3 个外域键击被拒后，in-scope 点击必须照常录制"
    action = actions[0]
    assert action.observed_target.automation_id == "RecordingFixture"
    assert action.observed_target.text == "EDR-WD Native Recording QA [region01]"
    assert action.evidence["foregroundPid"] == 1396
    assert action.evidence["window"]["processName"] == "powershell.exe"
    assert action.scope.process_name == "powershell.exe"
    assert correlator.foreign_app_events == 3
    assert correlator.out_of_scope_events == 3


# ---------------------------------------------------------------------------
# 4) foreign 总数与成功动作并存（防「修计数=压动作」式过度修正）
# ---------------------------------------------------------------------------


def test_foreign_total_coexists_with_recorded_actions():
    correlator, clock = _correlator(fixture_visible=True)
    seq = 1
    for _ in range(6):
        out = correlator.correlate(_pkt(clock, "key_command", key="X"),
                                   SCOPE, seq)
        seq += 1
        assert out is None
    produced = []
    for point in CLICK_POINTS:
        click_events, seq = _feed_click(correlator, clock, seq, point,
                                        in_scope=True)
        produced += click_events
    actions = _actions(produced)
    assert correlator.foreign_app_events == 6, "6 个外域键击必须各计一次"
    assert correlator.out_of_scope_events == 6
    assert len(actions) == 2, "foreign 计数不得以任何方式吞掉 in-scope 动作"
    assert [list(a.input["screenPoint"]) for a in actions] == [list(p) for p in CLICK_POINTS]
    assert all(a.observed_target.automation_id == "RecordingFixture"
               for a in actions)


# ---------------------------------------------------------------------------
# 5) [C−5s, C] 边界为闭区间（06 §4/§6 边界证据；既有测试未钉死端点）
# ---------------------------------------------------------------------------


def _aware(minute=0, second=0):
    from datetime import datetime, timezone
    return datetime(2026, 10, 25, 12, minute, second, tzinfo=timezone.utc)


def test_time_recent_window_boundaries_are_inclusive():
    from target.recording.region_assertion import time_recent_matches

    now = _aware(second=3)
    # 恰为下界 C−5s → 11:59:58Z：闭区间应通过。
    status, detail = time_recent_matches("2026-10-25T11:59:58+00:00", now, 5.0)
    assert status == "pass", f"下界应闭区间可过: {detail}"
    assert detail["windowStart"] == "2026-10-25T11:59:58+00:00"
    # 下界之外一秒 → 失败。
    assert time_recent_matches("2026-10-25T11:59:57+00:00", now, 5.0)[0] == "fail"
    # 恰为锚点 C 本身：通过（未来判定为严格大于）。
    status, detail = time_recent_matches("2026-10-25T12:00:03+00:00", now, 5.0)
    assert status == "pass", f"锚点 C 应闭区间可过: {detail}"


# ---------------------------------------------------------------------------
# 6) F57 去重语义（轮3钉缺陷、轮4改语义钉）：按 correlator 实例记账，
#    不得写进 packet；地址复用实验退役为报告原证据
# ---------------------------------------------------------------------------


def _foreign_key_packet(clock):
    return _pkt(clock, "key_command", key="X")


def test_same_packet_counted_once_per_correlator_instance():
    """同一实例收到同一 packet 两次：foreign 恰好 +1（去重契约本体）。"""
    correlator, clock = _correlator(fixture_visible=False)
    packet = _foreign_key_packet(clock)
    assert correlator.correlate(packet, SCOPE, 1) is None
    assert correlator.correlate(packet, SCOPE, 2) is None
    assert correlator.foreign_app_events == 1, "同一实例同包重复拒因只计一次"
    assert correlator.out_of_scope_events == 2, (
        "out_of_scope_events 按真实到达次数计数（1 包 1 次，两次到达=2）")
    # foreign 拒因只计数不入列（产品语义：仅本应用窗口的拒因才入列）。
    assert correlator.rejections == [], "foreign 计数路径不应产生入列条目"


def test_same_packet_counted_independently_by_two_correlators():
    """同一原始包交给两个独立 correlator：各自正确计数一次。

    旧实现把 _rej_noted 写进 packet（object.__setattr__ 突破 frozen），
    第二个实例看到标记直接跳过 → 诊断丢失（本测试在旧实现下失败）。
    修复语义：按实例记账，不动共享的冻结值对象。"""
    packet = _foreign_key_packet(_Clock())
    first, _ = _correlator(fixture_visible=False)
    second, _ = _correlator(fixture_visible=False)
    assert first.correlate(packet, SCOPE, 1) is None
    assert second.correlate(packet, SCOPE, 1) is None
    assert first.foreign_app_events == 1
    assert second.foreign_app_events == 1, (
        "独立实例必须独立计数：跨实例共享的去重状态使第二个实例为 0")


def test_new_generation_correlator_counts_old_marked_packet():
    """重启/重放新代 correlator 收到旧代已拒 packet：诊断不得因旧标记丢失。

    旧实现在旧对象上留 _rej_noted，新代读到即跳过（本测试在旧实现下
    失败）；按实例记账后新代独立计数。"""
    old_packet = _foreign_key_packet(_Clock())
    old_gen, _ = _correlator(fixture_visible=False)
    old_gen.correlate(old_packet, SCOPE, 1)
    assert old_gen.foreign_app_events == 1
    new_gen, _ = _correlator(fixture_visible=False)
    new_gen.correlate(old_packet, SCOPE, 1)
    assert new_gen.foreign_app_events == 1, (
        "新代不得因旧对象上的去重标记丢诊断")


def test_distinct_packets_each_rejection_listed():
    """两个不同 packet 先后被拒：各自拒因都必须入列（语义契约）。

    轮4按监督评审改写：不再以 CPython 地址复用为前置——正确的按实例
    记账保留首个 packet 引用时复用本就不会发生，强制复用会把正确修复
    误判为前置失败。地址复用吞账（旧 _last_rej_packet 裸 id）保留为
    旧/中间实现实验原证据（轮3 raw 输出，windows 0cab81c5 与 weakref
    中间版 0271e8bc），见轮3/轮4报告，不进入验收门槛。本测试只钉
    语义：两个不同包（引用都活着，无分配器操作）各入列一条。"""

    def _listing_correlator():
        # in-scope 窗口 + 命中元素为 None → pointer_up 走
        # target_not_identifiable 列表化拒因路径（不是点击、不是 foreign）。
        return WindowsUIACorrelator(
            _ScriptedResolver(fixture_visible=True, element_visible=False),
            double_click_ms=500, drag_threshold=(4, 4))

    correlator = _listing_correlator()
    clock = _Clock()
    first = _pkt(clock, "pointer_up", screen_point=(595, 360),
                 button="left")
    assert correlator.correlate(first, SCOPE, 1) is None
    assert correlator.unidentified_target_events == 1
    assert len(correlator.rejections) == 1, "首个拒因应入列"
    assert correlator.correlate(first, SCOPE, 2) is None
    assert correlator.unidentified_target_events == 2, (
        "unidentified_target_events 按到达计数（与 out_of_scope_events 同语义，"
        "不去重）；去重约束的是入列，不是该计数器")
    assert len(correlator.rejections) == 1, (
        "同包同实例重复到达：入列仍只一次")
    second = _pkt(clock, "pointer_up", screen_point=(595, 360),
                  button="left")   # 新包对象（monotonic_ms 不同），引用共存
    correlator.correlate(second, SCOPE, 3)
    assert correlator.unidentified_target_events == 3, "每次到达各计一次"
    assert len(correlator.rejections) == 2, (
        "两个不同 packet 的拒因都必须入列，第二条不得被去重吞掉")
    assert all(entry.get("reason") == "target_not_identifiable"
               for entry in correlator.rejections), "两条拒因各自保留原原因"


# ---------------------------------------------------------------------------
# 7) 06 设计 §5「服务断连」：host-lost 必须与取消/确认路径同样完成
#    遮罩配对清理（valve 释放 → 看门狗停表 → 遮罩销毁），不能只靠
#    root 延迟销毁兜底。轮3/4 审查「窗口②」缺口的修复验收信号。
# ---------------------------------------------------------------------------


def test_host_lost_closes_region_overlay_pairing(monkeypatch):
    """06 设计 §5：「失败、取消、目标消失和服务断连都必须清理遮罩、
    释放快捷键并恢复此前状态」。

    正常取消/确认路径已有配对清理（``_close_region_overlay``），但
    ``_handle_host_lost``（recorder_panel.py ``472a40e5``）只释放热键并
    延迟销毁 root——遮罩/valve/看门狗保持打开。host 失联后 valve 心跳
    无人刷新 → 看门狗 ``hard_exit_due()`` → ``os._exit(87)`` 抢在优雅
    退出前把面板进程打死（F56 崩溃族嫌疑路径之一，轮4 报告 §3 窗口②）。

    本测试现为 RED（修复验收信号，与轮2 两项失败回归同模式）：主会话
    按设计补齐配对清理后转 GREEN。既有契约同时钉住：热键释放、2.5s
    延迟退出、``host_lost_handled`` 单次处理——修复不得弱化它们。"""
    import threading
    from types import SimpleNamespace as NS

    import target.recording.region_overlay as ro
    from target.recording.recorder_panel import RecorderPanel

    panel = RecorderPanel("fixture", None, lambda *a, **k: None)
    seen: dict = {}
    panel._root = NS(
        after=lambda ms, fn=None: seen.__setitem__("exit_after_ms", ms),
        unbind_all=lambda *a, **k: None,
        deiconify=lambda: seen.__setitem__("panel_restored", True),
        destroy=lambda: seen.__setitem__("root_destroyed", True),
    )
    panel._status_label = None
    panel._region_overlay = NS(
        destroy=lambda: seen.__setitem__("overlay_destroyed", True))
    panel._region_valve = NS(
        on_overlay_close=lambda: seen.__setitem__("valve_released", True))
    watchdog_stop = threading.Event()
    panel._region_watchdog_stop = watchdog_stop

    released: list[str] = []
    # _handle_host_lost 内是局部 import——补丁打在来源模块（同既有失联测试）
    monkeypatch.setattr(ro, "request_hotkey_shutdown",
                        lambda: released.append("hotkey") or True)

    panel._host_lost.set()
    panel._poll()                       # 真实触发路径（stdin EOF → 轮询）

    assert panel._host_lost_handled is True, "host-lost 单次处理（既有契约）"
    assert released == ["hotkey"], "热键释放仍须发生（既有契约）"
    assert seen.get("exit_after_ms") == 2500, "2.5s 延迟退出仍须排程（既有契约）"
    assert seen.get("valve_released") is True, (
        "服务断连必须释放输入 valve（06 §5）；当前实现 _handle_host_lost "
        "不调用 _close_region_overlay，valve 心跳失联后只能由看门狗 "
        "os._exit(87) 硬退")
    assert watchdog_stop.is_set(), "遮罩看门狗必须随失联清理停表"
    assert seen.get("overlay_destroyed") is True, (
        "遮罩必须显式销毁，不能只靠 2.5s 后 root.destroy 兜底")
    assert panel._region_overlay is None and panel._region_valve is None, (
        "清理后遮罩/valve 引用必须置空（与取消/确认路径同状态）")


# ---------------------------------------------------------------------------
# 8) 06 设计 §1/§6「第二、第三行或其他位置满足条件也应有效」：行位无关
#    语义钉（既有覆盖只到第二行；rowIndex 仅作证据，不得固化为身份）
# ---------------------------------------------------------------------------


def test_third_row_only_match_passes_any_position():
    """第一、二行不匹配而第三行满足时必须通过（quantifier=any）。

    既有测试（test_region_assertion_model.py::test_second_row_match_passes…）
    只钉到第二行；count 测试中行0/1 同样匹配，无「唯一匹配在第三行」的
    显式用例。06 §6 验收行明确「第一行不匹配而第二或第三行满足时通过」。
    同时钉住：matchedRows 证据指向第三行（行位仅证据、不固化为目标身份，
    06 §2），且同一 fixtures 在 quantifier=all 下失败（不得跨行拼接）。"""
    from target.recording.region_assertion import evaluate_region_conditions

    now = _aware(second=3)
    rows = [
        {"rowIndex": 0, "name": "无关甲"},
        {"rowIndex": 1, "name": "无关乙"},
        {"rowIndex": 2, "name": "目标内容-3"},
    ]
    conditions = {"op": "all", "children": [
        {"field": "name", "operator": "contains", "expected": "目标内容"},
    ]}
    outcome = evaluate_region_conditions(conditions, rows, now, 5.0)
    assert outcome["result"] == "pass", "仅第三行满足时必须通过（任意行位）"
    assert [r["rowIndex"] for r in outcome["matchedRows"]] == [2], (
        "matchedRows 证据应指向第三行；行位仅证据、不固化为目标身份")
    assert evaluate_region_conditions(
        conditions, rows, now, 5.0, quantifier="all")["result"] == "fail", (
        "同 fixtures 的 all 量词必须失败：任何单行都不完整满足，"
        "不得跨行拼接成通过")


# ---------------------------------------------------------------------------
# 9) F56 轮3 缺口(b)：WndProc 探针 restore 失败不得静默吞——06 §5
#    「恢复此前状态」要求清理失败可见；失败探针不得阻断其余探针恢复
# ---------------------------------------------------------------------------


def test_wndproc_probe_restore_failure_is_surfaced_not_swallowed(capsys):
    """``_restore_wndproc_probes`` 当前逐探针吞异常后直接清空列表
    （recorder_panel.py 472a40e5:742-749）：restore 失败的 HWND 保持
    子类化而回调对象随后被 GC → 下一条窗口消息落入已释放的原生跳板
    （轮3 报告 §3 缺口 b，0xC0000409 嫌疑形态），且**零痕迹**——与本
    代码库 F28 教训（静默吞异常让回归不可见）同类。

    离线可钉契约（不伪造原生 Tcl 行为）：
    1. 失败探针不得阻断其余探针恢复（每个 restore 都被尝试）；
    2. 清单最终清空（不留悬挂引用）；
    3. **失败必须留痕**（stderr/面板日志可见）——当前实现缺失，本测试
       即 RED，主会话按 06 §5 补齐可见性后转 GREEN。"""
    from types import SimpleNamespace as NS

    from target.recording.recorder_panel import RecorderPanel

    panel = RecorderPanel("fixture", None, lambda *a, **k: None)
    calls: list[str] = []

    def _probe(name: str, ok: bool):
        def restore():
            calls.append(name)          # 先记录「尝试」本身（契约：每个都被尝试）
            if not ok:
                raise RuntimeError(f"{name}-failed")
        return NS(restore=restore)

    probe_first = _probe("first", ok=True)
    probe_bad = _probe("bad", ok=False)
    probe_last = _probe("last", ok=True)
    panel._region_wndproc_probes = [probe_first, probe_bad, probe_last]

    panel._restore_wndproc_probes()

    assert calls == ["first", "bad", "last"], (
        "失败探针不得阻断其余探针恢复：每个 restore 都必须被尝试")
    assert panel._region_wndproc_probes == [], "清单最终清空，不留悬挂引用"
    err = capsys.readouterr().err
    assert "bad" in err and "restore" in err.lower(), (
        "restore 失败必须留痕（06 §5「恢复此前状态」+F28 零痕迹教训）；"
        "当前实现逐个静默吞异常，失败 HWND 保持子类化且无任何可见记录")


# ---------------------------------------------------------------------------
# 10) windows.py `_last_rej_packet` 裸 id 残留块：记账状态不得吞掉不同的
#    活包（确定性毒化状态法，不依赖分配器地址复现——轮4退役_allocator
#    前置的理由见测试 6 docstring；本测试钉两套去重机制的契约一致性）
# ---------------------------------------------------------------------------


def test_rejection_dedup_state_must_not_swallow_distinct_live_packet():
    """裸 id 判等无法区分「同一包见过」与「另一包恰住在记录的地址」。

    上方 weakref 身份去重用 ``ref() is packet`` 验证（防 ABA）；其后的
    ``_last_rej_packet`` 裸 id 块（windows.py f5005139:1705-1712）没有该
    验证。轮4 实验原证据（0271e8bc）已演示：首包被回收→地址复用→第二
    个不同包的入列拒因被吞（unidentified=2 但 rejections=1）。该块在
    weakref 去重之下只剩此风险（轮4 建议「并入后删除该块」，主会话未决）。

    本测试不依赖分配器：直接把残留记账置为「下一个待处理包的 id」——
    这正是地址复用发生后的等价状态。契约：**不同活包的拒因必须入列**；
    同包去重语义由测试 6/9 另行钉住。当前实现在此状态吞掉第二条 → RED。"""

    def _listing_correlator():
        # in-scope 窗口 + 命中元素 None → pointer_up 走
        # target_not_identifiable 入列路径（非点击、非 foreign）。
        return WindowsUIACorrelator(
            _ScriptedResolver(fixture_visible=True, element_visible=False),
            double_click_ms=500, drag_threshold=(4, 4))

    correlator = _listing_correlator()
    clock = _Clock()
    first = _pkt(clock, "pointer_up", screen_point=(595, 360), button="left")
    assert correlator.correlate(first, SCOPE, 1) is None
    assert len(correlator.rejections) == 1, "首个拒因应入列"

    second = _pkt(clock, "pointer_up", screen_point=(595, 360), button="left")
    # 模拟复用后残留：记账 id 现指向一个不同的、活着的包。
    correlator._last_rej_packet = id(second)
    correlator.correlate(second, SCOPE, 2)
    assert correlator.unidentified_target_events == 2, "到达计数按包计（不删）"
    assert len(correlator.rejections) == 2, (
        "裸 id 记账不得吞掉不同活包的拒因：残留 id 恰指向新包地址时，"
        "第二条 target_not_identifiable 仍必须入列（weakref 去重用 "
        "ref() is packet 已能正确处理同包场景，删除裸 id 块即转绿）")


# ---------------------------------------------------------------------------
# 11) 06 设计 §6「同名行、排序变化…均有可解释结果」：行集变化下匹配
#    跟随内容而非行位；同名多行各自独立求值（既有测试无重排/同名覆盖）
# ---------------------------------------------------------------------------


def test_row_set_changes_keep_matching_content_based():
    """同一行集合重排后匹配必须跟随内容而非行位；同名多行各自独立求值。

    06 §2：「选中的示例行用来指定字段，不把它的行号或临时 runtime ID
    固化为后续目标」；§6：「同名行、排序变化、控件重建、空列表及虚拟
    列表范围均有可解释结果」。既有覆盖：空列表（model 层 342 行）、
    第三行唯一匹配（本文件第 10 项）；**重排与同名无任何测试**。
    生产求值器逐样本无状态（docstring：从不跨行/跨样本合并），本测试
    钉住该语义不被后续改动破坏。"""
    from target.recording.region_assertion import evaluate_region_conditions

    now = _aware(second=3)
    conditions = {"op": "all", "children": [
        {"field": "name", "operator": "contains", "expected": "目标"},
    ]}
    # 样本1：唯一匹配在第三行。
    sample_a = [{"rowIndex": 0, "name": "甲"},
                {"rowIndex": 1, "name": "乙"},
                {"rowIndex": 2, "name": "目标"}]
    # 样本2：同一行集重排——目标行到了第一位。
    sample_b = [{"rowIndex": 0, "name": "目标"},
                {"rowIndex": 1, "name": "甲"},
                {"rowIndex": 2, "name": "乙"}]
    assert evaluate_region_conditions(
        conditions, sample_a, now, 5.0)["matchedRows"][0]["rowIndex"] == 2
    outcome_b = evaluate_region_conditions(conditions, sample_b, now, 5.0)
    assert outcome_b["result"] == "pass"
    assert [r["rowIndex"] for r in outcome_b["matchedRows"]] == [0], (
        "排序变化后匹配必须跟随内容：行位不是身份，重排不改变「哪一行"
        "满足」，只改变它在本次快照中的下标")
    # 样本3：同名两行——各自独立求值，均入证据（不合并、不择一）。
    sample_c = [{"rowIndex": 0, "name": "目标"},
                {"rowIndex": 1, "name": "目标"},
                {"rowIndex": 2, "name": "丙"}]
    outcome_c = evaluate_region_conditions(conditions, sample_c, now, 5.0)
    assert outcome_c["result"] == "pass"
    assert [r["rowIndex"] for r in outcome_c["matchedRows"]] == [0, 1], (
        "同名行必须各自独立求值并全部留证据（06 §6 同名行可解释结果）")


# ---------------------------------------------------------------------------
# 7) F64b WndProc 探针 restore 生命周期（轮5）：真实 probe + fake native
# ---------------------------------------------------------------------------


class _FakeWndProcFn:
    """可调用且容忍 restype/argtypes 赋值的假原生函数桩。"""

    def __init__(self, fn):
        self._fn = fn
        self.restype = None
        self.argtypes = None

    def __call__(self, *args, **kwargs):
        return self._fn(*args, **kwargs)


class _FakeUser32:
    """SetWindowLongPtrW 可注入成败的假 user32（真实 probe 消费面）。

    假函数以 _FakeWndProcFn 包装：probe 会对它们赋 restype/argtypes，
    绑定方法对象不可赋属性，必须用带 __dict__ 的可调用实例。"""

    def __init__(self):
        self.fake_original = 0x5A5A5A5A
        self.fail_next_set_long = False
        self.set_calls: list[tuple[int, int, int]] = []
        self.GetWindowLongPtrW = _FakeWndProcFn(self._get_long_impl)
        self.SetWindowLongPtrW = _FakeWndProcFn(self._set_long_impl)
        self.CallWindowProcW = _FakeWndProcFn(lambda *a: 0)

    def _get_long_impl(self, hwnd, index):
        return self.fake_original

    def _set_long_impl(self, hwnd, index, proc):
        self.set_calls.append((int(hwnd), int(index), int(proc)))
        if self.fail_next_set_long:
            self.fail_next_set_long = False
            return 0     # Win32 失败约定：返回 0
        return self.fake_original


def _real_probe():
    """构造真实 WndProcReadonlyProbe（macOS 上注入 fake native）。"""
    import ctypes
    import uuid
    from unittest import mock

    from target.recording.region_overlay import WndProcReadonlyProbe

    fake_user32 = _FakeUser32()

    class _FakeWindll:
        user32 = fake_user32

    with mock.patch.object(ctypes, "windll", _FakeWindll(), create=True), \
            mock.patch.object(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE,
                              create=True), \
            mock.patch.object(ctypes, "get_last_error", lambda: 5,
                              create=True):
        probe = WndProcReadonlyProbe(
            "t-" + uuid.uuid4().hex[:6], 0x1F00D)
    assert probe.installed is True
    assert fake_user32.set_calls, "attach 必须真实调用 SetWindowLongPtrW"
    return probe, fake_user32


def _callback_gone_after_drop(probe) -> bool:
    """丢弃 probe 后回调是否被 GC（GC=HWND 仍指 trampoline 时即悬空源）。"""
    import gc
    import weakref

    ref = weakref.ref(probe._proc)
    del probe
    gc.collect()
    return ref() is None


def test_wndproc_restore_native_failure_raises():
    """restore 遇原生失败必须可判定（抛错），不得静默返回。

    pre-F64b 行为（region_overlay `try/except pass`）下本测试失败：
    panel 的 F64 leftovers 留痕永远无法触发，真实失败到不了外层日志。"""
    import pytest

    probe, fake = _real_probe()
    fake.fail_next_set_long = True
    with pytest.raises(RuntimeError, match="wndproc-restore-failed"):
        probe.restore()


def test_wndproc_restore_failure_keeps_callback_alive_after_probe_drop():
    """restore 失败后回调必须强保活：OS 只存函数指针不持 Python 引用，
    probe 被（panel 清列表）丢弃时回调不得被 GC——否则活 HWND 的下一条
    消息调进已释放 trampoline（悬空调用源）。retired_callbacks 即为此契约。"""
    probe, fake = _real_probe()
    fake.fail_next_set_long = True
    try:
        probe.restore()
    except RuntimeError:
        pass   # 失败已留痕（见上测），此处只验证生命周期
    assert not _callback_gone_after_drop(probe), (
        "restore 失败后回调被 GC：失败路径未把回调转入持久保活（对照 "
        "attach 失败路径的 _retire_probe_callback 契约）")


def test_wndproc_attach_failure_keeps_callback_alive():
    """attach 失败路径同样必须保活回调（既有正确模式的回归钉）。"""
    import ctypes
    import gc
    import weakref
    from unittest import mock

    import pytest

    from target.recording.region_overlay import WndProcReadonlyProbe

    fake_user32 = _FakeUser32()
    fake_user32.fail_next_set_long = True   # attach 即失败

    class _FakeWindll:
        user32 = fake_user32

    before = len(WndProcReadonlyProbe.retired_callbacks)
    with mock.patch.object(ctypes, "windll", _FakeWindll(), create=True), \
            mock.patch.object(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE,
                              create=True), \
            mock.patch.object(ctypes, "get_last_error", lambda: 5,
                              create=True):
        with pytest.raises(RuntimeError, match="wndproc-attach-failed"):
            WndProcReadonlyProbe("t-attach-fail", 0x1F00D)
    assert len(WndProcReadonlyProbe.retired_callbacks) > before, (
        "attach 失败必须把回调记入 retired_callbacks")
    ref = weakref.ref(WndProcReadonlyProbe.retired_callbacks[-1])
    gc.collect()
    assert ref() is not None, "retired_callbacks 必须强引用失败回调"


def test_panel_restore_path_reports_real_probe_failure():
    """panel `_restore_wndproc_probes` 收到真实 probe 的失败必须留痕。

    既有 shim 测试用 fake probe 抛错，绕过真实构造/原生流程，无法证明
    「真实失败能到达 panel 日志」；本测试用真实 probe + fake native
    端到端验证：失败 → 异常冒泡 → panel 记录 → 清单清空不抛。"""
    from unittest import mock

    from target.recording.recorder_panel import RecorderPanel

    probe, fake = _real_probe()
    fake.fail_next_set_long = True
    panel = RecorderPanel.__new__(RecorderPanel)
    panel._region_wndproc_probes = [probe]
    notes: list[str] = []
    with mock.patch.object(RecorderPanel, "_panel_log_note",
                           lambda self, note: notes.append(str(note))):
        panel._restore_wndproc_probes()   # 不得抛
    assert panel._region_wndproc_probes == [], "处理后清单应清空"
    assert any("wndproc-restore-failed" in note for note in notes), (
        "真实 probe 的 restore 失败必须出现在 panel 留痕中（shim 测试"
        "验证不到的端到端路径）")
    assert any(str(probe.hwnd) in note for note in notes), (
        "留痕必须能定位到失败 probe（hwnd 可辨识）")


def test_wndproc_restore_failure_keeps_alive_even_if_get_last_error_raises():
    """get_last_error 自身异常时也必须先保活回调。

    契约：retire 必须先于任何可失败步骤（lastError 取因、消息构造）。
    当前实现先取 lastError（且 ctypes 未导入→NameError）后 retire，
    取因一旦抛错即跳过保活——本测试当前失败，修复后应通过。"""
    import ctypes
    from unittest import mock

    def _raising_get_last_error():
        raise RuntimeError("get-last-error itself failed")

    probe, fake = _real_probe()
    fake.fail_next_set_long = True
    with mock.patch.object(ctypes, "get_last_error",
                           _raising_get_last_error, create=True):
        try:
            probe.restore()
        except Exception:
            pass   # 失败必须可感知（形态不限），此处只验证生命周期
    assert not _callback_gone_after_drop(probe), (
        "get_last_error 抛错时回调未保活：retire 必须先于取因/消息构造执行")


def test_retired_callbacks_trim_must_not_release_installed_callback():
    """retired_callbacks 有界 256 淘汰不得释放仍装在活 HWND 的旧回调。

    全程驱动生产路径（不手工模拟裁剪、不在测试体内裁剪/改写列表）：
    1) 真实 probe + fake native：attach 成功、restore 失败 → 真实
       restore 失败路径把回调退休进保活名单（此刻回调仍装在活 HWND，
       fake 建模 attach 成功 + restore 失败的真实状态）；
    2) 通过真实 ``_retire_probe_callback`` 追加足量保活条目（生产函数
       自带淘汰），把最早条目挤出 256 窗口——压力量按 cb 在名单中的
       实际位置只读计算（index 只读，不改写列表）；
    3) 丢掉全部局部强引用（cb/probe，cb→_dispatch→probe→_proc 为引用
       环需 gc）后断言弱引用仍活。
    契约（形态无关，不强迫实现结构）：restore 失败未恢复的回调必须
    保活到恢复成功为止——当前实现淘汰即红，正确修复（如已安装条目
    无条件保活）后转绿。触发量：长会话累计 ≥256 次失败即现。"""
    import gc
    import weakref
    from types import SimpleNamespace

    from target.recording.region_overlay import (
        WndProcReadonlyProbe,
        _retire_probe_callback,
    )

    first, fake = _real_probe()
    fake.fail_next_set_long = True
    try:
        first.restore()   # 真实 restore 失败 → 生产 retire 路径
    except Exception:
        pass   # 失败可感知性已由其余测试钉住；此处只看生命周期
    cb = first._proc
    ref = weakref.ref(cb)
    lst = WndProcReadonlyProbe.retired_callbacks
    # 生产 retire 通道施加淘汰压力：cb 之后每多 1 条占 1 个窗口名额，
    # 名额超过 256 即被淘汰（生产函数每次追加后自带 del lst[:-256]）。
    after = len(lst) - 1 - lst.index(cb)
    for _ in range(257 + after):
        _retire_probe_callback(SimpleNamespace(_proc=object()))
    # 丢全部局部强引用后 gc（弱引用目标不得被本地变量钉活）。
    del cb, first, fake
    gc.collect()
    assert ref() is not None, (
        "restore 失败未恢复的回调被有界 256 淘汰释放：已安装回调必须"
        "无条件保活到恢复成功为止（当前 del lst[:-256] 区分不了"
        "已安装/未安装即统一裁剪），淘汰逻辑不得在正确产品上仍红")


# ---------------------------------------------------------------------------
# 8) F52l/F52k 旧代 reader 隔离（轮8）：真实 _consume 直驱（无线程/子进程）
# ---------------------------------------------------------------------------


class _FakePanelProcess:
    """可注入 stdout 帧序列的假面板进程（_consume 只消费 stdout）。"""

    def __init__(self, lines):
        self.stdout = list(lines)
        self._poll = 0

    def poll(self):
        return self._poll


def test_stale_generation_reader_cannot_satisfy_or_poison_new_generation():
    """旧代 reader 的输出必须全部落在旧代对象上（F52l/F52k 代隔离）。

    对抗构造按 F52l 复验反例：旧代 gen_acks 为**空 dict**（历史
    `gen_acks or self._push_acks` 回退会写新代 done）；新代正以同 token
    等待 push 回执。直驱真实 ``RecorderPanelHost._consume``（同步、无
    线程/子进程/Tk），喂入旧代帧：同 token pushed ACK、panelError、EOF。
    断言：新代等待不得被旧代 ACK 满足；新代 ready/_last_error 不得被
    旧代 panelError 污染；EOF 只置本代 closed、绝不置任何 ack Event
    （F52k 断联不得伪成功）。既有测试只手工模拟 consume 副作用
    （test_region_ui push 用例直接 done.set()），未驱动真实 reader。"""
    import threading

    from target.recording.ui_controller import RecorderPanelHost

    host = RecorderPanelHost.__new__(RecorderPanelHost)
    host._push_acks = {}          # 新代 push_to_panel 登记处（当前代对象）
    host._ready = threading.Event()
    host._last_error = None

    stale_acks: dict = {}         # 旧代 ACK 表（空 = 历史回退反例形态）
    stale_closed = threading.Event()
    stale_ready = threading.Event()
    stale_error: list = []

    new_done = threading.Event()
    host._push_acks["T"] = new_done   # 新代正在等待 token T 的回执

    frames = [
        '{"pushed": "T"}',          # 旧代面板对同 token 的迟到回执
        '{"panelError": "stale-boom"}',
        # stdout 迭代自然结束 = EOF（F52k：断联信号）
    ]
    process = _FakePanelProcess(frames)

    host._consume(process=process, stop_evt=threading.Event(),
                  gen_acks=stale_acks, gen_closed=stale_closed,
                  gen_ready=stale_ready, gen_error=stale_error)

    assert stale_error == ["stale-boom"], "旧代 panelError 归旧代 error 列表"
    assert stale_ready.is_set(), "旧代 error 走旧代 ready（握手按代对象）"
    assert not host._ready.is_set(), (
        "旧代 panelError 不得置位新代 ready（F52l 代隔离）")
    assert host._last_error is None, (
        "旧代 panelError 不得写共享 _last_error（gen_error 提供时）")
    assert not new_done.is_set(), (
        "旧代同 token ACK 不得满足新代等待：空旧代表不得回退到新代表"
        "（F52l 反例：`gen_acks or self._push_acks` 即泄漏）")
    assert stale_acks == {}, "旧代 ACK 写旧代表（本例无匹配，保持为空）"
    assert stale_closed.is_set(), "EOF 只置本代 closed（F52k）"
    assert not new_done.is_set(), (
        "EOF 绝不置任何 ack Event——断联不得伪成功（F52k 反例已封）")


# ---------------------------------------------------------------------------
# 9) F66 push echo 链可观测性（轮9）：echo 发送失败不得静默吞
# ---------------------------------------------------------------------------


def test_pushed_echo_failure_must_leave_reconcilable_trace(capsys):
    """push echo（on_pushed→emit）失败必须留痕，不得静默吞（F34 同族）。

    F66 现场：confirm 推送 ack-timeout-no-echo（进程活、隔离 stderr 0
    字节）。push→echo 链各环均有对账打点（pump ack-line/confirm-
    response/stdin-eof 均写 stderr），唯独 echo 发送环
    （recorder_panel._poll 内 ``on_pushed`` 调用）包 ``except Exception:
    pass``——render 成功而 echo 失败时零痕迹：host 只能报
    ack-timeout-no-echo，面板侧无任何对账证据，断链无法定位。

    勘误（轮10）：轮9 版用 ``__new__`` 存根缺
    ``_region_cancel_request_id`` 等状态，失败发生在 render 前置
    AttributeError——非有效 echo 失败实证。本版改用**真实
    ``RecorderPanel.__init__``**（纯属性初始化、无 Tk——建窗在
    ``run()``，测试不调），render 所触属性全部在位；``_status_label is
    None`` 令 ``_render_ack`` 早退（recorder_panel:1761）不触模态框。
    见证列表证明 ``on_pushed`` 确被调用后，再注入 echo 异常并要求
    stderr 留含 token 痕迹；同时断言无 render 前置异常混入。契约只
    要求「可对账」（实现不限），不强迫结构、不 skip/xfail。"""
    from target.recording.recorder_panel import RecorderPanel

    calls: list[str] = []

    def witness_on_pushed(token: str) -> None:
        calls.append(token)   # 见证：echo 环确被执行
        raise RuntimeError("echo-write-failed")

    panel = RecorderPanel("f66-echo", None,
                          lambda *a, **k: None, on_pushed=witness_on_pushed)
    # run() 未调（无 Tk）：_root/_status_label 保持 __init__ 的 None——
    # _poll 直驱一次排空；_render_ack 走 _status_label is None 早退。
    panel._incoming.put({"ack": "T-f66", "result": {"ok": True}})
    panel._poll()

    assert panel._incoming.empty(), "ack 消息应被排空（render 路径完成）"
    assert calls == ["T-f66"], (
        "on_pushed 必须确被调用——无此见证则本测试未触及 echo 环")
    trace = capsys.readouterr().err
    assert "AttributeError" not in trace, (
        "fixture 缺陷：render 前置异常混入 stderr，非 echo 失败证据")
    assert "T-f66" in trace, (
        "push echo 发送失败零留痕：render 成功而回执失败时 host 只能报 "
        "ack-timeout-no-echo，面板侧无对账证据（F34 同族断链盲点）——"
        "echo 环必须与 pump/confirm-response/stdin-eof 同样留痕")


# ---------------------------------------------------------------------------
# 10) F67 渲染失败语义（轮11）：render 抛错不得被确认渲染成功，
#     pushFailed 信号必须可对账（真实 _poll + 真实 _consume 全链）
# ---------------------------------------------------------------------------


class _RaisingStatusLabel:
    """模拟 Tk 控件态失效：configure 抛错（render 异常的最小忠实注入）。"""

    def configure(self, **_kw):
        raise RuntimeError("tcl-widget-state-failed")


class _OpenStreamProcess:
    """stdout 供帧后保持打开（进程活、流未断）的假面板进程。"""

    def __init__(self, lines, gate):
        self._lines = list(lines)
        self._gate = gate
        self.stdout = iter(self)

    def __iter__(self):
        return self

    def __next__(self):
        if self._lines:
            return self._lines.pop(0)
        self._gate.wait(timeout=5.0)   # 流保持打开=push 等待期语义成立
        raise StopIteration

    def poll(self):
        return None


def test_render_failure_must_not_confirm_success_and_failure_token_must_reconcile(capsys):
    """F68 语义回归：渲染成功才回执；渲染失败零确认帧且留痕可对账。

    演进背景（监督轮11 指出 F67 语义风险后，主会话 F68 修正）：
    F67 曾把 echo 前移到 render 之前（送达即回执），render 抛错时另发
    ``token+":pushFailed:"+类型``——但真实 ``_consume`` 只按
    ``acks.get(str(pushed))`` 精确匹配，后缀 failure token 无等待者且
    原 token done 已被前置 echo 置位 → ``push_to_panel`` 返回 True：
    渲染失败被确认成功。F68（recorder_panel `70b51439`）还原「渲染
    成功才回执」并撤后缀帧；向导改 ``after(0)`` 非阻塞创建。

    本回归钉住 F68 契约（全链真实代码）：
    A) render 抛错（受控控件态注入）→ 零确认帧（无 plain/无后缀）+
       traceback 留痕 → 真实 ``_consume`` 下 done 不置位、流未断 →
       ``push_to_panel`` 按真实等待代码超时如实 False；
    B) render 成功（记录型控件态见证）→ 恰一帧 plain、顺序为
       render 完成→echo；真实 ``_consume`` 置位 done。
    不强迫结构、不 skip/xfail；任何 ``:pushFailed:`` 后缀帧重现即红。"""
    import json
    import threading
    import time

    from target.recording.recorder_panel import RecorderPanel
    from target.recording.ui_controller import RecorderPanelHost

    class _RecordingStatusLabel:
        """记录 configure 调用：见证 render 确已完成（echo 之前）。"""

        def __init__(self, log):
            self._log = log

        def configure(self, **_kw):
            self._log.append("rendered")

    # ── A) render 抛错：零确认帧 + 留痕 + host done 不置位 ──────────
    frames_a: list[str] = []
    panel_a = RecorderPanel(
        "f68-a", None, lambda *a, **k: None,
        on_pushed=lambda t: frames_a.append(
            json.dumps({"pushed": t}, ensure_ascii=False)))
    panel_a._status_label = _RaisingStatusLabel()
    panel_a._incoming.put({"snapshot": {"viewState": "RECORDING"},
                           "ack": "T-f68a"})
    panel_a._poll()
    assert frames_a == [], (
        "render 抛错不得发出任何确认帧（F68 已撤 F67 前置 echo 与 "
        "pushFailed 后缀——后缀帧在真实 _consume 无等待者且会伪造成功）")
    assert "Traceback" in capsys.readouterr().err, (
        "render 异常必须留痕（F28 栈打 stderr）——失败必须可对账")

    host_a = RecorderPanelHost.__new__(RecorderPanelHost)
    host_a._push_acks = {}
    host_a._ready = threading.Event()
    host_a._last_error = None
    acks_a = {"T-f68a": threading.Event()}
    gate_a = threading.Event()
    reader_a = threading.Thread(
        target=host_a._consume,
        args=(_OpenStreamProcess(frames_a, gate_a), threading.Event(),
              acks_a, threading.Event(), threading.Event(), []),
        daemon=True)
    reader_a.start()
    try:
        time.sleep(0.15)   # 给真实 reader 消费窗口（空流：无帧可确认）
        assert not acks_a["T-f68a"].is_set(), (
            "渲染失败时 host 等待事件不得被置位——push_to_panel（done 未"
            "置位）超时如实返回 False，不得谎报送达/渲染成功")
    finally:
        gate_a.set()
        reader_a.join(timeout=2.0)

    # ── B) render 成功：render 完成→echo，恰一帧 plain ─────────────
    order: list[str] = []
    frames_b: list[str] = []

    def on_pushed_b(t: str) -> None:
        order.append("echo:" + t)
        frames_b.append(json.dumps({"pushed": t}, ensure_ascii=False))

    panel_b = RecorderPanel("f68-b", None, lambda *a, **k: None,
                            on_pushed=on_pushed_b)
    panel_b._status_label = _RecordingStatusLabel(order)
    panel_b._detail_label = _RecordingStatusLabel(order)   # :1746 无守卫直用
    panel_b._incoming.put({"snapshot": {"viewState": "RECORDING"},
                           "ack": "T-f68b"})
    panel_b._poll()
    assert order[-1] == "echo:T-f68b" and set(order[:-1]) == {"rendered"}, (
        f"回执必须发生在 render 完成之后（F68 还原序），实得 {order}")
    assert frames_b == [json.dumps({"pushed": "T-f68b"})], (
        "render 成功应恰发一帧 plain 确认")
    assert all(":pushFailed:" not in f for f in frames_b), (
        "pushFailed 后缀帧不得重现（F67 缺陷：真实 _consume 无等待者）")

    host_b = RecorderPanelHost.__new__(RecorderPanelHost)
    host_b._push_acks = {}
    host_b._ready = threading.Event()
    host_b._last_error = None
    acks_b = {"T-f68b": threading.Event()}
    gate_b = threading.Event()
    reader_b = threading.Thread(
        target=host_b._consume,
        args=(_OpenStreamProcess(frames_b, gate_b), threading.Event(),
              acks_b, threading.Event(), threading.Event(), []),
        daemon=True)
    reader_b.start()
    try:
        assert acks_b["T-f68b"].wait(timeout=2.0), (
            "真实 _consume 应消费 render 后的确认帧")
        assert acks_b["T-f68b"].is_set(), (
            "渲染成功→done 置位：push_to_panel（closed 未置位+done 置位）"
            "按真实等待代码返回 True——渲染完成语义成立")
    finally:
        gate_b.set()
        reader_b.join(timeout=2.0)


# ---------------------------------------------------------------------------
# 11) F68 后续（轮12）：延迟向导的 wizardReady 必须与送达可区分，
#     创建失败必须本地可对账（可控 fake root 任务队列驱动真实链路）
# ---------------------------------------------------------------------------


class _WizVar:
    """fake StringVar/BooleanVar：get/set 存值（驱动向导 _build/摘要）。"""

    def __init__(self, value=""):
        self._value = value

    def get(self):
        return self._value

    def set(self, value):
        self._value = value


class _WizWidget:
    """fake tk/ttk 控件：current 同步 textvariable（真实控件语义）。"""

    def __init__(self, parent=None, **kwargs):
        self.kwargs = dict(kwargs)
        self.dialog = None

    def grid(self, **kwargs):
        pass

    def configure(self, **kwargs):
        self.kwargs.update(kwargs)

    def current(self, index):
        variable = self.kwargs.get("textvariable")
        if variable is not None:
            variable.set(self.kwargs["values"][index])

    def bind(self, *args):
        pass

    def title(self, *args):
        pass

    def attributes(self, *args):
        pass

    def protocol(self, *args):
        pass

    def geometry(self, *args):
        pass

    def destroy(self):
        pass


class _WizEntry(_WizWidget):
    """fake ttk.Entry：insert/get 读写（grid 等继承自 _WizWidget）。"""

    def insert(self, _index, text):
        self.kwargs["text"] = text

    def get(self):
        return str(self.kwargs.get("text", ""))


def _wizard_fake_toolkit():
    """驱动真实 RegionWizardDialog._build 完成的最小 fake tk/ttk。"""
    from types import SimpleNamespace

    def make(cls_name):
        def factory(*args, **kwargs):
            if cls_name == "Entry":
                return _WizEntry(*args, **kwargs)
            return _WizWidget(*args, **kwargs)
        return factory

    tk = SimpleNamespace(Toplevel=make("Toplevel"),
                         StringVar=lambda value="": _WizVar(value),
                         BooleanVar=lambda value=False: _WizVar(value),
                         END="end")
    ttk = SimpleNamespace(
        Label=make("Label"), Combobox=make("Combobox"),
        Entry=make("Entry"), Checkbutton=make("Checkbutton"),
        Button=make("Button"))
    return SimpleNamespace(tk=tk, ttk=ttk, messagebox=None)


class _TaskQueueRoot:
    """可控 after 队列：run_due 只执行 0ms 任务（F68 延迟创建入口）。"""

    def __init__(self):
        self._tasks = []

    def after(self, ms, fn):
        self._tasks.append((ms, fn))
        return f"after-{len(self._tasks)}"

    def run_due(self):
        due = [fn for ms, fn in self._tasks if ms == 0]
        self._tasks = [(ms, fn) for ms, fn in self._tasks if ms != 0]
        for fn in due:
            fn()

    def state(self):
        return "withdrawn"

    def winfo_ismapped(self):
        return 0


def _f68_wizard_panel(toolkit, root, frames, token):
    import json

    from target.recording.recorder_panel import RecorderPanel

    panel = RecorderPanel(
        "f68-wizard", None, lambda *a, **k: None,
        on_pushed=lambda t: frames.append(
            json.dumps({"pushed": t}, ensure_ascii=False)))
    panel._root = root
    panel._tk = toolkit.tk if toolkit is not None else None
    panel._ttk = toolkit.ttk if toolkit is not None else None
    panel._messagebox = toolkit.messagebox if toolkit is not None else None
    # 直驱真实 _poll（echo 由 _poll 在 render 返回后发出——轮10/11 同法）
    panel._incoming.put({"ack": token,
                         "result": {"ok": True,
                                    "region": {"state": "confirmed",
                                               "selection": {"container": None,
                                                             "components": []}}}})
    panel._poll()
    return panel


def test_wizard_ready_must_be_distinguishable_from_delivery_and_failure_reconcilable(capsys):
    """延迟向导创建的 wizardReady 语义（F68 after(0) 后的边界钉住）。

    字段承诺核对（server.py region_confirm ~1249）：``panelNotified`` =
    ``push_to_panel`` 返回值 = 送达+render 完成回执（F68 序），**不含**
    向导创建结果——render 只把向导构建排进 after 队列。本回归用可控
    fake root 任务队列驱动真实 ``render/_render_ack`` 与**真实延迟
    ``_open_region_wizard``**（宽容 fake toolkit 令构建可完成）：
    - 成功就绪：排空队列后面板状态可区分（``_region_wizard.dialog``
      非空）——wizardReady 在面板侧可观测；
    - 创建失败（tk 缺失→F40）：``_region_wizard`` 保持 None + stderr
      ``wizard-build-failed`` 留痕（本地可对账），且送达帧不变、无
      任何 host 可见失败帧——失败不得被当作向导就绪，也不得以零痕迹
      吞掉。
    协议边界（不扩协议）：当前 ``panelNotified`` 仅送达/渲染语义；
    **主链欠独立 wizardReady 字段**——host 经 push 通道无法区分向导
    就绪与创建失败，如实记录为本报告结论，不在测试内虚构该字段。"""
    import json

    root = _TaskQueueRoot()
    frames: list[str] = []
    panel = _f68_wizard_panel(_wizard_fake_toolkit(), root, frames,
                              "T-wiz-ok")
    assert frames == [json.dumps({"pushed": "T-wiz-ok"})], (
        "确认送达回执在 render 返回时发出（panelNotified 的字段承诺：送达+"
        "render 完成回执，同步创建含在内）")
    assert panel._region_wizard is not None, (
        "成功就绪场景：真实延迟/同步构造后面板侧向导对象必须在位")
    assert panel._region_wizard.dialog is not None, (
        "wizardReady 必须与送达可区分：面板状态可观测向导已构建")
    assert len(frames) == 1, "成功路径不产生额外帧（panelNotified 语义不变）"

    # 失败路径：tk 缺失 → RegionWizardDialog 构建抛错 → F40 恢复网
    frames_fail: list[str] = []
    panel_fail = _f68_wizard_panel(None, _TaskQueueRoot(), frames_fail,
                                   "T-wiz-bad")
    assert frames_fail == [json.dumps({"pushed": "T-wiz-bad"})], (
        "送达回执与向导结果无关（当前字段承诺）——这正是主链欠 "
        "wizardReady 的边界")
    panel_fail._root.run_due()
    assert panel_fail._region_wizard is None, (
        "创建失败场景：向导对象不得在位（不能被当向导就绪）")
    trace = capsys.readouterr().err
    assert "wizard-build-failed" in trace, (
        "向导创建失败必须本地可对账（F40 留痕），不得零痕迹吞掉")
    assert len(frames_fail) == 1 and ":pushFailed:" not in frames_fail[0], (
        "本 fixture 未接 _host_signal：push 通道在该接线下无向导失败信号"
        "（F69 起向导失败经 _host_signal 发 wizardFailed 帧——三相可对账"
        "见 test_render_failure_must_not_... 与三相回归；:pushFailed: "
        "后缀帧形态已撤，不得重现）")


# ---------------------------------------------------------------------------
# 12) F69/F70 三相回执（轮13）：真实 _poll→_consume 全链状态机
#     received 仅送达不置 done；pushed=渲染完成；wizardReady/Failed 同
#     token 可对账；旧代帧不得污染新代等待。
# ---------------------------------------------------------------------------


def _f70_capture_panel(frames, toolkit=None):
    """真实 RecorderPanel：received/pushed/wizard 三相帧全部入 frames。"""
    import json as _json

    from target.recording.recorder_panel import RecorderPanel

    def emit_frame(msg):
        frames.append(_json.dumps(msg, ensure_ascii=False))

    panel = RecorderPanel("f70", None, lambda *a, **k: None,
                          on_pushed=lambda token: emit_frame({"pushed": token}))
    panel._host_signal = emit_frame   # F69 三相回执桥（真实接线同款）
    panel._root = _TaskQueueRoot()
    if toolkit is not None:
        panel._tk, panel._ttk = toolkit.tk, toolkit.ttk
        panel._messagebox = toolkit.messagebox
    return panel


def test_three_phase_push_frames_reconcile_per_token_without_cross_generation_pollution(capsys):
    """F69/F70 三相回执契约（真实 _poll→真实 _consume，无手工 done.set）。

    A) received 仅送达：render 失败时帧序=[received]、无 pushed → 真实
       _consume 不置 render done——本断言只证 done 通道：F71 起
       push_to_panel 在 received 即返回 True（deliveryAck 语义），
       rendered=False 仅存 _last_push_result（API 未透出=缺口），故
       「render 失败→push 返回 False」在 F71 下不成立，本回归不扩大
       结论；received 若被 host 当渲染完成（置 render done）即红；
    B) 全成功：帧序=[received, wizardReady, pushed]（wizardReady 在
       render 内同步创建后、pushed 在 render 返回后）→ done 由真实
       pushed 帧置位；wizardReady 必带 rootState/mapped（真实可见态）；
    C) 向导失败：帧序=[received, wizardFailed, pushed]（F40 吞后 render
       正常返回）→ 同 token 两帧并存、各自可对账；done 由 pushed 置位
       ——pushed=True 的语义是「渲染完成」，向导结果须由 wizard 帧
       另行对账（F71 起 host 已路由 wizard 帧→_wizard_phase 持久追踪）；
    D) 旧代不污染：旧代 reader（空 gen_acks）消费全部四类帧时，新代
       done 不得被置位（F52l 契约扩展到 received/wizard 帧）。
    不强迫 push_to_panel 同步等向导；F71 起等待在 received 即返回
    （deliveryAck 语义）——panelNotified 已降为送达语义，而
    push_to_panel docstring 仍写「渲染完成后回显」=描述失实必须指出；
    用户流程必须另核 wizard_phase(token) 与保存持久 ACK。"""
    import json
    import threading
    import time

    from target.recording.ui_controller import RecorderPanelHost

    def drain_and_consume(frames, acks, closed, host=None):
        """真实面板排空 → 帧序进真实 _consume（开放流，关键字对齐 F71）。

        host 可传入共享实例：真实产品里 host 对象跨代存活，相位追踪
        （_wizard_phase）的跨代污染必须按「同一 host、两代 reader」
        构造才成立。"""
        gate = threading.Event()
        process = _OpenStreamProcess(list(frames), gate)
        if host is None:
            host = RecorderPanelHost.__new__(RecorderPanelHost)
            host._push_acks = {}
            host._push_acks_received = {}   # F71：received 独立等待表
            host._wizard_phase = {}         # F71：token→相位持久追踪
            host._ready = threading.Event()
            host._last_error = None
        reader = threading.Thread(
            target=host._consume,
            kwargs={"process": process, "stop_evt": threading.Event(),
                    "gen_acks": acks, "gen_acks_received": {},
                    "gen_closed": closed, "gen_ready": threading.Event(),
                    "gen_error": []},
            daemon=True)
        reader.start()
        try:
            yield host
        finally:
            gate.set()
            reader.join(timeout=2.0)

    # ── A) render 失败：received 不置 done ─────────────────────────
    frames_a: list[str] = []
    panel_a = _f70_capture_panel(frames_a)
    panel_a._status_label = _RaisingStatusLabel()
    panel_a._incoming.put({"snapshot": {"viewState": "RECORDING"},
                           "ack": "T-a"})
    panel_a._poll()
    parsed = [json.loads(f) for f in frames_a]
    assert parsed == [{"received": "T-a"}], (
        f"render 失败应仅发 received 送达帧，实得 {parsed}")
    acks_a = {"T-a": threading.Event()}
    for _host in drain_and_consume(frames_a, acks_a,
                                   threading.Event()):
        time.sleep(0.15)
        assert not acks_a["T-a"].is_set(), (
            "received 仅送达：不得置渲染完成 done——host 若把 received "
            "当渲染完成即违反三相契约（push 返回值语义为 deliveryAck，"
            "本断言只证 render done 通道，不证 push 返回值）")

    # ── B) 全成功：wizardReady 带 rootState/mapped，done 由 pushed 置位 ─
    frames_b: list[str] = []
    panel_b = _f70_capture_panel(frames_b, toolkit=_wizard_fake_toolkit())
    panel_b._incoming.put({"ack": "T-b",
                           "result": {"ok": True,
                                      "region": {"state": "confirmed",
                                                 "selection": {
                                                     "container": None,
                                                     "components": []}}}})
    panel_b._poll()
    parsed_b = [json.loads(f) for f in frames_b]
    assert parsed_b[0] == {"received": "T-b"} and any(
        "wizardReady" in p for p in parsed_b) and parsed_b[-1] == {
        "pushed": "T-b"}, f"三相帧序/内容不符：{parsed_b}"
    ready = next(p for p in parsed_b if "wizardReady" in p)
    assert ready["wizardReady"] == "T-b" and "rootState" in ready \
        and "mapped" in ready, (
        "wizardReady 必须携带真实可见态（rootState/mapped）且按 token 对账")
    acks_b = {"T-b": threading.Event()}
    for _host in drain_and_consume(frames_b, acks_b, threading.Event()):
        assert acks_b["T-b"].wait(timeout=2.0), (
            "pushed 帧应经真实 _consume 置位 done（渲染完成语义）")

    # ── C) 向导失败：wizardFailed 与 pushed 同 token 并存、各自可对账 ──
    frames_c: list[str] = []
    panel_c = _f70_capture_panel(frames_c, toolkit=None)   # tk 缺失→F40
    panel_c._incoming.put({"ack": "T-c",
                           "result": {"ok": True,
                                      "region": {"state": "confirmed",
                                                 "selection": {
                                                     "container": None,
                                                     "components": []}}}})
    panel_c._poll()
    parsed_c = [json.loads(f) for f in frames_c]
    failed = next((p for p in parsed_c if "wizardFailed" in p), None)
    assert failed is not None and failed["wizardFailed"] == "T-c" \
        and "error" in failed, f"向导失败帧缺失或无对账字段：{parsed_c}"
    assert parsed_c[-1] == {"pushed": "T-c"}, (
        "F40 吞错后 render 正常返回：pushed（渲染完成）应照发")
    assert "wizard-build-failed" in capsys.readouterr().err, (
        "向导失败必须留本地痕迹")
    acks_c = {"T-c": threading.Event()}
    for _host in drain_and_consume(frames_c, acks_c, threading.Event()):
        assert acks_c["T-c"].wait(timeout=2.0), (
            "pushed 语义不受向导失败影响（渲染完成）——向导结果由 "
            "wizardFailed 帧另行对账（host 解析缺口见报告）")

    # ── D) 旧代不污染：四类帧过旧代 reader，新代 done 不动 ──────────
    frames_d = frames_b + [json.dumps({"wizardFailed": "T-d",
                                       "error": "x"})]
    stale_acks: dict = {}
    stale_closed = threading.Event()
    new_acks = {"T-b": threading.Event()}
    for _host in drain_and_consume(frames_d, stale_acks, stale_closed):
        time.sleep(0.15)
        assert not new_acks["T-b"].is_set(), (
            "旧代 reader 消费 received/pushed/wizard 帧（含同 token "
            "pushed）不得置位新代 done（F52l 代隔离扩展到新帧型）")
        assert stale_acks == {} and not stale_closed.is_set(), (
            "开放流（未 EOF）：旧代无匹配等待者、closed 未置位——"
            "EOF→closed 契约由旧代隔离回归另钉")
        assert not new_acks["T-b"].is_set(), (
            "旧代流量不得污染新代等待状态")

    # ── D2) 相位跨代隔离（轮15，改经公开 wizard_phase 查询——不锁存储
    #    结构）：同一 host 跨代存活=真实产品形态。新代 reader 真实消费
    #    先立 ready 相位；host._push_acks 绑定为新代实际 acks（修后
    #    wizard_phase 以 id(self._push_acks) 判当前代）；旧代 reader
    #    （独立 gen_acks dict=不同代归属）再发同 token 两方向旧代帧。
    #    契约：①当前相位（wizard_phase 默认查询）仍为新代 ready，不随
    #    旧代帧改变；②include_history=True 可见旧代记录且带代归属
    #    （可对账），历史顺序不作要求（独立历史允许）。──
    new_acks_d = {"T-b": threading.Event()}   # 新代实际 acks
    host_d = None
    for shared in drain_and_consume(frames_b, new_acks_d,
                                    threading.Event()):
        host_d = shared   # 新代 reader（真实 ready 相位 + pushed done）
    host_d._push_acks = new_acks_d   # 当前代等待表=新代实际 acks
    current = host_d.wizard_phase("T-b")
    assert current and current.get("phase") == "ready" \
        and current.get("rootState") != "stale-state", (
        f"前置：新代真实消费后当前相位应为新代 ready，实得 {current}")
    assert new_acks_d["T-b"].is_set(), "前置：新代 pushed done 应已置位"
    stale_acks_d: dict = {}   # 旧代独立等待表（不同 id→旧代归属）
    stale_frames_d2 = [
        json.dumps({"wizardFailed": "T-b", "error": "stale-era-crash"}),
        json.dumps({"wizardReady": "T-b", "rootState": "stale-state",
                    "mapped": "0"})]
    for shared in drain_and_consume(stale_frames_d2, stale_acks_d,
                                    threading.Event(), host=host_d):
        time.sleep(0.15)
        current = shared.wizard_phase("T-b")   # 公开查询：仅当前代
        assert current and current.get("phase") == "ready" \
            and current.get("rootState") != "stale-state" \
            and current.get("error") is None, (
            f"同 token 旧代两方向帧改变了当前相位（实得 {current}）："
            "相位判定是用户就绪依据——旧代信息可入独立历史（按代归属"
            "保留），当前相位必须代隔离，不得仅靠 token「应唯一」")
        history = shared.wizard_phase("T-b", include_history=True)
        stale_entries = [e for e in history
                         if e.get("gen") == id(stale_acks_d)]
        assert len(stale_entries) >= 2 and all(
            e.get("phase") in ("ready", "failed")
            for e in stale_entries), (
            f"旧代帧应带代归属入历史可对账（实得 {history}）——"
            "顺序不作要求（独立历史允许）")


# ---------------------------------------------------------------------------
# 13) F72 server 透传分层（轮15，监督②实证实测）：真实 region_confirm_action
#     工具链（无远端；apply_ui_request/push_to_pagination 以 stub 注入，
#     server 侧代码为真实路径）——失败 push 不得透传陈旧 token 的分层
#     结果，且失败原因必须入响应。
# ---------------------------------------------------------------------------


def test_region_confirm_push_layering_must_not_leak_stale_token_result(monkeypatch):
    """真实 server region_confirm_action 链上的失败 push 透传契约：

    push 失败（{"ok": False, "reason": …}）时 panelNotified=False 且
    panelPushReason 必须入响应——当前 `if not pushed:` 对非空 dict 恒
    False=死路径（server.py:1259 实测 RED）；陈旧 _last_push_result
    （上一次 push 的 token/分层）不得透传为本次结果（F72 已加
    token==request_id 校验，本回归守护不回退）。"""
    import json
    from types import SimpleNamespace

    import target.server as srv

    confirm_result = {"ok": True,
                      "region": {"state": "confirmed",
                                 "selection": {"container": None,
                                               "components": []}}}
    monkeypatch.setattr(
        srv, "_recording_ui_controller",
        SimpleNamespace(apply_ui_request=lambda name, args: dict(confirm_result)))

    # ── A) 失败 push + 陈旧 _last_push_result（上一 token 残留）──────
    stale = {"token": "OLD-TOKEN", "deliveryAck": True, "rendered": True,
             "wizardPhase": {"phase": "ready", "rootState": "x"}}
    host = SimpleNamespace(
        push_to_panel=lambda msg, timeout=2.0:
            {"ok": False, "reason": "ack-timeout-no-echo"},
        _last_push_result=stale,
        _last_send_error="ack-timeout-no-echo")
    monkeypatch.setattr(srv, "_recording_panel_host", host)
    resp = json.loads(srv.region_confirm_action())
    assert resp.get("panelNotified") is False, (
        "失败 push：panelNotified 必须为 False")
    assert not any(k in resp for k in
                   ("panelDeliveryAck", "panelRendered", "panelWizardPhase")), (
        f"陈旧 token 的分层结果不得透传为本次结果（实得 {resp}）："
        "守卫必须校验 _last_push_result.token==本次 request_id")
    assert resp.get("panelPushReason") == "ack-timeout-no-echo", (
        f"失败原因必须入响应（实得 {resp}）：push 返回 dict 后 "
        "`if not pushed:` 恒 False=死路径")


def test_region_confirm_push_success_layering_follows_current_token(monkeypatch):
    """真实 server region_confirm_action 链上的成功 push 基线：分层字段
    随本次 token 如实透传（防止修复失败路径时把成功路径一并切断；
    _last_push_result 由真实 push 语义在返回前写入）。"""
    import json
    from types import SimpleNamespace

    import target.server as srv

    confirm_result = {"ok": True,
                      "region": {"state": "confirmed",
                                 "selection": {"container": None,
                                               "components": []}}}
    monkeypatch.setattr(
        srv, "_recording_ui_controller",
        SimpleNamespace(apply_ui_request=lambda name, args: dict(confirm_result)))
    host2 = SimpleNamespace(_last_send_error=None)

    def fake_push(msg, timeout=2.0):
        token = str(msg.get("ack"))
        host2._last_push_result = {"token": token, "deliveryAck": True,
                                   "rendered": True,
                                   "wizardPhase": {"phase": "ready",
                                                   "rootState": "normal",
                                                   "mapped": "1"}}
        return dict(host2._last_push_result, ok=True)

    host2.push_to_panel = fake_push
    monkeypatch.setattr(srv, "_recording_panel_host", host2)
    resp2 = json.loads(srv.region_confirm_action())
    assert resp2.get("panelNotified") is True and \
        resp2.get("panelRendered") is True and \
        resp2.get("panelDeliveryAck") is True and \
        resp2.get("panelWizardPhase") == {"phase": "ready",
                                          "rootState": "normal",
                                          "mapped": "1"}, (
        f"成功 push：分层字段必须随本次 token 如实透传（实得 {resp2}）")
