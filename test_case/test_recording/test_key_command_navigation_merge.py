# 回归：裸 HOME/END 导航键与同 Edit 后续 text_commit 的编译归并边界。
#
# 契约（见 agent/recording/compiler.py::_navigation_merges）：
# - 录制器的 text_commit 捕获的是物化后的完整字段值，回放主路径
#   set_edit_text 整字段替换、与光标位置无关，因此紧邻其前、同一 Edit 上的
#   裸 HOME/END（无任何修饰键）对回放结果没有可观察影响，编译期归并进该
#   text_commit 步骤，原始捕获事件保留不动，归并记录进 compile-report
#   diagnostics（不静默）。
# - 除此之外的一切 key_command 保持 compile_action_unsupported：带修饰键
#   （SHIFT+HOME 选择语义）、非 HOME/END 键（ENTER 等副作用键）、目标不同、
#   窗口不同、与 text_commit 之间被其他事件隔断、受保护 Edit、无后续
#   text_commit —— 一律不归并，继续 incomplete。
from __future__ import annotations

import pytest

from agent.recording.compiler import compile_recording
from target.recording.models import RawRecording

pytestmark = pytest.mark.unit

_WINDOW = {"title": "EDR-WD Native Recording QA [qa-dsh-fixed-1025]", "processName": "powershell.exe", "handle": 394122}


def _edit_target(sequence: int, *, fingerprint: str = "sha256:edit-input", protected: bool | None = False) -> dict:
    return {
        "snapshotId": f"OBS-{sequence}",
        "targetId": "T0001",
        "fingerprint": fingerprint,
        "controlType": "Edit",
        "automationId": "FixtureInput",
        "text": "QA input",
        "protected": protected,
    }


def _event(sequence: int, kind: str, *, target: dict | None = None, input_: dict | None = None, handle: int | None = 394122) -> dict:
    evidence = {"window": {**_WINDOW, "handle": handle}} if handle is not None else {}
    return {
        "sequence": sequence,
        "wallTime": f"2026-10-24T20:27:{sequence:02d}Z",
        "monotonicMs": 82254921 + sequence * 137,
        "type": kind,
        "scope": {
            "target": "target",
            "backend": "windows_pywinauto",
            "processName": "powershell.exe",
            "windowTitle": "^EDR-WD Native Recording QA \\[qa-dsh-fixed-1025\\]$",
        },
        "input": input_ or {},
        "observedTarget": target if target is not None else _edit_target(sequence),
        "evidence": evidence,
        "causalId": f"cause-{sequence}",
        "assertion": None,
    }


def _recording(*events: dict) -> RawRecording:
    return RawRecording.from_dict({
        "schema": "edr.desktop-recording/v1",
        "sessionId": "REC-MERGE-1",
        "name": "home-end merge",
        "captureDiagnostics": {"droppedPackets": 0, "correlationErrorCount": 0},
        "events": list(events),
    })


def _click(sequence: int, *, handle: int | None = 394122) -> dict:
    return _event(sequence, "pointer_click", input_={"button": "left", "clickCount": 1, "screenPoint": [234, 438]}, handle=handle)


def _key(sequence: int, key: str, *, modifiers: list | None = None, fingerprint: str = "sha256:edit-input", handle: int | None = 394122) -> dict:
    return _event(
        sequence, "key_command",
        target=_edit_target(sequence, fingerprint=fingerprint),
        input_={"key": key, "modifiers": modifiers if modifiers is not None else []},
        handle=handle,
    )


def _commit(sequence: int, value: str, *, fingerprint: str = "sha256:edit-input", protected: bool | None = False, handle: int | None = 394122) -> dict:
    return _event(
        sequence, "text_commit",
        target=_edit_target(sequence, fingerprint=fingerprint, protected=protected),
        input_={"value": value},
        handle=handle,
    )


def test_bare_home_end_before_same_edit_text_commit_merge_and_stay_ready():
    recording = _recording(
        _click(1),
        _key(2, "HOME"),
        _key(3, "END"),
        _commit(4, "baselineaindependent-0426unique0427"),
        _click(5),
    )

    result = compile_recording(recording)

    assert result.issues == ()
    assert result.golden.status == "ready"
    assert [step.action_id for step in result.case.steps] == ["gui.click", "gui.type_text", "gui.click"]
    committed = result.case.steps[1]
    assert committed.args["text"] == "baselineaindependent-0426unique0427"
    # 归并必须可审计：compile-report diagnostics 记录被归并的原始事件。
    merged = result.diagnostics.get("mergedNavigationKeyEvents")
    assert merged == [
        {"sequence": 2, "key": "HOME", "mergedIntoSequence": 4},
        {"sequence": 3, "key": "END", "mergedIntoSequence": 4},
    ]
    # 原始捕获一个事件都不能少：归并只发生在编译期。
    assert len(recording.events) == 5


@pytest.mark.parametrize("modifiers", [["SHIFT"], ["CTRL"], ["ALT"]])
def test_home_end_with_any_modifier_stays_unsupported(modifiers):
    recording = _recording(
        _click(1),
        _key(2, "HOME", modifiers=modifiers),
        _key(3, "END", modifiers=modifiers),
        _commit(4, "typed-value"),
    )

    result = compile_recording(recording)

    assert result.golden.status == "incomplete"
    assert {x.code for x in result.issues} == {"compile_action_unsupported"}
    assert {x.sequence for x in result.issues} == {2, 3}
    assert "mergedNavigationKeyEvents" not in result.diagnostics


@pytest.mark.parametrize("key", ["ENTER", "TAB", "ESCAPE", "DELETE", "BACKSPACE", "LEFT", "UP"])
def test_side_effect_and_unlisted_keys_stay_unsupported(key):
    recording = _recording(
        _click(1),
        _key(2, key),
        _commit(3, "typed-value"),
    )

    result = compile_recording(recording)

    assert result.golden.status == "incomplete"
    assert {x.code for x in result.issues} == {"compile_action_unsupported"}
    assert {x.sequence for x in result.issues} == {2}


def test_navigation_without_following_text_commit_stays_unsupported():
    recording = _recording(
        _click(1),
        _key(2, "HOME"),
        _key(3, "END"),
        _click(4),
    )

    result = compile_recording(recording)

    assert result.golden.status == "incomplete"
    assert {x.code for x in result.issues} == {"compile_action_unsupported"}
    assert {x.sequence for x in result.issues} == {2, 3}


def test_navigation_on_different_control_than_commit_stays_unsupported():
    recording = _recording(
        _click(1),
        _key(2, "HOME", fingerprint="sha256:edit-a"),
        _commit(3, "typed-value", fingerprint="sha256:edit-b"),
    )

    result = compile_recording(recording)

    assert result.golden.status == "incomplete"
    assert {x.code for x in result.issues} == {"compile_action_unsupported"}
    assert {x.sequence for x in result.issues} == {2}


def test_navigation_in_different_window_than_commit_stays_unsupported():
    recording = _recording(
        _click(1),
        _key(2, "HOME", handle=111111),
        _commit(3, "typed-value", handle=394122),
    )

    result = compile_recording(recording)

    assert result.golden.status == "incomplete"
    assert {x.code for x in result.issues} == {"compile_action_unsupported"}
    assert {x.sequence for x in result.issues} == {2}


def test_intervening_event_breaks_adjacency_and_stays_unsupported():
    recording = _recording(
        _click(1),
        _key(2, "HOME"),
        _click(3),
        _commit(4, "typed-value"),
    )

    result = compile_recording(recording)

    assert result.golden.status == "incomplete"
    assert {x.code for x in result.issues} == {"compile_action_unsupported"}
    assert {x.sequence for x in result.issues} == {2}


@pytest.mark.parametrize("protected", [True, None])
def test_navigation_before_protected_or_unknown_commit_stays_unsupported(protected):
    # 捕获模型层面受保护/未知保护目标禁止明文 value（recording_secret_plaintext），
    # 合法形态是环境变量 textSource；无论哪种形态，导航键都不得归并。
    recording = _recording(
        _click(1),
        _key(2, "HOME"),
        _event(
            3, "text_commit",
            target=_edit_target(3, protected=protected),
            input_={"textSource": {"kind": "env", "name": "QA_SECRET"}},
        ),
    )

    result = compile_recording(recording)

    assert result.golden.status == "incomplete"
    assert "compile_action_unsupported" in {x.code for x in result.issues}
    assert "mergedNavigationKeyEvents" not in result.diagnostics


def test_trailing_navigation_at_end_of_recording_stays_unsupported():
    recording = _recording(
        _click(1),
        _commit(2, "typed-value"),
        _key(3, "END"),
    )

    result = compile_recording(recording)

    assert result.golden.status == "incomplete"
    assert {x.code for x in result.issues} == {"compile_action_unsupported"}
    assert {x.sequence for x in result.issues} == {3}


def test_step_event_projection_stays_aligned_after_merge():
    # artifacts 投影按 zip(result.case.steps, events_for_compiled_steps(...))
    # 对齐步骤与源事件；归并后投影必须同样跳过被归并的导航键事件，
    # 否则 type_text 步会拿到 HOME 事件的证据帧（错位即静默污染 selector）。
    from agent.recording.compiler import events_for_compiled_steps

    recording = _recording(
        _click(1),
        _key(2, "HOME"),
        _key(3, "END"),
        _commit(4, "baselineaindependent-0426unique0427"),
        _click(5),
    )

    aligned = events_for_compiled_steps(recording.events)

    assert [event.sequence for event in aligned] == [1, 4, 5]
    assert [event.type for event in aligned] == ["pointer_click", "text_commit", "pointer_click"]


def test_mid_run_key_on_different_control_blocks_whole_run():
    # 连续段中每个导航事件都必须逐一与 commit 同控件；段内任一成员
    # 身份不符则整段不得归并（段中间隔断了前键与 commit 的相邻性）。
    recording = _recording(
        _click(1),
        _key(2, "HOME", fingerprint="sha256:edit-input"),
        _key(3, "END", fingerprint="sha256:other"),
        _commit(4, "typed-value", fingerprint="sha256:edit-input"),
    )

    result = compile_recording(recording)

    assert result.golden.status == "incomplete"
    assert {x.code for x in result.issues} == {"compile_action_unsupported"}
    assert {x.sequence for x in result.issues} == {2, 3}
    assert "mergedNavigationKeyEvents" not in result.diagnostics


def test_mid_run_key_in_different_window_blocks_whole_run():
    recording = _recording(
        _click(1),
        _key(2, "HOME", handle=394122),
        _key(3, "END", handle=999),
        _commit(4, "typed-value", handle=394122),
    )

    result = compile_recording(recording)

    assert result.golden.status == "incomplete"
    assert {x.code for x in result.issues} == {"compile_action_unsupported"}
    assert {x.sequence for x in result.issues} == {2, 3}
    assert "mergedNavigationKeyEvents" not in result.diagnostics


@pytest.mark.parametrize("commit_handle", [394122, None])
def test_missing_window_handle_is_never_identity(commit_handle):
    # 缺失的窗口 handle 不得用 None==None 冒充同窗口身份。
    recording = _recording(
        _click(1, handle=None),
        _key(2, "HOME", handle=None),
        _commit(3, "typed-value", handle=commit_handle),
    )

    result = compile_recording(recording)

    assert result.golden.status == "incomplete"
    assert {x.code for x in result.issues} == {"compile_action_unsupported"}
    assert {x.sequence for x in result.issues} == {2}
    assert "mergedNavigationKeyEvents" not in result.diagnostics


def test_navigation_on_unknown_protection_target_stays_unsupported():
    # 保护状态只在 commit 侧已知还不够：导航事件自身保护未知同样不得归并。
    nav_target = _edit_target(2, fingerprint="sha256:edit-input")
    nav_target["protected"] = None
    recording = _recording(
        _click(1),
        _event(2, "key_command", target=nav_target, input_={"key": "HOME", "modifiers": []}),
        _commit(3, "typed-value", fingerprint="sha256:edit-input"),
    )

    result = compile_recording(recording)

    assert result.golden.status == "incomplete"
    assert "compile_action_unsupported" in {x.code for x in result.issues}
    assert "mergedNavigationKeyEvents" not in result.diagnostics


def test_navigation_on_non_edit_control_stays_unsupported():
    # 导航事件自身也必须是 Edit；即使指纹与 commit 相同，控件类型不符不归并。
    nav_target = _edit_target(2, fingerprint="sha256:edit-input")
    nav_target["controlType"] = "Button"
    recording = _recording(
        _click(1),
        _event(2, "key_command", target=nav_target, input_={"key": "END", "modifiers": []}),
        _commit(3, "typed-value", fingerprint="sha256:edit-input"),
    )

    result = compile_recording(recording)

    assert result.golden.status == "incomplete"
    assert "compile_action_unsupported" in {x.code for x in result.issues}
    assert "mergedNavigationKeyEvents" not in result.diagnostics
