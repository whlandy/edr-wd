"""Portable Win32 ABI/dispatch probes; no live desktop operations."""
import ctypes

import pytest

from target.automation import native_pointer as native


class API:
    def __init__(self, *, set_ok=True, get_ok=True, point=(10, 20), counts=(1, 1)):
        self.set_ok, self.get_ok, self.point = set_ok, get_ok, point
        self.counts = iter(counts)
        self.flags = []

    def SetCursorPos(self, x, y):
        self.requested = (x, y)
        return self.set_ok

    def GetCursorPos(self, ptr):
        ptr._obj.x, ptr._obj.y = self.point
        return self.get_ok

    def SendInput(self, count, ptr, size):
        assert count == 1 and size == ctypes.sizeof(native.INPUT)
        assert ptr._obj.type == 0
        self.flags.append(ptr._obj.mi.dwFlags)
        return next(self.counts)


def run(api):
    return native.send_left_click(10, 20, api=api, sleep=lambda _: None,
                                  last_error=lambda: 5)


def test_windows_input_abi():
    assert ctypes.sizeof(native.POINT) == 8
    assert ctypes.sizeof(native.INPUT) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)


def test_checked_send_is_not_verified_effect():
    api = API()
    result = run(api)
    assert api.flags == [2, 4]
    assert result['ok'] and result['event_dispatched']
    assert result['events_inserted'] == 2
    assert result['effect_verified'] is False
    assert result['method'] == 'SendInput'


@pytest.mark.parametrize('kwargs,stage', [
    ({'set_ok': False}, 'SetCursorPos'),
    ({'get_ok': False}, 'GetCursorPos'),
    ({'point': (9, 20)}, 'cursor_position_mismatch'),
])
def test_failed_position_never_clicks(kwargs, stage):
    api = API(**kwargs)
    result = run(api)
    assert not result['ok'] and not result['event_dispatched']
    assert result['stage'] == stage
    assert api.flags == []


def test_down_rejected_is_not_dispatched():
    api = API(counts=(0,))
    result = run(api)
    assert not result['ok'] and not result['event_dispatched']
    assert result['events_inserted'] == 0 and api.flags == [2]
    assert result['stage'] == 'LEFTDOWN'


def test_up_rejected_remains_partial_unknown_no_retry():
    api = API(counts=(1, 0))
    result = run(api)
    assert not result['ok'] and result['event_dispatched']
    assert result['events_inserted'] == 1 and api.flags == [2, 4]
    assert result['outcome'] == 'unknown'
    from target.scroll.pointer_result import normalize
    envelope = normalize(result, tool='click_at')
    assert envelope['event_dispatched'] and envelope['code'] == 'verification_unavailable'


def test_exception_after_down_preserves_partial_unknown():
    api = API(counts=(1,))
    result = run(api)
    assert not result['ok'] and result['event_dispatched']
    assert result['events_inserted'] == 1 and result['outcome'] == 'unknown'
