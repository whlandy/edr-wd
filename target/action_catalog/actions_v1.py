"""
actions_v1.py — V1 catalog entries (27 actions).

Mirrors `docs/todo/llm-action-id-sequences.md` "Proposed Action Catalog V1"
table. This is the only place where the 27 entries are declared; the
registry module imports them and validates uniqueness / enums at
construction.

To add a new action in a future minor version, append a new
`ActionSpec(...)` here. The registry rejects duplicate IDs/codes/tool
names, so a conflict fails fast at module import.

`execution_provider` defaults to `"backend"`; only `restore_edr` is
`"server_inline"` because no backend implements it.
"""

from __future__ import annotations

from typing import Mapping

from .models import ActionSpec


_OBJ: Mapping[str, object] = {"type": "object"}
_OBJ_EMPTY: Mapping[str, object] = {"type": "object", "additionalProperties": False}


ACTIONS_V1: tuple[ActionSpec, ...] = (
    # ---- session (A001-A005) ---------------------------------------------
    ActionSpec(
        action_id="session.connect",
        action_code="A001",
        tool_name="connect",
        description=(
            "Connect to a GUI application by window title (regex), process "
            "name, PID, app name, or bundle id. Must be called before any "
            "connect-required operation."
        ),
        category="session",
        input_schema=_OBJ_EMPTY,
        result_schema=_OBJ_EMPTY,
        backends=("windows_pywinauto", "macos_accessibility"),
        requires=(),
        side_effect="session_mutation",
        risk="low",
        rollback_class="reversible",
        default_screenshot="none",
        transition_policy="possible",
    ),
    ActionSpec(
        action_id="session.window_lock.set",
        action_code="A002",
        tool_name="lock_window",
        description=(
            "Lock subsequent click/drag/scroll actions to the connected or "
            "specified window. Pointer actions verify the active/frontmost "
            "window before executing."
        ),
        category="session",
        input_schema=_OBJ_EMPTY,
        result_schema=_OBJ_EMPTY,
        backends=("windows_pywinauto", "macos_accessibility"),
        requires=("connected_window",),
        side_effect="session_mutation",
        risk="low",
        rollback_class="reversible",
        default_screenshot="none",
        transition_policy="never",
    ),
    ActionSpec(
        action_id="session.window_lock.clear",
        action_code="A003",
        tool_name="unlock_window",
        description="Clear the current target window lock.",
        category="session",
        input_schema=_OBJ_EMPTY,
        result_schema=_OBJ_EMPTY,
        backends=("windows_pywinauto", "macos_accessibility"),
        requires=(),
        side_effect="session_mutation",
        risk="low",
        rollback_class="reversible",
        default_screenshot="none",
        transition_policy="never",
    ),
    ActionSpec(
        action_id="observe.window_lock",
        action_code="A004",
        tool_name="get_window_lock",
        description="Return the current target window lock snapshot, if any.",
        category="observation",
        input_schema=_OBJ_EMPTY,
        result_schema=_OBJ_EMPTY,
        backends=("windows_pywinauto", "macos_accessibility"),
        requires=(),
        side_effect="none",
        risk="low",
        rollback_class="logical_only",
        default_screenshot="none",
        transition_policy="never",
    ),
    ActionSpec(
        action_id="session.window_lock.verify",
        action_code="A005",
        tool_name="verify_window_lock",
        description=(
            "Verify the active/frontmost window still matches the current "
            "lock. If activate=True, the backend may try to bring the locked "
            "window forward once."
        ),
        category="session",
        input_schema=_OBJ_EMPTY,
        result_schema=_OBJ_EMPTY,
        backends=("windows_pywinauto", "macos_accessibility"),
        requires=("window_lock",),
        side_effect="none",
        risk="low",
        rollback_class="logical_only",
        default_screenshot="none",
        transition_policy="never",
    ),
    # ---- application (A006) ----------------------------------------------
    ActionSpec(
        action_id="app.activate",
        action_code="A006",
        tool_name="activate_app",
        description=(
            "Activate (bring to foreground) an application by app_name or "
            "bundle_id."
        ),
        category="workflow",
        input_schema=_OBJ_EMPTY,
        result_schema=_OBJ_EMPTY,
        backends=("windows_pywinauto", "macos_accessibility"),
        requires=(),
        side_effect="gui_mutation",
        risk="low",
        rollback_class="reversible",
        default_screenshot="none",
        transition_policy="possible",
    ),
    # ---- observation tree (A010-A011) ------------------------------------
    ActionSpec(
        action_id="observe.control_tree",
        action_code="A010",
        tool_name="dump_tree",
        description=(
            "Dump the control tree of the connected window. Returns a flat "
            "list of controls with class_name, text, control_id, rectangle, "
            "is_visible, is_enabled."
        ),
        category="observation",
        input_schema=_OBJ_EMPTY,
        result_schema=_OBJ_EMPTY,
        backends=("windows_pywinauto", "macos_accessibility"),
        requires=("connected_window",),
        side_effect="none",
        risk="low",
        rollback_class="logical_only",
        default_screenshot="none",
        transition_policy="never",
    ),
    ActionSpec(
        action_id="observe.find_control",
        action_code="A011",
        tool_name="find_control",
        description=(
            "Find controls/components in the connected window by text, role, "
            "identifier, or title regex."
        ),
        category="observation",
        input_schema=_OBJ_EMPTY,
        result_schema=_OBJ_EMPTY,
        backends=("windows_pywinauto", "macos_accessibility"),
        requires=("connected_window",),
        side_effect="none",
        risk="low",
        rollback_class="logical_only",
        default_screenshot="none",
        transition_policy="never",
    ),
    # ---- semantic input (A020-A021) --------------------------------------
    ActionSpec(
        action_id="gui.click",
        action_code="A020",
        tool_name="click",
        description=(
            "Click a control by control_id, text, class_name, automation_id, "
            "or derived filters. Prefer this for component-tree actions."
        ),
        category="semantic_input",
        input_schema=_OBJ_EMPTY,
        result_schema=_OBJ_EMPTY,
        backends=("windows_pywinauto", "macos_accessibility"),
        requires=("connected_window", "window_lock", "target_ref"),
        side_effect="gui_mutation",
        risk="medium",
        rollback_class="reconstructable",
        default_screenshot="after",
        transition_policy="possible",
        preferred_over=("gui.click_target",),
    ),
    ActionSpec(
        action_id="gui.click_target",
        action_code="A021",
        tool_name="click_target",
        description=(
            "Click the centre of a matched control's screen rectangle. Use "
            "only when click() does not trigger the UI reaction."
        ),
        category="semantic_input",
        input_schema=_OBJ_EMPTY,
        result_schema=_OBJ_EMPTY,
        backends=("windows_pywinauto", "macos_accessibility"),
        requires=("connected_window", "window_lock", "target_ref"),
        side_effect="gui_mutation",
        risk="medium",
        rollback_class="reconstructable",
        default_screenshot="after",
        transition_policy="possible",
        preferred_over=("pointer.click_window",),
    ),
    # ---- pointer input (A022-A029) ---------------------------------------
    ActionSpec(
        action_id="pointer.click_screen",
        action_code="A022",
        tool_name="click_at",
        description="Click absolute screen coordinates (x, y).",
        category="pointer_input",
        input_schema=_OBJ_EMPTY,
        result_schema=_OBJ_EMPTY,
        backends=("windows_pywinauto", "macos_accessibility"),
        requires=("window_lock",),
        side_effect="gui_mutation",
        risk="high",
        rollback_class="reconstructable",
        default_screenshot="after",
        transition_policy="possible",
    ),
    ActionSpec(
        action_id="pointer.click_window",
        action_code="A023",
        tool_name="click_window_at",
        description=(
            "Click window-relative coordinates converted to screen space "
            "using the connected window's rectangle."
        ),
        category="pointer_input",
        input_schema=_OBJ_EMPTY,
        result_schema=_OBJ_EMPTY,
        backends=("windows_pywinauto", "macos_accessibility"),
        requires=("connected_window", "window_lock"),
        side_effect="gui_mutation",
        risk="high",
        rollback_class="reconstructable",
        default_screenshot="after",
        transition_policy="possible",
    ),
    ActionSpec(
        action_id="pointer.double_click",
        action_code="A024",
        tool_name="double_click_at",
        description="Double click absolute screen coordinates (x, y).",
        category="pointer_input",
        input_schema=_OBJ_EMPTY,
        result_schema=_OBJ_EMPTY,
        backends=("windows_pywinauto", "macos_accessibility"),
        requires=("window_lock",),
        side_effect="gui_mutation",
        risk="high",
        rollback_class="reconstructable",
        default_screenshot="after",
        transition_policy="possible",
    ),
    ActionSpec(
        action_id="pointer.right_click",
        action_code="A025",
        tool_name="right_click_at",
        description="Right click absolute screen coordinates (x, y).",
        category="pointer_input",
        input_schema=_OBJ_EMPTY,
        result_schema=_OBJ_EMPTY,
        backends=("windows_pywinauto", "macos_accessibility"),
        requires=("window_lock",),
        side_effect="gui_mutation",
        risk="high",
        rollback_class="reconstructable",
        default_screenshot="after",
        transition_policy="possible",
    ),
    ActionSpec(
        action_id="pointer.middle_click",
        action_code="A026",
        tool_name="middle_click_at",
        description="Middle click absolute screen coordinates (x, y).",
        category="pointer_input",
        input_schema=_OBJ_EMPTY,
        result_schema=_OBJ_EMPTY,
        backends=("windows_pywinauto", "macos_accessibility"),
        requires=("window_lock",),
        side_effect="gui_mutation",
        risk="high",
        rollback_class="reconstructable",
        default_screenshot="after",
        transition_policy="possible",
    ),
    ActionSpec(
        action_id="pointer.hover",
        action_code="A027",
        tool_name="hover_at",
        description="Move the mouse to absolute coordinates (x, y) without clicking.",
        category="pointer_input",
        input_schema=_OBJ_EMPTY,
        result_schema=_OBJ_EMPTY,
        backends=("windows_pywinauto", "macos_accessibility"),
        requires=("window_lock",),
        side_effect="none",
        risk="low",
        rollback_class="logical_only",
        default_screenshot="none",
        transition_policy="never",
    ),
    ActionSpec(
        action_id="pointer.drag",
        action_code="A028",
        tool_name="drag",
        description=(
            "Drag the mouse from (x1, y1) to (x2, y2). Use for sliders, list "
            "reordering, selection rectangles, and drag-and-drop."
        ),
        category="pointer_input",
        input_schema=_OBJ_EMPTY,
        result_schema=_OBJ_EMPTY,
        backends=("windows_pywinauto", "macos_accessibility"),
        requires=("window_lock",),
        side_effect="gui_mutation",
        risk="high",
        rollback_class="reconstructable",
        default_screenshot="after",
        transition_policy="possible",
    ),
    ActionSpec(
        action_id="pointer.scroll",
        action_code="A029",
        tool_name="scroll",
        description="Scroll the mouse wheel by a signed number of clicks.",
        category="pointer_input",
        input_schema=_OBJ_EMPTY,
        result_schema=_OBJ_EMPTY,
        backends=("windows_pywinauto", "macos_accessibility"),
        requires=("window_lock",),
        side_effect="gui_mutation",
        risk="medium",
        rollback_class="reconstructable",
        default_screenshot="none",
        transition_policy="never",
    ),
    # ---- semantic input cont. (A030-A032) --------------------------------
    ActionSpec(
        action_id="gui.type_text",
        action_code="A030",
        tool_name="type_text",
        description=(
            "Type text into an edit control. Finds the control by "
            "control_id, text, or class_name, then sets the text content."
        ),
        category="semantic_input",
        input_schema=_OBJ_EMPTY,
        result_schema=_OBJ_EMPTY,
        backends=("windows_pywinauto", "macos_accessibility"),
        requires=("connected_window", "window_lock", "target_ref"),
        side_effect="gui_mutation",
        risk="medium",
        rollback_class="reversible",
        default_screenshot="after",
        transition_policy="possible",
    ),
    ActionSpec(
        action_id="gui.select",
        action_code="A031",
        tool_name="select",
        description=(
            "Select an item from a combo box (dropdown). Specify the combo "
            "by control_id, text, or class_name. Provide either item text "
            "(str) or zero-based index (int)."
        ),
        category="semantic_input",
        input_schema=_OBJ_EMPTY,
        result_schema=_OBJ_EMPTY,
        backends=("windows_pywinauto", "macos_accessibility"),
        requires=("connected_window", "window_lock", "target_ref"),
        side_effect="gui_mutation",
        risk="medium",
        rollback_class="reversible",
        default_screenshot="after",
        transition_policy="possible",
    ),
    ActionSpec(
        action_id="observe.control_text",
        action_code="A032",
        tool_name="get_text",
        description="Get the text content of a control.",
        category="observation",
        input_schema=_OBJ_EMPTY,
        result_schema=_OBJ_EMPTY,
        backends=("windows_pywinauto", "macos_accessibility"),
        requires=("connected_window", "window_lock", "target_ref"),
        side_effect="none",
        risk="low",
        rollback_class="logical_only",
        default_screenshot="none",
        transition_policy="never",
    ),
    # ---- observation misc (A040) -----------------------------------------
    ActionSpec(
        action_id="observe.screenshot",
        action_code="A040",
        tool_name="screenshot",
        description=(
            "Take a screenshot of the connected window. Returns base64 PNG "
            "image if path is not specified. Set path to save to file instead."
        ),
        category="observation",
        input_schema=_OBJ_EMPTY,
        result_schema=_OBJ_EMPTY,
        backends=("windows_pywinauto", "macos_accessibility"),
        requires=("connected_window",),
        side_effect="none",
        risk="low",
        rollback_class="logical_only",
        default_screenshot="none",
        transition_policy="never",
    ),
    # ---- HiSec workflow (A050-A051) ---------------------------------------
    ActionSpec(
        action_id="hisec.activate_edr",
        action_code="A050",
        tool_name="activate_edr",
        description=(
            "HiSec EDR specific. On Windows, ensure HisecEndpointAgent is "
            "visible, then prefer EDRClient.exe 17 --show and fall back to "
            "clicking edrWidget. By default waits up to 15 s for EDRClient "
            "to appear."
        ),
        category="workflow",
        input_schema=_OBJ_EMPTY,
        result_schema=_OBJ_EMPTY,
        backends=("windows_pywinauto", "macos_accessibility"),
        requires=(),
        side_effect="system_mutation",
        risk="high",
        rollback_class="reconstructable",
        default_screenshot="after",
        transition_policy="expected",
    ),
    # restore_edr is the only action with execution_provider="server_inline":
    # it has no backend implementation; the dispatch lives inline in
    # target/server.py. Catalog still owns the entry, its enablement
    # defaults to True on both backends, and the status.action_space bit.
    ActionSpec(
        action_id="hisec.restore_edr",
        action_code="A051",
        tool_name="restore_edr",
        description=(
            "Restore the EDR window if minimized. Call before "
            "dump_tree/screenshot if the window may be minimized."
        ),
        category="workflow",
        input_schema=_OBJ_EMPTY,
        result_schema=_OBJ_EMPTY,
        backends=("windows_pywinauto", "macos_accessibility"),
        requires=("connected_window",),
        side_effect="session_mutation",
        risk="low",
        rollback_class="reversible",
        default_screenshot="none",
        transition_policy="never",
        execution_provider="server_inline",
    ),
    # ---- observation window set (A060-A062) ------------------------------
    ActionSpec(
        action_id="observe.windows",
        action_code="A060",
        tool_name="list_windows",
        description=(
            "List all top-level windows visible on the desktop. Does NOT "
            "require a prior connect() call."
        ),
        category="observation",
        input_schema=_OBJ_EMPTY,
        result_schema=_OBJ_EMPTY,
        backends=("windows_pywinauto", "macos_accessibility"),
        requires=(),
        side_effect="none",
        risk="low",
        rollback_class="logical_only",
        default_screenshot="none",
        transition_policy="never",
    ),
    ActionSpec(
        action_id="observe.window_open",
        action_code="A061",
        tool_name="is_window_open",
        description=(
            "Check if any window matches the given criteria (title regex, "
            "process name, or class name)."
        ),
        category="observation",
        input_schema=_OBJ_EMPTY,
        result_schema=_OBJ_EMPTY,
        backends=("windows_pywinauto", "macos_accessibility"),
        requires=(),
        side_effect="none",
        risk="low",
        rollback_class="logical_only",
        default_screenshot="none",
        transition_policy="never",
    ),
    ActionSpec(
        action_id="observe.wait_window",
        action_code="A062",
        tool_name="wait_window",
        description=(
            "Poll until a window matching the given criteria appears, or "
            "timeout expires. Default timeout=10 s, interval=0.5 s."
        ),
        category="observation",
        input_schema=_OBJ_EMPTY,
        result_schema=_OBJ_EMPTY,
        backends=("windows_pywinauto", "macos_accessibility"),
        requires=(),
        side_effect="none",
        risk="low",
        rollback_class="logical_only",
        default_screenshot="none",
        transition_policy="never",
    ),
)


__all__ = ["ACTIONS_V1"]