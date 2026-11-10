"""C05→落盘探针（设计11 §9 D1 遗留项 / §5.1，真实链路，先红后绿）。

1500 片给 ControlInventory 修了 window_changed 空读补拍，并自建了
``skipped``（结构化跳过，per-reason）与 ``retried``（补拍，per-reason）
两个透明计数器（§5.1：fallback 必须计数且可解释，不静默）。但两个计数
只活在 inventory 对象上：

- source.diagnostics()（F52 status 出口）不含任何 inventory 字段；
- session.recording() 落盘 captureDiagnostics 是固定 6 键投影，无
  inventory 任何字段；
- inventory 自身 diagnostics() 只暴露 snapshotCount/skipped/errors，
  连 retried 都不出去。

后果与 C06 同型（方向相反：那次是"字段在、无人递增"，这次是"计数在、
管道断"）——一份真实录制里发生过的菜单空读与补拍，在最终文档中不可见，
按 reason 分型的证据链在落盘层断掉。

探针A（负例原料，当前代码应红）：真实 QueuedCaptureSource + 真实
WindowsUIACorrelator + 真实 ControlInventory + 真实 RecordingSession。
SHOW walk 空读 → population 完成 → 补拍成功。停止后落盘文档的
captureDiagnostics 必须含 inventorySkipped/inventoryRetried 两个计数
（总数），且补拍成功时 retried >= 1、skipped["no_controls"] >= 1 的
事实在文档可核对（至少非零，证明空读与恢复到达了落盘层）。
探针B（零语义锁定）：无 inventory 的 source（macOS resolver / 显式
不挂 inventory 的组合）落盘读 0，不出 unknown_field，schema 契约
（additionalProperties:false）不破。
"""
from __future__ import annotations

import json
import threading
import time

import pytest

from target.recording.inventory import ControlInventory
from target.recording.models import CaptureScope
from target.recording.source import HookPacket, QueuedCaptureSource
from target.recording.windows import WindowsUIACorrelator

pytestmark = pytest.mark.unit

SCOPE = CaptureScope("target", "windows_pywinauto", "EDRClient.exe", "^logo1$")

MENU_HANDLE = 6751880  # live-REC-647312 第4菜单的真实 handle
MAIN_HANDLE = 13239958


class _PopulationResolver:
    """菜单 SHOW 后首读空、随后 population 完成并保持完整的 resolver。

    与 1500 探针同一形状；补拍（空读+500ms）发生在菜单仍可见的窗口内。
    """

    def __init__(self, populate_after: int) -> None:
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

    def control_tree(self, handle, max_depth=12):
        self.walks.append(handle)
        if self.destroyed.is_set():
            return None
        if handle == MENU_HANDLE and len(self.walks) < self.populate_after:
            return {"ok": True, "title": "EDRClient", "complete": False,
                    "controls": []}
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


class _NoopDriver:
    def start(self, emit): ...
    def stop(self): ...


def _trans(kind, ms, handle, title="EDRClient"):
    return HookPacket(
        kind="window_transition", monotonic_ms=ms,
        wall_time="2026-11-06T00:00:00.000Z",
        native={"kind": kind, "processName": "EDRClient.exe",
                "title": title, "pid": 42, "handle": handle},
    )


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _run_source_with_inventory(resolver, inventory):
    """真实生产链：source.start() → correlator.seed_scope → inventory.start；
    worker 出队 correlate → SHOW walk 请求 → 空读 skip + 补拍。"""
    from target.recording.session import RecordingSession
    session = RecordingSession("REC-C05D", "review", SCOPE)
    source = QueuedCaptureSource(
        scope=SCOPE, sink=session.ingest, driver=_NoopDriver(),
        correlator=WindowsUIACorrelator(resolver, inventory=inventory),
        inventory=inventory)
    session._source = source
    session.start()
    return session, source


# --- 探针A：真实链上的 skip/补拍计数必须到达落盘文档（预期红） ----------

def test_c05_inventory_skip_and_retry_counts_reach_persisted_document():
    """§5.1：结构化 skip 与补拍在落盘层可见。菜单 SHOW walk 空读
    （skipped.no_controls=1）→ population 完成 → 补拍成功（retried
    .no_controls=1、菜单帧在手）。停止后 session.recording() 的
    captureDiagnostics 必须携带这两个计数的落盘投影。"""
    resolver = _PopulationResolver(populate_after=3)  # seed#1 + 菜单首读#2 空
    inventory = ControlInventory(resolver)  # 生产默认 debounce 1500ms
    session, source = _run_source_with_inventory(resolver, inventory)
    try:
        correlator = source._correlator
        correlator.correlate(_trans("show", 100, MENU_HANDLE), SCOPE, 1)
        ok = _wait(lambda: any(
            s["window"].get("handle") == MENU_HANDLE and s["complete"]
            for s in inventory.snapshots))
        assert ok, (
            f"补拍未保住菜单帧（walks={resolver.walks}, "
            f"skipped={inventory.skipped}, retried={inventory.retried}）"
            "—— 本探针以 1500 已修复行为为前置")
    finally:
        _, recording = session.stop()

    # 前置事实（inventory 对象上真实发生过的）：
    assert inventory.skipped.get("no_controls") == 1, inventory.skipped
    assert inventory.retried.get("no_controls") == 1, inventory.retried

    diag = recording.capture_diagnostics
    assert diag.get("inventorySkipped") == 1, (
        f"C05→落盘缺口:inventory skip 计数未到达落盘文档,diag={diag}。"
        "§5.1:结构化跳过必须计数且在最终文档可解释,不能只活在内存对象上")
    assert diag.get("inventoryRetried") == 1, (
        f"C05→落盘缺口:inventory 补拍计数未到达落盘文档,diag={diag}。"
        "1500 修复的恢复动作本身必须可审计")
    # per-reason 明细进 status 出口（F52），供排障时读 reason 分型。
    status_diag = source.diagnostics()
    assert status_diag.get("inventorySkipped", {}).get("no_controls") == 1, (
        status_diag.get("inventorySkipped"))
    assert status_diag.get("inventoryRetried", {}).get("no_controls") == 1, (
        status_diag.get("inventoryRetried"))


# --- 探针B：无 inventory 时读 0 且不出 unknown_field（schema 契约） ----

def test_c05_inventory_counts_default_zero_and_keep_schema_parity():
    """显式不挂 inventory 的 source（当前 factory 之外的真实组合）落盘
    读 0；模型允许集与 JSON schema（additionalProperties:false）同步
    —— 模型接受而 schema 拒绝（或反之）的文档不得存在。"""
    from target.recording.session import RecordingSession
    resolver = _PopulationResolver(populate_after=10**9)
    session = RecordingSession("REC-C05E", "review", SCOPE)
    source = QueuedCaptureSource(
        scope=SCOPE, sink=session.ingest, driver=_NoopDriver(),
        correlator=WindowsUIACorrelator(resolver), inventory=None)
    session._source = source
    session.start()
    correlator = source._correlator
    correlator.correlate(_trans("show", 100, MAIN_HANDLE), SCOPE, 1)
    _wait(lambda: source.diagnostics()["queueDepth"] == 0)
    time.sleep(0.05)
    _, recording = session.stop()

    diag = recording.capture_diagnostics
    assert diag.get("inventorySkipped", 0) == 0, diag
    assert diag.get("inventoryRetried", 0) == 0, diag

    # 模型/schema 同步（test_raw_v2_window_timeline 同型断言）。
    import jsonschema
    from pathlib import Path
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "schema"
         / "desktop-recording.schema.json").read_text(encoding="utf-8"))
    document = recording.to_dict()
    document["schema"] = "edr.desktop-recording/v2"
    jsonschema.validate(document, schema)
