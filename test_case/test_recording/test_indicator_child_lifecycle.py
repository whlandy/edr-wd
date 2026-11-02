"""Indicator child-process lifecycle regression: a dead parent must not leave
an always-on-top orphan recorder window (batch 18, task-domains 6+8).

The recorder indicator runs in a child process (SubprocessTkIndicator) because
Aqua Tk must own the main thread.  The child's only shutdown drivers are the
parent's commands on stdin and the parent's round-trip on the Stop button.
Historically both depended on the parent being alive:

- When the parent dies (crash, kill, interpreter teardown), its stdin write
  end closes and the child's pump thread reaches EOF and silently exits —
  while the Tk mainloop keeps running.  The always-on-top recorder window is
  then orphaned forever, and it cannot even be dismissed: the X button only
  reports to the dead pipe.  No parent-side mechanism can help (the lease
  watchdog lives in the dead process); only the child can save itself.
- The Stop/X-button callback reports to the parent and lets the parent drive
  the shutdown.  That is correct while the pipe is alive; with a dead pipe it
  must self-terminate instead.

Contract locked here:

- stdin EOF (parent gone) → the child terminates itself within a bounded
  time, verified against a real child process (honest end-to-end, not a
  source-shape assertion).
- A "stop" command from a LIVE parent still closes the child cleanly (the
  graceful SubprocessTkIndicator.stop path is unchanged).
- The child stop callback self-terminates when the report cannot be delivered
  (emit fails), and does NOT self-terminate when it can — a self-destroy on a
  live pipe would desynchronize the window from a still-running session.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

PROJECT_ROOT = Path(__file__).resolve().parents[2]

CHILD_ENTRY = (
    "from target.recording.indicator import _indicator_child_main; "
    "_indicator_child_main()"
)

SCOPE_CONFIG = {
    "target": "t",
    "backend": "windows_pywinauto",
    "processName": "E.exe",
    "windowTitle": "^E$",
}


def _spawn_child() -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(PROJECT_ROOT)]
        + [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    )
    return subprocess.Popen(
        [sys.executable, "-c", CHILD_ENTRY],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        env=env,
        text=True,
        cwd=str(PROJECT_ROOT),
    )


def _read_ready(process: subprocess.Popen, timeout: float = 20.0) -> dict:
    lines: queue.Queue = queue.Queue()

    def reader() -> None:
        try:
            lines.put(process.stdout.readline())
        except Exception as exc:  # noqa: BLE001 - surfaced via the queue
            lines.put(exc)

    threading.Thread(target=reader, daemon=True).start()
    line = lines.get(timeout=timeout)
    if isinstance(line, Exception):
        raise line
    if not line:
        pytest.fail("indicator child closed stdout before the ready line")
    return json.loads(line)


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.kill()
    for stream in (process.stdin, process.stdout):
        try:
            stream.close()
        except Exception:
            pass
    process.wait(timeout=10)


def test_child_exits_when_parent_stdin_reaches_eof():
    process = _spawn_child()
    try:
        process.stdin.write(
            json.dumps({"name": "review", "scope": SCOPE_CONFIG}) + "\n"
        )
        process.stdin.flush()
        ready = _read_ready(process)
        if ready.get("error"):
            pytest.skip(
                f"tkinter unavailable in this environment: {ready['error']}"
            )
        assert process.poll() is None, (
            "child exited on its own before EOF — cannot verify the contract"
        )
        process.stdin.close()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
            pytest.fail(
                "orphan recorder window: child survived parent stdin EOF "
                "(dead parent left an undismissable always-on-top window)"
            )
    finally:
        _terminate(process)


def test_child_still_exits_on_stop_command_from_live_parent():
    process = _spawn_child()
    try:
        process.stdin.write(
            json.dumps({"name": "review", "scope": SCOPE_CONFIG}) + "\n"
        )
        process.stdin.flush()
        ready = _read_ready(process)
        if ready.get("error"):
            pytest.skip(
                f"tkinter unavailable in this environment: {ready['error']}"
            )
        process.stdin.write(json.dumps({"command": "stop", "value": None}) + "\n")
        process.stdin.flush()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
            pytest.fail(
                "graceful path regression: live parent's stop command no "
                "longer closes the indicator child"
            )
    finally:
        _terminate(process)


def _indicator_without_ui():
    from target.recording.indicator import (
        IndicatorCallbacks,
        TkRecordingIndicator,
    )
    from target.recording.models import CaptureScope

    return TkRecordingIndicator(
        "review",
        CaptureScope("t", "windows_pywinauto", "E.exe", "^E$"),
        IndicatorCallbacks(
            pause=lambda: None,
            resume=lambda: None,
            stop=lambda: None,
            add_assertion=lambda payload: None,
        ),
    )


class _swapped_stdio:
    """Temporarily replace sys.<name>; the real emitter reads sys.stdout live."""

    def __init__(self, module, name: str, replacement) -> None:
        self._module, self._name, self._replacement = module, name, replacement
        self._original = None

    def __enter__(self):
        self._original = getattr(self._module, self._name)
        setattr(self._module, self._name, self._replacement)
        return self

    def __exit__(self, *exc):
        setattr(self._module, self._name, self._original)
        return False


def test_child_stop_callback_self_terminates_when_pipe_is_dead():
    import io

    from target.recording.indicator import _child_callbacks, _child_emit

    indicator = _indicator_without_ui()
    real_emit = _child_emit()
    # Drive the REAL emitter against a write-end-closed pipe: writes fail
    # with ValueError ("I/O operation on closed file"), exactly the
    # dead-parent signal the stop path must honor.  No mock — an emit that
    # lies about delivery must fail this test.
    dead_stdout = io.TextIOWrapper(io.BytesIO())
    dead_stdout.close()
    with _swapped_stdio(sys, "stdout", dead_stdout):
        delivered = real_emit("stop")
    assert delivered is False, "emitter must report delivery failure on a dead pipe"
    callbacks = _child_callbacks(indicator, real_emit)
    with _swapped_stdio(sys, "stdout", dead_stdout):
        callbacks.stop()
    assert indicator._commands.get(timeout=1) == ("stop", None), (
        "a stop that cannot be reported to a dead parent must terminate the "
        "child, otherwise the window is undismissable"
    )


def test_child_stop_callback_reports_to_live_parent_without_self_terminating():
    from target.recording.indicator import _child_callbacks

    indicator = _indicator_without_ui()
    reported: list[str] = []

    def live_emit(kind, payload=None):
        reported.append(kind)
        return True

    callbacks = _child_callbacks(indicator, emit=live_emit)
    callbacks.stop()
    assert reported == ["stop"], "live parent must receive the stop report"
    with pytest.raises(queue.Empty):
        indicator._commands.get(timeout=0.2), (
            "a self-destroy on a live pipe desynchronizes the window from a "
            "still-running session"
        )
