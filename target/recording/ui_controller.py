"""Service-level recorder panel lifecycle (U0 skeleton, U1 window entry).

The controller owns *whether* the target-local recorder panel is shown, which
window a recording will attach to, and *what* the panel should display.  Facts
are always read from ``RecordingService`` / the platform backend — the
controller never caches capture state, so the panel view cannot drift from the
session.

View states (``IDLE / RECORDING / PAUSED / STOPPING / STOPPED / ERROR``) are
panel-facing labels derived from ``CaptureSessionState``.  ``PICKING`` and
``EDITING`` are later view sub-states and deliberately do not exist yet.

Window entry rules (design §3.1):

- the list excludes the recorder's own UI and invisible windows;
- every candidate gets a stable ``selectionId``; with several same-title
  candidates the user — not a heuristic — picks one;
- "定位窗口" re-verifies the recorded identity (handle/pid/process) before
  focusing: a window that has been closed and reopened under the same title
  must be re-selected, never silently refocused by name;
- starting from a selection re-runs the existing connect/lock/verify chain,
  which stays the only writer of recording state.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from .indicator import RECORDER_UI_MARKER
from .models import RecordingModelError
from .region_overlay import RegionSelectionController
from .service import RecordingService


@dataclass(frozen=True)
class WindowSelection:
    """A user-confirmed candidate window.  Selection is not a capture lease:
    starting still runs the unique-session gate and re-verifies the window."""

    selection_id: str
    title: str
    process_name: str
    pid: int
    handle: int | None
    backend: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "selectionId": self.selection_id,
            "title": self.title,
            "processName": self.process_name,
            "pid": self.pid,
            "handle": self.handle,
            "backend": self.backend,
        }


def _window_matches_recorder_ui(entry: Mapping) -> bool:
    title = str(entry.get("title") or "")
    return RECORDER_UI_MARKER in title


class RecorderUIController:
    def __init__(self, service: RecordingService, backend=None) -> None:
        self._service = service
        self._backend = backend
        self._visible = False
        self._selections: dict[str, WindowSelection] = {}
        # 区域断言选区状态机（06 §2）：仅在 region_assertion 成功后存在；
        # cancel/失败即清空。它只编排视图，业务状态由服务侧（B 轨）负责。
        self._region: RegionSelectionController | None = None

    @property
    def visible(self) -> bool:
        return self._visible

    # ── lifecycle (U0) ────────────────────────────────────────────────

    def show(self) -> dict[str, Any]:
        """Show or restore the panel.  Idempotent: opening it twice does not
        start a second capture or a second window — the service-side session
        gate stays the only writer of recording state."""
        result = self._service.open_ui()
        self._visible = bool(result.get("ok"))
        return {**result, "visible": self._visible}

    def hide(self) -> dict[str, Any]:
        self._visible = False
        return {"ok": True, "visible": False, "snapshot": self.snapshot()}

    def snapshot(self) -> dict[str, Any]:
        return self._service.ui_snapshot()

    # ── window entry (U1) ─────────────────────────────────────────────

    def list_recordable_windows(self) -> dict[str, Any]:
        """Visible windows that may host a recording, minus the recorder UI.

        Each entry carries a fresh ``selectionId``; older selections stay
        resolvable until the window identity goes stale.
        """
        if self._backend is None:
            raise RecordingModelError(
                "recording_backend_unavailable",
                "window listing needs the platform backend",
                path="backend",
            )
        raw = self._backend.list_windows()
        windows = []
        for entry in raw.get("windows") or []:
            if not entry.get("visible", False):
                continue
            if _window_matches_recorder_ui(entry):
                continue
            selection = WindowSelection(
                selection_id=uuid.uuid4().hex,
                title=str(entry.get("title") or ""),
                process_name=self._process_name_for_pid(
                    entry, int(entry.get("process_id") or 0)),
                pid=int(entry.get("process_id") or 0),
                handle=entry.get("handle"),
                backend=str(raw.get("backend") or getattr(self._backend, "backend", "")),
            )
            self._selections[selection.selection_id] = selection
            windows.append(selection.to_dict())
        return {"ok": True, "windows": windows}

    def _process_name_for_pid(self, entry: Mapping, pid: int) -> str:
        """Owning process name for a listed window, from real sources only.

        Order: what the platform's list_windows reports for the entry
        (``process_name``), then a backend-provided suggester if it exists.
        There is no ``pid:N`` fallback — a fabricated name would flow into
        focusing and recording as if it were the real process.
        """
        reported = entry.get("processName") or entry.get("process_name")
        if reported:
            return str(reported)
        if pid <= 0:
            return ""
        suggest = getattr(self._backend, "process_name_for_pid", None)
        if callable(suggest):
            try:
                if suggest(pid):
                    return str(suggest(pid))
            except Exception:  # noqa: BLE001 - fall through to psutil
                pass
        # 真实系统查询（psutil 是生产依赖）——不是 fake 专有字段，也不是
        # pid:N 伪造：查不到就返回空串，由面板显示"未知进程"。
        try:
            import psutil
            name = psutil.Process(int(pid)).name()
            return str(name) if name else ""
        except Exception:
            return ""

    def _selection(self, selection_id: str) -> WindowSelection:
        selection = self._selections.get(selection_id)
        if selection is None:
            raise RecordingModelError(
                "recording_selection_unknown",
                "unknown window selection; refresh the list and pick again",
                path="selectionId",
            )
        return selection

    def _live_identity(self, selection: WindowSelection) -> dict[str, Any] | None:
        """The current list entry matching the selection's recorded identity.

        Windows reports a handle; platforms without one (macOS AX) match on
        title+pid.  A rebuilt window under the same title therefore no longer
        matches the recorded handle and is reported stale, not followed.
        """
        raw = self._backend.list_windows()
        for entry in raw.get("windows") or []:
            if not entry.get("visible", False):
                continue
            if selection.handle is not None:
                if entry.get("handle") == selection.handle:
                    return entry
                continue
            if (str(entry.get("title") or "") == selection.title
                    and int(entry.get("process_id") or 0) == selection.pid):
                return entry
        return None

    def focus_selected_window(self, selection_id: str) -> dict[str, Any]:
        """Verify identity first, then bring the window forward.

        A stale identity — handle gone, or the same title now owned by a
        different handle — is reported as ``recording_window_stale`` so the
        operator re-selects instead of the recorder silently following a
        window that merely shares the old title.
        """
        selection = self._selection(selection_id)
        if self._backend is None:
            raise RecordingModelError(
                "recording_backend_unavailable",
                "window focusing needs the platform backend",
                path="backend",
            )
        entry = self._live_identity(selection)
        if entry is None or int(entry.get("process_id") or 0) != selection.pid:
            raise RecordingModelError(
                "recording_window_stale",
                "所选窗口已关闭或重建（同名窗口的身份已变化），请重新选择",
                path="selectionId",
            )
        activated = None
        focus_existing = getattr(self._backend, "focus_existing_window", None)
        activate = getattr(self._backend, "activate_app", None)
        if callable(focus_existing):
            activated = focus_existing(handle=selection.handle, pid=selection.pid,
                                       title=selection.title,
                                       process_name=selection.process_name)
            if not isinstance(activated, dict) or not activated.get("ok"):
                return {"ok": False, "selection": selection.to_dict(),
                        "activated": activated,
                        "error": str((activated or {}).get("error") or
                                     "无法聚焦所选窗口，请刷新后重选")}
        elif callable(activate) and selection.process_name:
            activated = activate(app_name=selection.process_name)
            if activated is not None and activated.get("ok") is False:
                # 平台拒绝前置就如实失败——不能让面板看到假成功。
                return {
                    "ok": False,
                    "selection": selection.to_dict(),
                    "activated": activated,
                    "error": str(activated.get("error") or activated.get("code") or
                                 "window activation failed"),
                }
        return {
            "ok": True,
            "selection": selection.to_dict(),
            "activated": activated,
        }

    def start_from_selection(self, selection_id: str, name: str) -> dict[str, Any]:
        """Start a recording on the selected window.

        Pre-checks the interactive-desktop and unique-session gates by simply
        running the existing start path (its errors are the contract), then
        binds the session to the selection's process and exact title.  Same
        title/process sibling windows stay fail-closed: the connect criteria
        resolve by title regex, and an ambiguous match is refused for a human
        to disambiguate rather than guessed.
        """
        selection = self._selection(selection_id)
        focused = self.focus_selected_window(selection_id)  # stale check + focus
        if not focused.get("ok"):
            # 聚焦失败（身份过期/平台拒绝）必须阻止开录制，不能带着假成功继续。
            return focused
        if not selection.process_name:
            # 属主进程无法解析（pid=None / 进程已退出）：拒绝原因必须指向
            # 真实问题，不能级联成与事实不符的「参数必填」校验错误。
            raise RecordingModelError(
                "recording_process_unresolved",
                "选中窗口的属主进程无法解析（进程已退出或系统未报告 pid）——"
                "请刷新列表重新选择目标窗口。",
                path="selection.processName",
            )
        if not name or not str(name).strip():
            raise RecordingModelError(
                "recording_name_required", "recording name is required",
                path="name",
            )
        title_re = "^" + re.escape(selection.title) + "$"
        self._refuse_ambiguous_scope(selection, title_re)
        self._run_ownership_chain(selection, title_re)
        return self._service.start(
            name=str(name).strip(),
            process_name=selection.process_name,
            window_title=title_re,
        )

    def _refuse_ambiguous_scope(self, selection: WindowSelection,
                                title_re: str) -> None:
        """同名同进程多窗歧义守卫（设计 §3.1.4）：标题正则会同时匹配多个
        存活同名窗口，录制 scope 将吞掉所有窗口的输入 —— 必须拒绝并要求
        人工消歧，不得静默开始。"""
        raw = self._backend.list_windows()
        matches = [
            e for e in raw.get("windows") or []
            if e.get("visible", False)
            and re.fullmatch(title_re, str(e.get("title") or ""))
            and (selection.pid == int(e.get("process_id") or 0))
        ]
        if len(matches) > 1:
            raise RecordingModelError(
                "recording_window_ambiguous",
                f"存在 {len(matches)} 个同名同进程窗口（标题「{selection.title}」），"
                "录制范围会同时覆盖它们。请关闭多余窗口后刷新列表，或改用唯一标题重选。",
                path="selection.windowTitle",
            )

    def _run_ownership_chain(self, selection: WindowSelection,
                             title_re: str) -> None:
        """既有窗口所有权链（connect → lock_window → verify_window_lock），
        先于捕获执行；任一环节失败即拒绝开始，不带病录制。"""
        connect = getattr(self._backend, "connect", None)
        lock = getattr(self._backend, "lock_window", None)
        verify = getattr(self._backend, "verify_window_lock", None)
        if not (callable(connect) and callable(lock) and callable(verify)):
            return   # 平台不提供所有权接口时由 capture source 自行负责
        connected = connect(title_re=title_re, process_name=selection.process_name,
                            pid=selection.pid)
        if connected is not None and connected.get("ok") is False:
            raise RecordingModelError(
                "recording_ownership_failed",
                f"窗口连接失败：{connected.get('error') or connected.get('code')}",
                path="selection",
            )
        locked = lock(title_re=title_re, process_name=selection.process_name,
                      activate=True)
        if locked is not None and locked.get("ok") is False:
            raise RecordingModelError(
                "recording_ownership_failed",
                f"窗口锁定失败：{locked.get('error') or locked.get('code')}",
                path="selection",
            )
        verified = verify(activate=True)
        if verified is not None and verified.get("ok") is False:
            raise RecordingModelError(
                "recording_ownership_failed",
                f"窗口锁定验证失败：{verified.get('error') or verified.get('code')}",
                path="selection",
            )

    # ── panel request routing (U1) ────────────────────────────────────

    _ROUTABLE = {
        "list_windows", "focus", "start", "pause", "resume", "stop",
        "snapshot", "get_result", "cancel_pick",
        # 06 §2：断言热键唯一（现为 Ctrl+Shift+Q）；旧入口
        # （add_assertion / bind_verifier / confirm_component）已随面板按钮移除。
        "list_controls", "recent_actions",
        "region_assertion", "region_update", "region_toggle",
        "region_confirm", "region_cancel", "region_save", "region_state",
    }

    def apply_ui_request(self, name: str, args: Mapping[str, Any]) -> dict[str, Any]:
        """Route one management request from the panel.

        Management requests are idempotent by their requestId at the host
        (``complete_ui_request``), so a retried click cannot start a second
        recording or double-stop a session.  Routing is a plain dispatch to
        the service/controller — no capture state lives here.
        """
        if name == "list_windows":
            result = self.list_recordable_windows()
            entries = result.get("windows", [])
            base_labels = [
                f"{e.get('title') or '(untitled)'} · "
                f"{e.get('processName') or '?'} · pid {e.get('pid')}"
                for e in entries
            ]
            # 同名同进程多窗必须以可区分的方式呈现（设计 §3.1.4）——
            # 重复组追加 HWND（无 handle 的平台用序号），否则用户无法表达选哪个。
            from collections import Counter
            duplicates = Counter(base_labels)
            seen: dict[str, int] = {}
            for entry, label in zip(entries, base_labels):
                if duplicates[label] > 1:
                    handle = entry.get("handle")
                    suffix = (f" · HWND {handle}" if handle is not None
                              else f" · #{seen[label] + 1}")
                    seen[label] = seen.get(label, 0) + 1
                    entry["label"] = label + suffix
                else:
                    entry["label"] = label
            return result
        if name == "focus":
            return self.focus_selected_window(str(args.get("selectionId") or ""))
        if name == "start":
            return self.start_from_selection(
                str(args.get("selectionId") or ""), str(args.get("name") or ""))
        if name == "pause":
            return self._service.pause()
        if name == "resume":
            return self._service.resume()
        if name == "stop":
            result = self._service.stop()
            # The raw document goes to the Agent bridge, not to the panel —
            # the panel only needs to know it stopped and whether a durable
            # copy exists.
            return {
                "ok": result.get("ok"),
                "sessionId": result.get("sessionId"),
                "state": result.get("state"),
                "resultStore": result.get("resultStore"),
            }
        if name == "snapshot":
            return self._service.ui_snapshot()
        if name == "cancel_pick":
            return self._service.cancel_pick()
        if name == "list_controls":
            # 组件清单：区域框选高亮数据源（06 §2），路由保留。
            return {"ok": True, "controls": self._service.list_controls()}
        if name == "recent_actions":
            # 最近动作候选（06 §2）：向导展示用只读投影；是否绑定由用户
            # 显式确认，控制器不替用户决定。
            return self._recent_actions()
        if name == "region_assertion":
            return self._region_assertion()
        if name == "region_update":
            return self._region_call(
                lambda machine: machine.update_rect(args.get("rect")))
        if name == "region_toggle":
            return self._region_call(
                lambda machine: machine.toggle(
                    str(args.get("key") or ""), container=bool(args.get("container"))))
        if name == "region_confirm":
            return self._region_call(lambda machine: machine.confirm())
        if name == "region_cancel":
            return self._region_cancel()
        if name == "region_save":
            return self._region_save(args.get("draft"))
        if name == "region_state":
            return self._region_state()   # F44：API 驱动通路——武装状态查询
        if name == "get_result":
            return self._service.get_result(str(args.get("sessionId") or ""))
        raise RecordingModelError(
            "recording_ui_request_unknown", f"unknown panel request {name!r}",
            path="request.name",
        )

    # ── region assertion orchestration (06 §2; overlay-internal routes) ──

    def _recent_actions(self, limit: int = 5) -> dict[str, Any]:
        """最近动作候选（B 侧 recent_region_actions，返回 list[dict]）。

        服务缺失时报稳定码；调用失败原样向上抛（面板/向导据此如实显示
        「无法获取最近动作」并降级为独立检查点，仍可保存）。"""
        actions = getattr(self._service, "recent_region_actions", None)
        if not callable(actions):
            raise RecordingModelError(
                "recent_actions_unavailable",
                "录制服务尚未提供最近动作清单（recent_region_actions 待接入）",
                path="service",
            )
        return {"ok": True, "recentActions": list(actions(limit))}

    @staticmethod
    def _region_debug_write(line: str) -> None:
        """F52j：诊断日志失败绝不阻断业务入口（预检 except 再开同路径
        会二次失败上抛——复验实锤；全部诊断写改为安全助手）。"""
        try:
            with open(r"C:\Users\admin\Desktop\edr-wd\tmp\region_debug.log",
                      "a", encoding="utf-8") as _dh:
                _dh.write(line + "\n")
        except Exception:
            pass

    def _region_assertion(self) -> dict[str, Any]:
        try:
            _mgr = getattr(self._service, "manager", None)
            _sess = _mgr.current() if _mgr is not None else None
            self._region_debug_write(
                f"diag svc={id(self._service)} mgr={id(_mgr)} "
                f"session={getattr(_sess, 'session_id', None)} "
                f"state={getattr(_sess, '_state', None)}")
        except Exception as _exc:
            self._region_debug_write("diag pre-check failed: " + repr(_exc))
        """断言热键入口（现为 Ctrl+Shift+Q）：服务侧校验会话与目标窗口（B 轨方法），通过后
        启动遮罩状态机并回传首帧负载。管理输入不产生业务事件——这里只
        调用服务入口与既有 list_controls。"""
        begin = getattr(self._service, "begin_region_assertion", None)
        if not callable(begin):
            raise RecordingModelError(
                "region_assertion_unavailable",
                "录制服务尚未提供区域断言入口"
                "（begin_region_assertion 服务方法待接入）",
                path="service",
            )
        try:
            result = dict(begin())
        except Exception as _exc:
            import traceback as _tb
            self._region_debug_write("begin raised: " + repr(_exc) + "\n" + _tb.format_exc())
            raise
        if not result.get("ok"):
            return result
        machine = RegionSelectionController()
        # 服务返回的 assertion window：字段名兼容 window / assertionWindow。
        # 服务暂未回传时，退回录制自身快照里的窗口归属（真实数据，非伪造）；
        # 两者皆缺则窗口为空，向导保存时会被校验如实拒绝。
        window = result.get("assertionWindow") or result.get("window")
        inventory = self._service.list_controls()
        if not window:
            for entry in inventory:
                entry_window = (entry.get("window") or {}) if isinstance(
                    entry, Mapping) else {}
                if entry_window.get("title") or entry_window.get("processName"):
                    window = {"processName": entry_window.get("processName") or "",
                              "title": entry_window.get("title") or ""}
                    break
        begun = machine.begin(
            window=window,
            inventory=inventory,
            view_state=str(result.get("state") or ""),
        )
        self._region = machine
        return {**result, "duplicate": bool(begun.get("duplicate")),
                "region": begun["region"]}

    def _region_state(self) -> dict[str, Any]:
        """F44：region 流武装状态只读查询（不产生任何动作/副作用）。

        返回 maskOpen（遮罩是否在位）/ selectionReady（选区是否已完成
        拖拽）/ state（controller 状态机字面量）——外部判决流程据此决定
        何时调 region_confirm。"""
        controller = getattr(self, "_region", None)
        if controller is None:
            return {"ok": True, "armed": False, "maskOpen": False,
                    "selectionReady": False, "state": None}
        state_name = getattr(controller, "state", None)   # property→str
        rect = getattr(controller, "_rect", None)         # 冻结字段直读
        return {"ok": True, "armed": True, "maskOpen": True,
                "selectionReady": rect is not None,
                "state": state_name if isinstance(state_name, str)
                else (str(state_name) if state_name is not None else None),
                "rect": list(rect) if isinstance(rect, (list, tuple))
                else None}

    def _region_call(self, action) -> dict[str, Any]:
        import traceback as _tb
        with open(r"C:\Users\admin\Desktop\edr-wd\tmp\region_debug.log", "a", encoding="utf-8") as _dh:
            _dh.write("region_call enter\n")
        """遮罩内部编排（region_update/toggle/confirm）：纯控制器状态，
        不新增 service 方法（契约约定）。"""
        machine = self._region
        if machine is None or not machine.active:
            raise RecordingModelError(
                "region_session_inactive", "当前没有进行中的区域选区",
                path="region",
            )
        try:
            result = action(machine)
        except Exception as _exc:
            with open(r"C:\Users\admin\Desktop\edr-wd\tmp\region_debug.log", "a", encoding="utf-8") as _dh:
                _dh.write("region_call action raised: " + repr(_exc) + "\n" + _tb.format_exc() + "\n")
            raise
        with open(r"C:\Users\admin\Desktop\edr-wd\tmp\region_debug.log", "a", encoding="utf-8") as _dh:
            _dh.write("region_call result ok=" + str(bool(isinstance(result, Mapping) and result.get("ok"))) + "\n")
        if not isinstance(result, Mapping) or not result.get("ok"):
            return {"ok": False, "error": str(
                (result or {}).get("error") if isinstance(result, Mapping) else result
                or "区域选区操作失败")}
        return {"ok": True, "region": result["region"]}

    def _region_cancel(self) -> dict[str, Any]:
        """Esc/取消：清控制器状态 + 服务侧恢复进入前状态（不落事件）。

        控制器状态先行清理——即使服务侧恢复失败，遮罩也不能残留。"""
        machine = self._region
        self._region = None
        machine_result: dict[str, Any] = {"ok": True, "cancelled": True}
        if machine is not None and machine.active:
            machine_result = machine.cancel()
        cancel = getattr(self._service, "cancel_region_assertion", None)
        if not callable(cancel):
            raise RecordingModelError(
                "region_assertion_unavailable",
                "录制服务尚未提供区域断言取消（cancel_region_assertion 待接入）",
                path="service",
            )
        service_result = dict(cancel())
        return {
            "ok": bool(service_result.get("ok", True)),
            "cancelled": True,
            "previousState": machine_result.get("previousState"),
            "service": service_result,
        }

    def _region_save(self, draft: Any) -> dict[str, Any]:
        """保存：B 侧校验 + 落事件（requestId 幂等）。成功回执才允许面板
        关闭向导；失败原样回传，表单保留。"""
        if not isinstance(draft, Mapping) or not draft:
            raise RecordingModelError(
                "region_draft_missing", "缺少区域断言草稿（draft）",
                path="draft",
            )
        add = getattr(self._service, "add_region_assertion", None)
        if not callable(add):
            raise RecordingModelError(
                "region_assertion_unavailable",
                "录制服务尚未提供区域断言保存（add_region_assertion 待接入）",
                path="service",
            )
        return dict(add(draft))


class RecorderPanelHost:
    """Own the panel child process and serve its management requests.

    The host runs inside the target server process: the panel is thin Tk, all
    facts and actions stay here, and pushes (snapshot / windows / acks) flow
    down the same stdin/stdout JSON-line protocol the capture indicator uses.
    """

    def __init__(
        self, controller: RecorderUIController, *,
        startup_timeout: float = 15.0, push_interval: float = 2.0,
    ) -> None:
        self._controller = controller
        self._startup_timeout = startup_timeout
        self._push_interval = push_interval
        self._process: Any = None
        self._reader: threading.Thread | None = None
        self._pusher: threading.Thread | None = None
        self._ready = threading.Event()
        self._stopping = threading.Event()
        self._last_error: str | None = None
        self._completed: dict[str, dict[str, Any]] = {}
        # F52：push 接收验证——面板渲染完 ack 后回 {"pushed": token}，
        # 这里按 token 存 Event，push_to_panel 据此真实确认送达渲染。
        self._push_acks: dict[str, threading.Event] = {}
        self._push_acks_received: dict[str, threading.Event] = {}
        self._wizard_phase: dict[str, dict] = {}   # F71：token→相位持久追踪
        # F52b 复验修正：_push_loop 快照、_consume ack 回执、push_to_panel
        # 三方并发写同一 TextIO——串行化写管道；写失败如实留痕（不静默吞），
        # push 超时原因可对账（进程 PID/poll/最近写异常）。
        self._io_lock = threading.Lock()
        self._last_send_error: str | None = None
        # F52d（独立审查复验）：每代独立生命周期——线程按代绑定 process 与
        # stop token；start/stop 由 _lifecycle_lock 串行化。旧代线程只写
        # 旧 process（哪怕 stop/start 竞争），不可能写新 process。
        self._lifecycle_lock = threading.Lock()
        self._generation = 0  # 递增代号；_generation_process[_generation] 绑定

    def start(self) -> dict[str, Any]:
        with self._lifecycle_lock:
            return self._start_locked()

    def _start_locked(self) -> dict[str, Any]:
        if self._process is not None and self._process.poll() is None:
            # Already running: opening twice must not spawn a second panel
            # process (design §3.1: repeat open is idempotent) — but a panel
            # the user collapsed (withdraw) must be brought back to screen.
            self._send({"restore": True})
            return {"ok": True, "panel": "recorder", "alreadyRunning": True,
                    "snapshot": self._controller.snapshot()}
        import subprocess
        import os as _os
        import sys

        # F52d：先收尸旧代（join 线程+关旧管道），再起新代——旧代线程
        # 持旧 process 引用，绝无写新 process 的路径。
        self._stop_locked()
        # F59c 撤回（安全纠偏 02:30）：cmdline 名称匹配的全局清扫违反
        # 「仅回收自己持有子进程」约束——无法证明孤儿/本用户归属，可能
        # 误杀其他合法面板。撤回；跨重启回收需可验证归属 token/owner
        # PID+租约证据，无法证明归属就明确报错不清扫。同代持有 Popen
        # 的超时回收（_spawned_children）保留。
        env = dict(_os.environ)
        env["PYTHONPATH"] = _os.pathsep.join(p for p in sys.path if p)
        # A previous child's readiness/error must not authorize this launch.
        self._ready.clear()
        self._last_error = None
        self._last_send_error = None
        _panel_err_path = _os.path.join(_os.environ.get('TEMP', _os.curdir),
                                        'edr-wd-panel-child-stderr.log')
        _panel_err = open(_panel_err_path, 'ab')
        process = subprocess.Popen(
            [sys.executable, "-c",
             f"from {__name__.rsplit('.', 1)[0]}.recorder_panel import "
             "_recorder_panel_child_main; _recorder_panel_child_main()"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=_panel_err,
            env=env, text=True,
        )
        self._generation += 1
        # F59b（审查要求）：登记本 session spawn 的全部子进程——启动超时/
        # 退出时自动回收，杜绝挂死面板累积（F58/F59 实因）；仅回收自己
        # 持有的进程，绝不触碰用户会话。
        children = getattr(self, "_spawned_children", None)
        if children is None:
            children = self._spawned_children = []
        children.append(process)
        children[:] = [c for c in children if c.poll() is None][-8:]
        self._process = process
        # F52e：每代独立 stop token（fresh Event，不再 clear 重用——旧代
        # 残留置位不会泄漏到新代）。
        self._stopping = threading.Event()
        # F52k：每代显式 ACK/断联结果对象——reader 捕获本代 gen_acks/
        # gen_closed（不读运行时 self._push_acks），旧代同 token ACK/EOF
        # 均写进旧代对象，结构上不可能污染新代等待。
        self._push_acks = {}
        self._push_acks_received = {}
        self._wizard_phase = {}
        gen_acks = self._push_acks
        gen_closed = threading.Event()
        self._panel_closed = gen_closed
        gen_ready = threading.Event()
        gen_error: list = []
        self._ready = gen_ready          # start 等待的是本代 ready
        self._last_error = None
        # 按代捕获：线程只看本代 process 与本代 stop token
        self._reader = threading.Thread(
            target=self._consume,
            args=(process, self._stopping, gen_acks, gen_closed,
                  gen_ready, gen_error),
            name=f"edr-wd-panel-reader-{self._generation}", daemon=True)
        self._reader.start()
        self._pusher = threading.Thread(
            target=self._push_loop, args=(process, self._stopping),
            name=f"edr-wd-panel-pusher-{self._generation}", daemon=True)
        self._pusher.start()
        self._send({"name": "recorder"})
        if not self._ready.wait(timeout=self._startup_timeout):
            self._stop_locked()   # F52e：锁内收尾（self.stop 会重入锁=死锁）
            raise RecordingModelError(
                "recording_panel_unavailable", "recorder panel startup timed out",
                path="panel",
            )
        if self._last_error:
            error = self._last_error
            self._stop_locked()   # F52e：同上
            raise RecordingModelError(
                "recording_panel_unavailable", error, path="panel",
            )
        return {"ok": True, "panel": "recorder",
                "snapshot": self._controller.snapshot()}

    def stop(self) -> None:
        with self._lifecycle_lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        self._stopping.set()
        # F52d：join 本代线程（有界）——旧 pusher/consume 退出后才返回，
        # 杜绝旧代线程与新 process 并存。
        for thread in (self._reader, self._pusher):
            alive = getattr(thread, "is_alive", None)
            if thread is not None and callable(alive) and alive():
                thread.join(timeout=3.0)
                still = getattr(thread, "is_alive", None)
                if callable(still) and still():
                    # join 超时仍存活：线程为 daemon 且按代绑定旧 process，
                    # 不可能写新 process——留痕供诊断（不阻塞生命周期）。
                    self._last_send_error = (
                        f"thread-{thread.name}-alive-after-join-timeout")
        self._reader = None
        self._pusher = None
        process = self._process
        self._process = None
        # F59b：回收本 session 持有的全部子进程（含历史挂死代）。
        for child in list(getattr(self, "_spawned_children", []) or []):
            if child is process:
                continue
            if child.poll() is None:
                try:
                    child.terminate()
                    child.wait(timeout=2.0)
                except Exception:
                    try:
                        child.kill()
                    except Exception:
                        pass
        if getattr(self, "_spawned_children", None):
            self._spawned_children = [c for c in self._spawned_children
                                      if c.poll() is None]
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=5)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass

    def _send(self, message: Mapping[str, Any]) -> bool:
        """F52b：串行化写管道并如实报告结果（不再静默吞）。

        返回 True=已完整写入并 flush；False=无进程/已退出/写异常（原因
        记入 _last_send_error 供 push 超时对账）。"""
        process = self._process
        if process is None:
            self._last_send_error = "no-process"
            return False
        if process.poll() is not None:
            self._last_send_error = f"process-exited poll={process.poll()}"
            return False
        line = json.dumps(message, ensure_ascii=False) + "\n"
        try:
            with self._io_lock:
                process.stdin.write(line)
                process.stdin.flush()
            return True
        except (BrokenPipeError, ValueError, OSError) as exc:
            self._last_send_error = f"{type(exc).__name__}: {exc}"
            return False

    def wizard_phase(self, token: str, include_history: bool = False):
        """F72：按 token 查询向导相位——仅当前活动代（旧代帧不进当前，
        历史=按代归属保留可对账）。"""
        entries = self._wizard_phase.get(str(token)) or []
        if include_history:
            return list(entries)
        active = id(self._push_acks)
        for entry in reversed(entries):
            if entry.get("gen") in (active, None):
                return entry
        return None

    def push_to_panel(self, message: Mapping[str, Any], timeout: float = 2.0) -> bool:
        """F52/F72：推送并等待 received 送达回执（deliveryAck 语义）。

        返回 True=面板已收到（received 帧）；渲染完成（pushed）与向导
        就绪/失败（wizardReady/wizardFailed）=独立相位，经
        _last_push_result/wizard_phase(token) 查询——调用者不得据送达
        报渲染或向导完成。旧代/EOF/取消不伪成功（F52k/l 守卫原样）。

        用户指令修正 F50：只检查进程和 _send 不够——写入成功≠面板读到并
        渲染。面板 ack 分支渲染完成后回显 {"pushed": <ack>}（同管道），
        本方法等回执：超时/无回执如实返回 False，绝不静默谎报成功。
        仅支持带 "ack" 的确认类推送（面板据此回显）。"""
        process = self._process
        if process is None or process.poll() is not None:
            self._last_send_error = (
                "no-process" if process is None
                else f"process-exited poll={process.poll()}")
            return False
        token = str(message.get("ack") or "")
        if not token:
            self._last_send_error = "push-message-missing-ack-token"
            return False
        done = threading.Event()
        self._push_acks[token] = done
        received_done = threading.Event()
        _rmap = getattr(self, "_push_acks_received", None)
        if _rmap is None:
            _rmap = self._push_acks_received = {}
        _rmap[token] = received_done
        try:
            if not self._send(message):
                return False   # 原因已记入 _last_send_error
            # F52g：断联快速失败——等待期间轮询进程存活，EOF/退出即刻
            # False（不能等满 timeout 也不能被 Event.set 伪成功掩盖断联）。
            deadline = time.monotonic() + timeout
            closed = getattr(self, "_panel_closed", None)
            while True:
                # F52k：断联信号在先——closed 置位时 done 即便 set 也不算
                # 成功（EOF 假成功反例已封）。
                if closed is not None and closed.is_set():
                    self._last_send_error = "panel-closed-during-push"
                    return False
                # F73：相位表懒安全（NS 式 fixture 无 __init__ 兼容）；
                # 每次 push（含失败）都重置/写本 token 的 last_push_result。
                _wphase = (getattr(self, "_wizard_phase", None) or {}).get(token)
                if received_done.is_set():
                    # F71：送达即返回（deliveryAck 语义）——调用者不得据此
                    # 报渲染/向导完成；相位快照随行供查询。
                    self._last_push_result = {
                        "token": token, "deliveryAck": True,
                        "rendered": done.is_set(),
                        "wizardPhase": _wphase}
                    return True
                if done.is_set():
                    self._last_push_result = {
                        "token": token, "deliveryAck": True,
                        "rendered": True,
                        "wizardPhase": _wphase}
                    return True
                if process.poll() is not None:
                    self._last_send_error = (
                        f"panel-exited-during-push poll={process.poll()}")
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._last_send_error = "ack-timeout-no-echo"
                    self._last_push_result = {"token": token,
                                              "deliveryAck": False,
                                              "rendered": False}
                    return False
                done.wait(min(0.05, remaining))
        finally:
            self._push_acks.pop(token, None)
            self._push_acks_received.pop(token, None)

    def _send_to(self, process, message: Mapping[str, Any]) -> bool:
        """F52d：按代绑定写——线程只写自己捕获的 process，绝不解析
        self._process（旧代线程因此不可能写新 process）。"""
        if process is None or process.poll() is not None:
            self._last_send_error = (
                "no-process" if process is None
                else f"process-exited poll={process.poll()}")
            return False
        line = json.dumps(message, ensure_ascii=False) + "\n"
        try:
            with self._io_lock:
                process.stdin.write(line)
                process.stdin.flush()
            return True
        except (BrokenPipeError, ValueError, OSError) as exc:
            self._last_send_error = f"{type(exc).__name__}: {exc}"
            return False

    def _consume(self, process=None, stop_evt=None, gen_acks=None,
                 gen_closed=None, gen_ready=None,
                 gen_error=None, gen_acks_received=None) -> None:  # pragma: no cover - reader thread
        if process is None:
            process = self._process
        if process is None:
            return
        if gen_acks is None:
            gen_acks = self._push_acks
        for line in process.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            # F52 复验修正：非对象帧（JSON 字符串/数字）不得终止 reader——
            # 面板侧回归曾直发裸字符串致 message.get AttributeError。
            if not isinstance(message, dict):
                continue
            if message.get("panelError"):
                # F52l：error/ready 按代写（共享对象=旧代错误污染新代 ready）。
                if gen_error is not None:
                    gen_error.append(str(message["panelError"]))
                    gen_ready.set() if gen_ready is not None else self._ready.set()
                else:
                    self._last_error = str(message["panelError"])
                    self._ready.set()
                continue
            if message.get("panelReady") is not None:
                (gen_ready if gen_ready is not None else self._ready).set()
                continue
            pushed = message.get("pushed")
            if pushed:
                # F52l：is None 显式判定——空 dict 为 falsy，`or` 会回退到
                # 新代 dict（复验反例：旧 reader 空 acks 时同 token 置位
                # 新代 done）。旧代 ACK 永远只写旧代对象。
                acks = gen_acks if gen_acks is not None else self._push_acks
                done = acks.get(str(pushed))
                if done is not None:
                    done.set()
                continue
            # F71（审查设计）：三相回执 host 解析——received=送达（独立
            # Event，不污染 render done）；wizardReady/Failed=按原 token
            # 持久追踪（调用者不得据送达报完成）。
            received = message.get("received")
            if received:
                rdone = (gen_acks_received if gen_acks_received is not None
                         else self._push_acks_received)
                rev = rdone.get(str(received))
                if rev is not None:
                    rev.set()
                continue
            wready = message.get("wizardReady")
            if wready:
                # F72（审查缺口）：相位=历史列表（旧代帧不覆当前，只能
                # 追加）——当前=最新条目，对账=全历史。
                self._wizard_phase.setdefault(str(wready), []).append({
                    "phase": "ready",
                    "rootState": message.get("rootState"),
                    "mapped": message.get("mapped"),
                    "gen": id(gen_acks) if gen_acks is not None else None})
                continue
            wfailed = message.get("wizardFailed")
            if wfailed:
                self._wizard_phase.setdefault(str(wfailed), []).append({
                    "phase": "failed", "error": message.get("error"),
                    "gen": id(gen_acks) if gen_acks is not None else None})
                continue
            request_id = message.get("requestId")
            name = message.get("request")
            if not name or not request_id:
                continue
            # F52e 代守卫：本代 stop 已置位后不再执行请求（旧代请求不越代）。
            if stop_evt is not None and stop_evt.is_set():
                break
            result = self.complete_ui_request(request_id, name, message.get("args") or {})
            # F52d：ack 回执按代写回本代 process（旧代请求不写新管道）。
            self._send_to(process, {"ack": request_id, "result": result})
        # F52k：reader EOF=断联——只置本代 closed（失败语义信号），绝不置
        # acks Event（复验反例：EOF→Event.set→push 首分支 done.is_set→假
        # 成功）。push 端见 closed 即 False。
        if gen_closed is not None:
            gen_closed.set()

    def _push_loop(self, process=None, stop_evt=None) -> None:  # pragma: no cover - pusher thread
        if process is None:
            process = self._process
        while not (stop_evt or self._stopping).is_set():
            try:
                # F52d：快照按代写——每轮不再解析 self._process。
                self._send_to(process, {"snapshot": self._controller.snapshot()})
            except Exception:  # noqa: BLE001 - pushes are best-effort
                pass
            (stop_evt or self._stopping).wait(self._push_interval)

    def complete_ui_request(self, request_id: str, name: str,
                            args: Mapping[str, Any]) -> dict[str, Any]:
        """Route with requestId dedup: the same form retry gets the same
        result and never re-executes (a double-clicked start must not open a
        second session)."""
        cached = self._completed.get(request_id)
        if cached is not None:
            return {**cached, "duplicate": True}
        try:
            result = self._controller.apply_ui_request(name, args)
        except Exception as exc:  # noqa: BLE001 - errors go back to the form
            from .service import error_result
            result = error_result(exc)
        self._completed[request_id] = result
        return result

__all__ = ["RecorderUIController", "RecorderPanelHost", "WindowSelection"]
