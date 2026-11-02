"""Target-local recorder panel (service-level, U1) running in its own process.

The panel is the always-available entry on the target's interactive desktop —
it exists while idle, not only during a capture.  It runs in a child process
for the same reason the capture indicator does: Aqua Tk must own the process
main thread, and a Tcl panic must be able to kill only the panel.

Protocol (JSON lines):

- parent → child: ``{"snapshot": {...}}``, ``{"windows": [...]}``,
  ``{"ack": requestId, "result": {...}}``, ``{"bye": true}``
- child → parent: ``{"request": name, "requestId": id, "args": {...}}``

The panel only renders state and sends management requests; it never touches
session internals.  Requests are routed by
``RecorderUIController.apply_ui_request`` in the service process, and every
window title carries ``RECORDER_UI_MARKER`` so screenshot redaction, input
attribution and window discovery keep excluding it.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from types import SimpleNamespace
from typing import Any, Callable, Mapping

from .indicator import RECORDER_UI_MARKER
from .models import CaptureScope
from .region_overlay import bindall_diag_enabled
from .region_overlay import overlay_synth_enabled

PANEL_TITLE = f"EDR-WD 录制面板 {RECORDER_UI_MARKER}"



# F52g：退休 LL 钩子探针保活列表（防 trampoline 悬空,见 _ll_watchdog_tick）
_RETIRED_HOOK_PROBES: list = []

class RecorderPanel:
    """Tk panel.  Owns no state beyond widgets; ``render`` applies snapshots."""

    def __init__(
        self,
        name: str,
        scope: CaptureScope | None,
        on_request: Callable[[str, str, Mapping[str, Any]], None],
        *,
        arm_valve_watchdog: bool = False,
        on_pushed: Callable[[str], None] | None = None,
    ) -> None:
        self.name = name
        self.scope = scope
        self.on_request = on_request
        # F52：push 渲染回执——ack 消息渲染完成后经此回调回显
        # {"pushed": <ack>}，host 端 push_to_panel 据此确认送达。
        self.on_pushed = on_pushed
        # 看门狗线程只在真实子进程入口武装（测试进程绝不 os._exit）。
        self._arm_valve_watchdog = arm_valve_watchdog
        import queue
        self._incoming: "queue.Queue[Mapping]" = queue.Queue()
        self._root = None
        self._window_var = None
        self._windows: list[dict[str, Any]] = []
        self._name_var = None
        self._status_label = None
        self._detail_label = None
        self._start_button = None
        self._pause_button = None
        self._stop_button = None
        self._messagebox = None
        self._ttk = None
        self._tk = None
        # 区域选区遮罩（06 §2）：遮罩开着时主面板收起，Esc/取消后恢复。
        self._region_overlay = None
        self._region_canvas = None
        self._region_drag_start = None
        self._region_status_label = None
        self._region_cancel_request_id: str | None = None
        # 区域断言向导（遮罩确认后打开；保存收到成功回执才关闭）。
        self._region_wizard = None
        self._region_actions_request_id: str | None = None
        # 输入死亡安全阀（11.26 实机遮罩吞输入事件的防线）。
        self._region_valve = None
        self._region_watchdog_stop: threading.Event | None = None
        self._confirm_sent = False   # F38：confirm 链状态位（host-lost 对账）
        self._synthetic_btn_down: bool | None = None   # F39：统一合成状态
        # Tk 事件路由诊断/兜底（F5）：widget 层派发时间戳（bind_all 去重）、
        # bind_all 兜底按下状态、bind_all 绑定句柄。
        self._region_widget_dispatch: dict[str, float] = {}
        self._region_fallback_active = False
        self._region_diag_bindings: list[tuple[str, Any]] = []
        # F12：面板内 WndProc 只读探针（REGION_WNDPROC_DIAG=1 启用）。
        self._region_wndproc_probes: list[Any] = []
        # F14：双层输入观测器（REGION_INPUT_DIAG=1）——LL 钩子 + raw 汇。
        self._region_hook_probe: Any = None
        # F16：父服务失联检测——stdin 泵退出（管道关闭=服务进程死亡）。
        self._host_lost = threading.Event()
        self._host_lost_handled = False

    # ── child-process entry helpers ───────────────────────────────────

    def run(self) -> None:  # pragma: no cover - exercised on live desktop
        try:
            import tkinter as tk
            from tkinter import messagebox, ttk

            self._tk, self._ttk, self._messagebox = tk, ttk, messagebox
            root = tk.Tk()
            self._root = root
            root.title(PANEL_TITLE)
            root.resizable(False, False)
            root.attributes("-topmost", True)

            frame = ttk.Frame(root, padding=10)
            frame.grid()
            ttk.Label(frame, text=f"EDR-WD 录制面板 · {self.name}").grid(
                row=0, column=0, columnspan=3, sticky="w")
            ttk.Label(
                frame,
                text="此面板位于目标机的交互桌面；录制由本机服务执行。",
                foreground="#555",
            ).grid(row=1, column=0, columnspan=3, sticky="w")

            ttk.Label(frame, text="窗口：").grid(row=2, column=0, sticky="w")
            self._window_var = tk.StringVar()
            window_box = ttk.Combobox(
                frame, textvariable=self._window_var, width=46, state="readonly")
            window_box.grid(row=2, column=1, sticky="w")
            self._window_box = window_box
            ttk.Button(frame, text="刷新", command=lambda: self._request(
                "list_windows")).grid(row=2, column=2)
            ttk.Button(frame, text="定位窗口", command=lambda: self._request(
                "focus", selectionId=self._current_selection_id())).grid(row=3, column=1, sticky="w")

            ttk.Label(frame, text="名称：").grid(row=4, column=0, sticky="w")
            self._name_var = tk.StringVar()
            ttk.Entry(frame, textvariable=self._name_var, width=48).grid(
                row=4, column=1, sticky="w")

            self._paused_view = False
            self._status_label = ttk.Label(frame, text="就绪，尚未录制")
            self._status_label.grid(row=5, column=0, columnspan=3, sticky="w")
            self._detail_label = ttk.Label(frame, text="", foreground="#555")
            self._detail_label.grid(row=6, column=0, columnspan=3, sticky="w")

            self._start_button = ttk.Button(
                frame, text="开始录制",
                command=lambda: self._request(
                    "start", selectionId=self._current_selection_id(),
                    name=self._name_var.get()))
            self._start_button.grid(row=7, column=0)
            self._pause_button = ttk.Button(
                frame, text="暂停", state="disabled",
                command=self._toggle_pause)
            self._pause_button.grid(row=7, column=1)
            self._stop_button = ttk.Button(
                frame, text="停止并保存", state="disabled",
                command=lambda: self._request("stop"))
            self._stop_button.grid(row=7, column=2)
            # 06 §2：断言热键唯一（现为 Ctrl+Shift+Q，用户裁定）；面板的三个旧断言入口按钮
            # （组件拾取 / 动作验证 / 组件列表）已全部移除，不得复活。
            self._verifier_label = ttk.Label(frame, text="", foreground="#b26a00")
            self._verifier_label.grid(row=8, column=0, columnspan=3, sticky="w")
            self._hotkey_label = ttk.Label(frame, text="断言入口：注册中…",
                                           foreground="#555")
            self._hotkey_label.grid(row=9, column=0, columnspan=3, sticky="w")

            root.protocol("WM_DELETE_WINDOW", self._on_close)
            self._register_hotkey()
            root.after(200, self._poll)
            try:
                root.mainloop()
            finally:
                # F39 退出区分器：mainloop 返回（优雅）——与 atexit 行
                # 联合判读；异常栈经 Tk report_callback_exception 落 stderr。
                try:
                    print("[panel] mainloop-exited", file=sys.stderr,
                          flush=True)
                except Exception:
                    pass
        except Exception as exc:  # pragma: no cover - live desktop
            try:
                sys.stdout.write(json.dumps({"panelError": str(exc)}) + "\n")
                sys.stdout.flush()
            except Exception:
                pass

    def _on_close(self) -> None:  # pragma: no cover - live desktop
        """录制中关闭面板 → 停止并保存 / 收起 / 取消（设计 §3.1.7）。"""
        state = (self._last_snapshot or {}).get("viewState")
        if state in {"RECORDING", "PAUSED"}:
            choice = self._messagebox.askyesnocancel(
                "关闭录制面板",
                "录制进行中。选择：\n是 = 停止并保存\n否 = 收起面板（录制继续）\n取消 = 返回",
            )
            if choice is None:
                return
            if choice is True:
                self._request("stop")
                return
            if self._root is not None:
                self._root.withdraw()   # 收起：保留进程与录制，桌面入口可恢复
                return
        if self._root is not None:
            self._root.destroy()

    # ── requests up to the host ───────────────────────────────────────

    def _panel_diag(self, text: str) -> None:
        try:
            import ctypes as _ct
            pt = _ct.wintypes.POINT()
            ok = bool(_ct.windll.user32.GetCursorPos(_ct.byref(pt)))
            err = _ct.windll.kernel32.GetLastError()
            fg = _ct.windll.user32.GetForegroundWindow()
            text = (text + " | selftest GetCursorPos=" + str(ok) + " err=" + str(err)
                    + " pt=" + str((pt.x, pt.y)) + " fg=" + hex(fg))
        except Exception as _e:
            text = text + " | selftest raised " + repr(_e)
        try:
            import os as _os
            with open(_os.path.join(_os.environ.get("TEMP", "."), "panel-diag.log"), "a", encoding="utf-8") as _h:
                _h.write(text + "\n")
        except Exception:
            pass

    def _panel_log_note(self, note: str) -> None:
        """F38：confirm 链打点（stderr=panel 日志通道，F33 落盘）。"""
        try:
            print(f"[confirm] {note}", file=sys.stderr, flush=True)
        except Exception:
            pass

    def _fire_return_key(self) -> None:
        """F43：键盘态轮询 Return 下降沿 → 确认按钮 invoke。

        与 FROM_MOTION 同一已验证模式（轮询读 GetAsyncKeyState、沿即
        证据+动作、合成事件直入 Tk 队列绕过翻译层）；不依赖 motion 流
        ——静止点击盲区的键盘侧补全。仅遮罩存续期采样（valve 存续），
        向导期不误触。"""
        try:
            self._panel_log_note("key-return fired (poll edge)")
        except Exception:
            pass
        button = getattr(self, "_region_confirm_button", None)
        if button is None:
            return
        try:
            button.invoke()
        except Exception:
            pass   # 合成失败绝不外溢

    def _recover_if_windowless(self) -> None:
        """F41：确认链 5s 后向导缺席 → 恢复主窗+红字（不静默消失）。"""
        try:
            if self._region_wizard is not None:
                return   # 向导在位，正常路径
            if self._region_overlay is not None:
                return   # 重新框选中
            if self._root is None:
                return
            self._panel_log_note("recover-if-windowless triggered")
            is_mapped = None
            if callable(getattr(self._root, "winfo_ismapped", None)):
                try:
                    is_mapped = self._root.winfo_ismapped()
                except Exception:
                    is_mapped = None
            if is_mapped is False and callable(
                    getattr(self._root, "deiconify", None)):
                self._root.deiconify()
            if self._detail_label is not None:
                self._detail_label.configure(
                    text="断言向导未能打开：请重新框选或重启录制面板",
                    foreground="#c62828")
        except Exception:
            pass

    def _dispatch_synthetic_button(self, want_down: bool,
                                   x: int, y: int) -> None:
        """F39 统一合成状态机：FROM_MOTION 与 LL 双通道共用以防双触发。
        目标=光标下控件（winfo_containing，屏幕坐标）；状态一致则跳过；
        遮罩树外忽略。合成事件走该控件自身绑定（ttk press+release 对
        触发 command；canvas press/realease 走选区状态机——语义与真实
        输入一致）。"""
        try:
            widget = self._tk.winfo_containing(int(x), int(y))
        except Exception:
            return
        if widget is None:
            return
        if self._synthetic_btn_down == want_down:
            return   # 状态一致（另一通道已生成）——防双触发
        self._synthetic_btn_down = want_down
        sequence = "<Button-1>" if want_down else "<ButtonRelease-1>"
        try:
            widget.event_generate(sequence, x=int(x), y=int(y))
        except Exception:
            pass   # 合成失败绝不外溢

    def _on_ll_button(self, kind: str, x: int, y: int) -> None:
        """LL 钩子 DOWN/UP → 合成（Tk 线程经 _incoming 排空调用）。"""
        self._panel_log_note(f"ll-button {kind} at=({x},{y})")
        self._dispatch_synthetic_button(kind == "down", x, y)

    def _on_confirm_region_clicked(self) -> None:
        """确认按钮 handler（F38 从 lambda 提方法：链路打点可测）。"""
        self._panel_log_note("confirm-clicked")
        self._note_region_input()
        self._request("region_confirm")

    def _request(self, request_name: str, **args: Any) -> None:  # pragma: no cover - live desktop
        self._panel_diag("request " + request_name)
        if request_name == "region_confirm":
            self._confirm_sent = True
            self._panel_log_note("confirm-request emitted")   # F38
        self.on_request(request_name, uuid.uuid4().hex, args)

    def _request_with_id(self, request_name: str, request_id: str,
                         **args: Any) -> None:
        """同 ``_request``，但由调用方指定 requestId——向导保存回执要按
        同一 id 匹配（成功才关窗，失败保留表单）。"""
        self.on_request(request_name, request_id, args)

    def _current_selection_id(self) -> str:
        title = self._window_var.get() if self._window_var is not None else ""
        for entry in self._windows:
            if entry.get("label") == title:
                return str(entry.get("selectionId") or "")
        return ""

    # ── render from host pushes ───────────────────────────────────────

    def render(self, message: Mapping[str, Any]) -> None:
        """Apply one host push (snapshot / windows / ack / restore).  Called on
        the Tk thread via the poll loop."""
        if message.get("regionHotkey"):
            self._on_region_hotkey()
        ll_button = message.get("llButton")   # F39：LL 钩子转发通道
        if isinstance(ll_button, Mapping):
            self._on_ll_button(str(ll_button.get("kind")),
                               int(ll_button.get("x") or 0),
                               int(ll_button.get("y") or 0))
        if "restore" in message:
            self._restore_panel()
        if "windows" in message:
            self._apply_windows(message.get("windows") or [])
        if "snapshot" in message:
            self._last_snapshot: Mapping[str, Any] = message["snapshot"]
            self._render_snapshot(message["snapshot"])
        if "ack" in message:
            result = message.get("result") or {}
            request_id = message.get("ack")
            # list_windows 的结果经 ACK 回来：候选必须渲染，不能只认顶层 push。
            if "windows" in result:
                self._apply_windows(result.get("windows") or [])
            self._render_ack(result)
            region = result.get("region")
            if isinstance(region, Mapping):
                if region.get("state") == "confirmed":
                    # 所选冻结 → 打开向导（遮罩关闭，面板保持收起）。
                    self._panel_log_note("confirm-response confirmed")
                    # F68b（审查指正）：向导源码无 wait_window/grab_set/
                    # mainloop——「模态阻塞根因」未证，after(0) 依据撤回；
                    # 还原同步创建（原行为），echo 链断点按真实 append
                    # stderr+token 节点另证（F68 实机复验为准）。
                    self._open_region_wizard(region.get("selection") or {})
                    # F41 兜底网：5s 后向导仍未在位 → 恢复主窗（windowless
                    # zombie 终极防御——任何静默失败都有可见出口）。
                    if callable(getattr(self._root, "after", None)):
                        self._root.after(5000, self._recover_if_windowless)
                else:
                    self._show_region_overlay(region)
            # 取消回执（无论成败）都关遮罩/向导并恢复面板——Esc 后不能残留。
            if request_id is not None and request_id == self._region_cancel_request_id \
                    and (self._region_overlay is not None
                         or self._region_wizard is not None):
                self._close_region_wizard(restore=False)
                self._close_region_overlay()
            # 最近动作候选回执：喂给打开中的向导（失败由向导如实降级）。
            if request_id is not None \
                    and request_id == self._region_actions_request_id:
                self._region_actions_request_id = None
                wizard = self._region_wizard
                if wizard is not None:
                    wizard.apply_recent_actions(result)
            # 向导保存回执：成功才关向导并恢复面板；失败保留表单。
            wizard = self._region_wizard
            if wizard is not None and request_id is not None \
                    and request_id == wizard.save_request_id:
                if result.get("ok") is True:
                    sequence = result.get("sequence")
                    self._close_region_wizard()
                    if self._detail_label is not None:
                        self._detail_label.configure(text=(
                            "已保存区域断言"
                            + (f"（sequence {sequence}）" if sequence else "")))
                else:
                    wizard.apply_save_ack(result)

    def _apply_windows(self, windows) -> None:
        """候选写入下拉：真实配置 values、保留当前选择（若仍存在）。"""
        self._windows = list(windows or [])
        labels = [str(w.get("label") or w.get("title") or "") for w in self._windows]
        if self._window_box is None:
            return
        self._window_box.configure(values=labels)
        if labels:
            current = self._window_var.get() if self._window_var is not None else ""
            if current not in labels:
                self._window_var.set(labels[0])

    def _register_hotkey(self) -> None:  # pragma: no cover - live desktop
        """在面板子进程内注册断言热键（现为 Ctrl+Shift+Q，用户裁定）。

        注册失败必须在面板上明确告知（红色常驻提示），绝不悄悄改键。
        热键回调经 ``_incoming`` 队列转到 Tk 主线程执行。
        """
        from .region_overlay import REGION_HOTKEY_LABEL, register_region_hotkey

        # F33：注册结果全量落 panel 日志（红字只是 UI，运维要判读码）。
        try:
            print(f"[hotkey] register attempt: {REGION_HOTKEY_LABEL}",
                  file=sys.stderr)
        except Exception:
            pass
        result = register_region_hotkey(
            self._hotkey_fired, root=self._root, platform=sys.platform,
        )
        try:
            print(f"[hotkey] result ok={result.get('ok')} "
                  f"errorCode={result.get('errorCode')} "
                  f"error={result.get('error')!r}", file=sys.stderr)
        except Exception:
            pass
        if result.get("ok"):
            text = f"断言入口：{result.get('label')}（随时按下呼出断言选区）"
            if result.get("annotation"):
                text = f"断言入口：{result.get('label')} —— {result['annotation']}"
            self._hotkey_label.configure(text=text, foreground="#555")
        else:
            # F6：热键被占最常见原因是重启服务后残留的孤儿面板仍持有注册
            # （旧面板还活着 → 它的管道已断，按键写进去静默无响应）。
            self._hotkey_label.configure(
                text=(f"断言入口不可用：{result.get('error')}"
                      "——若刚重启过服务，请先清理残留的旧面板进程再重试"),
                foreground="#c62828")

    def _hotkey_fired(self) -> None:
        """F34：handler 入口打点（OS 收到 WM_HOTKEY → on_activate 链）。"""
        try:
            print(f"[hotkey] fired thread={threading.get_ident()} "
                  f"queue={self._incoming.qsize()}",
                  file=sys.stderr, flush=True)
        except Exception:
            pass
        self._incoming.put({"regionHotkey": True})

    def _on_region_hotkey(self) -> None:  # pragma: no cover - live desktop
        # F34：Tk 线程收到打点（fired 之后零行=marshal/_poll 断链）
        try:
            print(f"[hotkey] render-fired thread={threading.get_ident()} "
                  f"overlay_open={self._region_overlay is not None}",
                  file=sys.stderr, flush=True)
        except Exception:
            pass
        if self._region_overlay is not None:
            return   # 遮罩已开：重复热键不再叠加
        self._request("region_assertion")

    def _apply_uipi_allow(self, overlay: Any) -> None:
        """F13 修复候选：ChangeWindowMessageFilterEx 放行低 IL 注入按钮。

        UIPI 假说：服务子进程（高 IL）遮罩收到中 IL 注入器的移动但按钮被
        静默丢弃（历史观察"点击落 fixture"=低 IL 表面收得到）。对 overlay
        内窗+wrapper 放行按钮消息即直接解；逐消息结果写 valve 日志。
        REGION_UIPI_ALLOW=0 关闭对照。"""
        if sys.platform != "win32":
            return
        try:
            from .region_overlay import (apply_uipi_message_allow,
                                         uipi_allow_enabled)
            if not uipi_allow_enabled():
                valve = self._region_valve
                if valve is not None:
                    valve.log_diagnostic("uipi_allow disabled by env")
                return
            targets = []
            try:
                inner = int(overlay.winfo_id())
                targets.append(inner)
                import ctypes
                targets.append(int(ctypes.windll.user32.GetParent(inner)))
            except Exception:
                pass
            for widget in (getattr(self, '_region_canvas', None),):
                try:
                    if widget is not None:
                        targets.append(int(widget.winfo_id()))
                except Exception:
                    pass
            valve = self._region_valve
            for hwnd in targets:
                if not hwnd:
                    continue
                results = apply_uipi_message_allow(hwnd)
                if valve is not None:
                    valve.log_diagnostic(f"uipi_allow hwnd={hwnd} {results}")
        except Exception as exc:
            valve = self._region_valve
            if valve is not None:
                valve.log_diagnostic(f"uipi_allow_failed {exc!r}")

    def _install_bindall_diag(self, overlay: Any) -> None:
        """F17 取证通道（REGION_BINDALL_DIAG=1，默认关）：Tk 事件翻译层判决。

        目标层已收窄：注入按钮进系统层（GetAsyncKeyState 实证）、消息层投递
        健康（修正 marshal 后 DOWN/UP 双达画布 WndProc）、但 Tk 绑定不触发且
        Tk 指针缓存不更新。判决矩阵（bind_all 是全 widget 兜底 tag）：
        - bind_all 见 ButtonPress 而 canvas bind 不触发 → canvas 级绑定
          （bindtag/路径解析）问题；
        - bind_all 也不见 → Tk 窗口映射/指针状态问题（HWND→TkWindow 查找
          失败静默丢弃 / pointerWindow 未更新）。
        通道：bind_all press/release/motion 每事件写 valve 日志（含 serial/
        widget/坐标）；canvas press/release 处理器同写（serial 可对账）；
        每秒轮询补 winfo containing（Tk 自己的"根坐标→窗口"解析结果）与
        几何/DPI 全量快照（1696x1008 vs 1680x988 失配定位）。"""
        if sys.platform != "win32" or not bindall_diag_enabled():
            return
        valve = self._region_valve
        root = self._root
        bind_all = getattr(root, "bind_all", None)
        if not callable(bind_all):
            return
        self._bindall_diag_bindings: list[tuple[str, Any]] = []

        def make(kind: str):
            def handler(event: Any) -> None:
                try:
                    if valve is not None:
                        valve.log_diagnostic(
                            f"bindall {kind} serial={getattr(event, 'serial', '?')}"
                            f" widget={getattr(event, 'widget', None)!r}"
                            f" x={getattr(event, 'x', '?')}"
                            f",{getattr(event, 'y', '?')}"
                            f" root={getattr(event, 'x_root', '?')}"
                            f",{getattr(event, 'y_root', '?')}")
                except Exception:
                    pass
            return handler

        for sequence, kind in (("<ButtonPress-1>", "press"),
                               ("<ButtonRelease-1>", "release"),
                               ("<Motion>", "motion")):
            try:
                func_id = bind_all(sequence, make(kind), add="+")
                self._bindall_diag_bindings.append((sequence, func_id))
            except Exception:
                pass
        try:
            overlay.bind("<Destroy>",
                         lambda _e: self._unbind_bindall_diag(), add="+")
        except Exception:
            pass
        if valve is not None:
            valve.log_diagnostic("bindall_diag installed")

    def _unbind_bindall_diag(self) -> None:
        unbind_all = getattr(self._root, "unbind_all", None)
        for sequence, func_id in getattr(self, "_bindall_diag_bindings", []):
            try:
                unbind_all(sequence, func_id)
            except Exception:
                pass
        self._bindall_diag_bindings = []

    def _install_input_diag(self, overlay: Any) -> None:
        """F14 双层输入观测器（REGION_INPUT_DIAG=1，默认关）。

        LL 钩子（本线程有 Tk 消息泵）只读记录每个鼠标事件（含
        LLMHF_INJECTED 标志与驱动 extra 标记）；WM_INPUT 裸输入汇经
        WndProc 探针的 _record_rawinput 观测 raw 层按钮位。两者对照
        定位按钮被吞的层（钩子链上 / win32k 级 / 内核过滤驱动）。
        关闭遮罩时卸载。"""
        if sys.platform != "win32":
            return
        try:
            from .region_overlay import (MouseHookDiagProbe,
                                         btn_from_ll_enabled,
                                         input_diag_enabled,
                                         register_rawinput_sink)
            ll_forward = btn_from_ll_enabled()
            if not input_diag_enabled() and not ll_forward:
                return
            if self._region_hook_probe is None:
                # F39：LL 转发通道——DOWN/UP 入 _incoming（线程安全），
                # Tk 线程排空合成（静止点击盲区修复）。
                sink = (lambda kind, x, y, _self=self:
                        _self._incoming.put({"llButton": {"kind": kind,
                                                          "x": x, "y": y}})
                        ) if ll_forward else None
                self._region_hook_probe = MouseHookDiagProbe(
                    forward_sink=sink)
                if ll_forward and self._region_hook_probe.installed:
                    self._panel_log_note("ll-forward installed")
            valve = self._region_valve
            status = register_rawinput_sink(int(overlay.winfo_id()))
            if valve is not None:
                valve.log_diagnostic(
                    f"input_diag installed={self._region_hook_probe.installed}"
                    f" rawsink={status}")
        except Exception as exc:
            valve = self._region_valve
            if valve is not None:
                valve.log_diagnostic(f"input_diag_install_failed {exc!r}")

    def _uninstall_input_diag(self) -> None:
        try:
            if self._region_hook_probe is not None:
                self._region_hook_probe.uninstall()
                valve = self._region_valve
                if valve is not None:
                    valve.log_diagnostic(
                        f"input_diag_final {self._region_hook_probe.summary()}")
        except Exception:
            pass
        self._region_hook_probe = None

    @staticmethod
    def _collect_interactive_widgets(root: Any) -> list[Any]:
        """F37：遮罩下全部交互控件（确认/取消/整容器按钮、输入框等）。

        判决链补充：翻译丢弃作用于遮罩内**每个** hwnd——FROM_MOTION/
        forward 钩子只挂 canvas 时，canvas 外控件输入路径仍死。探针须
        挂所有交互控件 hwnd，合成事件按 WM_MOUSEMOVE 实际落点 hwnd
        分发（光标在按钮上→按钮探针→合成到按钮→ttk press+release 对
        触发 command）。防双触发天然成立：Windows 消息按命中窗路由，
        同一时刻消息只落一个控件 hwnd。"""
        seen: set[int] = set()
        widgets: list[Any] = []
        interactive = {"TButton", "TEntry", "TCombobox", "TCheckbutton",
                       "TRadiobutton", "TScale", "TSpinbox", "Canvas",
                       "Listbox", "Button", "Entry"}
        stack = [root]
        while stack:
            node = stack.pop()
            try:
                children = node.winfo_children()
                cls = node.winfo_class()
            except Exception:
                continue
            if cls in interactive:
                try:
                    key = node.winfo_id()
                except Exception:
                    key = id(node)
                if key not in seen:
                    seen.add(key)
                    widgets.append(node)
            stack.extend(children)
        return widgets

    def _install_wndproc_probe(self, overlay: Any) -> None:
        """F12 最后一刀：面板内 WndProc 只读探针（REGION_WNDPROC_DIAG=1）。

        判决表：LBUTTONDOWN 到 WndProc 而 Tk press 处理器零运行 → 死在
        Tk 消息→事件翻译层；WndProc 都没收到 → OS 投递层（对照
        WM_NCHITTEST 命中窗 / WM_MOUSEACTIVATE）。计数由每秒 after 轮询
        写 valve 日志，回调内零 I/O。"""
        self._region_wndproc_probes = []
        if sys.platform != "win32":
            return
        try:
            from .region_overlay import WndProcReadonlyProbe
            from .region_overlay import (btn_forward_enabled,
                                         btn_from_motion_enabled)
            extra = [w for w in (getattr(self, '_region_canvas', None),) if w is not None]
            # F37：多控件补偿——确认/取消等控件 hwnd 各自挂探针，
            # 合成事件按消息实际落点 hwnd 分发（canvas 外输入路径复活）。
            for widget in self._collect_interactive_widgets(overlay):
                if all(widget.winfo_id() != w.winfo_id() for w in extra):
                    extra.append(widget)
            probes = WndProcReadonlyProbe.attach(
                overlay, extra_widgets=extra,
                forward=btn_forward_enabled(),          # F29：按钮消息转发
                motion_infer=btn_from_motion_enabled())  # F32：motion 流推断
            self._region_wndproc_probes = probes
            self._region_probe_widgets = list(extra)   # F41：对账/重装用
            try:
                valve0 = self._region_valve
                if valve0 is not None:
                    valve0.log_diagnostic("wndproc_map " + ", ".join(
                        f"{p.label}=hwnd{p.hwnd}" for p in probes))
            except Exception:
                pass
            valve = self._region_valve
            if valve is not None:
                valve.log_diagnostic(
                    "wndproc_installed "
                    + (", ".join(f"{p.label}:{p.hwnd}" for p in probes)
                       or "none"))
            if probes and callable(getattr(self._root, "after", None)):
                self._root.after(1000, self._flush_wndproc_diag)
        except Exception as exc:
            self._region_wndproc_probes = []
            valve = self._region_valve
            if valve is not None:
                valve.log_diagnostic(f"wndproc_install_failed {exc!r}")

    def _flush_wndproc_diag(self) -> None:
        """每秒把 WndProc 计数写 valve 日志；遮罩关闭自停。"""
        valve = self._region_valve
        for probe in self._region_wndproc_probes:
            try:
                if valve is not None:
                    valve.log_diagnostic(f"wndproc {probe.summary()}")
            except Exception as exc:
                if valve is not None:
                    valve.log_diagnostic(f"wndproc_flush_err {exc!r}")
        hook_probe = self._region_hook_probe
        if hook_probe is not None:
            try:
                if valve is not None:
                    valve.log_diagnostic(f"input_diag {hook_probe.summary()}")
            except Exception as exc:
                if valve is not None:
                    valve.log_diagnostic(f"input_diag_flush_err {exc!r}")
        if valve is not None:
            try:
                valve.log_diagnostic(
                    "wndproc_flush_heartbeat probes="
                    + str(len(self._region_wndproc_probes))
                    + " overlay="
                    + str(self._region_overlay is not None))
            except Exception:
                pass
        if (self._region_overlay is not None and self._region_wndproc_probes
                and callable(getattr(self._root, "after", None))):
            self._root.after(1000, self._flush_wndproc_diag)

    def _restore_wndproc_probes(self) -> None:
        """遮罩关闭恢复原窗口过程。"""
        leftovers = []
        for probe in self._region_wndproc_probes:
            try:
                probe.restore()
            except Exception as exc:
                # F64（审查测试钉死）：restore 失败必须留痕且保留回调
                # 生命周期（回调仍被 OS 引用，吞异常清列表=悬空调用源）。
                leftovers.append((probe, exc))
        if leftovers:
            # 留痕（surfaced），清单仍清空不留悬挂引用（审查契约）；回调
            # 生命周期由 retired_callbacks 强引用保活（OS 只存函数
            # 指针不持 Python callback——审查实锤更正），失败细节入面板日志。
            try:
                self._panel_log_note(
                    "wndproc-restore-failed probes=" + repr(
                        [(getattr(p, 'hwnd', '?'), f'{type(e).__name__}: {e}')
                         for p, e in leftovers]))
            except Exception:
                pass
        self._region_wndproc_probes = []

    def _diag_canvas_event(self, kind: str, event: Any) -> None:
        """F17：canvas 处理器到达记录（bindall diag 同通道，serial 对账）。"""
        if not bindall_diag_enabled():
            return
        valve = self._region_valve
        if valve is None:
            return
        try:
            valve.log_diagnostic(
                f"{kind} serial={getattr(event, 'serial', '?')}"
                f" widget={getattr(event, 'widget', None)!r}"
                f" x={getattr(event, 'x', '?')},{getattr(event, 'y', '?')}")
        except Exception:
            pass

    def _synth_press_probe(self) -> None:
        """F27：合成 press/motion 对照（REGION_OVERLAY_SYNTH=1，默认关）。

        event_generate 绕过 OS 消息翻译直入 Tk 事件队列；自含一次性
        bind_all 探针（不依赖 REGION_BINDALL_DIAG）记录 seen/missed，
        1 秒后 settle 出 synth_verdict 并解除探针。motion 为机制自检：
        已知活——若 press MISSED 而 motion seen，分叉确凿指向按钮特有
        路径（bindtags/注册时序或 OS→Tk 翻译段，见门 docstring 判决）。"""
        overlay = self._region_overlay
        canvas = self._region_canvas
        valve = self._region_valve
        root = self._root
        if overlay is None or canvas is None or root is None:
            return
        try:
            self._synth_press_probe_inner(overlay, canvas, valve, root)
        except Exception:
            try:
                if valve is not None:
                    valve.log_diagnostic("synth_probe_err (swallowed)")
            except Exception:
                pass   # F28：钩子异常绝不外溢

    def _synth_press_probe_inner(self, overlay, canvas, valve, root) -> None:
        seen = {"press": False, "motion": False}

        def recorder(kind: str):
            def handler(event: Any) -> None:
                seen[kind] = True
                if valve is not None:
                    valve.log_diagnostic(
                        f"synth_{kind}_seen serial={getattr(event, 'serial', '?')}"
                        f" x={getattr(event, 'x', '?')},{getattr(event, 'y', '?')}")
            return handler

        bind_all = getattr(root, "bind_all", None)
        probe_ids: list[tuple[str, Any]] = []
        if callable(bind_all):
            for sequence, kind in (("<ButtonPress-1>", "press"),
                                   ("<Motion>", "motion")):
                try:
                    probe_ids.append((sequence, bind_all(sequence, recorder(kind), add="+")))
                except Exception:
                    pass
        if valve is not None:
            valve.log_diagnostic("synth_press_sent <Button-1> x=100 y=100")
        try:
            canvas.event_generate("<Motion>", x=100, y=100)
            canvas.event_generate("<Button-1>", x=100, y=100)
            canvas.event_generate("<ButtonRelease-1>", x=100, y=100)
        except Exception as exc:
            if valve is not None:
                valve.log_diagnostic(f"synth_send_err {exc!r}")

        def settle() -> None:
            try:
                if valve is not None:
                    valve.log_diagnostic(
                        "synth_verdict "
                        f"press={'seen' if seen['press'] else 'MISSED'}"
                        f" motion={'seen' if seen['motion'] else 'MISSED'}")
                unbind_all = getattr(root, "unbind_all", None)
                if callable(unbind_all):
                    for sequence, func_id in probe_ids:
                        try:
                            unbind_all(sequence, func_id)
                        except Exception:
                            pass
            except Exception:
                pass

        try:
            overlay.after(1000, settle)
        except Exception:
            settle()

    def _ll_watchdog_tick(self, probe: Any) -> None:
        """F41：LL 钩子停滞自愈——Windows 对 LL 钩子超时静默摘除（回调
        超时/主线程卡顿>LowLevelHooksTimeout），不通知进程。判据：连续
        两次 5 拍 flush 间 counts 总和零变化 → 视为被摘 → 卸载重装
        （节流：两次重装间隔 ≥30s，防风暴）。"""
        total = sum(probe.counts.values())
        last_total, last_ticks = getattr(self, "_ll_last_total", None), \
            getattr(self, "_ll_stale_ticks", 0)
        if last_total is not None and total == last_total:
            self._ll_stale_ticks = last_ticks + 1
        else:
            self._ll_stale_ticks = 0
        self._ll_last_total = total
        if self._ll_stale_ticks < 2:
            return
        now = time.monotonic()
        if now - getattr(self, "_ll_last_reinstall", 0.0) < 30.0:
            return
        self._ll_last_reinstall = now
        self._ll_stale_ticks = 0
        try:
            from .region_overlay import MouseHookDiagProbe, btn_from_ll_enabled
            if not btn_from_ll_enabled():
                return
            self._panel_log_note("ll-hook-reinstall (stale counts)")
            sink = (lambda kind, x, y, _self=self:
                    _self._incoming.put({"llButton": {"kind": kind,
                                                      "x": x, "y": y}}))
            probe.uninstall()
            # F52g（独立审查实锤风险）：退休探针进全局保活列表——Unhook
            # 返回不代表 OS 钩子队列中已排队的回调排空，旧 _proc trampoline
            # 被 GC 回收后任何在途调用=ucrtbase fail_fast(c0000409 同偏移
            # 特征)。保活至进程结束（对象数有限，无泄漏实质）。
            _RETIRED_HOOK_PROBES.append(probe)
            fresh = MouseHookDiagProbe(forward_sink=sink)
            self._region_hook_probe = fresh
            valve = self._region_valve
            if valve is not None:
                valve.log_diagnostic(
                    f"llhook reinstalled={fresh.installed} err={fresh.error}")
        except Exception:
            pass

    def _verify_wndproc_probes(self) -> None:
        """F41：探针 hwnd 与活控件对账——脱节（resync 重建/控件换 hwnd）
        即整体重装。零消息探针+活 canvas 的实机症状（w1/w2/w3 零计数）
        直指 hwnd 脱节；此校验在每秒 poll 中自愈。"""
        probes = self._region_wndproc_probes
        widgets = getattr(self, "_region_probe_widgets", None)
        if not probes or not widgets or self._region_overlay is None:
            return
        try:
            current_ids = {int(w.winfo_id()) for w in widgets}
            installed_ids = {int(p.hwnd) for p in probes}
            inner = int(self._region_overlay.winfo_id())
        except Exception:
            return
        if current_ids <= installed_ids | {inner}:
            return   # 全部一致
        try:
            self._panel_log_note(
                f"probe-hwnd-stale installed={sorted(installed_ids)} "
                f"current={sorted(current_ids)}")
            self._install_wndproc_probe(self._region_overlay)
        except Exception:
            pass

    def _sync_overlay_geometry(self, note: str) -> bool:
        """F18：Tk geometry 与期望全屏同步（几何坏死根因的修复主体）。

        任何来源的原生缩放（valve SetWindowPos 等）绕过 Tk 模型后，在此
        用 overlay.geometry(f"{w}x{h}+0+0") 重新声明并 update_idletasks，
        使 Tk 指针跟踪/containing/绑定派发恢复。期望尺寸=winfo_screen
        width/height（DPI 感知开启时=物理像素，与驱动坐标一致）。失步时
        valve 日志 geometry_resync（首查+每 10 拍），避免刷屏。"""
        overlay = self._region_overlay
        root = self._root
        if overlay is None or root is None:
            return False
        try:
            expected_w = int(root.winfo_screenwidth())
            expected_h = int(root.winfo_screenheight())
            width = int(overlay.winfo_width())
            height = int(overlay.winfo_height())
            from .region_overlay import overlay_geometry_desynced
            if not overlay_geometry_desynced(width, height,
                                             expected_w, expected_h):
                self._geometry_sync_state = "ok"
                return False
            overlay.geometry(f"{expected_w}x{expected_h}+0+0")
            try:
                overlay.update_idletasks()
            except Exception:
                pass
            now_width = int(overlay.winfo_width())
            now_height = int(overlay.winfo_height())
            healed = not overlay_geometry_desynced(now_width, now_height,
                                                   expected_w, expected_h)
            self._geometry_sync_state = ("healed" if healed else "stuck")
            valve = self._region_valve
            if valve is not None:
                self._geometry_sync_logs = getattr(
                    self, "_geometry_sync_logs", 0) + 1
                if self._geometry_sync_logs == 1 or \
                        self._geometry_sync_logs % 10 == 0:
                    valve.log_diagnostic(
                        f"geometry_resync note={note}"
                        f" was={width}x{height} want={expected_w}x{expected_h}"
                        f" now={now_width}x{now_height} healed={healed}")
            return healed
        except Exception:
            return False

    def _bindall_diag_poll(self) -> None:
        """F17：每秒补 winfo containing + 几何/DPI 快照（跟随 valve 轮询）。"""
        if not bindall_diag_enabled() or self._region_overlay is None:
            return
        valve = self._region_valve
        if valve is None:
            return
        try:
            containing = None
            ptr = self._region_canvas.winfo_pointerxy() \
                if self._region_canvas is not None else None
            if ptr is not None:
                try:
                    containing = str(self._root.tk.call(
                        "winfo", "containing", int(ptr[0]), int(ptr[1])))
                except Exception as exc:
                    containing = f"err:{exc!r}"
            overlay = self._region_overlay
            geo = (f"screen={self._root.winfo_screenwidth()}"
                   f"x{self._root.winfo_screenheight()}"
                   f" overlay={overlay.winfo_width()}x{overlay.winfo_height()}"
                   f"+{overlay.winfo_rootx()}+{overlay.winfo_rooty()}")
            dpi = ""
            if sys.platform == "win32":
                try:
                    import ctypes
                    dpi = (f" dpi_sys={ctypes.windll.user32.GetDpiForSystem()}"
                           f" tk_scaling={self._root.tk.call('tk', 'scaling')}")
                except Exception:
                    pass
            valve.log_diagnostic(
                f"bindall poll ptr={ptr} containing={containing} {geo}{dpi}")
        except Exception:
            pass

    def _hide_root_for_region(self) -> None:
        """隐藏录制面板（F10）：默认不再 withdraw——V6 假说「withdraw 使
        Tk 指针窗口解析楔死（缺陷①）」的修正版。改为移出屏幕 + 置底 +
        全透明（三重保险防 Windows 把越界窗口拉回可视区），视觉等价且
        root 保持 mapped，指针解析始终有正常目标。REGION_ROOT_HIDE_MODE
        =withdraw 回到旧行为（对照用）。关闭遮罩时 _restore_root_after_
        region 恢复原 geometry/state。"""
        root = self._root
        self._region_root_saved = None
        # F11：判别运行保持 withdraw 默认（与 F5 原始条件完全一致，单变量
        # 原则）；offscreen 修正候选由 REGION_ROOT_HIDE_MODE=offscreen 按需
        # 验证（V6 p6 虽证伪了复刻体里的 withdraw 假说，真实面板未定）。
        mode = os.environ.get("REGION_ROOT_HIDE_MODE", "withdraw")
        try:
            if mode == "withdraw":
                root.withdraw()
                return
            saved_state = str(root.state()) if hasattr(root, "state") else ""
            self._region_root_saved = (int(root.winfo_x()), int(root.winfo_y()),
                                       int(root.winfo_width()),
                                       int(root.winfo_height()), saved_state)
            screen_w = max(1, int(root.winfo_screenwidth()))
            screen_h = max(1, int(root.winfo_screenheight()))
            root.geometry(f"+{screen_w + 40}+{screen_h + 40}")
            try:
                root.attributes("-alpha", 0.0)
            except Exception:
                pass
            try:
                root.lower()
            except Exception:
                pass
        except Exception:
            self._region_root_saved = None
            try:
                root.withdraw()   # 任何失败回退旧行为
            except Exception:
                pass

    def _restore_root_after_region(self) -> None:
        """恢复 _hide_root_for_region 保存的 geometry/state；withdraw
        模式或无保存记录时不动（deiconify 由调用方负责）。"""
        root = self._root
        if root is None:
            return
        saved = getattr(self, "_region_root_saved", None)
        if not saved:
            return
        x, y, w, h, state_name = saved
        try:
            if str(state_name) == "zoomed":
                root.state("zoomed")
            else:
                root.geometry(f"{max(1, w)}x{max(1, h)}+{x}+{y}")
            try:
                root.attributes("-alpha", 1.0)
            except Exception:
                pass
        except Exception:
            pass
        self._region_root_saved = None

    def _make_desktop_reattached_key_state(self):
        """F48：每拍 OpenInputDesktop+SetThreadDesktop 重附着当前输入桌
        面后再读 GetAsyncKeyState。RDP 会话重连/切换后 input desktop
        重建，spawn 时附着的旧桌面=陈旧→键态读取恒空（与光标查询冻结
        同族：输入态查询的会话/桌面陈旧性）。失败回退旧路径+打点（状态
        变化节流）。非 win32 → None（valve 走默认）。"""
        try:
            import ctypes
            user32 = ctypes.windll.user32
        except Exception:
            return None
        DESKTOP_READOBJECTS = 0x0001
        state = {"handle": None, "last_ok": None}

        def read(vk: int) -> bool:
            hdesk = None
            try:
                hdesk = user32.OpenInputDesktop(0, False,
                                                DESKTOP_READOBJECTS)
            except Exception:
                hdesk = None
            ok = False
            if hdesk:
                ok = bool(user32.SetThreadDesktop(hdesk))
                if ok:
                    old = state["handle"]
                    if old:
                        try:
                            user32.CloseDesktop(old)
                        except Exception:
                            pass
                    state["handle"] = hdesk
                else:
                    try:
                        user32.CloseDesktop(hdesk)
                    except Exception:
                        pass
            if state["last_ok"] != ok:   # 变化才打点（防刷屏）
                state["last_ok"] = ok
                try:
                    self._panel_log_note(
                        f"input-desktop-reattach ok={ok}")
                except Exception:
                    pass
            try:
                return bool(user32.GetAsyncKeyState(vk) & 0x8000)
            except Exception:
                return False

        return read

    def _show_region_overlay(self, region: Mapping[str, Any]) -> None:
        """半透明可取消选区层（管理输入：不穿透、不入业务事件）。

        主面板收起；全屏遮罩置顶半透明，拖出矩形后回传 region_update，
        高亮回执画出可选组件，Esc/取消按钮回传 region_cancel。
        """
        if self._region_overlay is not None:
            self._apply_region_payload(region)
            return
        if self._root is None:
            return
        self._close_region_wizard(restore=False)   # 重新框选时先关旧向导
        self._hide_root_for_region()   # 隐藏录制面板（06 §2）
        overlay = self._tk.Toplevel(self._root)
        self._region_overlay = overlay
        overlay.title(f"断言选区 {RECORDER_UI_MARKER}")
        overlay.attributes("-topmost", True)
        try:
            overlay.attributes("-alpha", 0.25)
        except Exception:
            pass
        width = max(1, int(self._root.winfo_screenwidth()))
        height = max(1, int(self._root.winfo_screenheight()))
        overlay.geometry(f"{width}x{height}+0+0")
        # 操作条：确认所选（进向导）/ 选择整个容器 / 取消（06 §2）
        bar = self._ttk.Frame(overlay)
        bar.pack(side="bottom", fill="x")
        self._region_confirm_button = self._ttk.Button(
            bar, text="确认所选（进入断言向导）",
            command=self._on_confirm_region_clicked)
        self._region_confirm_button.pack(side="left", padx=8, pady=6)
        self._ttk.Button(bar, text="选择整个容器",
                         command=lambda: (self._note_region_input(),
                                          self._request("region_toggle",
                                                        container=True))
                         ).pack(side="left", padx=8, pady=6)
        self._ttk.Button(bar, text="取消（Esc）",
                         command=self._request_region_cancel
                         ).pack(side="right", padx=8, pady=6)
        canvas = self._tk.Canvas(overlay, bg="#37474f", highlightthickness=0)
        canvas.pack(fill="both", expand=True)
        self._region_canvas = canvas
        self._region_status_label = canvas.create_text(
            12, 12, anchor="nw", fill="white", width=width - 24, font=("TkDefaultFont", 12),
            text="拖出矩形选择区域；松开后点击高亮组件增选/取消；Esc 取消。"
                 "选区是管理操作，不会写入业务动作。",
        )
        # F42 键盘通道：WM_KEYDOWN 翻译路径与鼠标独立（本机鼠标按钮消息
        # 栈深层异常未知，键盘热键注入全夜可用）——Return 直达确认 invoke、
        # Space/Tab 走 ttk 默认焦点链、Esc 已有兜底。存 _region_diag_bindings
        # 随遮罩关闭统一解除（复用 F5 机制）。活性打点供实机判读。
        def _on_return(_event: Any) -> str:
            self._panel_log_note("key-return fired (keyboard channel)")
            try:
                self._region_confirm_button.invoke()
            except Exception:
                pass
            return "break"

        try:
            self._region_confirm_button.focus_set()   # Tab/Space 语义起点
            _bind_all = getattr(self._root, "bind_all", None)
            if callable(_bind_all):
                _return_fid = _bind_all("<Return>", _on_return)
                # bind_all 返回 funcid——与诊断绑定同构，随遮罩统一解除
                self._region_diag_bindings.append(("<Return>", _return_fid))
        except Exception:
            pass
        canvas.bind("<ButtonPress-1>", self._on_region_press_tracked)
        canvas.bind("<B1-Motion>", self._on_region_drag)
        canvas.bind("<ButtonRelease-1>", self._on_region_release_tracked)
        overlay.bind("<Escape>", lambda _event: self._request_region_cancel())
        overlay.protocol("WM_DELETE_WINDOW", self._request_region_cancel)
        # 全局 Esc 兜底：遮罩未持有键盘焦点时（输入死亡场景里点击永远到不了
        # 遮罩 → 它永远拿不到焦点），Esc 落到前台窗口而非遮罩。bind_all 在
        # 遮罩存续期间接管 Esc，关闭时解除。F5 实测的「Esc 无反应」一半是这个。
        try:
            self._root.bind_all("<Escape>",
                                lambda _event: self._request_region_cancel())
        except Exception:
            pass
        try:
            overlay.focus_set()   # 尽力把键盘焦点拿到遮罩上
        except Exception:
            pass
        # 输入死亡安全阀（11.26 实机）：任何输入证据都刷新 last_input；
        # Tcl 心跳 + 指针采样判定输入是否到达；看门狗线程兜底硬退出。
        from .region_overlay import (RegionInputValve,
                                     acquire_panel_instance_mutex,
                                     valve_gate_enabled)

        from .region_overlay import region_input_grace_seconds
        valve = RegionInputValve(
            grace_seconds=region_input_grace_seconds(),   # F23：env 可覆盖宽限
            release_clip_enabled=valve_gate_enabled(),   # F23：门 0 旁路主动释放
            key_state_fn=self._make_desktop_reattached_key_state())  # F48
        valve.on_overlay_open()
        self._region_valve = valve
        canvas.bind("<Motion>", self._note_region_input)
        overlay.bind("<Motion>", self._note_region_input)
        if self._arm_valve_watchdog:
            self._start_region_valve_watchdog()
        self._region_watchdog_poll()
        self._bindall_diag_poll()
        # F18 根因修复：原生 SetWindowPos 全屏绕过 Tk 几何模型（实机铁证
        # overlay=1x1+0+0 → containing 空/绑定派发全灭）。打开后两个槽位
        # 同步，每秒 valve 轮询自愈兜底。
        schedule = getattr(self._root, "after", None)
        if callable(schedule):
            schedule(80, lambda: self._sync_overlay_geometry("open80"))
            schedule(500, lambda: self._sync_overlay_geometry("open500"))
            try:
                if overlay_synth_enabled():   # F27 判别性自测（默认关）
                    schedule(2000, self._synth_press_probe)
            except Exception:
                pass   # F28：钩子任何异常不得阻断开遮罩主流程
        self._apply_region_payload(region)
        # F7 回归修正（F8）：两层全部 env 门控且互不干扰——
        # REGION_ROUTER_DIAG=0 关 bind_all 诊断/兜底（默认开，add="+" 追加式，
        # 处理器内部全兜底异常）；REGION_WARP_RECALC=1 才开 warp（默认关：
        # warp 把光标拉到遮罩中心，与注入驱动的 SetCursorPos 拖拽互抢，
        # 且 500ms 重复 warp 会在拖拽中途抢光标——F7 回归头号嫌疑）。
        from .region_overlay import (bindall_diag_enabled,
                                     router_diag_enabled, valve_gate_enabled,
                                     warp_recalc_enabled)
        if router_diag_enabled():
            self._install_region_pointer_diagnostics()
        if warp_recalc_enabled():
            self._force_pointer_recalc(overlay)
        if valve_gate_enabled():
            # F23 消融门（REGION_VALVE_GATE=0 旁路）：按钮消息路径上的
            # 邻近机器——subclass 探针改写 WndProc、MSGFLT_ALLOW 改消息
            # 过滤。valve 本体是纯被动观察者（源码核查：无按钮吞噬路径），
            # watchdog/日志保留。
            self._install_wndproc_probe(overlay)
            self._apply_uipi_allow(overlay)
        self._install_input_diag(overlay)
        self._install_bindall_diag(overlay)

    def _install_region_pointer_diagnostics(self) -> None:
        """bind_all 层 ButtonPress 到达性诊断 + 兜底派发（F5 取证/修复）。

        实机特征：canvas widget 级 `<Motion>` 洪泛到达而 `<ButtonPress-1>`
        处理器从不运行。三个假设一次分辨：
        - bind_all ButtonPress 也不到 → 按键消息根本没进 Tk 派发（注入
          路径/消息层问题，下一步做 WndProc 级探针）；
        - 到达且 event.widget 正确 → widget 绑定/tag 解析问题；
        - 到达但 event.widget 错误 → Tk 指针窗口解析错位（父 session 假设）。
        同时把 bind_all 作为兜底派发通道：widget 层没处理时由 x_root/y_root
        换算 canvas 坐标手工走 press/drag/release 逻辑（80ms 去重防双发）。
        """
        root = self._root
        bind_all = getattr(root, "bind_all", None)
        if not callable(bind_all):
            return
        valve = self._region_valve

        def make_handler(kind: str):
            def handler(event: Any) -> None:
                try:
                    detail = (
                        f"bind_all {kind} widget={getattr(event, 'widget', None)!r}"
                        f" x={getattr(event, 'x', None)},y={getattr(event, 'y', None)}"
                        f" root={getattr(event, 'x_root', None)}"
                        f",{getattr(event, 'y_root', None)}")
                    if valve is not None:
                        valve.log_diagnostic(detail)
                    now = time.monotonic()
                    last = self._region_widget_dispatch.get(kind)
                    if last is not None and now - last < 0.08:
                        return   # widget 层已处理同一事件，不重复派发
                    if kind == "press":
                        self._region_fallback_active = True
                    elif kind == "release":
                        self._region_fallback_active = False
                    self._dispatch_region_pointer(kind, event)
                except Exception:
                    pass   # 诊断/兜底绝不反噬事件派发
            return handler

        self._region_diag_bindings = []
        for sequence, kind in (("<ButtonPress-1>", "press"),
                               ("<ButtonRelease-1>", "release")):
            try:
                # add="+"：追加而非替换——绝不影响既有 all-tag 绑定。
                func_id = bind_all(sequence, make_handler(kind), add="+")
                self._region_diag_bindings.append((sequence, func_id))
            except Exception:
                pass

        def motion_handler(event: Any) -> None:
            try:
                if self._region_fallback_active:
                    self._dispatch_region_pointer("drag", event)
            except Exception:
                pass

        try:
            motion_func_id = bind_all("<Motion>", motion_handler, add="+")
            self._region_diag_bindings.append(("<Motion>", motion_func_id))
        except Exception:
            pass

    def _dispatch_region_pointer(self, kind: str, event: Any) -> None:
        """bind_all 兜底：把 root 坐标换算成 canvas 坐标走原生处理逻辑。"""
        canvas = self._region_canvas
        if canvas is None:
            return
        try:
            origin_x = canvas.winfo_rootx()
            origin_y = canvas.winfo_rooty()
        except Exception:
            return
        x_root = getattr(event, "x_root", None)
        y_root = getattr(event, "y_root", None)
        if x_root is None or y_root is None:
            return
        shim = SimpleNamespace(x=int(x_root) - int(origin_x),
                               y=int(y_root) - int(origin_y))
        handler = {"press": self._on_region_press,
                   "drag": self._on_region_drag,
                   "release": self._on_region_release}.get(kind)
        if handler is None:
            return
        try:
            handler(shim)
        except Exception:
            pass

    def _force_pointer_recalc(self, overlay: Any) -> None:
        """遮罩显示后强制 Tk 重算指针窗口（event generate Motion -warp）。

        F5 特征指向 Tk 的指针窗口缓存停在旧窗口上；warp 一次让
        TkUpdatePointer 按当前位置重算 Enter/Leave 与目标窗口。80ms 后
        执行（窗口已映射），500ms 再补一次；失败静默。
        """
        def warp() -> None:
            try:
                overlay.update_idletasks()
                width = max(1, int(overlay.winfo_width()))
                height = max(1, int(overlay.winfo_height()))
                overlay.event_generate("<Motion>", warp=True,
                                       x=width // 2, y=height // 2)
            except Exception:
                pass

        schedule = getattr(self._root, "after", None)
        if callable(schedule):
            schedule(80, warp)   # 只强刷一次；重复 warp 会在拖拽中途抢光标

    def _note_region_input(self, _event: Any = None) -> None:
        """安全阀输入证据：遮罩上任何输入事件都证明输入通路活着。"""
        valve = self._region_valve
        if valve is not None:
            valve.on_input_event()

    def _region_watchdog_poll(self) -> None:
        """安全阀心跳（Tk 线程，每秒）：心跳 + 指针采样 + 判死取消。"""
        self._sync_overlay_geometry("poll")   # F18：每秒几何自愈
        self._verify_wndproc_probes()         # F41：探针 hwnd 自愈
        valve = self._region_valve
        if valve is None or self._region_overlay is None or valve.closed:
            return
        valve.heartbeat()
        hook_probe = self._region_hook_probe
        if hook_probe is not None:
            self._ll_flush_i = getattr(self, "_ll_flush_i", 0) + 1
            if self._ll_flush_i % 5 == 0:   # F40：钩子死活判读（MOVE 涨=活）
                valve.log_diagnostic(
                    f"llhook {hook_probe.summary()}")   # summary 截 records
                # F47：探针 last_move 读数——断点直指（活/死/陈旧一目了然）
                for _p in (self._region_wndproc_probes or []):
                    _pt = getattr(_p, "last_move_screen", None)
                    _at = getattr(_p, "last_move_at", 0.0)
                    _age = (int((time.monotonic() - _at) * 1000)
                            if _at else -1)
                    valve.log_diagnostic(
                        f"wndproc {_p.label} last_move={_pt}@{_age}ms")
            self._ll_watchdog_tick(hook_probe)   # F41：停滞自动重装
        canvas = self._region_canvas
        pointer = None
        try:
            pointer = canvas.winfo_pointerxy() if canvas is not None else None
        except Exception:
            pointer = None
        # OS 级对照快照（win32）：原始 GetCursorPos / GetClipCursor / 前台
        # 窗口。F6 判据：Tk 查询静止而原始读数在动 → 查询侧被污染；两者
        # 都静止且 clip 退化 → 光标被裁剪矩形钉死（可自动释放）。
        probe = {}
        if sys.platform == "win32":
            from .region_overlay import win32_input_probe
            probe = win32_input_probe()
        probe["tk_thread"] = threading.get_ident()
        # F45：Tk winfo_pointerxy 查询侧污染实证（497 拍冻结于历史点击点，
        # 同期 w0 探针 WM_MOUSEMOVE=35 光标在动）——喂值改用 probe["cursor"]
        # （GetCursorPos 原始读，不经 Tk、每拍实时）为主，Tk 值降级为对照；
        # 两者都缺才喂 None（moved=False 路径）。
        # F46：位置源优先级——探针 WM_MOUSEMOVE 消息坐标（内核路由真值）
        # > GetCursorPos 原始读 > Tk 查询 > None。任务上下文光标查询全部
        # 失真（SetCursorPos 成功而 GetCursorPos 恒 (0,0)，原子实验实证）
        # ——消息流健康（w0 探针 MOVE 计数+坐标正确），故以探针为源。
        raw_cursor = probe.get("cursor")
        move_pt = None
        move_at = 0.0
        for _p in (self._region_wndproc_probes or []):
            _pt = getattr(_p, "last_move_screen", None)
            _at = getattr(_p, "last_move_at", 0.0)
            if isinstance(_pt, (tuple, list)) and len(_pt) == 2 and _at >= move_at:
                move_pt, move_at = (int(_pt[0]), int(_pt[1])), _at
        if move_pt is not None:
            px, py = move_pt
            try:
                probe["probeMove"] = {"pt": [px, py],
                                      "ageMs": int((time.monotonic()
                                                    - move_at) * 1000)}
            except Exception:
                pass
        elif isinstance(raw_cursor, (tuple, list)) and len(raw_cursor) == 2:
            px, py = int(raw_cursor[0]), int(raw_cursor[1])
        elif isinstance(pointer, tuple) and len(pointer) == 2:
            px, py = int(pointer[0]), int(pointer[1])
        else:
            px, py = None, None
        decision = valve.sample_pointer(px, py, probe=probe or None)
        if decision.get("returnEdge"):
            self._fire_return_key()   # F43：键盘态轮询→合成 Return
        if decision.get("action") == "cancel":
            self._region_valve_auto_cancel(str(decision.get("reason") or ""))
            return
        schedule = getattr(self._root, "after", None)
        if callable(schedule):
            schedule(1000, self._region_watchdog_poll)

    def _region_valve_auto_cancel(self, reason: str) -> None:
        """安全阀判定输入死亡：自动取消遮罩、恢复面板并留痕。"""
        if self._detail_label is not None:
            try:
                self._detail_label.configure(text=(
                    f"选区输入无响应（{reason}），已自动取消并恢复面板；"
                    "可用断言热键重新呼出"))
            except Exception:
                pass
        self._request_region_cancel()

    def _start_region_valve_watchdog(self) -> None:
        """看门狗线程（非 Tk）：Tcl 心跳失联 → 硬退出子进程解锁桌面。"""
        stop = threading.Event()
        self._region_watchdog_stop = stop
        valve = self._region_valve

        def run() -> None:  # pragma: no cover - live desktop
            while not stop.wait(1.0):
                try:
                    # F62（faulthandler 栈实锤）：confirm 关遮罩清 _region_valve
                    # 后，tick 闭包仍持旧 valve 调 hard_exit_due()→触碰已销毁
                    # Tk→Tcl abort=0xC0000409。closed 为纯属性读，先查后操作。
                    if stop.is_set():
                        break
                    if valve is None or getattr(valve, "closed", False) \
                            or valve.modal_active:
                        continue   # 模态框期间 Tcl 心跳本来就停，不许误杀
                    # F8 分裂探针：非 Tk 线程独立读原始光标/线程/桌面。与
                    # Tk 线程每秒的 sample 行对照——同进程内两个线程读数
                    # 一动一静 → 线程输入队列/桌面附着问题；两者同静止
                    # → 进程级桌面错位；两者都动而 Tk 事件仍死 → 回到
                    # Tk 派发层（WndProc 探针）。
                    if sys.platform == "win32":
                        from .region_overlay import win32_input_probe
                        probe = win32_input_probe()
                        valve.log_diagnostic(
                            "watchdog raw={} clip={} fg={!r} wt={} dt={}"
                            .format(probe.get("cursor"), probe.get("clip"),
                                    probe.get("foreground"),
                                    probe.get("win_thread"),
                                    probe.get("desktop")))
                    if valve.hard_exit_due():
                        valve.request_hard_exit()
                        return
                except Exception:
                    pass

        threading.Thread(target=run,
                         name="edr-wd-region-valve-watchdog",
                         daemon=True).start()

    def _on_region_press(self, event) -> None:
        self._panel_diag("region_press " + str((event.x, event.y)))  # pragma: no cover - live desktop
        self._diag_canvas_event("canvas_press", event)
        self._note_region_input()
        self._region_drag_start = (int(event.x), int(event.y))

    def _on_region_press_tracked(self, event) -> None:
        """widget 层派发时间戳（bind_all 去重用）；兜底派发不经过这里。"""
        self._region_widget_dispatch["press"] = time.monotonic()
        self._on_region_press(event)

    def _on_region_release_tracked(self, event) -> None:
        """widget 层派发时间戳（bind_all 去重用）；兜底派发不经过这里。"""
        self._region_widget_dispatch["release"] = time.monotonic()
        self._on_region_release(event)

    def _on_region_drag(self, event) -> None:  # pragma: no cover - live desktop
        self._note_region_input()
        canvas = self._region_canvas
        start = self._region_drag_start
        if canvas is None or start is None:
            return
        canvas.delete("rubber")
        canvas.create_rectangle(
            start[0], start[1], int(event.x), int(event.y),
            outline="#ffeb3b", width=2, tags="rubber")

    def _on_region_release(self, event) -> None:  # pragma: no cover - live desktop
        self._panel_diag("region_release " + str((event.x, event.y)))
        self._diag_canvas_event("canvas_release", event)
        """松开：位移极小视为点击（增选/取消命中组件），否则提交框选矩形。"""
        from .region_overlay import DRAG_CLICK_TOLERANCE_PX

        start = self._region_drag_start
        self._region_drag_start = None
        if start is None:
            return
        dx, dy = abs(int(event.x) - start[0]), abs(int(event.y) - start[1])
        if dx <= DRAG_CLICK_TOLERANCE_PX and dy <= DRAG_CLICK_TOLERANCE_PX:
            canvas = self._region_canvas
            if canvas is None:
                return
            found = canvas.find_closest(int(event.x), int(event.y))
            if found:
                tags = canvas.gettags(found[0])
                keys = [t for t in tags if t.startswith("comp:")]
                if keys:
                    self._request("region_toggle", key=keys[0].split(":", 1)[1])
            return
        left, top = min(start[0], int(event.x)), min(start[1], int(event.y))
        right, bottom = max(start[0], int(event.x)), max(start[1], int(event.y))
        self._request("region_update", rect=[left, top, right, bottom])

    def _request_region_cancel(self) -> None:  # pragma: no cover - live desktop
        """Esc/关闭：先记下 requestId，回执到达（无论成败）都关遮罩并恢复面板。"""
        self._note_region_input()
        request_id = uuid.uuid4().hex
        self._region_cancel_request_id = request_id
        self.on_request("region_cancel", request_id, {})

    def _apply_region_payload(self, region: Mapping[str, Any]) -> None:
        """把 region_* 回执负载画到遮罩上（高亮/所选清单/状态）。"""
        canvas = self._region_canvas
        if canvas is None or not isinstance(region, Mapping):
            return
        canvas.delete("highlight")
        for highlight in region.get("highlights") or []:
            rect = highlight.get("rect") or []
            if len(rect) != 4:
                continue
            color = "#00e676" if highlight.get("selected") else "#40c4ff"
            canvas.create_rectangle(
                rect[0], rect[1], rect[2], rect[3], outline=color, width=2,
                tags=(f"comp:{highlight.get('key')}", "highlight"))
        selection = region.get("selection") or {}
        lines = []
        container = selection.get("container")
        if container:
            lines.append(
                f"整个容器：{container.get('controlType')} · {container.get('name')}"
                f" · 内容：{container.get('contentText')}")
        for component in selection.get("components") or []:
            lines.append(
                f"已选：{component.get('controlType')} · {component.get('name')}"
                f" · 内容：{component.get('contentText')}"
                f" · 窗口：{(component.get('window') or {}).get('title') or '未知'}")
        state = region.get("state")
        if state == "confirmed":
            lines.append("已确认所选，正在打开断言向导…")
        if lines:
            canvas.itemconfigure(
                self._region_status_label,
                text="\n".join([canvas.itemcget(self._region_status_label, "text")
                                .split("\n")[0], *lines]))

    def _open_region_wizard(self, selection: Mapping[str, Any]) -> None:
        """遮罩确认所选后打开中文向导（查找什么 / 怎样满足）。"""
        from .region_wizard import RegionWizardDialog

        self._panel_log_note("wizard-opening")
        # F76（审查授权）：阻塞步定谳节点——close/ctor 前后各 1 行
        self._panel_log_note("wizard-close-start")
        self._close_region_overlay(restore=False)   # 面板保持收起，进入向导
        self._panel_log_note("wizard-close-done")
        _wiz_token = getattr(self, "_current_push_token", None)

        def _wizard_failed(exc: Exception) -> None:
            # F40：向导构建失败绝不留 windowless 面板——恢复前台+红字。
            self._panel_log_note(f"wizard-build-failed {exc!r}")
            # F69：向导失败信号——token 对账，host 不得报完成
            if _wiz_token and self._host_signal is not None:
                try:
                    self._host_signal({"wizardFailed": _wiz_token,
                                       "error": repr(exc)})
                except Exception:
                    pass
            import traceback as _tb
            try:
                _tb.print_exc(file=sys.stderr)
            except Exception:
                pass
            try:
                if self._root is not None:
                    self._root.deiconify()
                if self._detail_label is not None:
                    self._detail_label.configure(
                        text=f"断言向导打开失败：{exc}",
                        foreground="#c62828")
            except Exception:
                pass

        def on_save(draft: Mapping[str, Any]) -> str:
            request_id = uuid.uuid4().hex
            self._request_with_id("region_save", request_id, draft=dict(draft))
            return request_id

        def on_cancel() -> None:
            self._request_region_cancel()

        self._panel_log_note("wizard-ctor-start")
        try:
            self._region_wizard = RegionWizardDialog(
                {"tk": self._tk, "ttk": self._ttk,
                 "messagebox": self._messagebox, "parent": self._root},
                selection, on_save=on_save, on_cancel=on_cancel)
        except Exception as exc:   # F40：windowless zombie 防御
            _wizard_failed(exc)
            return
        if self._region_wizard.dialog is None:
            _wizard_failed(RuntimeError("wizard dialog is None"))
            return
        try:
            state = str(self._root.state()) if self._root is not None else "?"
        except Exception:
            state = "?"
        try:
            mapped = (self._root.winfo_ismapped()
                      if self._root is not None and
                      callable(getattr(self._root, "winfo_ismapped", None))
                      else "?")
        except Exception:
            mapped = "?"
        self._panel_log_note(f"wizard-opened root_state={state} "
                             f"root_mapped={mapped}")
        # F69：向导就绪=真实可见态核验后发出（host 不把 render 返回当就绪）
        if _wiz_token and self._host_signal is not None:
            try:
                self._host_signal({"wizardReady": _wiz_token,
                                   "rootState": state, "mapped": str(mapped)})
            except Exception:
                pass
        # 向导打开即取最近动作候选（06 §2）；回执失败由向导如实降级。
        self._panel_log_note("wizard-opened")
        self._region_actions_request_id = uuid.uuid4().hex
        self._request_with_id("recent_actions", self._region_actions_request_id)

    def _close_region_wizard(self, *, restore: bool = True) -> None:
        wizard = self._region_wizard
        self._region_wizard = None
        self._region_actions_request_id = None
        if wizard is not None and getattr(wizard, "dialog", None) is not None:
            try:
                wizard.dialog.destroy()
            except Exception:
                pass
        if restore and self._root is not None:
            self._root.deiconify()

    def _close_region_overlay(self, *, restore: bool = True) -> None:
        overlay = self._region_overlay
        self._region_overlay = None
        self._region_canvas = None
        self._region_drag_start = None
        self._region_cancel_request_id = None
        valve = self._region_valve
        self._region_valve = None
        if valve is not None:
            valve.on_overlay_close()
        stop = self._region_watchdog_stop
        self._region_watchdog_stop = None
        if stop is not None:
            stop.set()   # 看门狗线程随遮罩关闭而退出
        unbind_all = getattr(self._root, "unbind_all", None)
        if callable(unbind_all):
            try:
                unbind_all("<Escape>")   # 解除全局 Esc 兜底
            except Exception:
                pass
            for sequence, func_id in self._region_diag_bindings:
                try:
                    unbind_all(sequence, func_id)   # 解除诊断/兜底绑定
                except Exception:
                    pass
        self._region_diag_bindings = []
        self._region_fallback_active = False
        self._restore_wndproc_probes()
        self._uninstall_input_diag()
        if overlay is not None:
            try:
                overlay.destroy()
            except Exception:
                pass
        if restore and self._root is not None:
            # F10：offscreen 模式先恢复原 geometry/state（deiconify 对
            # mapped 窗口无害）；withdraw 模式无保存记录则旧行为。
            self._restore_root_after_region()
            self._root.deiconify()   # 恢复进入前状态（面板收回前台）

    def _restore_panel(self) -> None:  # pragma: no cover - live desktop
        """收起后的恢复：deiconify 回到屏幕（host open 的 alreadyRunning 路径）。"""
        if self._root is not None:
            self._root.deiconify()

    def _render_snapshot(self, snapshot: Mapping[str, Any]) -> None:
        if self._status_label is None:
            return
        view = snapshot.get("viewState")
        session = snapshot.get("session") or {}
        labels = {
            "IDLE": "就绪，尚未录制",
            "RECORDING": "● 录制中",
            "PAUSED": "⏸ 已暂停",
            "STOPPING": "正在停止…",
            "STOPPED": "已停止",
            "ERROR": "出错",
        }
        self._status_label.configure(text=labels.get(view, str(view)))
        detail = []
        if session:
            detail.append(f"已录 {session.get('actionCount', 0)} 个动作")
            remaining = session.get("leaseSecondsRemaining")
            if isinstance(remaining, (int, float)):
                detail.append(f"租约 {remaining:.0f} 秒")
            if session.get("name"):
                detail.append(str(session["name"]))
        pending = snapshot.get("pendingResults") or []
        if pending:
            detail.append(f"待收集结果 {len(pending)} 份（Agent 连接后自动下载）")
        self._detail_label.configure(text=" · ".join(detail))
        recording = view in {"RECORDING", "PAUSED"}
        self._paused_view = view == "PAUSED"
        if self._start_button is not None:
            self._start_button.configure(state="disabled" if recording else "normal")
        if self._pause_button is not None:
            self._pause_button.configure(
                state="normal" if recording else "disabled",
                text="恢复" if self._paused_view else "暂停")
        if self._stop_button is not None:
            self._stop_button.configure(state="normal" if recording else "disabled")

    def _toggle_pause(self) -> None:  # pragma: no cover - live desktop
        """PAUSED 时发 resume，RECORDING 时发 pause（按钮文字同步）。"""
        if self._paused_view:
            self._request("resume")
        else:
            self._request("pause")

    def _render_ack(self, result: Mapping[str, Any]) -> None:
        if self._status_label is None:
            return
        if result.get("ok"):
            directory = result.get("directory")
            if directory:
                self._detail_label.configure(text=f"产物已保存：{directory}")
        else:
            # 原生模态框会停掉 Tcl after 心跳——看门狗须在此期间暂停，
            # 否则用户读错误对话框超过 8 秒就会误触发硬退出。
            valve = self._region_valve
            if valve is not None:
                valve.enter_modal()
            try:
                self._messagebox.showerror(
                    "操作失败", str(result.get("error") or result))
            except Exception:
                pass
            finally:
                if valve is not None:
                    valve.exit_modal()

    _host_signal = None   # F69：三相回执桥（pump 注入 emit）

    def _poll(self) -> None:  # pragma: no cover - live desktop
        """Drain host pushes on the Tk thread (Tk is not thread-safe)."""
        while True:
            try:
                message = self._incoming.get_nowait()
            except Exception:
                break
            if message.get("ack"):
                self._current_push_token = str(message.get("ack"))
            try:
                # F69（审查设计三相回执）：received=送达即回执（host 知道
                # 推送成功，F67 已验证可行）；rendered=渲染完成信号；向导
                # 就绪/失败另发（render 返回≠向导就绪）。失败 token 对账，
                # host 不提前宣称完成。
                ack_token = message.get("ack")
                # F69：received=独立信号（不置 done——审查 A 契约：render
                # 失败零确认帧；pushed=渲染成功确认，语义不变）
                if ack_token and self._host_signal is not None:
                    try:
                        self._host_signal({"received": str(ack_token)})
                    except Exception:
                        pass
                self.render(message)
                # F69b：pushed=渲染成功确认（done 语义，F68 契约原样）；
                # rendered 独立信号撤（审查 fixture 实证=重复帧）
                if ack_token and self.on_pushed is not None:
                    try:
                        self.on_pushed(str(ack_token))
                    except Exception as _eerr:
                        try:
                            self._panel_log_note(
                                f"push-echo-failed token={ack_token} "
                                f"{type(_eerr).__name__}: {_eerr}")
                        except Exception:
                            pass
            except Exception:
                # F68：渲染失败不回执（host 如实 False）——F67 的 pushFailed
                # 后缀无人匹配且 done 已 set，已撤。
                # F28：静默吞异常让开遮罩回归不可见（实机症状=零痕迹）——
                # 栈打 stderr（子进程 stderr 服务侧可见），不中断轮询。
                import traceback
                try:
                    traceback.print_exc()
                except Exception:
                    pass
        if self._host_lost.is_set() and not self._host_lost_handled:
            self._host_lost_handled = True
            self._handle_host_lost()
            return
        if self._root is not None:
            self._root.after(200, self._poll)

    def _handle_host_lost(self) -> None:  # pragma: no cover - live desktop
        """父服务失联（F16）：释放热键 → 明确反馈 → 2.5 秒后自动退出。

        监督实测：后台 stopped 而活面板仍显示陈旧状态，要求用户杀进程
        是运维欠账。此处不依赖服务：stdin 管道关闭即判死；热键释放
        （PostThreadMessage WM_QUIT → 泵退出即 UnregisterHotKey）保证
        新面板可注册；先写状态栏再延迟销毁，无人值守也自动退场。"""
        self._panel_log_note(
            "host-lost triggered overlay_open="
            f"{self._region_overlay is not None} wizard_open="
            f"{self._region_wizard is not None} confirm_sent="
            f"{self._confirm_sent}")   # F38：静默退出定谳点位
        # F64（审查测试钉死）：host-lost 先成对关闭遮罩/valve/看门狗
        # （restore=False：桌面即将退出无需恢复面板几何），杜绝看门狗
        # /valve 与遮罩销毁竞争；热键释放/2500ms 退出/单次处理保持。
        try:
            self._close_region_overlay(restore=False)
        except Exception:
            pass
        root = self._root
        try:
            from .region_overlay import request_hotkey_shutdown
            released = request_hotkey_shutdown()
        except Exception:
            released = False
        try:
            self.on_request("panelExit", uuid.uuid4().hex,
                            {"reason": "host_lost", "hotkeyReleased": released})
        except Exception:
            pass
        try:
            if self._status_label is not None:
                self._status_label.configure(
                    text=("父服务已失联：热键已释放，面板即将退出。"
                          "请从服务侧重新启动录制面板。"),
                    foreground="#c62828")
        except Exception:
            pass
        try:
            print("edr-wd-recorder-panel: host lost, exiting", flush=True)
        except Exception:
            pass
        if root is not None:
            try:
                root.after(2500, root.destroy)
            except Exception:
                pass


def _panel_log_path() -> str:
    """F33：面板子进程诊断日志（%TEMP%/edr-wd-region-panel.log）。

    面板子进程 stderr 不到 server 日志——F27/F32 两轮开遮罩回归「零痕迹」
    的观测黑洞。child_main 入口即重定向 stderr 到本文件（append）+ 原生
    faulthandler，任何 traceback/异常栈落盘可查。"""
    import tempfile
    return os.path.join(tempfile.gettempdir(), "edr-wd-region-panel.log")


def _recorder_panel_child_main() -> None:  # pragma: no cover - child entry
    # F56：崩溃取证——0xC0000409 可复现，fatal 时自动打 Python 栈到 stderr。
    import faulthandler as _fh
    try:
        _fh.enable()
    except Exception:
        pass
    """Child entry: own the main thread; relay requests over stdout."""
    # F33：先接住 stderr（config 解析异常也要可见），再读 config。
    try:
        _panel_log = open(_panel_log_path(), "a", encoding="utf-8",
                          buffering=1)
        sys.stderr = _panel_log
        import faulthandler
        faulthandler.enable(file=_panel_log)
        print(f"=== panel child start pid={os.getpid()} "
              f"{time.strftime('%Y-%m-%dT%H:%M:%S')} ===", file=_panel_log)
    except Exception:
        pass   # 日志失败不阻断启动
    config = json.loads(sys.stdin.readline())
    # F6③ 单实例互斥（真机案例：僵尸面板 8244 无窗口却占 Ctrl+Shift+Q，
    # 后到可见面板注册失败红字）。拿不到互斥=已有活面板：本实例立即
    # 上报退出，绝不注册热键与活面板冲突。
    # F25：本名只在 _show_region_overlay 局部作用域导入过——裸跑
    # child_main 直接 NameError 秒死（实机 open_recording_ui False 根因）；
    # 此处惰性导入防循环依赖，与遮罩路径模式一致。
    from .region_overlay import acquire_panel_instance_mutex
    acquired, holder = acquire_panel_instance_mutex()
    if not acquired:
        sys.stdout.write(json.dumps(
            {"panelExit": True, "reason": "already_running",
             "holder": holder}, ensure_ascii=False) + "\n")
        try:
            sys.stdout.flush()
        except Exception:
            pass
        print("edr-wd-recorder-panel: another panel instance holds the "
              "mutex, exiting", flush=True)
        return
    if sys.platform == "win32":
        # F16③→F17：DPI 感知改 env 门控（默认关）。F17 实测遮罩
        # rect=1696x1008 而 GetClipCursor=1680x988（16x20 失配）疑为
        # DPI 声明后的 Tk 几何残留——回归默认隔离变量；REGION_DPI_AWARE=1
        # 按需开（声明后画布坐标=物理屏幕坐标=驱动坐标系）。
        try:
            from .region_overlay import dpi_aware_enabled
            if dpi_aware_enabled():
                import ctypes
                try:
                    ctypes.windll.user32.SetProcessDpiAwarenessContext(
                        ctypes.c_void_p(-4))
                except Exception:
                    ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass

    def emit(message: Mapping[str, Any]) -> None:
        try:
            sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except (BrokenPipeError, ValueError):
            pass

    def on_request(name: str, request_id: str, args: Mapping[str, Any]) -> None:
        emit({"request": name, "requestId": request_id, "args": dict(args)})

    panel = RecorderPanel(config.get("name", "recorder"), None, on_request,
                          arm_valve_watchdog=True,
                          on_pushed=lambda token: emit({"pushed": token}))
    panel._host_signal = lambda msg: emit(msg)   # F69：三相回执桥

    def pump() -> None:
        # Tk renders only on its own thread: stdin messages are queued here
        # and drained by the panel's after() poll on the main thread.
        for line in sys.stdin:
            try:
                message = json.loads(line)
            except ValueError:
                # F52b：坏帧计数留痕（并发写/管道问题的对账证据）。
                try:
                    print("[pump] bad-frame (json.loads failed)", file=sys.stderr,
                          flush=True)
                except Exception:
                    pass
                continue
            if isinstance(message, dict) and "ack" in message:
                # F52b：ack 到达留痕（精确 token——push 超时对账证据）。
                try:
                    print(f"[pump] ack-line received token={message.get('ack')}",
                          file=sys.stderr, flush=True)
                except Exception:
                    pass
            panel._incoming.put(message)
        # F16：stdin EOF = 父服务进程已死（管道关闭）——置位供轮询处理。
        try:
            print("[confirm] stdin-eof (pump exit)", file=sys.stderr,
                  flush=True)   # F38：管道断开时间点（对账 confirm-response）
        except Exception:
            pass
        panel._host_lost.set()

    threading.Thread(target=pump, daemon=True).start()
    # F16①：面板打开即向后端拉实时快照——指示器状态必须与后端一致，
    # 不得显示上一会话的陈旧状态（监督实测：后台 stopped 而面板仍
    # 显示 recording/2steps）。
    panel._request("snapshot")
    emit({"panelReady": True})
    panel.run()
    try:
        print("[panel] child-main-return", file=sys.stderr, flush=True)
    except Exception:
        pass   # F39：run() 返回（优雅退出）末位标记
