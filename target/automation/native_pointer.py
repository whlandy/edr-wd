"""Checked Windows pointer submission, not a UI-effect verifier.

Caller owns process/window/lease guards. No retry or alternative injection
after a partial send. Fixed-width Win32 fields also make ABI tests portable.
"""
import ctypes
import time


class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_int32), ("y", ctypes.c_int32)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", ctypes.c_int32), ("dy", ctypes.c_int32),
                ("mouseData", ctypes.c_uint32), ("dwFlags", ctypes.c_uint32),
                ("time", ctypes.c_uint32), ("dwExtraInfo", ctypes.c_size_t)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", ctypes.c_uint16), ("wScan", ctypes.c_uint16),
                ("dwFlags", ctypes.c_uint32), ("time", ctypes.c_uint32),
                ("dwExtraInfo", ctypes.c_size_t)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", ctypes.c_uint32), ("wParamL", ctypes.c_uint16),
                ("wParamH", ctypes.c_uint16)]


class _UNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("payload",)
    _fields_ = [("type", ctypes.c_uint32), ("payload", _UNION)]


def _user32():
    api = ctypes.WinDLL("user32", use_last_error=True)
    api.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    api.SetCursorPos.restype = ctypes.c_int
    api.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
    api.GetCursorPos.restype = ctypes.c_int
    api.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]
    api.SendInput.restype = ctypes.c_uint
    return api


def send_left_click(x, y, *, api=None, sleep=time.sleep, last_error=None):
    """Return OS submission facts; ok never means the application reacted."""
    sent = 0
    result = {"ok": False, "method": "SendInput", "x": int(x), "y": int(y),
              "event_dispatched": False, "events_inserted": 0,
              "effect_verified": False, "pointer_moved": False}
    try:
        api = _user32() if api is None else api
        last_error = last_error or getattr(ctypes, "get_last_error", lambda: 0)
        def refused(stage):
            result.update(stage=stage, win32_error=int(last_error()),
                          code="verification_unavailable" if sent else "not_dispatched",
                          error=f"native pointer submission failed at {stage}",
                          outcome="unknown" if sent else "failed")
            return result
        if not api.SetCursorPos(int(x), int(y)):
            return refused("SetCursorPos")
        result["pointer_moved"] = True
        sleep(0.15)
        point = POINT()
        if not api.GetCursorPos(ctypes.byref(point)):
            return refused("GetCursorPos")
        result["cursor"] = {"x": point.x, "y": point.y}
        if (point.x, point.y) != (int(x), int(y)):
            return refused("cursor_position_mismatch")
        for flag in (2, 4):  # LEFTDOWN, LEFTUP; never replay a partial click.
            packet = INPUT(type=0, payload=_UNION(mi=MOUSEINPUT(dwFlags=flag)))
            count = int(api.SendInput(1, ctypes.byref(packet), ctypes.sizeof(INPUT)))
            if count != 1:
                return refused("LEFTDOWN" if flag == 2 else "LEFTUP")
            sent += count
            result.update(events_inserted=sent, event_dispatched=True)
            if flag == 2:
                sleep(0.06)
        result.update(ok=True, code="ok", stage="submitted")
        return result
    except Exception as exc:
        result.update(error=f"{type(exc).__name__}: {exc}", stage="native_exception",
                      outcome="unknown", code="verification_unavailable")
        return result
