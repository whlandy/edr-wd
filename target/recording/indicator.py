"""Target-local visible recording indicator and explicit assertion editor."""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

from .models import CaptureScope, RecordingModelError

# Marker carried in the recorder window's title so every layer — screenshot
# redaction, foreground resolution — can recognise the recorder's own UI.
RECORDER_UI_MARKER = "[recorder_ui=true]"


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
            if callback == "pause":
                self.callbacks.pause()
            elif callback == "resume":
                self.callbacks.resume()
            elif callback == "stop":
                self.callbacks.stop()
            elif callback == "add_assertion":
                self.callbacks.add_assertion(payload or {})

    def start(self) -> None:
        import subprocess

        env = dict(os.environ)
        # The child imports this package; carry the parent's import roots so
        # it works from a source checkout and from an installed package.
        env["PYTHONPATH"] = os.pathsep.join(
            path for path in sys.path if path
        )
        self._process = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "from target.recording.indicator import _indicator_child_main;"
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

    def stop(self) -> None:
        self._send("stop", None)
        process = self._process
        if process is not None:
            try:
                process.wait(timeout=5)
            except Exception:
                pass
        self._terminate()


def _indicator_child_main() -> None:  # pragma: no cover - child process entry
    """Child entry point: own the main thread and run the Tk indicator on it."""
    config = json.loads(sys.stdin.readline())
    scope = CaptureScope.from_dict(config["scope"])

    def emit(kind: str, payload: Any = None) -> None:
        # The parent can exit first — a session that ends for any other reason
        # tears down this pipe while the Tk loop is still alive.  A button
        # press afterwards must not raise out of the Tk callback thread.
        try:
            sys.stdout.write(json.dumps({"callback": kind, "payload": payload}) + "\n")
            sys.stdout.flush()
        except (BrokenPipeError, ValueError):
            pass

    indicator = TkRecordingIndicator(
        config["name"],
        scope,
        IndicatorCallbacks(
            pause=lambda: emit("pause"),
            resume=lambda: emit("resume"),
            stop=lambda: emit("stop"),
            add_assertion=lambda payload: emit("add_assertion", payload),
        ),
    )

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

    threading.Thread(target=announce, daemon=True).start()
    threading.Thread(target=pump, daemon=True).start()
    # Tk owns this process's main thread, which is what macOS requires.
    indicator._run()


def tkinter_indicator_factory(
    name: str,
    scope: CaptureScope,
    callbacks: IndicatorCallbacks,
) -> RecordingIndicator:
    if sys.platform == "darwin":
        return SubprocessTkIndicator(name, scope, callbacks)
    return TkRecordingIndicator(name, scope, callbacks)


__all__ = [
    "IndicatorCallbacks", "NullRecordingIndicator", "RecordingIndicator",
    "SubprocessTkIndicator", "TkRecordingIndicator", "tkinter_indicator_factory",
]
