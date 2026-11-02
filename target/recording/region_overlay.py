"""Region-assertion selection overlay: pure state machine + hotkey plan (A轨).

设计依据 docs/architecture/06-region-multicomponent-assertion-design.md §2/§5：

- Ctrl+Shift+Q 是唯一断言入口（用户裁定，2026-10-26）；录制中或暂停时可用；服务侧校验由
  ``RecordingService.begin_region_assertion``（B 轨）负责，本模块只维护
  遮罩视图状态机。
- 选区是**管理输入**：状态机不产生任何业务 ``RawCaptureEvent``——它根本
  不持有 ingest 通道；高亮数据来自既有 ``control_inventory``（"list_controls"
  路由），条目自带 ``rect`` 时才参与几何框选。
- Esc / 取消 / 失败都恢复进入前状态；任何一路退出都清空选择。
- 展示所选组件的名称/类型/当前可读内容/所属窗口；读不到显示「无法读取」，
  绝不补空值冒充观测（``value is None`` 或无读取状态 → unknown）。
- 快捷键注册失败必须明确告知，不允许悄悄改键。

平台相关绘制（半透明全屏窗口、高亮框）属于实机桌面细节，留在面板子进程；
本模块保持离线可测（沿用 PickerController 的注入风格）。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from collections import deque
from typing import Any, Callable, Mapping, Sequence

try:  # target-local deployment keeps modules importable standalone
    from .models import RecordingModelError
except ImportError:  # pragma: no cover - flat deployment layout
    from models import RecordingModelError  # type: ignore[no-redef]

REGION_HOTKEY_LABEL = "Ctrl+Shift+Q"

# 拖选语义（06 §2）：松开鼠标后，矩形内可识别组件自动高亮并预选，
# 随后逐个增选/取消，或改选整个容器。
DRAG_CLICK_TOLERANCE_PX = 4

# control_inventory 条目里可充当"整个容器"的控制类型（平面列表无层级，
# 按与框选矩形的相交面积取最大者）。
CONTAINER_CONTROL_TYPES = frozenset({
    "List", "Table", "DataGrid", "Tree", "TreeItem", "Pane", "Group",
    "DataItem", "Client", "Document", "Window", "Custom",
})

STATE_IDLE = "idle"
STATE_SELECTING = "selecting"    # 遮罩已显示，等待拖出矩形
STATE_MARKING = "marking"        # 矩形已固定，可增选/取消组件
STATE_CONFIRMED = "confirmed"    # 已确认所选，进入向导

UNREADABLE_TEXT = "无法读取"

_ACTIVE_VIEW_STATES = frozenset({"RECORDING", "PAUSED"})


# ── 快捷键（唯一断言入口） ──────────────────────────────────────────


def plan_hotkey_registration(platform: str) -> dict[str, Any]:
    """How this platform binds the single assertion hotkey.

    Windows 用原生 RegisterHotKey（全局，业务窗口有焦点也能呼出）；
    macOS 没有同等的无权限全局热键，只能 Tk bind 尽力而为，并显式标注
    局限——绝不注册第二个键冒充统一入口。
    """
    if platform == "win32":
        return {
            "mode": "register_hotkey",
            "label": REGION_HOTKEY_LABEL,
            "modifiers": {"ctrl": True, "alt": False, "shift": True},
            "virtualKey": 0x51,   # 'Q'
        }
    sequence = "<Control-Shift-q>"
    annotation = (
        "macOS 尽力而为：仅当录制面板获得键盘焦点时可触发，"
        "不注册全局热键、不改用其他按键"
    )
    if platform != "darwin":
        annotation = (
            "当前平台无全局热键实现：仅当录制面板获得键盘焦点时"
            "可触发（尽力而为），不改用其他按键"
        )
    return {"mode": "tk_bind", "label": REGION_HOTKEY_LABEL,
            "sequence": sequence, "annotation": annotation}


HOTKEY_ID = 0xB00B
_hotkey_thread_id: int | None = None
_hotkey_register_error = 0


def request_hotkey_shutdown() -> bool:
    """向热键线程投递 WM_QUIT：消息循环退出即 UnregisterHotKey 释放热键。

    面板失联退出（F16）必须释放热键，否则新面板注册冲突（孤儿面板问题）。
    热键线程未运行/已退出返回 False。
    """
    import ctypes

    thread_id = _hotkey_thread_id
    if not thread_id:
        return False
    try:
        return bool(ctypes.windll.user32.PostThreadMessageW(
            thread_id, 0x0012, 0, 0))   # WM_QUIT
    except Exception:
        return False


def win32_hotkey_thread_body(
    on_activate: Callable[[], None],
    on_registered: Callable[[], None] | None = None,
) -> bool:
    """Register Ctrl+Shift+Q on the calling thread and pump its message loop.

    必须在注册线程上取消息；线程常驻（daemon）。注册失败返回 False——
    调用方据此给面板明确错误，而不是换个键。
    F16：``on_registered`` 在 RegisterHotKey 成功后立即回调——握手与
    消息循环寿命分离（此前调用方等线程返回，常驻泵永不返回 → 正常成功
    也误报"注册超时"红字）。消息循环退出（WM_QUIT）即 UnregisterHotKey。
    """
    import ctypes
    from ctypes import wintypes

    global _hotkey_thread_id, _hotkey_register_error
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.windll.kernel32
    MOD_SHIFT = 0x0004
    MOD_CONTROL = 0x0002
    WM_HOTKEY = 0x0312
    _hotkey_thread_id = int(kernel32.GetCurrentThreadId())
    def _hlog(message_text: str) -> None:
        """F34：热键链观测（stderr=panel 日志通道，F33 重定向后落盘）。"""
        try:
            import sys
            print(f"[hotkey] {message_text}", file=sys.stderr, flush=True)
        except Exception:
            pass

    if not user32.RegisterHotKey(None, HOTKEY_ID, MOD_CONTROL | MOD_SHIFT, 0x51):
        _hotkey_thread_id = None
        try:
            _hotkey_register_error = int(ctypes.get_last_error())
        except Exception:
            _hotkey_register_error = 0
        _hlog(f"register-failed err={_hotkey_register_error}")
        return False
    if on_registered is not None:
        try:
            on_registered()
        except Exception:  # noqa: BLE001 - 握手回调失败不影响注册本身
            pass
    _hlog(f"pump-start thread={_hotkey_thread_id} id={HOTKEY_ID:#x}")
    message = wintypes.MSG()
    while True:
        ret = user32.GetMessageW(ctypes.byref(message), None, 0, 0)
        if ret == -1:
            try:
                err = int(ctypes.get_last_error())
            except Exception:
                err = -1
            _hlog(f"loop-exit reason=getmessage-error err={err}")
            break
        if ret == 0:
            _hlog("loop-exit reason=wm-quit")
            break
        if message.message == WM_HOTKEY:
            _hlog(f"wm-hotkey thread={_hotkey_thread_id}")
            try:
                on_activate()
                _hlog("on-activate ok")
            except Exception:  # noqa: BLE001 - 记录后继续（循环不死）
                import traceback
                try:
                    traceback.print_exc(file=sys.stderr)
                except Exception:
                    pass
                _hlog("on-activate error (traceback above)")
    user32.UnregisterHotKey(None, HOTKEY_ID)
    _hotkey_thread_id = None
    _hlog("loop-exit done unregistered")
    return True


def register_region_hotkey(
    on_activate: Callable[[], None],
    *,
    root: Any = None,
    platform: str | None = None,
    register_fn: Callable[[Callable[[], None]], bool] | None = None,
    wait_timeout: float = 2.0,
) -> dict[str, Any]:
    """Bind the assertion hotkey, reporting failure explicitly.

    ``register_fn`` 注入点：离线测试传入假注册函数。Windows 路径在
    daemon 线程注册并等结果；失败（返回 False/抛错/超时）一律返回
    ``ok=False`` 与中文原因——调用方必须把失败显示给用户，不得改键。
    """
    platform = platform or __import__("sys").platform
    plan = plan_hotkey_registration(platform)
    if plan["mode"] == "tk_bind":
        annotation = plan.get("annotation", "")
        if root is not None:
            try:
                root.bind_all(plan["sequence"], lambda _event: on_activate())
            except Exception as exc:  # noqa: BLE001 - binding failure is fatal
                return {
                    "ok": False, "mode": plan["mode"],
                    "error": f"快捷键 {plan['label']} 绑定失败：{exc}",
                    "annotation": annotation,
                }
        return {"ok": True, "mode": plan["mode"], "label": plan["label"],
                "annotation": annotation}

    outcome: dict[str, Any] = {}
    import threading

    done = threading.Event()

    def runner() -> None:
        def _on_registered() -> None:
            done.set()   # F16：注册成功即握手，不等消息循环寿命

        try:
            if register_fn is None:
                outcome["ok"] = bool(win32_hotkey_thread_body(
                    on_activate, on_registered=_on_registered))
            else:
                # 离线注入的假注册函数保持旧语义：返回即握手完成。
                outcome["ok"] = bool(register_fn(on_activate))
        except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
            outcome["error"] = str(exc)
        finally:
            done.set()

    threading.Thread(target=runner, name="edr-wd-region-hotkey",
                     daemon=True).start()
    if not done.wait(timeout=wait_timeout):
        return {
            "ok": False, "mode": plan["mode"],
            "error": (f"快捷键 {REGION_HOTKEY_LABEL} 注册超时，断言入口不可用；"
                      "不会改用其他按键，请检查系统后重启录制面板。"),
        }
    if outcome.get("error"):
        payload = {
            "ok": False, "mode": plan["mode"],
            "error": (f"快捷键 {REGION_HOTKEY_LABEL} 注册失败："
                      f"{outcome['error']}。断言入口不可用；"
                      "不会改用其他按键，请解决冲突后重启录制面板。"),
        }
        if _hotkey_register_error:
            payload["errorCode"] = _hotkey_register_error   # F33：可判读
        return payload
    if outcome.get("ok") is False:
        # 注意：真机常驻泵路径 outcome 可能为空（线程仍在 GetMessage）——
        # done 已由 on_registered 握手置位即成功；仅显式 False 才是失败。
        conflict = hotkey_conflict_note(_hotkey_register_error)
        if conflict:
            return {
                "ok": False, "mode": plan["mode"],
                "error": (f"快捷键 {REGION_HOTKEY_LABEL} 注册失败：{conflict}"),
                "errorCode": _hotkey_register_error,   # F33：1409 可判读
            }
        return {
            "ok": False, "mode": plan["mode"],
            "error": (f"快捷键 {REGION_HOTKEY_LABEL} 注册失败"
                      "（可能已被其他程序占用）。断言入口不可用；"
                      "不会改用其他按键，请解决冲突后重启录制面板。"),
        }
    return {"ok": True, "mode": plan["mode"], "label": REGION_HOTKEY_LABEL}


# ── 选区状态机 ──────────────────────────────────────────────────────


def _normalize_rect(rect: Any) -> tuple[int, int, int, int]:
    """Accept ``[l, t, r, b]`` (or mapping) and normalize negative extents."""
    if isinstance(rect, Mapping):
        try:
            values = (rect["left"], rect["top"], rect["right"], rect["bottom"])
        except KeyError as exc:
            raise RecordingModelError(
                "region_rect_invalid",
                f"选区矩形缺少字段：{exc}", path="rect",
            ) from exc
    elif isinstance(rect, Sequence) and not isinstance(rect, (str, bytes)):
        values = tuple(rect)
    else:
        raise RecordingModelError(
            "region_rect_invalid", "选区矩形必须是 [l,t,r,b]", path="rect")
    if len(values) != 4:
        raise RecordingModelError(
            "region_rect_invalid", "选区矩形必须是 4 个数值 [l,t,r,b]",
            path="rect")
    try:
        left, top, right, bottom = (int(v) for v in values)
    except (TypeError, ValueError) as exc:
        raise RecordingModelError(
            "region_rect_invalid", f"选区矩形含非数值：{values!r}",
            path="rect",
        ) from exc
    return (min(left, right), min(top, bottom),
            max(left, right), max(top, bottom))


def _rects_intersect(a: Sequence[int], b: Sequence[int]) -> bool:
    al, at, ar, ab = a
    bl, bt, br, bb = b
    return not (ar <= bl or br <= al or ab <= bt or bb <= at)


def _rect_area(rect: Sequence[int]) -> int:
    return max(0, rect[2] - rect[0]) * max(0, rect[3] - rect[1])


def _normalize_inventory(entries: Sequence[Mapping[str, Any]]) -> list[dict]:
    """Flatten host inventory entries into machine records.

    每条记录带稳定 ``key``；``rect`` 可选——无几何信息的条目（既有
    control_inventory 在部分平台不含 rect）不能参与拖选命中，但保留供
    逐个增选与后续向导引用。
    """
    normalized: list[dict] = []
    for index, entry in enumerate(entries or []):
        if not isinstance(entry, Mapping):
            continue
        component = dict(entry.get("component") or entry)
        rect = entry.get("rect") or entry.get("rectangle") or component.get("rect")
        rect = _normalize_rect(rect) if rect else None
        window = dict(entry.get("window") or {})
        read_status = str(entry.get("readStatus") or (
            "ok" if component.get("value") is not None else "unknown"))
        normalized.append({
            "key": str(entry.get("key") or f"c{index}"),
            "component": component,
            "rect": rect,
            "window": window,
            "readStatus": read_status,
            "readError": entry.get("readError"),
        })
    return normalized


def _intersect_area(a: Sequence[int], b: Sequence[int]) -> int:
    left = max(a[0], b[0])
    top = max(a[1], b[1])
    right = min(a[2], b[2])
    bottom = min(a[3], b[3])
    if right <= left or bottom <= top:
        return 0
    return (right - left) * (bottom - top)


class RegionSelectionController:
    """begin → update_rect → toggle* → confirm / cancel，纯逻辑状态机。

    与 PickerController 相同的离线可测风格：状态恢复与窗口校验通过注入
    的数据完成（这里由 ``RecorderUIController`` 从服务侧结果喂入），本类
    不做 I/O、不持有捕获通道，因此选区操作不可能产生业务事件。
    """

    def __init__(self, *, monotonic: Callable[[], float] = time.monotonic) -> None:
        self._monotonic = monotonic
        self._state = STATE_IDLE
        self._window: dict[str, Any] = {}
        self._inventory: list[dict] = []
        self._by_key: dict[str, dict] = {}
        self._rect: tuple[int, int, int, int] | None = None
        self._selected: dict[str, dict] = {}
        self._container_key: str | None = None
        self._previous_state: str | None = None
        self._started_at: float | None = None

    # -- 生命周期 -----------------------------------------------------

    @property
    def active(self) -> bool:
        return self._state in {STATE_SELECTING, STATE_MARKING}

    @property
    def state(self) -> str:
        return self._state

    def begin(self, *, window: Mapping[str, Any] | None,
              inventory: Sequence[Mapping[str, Any]] | None,
              view_state: str) -> dict[str, Any]:
        """Start a region session.  ``view_state`` 来自服务侧校验结果
        （RECORDING/PAUSED，大小写不敏感）；再校验一次属于纵深防御。"""
        if self.active:
            return {"ok": True, "duplicate": True, "region": self.payload()}
        normalized_state = str(view_state or "").upper()
        if normalized_state not in _ACTIVE_VIEW_STATES:
            raise RecordingModelError(
                "recording_state_invalid",
                "区域断言只能在录制中或暂停时使用",
                path="session.state",
            )
        self._window = dict(window or {})
        self._inventory = _normalize_inventory(inventory or [])
        self._by_key = {entry["key"]: entry for entry in self._inventory}
        self._rect = None
        self._selected = {}
        self._container_key = None
        self._previous_state = normalized_state
        self._started_at = self._monotonic()
        self._state = STATE_SELECTING
        return {"ok": True, "region": self.payload()}

    def update_rect(self, rect: Any) -> dict[str, Any]:
        """Fix the rubber-band rect and preselect intersecting components.

        框选预选的是**组件**：容器类型（List/Table/…）不自动选中——它们由
        「选择整个容器」单独给出（06 §2：列表优先给出容器及行、列结构，
        容器与行是两种不同的选择）。逐个 toggle 仍可显式增选任何条目。
        """
        self._require_active()
        self._rect = _normalize_rect(rect)
        self._state = STATE_MARKING
        for entry in self._inventory:
            entry_rect = entry.get("rect")
            if entry_rect is None or not _rects_intersect(entry_rect, self._rect):
                continue
            control_type = str(entry["component"].get("controlType") or "")
            if control_type in CONTAINER_CONTROL_TYPES:
                continue
            self._selected.setdefault(entry["key"], entry)
        return {"ok": True, "region": self.payload()}

    def toggle(self, key: str | None = None, *,
               container: bool = False) -> dict[str, Any]:
        """增选/取消一个组件，或切换「整个容器」。"""
        self._require_active()
        if container:
            candidate = self._largest_container()
            if candidate is None:
                raise RecordingModelError(
                    "region_container_not_found",
                    "所选区域内没有可识别的容器组件（列表/表格/面板）",
                    path="region.container",
                )
            if self._container_key == candidate["key"]:
                self._container_key = None
            else:
                self._container_key = candidate["key"]
            return {"ok": True, "region": self.payload()}
        entry = self._by_key.get(str(key or ""))
        if entry is None:
            raise RecordingModelError(
                "region_component_unknown",
                f"未知组件 {key!r}：请重新框选后选择",
                path="region.key",
            )
        if entry["key"] in self._selected:
            del self._selected[entry["key"]]
        else:
            self._selected[entry["key"]] = entry
        return {"ok": True, "region": self.payload()}

    def confirm(self) -> dict[str, Any]:
        """Freeze the selection for the wizard; at least one target required."""
        self._require_active()
        if not self._selected and self._container_key is None:
            raise RecordingModelError(
                "region_selection_empty",
                "尚未选择任何组件或容器：请拖出矩形并选择目标",
                path="region.selection",
            )
        self._state = STATE_CONFIRMED
        return {"ok": True, "region": self.payload()}

    def cancel(self) -> dict[str, Any]:
        """Esc/取消：清空遮罩状态，恢复进入前状态由服务侧完成。"""
        self._require_active()
        previous = self._previous_state
        self._reset()
        return {"ok": True, "cancelled": True, "previousState": previous}

    # -- 展示 ---------------------------------------------------------

    def payload(self) -> dict[str, Any]:
        """Everything the overlay window needs to draw and display."""
        highlights = [
            {"key": entry["key"], "rect": list(entry["rect"]),
             "selected": entry["key"] in self._selected}
            for entry in self._inventory if entry.get("rect")
        ]
        return {
            "state": self._state,
            "window": dict(self._window),
            "rect": list(self._rect) if self._rect else None,
            "highlights": highlights,
            "candidateCount": sum(1 for e in self._inventory if e.get("rect")),
            "selection": self.selection_summary(),
        }

    def selection_summary(self) -> dict[str, Any]:
        """所选组件的名称/类型/当前可读内容/所属窗口。

        读不到的内容显示「无法读取」——``value is None`` 或读取状态非 ok
        时如实呈现，不补空值冒充观测（空字符串是合法读取值，保留）。
        """
        return {
            "window": dict(self._window),
            "container": (self._display(self._by_key[self._container_key])
                          if self._container_key else None),
            "components": [self._display(entry) for entry in self._selected.values()],
        }

    # -- 内部 ---------------------------------------------------------

    def _display(self, entry: dict) -> dict[str, Any]:
        component = entry["component"]
        read_status = entry.get("readStatus") or "unknown"
        value = component.get("value")
        readable = read_status == "ok" and value is not None
        window = entry.get("window") or self._window
        return {
            "key": entry["key"],
            "name": component.get("name") or component.get("automationId")
            or "（未命名组件）",
            "controlType": component.get("controlType") or "未知类型",
            "contentText": str(value) if readable else UNREADABLE_TEXT,
            # 原始观测值：仅读取成功时非 None（空串是合法读取值）；向导的
            # sampleValue 只取这份真实观测，读不到绝不补空值。
            "sampleValue": value if readable else None,
            "readStatus": read_status,
            "readError": entry.get("readError"),
            "window": {
                "title": window.get("title") or self._window.get("title") or "",
                "processName": window.get("processName")
                or self._window.get("processName") or "",
            },
            # 供向导引用的身份字段（不含 handle/pid —— 它们是 ephemeral，
            # 只允许出现在会话校验与 evidence.window，见 models.py）。
            "identity": {
                "controlType": component.get("controlType"),
                "automationId": component.get("automationId"),
                "identifier": component.get("identifier"),
                "name": component.get("name"),
            },
        }

    def _largest_container(self) -> dict | None:
        candidates = [
            entry for entry in self._inventory
            if entry.get("rect") and self._rect
            and _intersect_area(entry["rect"], self._rect) > 0
            and str(entry["component"].get("controlType") or "")
            in CONTAINER_CONTROL_TYPES
        ]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda entry: (
                _intersect_area(entry["rect"], self._rect or (0, 0, 0, 0)),
                _rect_area(entry["rect"]),
            ),
        )

    def _require_active(self) -> None:
        if not self.active:
            raise RecordingModelError(
                "region_session_inactive",
                "当前没有进行中的区域选区",
                path="region",
            )

    def _reset(self) -> None:
        self._state = STATE_IDLE
        self._inventory = []
        self._by_key = {}
        self._rect = None
        self._selected = {}
        self._container_key = None
        self._window = {}
        self._previous_state = None
        self._started_at = None


__all__ = [
    "RegionSelectionController", "REGION_HOTKEY_LABEL",
    "plan_hotkey_registration", "register_region_hotkey",
    "request_hotkey_shutdown", "input_diag_enabled", "register_rawinput_sink",
    "overlay_geometry_desynced", "valve_gate_enabled", "VALVE_GATE_ENV",
    "overlay_synth_enabled", "OVERLAY_SYNTH_ENV",
    "btn_forward_enabled", "BTN_FORWARD_ENV", "button_event_sequence",
    "btn_from_motion_enabled", "BTN_FROM_MOTION_ENV",
    "input_death_detect_enabled", "INPUT_DEATH_DETECT_ENV",
    "btn_from_ll_enabled", "BTN_FROM_LL_ENV",
    "KEY_VK_RETURN",
    "lparam_to_client_coords",
    "acquire_panel_instance_mutex", "evaluate_mutex_wait",
    "region_input_grace_seconds",
    "hotkey_conflict_note", "HOTKEY_ERR_ALREADY_REGISTERED",
    "MouseHookDiagProbe", "ll_hook_record",
    "win32_hotkey_thread_body", "STATE_IDLE", "STATE_SELECTING",
    "STATE_MARKING", "STATE_CONFIRMED", "UNREADABLE_TEXT",
    "CONTAINER_CONTROL_TYPES", "DRAG_CLICK_TOLERANCE_PX",
    "RegionInputValve", "region_valve_log_path",
    "REGION_INPUT_GRACE_SECONDS", "REGION_MOVE_CONFIRM_SAMPLES",
    "REGION_HEARTBEAT_STALE_SECONDS", "REGION_VALVE_LOG_NAME",
    "win32_input_probe", "clip_is_suspicious",
    "router_diag_enabled", "warp_recalc_enabled",
    "REGION_ROUTER_DIAG_ENV", "REGION_WARP_RECALC_ENV",
]


# ── 输入死亡安全阀（11.26 实机遮罩吞输入事件的防线） ────────────────

REGION_VALVE_LOG_NAME = "edr-wd-region-valve.log"
REGION_INPUT_GRACE_SECONDS = 60.0     # 从未收到任何输入确认 → 到期自动取消
REGION_MOVE_CONFIRM_SAMPLES = 3       # 指针连续移动采样数（≈3 秒判死）
REGION_MOVE_MIN_DISTANCE_PX = 2
REGION_HEARTBEAT_STALE_SECONDS = 8.0  # Tcl 心跳失联 → 看门狗硬退出兜底

# F7 构建回归的两层开关（setx 只影响新进程——改完必须整个重启服务链）：
# REGION_ROUTER_DIAG=1 才启用 bind_all 诊断/兜底层（默认关——V5 实机
# 实锤：安装该层后 11.26 上全部输入派发死亡，缺陷②；根因未明前不入
# 默认路径。本地 macOS 合成事件实验证明绑定机制本身健康（widget→all
# 顺序正常），死亡在 OS 投递/指针解析层）。
# REGION_WARP_RECALC=1 才启用指针 warp 强刷（默认关——它是 F7 回归的
# 头号嫌疑：warp 会把光标拉到遮罩中心，与注入驱动的 SetCursorPos 拖拽
# 序列互相打架，500ms 重复 warp 更会在拖拽中途抢光标）。
REGION_ROUTER_DIAG_ENV = "REGION_ROUTER_DIAG"
REGION_WARP_RECALC_ENV = "REGION_WARP_RECALC"


def router_diag_enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = environ if environ is not None else os.environ
    return env.get(REGION_ROUTER_DIAG_ENV, "0") == "1"


def warp_recalc_enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = environ if environ is not None else os.environ
    return env.get(REGION_WARP_RECALC_ENV, "0") == "1"


def _token_integrity_level(process_handle: int) -> str | None:
    """进程 TokenIntegrityLevel 十六进制串（0x3000=Medium/0x4000=High/
    0x5000=System）。F12 判决=OS 投递层丢按钮——UIPI 完整性级错位是头号
    嫌疑（服务子进程高 IL vs 注入器中 IL：按钮被静默丢弃、移动放行、
    点击落中 IL fixture——与全部实机观察吻合）。"""
    try:
        import ctypes
        from ctypes import wintypes

        advapi32 = ctypes.windll.advapi32
        advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                              ctypes.POINTER(wintypes.HANDLE)]
        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(wintypes.HANDLE(process_handle),
                                         0x0008,   # TOKEN_QUERY
                                         ctypes.byref(token)):
            return None
        try:
            ret_len = wintypes.DWORD(0)
            advapi32.GetTokenInformation(token, 25, None, 0,
                                         ctypes.byref(ret_len))   # TokenIntegrityLevel
            if ret_len.value == 0:
                return None
            buf = ctypes.create_string_buffer(ret_len.value)
            if not advapi32.GetTokenInformation(token, 25, buf, ret_len.value,
                                                ctypes.byref(ret_len)):
                return None
            label = ctypes.cast(buf, ctypes.POINTER(wintypes.SID))
            count_ptr = advapi32.GetSidSubAuthorityCount(ctypes.byref(label))
            count = ctypes.cast(count_ptr, ctypes.POINTER(ctypes.c_ubyte))[0]
            rid_ptr = advapi32.GetSidSubAuthority(ctypes.byref(label), count - 1)
            rid = ctypes.cast(rid_ptr, ctypes.POINTER(ctypes.c_uint32))[0]
            return f"0x{rid:04x}"
        finally:
            ctypes.windll.kernel32.CloseHandle(token)
    except Exception:
        return None


def _process_integrity_level(pid: int) -> str | None:
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFO
        if not handle:
            return None
        try:
            return _token_integrity_level(handle)
        finally:
            kernel32.CloseHandle(wintypes.HANDLE(handle))
    except Exception:
        return None


def win32_input_probe() -> dict[str, Any]:
    """OS 级输入快照（仅 win32；其余平台/失败返回 {}，绝不抛出）。

    F6 线索「Tk winfo_pointerxy 静止」需要与原始 Win32 状态对照才能定性：
    - ``cursor``：``GetCursorPos`` 原始读数（Tk 查询在 Windows 上底层就是
      它——两者不一致 → Tk/查询侧被污染；两者一致且静止 → 指针真被钉死）；
    - ``clip``：``GetClipCursor`` 当前裁剪矩形。若被钉成退化矩形，光标物理
      上无法移动、注入坐标会被裁回原点，同时解释「指针静止」与「点击
      无处可去」；无裁剪时返回全虚拟屏边界；
    - ``foreground``：前台窗口标题（判定注入按键/点击实际去向——例如 Esc
      在遮罩未持有键盘焦点时落到了别的窗口）。
    每秒一次、全部非阻塞；只在 Tk 线程里由面板轮询调用。
    """
    if sys.platform != "win32":
        return {}
    # F12：逐调用隔离 + 错误回传——F8 的整体 try 使任何一个 API 失败就把
    # 全部字段变 None（实机 11.26 判决轮的实际故障模式，判读作废一轮）。
    # 现在单点失败只影响自己，并把异常写进 error 字段自诊断。
    out: dict[str, Any] = {"cursor": None, "clip": None, "foreground": "",
                           "win_thread": None, "desktop": None, "error": ""}
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        user32.GetCursorPos.restype = wintypes.BOOL
        try:
            point = wintypes.POINT()
            out["cursor"] = ([int(point.x), int(point.y)]
                             if user32.GetCursorPos(ctypes.byref(point))
                             else None)
        except Exception as exc:
            out["error"] = f"cursor:{exc!r}"
        try:
            rect = wintypes.RECT()
            out["clip"] = ([int(rect.left), int(rect.top),
                            int(rect.right), int(rect.bottom)]
                           if user32.GetClipCursor(ctypes.byref(rect)) else None)
        except Exception as exc:
            out["error"] = (out["error"] + f" clip:{exc!r}").strip()
        try:
            hwnd = user32.GetForegroundWindow()
            if hwnd:
                buffer = ctypes.create_unicode_buffer(64)
                user32.GetWindowTextW(hwnd, buffer, 64)
                out["foreground"] = buffer.value[:40]
                out["fg_hwnd"] = int(hwnd)
        except Exception as exc:
            out["error"] = (out["error"] + f" fg:{exc!r}").strip()
        try:
            kernel32 = ctypes.windll.kernel32
            out["win_thread"] = int(kernel32.GetCurrentThreadId())
            getter = getattr(user32, "GetThreadDesktop", None)
            out["desktop"] = (int(getter(out["win_thread"]))
                              if callable(getter) else 0)
        except Exception as exc:
            out["error"] = (out["error"] + f" thread:{exc!r}").strip()
        try:
            il = _token_integrity_level(kernel32.GetCurrentProcess())
            out["il"] = il
            fg_hwnd = out.get("fg_hwnd")
            if fg_hwnd:
                pid = wintypes.DWORD(0)
                user32.GetWindowThreadProcessId(wintypes.HWND(fg_hwnd),
                                                ctypes.byref(pid))
                if pid.value:
                    out["fg_pid"] = int(pid.value)
                    out["fg_il"] = _process_integrity_level(int(pid.value))
        except Exception as exc:
            out["error"] = (out["error"] + f" il:{exc!r}").strip()
    except Exception as exc:   # import 级失败
        out["error"] = f"probe:{exc!r}"
    return out


def clip_is_suspicious(clip: Sequence[int] | None,
                       cursor: Sequence[int] | None) -> str | None:
    """判定裁剪矩形是否异常（纯函数，离线可测）。

    - ``degenerate``：宽或高 ≤1px——光标被钉死在一个点上，SetCursorPos
      的目标坐标也会被裁回该点（F6「指针查询静止」的最可能机制）；
    - ``cursor_outside_clip``：报告的裁剪矩形不含原始光标位置——状态自相
      矛盾（查询被污染或竞态），同样标记取证。
    正常无裁剪时 GetClipCursor 返回全虚拟屏边界，光标必在其中 → None。
    """
    if not clip or len(clip) != 4:
        return None
    left, top, right, bottom = (int(v) for v in clip)
    if right - left <= 1 or bottom - top <= 1:
        return "degenerate"
    if cursor and len(cursor) == 2:
        x, y = int(cursor[0]), int(cursor[1])
        if not (left <= x < right and top <= y < bottom):
            return "cursor_outside_clip"
    return None


def _win32_release_clip() -> None:
    """ClipCursor(NULL)：释放整个桌面（同一 desktop 内任意进程可调用）。"""
    if sys.platform != "win32":
        return
    import ctypes

    ctypes.windll.user32.ClipCursor(None)


def region_valve_log_path(environ: Mapping[str, str] | None = None) -> str:
    """诊断日志位置：%TEMP%/edr-wd-region-valve.log（写失败不抛出）。"""
    env = environ if environ is not None else os.environ
    base = env.get("TEMP") or env.get("TMP") or tempfile.gettempdir()
    return os.path.join(base, REGION_VALVE_LOG_NAME)


class RegionInputValve:
    """遮罩「输入死亡」安全阀：三条防线，纯状态机，离线可测。

    背景：11.26 实机上遮罩出现过「窗口存活、hit-test 命中、但任何 Tk 输入
    事件都不派发」的死锁——全屏置顶遮罩一旦吞输入，用户只能杀进程逃生。
    本阀不试图修复死锁，只保证桌面永远能自行解锁：

    1. 输入确认：遮罩上任何输入证据（按下/拖动/悬停/Esc/按钮）刷新
       ``last_input_at``；有确认就绝不自动取消。
    2. 指针判死：Tcl 心跳仍活（after 采样 ``winfo_pointerxy`` 正常）但指针
       连续移动而输入事件零到达 → 判定输入死亡，自动取消遮罩恢复面板。
       （指针没动 = 用户可能只是静置观看，不据此判定。）
    3. 到期兜底：遮罩打开后 ``grace_seconds`` 内从未收到任何输入确认，
       自动取消（用户静置也会退出遮罩，宁可多按一次热键也不锁桌面）；
       Tcl 心跳失联超过 ``stale_seconds`` → 看门狗线程调 ``hard_exit``
       结束面板子进程（桌面立即解锁；host 经管道 EOF 观测到面板退出，
       可用 open_recording_ui 重新打开）。

    所有事件写一行诊断日志（%TEMP%/edr-wd-region-valve.log），供实机
    取证；写失败静默。
    """

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        hard_exit: Callable[[int], Any] = os._exit,
        log_path: str | None = None,
        grace_seconds: float = REGION_INPUT_GRACE_SECONDS,
        move_samples: int = REGION_MOVE_CONFIRM_SAMPLES,
        stale_seconds: float = REGION_HEARTBEAT_STALE_SECONDS,
        release_clip_fn: Callable[[], Any] | None = None,
        release_clip_enabled: bool = True,
        key_state_fn: Callable[[int], bool] | None = None,
    ) -> None:
        self._monotonic = monotonic
        self._hard_exit = hard_exit
        # F43 键盘态轮询：可注入读函数（离线可测）；None → win32 默认。
        self._key_state_fn = key_state_fn
        self._return_state: bool | None = None
        self.return_key_edges = 0
        self._log_path = (log_path if log_path is not None
                          else region_valve_log_path())
        self._grace = float(grace_seconds)
        self._move_samples = max(1, int(move_samples))
        self._stale = float(stale_seconds)
        # 可疑裁剪矩形自动释放（F6 修复候选）：None → win32 默认实现。
        self._release_clip_fn = release_clip_fn
        self._release_clip_enabled = bool(release_clip_enabled)
        self.created_at = 0.0
        self.last_input_at: float | None = None
        self.heartbeat_at = 0.0
        self.last_pointer: tuple[int, int] | None = None
        self.move_streak = 0
        self.closed = False
        self.cancel_reason: str | None = None
        self._modal_depth = 0

    @property
    def modal_active(self) -> bool:
        """原生模态框（showerror 等）打开期间 Tcl after 心跳会停。"""
        return self._modal_depth > 0

    def enter_modal(self) -> None:
        self._modal_depth += 1

    def exit_modal(self) -> None:
        self._modal_depth = max(0, self._modal_depth - 1)

    def on_overlay_open(self) -> None:
        now = self._monotonic()
        self.created_at = now
        self.heartbeat_at = now
        self.last_input_at = None
        self.last_pointer = None
        self.move_streak = 0
        self.closed = False
        self.cancel_reason = None
        self._log("open", f"grace={self._grace:g}s")

    def on_input_event(self, note: str = "") -> None:
        """任何输入证据（真实事件回调里调用）——有确认就永不自动取消。"""
        if self.closed:
            return
        self.last_input_at = self._monotonic()
        self.move_streak = 0
        self._log("input", note)

    def sample_pointer(self, x: int | None, y: int | None,
                       probe: Mapping[str, Any] | None = None) -> dict[str, str]:
        """Tk 线程每秒采样一次；返回 {"action": "cancel"|"none", ...}。

        ``probe`` 是 ``win32_input_probe()`` 的快照（可选）：每次采样连同
        Tk 查询值一起写取证日志；发现可疑裁剪矩形（退化/不含光标）时按
        配置自动 ``ClipCursor(NULL)`` 释放——这是 F6「指针查询静止」的
        头号修复候选：光标被钉死后 Tk 与原始查询都会静止、注入坐标会被
        裁回原点。释放动作本身非阻塞且不抛出。
        """
        if self.closed:
            return {"action": "none", "reason": "closed"}
        suspicious = clip_is_suspicious(
            (probe or {}).get("clip"), (probe or {}).get("cursor"))
        if suspicious and self._release_clip_enabled:
            self._release_clip()
        pm = (probe or {}).get("probeMove")
        self._log("sample", (
            f"tk=({x},{y}) raw={(probe or {}).get('cursor')}"
            f" ptsrc={('move@%sms' % pm['ageMs']) if pm else 'none'}"
            f" clip={(probe or {}).get('clip')}"
            f" fg={(probe or {}).get('foreground')!r}"
            f" wt={(probe or {}).get('win_thread')}"
            f" dt={(probe or {}).get('desktop')}"
            f" tkt={(probe or {}).get('tk_thread')}"
            + (f" ERR={(probe or {}).get('error')}"
               if (probe or {}).get("error") else "")
            + (f" SUSPICIOUS={suspicious}" if suspicious else "")))
        if x is None or y is None:
            return {"action": "none", "reason": "pointer_unavailable"}
        now = self._monotonic()
        moved = False
        if self.last_pointer is not None:
            dx = abs(int(x) - self.last_pointer[0])
            dy = abs(int(y) - self.last_pointer[1])
            moved = (dx > REGION_MOVE_MIN_DISTANCE_PX
                     or dy > REGION_MOVE_MIN_DISTANCE_PX)
        self.last_pointer = (int(x), int(y))
        # F35 证据扩展：左按钮态变化沿也算输入证据（与 FROM_MOTION 同一
        # GetAsyncKeyState 读取点）——真实拖拽按下/抬起即时刷新
        # last_input_at，注入场景证据流即时恢复，解锁检测器死锁。
        edge = self._button_state_edge()
        if edge:
            self.on_input_event("button_down_or_edge")   # F36：沿/按住即证据
        return_edge = self._return_key_edge()
        if return_edge:
            self.on_input_event("return_key_edge")       # F43：Return 即证据
        if not input_death_detect_enabled():
            return {"action": "none", "reason": "death_detect_disabled",
                    "returnEdge": return_edge}
        if not edge:   # F36：证据拍吞掉当拍移动（on_input_event 已重置）
            self.move_streak = self.move_streak + 1 if moved else 0
        if self.move_streak >= self._move_samples:
            if self.last_input_at is None:
                return {**self._cancel("pointer_moved_without_input_events"),
                        "returnEdge": return_edge}
            if now - self.last_input_at >= self._grace:
                return {**self._cancel("pointer_moved_without_recent_input"),
                        "returnEdge": return_edge}
        if self.last_input_at is None and now - self.created_at >= self._grace:
            return {**self._cancel("no_input_within_grace"),
                    "returnEdge": return_edge}
        return {"action": "none", "reason": "", "returnEdge": return_edge}

    def _return_key_edge(self) -> bool:
        """F43：VK_RETURN(0x0D) 相对上一采样的下降沿（按下=输入意图）。
        None 首帧只校准；读失败一律 False（零副作用）。轮询不依赖
        motion 流——补上 FROM_MOTION 的静止点击盲区（键盘侧同款）。"""
        try:
            if self._key_state_fn is not None:
                state = bool(self._key_state_fn(0x0D))
            else:
                import ctypes
                state = bool(
                    ctypes.windll.user32.GetAsyncKeyState(0x0D) & 0x8000)
        except Exception:
            return False
        last = self._return_state
        self._return_state = state
        if last is not None and state and last is False:
            self.return_key_edges += 1   # 沿计数（对账用）
            return True
        return False

    def _button_state_edge(self) -> bool:
        """F36：左按钮活跃输入证据 = 变化沿 OR 按住（state==down）。

        F35 语义缺口实机实锤：拖拽全程按住→仅按下那刻一个沿→之后每拍
        「无沿」→ move_streak 照常累积→中途判死。按住的按钮本身就是
        活跃输入：按住期间每拍 motion 都刷新 last_input_at+重置 streak。
        None 首帧 down（motion 流首帧即按住=拖拽已开始）也算证据；
        非 win32/读取失败一律 False（零副作用）。"""
        try:
            import ctypes
            state = bool(ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000)
        except Exception:
            return False
        last = getattr(self, "_last_btn_state", None)
        self._last_btn_state = state
        if last is None:
            return state                     # 首帧：down=拖拽开始=证据
        return last != state or state        # 沿 OR 按住

    def _release_clip(self) -> None:
        """释放可疑的光标裁剪矩形（ClipCursor(NULL)，win32 默认实现）。"""
        self._log("clip_release_attempt", "suspicious clip rect detected")
        try:
            if self._release_clip_fn is None:
                self._release_clip_fn = _win32_release_clip
            self._release_clip_fn()
        except Exception as exc:
            self._log("clip_release_failed", str(exc))

    def heartbeat(self) -> None:
        """Tk 线程的 after 循环每秒调用；看门狗据此判断 Tcl 是否还活着。"""
        if not self.closed:
            self.heartbeat_at = self._monotonic()

    def hard_exit_due(self) -> bool:
        return not self.closed and \
            (self._monotonic() - self.heartbeat_at) > self._stale

    def request_hard_exit(self) -> None:
        """最后防线：Tcl 心跳失联 → 硬退出面板子进程，立即解锁桌面。"""
        self._log("hard_exit", "tcl heartbeat stale — unlock desktop")
        self.closed = True
        try:
            self._hard_exit(87)
        except Exception:
            pass

    def on_overlay_close(self, note: str = "") -> None:
        if not self.closed:
            self.closed = True
            self._log("close", note)

    def log_diagnostic(self, note: str) -> None:
        """F5 诊断通道：Tk 事件路由取证（bind_all 层 ButtonPress 到达性）。"""
        self._log("diag", note)

    def _cancel(self, reason: str) -> dict[str, str]:
        self.closed = True
        self.cancel_reason = reason
        self._log("auto_cancel", reason)
        return {"action": "cancel", "reason": reason}

    def _log(self, event: str, detail: str = "") -> None:
        try:
            wall = time.strftime("%Y-%m-%dT%H:%M:%S")
            with open(self._log_path, "a", encoding="utf-8") as handle:
                handle.write(
                    f"{wall} | {self._monotonic():.3f} | {event}"
                    + (f" | {detail}" if detail else "") + "\n")
        except Exception:
            pass


# ── F12：面板内 WndProc 只读探针（REGION_WNDPROC_DIAG=1 启用）──────────
# V1–V6 复刻体穷尽后，唯一可复现环境是真实面板。子类化只发生在遮罩
# overlay 的 HWND（内窗 + wrapper 父窗）上；回调仅计数 + 缓存按钮采样
# （绝不做文件 I/O、绝不重入 Tk——它在 Tk 消息泵内部执行），由面板的
# 每秒 after 轮询读取计数写 valve 日志；遮罩关闭恢复原 WndProc。
WNDPROC_DIAG_ENV = "REGION_WNDPROC_DIAG"

_WM_MOUSEMOVE = 0x0200


def client_pt_from_lparam(lparam: int) -> tuple[int, int]:
    """F46：WM_MOUSEMOVE lParam → 客户区 (x, y)（低字 x、高字 y，有符号）。"""
    import ctypes
    x = ctypes.c_short(lparam & 0xFFFF).value
    y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
    return int(x), int(y)
_WM_LBUTTONDOWN = 0x0201
_WM_LBUTTONUP = 0x0202
_WM_NCHITTEST = 0x0084
_WM_MOUSEACTIVATE = 0x0021
_WM_SETFOCUS = 0x0007
_WM_KILLFOCUS = 0x0008
_WM_ACTIVATE = 0x0006
_WM_INPUT = 0x00FF
WNDPROC_WATCHED = {
    _WM_MOUSEMOVE: "WM_MOUSEMOVE", _WM_LBUTTONDOWN: "WM_LBUTTONDOWN",
    _WM_LBUTTONUP: "WM_LBUTTONUP", _WM_NCHITTEST: "WM_NCHITTEST",
    _WM_MOUSEACTIVATE: "WM_MOUSEACTIVATE", _WM_SETFOCUS: "WM_SETFOCUS",
    _WM_KILLFOCUS: "WM_KILLFOCUS", _WM_ACTIVATE: "WM_ACTIVATE",
    _WM_INPUT: "WM_INPUT",
}
WNDPROC_THROTTLE = {"WM_MOUSEMOVE": 50, "WM_NCHITTEST": 20}


UIPI_ALLOW_ENV = "REGION_UIPI_ALLOW"
_UIPI_BUTTON_MESSAGES = (0x0201, 0x0202, 0x0203,   # LBUTTONDOWN/UP/DBLCLK
                         0x0204, 0x0205,           # RBUTTONDOWN/UP
                         0x0207, 0x0208)           # MBUTTONDOWN/UP
MSGFLT_ALLOW = 1


def uipi_allow_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """默认开：遮罩是管理层专用表面，允许低 IL 注入器投递按钮消息。
    REGION_UIPI_ALLOW=0 关闭（对照实验用）。"""
    env = environ if environ is not None else os.environ
    return env.get(UIPI_ALLOW_ENV, "1") != "0"


def apply_uipi_message_allow(hwnd: int) -> list[tuple[str, str]]:
    """ChangeWindowMessageFilterEx(MSGFLT_ALLOW)——高 IL 窗口接收低 IL
    注入按钮消息的标准放行（UIPI 投递层丢弃的直接解）。逐消息结果返回。"""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    results: list[tuple[str, str]] = []
    try:
        user32.ChangeWindowMessageFilterEx.restype = wintypes.BOOL
        user32.ChangeWindowMessageFilterEx.argtypes = [
            wintypes.HWND, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    except Exception:
        pass
    for msg in _UIPI_BUTTON_MESSAGES:
        try:
            ok = user32.ChangeWindowMessageFilterEx(wintypes.HWND(hwnd),
                                                    msg, MSGFLT_ALLOW, None)
            results.append((f"0x{msg:04x}", "ok" if ok else "fail"))
        except Exception as exc:
            results.append((f"0x{msg:04x}", f"err:{exc!r}"))
    return results


def wndproc_diag_enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = environ if environ is not None else os.environ
    return env.get(WNDPROC_DIAG_ENV, "0") == "1"


def _retire_probe_callback(probe: Any) -> None:
    """F64b：失败探针回调进 retired 强引用保活（防 GC 悬空 trampoline）。"""
    try:
        proc_obj = getattr(probe, "_proc", None)
        if proc_obj is not None:
            # F64d（审查实锤）：retired 列表不按数量淘汰——失败回调可能仍
            # 装在活 HWND 上，数量上限释放=原生仍引用的跳板被 GC。失败
            # 且未验证卸载者保活至进程结束（对象数极小）；已验证卸载/
            # 窗口销毁者方可释放（后续可扩展验证式释放）。
            WndProcReadonlyProbe.retired_callbacks.append(proc_obj)
    except Exception:
        pass

class WndProcReadonlyProbe:
    """只读子类化单个 HWND：计数 + 按钮采样缓冲，转发 CallWindowProc。"""

    THROTTLE = WNDPROC_THROTTLE

    def __init__(self, label: str, hwnd: int,
                 forward_widget: Any = None,
                 motion_infer: bool = False) -> None:
        import ctypes
        from ctypes import wintypes

        self.label = label
        self.hwnd = int(hwnd)
        # F29 翻译层补偿：本 hwnd 的按钮消息 → 该 widget 合成事件
        # （仅 canvas 级 widget 配置；wrapper/inner 不配——同一消息只
        # 落在一个 hwnd，多处转发会双触发）。
        self.forward_widget = forward_widget
        # F46 位置真值源：最后一条 WM_MOUSEMOVE 的屏幕坐标（消息坐标
        # =内核路由真值，不经光标查询——任务上下文 GetCursorPos 失真）。
        self.last_move_screen: tuple[int, int] | None = None
        self.last_move_at: float = 0.0
        # F32 结构性替代：按钮态经 motion 流推断（不依赖按钮消息）。
        self.motion_infer = bool(motion_infer)
        self._last_btn_state: bool | None = None
        self.forward_counts: dict[str, int] = {}
        self.counts: dict[str, int] = {}
        self.returns: dict[str, dict[str, int]] = {}
        self.button_samples: deque[str] = deque(maxlen=8)
        self.error = ""
        self._user32 = ctypes.windll.user32
        user32 = self._user32
        if hasattr(user32, "SetWindowLongPtrW"):
            self._set_long = user32.SetWindowLongPtrW
            self._get_long = user32.GetWindowLongPtrW
        else:   # 32 位 Python：SetWindowLongPtrW 是宏，退回 SetWindowLongW
            self._set_long = user32.SetWindowLongW
            self._get_long = user32.GetWindowLongW
            raise ValueError("32 位窗口过程回退未实现，探针放弃安装")
        pointer_size = ctypes.sizeof(ctypes.c_void_p)
        if pointer_size != 8:
            raise ValueError("WndProc 探针仅支持 64 位 Python")
        self._WNDPROC = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
            wintypes.WPARAM, wintypes.LPARAM)
        self._get_long.restype = ctypes.c_ssize_t
        self._get_long.argtypes = [wintypes.HWND, ctypes.c_int]
        self._set_long.restype = ctypes.c_ssize_t
        self._set_long.argtypes = [wintypes.HWND, ctypes.c_int,
                                   ctypes.c_ssize_t]
        user32.CallWindowProcW.restype = ctypes.c_ssize_t
        user32.CallWindowProcW.argtypes = [ctypes.c_ssize_t, wintypes.HWND,
                                           wintypes.UINT, wintypes.WPARAM,
                                           wintypes.LPARAM]
        self._original = self._get_long(self.hwnd, -4)   # GWLP_WNDPROC
        self._proc = self._WNDPROC(self._dispatch)   # 保引用防 GC
        try:
            _set_result = self._set_long(self.hwnd, -4, ctypes.cast(
                self._proc, ctypes.c_void_p).value)
        except Exception:
            self.installed = False
            raise
        if not _set_result:
            # 安装失败：先 retire 保活（防任何在途调用悬空），错误读取兜底。
            _retire_probe_callback(self)
            self.installed = False
            try:
                err = ctypes.get_last_error()
                err_s = f"{err} (use_last_error=False, 可能非本次错误)"
            except Exception as _e:
                err_s = f"unreadable:{type(_e).__name__}"
            raise RuntimeError(
                f"wndproc-attach-failed hwnd={self.hwnd} ret=0 "
                f"lastError={err_s}")
        self.installed = True

    def _dispatch(self, hwnd, msg, wparam, lparam):
        result = self._user32.CallWindowProcW(self._original, hwnd, msg,
                                              wparam, lparam)
        try:
            self._forward_button(hwnd, msg, lparam)
            if msg == _WM_MOUSEMOVE:
                self._infer_button_from_motion(lparam)
                self._record_last_move(hwnd, lparam)   # F46：位置真值源
            name = WNDPROC_WATCHED.get(msg)
            if name:
                self.counts[name] = self.counts.get(name, 0) + 1
                if msg in (_WM_LBUTTONDOWN, _WM_LBUTTONUP):
                    x = ctypes.c_short(lparam & 0xFFFF).value
                    y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                    self.button_samples.append(
                        f"{name} #{self.counts[name]} pt=({x},{y})"
                        f" wp={wparam:#x}")
                elif msg in (_WM_NCHITTEST, _WM_MOUSEACTIVATE):
                    # F13：返回值直方图——NCHITTEST 返 HTTRANSPARENT(-1)/
                    # HTNOWHERE(-2) = 点击被系统穿透下层（fixture）；返
                    # HTCLIENT(1) 而按钮仍不到 = 投递层丢弃（UIPI 头号）。
                    hist = self.returns.setdefault(name, {})
                    key = str(int(result))
                    hist[key] = hist.get(key, 0) + 1
                elif msg == _WM_INPUT:
                    self._record_rawinput(lparam)
                else:
                    throttle = self.THROTTLE.get(name, 1)
                    if self.counts[name] % throttle == 0:
                        self.button_samples.append(
                            f"{name} #{self.counts[name]}")
        except Exception:
            pass   # 探针绝不干扰消息流
        return result

    def _record_last_move(self, hwnd: int, lparam: int) -> None:
        """F46：WM_MOUSEMOVE 消息坐标 = 内核路由真值（不经光标查询——
        任务上下文 GetCursorPos/winfo_pointerxy 全部失真的位置源替代）。
        ClientToScreen 化为屏幕坐标；失败置 None（绝不干扰消息流）。
        F47：函数内显式 import ctypes（F46 版本依赖 __init__ 局部导入
        → NameError 被吞 → last_move 恒 None——静默失败根因实锤）。"""
        import ctypes
        from ctypes import wintypes
        import time as _time
        try:
            cx, cy = client_pt_from_lparam(lparam)
            point = wintypes.POINT(cx, cy)
            ok = self._user32.ClientToScreen(hwnd, ctypes.byref(point))
            if ok:
                self.last_move_screen = (int(point.x), int(point.y))
                self.last_move_at = _time.monotonic()
            else:
                self.last_move_screen = None
        except Exception:
            self.last_move_screen = None

    def _forward_button(self, hwnd: int, msg: int, lparam: int) -> None:
        """F29：按钮消息 → canvas 合成事件（翻译层补偿主体）。

        原消息已先照常传 Tk（CallWindowProc 在 _dispatch 头部）；此处
        以 after_idle 延迟到 Tk 回调上下文生成合成事件——event_generate
        直入 Tk 事件队列绕过 WndProc→XEvent 翻译（F27 判决：合成必触发）。
        坐标=lParam 客户区坐标（与 event_generate 语义一致）。计数入
        summary 供与 bindall/canvas_press 取证对账。"""
        widget = self.forward_widget
        if widget is None or int(hwnd) != self.hwnd:
            return
        sequence = button_event_sequence(msg)
        if sequence is None:
            return
        x, y = lparam_to_client_coords(lparam)
        key = "down" if "Release" not in sequence else "up"
        self.forward_counts[key] = self.forward_counts.get(key, 0) + 1

        def generate() -> None:
            try:
                widget.event_generate(sequence, x=x, y=y)
            except Exception:
                pass   # 转发失败绝不干扰消息流

        try:
            widget.after_idle(generate)
        except Exception:
            pass   # 无 after（非常规 widget）→ 放弃本次转发

    def _infer_button_from_motion(self, lparam: int) -> None:
        """F32：从 motion 流推断按钮态变化沿（结构性替代主体）。

        每条 WM_MOUSEMOVE 读 GetAsyncKeyState(VK_LBUTTON)（注入按钮
        F27 实证可见：everDown min=-32768）；与上次态比较，变化沿合成
        press/release（after_idle → event_generate，x,y=lParam 客户区）。
        F36 首帧语义：up 首帧静默校准；down 首帧生成 press（校准吃掉
        拖拽唯一沿的实机教训）。读取在 Tk 主线程（WndProc 同线程）安全；
        失败静默。"""
        widget = self.forward_widget
        if widget is None or not self.motion_infer:
            return
        try:
            import ctypes
            state = bool(ctypes.windll.user32.GetAsyncKeyState(0x01) & 0x8000)
        except Exception:
            return
        if self._last_btn_state is None:
            self._last_btn_state = state
            if not state:
                return   # up 首帧=常态校准（静默）
            # down 首帧=motion 流首帧即按住=拖拽已开始——F36 实机实锤：
            # 原校准吃掉唯一沿致 from_motion=0；现生成 press。
            sequence, key = "<Button-1>", "down"
        elif state == self._last_btn_state:
            return   # 按住保持：valve 侧已算证据，不重复生成
        else:
            self._last_btn_state = state
            sequence = "<Button-1>" if state else "<ButtonRelease-1>"
            key = "down" if state else "up"
        self.forward_counts[key] = self.forward_counts.get(key, 0) + 1
        x, y = lparam_to_client_coords(lparam)

        def generate() -> None:
            try:
                widget.event_generate(sequence, x=x, y=y)
            except Exception:
                pass   # 转发失败绝不干扰消息流

        try:
            widget.after_idle(generate)
        except Exception:
            pass

    def _record_rawinput(self, lparam: int) -> None:
        """WM_INPUT 裸输入观测（F14）：raw 层是否存在注入 DOWN——与 LL
        钩子对照定位过滤层（钩子链上 vs raw 之下/内核）。零 I/O。"""
        try:
            import ctypes
            from ctypes import wintypes

            if not getattr(self, "_raw_ready", False):
                class RAWINPUTHEADER(ctypes.Structure):
                    _fields_ = [("dwType", wintypes.DWORD),
                                ("dwSize", wintypes.DWORD),
                                ("hDevice", wintypes.HANDLE),
                                ("wParam", wintypes.WPARAM)]

                class _BTN(ctypes.Union):
                    _fields_ = [("usButtonFlags", wintypes.USHORT),
                                ("usButtonData", wintypes.USHORT)]

                class _MU(ctypes.Union):
                    _fields_ = [("ulButtons", wintypes.ULONG),
                                ("btn", _BTN)]

                class RAWMOUSE(ctypes.Structure):
                    _anonymous_ = ("u",)
                    _fields_ = [("usFlags", wintypes.USHORT), ("u", _MU),
                                ("ulRawButtons", wintypes.ULONG),
                                ("lLastX", wintypes.LONG),
                                ("lLastY", wintypes.LONG),
                                ("ulExtraInformation", wintypes.ULONG)]

                class _DATA(ctypes.Union):
                    _fields_ = [("mouse", RAWMOUSE),
                                ("pad", ctypes.c_ubyte * 40)]

                class RAWINPUT(ctypes.Structure):
                    _anonymous_ = ("data",)
                    _fields_ = [("header", RAWINPUTHEADER),
                                ("data", _DATA)]

                self._RAWINPUT = RAWINPUT
                self._user32.GetRawInputData.restype = wintypes.UINT
                self._user32.GetRawInputData.argtypes = [
                    wintypes.HANDLE, wintypes.UINT, ctypes.c_void_p,
                    ctypes.POINTER(wintypes.UINT), wintypes.UINT]
                self._raw_ready = True
            size = wintypes.UINT(0)
            RID_INPUT = 0x10000003
            if self._user32.GetRawInputData(
                    wintypes.HANDLE(lparam), RID_INPUT, None,
                    ctypes.byref(size),
                    ctypes.sizeof(self._RAWINPUT.header)) == 0xFFFFFFFF:
                return
            buf = ctypes.create_string_buffer(max(int(size.value), 64))
            got = self._user32.GetRawInputData(
                wintypes.HANDLE(lparam), RID_INPUT, buf, ctypes.byref(size),
                ctypes.sizeof(self._RAWINPUT.header))
            if got == 0xFFFFFFFF:
                return
            ri = ctypes.cast(buf, ctypes.POINTER(self._RAWINPUT)).contents
            if ri.header.dwType != 0:   # 0 = RIM_TYPEMOUSE
                return
            btn = ri.mouse.usButtonFlags
            if btn & 0x0001:   # RI_MOUSE_LEFT_BUTTON_DOWN
                self.counts["RAW_LDOWN"] = self.counts.get("RAW_LDOWN", 0) + 1
                self.button_samples.append(
                    f"RAW_LDOWN #{self.counts['RAW_LDOWN']}"
                    f" extra={ri.mouse.ulExtraInformation:#x}")
            if btn & 0x0002:   # RI_MOUSE_LEFT_BUTTON_UP
                self.counts["RAW_LUP"] = self.counts.get("RAW_LUP", 0) + 1
        except Exception:
            self.counts["RAW_ERR"] = self.counts.get("RAW_ERR", 0) + 1

    def restore(self) -> None:
        """F64b（审查实锤）：恢复必须核验返回值/Win32 错误——_set_long 返 0
        =失败（GetLastError 取因）。失败时回调仍装在活 HWND 上：进 retired
        强引用保活（OS 只存函数指针不持 Python callback，清引用=GC 回收
        trampoline=原生调进已释放内存=fast-fail 嫌疑路径）。"""
        import ctypes as _ct
        try:
            result = self._set_long(self.hwnd, -4, self._original)
        except Exception as exc:
            # F64c（审查实锤）：任何失败先 retire 保活，再安全取错误——
            # 不得让 NameError/取错异常跳过保活（回调被 GC=悬空 trampoline）。
            _retire_probe_callback(self)
            raise RuntimeError(
                f"wndproc-restore-exception hwnd={self.hwnd} "
                f"{type(exc).__name__}: {exc}") from exc
        if not result:
            _retire_probe_callback(self)   # 保活先于一切错误读取
            try:
                # windll 默认 use_last_error=False：GetLastError 可能陈旧；
                # 语义如实标注（lastError?=unreadable），不虚构错误值。
                err = _ct.get_last_error()
                err_s = f"{err} (use_last_error=False, 可能非本次错误)"
            except Exception as _e:
                err_s = f"unreadable:{type(_e).__name__}"
            raise RuntimeError(
                f"wndproc-restore-failed hwnd={self.hwnd} ret=0 "
                f"lastError={err_s}")

    retired_callbacks: list = []

    @classmethod
    def _retire(cls, proc_obj: Any) -> None:
        """失败探针回调保活至进程结束（对象数有限；已验证卸载后可释放）。"""
        if proc_obj is not None and getattr(proc_obj, '_proc', None) is not None:
            cls.retired_callbacks.append(proc_obj._proc)
            del cls.retired_callbacks[:-256]

    @classmethod
    def attach(cls, overlay: Any, extra_widgets: Any = None,
               forward: bool = False,
               motion_infer: bool = False) -> list["WndProcReadonlyProbe"]:
        """子类化 overlay 内窗 + wrapper 父窗（Toplevel 的真实 HWND）。

        ``forward=True``（F29 翻译层补偿门）时 extra_widgets 的探针携带
        转发目标——按钮消息经该探针转发为 widget 合成事件。"""
        if sys.platform != "win32" or not wndproc_diag_enabled():
            return []
        import ctypes

        probes: list["WndProcReadonlyProbe"] = []
        inner = int(overlay.winfo_id())
        try:
            wrapper = int(ctypes.windll.user32.GetParent(inner))
        except Exception:
            wrapper = 0
        targets: list[tuple[str, int, Any]] = [("inner", inner, None),
                                               ("wrapper", wrapper, None)]
        for idx, widget in enumerate(extra_widgets or ()):
            try:
                targets.append((f"w{idx}", int(widget.winfo_id()),
                                widget if (forward or motion_infer) else None))
            except Exception:
                continue
        for label, hwnd, forward_widget in targets:
            if not hwnd:
                continue
            try:
                probes.append(cls(label, hwnd, forward_widget=forward_widget,
                                  motion_infer=motion_infer))
            except Exception:
                continue
        return probes

    def summary(self) -> str:
        import json as _json
        returns = {name: dict(hist) for name, hist in self.returns.items()}
        forward = (f" forward={_json.dumps(self.forward_counts)}"
                   if self.forward_widget else "")
        return (f"{self.label} hwnd={self.hwnd}"
                f" counts={_json.dumps(self.counts)}"
                f" returns={_json.dumps(returns)}"
                f" samples={list(self.button_samples)}{forward}"
                + (f" err={self.error}" if self.error else ""))


INPUT_DIAG_ENV = "REGION_INPUT_DIAG"
WH_MOUSE_LL = 14
LLMHF_INJECTED = 0x1
LLMHF_LOWER_IL_INJECTED = 0x2
_WM_INPUT = 0x00FF
RID_INPUT = 0x10000003
RIDEV_INPUTSINK = 0x00000100


def overlay_geometry_desynced(width: int, height: int,
                              expected_w: int, expected_h: int) -> bool:
    """F18 根因修复的判定核心（纯函数，离线可测）。

    实机铁证：valve 原生 SetWindowPos 全屏后，Win32 窗口是全屏而
    **Tk geometry 模型停留 1x1**（bindall poll: overlay=1x1+0+0）→
    Tk 指针跟踪/containing/绑定派发全灭——一切按钮消息到达 WndProc
    后被 Tk 静默丢弃（根因=几何状态坏死，非任何过滤层）。
    判定：Tk 模型尺寸与期望全屏尺寸任一不等即失步（差 ≥1px 也算——
    1x1 vs 1680x988 是天壤之别，不允许"近似相等"）。"""
    try:
        return (int(width) != int(expected_w)
                or int(height) != int(expected_h))
    except Exception:
        return True   # 读不到模型尺寸（None/异常）→ 按失步处理


def input_diag_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """F14 双层输入观测器门（LL 钩子 + WM_INPUT 裸输入汇）。默认关。"""
    env = environ if environ is not None else os.environ
    return env.get(INPUT_DIAG_ENV, "0") == "1"


BINDALL_DIAG_ENV = "REGION_BINDALL_DIAG"
DPI_AWARE_ENV = "REGION_DPI_AWARE"
VALVE_GATE_ENV = "REGION_VALVE_GATE"
OVERLAY_SYNTH_ENV = "REGION_OVERLAY_SYNTH"
BTN_FORWARD_ENV = "REGION_BTN_FORWARD"
BTN_FROM_MOTION_ENV = "REGION_BTN_FROM_MOTION"
INPUT_DEATH_DETECT_ENV = "REGION_INPUT_DEATH_DETECT"
KEY_VK_RETURN = 0x0D   # F43：键盘态轮询目标键
BTN_FROM_LL_ENV = "REGION_BTN_FROM_LL"


def btn_from_ll_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """F39 LL 钩子转发门（默认 0）：WH_MOUSE_LL 在根因（Tk WndProc→XEvent
    翻译）**之下**——DOWN/UP 事件必达（F14 实证钩子层可见注入按钮）。
    与 FROM_MOTION 互补：FROM_MOTION 依赖 motion 流采样，静止点击
    （无移动的按下/抬起）零 motion 帧→钩子永不触发（F38 confirm-clicked
    零行的实机根因）。LL 通道覆盖静止点击+全部控件（屏幕坐标→
    winfo_containing 分发）。"""
    env = environ if environ is not None else os.environ
    return env.get(BTN_FROM_LL_ENV, "0") == "1"


def input_death_detect_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """F35 指针判死开关（默认 1=保持现行为）。=0 仅测试用：跳过
    move_streak 判死（motion 仍记录、grace 兜底保留）。注入场景下
    输入证据=被根因吞掉的 Tk 绑定事件 → 判死必触发（次生死锁）——
    解锁 FROM_MOTION 证据流验证窗口。"""
    env = environ if environ is not None else os.environ
    return env.get(INPUT_DEATH_DETECT_ENV, "1") == "1"


def btn_from_motion_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """F32 结构性替代门（默认 0）：不依赖按钮消息——探针在 WM_MOUSEMOVE
    时读 GetAsyncKeyState(VK_LBUTTON)，与上次态比较，变化沿即合成
    press/release（after_idle → event_generate）。依据：运动路径被证明
    健康（翻译正常、探针必达），按钮消息路径不明原因消失（运行时消融
    到底：真实 SendInput 按钮不达 canvas WndProc 而 motion=61 达、
    PostMessage err=0 仍不达）——按钮态嵌入 motion 流绕过整段按钮路径。
    首帧只校准不生成（None 哨兵）。"""
    env = environ if environ is not None else os.environ
    return env.get(BTN_FROM_MOTION_ENV, "0") == "1"


def btn_forward_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """F29 翻译层补偿门（默认 0）。=1 时 WndProc 探针把按钮消息转发为
    canvas 合成事件（event_generate）——判决链已闭环：按钮消息达
    WndProc、绑定层健康（合成 press 必触发）、唯独 WndProc→Tk XEvent
    翻译对按钮静默丢弃（运动正常翻译、输入源无关、四台外部机器无罪）。
    原消息照常传 Tk 保持系统状态一致；若 Tk 翻译日后恢复，须即关本门
    （否则双触发）。"""
    env = environ if environ is not None else os.environ
    return env.get(BTN_FORWARD_ENV, "0") == "1"


def lparam_to_client_coords(lparam: int) -> tuple[int, int]:
    """WM_LBUTTON* lParam → 客户区坐标（16 位有符号，拖拽出界可为负）。"""
    import ctypes
    x = ctypes.c_short(int(lparam) & 0xFFFF).value
    y = ctypes.c_short((int(lparam) >> 16) & 0xFFFF).value
    return (int(x), int(y))


_BUTTON_EVENT_MAP = {
    0x0201: "<Button-1>", 0x0202: "<ButtonRelease-1>",       # WM_LBUTTON*
    0x0204: "<Button-3>", 0x0205: "<ButtonRelease-3>",       # WM_RBUTTON*
    0x0207: "<Button-2>", 0x0208: "<ButtonRelease-2>",       # WM_MBUTTON*
}


def button_event_sequence(msg: int) -> str | None:
    """按钮消息 → Tk 事件序列（非按钮消息 None）。"""
    return _BUTTON_EVENT_MAP.get(int(msg))


def overlay_synth_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """F27 判别性自测钩子门（默认 0）。=1 时遮罩打开 2 秒后合成
    Button-1/Motion 事件（event_generate 绕过 OS 消息翻译）+ 自含探针
    记录 seen/missed（synth_* 行走 valve 日志）。
    判决语义：合成 press 触发绑定 → 绑定/tag 层无罪，损失在 OS→Tk
    消息翻译段（隐式 grab/按钮态掩码/pointerWindow 建立时机）；合成
    press 不触发 → 绑定注册本身在面板上下文损坏（与 motion 同 tag 同
    通道却分叉 → bindtags 或事件注册时序）。两向都有明确修复路径。"""
    env = environ if environ is not None else os.environ
    return env.get(OVERLAY_SYNTH_ENV, "0") == "1"
REGION_PANEL_MUTEX_NAME = "Local\\edr-wd-region-panel-instance"
HOTKEY_ERR_ALREADY_REGISTERED = 1409
_input_grace_env = "REGION_INPUT_GRACE"
_panel_mutex_handle = None


def region_input_grace_seconds(environ: Mapping[str, str] | None = None) -> float:
    """遮罩宽限秒数（env REGION_INPUT_GRACE 可覆盖，默认常量 60）。

    F23 对照轮教训：60s 宽限 auto_cancel 吃掉慢速注入——测试机用
    REGION_INPUT_GRACE 延长；产品语义（安全解锁兜底）不变。"""
    env = environ if environ is not None else os.environ
    raw = env.get(_input_grace_env, "")
    try:
        value = float(raw)
        return value if value > 0 else float(REGION_INPUT_GRACE_SECONDS)
    except (TypeError, ValueError):
        return float(REGION_INPUT_GRACE_SECONDS)


MUTEX_WAIT_ACQUIRED = 0          # WAIT_OBJECT_0：互斥空闲，本实例成为持有者
MUTEX_WAIT_ABANDONED = 0x80      # 原持有者死亡未释放 → 所有权移交本实例
MUTEX_WAIT_TIMEOUT = 0x102       # 他人持有 → 活面板/僵尸在跑


def evaluate_mutex_wait(wait_code: int) -> bool:
    """F24 回归修复核心（纯函数）：互斥所有权判定。

    旧实现的两个缺陷（启动回归根因）：① GetLastError 经 windll 读取
    不可靠（必须 use_last_error）；② ERROR_ALREADY_EXISTS=183 只表示
    「命名对象已存在」而非「他人持有」——QA fixture 的 mxtest 探针调过
    acquire 后句柄留在探针进程（模块全局持有）→ 探针进程存活期间对象
    持续存在 → 每个面板子进程 CreateMutexW 都见 183 → 误判
    already_running 自杀。修复：以 WaitForSingleObject(0) 的所有权语义
    为准——空闲(0)/被遗弃(0x80) 都是我们赢；超时(0x102) 才是真占用。"""
    code = int(wait_code)
    if code == MUTEX_WAIT_TIMEOUT:
        return False
    return True   # ACQUIRED / ABANDONED / 其他（WAIT_FAILED 等不拒开）


def acquire_panel_instance_mutex() -> tuple[bool, str]:
    """F6③ 单实例互斥：僵尸面板（open_recording_ui 竞态产物，无窗口）
    占住 Ctrl+Shift+Q → 后到可见面板注册失败红字。新实例拿不到互斥即
    自行退出并上报，绝不再注册热键与活面板冲突。句柄进程级持有。"""
    global _panel_mutex_handle
    if sys.platform != "win32":
        return (True, "")
    try:
        import ctypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.CreateMutexW(None, False, REGION_PANEL_MUTEX_NAME)
        if not handle:
            return (True, "")   # 创建失败不拒开（宁可双开不可拒开）
        _panel_mutex_handle = handle   # 进程级持有：退出即释放
        # 所有权判定（而非对象存在性）：空闲/被遗弃都算本实例持有。
        wait = kernel32.WaitForSingleObject(handle, MUTEX_WAIT_ACQUIRED)
        if evaluate_mutex_wait(wait):
            return (True, "")
        return (False, REGION_PANEL_MUTEX_NAME)
    except Exception:
        return (True, "")


def hotkey_conflict_note(error_code: int) -> str:
    """RegisterHotKey 失败码 → 面向用户的处置提示（1409=热键已注册）。"""
    if int(error_code) == HOTKEY_ERR_ALREADY_REGISTERED:
        return ("热键已被其他录制面板实例占用（可能存在残留面板进程）；"
                "请关闭旧面板或重启服务后重试。")
    return ""


def valve_gate_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """F23 消融门（默认 1=现行为）。=0 时旁路按钮消息路径上的全部邻近
    机器（WndProc subclass 探针 / UIPI MSGFLT_ALLOW / valve 的 ClipCursor
    主动释放），保留 watchdog 线程与日志（纯被动）。
    源码核查结论：RegionInputValve 是纯被动观察者——无按钮吞噬路径
    （无钩子/无 bind 消费/无 SetCapture）；motion 触发而 press 不触发的
    不对称性反证「统一输入门」假设（同一 bind_all tag、同一派发路径，
    门控若存在应同时吞运动）。本门是排除法消融：门 0 后 press 转活 →
    被旁路的机器之一是元凶（继续二分）；press 仍死 → 全部机器清白，
    元凶在遮罩核心构造（绑定/几何/Tk 按钮翻译本身）。"""
    env = environ if environ is not None else os.environ
    return env.get(VALVE_GATE_ENV, "1") == "1"


def bindall_diag_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """F17：bind_all 取证通道门（Tk 事件翻译层判决）。默认关。"""
    env = environ if environ is not None else os.environ
    return env.get(BINDALL_DIAG_ENV, "0") == "1"


def dpi_aware_enabled(environ: Mapping[str, str] | None = None) -> bool:
    """F16③ DPI 感知声明门。默认关：F17 实测遮罩 rect=1696x1008 而
    GetClipCursor=1680x988（16x20 失配）疑为 DPI 声明后的 Tk 几何残留
    ——回归默认（与 F16 之前行为一致）隔离变量，A/B 由 env 开。"""
    env = environ if environ is not None else os.environ
    return env.get(DPI_AWARE_ENV, "0") == "1"


def ll_hook_record(flags: int, extra_info: int, x: int, y: int,
                   msg_name: str) -> str:
    """LL 钩子记录行（纯函数，离线可测）：injected 标志是判决核心。"""
    marks = []
    if flags & LLMHF_INJECTED:
        marks.append("INJ")
    if flags & LLMHF_LOWER_IL_INJECTED:
        marks.append("LOWER_IL")
    tag = "+".join(marks) if marks else "PHYS"
    return (f"{msg_name} pt=({x},{y}) flags={flags:#x} {tag}"
            f" extra={extra_info:#x}")


class MouseHookDiagProbe:
    """WH_MOUSE_LL 只读诊断钩子：回调仅入有界缓冲（零 I/O），由面板每秒
    轮询读取。LL 钩子按安装时间倒序调用（新装先调）——判决矩阵：
    - 钩子看到注入 DOWN + WM_INPUT 也见 + 画布 WndProc 不见
      → 用户态钩子链中更晚安装者吞（EDR 服务）；
    - 钩子不见注入 DOWN 但 WM_INPUT 见
      → raw 输入与钩子链之间被吞（win32k 级策略）；
    - 两者都不见（移动/UP 可见）
      → 内核过滤驱动在 raw 之下吞 DOWN（EDR 内核组件；用户态放行无效）。"""

    def __init__(self, forward_sink: Any = None) -> None:
        import ctypes
        from ctypes import wintypes

        self.records: deque[str] = deque(maxlen=24)
        self.counts: dict[str, int] = {}
        self.forward_sink = forward_sink   # F39：DOWN/UP → 面板合成通道
        self.forward_counts: dict[str, int] = {}
        self.error = ""
        self.installed = False
        self._user32 = ctypes.windll.user32
        user32 = self._user32

        class MSLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [("pt", wintypes.POINT),
                        ("mouseData", wintypes.DWORD),
                        ("flags", wintypes.DWORD),
                        ("time", wintypes.DWORD),
                        ("dwExtraInfo", ctypes.c_size_t)]

        self._MSLL = MSLLHOOKSTRUCT
        self._HOOKPROC = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
        user32.SetWindowsHookExW.restype = wintypes.HANDLE
        user32.SetWindowsHookExW.argtypes = [ctypes.c_int, self._HOOKPROC,
                                             wintypes.HINSTANCE,
                                             wintypes.DWORD]
        user32.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t
        user32.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                          wintypes.WPARAM, wintypes.LPARAM]
        self._proc = self._HOOKPROC(self._on_mouse)
        self._handle = user32.SetWindowsHookExW(
            WH_MOUSE_LL, self._proc, None, 0)
        self.installed = bool(self._handle)
        if not self.installed:
            self.error = f"SetWindowsHookExW err={ctypes.GetLastError()}"

    def _on_mouse(self, n_code, wparam, lparam):
        try:
            if n_code >= 0:
                info = ctypes.cast(lparam, ctypes.POINTER(self._MSLL)).contents
                name = {0x0201: "DOWN", 0x0202: "UP", 0x0200: "MOVE",
                        0x0204: "RDOWN", 0x0205: "RUP"}.get(wparam, "?")
                self.counts[name] = self.counts.get(name, 0) + 1
                if name in ("DOWN", "UP"):
                    self.records.append(ll_hook_record(
                        info.flags, int(info.dwExtraInfo),
                        int(info.pt.x), int(info.pt.y), name))
                    self._ll_forward_event(name, int(info.pt.x),
                                           int(info.pt.y))
        except Exception:
            pass
        return self._user32.CallNextHookEx(self._handle, n_code, wparam,
                                           lparam)

    def _ll_forward_event(self, name: str, x: int, y: int) -> None:
        """F39 转发核心（纯方法，离线可测）：DOWN/UP → sink（屏幕坐标）。"""
        if self.forward_sink is None or name not in ("DOWN", "UP"):
            return
        key = "down" if name == "DOWN" else "up"
        self.forward_counts[key] = self.forward_counts.get(key, 0) + 1
        try:
            self.forward_sink(key, x, y)
        except Exception:
            pass   # 转发失败绝不干扰钩子链

    def uninstall(self) -> None:
        try:
            if self._handle:
                self._user32.UnhookWindowsHookEx(self._handle)
        except Exception:
            pass
        self._handle = None

    def summary(self) -> str:
        import json as _json
        forward = (f" forward={_json.dumps(self.forward_counts)}"
                   if self.forward_sink else "")
        return (f"llhook counts={_json.dumps(self.counts)}"
                f" records={list(self.records)}{forward}"
                + (f" err={self.error}" if self.error else ""))


def register_rawinput_sink(hwnd: int) -> str:
    """注册 WM_INPUT 裸输入汇（RIDEV_INPUTSINK，无需焦点）；返回状态串。"""
    if sys.platform != "win32":
        return "non-win32"
    try:
        import ctypes
        from ctypes import wintypes

        class RAWINPUTDEVICE(ctypes.Structure):
            _fields_ = [("usUsagePage", wintypes.USHORT),
                        ("usUsage", wintypes.USHORT),
                        ("dwFlags", wintypes.DWORD),
                        ("hwndTarget", wintypes.HWND)]

        user32 = ctypes.windll.user32
        user32.RegisterRawInputDevices.restype = wintypes.BOOL
        user32.RegisterRawInputDevices.argtypes = [
            ctypes.POINTER(RAWINPUTDEVICE), wintypes.UINT, wintypes.UINT]
        device = RAWINPUTDEVICE(1, 2, RIDEV_INPUTSINK, wintypes.HWND(hwnd))
        ok = user32.RegisterRawInputDevices(ctypes.byref(device), 1,
                                            ctypes.sizeof(RAWINPUTDEVICE))
        return "ok" if ok else f"fail err={ctypes.GetLastError()}"
    except Exception as exc:
        return f"err:{exc!r}"
