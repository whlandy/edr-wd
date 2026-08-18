"""Target-local visible recording indicator and explicit assertion editor."""

from __future__ import annotations

import json
import queue
import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from .models import CaptureScope, RecordingModelError


@dataclass(frozen=True)
class IndicatorCallbacks:
    pause: Callable[[], Any]
    resume: Callable[[], Any]
    stop: Callable[[], Any]
    add_assertion: Callable[[Mapping[str, Any]], Any]


class RecordingIndicator(Protocol):
    def start(self) -> None: ...
    def pause(self) -> None: ...
    def resume(self) -> None: ...
    def update_count(self, count: int) -> None: ...
    def open_assertion_editor(self, target: Mapping[str, Any] | None = None) -> None: ...
    def stop(self) -> None: ...


class NullRecordingIndicator:
    def start(self) -> None: pass
    def pause(self) -> None: pass
    def resume(self) -> None: pass
    def update_count(self, count: int) -> None: pass
    def open_assertion_editor(self, target: Mapping[str, Any] | None = None) -> None: pass
    def stop(self) -> None: pass


class TkRecordingIndicator:
    """Small always-on-top recorder UI running on its own Tk event loop."""

    WINDOW_TITLE = "EDR-WD Recorder [recorder_ui=true]"

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
            root.protocol("WM_DELETE_WINDOW", lambda: None)
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
            ttk.Button(frame, text="Add assertion", command=self._show_assertion_editor).grid(row=3, column=1)
            ttk.Button(
                frame, text="Stop", command=lambda: self._async(self.callbacks.stop)
            ).grid(row=3, column=2)

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

    def _toggle_pause(self) -> None:  # pragma: no cover - live UI
        self._paused = not self._paused
        self._pause_button.configure(text="Resume" if self._paused else "Pause")
        self._async(self.callbacks.pause if self._paused else self.callbacks.resume)

    def _show_assertion_editor(self) -> None:  # pragma: no cover - live UI
        if self._root is None:
            return
        dialog = self._tk.Toplevel(self._root)
        dialog.title("Add explicit assertion")
        dialog.attributes("-topmost", True)
        # assertion accepts: text_equals / text_contains / text_contains_time /
        # value_equals / visible / checked / enabled / window_open.
        # text_contains_time takes a strftime pattern (e.g. %Y-%m-%d) which is
        # rendered at replay time, not the timestamp visible while recording.
        fields = [
            ("assertion", "text_equals"), ("expected", ""),
            ("timeoutSeconds", "10"),
            ("automationId", self._assertion_target.get("automationId") or ""),
            ("identifier", self._assertion_target.get("identifier") or ""),
            ("controlType", self._assertion_target.get("controlType") or "Text"),
            ("name", self._assertion_target.get("name") or ""),
        ]
        entries = {}
        for row, (field, default) in enumerate(fields):
            self._ttk.Label(dialog, text=field).grid(row=row, column=0, sticky="w")
            entry = self._ttk.Entry(dialog, width=40)
            entry.insert(0, default)
            entry.grid(row=row, column=1)
            entries[field] = entry
        bind_previous = self._tk.BooleanVar(value=False)
        self._ttk.Checkbutton(
            dialog,
            text="bind as the previous action's result verifier",
            variable=bind_previous,
        ).grid(row=len(fields), column=1, sticky="w")

        def confirm() -> None:
            raw_expected = entries["expected"].get()
            try:
                expected = json.loads(raw_expected)
            except json.JSONDecodeError:
                expected = raw_expected
            payload = {field: entry.get() for field, entry in entries.items()}
            payload["expected"] = expected
            payload["bindPrevious"] = bool(bind_previous.get())
            if expected == "" and not self._messagebox.askyesno(
                "Confirm empty expected", "Use an explicit empty-string expected value?", parent=dialog
            ):
                return
            self._async(self.callbacks.add_assertion, payload)
            dialog.destroy()

        self._ttk.Button(dialog, text="Confirm", command=confirm).grid(row=len(fields) + 1, column=1)

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

    def stop(self) -> None:
        self._commands.put(("stop", None))
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)


def tkinter_indicator_factory(
    name: str,
    scope: CaptureScope,
    callbacks: IndicatorCallbacks,
) -> RecordingIndicator:
    return TkRecordingIndicator(name, scope, callbacks)


__all__ = [
    "IndicatorCallbacks", "NullRecordingIndicator", "RecordingIndicator",
    "TkRecordingIndicator", "tkinter_indicator_factory",
]
