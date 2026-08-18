from __future__ import annotations

import threading

import pytest

from target.recording.models import (
    CaptureScope,
    ObservedTarget,
    RawCaptureEvent,
    RecordingModelError,
)
from target.recording.source import CompositeHookDriver, HookPacket, QueuedCaptureSource
from target.recording.windows import WindowsUIACorrelator
from target.recording.session import CaptureSessionState, RecordingSession

pytestmark = pytest.mark.unit


SCOPE = CaptureScope("win-dev", "windows_pywinauto", "EDRClient.exe", "^EDRClient$")


class _FakeDriver:
    def __init__(self) -> None:
        self.emit = None
        self.stopped = False
        self.stop_called = threading.Event()

    def start(self, emit) -> None:
        self.emit = emit

    def stop(self) -> None:
        self.stopped = True
        self.stop_called.set()


class _BlockingCorrelator:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def correlate(self, packet, scope, sequence):
        self.entered.set()
        self.release.wait(timeout=2)
        return RawCaptureEvent(
            sequence=sequence,
            wall_time=packet.wall_time,
            monotonic_ms=packet.monotonic_ms,
            type="pointer_click",
            scope=scope,
            input={"button": "left"},
        )


class _DroppingCorrelator:
    def correlate(self, packet, scope, sequence):
        if packet.kind == "drop":
            return None
        return RawCaptureEvent(
            sequence=sequence,
            wall_time=packet.wall_time,
            monotonic_ms=packet.monotonic_ms,
            type="pointer_click",
            scope=scope,
            input={},
        )


class _FlushCorrelator:
    def correlate(self, packet, scope, sequence):
        return None

    def flush(self, scope, sequence):
        return RawCaptureEvent(
            sequence=sequence,
            wall_time="2026-08-17T00:00:01.000Z",
            monotonic_ms=1000,
            type="text_commit",
            scope=scope,
            input={"value": "final-value"},
            observed_target=ObservedTarget(
                "OBS-FLUSH", "T0001", "sha256:edit", "Edit", "username",
                protected=False,
            ),
        )


class _Resolver:
    def __init__(self, *, process="EDRClient.exe", title="EDRClient", element=None) -> None:
        self.process = process
        self.title = title
        self.hit_tests = 0
        self.element = element or {
            "controlType": "Button",
            "automationId": "btnApply",
            "text": "应用",
            "rect": [10, 20, 110, 60],
            "protected": False,
        }

    def foreground(self):
        return {"processName": self.process, "windowTitle": self.title, "pid": 42}

    def cursor_position(self):
        return (50, 40)

    def element_at(self, x, y):
        self.hit_tests += 1
        return dict(self.element)

    def refresh(self, target):
        return dict(self.element)

    def focused(self):
        return dict(self.element)


def _packet(kind="pointer_up", monotonic_ms=100, button="left"):
    return HookPacket(
        kind=kind,
        monotonic_ms=monotonic_ms,
        wall_time="2026-08-17T00:00:00.000Z",
        screen_point=(50, 40),
        button=button,
    )


def test_native_callback_only_enqueues_while_slow_correlation_runs_on_worker():
    driver = _FakeDriver()
    correlator = _BlockingCorrelator()
    captured = []
    source = QueuedCaptureSource(
        scope=SCOPE,
        sink=lambda event: captured.append(event) or True,
        driver=driver,
        correlator=correlator,
    )
    source.start()

    # This is the native callback path.  It returns even though correlation is
    # blocked on another thread.
    driver.emit(_packet())
    assert correlator.entered.wait(timeout=1)
    assert captured == []
    correlator.release.set()
    source.stop()

    assert driver.stopped is True
    assert [event.sequence for event in captured] == [1]


def test_session_stop_releases_lock_and_drains_pre_stop_hook_packets():
    driver = _FakeDriver()
    correlator = _BlockingCorrelator()
    session = RecordingSession("REC-1", "flow", SCOPE)
    source = QueuedCaptureSource(
        scope=SCOPE,
        sink=session.ingest,
        driver=driver,
        correlator=correlator,
    )
    session.attach_source(source)
    session.start()
    driver.emit(_packet())
    assert correlator.entered.wait(timeout=1)
    result = {}

    def stop_session():
        result["receipt"], result["recording"] = session.stop()

    stopper = threading.Thread(target=stop_session)
    stopper.start()
    assert driver.stop_called.wait(timeout=1)
    correlator.release.set()
    stopper.join(timeout=2)

    assert not stopper.is_alive()
    assert result["receipt"].state is CaptureSessionState.STOPPED
    assert [event.sequence for event in result["recording"].events] == [1]


def test_worker_originated_lease_stop_cannot_deadlock_when_queue_is_full():
    class _Clock:
        value = 1.0

        def __call__(self):
            return self.value

    clock = _Clock()
    driver = _FakeDriver()
    correlator = _BlockingCorrelator()
    session = RecordingSession(
        "REC-1", "flow", SCOPE, lease_seconds=1.0, monotonic=clock,
    )
    source = QueuedCaptureSource(
        scope=SCOPE,
        sink=session.ingest,
        driver=driver,
        correlator=correlator,
        queue_size=1,
    )
    session.attach_source(source)
    session.start()
    driver.emit(_packet(monotonic_ms=100))
    assert correlator.entered.wait(timeout=1)
    driver.emit(_packet(monotonic_ms=200))  # fills the sole pending slot
    clock.value = 3.0
    correlator.release.set()

    source._worker.join(timeout=2)

    assert not source._worker.is_alive()
    assert driver.stopped is True
    assert session.status().state is CaptureSessionState.STOPPED
    assert session.status().reason == "recording_lease_expired"


def test_queue_overflow_is_persisted_in_raw_capture_diagnostics():
    driver = _FakeDriver()
    correlator = _BlockingCorrelator()
    session = RecordingSession("REC-1", "flow", SCOPE)
    source = QueuedCaptureSource(
        scope=SCOPE,
        sink=session.ingest,
        driver=driver,
        correlator=correlator,
        queue_size=1,
    )
    session.attach_source(source)
    session.start()
    driver.emit(_packet(monotonic_ms=100))
    assert correlator.entered.wait(timeout=1)
    driver.emit(_packet(monotonic_ms=200))
    driver.emit(_packet(monotonic_ms=300))
    assert source.dropped_packets == 1
    correlator.release.set()

    _, recording = session.stop()

    assert recording.capture_diagnostics == {
        "droppedPackets": 1,
        "correlationErrorCount": 0,
        "outOfScopeEvents": 0,
    }


def test_session_stop_flushes_the_final_focused_text_commit_before_stopping():
    driver = _FakeDriver()
    session = RecordingSession("REC-1", "flow", SCOPE)
    source = QueuedCaptureSource(
        scope=SCOPE,
        sink=session.ingest,
        driver=driver,
        correlator=_FlushCorrelator(),
    )
    session.attach_source(source)
    session.start()

    _, recording = session.stop()

    assert [event.type for event in recording.events] == ["text_commit"]
    assert recording.events[0].input == {"value": "final-value"}


def test_dropped_packets_do_not_create_sequence_gaps():
    driver = _FakeDriver()
    captured = []
    source = QueuedCaptureSource(
        scope=SCOPE,
        sink=lambda event: captured.append(event) or True,
        driver=driver,
        correlator=_DroppingCorrelator(),
    )
    source.start()
    driver.emit(_packet("drop", 100))
    driver.emit(_packet("pointer_up", 200))
    source.stop()
    assert [event.sequence for event in captured] == [1]


def test_windows_uia_correlator_rejects_out_of_scope_before_hit_testing():
    resolver = _Resolver(process="Other.exe")
    event = WindowsUIACorrelator(resolver).correlate(_packet(), SCOPE, 1)
    assert event is None
    assert resolver.hit_tests == 0


def test_windows_uia_correlator_builds_click_with_semantic_target():
    resolver = _Resolver()
    event = WindowsUIACorrelator(resolver, double_click_ms=650).correlate(
        _packet(), SCOPE, 1,
    )
    assert event is not None
    assert event.type == "pointer_click"
    assert event.input["screenPoint"] == [50, 40]
    assert event.observed_target.automation_id == "btnApply"
    assert event.observed_target.protected is False
    assert event.evidence == {
        "foregroundPid": 42, "doubleClickIntervalMs": 650,
        "window": {"title": "EDRClient", "processName": "EDRClient.exe"},
    }


def test_windows_correlator_groups_only_a_same_target_double_click_causally():
    correlator = WindowsUIACorrelator(_Resolver(), double_click_ms=650)
    first = correlator.correlate(_packet(monotonic_ms=100), SCOPE, 1)
    second = correlator.correlate(_packet(monotonic_ms=350), SCOPE, 2)

    assert first.causal_id == second.causal_id


def test_assertion_preselection_performs_a_fresh_scoped_pointer_hit_test():
    resolver = _Resolver()
    source = QueuedCaptureSource(
        scope=SCOPE,
        sink=lambda _event: True,
        driver=_FakeDriver(),
        correlator=WindowsUIACorrelator(resolver),
    )

    observed = source.current_observed_target()

    assert observed.automation_id == "btnApply"
    assert resolver.hit_tests == 1


def test_assertion_preselection_never_hit_tests_an_out_of_scope_window():
    resolver = _Resolver(process="Other.exe")
    source = QueuedCaptureSource(
        scope=SCOPE,
        sink=lambda _event: True,
        driver=_FakeDriver(),
        correlator=WindowsUIACorrelator(resolver),
    )

    assert source.current_observed_target() is None
    assert resolver.hit_tests == 0


def test_windows_uia_correlator_emits_toggle_state_instead_of_duplicate_click():
    resolver = _Resolver(element={
        "controlType": "CheckBox",
        "automationId": "backupEnabled",
        "text": "启用备份",
        "rect": [10, 20, 110, 60],
        "protected": False,
        "toggleState": True,
    })
    event = WindowsUIACorrelator(resolver).correlate(_packet(), SCOPE, 1)
    assert event.type == "toggle_change"
    assert event.input == {"value": True}


def test_windows_uia_correlator_does_not_turn_right_click_on_checkbox_into_toggle():
    resolver = _Resolver(element={
        "controlType": "CheckBox",
        "automationId": "backupEnabled",
        "text": "启用备份",
        "rect": [10, 20, 110, 60],
        "protected": False,
        "toggleState": True,
    })

    event = WindowsUIACorrelator(resolver).correlate(
        _packet(button="right"), SCOPE, 1,
    )

    assert event.type == "pointer_click"
    assert event.input["button"] == "right"


def test_windows_uia_correlator_redacts_protected_text_at_source():
    resolver = _Resolver(element={
        "controlType": "Edit",
        "automationId": "password",
        "text": "密码",
        "rect": [10, 20, 110, 60],
        "protected": True,
        # A buggy platform wrapper may expose this key.  It must not escape.
        "value": "do-not-persist",
    })
    correlator = WindowsUIACorrelator(resolver)
    first = correlator.correlate(_packet(), SCOPE, 1)
    assert first.type == "pointer_click"
    assert correlator.correlate(HookPacket(
        kind="text_activity",
        monotonic_ms=150,
        wall_time="2026-08-17T00:00:00.050Z",
    ), SCOPE, 2) is None
    committed = correlator.correlate(HookPacket(
        kind="key_command",
        monotonic_ms=200,
        wall_time="2026-08-17T00:00:00.100Z",
        key="ENTER",
    ), SCOPE, 2)
    assert isinstance(committed, tuple)
    text_event, key_event = committed
    assert text_event.type == "text_commit"
    assert text_event.input == {
        "textSource": {"kind": "env", "name": "EDR_WD_SECRET_2"}
    }
    assert "do-not-persist" not in str(text_event.to_dict())
    assert key_event.sequence == 3


def test_clicking_through_an_unchanged_edit_does_not_create_text_commit():
    resolver = _Resolver(element={
        "controlType": "Edit", "automationId": "username", "text": "用户名",
        "rect": [10, 20, 210, 60], "protected": False, "value": "alice",
    })
    correlator = WindowsUIACorrelator(resolver)
    assert correlator.correlate(_packet(monotonic_ms=100), SCOPE, 1).type == "pointer_click"
    resolver.element = {
        "controlType": "Button", "automationId": "btnApply", "text": "应用",
        "rect": [10, 80, 110, 120], "protected": False,
    }

    event = correlator.correlate(_packet(monotonic_ms=200), SCOPE, 2)

    assert not isinstance(event, tuple)
    assert event.type == "pointer_click"


def test_keyboard_space_on_focused_checkbox_emits_toggle_not_text_activity():
    resolver = _Resolver(element={
        "controlType": "CheckBox", "automationId": "backupEnabled",
        "text": "启用备份", "rect": [10, 20, 210, 60],
        "protected": False, "toggleState": True,
    })
    correlator = WindowsUIACorrelator(resolver)

    event = correlator.correlate(HookPacket(
        kind="key_command",
        monotonic_ms=100,
        wall_time="2026-08-17T00:00:00.000Z",
        key="SPACE",
    ), SCOPE, 1)

    assert event.type == "toggle_change"
    assert event.input == {"value": True}


def test_keyboard_edit_command_marks_focused_text_dirty_without_persisting_clipboard():
    resolver = _Resolver(element={
        "controlType": "Edit", "automationId": "username", "text": "用户名",
        "rect": [10, 20, 210, 60], "protected": False, "value": "pasted-value",
    })
    correlator = WindowsUIACorrelator(resolver)

    assert correlator.correlate(HookPacket(
        kind="key_command",
        monotonic_ms=100,
        wall_time="2026-08-17T00:00:00.000Z",
        key="V",
        modifiers=("CTRL",),
    ), SCOPE, 1) is None
    committed = correlator.flush(SCOPE, 1)

    assert committed.input == {"value": "pasted-value"}
    assert "clipboard" not in str(committed.to_dict()).lower()


def test_text_activity_tracks_focused_edit_without_recording_raw_keys_and_flushes_on_stop():
    resolver = _Resolver(element={
        "controlType": "Edit",
        "automationId": "username",
        "text": "用户名",
        "rect": [10, 20, 210, 60],
        "protected": False,
        "value": "alice",
    })
    correlator = WindowsUIACorrelator(resolver)
    activity = HookPacket(
        kind="text_activity",
        monotonic_ms=100,
        wall_time="2026-08-17T00:00:00.000Z",
    )

    assert correlator.correlate(activity, SCOPE, 1) is None
    committed = correlator.flush(SCOPE, 1)

    assert committed.type == "text_commit"
    assert committed.input == {"value": "alice"}
    assert activity.key is None


class _PositionalResolver(_Resolver):
    """Return a different control depending on where the pointer is."""

    def __init__(self, elements) -> None:
        super().__init__()
        self.elements = elements

    def element_at(self, x, y):
        self.hit_tests += 1
        for (left, top, right, bottom), element in self.elements:
            if left <= x <= right and top <= y <= bottom:
                return dict(element)
        return None


def _pointer(kind, point, monotonic_ms, button="left"):
    return HookPacket(
        kind=kind,
        monotonic_ms=monotonic_ms,
        wall_time="2026-08-17T00:00:00.000Z",
        screen_point=point,
        button=button,
    )


def _slider_resolver():
    return _PositionalResolver([
        (
            (100, 100, 140, 140),
            {
                "controlType": "Thumb", "automationId": "sliderThumb",
                "text": "阈值", "rect": [100, 100, 140, 140], "protected": False,
            },
        ),
        (
            (300, 200, 400, 240),
            {
                "controlType": "Slider", "automationId": "sliderTrack",
                "text": "", "rect": [300, 200, 400, 240], "protected": False,
            },
        ),
    ])


def test_a_press_alone_records_nothing_until_its_release_classifies_it():
    correlator = WindowsUIACorrelator(_slider_resolver(), drag_threshold=(4, 4))
    assert correlator.correlate(_pointer("pointer_down", (110, 130), 100), SCOPE, 1) is None


def test_press_and_release_beyond_the_drag_threshold_records_a_drag_commit():
    correlator = WindowsUIACorrelator(_slider_resolver(), drag_threshold=(4, 4))
    correlator.correlate(_pointer("pointer_down", (110, 130), 100), SCOPE, 1)
    event = correlator.correlate(_pointer("pointer_up", (305, 210), 500), SCOPE, 1)

    assert event.type == "drag_commit"
    assert event.observed_target.automation_id == "sliderThumb"
    assert event.input["screenPoint"] == [110, 130]
    assert event.input["endPoint"] == [305, 210]
    assert event.input["durationSeconds"] == 0.4
    assert event.input["endTarget"]["automationId"] == "sliderTrack"
    assert event.input["endTarget"]["rect"] == [300, 200, 400, 240]
    # The release target's identity must stay free of observation-local IDs.
    assert "snapshotId" not in event.input["endTarget"]
    assert "targetId" not in event.input["endTarget"]


def test_a_release_within_the_drag_threshold_stays_an_ordinary_click():
    correlator = WindowsUIACorrelator(_slider_resolver(), drag_threshold=(4, 4))
    correlator.correlate(_pointer("pointer_down", (110, 130), 100), SCOPE, 1)
    event = correlator.correlate(_pointer("pointer_up", (113, 132), 180), SCOPE, 1)

    assert event.type == "pointer_click"
    assert "endTarget" not in event.input


def test_a_release_with_a_different_button_than_the_press_is_not_a_drag():
    correlator = WindowsUIACorrelator(_slider_resolver(), drag_threshold=(4, 4))
    correlator.correlate(_pointer("pointer_down", (110, 130), 100, button="left"), SCOPE, 1)
    event = correlator.correlate(_pointer("pointer_up", (305, 210), 500, button="right"), SCOPE, 1)

    assert event.type == "pointer_click"


def test_a_drag_onto_an_unresolvable_release_point_keeps_an_explicit_null_end_target():
    correlator = WindowsUIACorrelator(_slider_resolver(), drag_threshold=(4, 4))
    correlator.correlate(_pointer("pointer_down", (110, 130), 100), SCOPE, 1)
    event = correlator.correlate(_pointer("pointer_up", (900, 900), 500), SCOPE, 1)

    assert event.type == "drag_commit"
    assert event.input["endTarget"] is None


def _transition(kind, monotonic_ms, *, process="EDRClient.exe", title="策略详情", pid=42):
    return HookPacket(
        kind="window_transition",
        monotonic_ms=monotonic_ms,
        wall_time="2026-08-17T00:00:00.000Z",
        native={"kind": kind, "processName": process, "title": title, "pid": pid},
    )


def _click_then_transition(packet, *, transition_window_ms=3000):
    correlator = WindowsUIACorrelator(
        _Resolver(), double_click_ms=650, transition_window_ms=transition_window_ms,
    )
    click = correlator.correlate(_packet(monotonic_ms=100), SCOPE, 1)
    return correlator, click, correlator.correlate(packet, SCOPE, 2)


def test_a_window_opening_after_a_click_is_bound_to_that_click():
    _, click, event = _click_then_transition(_transition("opened", 700))

    assert event.type == "window_transition"
    assert event.causal_id == click.causal_id
    assert event.input["kind"] == "opened"
    assert event.input["title"] == "策略详情"
    assert event.input["processName"] == "EDRClient.exe"
    # The wait is derived from the latency the recording actually observed.
    assert event.input["timeoutSeconds"] == 5.0


def test_a_slow_window_gets_a_proportionally_longer_replay_timeout():
    _, _, event = _click_then_transition(_transition("opened", 2600))
    assert event.input["timeoutSeconds"] == 7.5


def test_a_window_transition_no_recent_action_explains_is_not_recorded():
    correlator = WindowsUIACorrelator(_Resolver())
    assert correlator.correlate(_transition("opened", 700), SCOPE, 1) is None


def test_a_window_transition_outside_the_correlation_window_is_not_recorded():
    _, _, event = _click_then_transition(
        _transition("opened", 9000), transition_window_ms=3000,
    )
    assert event is None


def test_a_transition_belonging_to_another_process_is_not_recorded():
    _, _, event = _click_then_transition(_transition("opened", 700, process="Other.exe"))
    assert event is None


def test_a_dialog_title_outside_the_recording_scope_still_binds():
    """A click almost always opens a window whose title differs from the scope."""
    _, click, event = _click_then_transition(
        _transition("opened", 700, title="确认删除"),
    )
    assert event is not None
    assert event.input["title"] == "确认删除"
    assert event.causal_id == click.causal_id


def test_a_window_close_records_a_closed_transition():
    _, click, event = _click_then_transition(_transition("closed", 500))
    assert event.input["kind"] == "closed"
    assert event.causal_id == click.causal_id


class _FailingDriver:
    def __init__(self) -> None:
        self.stopped = False

    def start(self, emit) -> None:
        raise RecordingModelError("recording_permission_missing", "no permission")

    def stop(self) -> None:
        self.stopped = True


def test_composite_driver_starts_every_driver_with_one_emitter():
    first, second = _FakeDriver(), _FakeDriver()
    composite = CompositeHookDriver(first, second)
    sink = lambda packet: None

    composite.start(sink)

    assert first.emit is sink and second.emit is sink


def test_composite_driver_stops_started_drivers_when_a_later_one_fails():
    started, failing = _FakeDriver(), _FailingDriver()
    composite = CompositeHookDriver(started, failing)

    with pytest.raises(RecordingModelError):
        composite.start(lambda packet: None)

    assert started.stopped is True


def test_composite_driver_stops_every_driver_even_after_a_failure():
    class _RaisingStop(_FakeDriver):
        def stop(self):
            super().stop()
            raise RuntimeError("unhook failed")

    raising, healthy = _RaisingStop(), _FakeDriver()
    composite = CompositeHookDriver(healthy, raising)
    composite.start(lambda packet: None)

    with pytest.raises(RuntimeError):
        composite.stop()

    assert healthy.stopped is True and raising.stopped is True


def _log_center_resolver():
    """Foreground reports whichever window the caller last selected."""

    class _Switching(_Resolver):
        def __init__(self):
            super().__init__()
            self.window_title = "EDRClient"

        def foreground(self):
            return {
                "processName": self.process,
                "windowTitle": self.window_title,
                "pid": 42,
            }

    return _Switching()


def _record_click(correlator, resolver, title, ms):
    resolver.window_title = title
    return correlator.correlate(_packet(monotonic_ms=ms), SCOPE, 1)


def test_input_in_a_window_the_recording_opened_stays_in_scope():
    """A click that opens a dialog must not orphan everything done inside it."""
    resolver = _log_center_resolver()
    correlator = WindowsUIACorrelator(resolver)

    opening_click = _record_click(correlator, resolver, "EDRClient", 100)
    transition = correlator.correlate(_transition("opened", 300, title="日志中心"), SCOPE, 2)
    inside = _record_click(correlator, resolver, "日志中心", 500)

    assert opening_click.type == "pointer_click"
    assert transition.causal_id == opening_click.causal_id
    assert inside is not None
    assert inside.type == "pointer_click"
    assert correlator.out_of_scope_events == 0


def test_a_window_closing_removes_it_from_the_derived_scope_again():
    resolver = _log_center_resolver()
    correlator = WindowsUIACorrelator(resolver)

    _record_click(correlator, resolver, "EDRClient", 100)
    correlator.correlate(_transition("opened", 300, title="日志中心"), SCOPE, 2)
    assert _record_click(correlator, resolver, "日志中心", 500) is not None

    _record_click(correlator, resolver, "EDRClient", 700)
    correlator.correlate(_transition("closed", 900, title="日志中心"), SCOPE, 4)

    assert _record_click(correlator, resolver, "日志中心", 1100) is None
    assert correlator.out_of_scope_events == 1


def test_an_unrelated_window_of_the_same_process_stays_out_of_scope():
    resolver = _log_center_resolver()
    correlator = WindowsUIACorrelator(resolver)

    assert _record_click(correlator, resolver, "关于", 100) is None
    assert correlator.out_of_scope_events == 1


def test_out_of_scope_input_is_counted_rather_than_silently_dropped():
    resolver = _log_center_resolver()
    correlator = WindowsUIACorrelator(resolver)

    for index in range(3):
        _record_click(correlator, resolver, "另一个窗口", 100 + index)

    assert correlator.out_of_scope_events == 3


def test_a_window_opening_on_its_own_is_not_a_step_but_is_still_in_scope():
    """Nothing the user did explains it, so it is not a recorded action.

    The scope still follows it: they may click inside it next, and dropping
    that input is how a recording ends up empty.
    """
    resolver = _log_center_resolver()
    correlator = WindowsUIACorrelator(resolver)

    assert correlator.correlate(_transition("opened", 300, title="弹窗"), SCOPE, 1) is None
    assert _record_click(correlator, resolver, "弹窗", 500) is not None
    assert correlator.out_of_scope_events == 0


def _wheel(monotonic_ms, delta=-120, point=(619, 520)):
    return HookPacket(
        kind="scroll",
        monotonic_ms=monotonic_ms,
        wall_time="2026-08-18T00:00:00.000Z",
        screen_point=point,
        native={"delta": delta},
    )


def _feed(correlator, packets, scope=SCOPE):
    """Drive the correlator the way the queue worker does, tracking sequence."""
    produced, sequence = [], 1
    for packet in packets:
        result = correlator.correlate(packet, scope, sequence)
        events = result if isinstance(result, tuple) else () if result is None else (result,)
        for event in events:
            assert event.sequence == sequence, "correlator broke sequence contiguity"
            produced.append(event)
            sequence += 1
    return produced, sequence


def test_a_wheel_gesture_persists_nothing_until_it_ends():
    """The step counter must not climb while one flick is still arriving."""
    correlator = WindowsUIACorrelator(_Resolver())
    produced, _ = _feed(correlator, [_wheel(100 + i * 16) for i in range(19)])
    assert produced == []


def test_the_gesture_is_recorded_once_when_the_next_input_arrives():
    correlator = WindowsUIACorrelator(_Resolver())
    packets = [_wheel(100 + i * 16, delta=-120) for i in range(19)]
    packets.append(_packet("pointer_up", monotonic_ms=2000))

    produced, _ = _feed(correlator, packets)

    assert [e.type for e in produced] == ["scroll_commit", "pointer_click"]
    scroll = produced[0]
    assert scroll.input["delta"] == -120 * 19
    assert scroll.input["notches"] == 19
    assert scroll.input["screenPoint"] == [619, 520]


def test_a_gesture_still_running_at_stop_is_flushed():
    correlator = WindowsUIACorrelator(_Resolver())
    _feed(correlator, [_wheel(100), _wheel(116)])

    flushed = correlator.flush(SCOPE, 1)

    events = flushed if isinstance(flushed, tuple) else (flushed,)
    assert [e.type for e in events] == ["scroll_commit"]
    assert events[0].input["notches"] == 2


def test_reversing_direction_records_the_first_gesture_and_starts_another():
    correlator = WindowsUIACorrelator(_Resolver())
    produced, _ = _feed(correlator, [
        _wheel(100, delta=-120), _wheel(116, delta=-120),
        _wheel(132, delta=120), _wheel(148, delta=120),
    ])
    assert [e.input["delta"] for e in produced] == [-240]
    flushed = correlator.flush(SCOPE, 3)
    assert flushed.input["delta"] == 240


def test_scrolling_elsewhere_records_the_first_gesture():
    correlator = WindowsUIACorrelator(_Resolver())
    produced, _ = _feed(correlator, [
        _wheel(100, point=(619, 520)), _wheel(116, point=(619, 520)),
        _wheel(132, point=(200, 100)),
    ])
    assert len(produced) == 1
    assert produced[0].input["screenPoint"] == [619, 520]


def test_a_long_pause_ends_the_gesture():
    correlator = WindowsUIACorrelator(_Resolver(), )
    produced, _ = _feed(correlator, [_wheel(100), _wheel(5000)])
    assert len(produced) == 1
    assert produced[0].input["notches"] == 1


def test_content_moving_under_the_pointer_does_not_split_the_gesture():
    """Hit testing returns a different row as the list scrolls; irrelevant."""
    rows = [
        {"controlType": "DataItem", "automationId": "", "text": f"日志 {i}",
         "rect": [0, 0, 100, 20], "protected": False}
        for i in range(4)
    ]

    class _Scrolling(_Resolver):
        def __init__(self):
            super().__init__()
            self.index = 0

        def element_at(self, x, y):
            element = rows[min(self.index, len(rows) - 1)]
            self.index += 1
            return dict(element)

    correlator = WindowsUIACorrelator(_Scrolling())
    produced, _ = _feed(correlator, [_wheel(100 + i * 16) for i in range(4)])

    assert produced == []
    assert correlator.flush(SCOPE, 1).input["notches"] == 4


class _SeedResolver(_Resolver):
    """Reports the application's already-open windows."""

    def __init__(self, windows):
        super().__init__()
        self._windows = windows
        self.window_title = "EDRClient"

    def foreground(self):
        return {"processName": self.process, "windowTitle": self.window_title, "pid": 42}

    def windows(self):
        return self._windows


def test_a_window_already_open_when_recording_starts_is_in_scope():
    """A live capture lost 102 of 103 events to exactly this gap."""
    resolver = _SeedResolver([
        {"title": "EDRClient", "pid": 42, "processName": "EDRClient.exe"},
        {"title": "日志中心", "pid": 42, "processName": "EDRClient.exe"},
    ])
    correlator = WindowsUIACorrelator(resolver)

    seeded = correlator.seed_scope(SCOPE)

    assert set(seeded) == {"EDRClient", "日志中心"}
    assert _record_click(correlator, resolver, "日志中心", 100) is not None
    assert correlator.out_of_scope_events == 0


def test_seeding_admits_only_the_scoped_application():
    resolver = _SeedResolver([
        {"title": "日志中心", "pid": 42, "processName": "EDRClient.exe"},
        {"title": "logo1", "pid": 60, "processName": "HiSecEndpointAgent.exe"},
        {"title": "记事本", "pid": 70, "processName": "Notepad.exe"},
    ])
    correlator = WindowsUIACorrelator(resolver)
    assert correlator.seed_scope(SCOPE) == ("日志中心",)


def test_a_resolver_that_cannot_enumerate_windows_seeds_nothing():
    correlator = WindowsUIACorrelator(_Resolver())
    assert correlator.seed_scope(SCOPE) == ()


def test_the_capture_source_seeds_the_scope_before_hooks_start():
    resolver = _SeedResolver([
        {"title": "日志中心", "pid": 42, "processName": "EDRClient.exe"},
    ])
    correlator = WindowsUIACorrelator(resolver)
    driver = _FakeDriver()
    source = QueuedCaptureSource(
        scope=SCOPE, sink=lambda event: True, driver=driver, correlator=correlator,
    )

    source.start()
    try:
        assert source.seeded_scope == ("日志中心",)
    finally:
        source.stop()


class _BackendResolver(WindowsUIACorrelator):
    pass


def test_scope_seeding_reads_the_backend_window_list():
    """The backend's enumeration is the one proven to work on a live target."""
    from target.recording.windows import WindowsUIAResolver

    class _Backend:
        def list_windows(self):
            return {"ok": True, "windows": [
                {"title": "logo1", "process_id": 5948},
                {"title": "日志中心", "process_id": 5948},
            ]}

    resolver = WindowsUIAResolver(_Backend())
    listed = resolver.windows()

    assert [w["title"] for w in listed] == ["logo1", "日志中心"]
    assert all(w["pid"] == 5948 for w in listed)


def test_a_failing_window_enumeration_is_reported_not_silently_empty():
    """An empty seed with no reason is what dropped 41 of 42 live events."""
    class _Failing:
        def windows(self):
            raise RuntimeError("UIA enumeration unavailable")

    correlator = WindowsUIACorrelator(_Failing())

    assert correlator.seed_scope(SCOPE) == ()
    assert "UIA enumeration unavailable" in correlator.scope_seed_error


def test_a_resolver_without_enumeration_says_so():
    correlator = WindowsUIACorrelator(_Resolver())
    assert correlator.seed_scope(SCOPE) == ()
    assert correlator.scope_seed_error == "resolver cannot enumerate windows"


def test_a_successful_seed_records_no_error():
    resolver = _SeedResolver([
        {"title": "日志中心", "pid": 42, "processName": "EDRClient.exe"},
    ])
    correlator = WindowsUIACorrelator(resolver)
    assert correlator.seed_scope(SCOPE) == ("日志中心",)
    assert correlator.scope_seed_error is None
