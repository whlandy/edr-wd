"""Target-local visible recording indicator and explicit assertion editor."""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from .models import CaptureScope, RecordingModelError

# Marker carried in the recorder window's title so every layer — screenshot
# redaction, foreground resolution — can recognise the recorder's own UI.
RECORDER_UI_MARKER = "[recorder_ui=true]"


def _expected_default(target: Mapping[str, Any]) -> str:
    """断言编辑器 expected 的预填默认值。

    取被选组件的当前取值 `value` —— 即「抓内容再设断言」。读不到就空串，
    行为与旧版一致（期望值仍可手填/改写）。
    """
    value = target.get("value") if isinstance(target, Mapping) else None
    return str(value) if value is not None else ""


@dataclass(frozen=True)
class IndicatorCallbacks:
    pause: Callable[[], Any]
    resume: Callable[[], Any]
    stop: Callable[[], Any]
    add_assertion: Callable[[Mapping[str, Any]], Any]
    # Read the component currently under the pointer for the wizard's
    # "立即试验" — read-only, never a click.  None disables the trial button.
    read_target: Callable[[], Any] | None = None


class RecordingIndicator(Protocol):
    def start(self) -> None: ...
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def update_count(self, count: int) -> None: ...
    def open_assertion_editor(self, target: Mapping[str, Any] | None = None) -> None: ...
    def stop(self) -> None: ...
    def hide_for_region_selection(self) -> dict[str, Any]: ...
    def restore_after_region_selection(self) -> dict[str, Any]: ...


class NullRecordingIndicator:
    def start(self) -> None: pass
    def pause(self) -> None: pass
    def resume(self) -> None: pass
    def update_count(self, count: int) -> None: pass
    def open_assertion_editor(self, target: Mapping[str, Any] | None = None) -> None: pass
    def stop(self) -> None: pass
    def hide_for_region_selection(self) -> dict[str, Any]:
        return {"ok": True, "hidden": False}

    def restore_after_region_selection(self) -> dict[str, Any]:
        return {"ok": True, "restored": False}


class TkRecordingIndicator:
    """Small always-on-top recorder UI running on its own Tk event loop."""

    WINDOW_TITLE = f"EDR-WD Recorder {RECORDER_UI_MARKER}"

    def __init__(
        self,
        name: str,
        scope: CaptureScope,
        callbacks: IndicatorCallbacks,
    ) -> None:
        self.name = name
        self.scope = scope
        self.callbacks = callbacks
        self._commands: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._startup_error: Exception | None = None
        self._root = None
        self._state_label = None
        self._count_label = None
        self._pause_button = None
        self._paused = False
        self._assertion_target: Mapping[str, Any] = {}
        # 区域选区让位状态（06 §2）：隐藏前是否可见由 _region_previous_state
        # 记忆，恢复时按记忆还原；从未隐藏则恢复是安全 no-op。
        self._region_hidden = False
        self._region_previous_state: str | None = None

    @staticmethod
    def _async(callback: Callable, *args) -> None:
        threading.Thread(target=callback, args=args, daemon=True).start()

    def _run(self) -> None:  # pragma: no cover - exercised on live desktop
        try:
            import tkinter as tk
            from tkinter import messagebox, ttk

            root = tk.Tk()
            self._root = root
            root.title(self.WINDOW_TITLE)
            root.attributes("-topmost", True)
            root.resizable(False, False)
            self._place_clear_of_default_windows(root)
            # The close button used to be a no-op so the recorder UI could not
            # be dismissed while a capture was running. That left it with no
            # way to be dismissed at all: when a stop is slow, or the parent
            # has gone, the window just sits there ignoring the user. Route it
            # to the same action as the Stop button instead — ending the
            # recording is what closing the recorder should mean.
            root.protocol(
                "WM_DELETE_WINDOW", lambda: self._async(self.callbacks.stop),
            )
            frame = ttk.Frame(root, padding=8)
            frame.grid()
            ttk.Label(frame, text=f"● REC  {self.name}", foreground="#c62828").grid(
                row=0, column=0, columnspan=4, sticky="w"
            )
            ttk.Label(frame, text=f"{self.scope.process_name} · {self.scope.window_title}").grid(
                row=1, column=0, columnspan=4, sticky="w"
            )
            self._state_label = ttk.Label(frame, text="recording")
            self._state_label.grid(row=2, column=0, sticky="w")
            self._count_label = ttk.Label(frame, text="0 steps")
            self._count_label.grid(row=2, column=1, sticky="w")
            self._pause_button = ttk.Button(frame, text="Pause", command=self._toggle_pause)
            self._pause_button.grid(row=3, column=0)
            # 06 §1：浮窗上的断言按钮已移除——Ctrl+Alt+Q 是唯一断言
            # 入口（绕过会话准备的直连编辑器入口不再存在）。断言编辑器本身
            # 保留：仍用于渲染服务侧冻结草稿（"assert" 命令 / 兼容路径）。
            ttk.Button(
                frame, text="Stop", command=lambda: self._async(self.callbacks.stop)
            ).grid(row=3, column=1)

            def poll() -> None:
                while True:
                    try:
                        command, value = self._commands.get_nowait()
                    except queue.Empty:
                        break
                    if command == "state":
                        self._state_label.configure(text=str(value))
                    elif command == "count":
                        self._count_label.configure(text=f"{value} steps")
                    elif command == "assert":
                        self._assertion_target = value or {}
                        self._show_assertion_editor()
                    elif command == "assert_ack":
                        self._apply_assert_ack(value)
                    elif command == "hide_for_region":
                        self._apply_hide_for_region()
                    elif command == "restore_after_region":
                        self._apply_restore_after_region()
                    elif command == "stop":
                        root.destroy()
                        return
                root.after(100, poll)

            self._messagebox = messagebox
            self._ttk = ttk
            self._tk = tk
            self._ready.set()
            root.after(100, poll)
            root.mainloop()
        except Exception as exc:
            self._startup_error = exc
            self._ready.set()
        finally:
            # Let the interpreter be finalized on the thread that created it.
            # `destroy()` runs here, but these references outlive it, so the
            # Tk object was deallocated whenever the session was collected —
            # on whatever thread happened to be running the collector. Tcl
            # answers a cross-thread finalize with Tcl_Panic and abort(), and
            # that took the whole MCP server down three times in ten days,
            # always at the same offset in tcl86t.dll. Dropping them here
            # leaves the local `root` as the last reference, and it dies with
            # this frame, on this thread.
            self._root = None
            self._state_label = None
            self._count_label = None
            self._pause_button = None
            self._assertion_dialog = None

    @staticmethod
    def _place_clear_of_default_windows(root) -> None:  # pragma: no cover - live UI
        """Park the always-on-top indicator where target windows are not.

        Tk's default placement is the top-left corner, which is exactly where
        an application window's navigation usually sits.  Because this window
        is `-topmost`, it then swallows clicks meant for the target: a live
        HiSec capture recorded two clicks that physically landed on this
        indicator as if they had happened inside the target window, and one
        of them hit a control here and ended the session.

        The bottom-right corner is chosen because a window placed there would
        have to extend past the screen edge to reach it.
        """
        try:
            root.update_idletasks()
            width = root.winfo_reqwidth()
            height = root.winfo_reqheight()
            margin = 24
            x = max(0, root.winfo_screenwidth() - width - margin)
            y = max(0, root.winfo_screenheight() - height - margin)
            root.geometry(f"+{x}+{y}")
        except Exception:
            # Placement is a convenience; never let it stop the recorder UI
            # from coming up.
            pass

    def _toggle_pause(self) -> None:  # pragma: no cover - live UI
        self._paused = not self._paused
        self._pause_button.configure(text="Resume" if self._paused else "Pause")
        self._async(self.callbacks.pause if self._paused else self.callbacks.resume)

    def _show_assertion_editor(self) -> None:  # pragma: no cover - live UI
        """中文类型化断言向导：类型 → 期望 → 摘要 → 试验 → 保存。

        默认路径不出现组件 ID 与 JSON：身份来自冻结草稿，期望控件按类型
        渲染（布尔是/否、文本单行）；读取失败如实披露，不预填空值冒充观测。
        保存带 draftId（requestId），成功 ACK 后关闭并计数，失败保留表单。
        """
        if self._root is None:
            return
        from .assertion_draft import (
            available_assertion_types, render_assertion_summary,
            validate_assertion_draft, preview_assertion,
        )
        draft = dict(self._assertion_target or {})
        observed = draft.get("observed") or {}
        dialog = self._tk.Toplevel(self._root)
        dialog.title("添加断言")
        dialog.attributes("-topmost", True)
        entries = {}

        ttk = self._ttk
        row = 0
        read_status = str(draft.get("readStatus") or "unknown")
        current_value = (observed or {}).get("value")
        if read_status == "ok" and current_value is not None:
            ttk.Label(dialog, text=f"当前值：{current_value}", foreground="#1b5e20").grid(
                row=row, column=0, columnspan=2, sticky="w")
        else:
            reason = draft.get("readError") or "未能读取当前值"
            ttk.Label(dialog, text=f"当前值：未能读取（{reason}）——可手填期望",
                      foreground="#b26a00").grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1

        choices = available_assertion_types(observed or None)
        ttk.Label(dialog, text="验证方式").grid(row=row, column=0, sticky="w")
        type_var = self._tk.StringVar()
        type_box = ttk.Combobox(dialog, state="readonly", width=24,
                                textvariable=type_var,
                                values=[f"{c['label']} ({c['type']})" for c in choices])
        type_box.current(0)
        type_box.grid(row=row, column=1, sticky="w")
        row += 1

        ttk.Label(dialog, text="期望结果").grid(row=row, column=0, sticky="w")
        kind_by_label = {f"{c['label']} ({c['type']})": c for c in choices}
        expected_entry = ttk.Entry(dialog, width=32)
        expected_bool = self._tk.StringVar(value="是")
        bool_box = ttk.Combobox(dialog, state="readonly", width=8,
                                textvariable=expected_bool, values=["是", "否"])
        initial = choices[0]
        if initial["valueKind"] == "bool":
            expected_bool.set("是" if draft.get("typedExpected") is True else "否")
            bool_box.grid(row=row, column=1, sticky="w")
        else:
            expected_entry.insert(0, str(draft.get("typedExpected") or ""))
            expected_entry.grid(row=row, column=1, sticky="w")
        entries["expected_entry"], entries["expected_bool"] = expected_entry, expected_bool
        entries["expect_row"] = row
        row += 1

        summary_var = self._tk.StringVar()
        summary_label = ttk.Label(dialog, textvariable=summary_var, foreground="#333",
                                  wraplength=420, justify="left")
        summary_label.grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1

        def _current_choice():
            return kind_by_label.get(type_var.get()) or choices[0]

        def _typed_expected(choice):
            return (True if expected_bool.get() == "是" else False) \
                if choice["valueKind"] == "bool" \
                else entries["expected_entry"].get()

        def _refresh_summary(*_):
            choice = _current_choice()
            probe = {**draft, "assertionType": choice["type"],
                     "typedExpected": _typed_expected(choice)}
            summary_var.set(render_assertion_summary(_draft_from(probe)))

        def _switch_editor(*_):
            """类型变更必须切换实际输入控件：布尔显示是/否，文本显示输入框。"""
            choice = _current_choice()
            if choice["valueKind"] == "bool":
                expected_entry.grid_remove()
                bool_box.grid(row=entries["expect_row"], column=1, sticky="w")
                expected_bool.set("是" if draft.get("typedExpected") is True else "否")
            else:
                bool_box.grid_remove()
                expected_entry.grid(row=entries["expect_row"], column=1, sticky="w")
                expected_entry.delete(0, len(expected_entry.get()))
                expected_entry.insert(0, str(draft.get("typedExpected") or ""))
            _refresh_summary()
        type_box.bind("<<ComboboxSelected>>", _switch_editor)
        expected_entry.bind("<KeyRelease>", _refresh_summary)
        expected_bool.trace_add("write", lambda *_: _refresh_summary())

        def _draft_from(probe):
            from .assertion_draft import AssertionDraft
            return AssertionDraft(
                draft_id=str(probe.get("draftId") or ""),
                assertion_type=probe.get("assertionType"),
                typed_expected=probe.get("typedExpected"),
                observed=dict(probe.get("observed") or {}),
                read_status=str(probe.get("readStatus") or "unknown"),
                read_error=probe.get("readError"),
                protection_status=probe.get("protectionStatus"),
                timeout_seconds=float(probe.get("timeoutSeconds") or 10),
                binding=tuple(probe["binding"]) if probe.get("binding") else None,
                expected_was_edited=bool(probe.get("expectedWasEdited")),
                window=dict(probe.get("window") or {}),
            )

        def _run_trial():
            """预演基于冻结观测（draft.observed），只读、不点击、不保存。
            对冻结组件的实时重读未实现——如需重读需窗口锁定，另行走回放链。"""
            choice = _current_choice()
            probe = {**draft, "assertionType": choice["type"],
                     "typedExpected": _typed_expected(choice)}
            d = _draft_from(probe)
            observed = d.observed or {}
            if probe.get("readStatus") != "ok":
                self._messagebox.showinfo(
                    "立即试验", "冻结时未能读取当前值，无法预演；期望仍可保存。",
                    parent=dialog)
                return
            result = preview_assertion(d, lambda: ("ok", observed.get("value")))
            text = {"pass": "满足 ✅", "fail": "不满足（可按此期望保存）",
                    "undetermined": "不可判定/读取失败"}[result["result"]]
            self._messagebox.showinfo(
                "立即试验", f"预演结果（基于冻结观测）：{text}\n"
                f"冻结值：{observed.get('value')!r}", parent=dialog)

        trial_button = ttk.Button(dialog, text="立即试验", command=_run_trial)
        trial_button.grid(row=row, column=0, sticky="w")
        row += 1

        bind_previous = self._tk.BooleanVar(value=False)
        bind_check = ttk.Checkbutton(
            dialog, text="作为上一个动作的结果验证", variable=bind_previous,
        )
        if draft.get("binding"):
            bind_check.configure(state="disabled")   # 显式绑定优先，见 confirm()
        bind_check.grid(row=row, column=1, sticky="w")
        row += 1

        def confirm() -> None:
            choice = _current_choice()
            probe = {**draft, "assertionType": choice["type"],
                     "typedExpected": _typed_expected(choice)}
            try:
                payload = validate_assertion_draft(_draft_from(probe))
            except Exception as exc:  # noqa: BLE001 - validation errors stay in form
                self._messagebox.showerror("无法保存", str(exc), parent=dialog)
                return
            if payload["expected"] == "" and not self._messagebox.askyesno(
                "确认空期望", "使用空字符串作为期望值？", parent=dialog
            ):
                return
            if draft.get("binding"):
                # 从“为该动作添加验证”进入：绑定已经显式指定（bindCausalId），
                # last-action 勾选不得覆盖它 —— 提交前移除该键。
                payload.pop("bindPrevious", None)
            else:
                payload["bindPrevious"] = bool(bind_previous.get())
            submit_button.configure(state="disabled")
            self._async(self._submit_assertion, payload, dialog)

        submit_button = ttk.Button(dialog, text="保存", command=confirm)
        submit_button.grid(row=row, column=1, sticky="w")
        # The ack lands on the poll thread, so the form must be reachable from
        # there: success destroys it, failure keeps it open with the error.
        self._assertion_dialog = {
            "dialog": dialog, "submit_button": submit_button, "entries": entries,
        }
        _refresh_summary()

    def _submit_assertion(self, payload: Mapping[str, Any], dialog) -> None:
        """把保存请求交给回调；成功/失败 ACK 只认**父进程的真实提交回执**。

        子进程模式下回调只是把请求 emit 给父进程（返回 None）—— None 不是
        成功，表单保持 pending，直到父进程按同一 requestId 回 ack。同进程
        直连（callbacks 返回 dict）才有立即的本地结果。
        """
        request_id = payload.get("requestId")
        if self._assertion_dialog is not None:
            # requestId 记在表单记录（外层 dict）上，ack 匹配用它。
            self._assertion_dialog["requestId"] = request_id
        try:
            result = self.callbacks.add_assertion(payload)
        except Exception as exc:  # noqa: BLE001 - the form must show the error
            self._commands.put(("assert_ack", {
                "ok": False, "error": str(exc), "requestId": request_id}))
            return
        if isinstance(result, Mapping):
            # 同进程直连：会话提交的真实回执，按 requestId 回表单。
            self._commands.put(("assert_ack", {
                **dict(result), "requestId": request_id}))
        # emit/IPC 路径（result is None）：不发任何 ack —— 表单停在
        # pending，成功/失败都等父进程的实际提交回执。

    def _apply_assert_ack(self, ack: Mapping[str, Any]) -> None:
        """仅当 ACK 的 requestId 匹配当前表单时才生效；错 ID 不关表单。"""
        dialog = self._assertion_dialog
        if dialog is None:
            return
        current = dialog.get("requestId") if isinstance(dialog, Mapping) else None
        if current is None:
            current = getattr(self, "_assertion_dialog_request_id", None)
        if current is not None and ack.get("requestId") not in (None, current):
            return   # 别的请求的回执，不作用于正在编辑的表单
        if ack.get("ok") is True:
            sequence = ack.get("sequence")
            saved = f"已保存 1 条断言（sequence {sequence}）" if sequence else "已保存 1 条断言"
            if self._count_label is not None:
                self._count_label.configure(text=saved)
            dialog["dialog"].destroy()
            self._assertion_dialog = None
            return
        button = dialog.get("submit_button")
        if button is not None:
            try:
                button.configure(state="normal")
            except Exception:
                pass
        message = str(ack.get("error") or "保存失败")
        try:
            self._messagebox.showerror("保存失败", message, parent=dialog["dialog"])
        except Exception:
            pass

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="edr-wd-recording-indicator", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise RecordingModelError(
                "recording_indicator_unavailable", "recording indicator startup timed out"
            )
        if self._startup_error is not None:
            raise RecordingModelError(
                "recording_indicator_unavailable", str(self._startup_error)
            )

    def pause(self) -> None:
        self._commands.put(("state", "paused"))

    def resume(self) -> None:
        self._commands.put(("state", "recording"))

    def update_count(self, count: int) -> None:
        self._commands.put(("count", count))

    def open_assertion_editor(self, target: Mapping[str, Any] | None = None) -> None:
        self._commands.put(("assert", dict(target or {})))

    def hide_for_region_selection(self) -> dict[str, Any]:
        """为区域选区让位：隐藏浮窗（经命令队列转 Tk 线程执行）。

        会话在持有锁时调用本方法——这里只入队，绝不做 Tk 调用、绝不把
        异常抛回录制主流程。
        """
        try:
            self._commands.put(("hide_for_region", None))
            return {"ok": True, "hidden": True}
        except Exception as exc:  # noqa: BLE001 - never raise into session lock
            return {"ok": False, "error": str(exc)}

    def restore_after_region_selection(self) -> dict[str, Any]:
        """区域选区结束后按记忆状态恢复浮窗；未隐藏过则是安全 no-op。"""
        try:
            self._commands.put(("restore_after_region", None))
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001 - never raise into session lock
            return {"ok": False, "error": str(exc)}

    def _apply_hide_for_region(self) -> dict[str, Any]:
        """Tk 线程内执行：记住隐藏前状态并收起浮窗。"""
        root = self._root
        if root is None:
            return {"ok": False, "error": "indicator window is gone"}
        try:
            if not self._region_hidden:
                try:
                    self._region_previous_state = str(root.state())
                except Exception:
                    self._region_previous_state = "normal"
                root.withdraw()
                self._region_hidden = True
            return {"ok": True, "hidden": True,
                    "previousState": self._region_previous_state}
        except Exception as exc:  # noqa: BLE001 - surfaced, not raised
            return {"ok": False, "error": str(exc)}

    def _apply_restore_after_region(self) -> dict[str, Any]:
        """Tk 线程内执行：按记忆恢复；隐藏前本就收起则维持收起。"""
        root = self._root
        if not self._region_hidden:
            return {"ok": True, "restored": False}
        if root is None:
            self._region_hidden = False
            return {"ok": False, "error": "indicator window is gone"}
        try:
            previous = self._region_previous_state or "normal"
            if previous != "withdrawn":
                root.deiconify()
            self._region_hidden = False
            self._region_previous_state = None
            return {"ok": True, "restored": True, "previousState": previous}
        except Exception as exc:  # noqa: BLE001 - surfaced, not raised
            return {"ok": False, "error": str(exc)}

    def stop(self) -> None:
        self._commands.put(("stop", None))
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)


class SubprocessTkIndicator:
    """Run the Tk indicator in a child process that owns its main thread.

    macOS Aqua Tk must run on the process main thread.  Driving
    ``TkRecordingIndicator`` from a worker thread there wedges the whole
    interpreter: ``start()`` never returns and the process stops responding to
    SIGINT/SIGQUIT.  The recorder still has to show a visible indicator, so
    rather than dropping the UI the same Tk code runs in a child process,
    where it legitimately owns the main thread.  Commands go down as JSON
    lines on stdin and button presses come back as JSON lines on stdout.
    """

    def __init__(
        self,
        name: str,
        scope: CaptureScope,
        callbacks: IndicatorCallbacks,
        *,
        startup_timeout: float = 15.0,
    ) -> None:
        self.name = name
        self.scope = scope
        self.callbacks = callbacks
        self._startup_timeout = startup_timeout
        self._process: Any = None
        self._reader: threading.Thread | None = None
        self._ready = threading.Event()
        self._startup_error: str | None = None
        # (x, y, w, h) of the recorder UI once it is on screen; consumed by
        # the correlator to reject input that landed here, not on the target.
        self.window_rect: tuple[int, int, int, int] | None = None

    def _send(self, command: str, value: Any = None) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            return
        try:
            process.stdin.write(json.dumps({"command": command, "value": value}) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, ValueError):
            # The child exited (user closed it, or it crashed).  The session
            # owns recording state, so a dead indicator must not break it.
            pass

    def _consume(self) -> None:
        process = self._process
        if process is None:
            return
        for line in process.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if message.get("ready") is not None:
                self._startup_error = message.get("error")
                rect = message.get("rect")
                if isinstance(rect, list) and len(rect) == 4:
                    self.window_rect = tuple(int(v) for v in rect)
                self._ready.set()
                continue
            callback = message.get("callback")
            payload = message.get("payload")
            # One raising callback must not kill the reader thread: the
            # session refuses stale button reports (recording_state_invalid)
            # the moment it settles, and a dead reader leaves the child's
            # window up with every button dead and no ack ever sent — a
            # live-looking orphan the session's own teardown cannot rescue
            # (the child only self-terminates on stdin EOF).  Same hazard
            # class as the batch-18 EOF orphan, different root cause.
            try:
                if callback == "pause":
                    self.callbacks.pause()
                elif callback == "resume":
                    self.callbacks.resume()
                elif callback == "stop":
                    self.callbacks.stop()
                elif callback == "add_assertion":
                    try:
                        result = self.callbacks.add_assertion(payload or {})
                    except Exception as exc:  # noqa: BLE001 - form must show the error
                        ack = {"ok": False, "error": str(exc)}
                    else:
                        if not isinstance(result, Mapping):
                            # 回调没有返回提交结果（emit/误接线返回 None）：
                            # 这不是成功 —— 发失败 ACK，表单保留给真实回执。
                            ack = {"ok": False,
                                   "error": "assertion submission returned no receipt "
                                            "(callback returned None)"}
                        else:
                            ack = dict(result)
                            ack.setdefault("ok", False)
                    # 双向 ACK：带同一 requestId 回给子进程表单 —— 成功才关表单，
                    # 失败保留表单；错 ID 的回执不会误关正在编辑的表单。
                    ack["requestId"] = (payload or {}).get("requestId")
                    self._send("assert_ack", ack)
            except Exception:
                continue

    def start(self) -> None:
        import subprocess

        env = dict(os.environ)
        # The child imports this package; carry the parent's import roots so
        # it works from a source checkout and from an installed package.
        env["PYTHONPATH"] = os.pathsep.join(
            path for path in sys.path if path
        )
        # The target deploys this package flattened — `recording.indicator`
        # there, `target.recording.indicator` in a source checkout — so the
        # child's import is derived from how *this* module was imported
        # rather than spelled out for one of the two layouts.
        self._process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                f"from {__name__} import _indicator_child_main;"
                " _indicator_child_main()",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            env=env,
            text=True,
        )
        self._reader = threading.Thread(
            target=self._consume, name="edr-wd-indicator-reader", daemon=True,
        )
        self._reader.start()
        self._process.stdin.write(json.dumps({
            "name": self.name, "scope": self.scope.to_dict(),
        }) + "\n")
        self._process.stdin.flush()
        if not self._ready.wait(timeout=self._startup_timeout):
            self._terminate()
            raise RecordingModelError(
                "recording_indicator_unavailable",
                "recording indicator startup timed out",
            )
        if self._startup_error:
            self._terminate()
            raise RecordingModelError(
                "recording_indicator_unavailable", self._startup_error,
            )

    def _terminate(self) -> None:
        process = self._process
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except Exception:
                process.kill()
        self._process = None

    def pause(self) -> None:
        self._send("state", "paused")

    def resume(self) -> None:
        self._send("state", "recording")

    def update_count(self, count: int) -> None:
        self._send("count", count)

    def open_assertion_editor(self, target: Mapping[str, Any] | None = None) -> None:
        self._send("assert", dict(target or {}))

    def hide_for_region_selection(self) -> dict[str, Any]:
        """转发给子进程浮窗：隐藏并记忆（子进程内 Tk 线程执行）。"""
        try:
            self._send("hide_for_region", None)
            return {"ok": True, "hidden": True}
        except Exception as exc:  # noqa: BLE001 - never raise into session lock
            return {"ok": False, "error": str(exc)}

    def restore_after_region_selection(self) -> dict[str, Any]:
        """转发给子进程浮窗：按记忆恢复；未隐藏过子进程侧安全 no-op。"""
        try:
            self._send("restore_after_region", None)
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001 - never raise into session lock
            return {"ok": False, "error": str(exc)}

    def stop(self) -> None:
        self._send("stop", None)
        process = self._process
        if process is not None:
            try:
                process.wait(timeout=5)
            except Exception:
                pass
        self._terminate()


def _child_callbacks(
    indicator: TkRecordingIndicator,
    emit: Callable[..., bool],
) -> IndicatorCallbacks:
    """Child-side callbacks.

    The Stop button reports to the parent, which owns recording state and
    answers by sending the "stop" command back down.  When the report cannot
    be delivered the parent is gone — nobody will ever send that command —
    so the child enqueues its own stop: the window closes instead of sitting
    there as an always-on-top orphan that cannot even be dismissed.
    """

    def on_stop() -> None:
        if not emit("stop"):
            indicator._commands.put(("stop", None))

    def on_assert(payload: Any) -> None:
        emit("add_assertion", payload)

    return IndicatorCallbacks(
        pause=lambda: emit("pause"),
        resume=lambda: emit("resume"),
        stop=on_stop,
        add_assertion=on_assert,
    )


def _child_emit() -> Callable[..., bool]:
    """Child-side stdout emitter; returns whether the report was delivered.

    The return value is the dead-parent signal for the stop path: a report
    that cannot be flushed means the parent is gone.
    """

    def emit(kind: str, payload: Any = None) -> bool:
        # The parent can exit first — a session that ends for any other reason
        # tears down this pipe while the Tk loop is still alive.  A button
        # press afterwards must not raise out of the Tk callback thread.
        try:
            sys.stdout.write(json.dumps({"callback": kind, "payload": payload}) + "\n")
            sys.stdout.flush()
        except (BrokenPipeError, ValueError):
            return False
        return True

    return emit


def _indicator_child_main() -> None:  # pragma: no cover - child process entry
    """Child entry point: own the main thread and run the Tk indicator on it."""
    config = json.loads(sys.stdin.readline())
    scope = CaptureScope.from_dict(config["scope"])
    emit = _child_emit()

    indicator = TkRecordingIndicator(
        config["name"],
        scope,
        IndicatorCallbacks(
            pause=lambda: None, resume=lambda: None,
            stop=lambda: None, add_assertion=lambda payload: None,
        ),
    )
    # Real callbacks need the indicator (dead-pipe stop self-terminates via
    # its command queue); _run() has not started yet, so rebinding here is
    # race-free.
    indicator.callbacks = _child_callbacks(indicator, emit)

    def announce() -> None:
        indicator._ready.wait()
        error = indicator._startup_error
        # The parent needs this window's rect so the correlator can refuse to
        # record input that physically landed on the recorder UI instead of
        # the target window.
        rect = None
        root = indicator._root
        if root is not None:
            try:
                root.update_idletasks()
                rect = [
                    int(root.winfo_rootx()), int(root.winfo_rooty()),
                    int(root.winfo_width()), int(root.winfo_height()),
                ]
            except Exception:
                rect = None
        try:
            sys.stdout.write(json.dumps({
                "ready": True, "error": None if error is None else str(error),
                "rect": rect,
            }) + "\n")
            sys.stdout.flush()
        except (BrokenPipeError, ValueError):
            pass

    def pump() -> None:
        for line in sys.stdin:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            indicator._commands.put((message.get("command"), message.get("value")))
        # stdin EOF: the parent process is gone (crash, kill, teardown).  No
        # one will ever send "stop" again, so the child terminates itself —
        # the alternative is an always-on-top orphan window that cannot be
        # dismissed (its own buttons only report to the dead pipe).
        indicator._commands.put(("stop", None))

    threading.Thread(target=announce, daemon=True).start()
    threading.Thread(target=pump, daemon=True).start()
    # Tk owns this process's main thread, which is what macOS requires.
    indicator._run()


def tkinter_indicator_factory(
    name: str,
    scope: CaptureScope,
    callbacks: IndicatorCallbacks,
) -> RecordingIndicator:
    """Show the indicator in a child process wherever Tk is available.

    macOS needs this because Aqua Tk must own the process main thread.
    Windows needs it for a different reason, learned the hard way: driving Tk
    from a worker thread works until the interpreter is torn down and built
    again, and the third recording of one server session aborted the whole
    process inside tcl86t.dll (0x80000003, a Tcl panic) — taking the MCP
    server, not just the indicator, down with it. A child process gets a fresh
    Tk per recording, and a panic can only kill the child.
    """
    if sys.platform in {"darwin", "win32"}:
        return SubprocessTkIndicator(name, scope, callbacks)
    return TkRecordingIndicator(name, scope, callbacks)


__all__ = [
    "IndicatorCallbacks", "NullRecordingIndicator", "RecordingIndicator",
    "SubprocessTkIndicator", "TkRecordingIndicator", "tkinter_indicator_factory",
]
