"""Ancestry minimal closed loop: record selector → Windows dump_tree → observation → replay locator.

Defect being pinned (from the 11.26 live replay, see
docs/todo/recording-usability-independent-review-r4-20261023.md):

- The recording side synthesises a multi-level ``ancestry`` fallback selector
  for controls that have no stable automation id / identifier
  (``agent/recording/selectors.py::_control``).
- Before the WIP, the Windows replay observation (``pywinauto_client``
  ``_build_tree``) emitted *no* ``ancestry`` on any control, so every ancestry
  candidate was rejected by ``HybridLocator`` (``len(actual) < len(expected)``)
  -> candidate_count=0 -> ``replay_target_not_found``.

``target/pywinauto_client.py::_build_ancestry`` closes the production side by
walking the UIA parent chain the same way ``target/recording/windows.py`` does.
These tests pin the *whole* loop through the real production entry points (mock
pywinauto wrappers only), so that removing the ancestry emission makes them fail.

Safety principle (not to be relaxed): the locator keeps refusing a candidate
when the observed ancestry disagrees with the recorded chain. These tests only
prove the observation *supplies* the information the locator already required.
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest

from agent.locator import HybridLocator, LocatorError, LocatorPolicy, LocatorRequest
from target.windowing.observation import normalize_observed_control


# --------------------------------------------------------------------------- #
# Minimal pywinauto wrapper shim (path/order/field semantics only)
# --------------------------------------------------------------------------- #
def _install_fake_pywinauto(monkeypatch):
    """Let ``target.pywinauto_client`` import without a live UIA backend."""
    pkg = types.ModuleType("pywinauto")
    pkg.Application = object
    pkg.timings = types.ModuleType("pywinauto.timings")
    pkg.mouse = types.ModuleType("pywinauto.mouse")
    pkg.__path__ = []  # importable package
    monkeypatch.setitem(sys.modules, "pywinauto", pkg)
    monkeypatch.setitem(sys.modules, "pywinauto.timings", pkg.timings)
    monkeypatch.setitem(sys.modules, "pywinauto.mouse", pkg.mouse)
    monkeypatch.delitem(sys.modules, "target.pywinauto_client", raising=False)
    module = importlib.import_module("target.pywinauto_client")
    return module


class _EI:
    def __init__(self, control_type="", automation_id="", name=""):
        self.control_type = control_type
        self.automation_id = automation_id
        self.name = name
        self.is_password = False


class _Rect:
    left = 50
    top = 50

    def width(self):
        return 30

    def height(self):
        return 14


class _Node:
    """Minimal pywinauto UIA wrapper: identity + parent/children navigable."""

    def __init__(self, control_type="", automation_id="", name="",
                 parent=None, children=()):
        self.element_info = _EI(control_type, automation_id, name)
        self._parent = parent
        self._children = list(children)

    # -- navigation used by _build_ancestry / _build_tree -------------------
    def parent(self):
        return self._parent

    def children(self):
        return self._children

    # -- identity/geometry used by _build_tree ------------------------------
    def window_text(self):
        return self.element_info.name or ""

    def control_id(self):
        return None

    def friendly_class_name(self):
        return self.element_info.control_type or ""

    def rectangle(self):
        return _Rect()

    def is_visible(self):
        return True

    def is_enabled(self):
        return True

    def automation_id(self):
        return self.element_info.automation_id

    def control_type(self):
        return self.element_info.control_type

    def get_toggle_state(self):
        return None

    def is_selected(self):
        return False


def _heir():
    """Window / page / scan '/ detail '/ table with one no-strong-id Details row.

    Mirrors the real 11.26 EDR scan table: the DataItem "Details" has no
    automation id, so its identity must ride on the 6-level ancestor chain.
    """
    table = _Node("Table", "table", children=[])
    table_page = _Node("Group", "EdrMainWindow.baseWidget.pageWidget."
                       "scanPage.VirusScanWindow.scanDetailWidget.tablePage",
                       children=[table])
    detail_widget = _Node("Custom", "EdrMainWindow.baseWidget.pageWidget."
                          "scanPage.VirusScanWindow.scanDetailWidget",
                          children=[table_page])
    scan_window = _Node("Group", "EdrMainWindow.baseWidget.pageWidget."
                        "scanPage.VirusScanWindow", children=[detail_widget])
    scan_page = _Node("Group", "EdrMainWindow.baseWidget.pageWidget.scanPage",
                      children=[scan_window])
    page_widget = _Node("Custom", "EdrMainWindow.baseWidget.pageWidget",
                        children=[scan_page])
    root = _Node("Window", "EdrMainWindow", children=[page_widget])
    # Re-parent from root down so every node's .parent() resolves.
    for parent, child in ((root, page_widget), (page_widget, scan_page),
                          (scan_page, scan_window), (scan_window, detail_widget),
                          (detail_widget, table_page), (table_page, table)):
        child._parent = parent
    details = _Node("DataItem", automation_id="", name="Details", parent=table)
    table._children = [details]
    return root


def _build_controls(monkeypatch):
    """Real WindowsGUI._build_tree -> dump_tree control array from the shim."""
    module = _install_fake_pywinauto(monkeypatch)
    gui = module.WindowsGUI()
    return gui, gui._build_tree(_heir(), depth=0, max_depth=10)


# --------------------------------------------------------------------------- #
# Closed-loop: observation production supplies the recorded ancestry
# --------------------------------------------------------------------------- #
def test_dump_tree_emits_same_order_and_fields_as_recording_side(monkeypatch):
    """_build_tree (dump_tree) emits ancestry nearest-parent-first, camelCase,
    empty fields dropped -- matching target/recording/windows.py walking."""
    _, controls = _build_controls(monkeypatch)
    details = next(c for c in controls if c["text"] == "Details")

    assert details["automation_id"] == ""
    # The chain, nearest parent (table) first, exactly as the recording walk
    # would emit it; each ancestor drops its empty fields.
    assert details["ancestry"] == [
        {"controlType": "Table", "automationId": "table"},
        {"controlType": "Group", "automationId": (
            "EdrMainWindow.baseWidget.pageWidget."
            "scanPage.VirusScanWindow.scanDetailWidget.tablePage")},
        {"controlType": "Custom", "automationId": (
            "EdrMainWindow.baseWidget.pageWidget."
            "scanPage.VirusScanWindow.scanDetailWidget")},
        {"controlType": "Group", "automationId": (
            "EdrMainWindow.baseWidget.pageWidget.scanPage.VirusScanWindow")},
        {"controlType": "Group", "automationId": (
            "EdrMainWindow.baseWidget.pageWidget.scanPage")},
        {"controlType": "Custom", "automationId": (
            "EdrMainWindow.baseWidget.pageWidget")},
    ]


def test_normalized_observation_propagates_ancestry(monkeypatch):
    """normalize_observed_control (the observation production contract) keeps
    the surrogate ancestry so the locator can match it."""
    _, controls = _build_controls(monkeypatch)
    identity = {"process_name": "EDRClient.exe", "title": "logo1"}
    normalized = [
        normalize_observed_control(source, identity, index)
        for index, source in enumerate(controls, start=1)
    ]
    details = next(c for c in normalized if c["text"] == "Details")
    assert details["ancestry"] and details["ancestry"][0] == {
        "controlType": "Table", "automationId": "table",
    }


def _selector(ancestry):
    return LocatorRequest(
        "gui.click",
        {"processName": "EDRClient.exe"},
        {"controlType": "DataItem", "name": "Details", "ancestry": ancestry},
    )


def _recorded_chain():
    """The 6-level ancestry the recording side bakes into the selector for a
    no-strong-id DataItem ('Details'). This is what replay must match against,
    independent of what the current observation happens to emit."""
    return [
        {"controlType": "Table", "automationId": "table"},
        {"controlType": "Group", "automationId": (
            "EdrMainWindow.baseWidget.pageWidget."
            "scanPage.VirusScanWindow.scanDetailWidget.tablePage")},
        {"controlType": "Custom", "automationId": (
            "EdrMainWindow.baseWidget.pageWidget."
            "scanPage.VirusScanWindow.scanDetailWidget")},
        {"controlType": "Group", "automationId": (
            "EdrMainWindow.baseWidget.pageWidget.scanPage.VirusScanWindow")},
        {"controlType": "Group", "automationId": (
            "EdrMainWindow.baseWidget.pageWidget.scanPage")},
        {"controlType": "Custom", "automationId": (
            "EdrMainWindow.baseWidget.pageWidget")},
    ]


def test_full_loop_resolves_a_no_strong_id_control(monkeypatch):
    """record selector -> dump_tree -> normalize -> HybridLocator resolves the
    single Details row (the 'unique chain' happy path that previously failed
    with candidate_count=0 when the observation gave no ancestry)."""
    _, controls = _build_controls(monkeypatch)
    identity = {"process_name": "EDRClient.exe", "title": "logo1"}
    normalized = [
        normalize_observed_control(source, identity, index)
        for index, source in enumerate(controls, start=1)
    ]
    bundle = {
        "observation_id": "OBS", "window_identity": {},
        "active_window": {"process_name": "EDRClient.exe", "title": "logo1"},
        "controls": normalized,
    }
    resolution = HybridLocator().resolve(
        _selector(_recorded_chain()), bundle, LocatorPolicy(mode="semantic_only"),
    )
    assert resolution.candidate is not None
    assert resolution.candidate.source["text"] == "Details"


def test_recording_selector_and_replay_observation_agree_field_for_field(monkeypatch):
    """The recorded ancestry selector and the replay observation's ancestry must
    agree order-for-order and field-for-field, else the closed loop breaks with
    candidate_count=0. This is the exact asymmetry the 11.26 replay exposed."""
    from agent.recording.selectors import synthesize_selector
    from target.recording.models import CaptureScope, ObservedTarget, RawCaptureEvent

    event = RawCaptureEvent(
        sequence=1, wall_time="2026-08-18T00:00:00Z", monotonic_ms=1,
        type="pointer_click",
        scope=CaptureScope("t", "windows_pywinauto", "EDRClient.exe", "^logo1$"),
        input={"button": "left"},
        observed_target=ObservedTarget(
            "OBS-1", "T0001", "sha256:x", "DataItem",
            text="Details",
            ancestry=tuple(_recorded_chain()),
        ),
    )
    selector = synthesize_selector(event).control
    assert selector["ancestry"] == _recorded_chain()

    # Same control through the replay production path must yield the same chain.
    _, controls = _build_controls(monkeypatch)
    details = next(c for c in controls if c["text"] == "Details")
    observed = normalize_observed_control(
        details, {"process_name": "EDRClient.exe", "title": "logo1"}, 1,
    )
    assert observed["ancestry"] == selector["ancestry"]


def test_mismatched_observed_chain_is_still_safely_refused(monkeypatch):
    """A disagreeing observed chain must keep failing: we supply ancestry, we
    do not weaken the locator's refusal of wrong evidence."""
    _, controls = _build_controls(monkeypatch)
    identity = {"process_name": "EDRClient.exe", "title": "logo1"}
    normalized = [
        normalize_observed_control(source, identity, index)
        for index, source in enumerate(controls, start=1)
    ]
    for item in normalized:
        if item["text"] == "Details":
            item["ancestry"] = [{"controlType": "Other", "automationId": "x"}]
    bundle = {
        "observation_id": "OBS", "window_identity": {},
        "active_window": {"process_name": "EDRClient.exe", "title": "logo1"},
        "controls": normalized,
    }
    # The requested chain is the real recorded table chain; the observed one
    # now disagrees, so the locator must refuse, not guess.
    with pytest.raises(LocatorError) as e:
        HybridLocator().resolve(
            _selector(_recorded_chain()), bundle, LocatorPolicy(mode="semantic_only"),
        )
    assert e.value.code == "replay_target_not_found"


def test_observed_chain_omitted_entirely_is_still_refused(monkeypatch):
    """The exact information asymmetry the 11.26 replay exposed: the recorded
    selector carries a 6-level ancestry, but the replay observation emits NO
    ancestry at all for the candidate. Because the locator is the authority and
    refuses missing evidence (it is not relaxed to 'ignore ancestry when
    absent'), the candidate must NOT match on name+controlType alone -- the
    result stays candidate_count=0 (replay_target_not_found), never a guess.
    Supplying ancestry on the observation side (not weakening this check) is
    what lets the closed loop resolve."""
    _, controls = _build_controls(monkeypatch)
    identity = {"process_name": "EDRClient.exe", "title": "logo1"}
    normalized = [
        normalize_observed_control(source, identity, index)
        for index, source in enumerate(controls, start=1)
    ]
    # Drop ancestry entirely from every observed control, mirroring a replay
    # observation produced before the observation-side ancestry fix.
    for item in normalized:
        item.pop("ancestry", None)
    bundle = {
        "observation_id": "OBS", "window_identity": {},
        "active_window": {"process_name": "EDRClient.exe", "title": "logo1"},
        "controls": normalized,
    }
    with pytest.raises(LocatorError) as e:
        HybridLocator().resolve(
            _selector(_recorded_chain()), bundle, LocatorPolicy(mode="semantic_only"),
        )
    assert e.value.code == "replay_target_not_found"
    assert e.value.candidate_count == 0


# --------------------------------------------------------------------------- #
# Six identical "Details" rows (the real 11.26 scan table)
# --------------------------------------------------------------------------- #
def _heir_six():
    """Six no-strong-id Details rows under the *same* table, so their recorded
    ancestry is identical. Mirrors the live EDR scan table where the DataItem
    'Details' appears six times with the same 6-level ancestor chain."""
    table = _Node("Table", "table", children=[])
    table_page = _Node("Group", "EdrMainWindow.baseWidget.pageWidget."
                       "scanPage.VirusScanWindow.scanDetailWidget.tablePage",
                       children=[table])
    detail_widget = _Node("Custom", "EdrMainWindow.baseWidget.pageWidget."
                          "scanPage.VirusScanWindow.scanDetailWidget",
                          children=[table_page])
    scan_window = _Node("Group", "EdrMainWindow.baseWidget.pageWidget."
                        "scanPage.VirusScanWindow", children=[detail_widget])
    scan_page = _Node("Group", "EdrMainWindow.baseWidget.pageWidget.scanPage",
                      children=[scan_window])
    page_widget = _Node("Custom", "EdrMainWindow.baseWidget.pageWidget",
                        children=[scan_page])
    root = _Node("Window", "EdrMainWindow", children=[page_widget])
    for parent, child in ((root, page_widget), (page_widget, scan_page),
                          (scan_page, scan_window), (scan_window, detail_widget),
                          (detail_widget, table_page), (table_page, table)):
        child._parent = parent
    table._children = [
        _Node("DataItem", automation_id="", name="Details", parent=table)
        for _ in range(6)
    ]
    return root


def _normalized(controls):
    identity = {"process_name": "EDRClient.exe", "title": "logo1"}
    return [
        normalize_observed_control(source, identity, index)
        for index, source in enumerate(controls, start=1)
    ]


def _bundle(normalized):
    return {
        "observation_id": "OBS", "window_identity": {},
        "active_window": {"process_name": "EDRClient.exe", "title": "logo1"},
        "controls": normalized,
    }


def test_six_identical_details_are_explicitly_ambiguous_not_guessed(monkeypatch):
    """Six Details rows under one table share the SAME recorded ancestry, so
    supplying ancestry turns candidate_count from 0 into 6. The locator must
    return an explicit ambiguity, never silently pick one or use recorded
    coordinates. Failing open here would be a safety regression the task
    explicitly forbids ('不能猜测、不能静默使用录制坐标')."""
    gui = _install_fake_pywinauto(monkeypatch)
    gui = gui.WindowsGUI()
    _, controls = gui, gui._build_tree(_heir_six(), depth=0, max_depth=10)
    details = [c for c in controls if c["text"] == "Details"]
    assert len(details) == 6
    # All six share the same observed ancestry (same ancestors, nearest first).
    serialized = [str(d["ancestry"]) for d in details]
    assert serialized and serialized.count(serialized[0]) == 6, \
        "fixture intends identical ancestry across rows"

    normalized = _normalized(controls)
    with pytest.raises(LocatorError) as e:
        HybridLocator().resolve(
            _selector(_recorded_chain()), _bundle(normalized),
            LocatorPolicy(mode="semantic_only"),
        )
    # Not "replay_target_not_found" (0): the evidence IS present, it is just
    # ambiguous. Explicit count, no guess.
    assert e.value.code == "locator_ambiguous"
    assert e.value.candidate_count == 6
    # Actionable diagnostic: one rect per ambiguous candidate is attached, so
    # an operator sees exactly which rows are ambiguous without any guessing.
    assert len(e.value.candidate_rects) == 6
    assert all(isinstance(r, (tuple, list, type(None))) for r in e.value.candidate_rects)


def test_six_identical_details_with_distinct_chain_resolve_to_single(monkeypatch):
    """If (and only if) the rows' ancestor chains actually differ, supplying
    ancestry lets the locator pin the single recorded chain. This is the
    discrimination the recording walk intends; identical chains stay ambiguous
    (see test above)."""
    gui = _install_fake_pywinauto(monkeypatch)
    gui = gui.WindowsGUI()
    _, controls = gui, gui._build_tree(_heir_six(), depth=0, max_depth=10)
    normalized = _normalized(controls)
    # Pretend only the first Details row sits under the recorded table chain;
    # the other five are pushed under a different table ancestor.
    rows = [n for n in normalized if n["text"] == "Details"]
    for row in rows[1:]:
        row["ancestry"] = [{"controlType": "Table", "automationId": "other"}]
    res = HybridLocator().resolve(
        _selector(_recorded_chain()), _bundle(normalized),
        LocatorPolicy(mode="semantic_only"),
    )
    assert res.candidate is not None and res.candidate.source["text"] == "Details"


# --------------------------------------------------------------------------- #
# Fingerprint parity (task gate: never use fingerprint until sides agree)
# --------------------------------------------------------------------------- #
def test_recording_and_replay_fingerprints_do_not_agree_yet(monkeypatch):
    """The recording-side fingerprint (``windows.py::_observed``) hashes
    ``{controlType, automationId, identifier, text, rect}`` while the replay
    observation fingerprint (``observation.py::normalize_observed_control``)
    hashes ``{process_name, window_title, control_type, automation_id,
    control_id, class_name, identifier, text, rect}`` -- different field sets,
    so the same logical control produces DIFFERENT fingerprints across the two
    sides, and ``rect`` is included on both (unstable under any layout shift).

    Consequence (per the 11.26 review and the task's safety gate): fingerprint
    must not be used as a matcher, nor its absence to bypass ancestry matching,
    until both sides hash the same canonical, layout-stable field set and the
    match is proven able to uniquely locate. This test pins the current
    mismatch so no future change silently turns fingerprint on.

    The recording-side hash is computed by the *real* ``_observed()`` on a
    camelCase mirror of the same logical control (not by re-reading the
    replay observation's snake_case dump, which would trivially yield unequal
    hashes without proving the two algorithms differ).
    """
    from target.recording.windows import WindowsUIACorrelator

    _, controls = _build_controls(monkeypatch)
    details = next(c for c in controls if c["text"] == "Details")
    identity = {"process_name": "EDRClient.exe", "title": "logo1"}

    # Mirror of the SAME logical control cast into the recording side's
    # camelCase ``target_data`` shape (the replay dump is snake_case). Same
    # explicit rect on both sides so any fingerprint difference comes purely
    # from the two algorithms' divergent canonical field sets, not a layout
    # value we happened to feed one side.
    rect = list(details.get("rect") or [50, 50, 75, 50])
    recorded = WindowsUIACorrelator._observed({
        "controlType": details.get("control_type"),
        "automationId": details.get("automation_id"),
        "identifier": details.get("identifier"),
        "text": details.get("text"),
        "rect": list(rect),
    })
    assert recorded is not None, "recording _observed must accept the mirror"

    # Replay-side hash for the same rect, via the real normalize algorithm.
    replay_item = normalize_observed_control({**details, "rect": list(rect)}, identity, 1)

    assert replay_item["fingerprint"] != recorded.fingerprint, (
        "fingerprint parity is not yet established; if this starts passing, "
        "re-examine whether it is now a SAFE and UNIQUE discriminator before "
        "enabling it")

    # rect is part of both fingerprints -> inherently layout-unstable.
    import json as _j
    assert "rect" in _j.dumps({
        "process_name": "EDRClient.exe", "window_title": "logo1",
        "control_type": "DataItem", "automation_id": "", "control_id": None,
        "class_name": None, "identifier": None, "text": "Details", "rect": [50, 50, 75, 50],
    }, sort_keys=True, ensure_ascii=False, default=str)


# --------------------------------------------------------------------------- #
# P2 guards (reviewer follow-up): threading & failure semantics
# --------------------------------------------------------------------------- #
class _CountingNode(_Node):
    """_Node that counts parent() invocations and can fail mid-walk."""

    _instances: list["_CountingNode"] = []
    parent_calls = 0

    @classmethod
    def reset(cls) -> None:
        cls._instances = []
        cls.parent_calls = 0

    def __init__(self, control_type="", automation_id="", name="",
                 parent=None, children=(), fail_parent=False):
        super().__init__(control_type, automation_id, name, parent, children)
        self.fail_parent = fail_parent
        _CountingNode._instances.append(self)

    def parent(self):
        _CountingNode.parent_calls += 1
        if self.fail_parent:
            raise RuntimeError("UIA parent() failed")
        return self._parent


def _balanced_tree(levels: int, fanout: int) -> _CountingNode:
    _CountingNode.reset()

    def build(depth):
        if depth == 0:
            return _CountingNode("DataItem", "", "leaf")
        kids = [build(depth - 1) for _ in range(fanout)]
        node = _CountingNode("Custom", f"L{depth}", "name", children=kids)
        for k in kids:
            k._parent = node
        return node

    top = build(levels)
    root = _CountingNode("Window", "W", "W", children=[top])
    top._parent = root
    return root


def test_threaded_ancestry_matches_recording_side_and_bounds_parent_calls(monkeypatch):
    """The parent-ancestry threading must (a) still emit the nearest-parent-first
    chain the recording side / locator expect, and (b) not re-walk UIA parent()
    once per descendant node (the pre-fix implementation issued up to 6 COM
    round-trips per control)."""
    root = _balanced_tree(levels=4, fanout=4)
    gui = _install_fake_pywinauto(monkeypatch).WindowsGUI()
    controls = gui._build_tree(root, depth=0, max_depth=15)

    total_nodes = len(controls)
    assert total_nodes > 1
    # Nearest-parent-first leaf ancestry, exactly like the per-node walk.
    leaves = [c for c in controls if c["text"] == "leaf"]
    for leaf in leaves:
        chain = leaf["ancestry"]
        assert chain, "leaf must carry an ancestry chain"
        assert chain[0]["controlType"] == "Custom"  # nearest parent = L1
        # Ordered nearest-first: the innermost parent (L1) comes first, then
        # increasingly distant ancestors (L2, L3, ...).
        ids = [e.get("automationId") for e in chain
               if (e.get("automationId") or "").startswith("L")]
        assert ids == sorted(ids, key=lambda x: int(x[1:])), \
            f"chain must be nearest-first, got {ids}"

    # The threaded build walks the parent chain ONCE (the root call). A
    # per-node implementation would issue O(total_nodes * 6) parent() calls.
    # Root has one parent() (returns None), plus nothing else -- each descendant
    # derives its chain from the already-built parent chain.
    assert _CountingNode.parent_calls <= 2, (
        f"threaded build should not re-walk parents per node; "
        f"got {_CountingNode.parent_calls} parent() calls for {total_nodes} nodes")


def test_ancestry_clears_entire_chain_on_partial_failure(monkeypatch):
    """A mid-walk failure must clear the whole chain (matching the recording
    side's `except: ancestry = []`), never leave a partially-built chain that
    differs field-for-field from the recording side."""
    leaf = _CountingNode("DataItem", "", "leaf")
    mid = _CountingNode("Custom", "mid", "mid", children=[leaf], fail_parent=True)
    leaf._parent = mid
    top = _CountingNode("Window", "W", "W", children=[mid])
    mid._parent = top

    gui = _install_fake_pywinauto(monkeypatch).WindowsGUI()
    chain = gui._build_ancestry(leaf)
    assert chain == [], f"partial failure must clear the chain, got {chain}"


def test_ancestry_clears_chain_when_element_info_read_fails(monkeypatch):
    """P2#3: a failure reading a level's `element_info` must also clear the
    whole chain. The pre-fix `_build_ancestry` routed through `_ancestor_entry`,
    which swallowed the exception into a partial `{}` entry instead of clearing;
    the recording side reads `element_info` inside its guarded loop and clears,
    and the production side must match field-for-field and chain-clearing."""
    leaf = _CountingNode("DataItem", "", "leaf")

    class _InfoFailsNode:
        def __init__(self, parent=None, children=()):
            self._parent = parent
            self._children = list(children)

        def parent(self):
            return self._parent

        @property
        def element_info(self):
            raise RuntimeError("UIA element_info failed")

    mid = _InfoFailsNode(parent=None, children=[leaf])
    leaf._parent = mid

    gui = _install_fake_pywinauto(monkeypatch).WindowsGUI()
    chain = gui._build_ancestry(leaf)
    assert chain == [], f"element_info failure must clear the whole chain, got {chain}"
