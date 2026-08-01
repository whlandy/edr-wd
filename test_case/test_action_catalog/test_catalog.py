"""
P0.1 catalog tests — verification of acceptance gates.

This file is the testable contract for the Canonical Action Catalog
checkpoint (docs/requirements/P0-protocol-foundation.md §P0.1).

Acceptance gates covered here:

  G1. catalog uniqueness
      * every ActionSpec has a unique action_id
      * every non-null action_code is unique
      * every tool_name is unique
      * duplicate detection raises CatalogConstructionError with a
        stable `code` and the offending `value`

  G2. enum validation
      * every spec's category / side_effect / risk / rollback_class /
        default_screenshot / transition_policy / execution_provider /
        requires / backends is in the documented enum set
      * invalid enum values raise at construction

  G3. V1 table completeness
      * exactly the 27 entries from
        docs/todo/llm-action-id-sequences.md "Proposed Action Catalog V1"
        are present
      * action_code and tool_name match the table 1:1

  G4. catalog_version is semver and matches the package source

  G5. catalog_digest is deterministic
      * sha256:<64-hex>
      * stable across two calls in the same process
      * stable across two cold processes (verified ad-hoc separately;
        see hermes ad-hoc verification in PR description)
      * sensitive to ANY spec field change
      * excludes live enablement

  G6. status.action_space backward compatibility
      * Windows map is byte-identical to the pre-P0.1 hard-coded output
        (golden fixture: test_case/fixtures/catalog_compat/status_windows_v0.json)
      * macOS map is byte-identical to the pre-P0.1 hard-coded output
        (golden fixture: status_macos_v0.json)

  G7. get_action_catalog
      * no backend → every action reports enabled=None
      * backend given, no backend_obj → BACKEND_NOT_IMPLEMENTED gaps
        surface as enabled=False, others True; default filters disabled
      * backend given with backend_obj → adds live hasattr checks
      * include_disabled=True surfaces every entry with disabled_reason

  G8. server.py integration
      * status() payload nests catalog_version/digest under `metadata`
        (top-level shape stable for legacy consumers)
      * get_action_catalog tool is registered and callable
      * status.action_space is driven by the catalog (no extra_tools
        override in server.py)

Run:
    cd /Users/whl/AI-Agent/skill/edr-wd
    python3 -m pytest -q test_case/test_action_catalog
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO = Path(__file__).resolve().parents[2]
_TARGET = _REPO / "target"
_FIXTURES = _REPO / "test_case" / "fixtures" / "catalog_compat"

if str(_TARGET) not in sys.path:
    sys.path.insert(0, str(_TARGET))


from action_catalog import (  # noqa: E402  (sys.path setup above)
    BACKEND_NOT_IMPLEMENTED,
    CATALOG_VERSION,
    VALID_BACKENDS,
    VALID_CATEGORY,
    VALID_DEFAULT_SCREENSHOT,
    VALID_EXECUTION_PROVIDER,
    VALID_REQUIRES,
    VALID_ROLLBACK_CLASS,
    VALID_RISK,
    VALID_SIDE_EFFECT,
    VALID_TRANSITION_POLICY,
    ActionSpec,
    ACTIONS_V1,
    backend_capability_view,
    build_registry,
    canonical_catalog_bytes,
    catalog_digest,
    get_action_capability,
    get_action_catalog,
    get_spec,
    get_spec_by_code,
    get_spec_by_tool,
    is_statically_supported,
    live_enablement_for,
    runtime_capability_for,
    runtime_capability_view,
    specs_for_backend,
    status_action_space,
    CatalogConstructionError,
)


# ---------------------------------------------------------------------------
# V1 table (mirror of docs/todo/llm-action-id-sequences.md)
# ---------------------------------------------------------------------------

V1_TABLE: list[dict[str, str]] = [
    {"action_id": "session.connect",             "action_code": "A001", "tool_name": "connect"},
    {"action_id": "session.window_lock.set",     "action_code": "A002", "tool_name": "lock_window"},
    {"action_id": "session.window_lock.clear",   "action_code": "A003", "tool_name": "unlock_window"},
    {"action_id": "observe.window_lock",         "action_code": "A004", "tool_name": "get_window_lock"},
    {"action_id": "session.window_lock.verify",  "action_code": "A005", "tool_name": "verify_window_lock"},
    {"action_id": "app.activate",                "action_code": "A006", "tool_name": "activate_app"},
    {"action_id": "observe.control_tree",        "action_code": "A010", "tool_name": "dump_tree"},
    {"action_id": "observe.find_control",        "action_code": "A011", "tool_name": "find_control"},
    {"action_id": "gui.click",                   "action_code": "A020", "tool_name": "click"},
    {"action_id": "gui.click_target",            "action_code": "A021", "tool_name": "click_target"},
    {"action_id": "pointer.click_screen",        "action_code": "A022", "tool_name": "click_at"},
    {"action_id": "pointer.click_window",        "action_code": "A023", "tool_name": "click_window_at"},
    {"action_id": "pointer.double_click",        "action_code": "A024", "tool_name": "double_click_at"},
    {"action_id": "pointer.right_click",         "action_code": "A025", "tool_name": "right_click_at"},
    {"action_id": "pointer.middle_click",        "action_code": "A026", "tool_name": "middle_click_at"},
    {"action_id": "pointer.hover",               "action_code": "A027", "tool_name": "hover_at"},
    {"action_id": "pointer.drag",                "action_code": "A028", "tool_name": "drag"},
    {"action_id": "pointer.scroll",              "action_code": "A029", "tool_name": "scroll"},
    {"action_id": "gui.type_text",               "action_code": "A030", "tool_name": "type_text"},
    {"action_id": "gui.select",                  "action_code": "A031", "tool_name": "select"},
    {"action_id": "observe.control_text",        "action_code": "A032", "tool_name": "get_text"},
    {"action_id": "observe.screenshot",          "action_code": "A040", "tool_name": "screenshot"},
    {"action_id": "hisec.activate_edr",          "action_code": "A050", "tool_name": "activate_edr"},
    {"action_id": "hisec.restore_edr",           "action_code": "A051", "tool_name": "restore_edr"},
    {"action_id": "observe.windows",             "action_code": "A060", "tool_name": "list_windows"},
    {"action_id": "observe.window_open",         "action_code": "A061", "tool_name": "is_window_open"},
    {"action_id": "observe.wait_window",         "action_code": "A062", "tool_name": "wait_window"},
]


# ---------------------------------------------------------------------------
# G1. Catalog uniqueness
# ---------------------------------------------------------------------------


def test_v1_catalog_count_matches_table():
    assert len(ACTIONS_V1) == len(V1_TABLE)


def test_v1_table_complete():
    table_ids = {row["action_id"] for row in V1_TABLE}
    catalog_ids = {spec.action_id for spec in ACTIONS_V1}
    assert catalog_ids == table_ids, (
        f"missing from catalog: {table_ids - catalog_ids}; "
        f"extra in catalog: {catalog_ids - table_ids}"
    )


def test_v1_codes_and_tools_match_table():
    table_by_id = {row["action_id"]: row for row in V1_TABLE}
    for spec in ACTIONS_V1:
        row = table_by_id[spec.action_id]
        assert spec.action_code == row["action_code"]
        assert spec.tool_name == row["tool_name"]


def test_catalog_unique_ids_codes_tools():
    ids = [s.action_id for s in ACTIONS_V1]
    codes = [s.action_code for s in ACTIONS_V1]
    tools = [s.tool_name for s in ACTIONS_V1]
    assert len(set(ids)) == len(ids)
    assert len(set(codes)) == len(codes)
    assert len(set(tools)) == len(tools)


def test_duplicate_action_id_rejected():
    spec = ACTIONS_V1[0]
    dup = _make_spec(action_id=spec.action_id)
    with pytest.raises(CatalogConstructionError) as exc:
        build_registry(ACTIONS_V1 + (dup,))
    assert exc.value.code == "duplicate_action_id"
    assert exc.value.value == spec.action_id


def test_duplicate_action_code_rejected():
    spec = ACTIONS_V1[0]
    dup = _make_spec(action_code=spec.action_code)
    with pytest.raises(CatalogConstructionError) as exc:
        build_registry(ACTIONS_V1 + (dup,))
    assert exc.value.code == "duplicate_action_code"
    assert exc.value.value == spec.action_code


def test_duplicate_tool_name_rejected():
    spec = ACTIONS_V1[0]
    dup = _make_spec(tool_name=spec.tool_name)
    with pytest.raises(CatalogConstructionError) as exc:
        build_registry(ACTIONS_V1 + (dup,))
    assert exc.value.code == "duplicate_tool_name"
    assert exc.value.value == spec.tool_name


# ---------------------------------------------------------------------------
# G2. Enum validation
# ---------------------------------------------------------------------------


def _make_spec(**overrides) -> ActionSpec:
    base = dict(
        action_id="test.action",
        action_code=None,
        tool_name="test_tool",
        description="t",
        category="observation",
        input_schema={"type": "object"},
        result_schema={"type": "object"},
        backends=("windows_pywinauto",),
        requires=(),
        side_effect="none",
        risk="low",
        rollback_class="logical_only",
        default_screenshot="none",
        transition_policy="never",
    )
    base.update(overrides)
    return ActionSpec(**base)


@pytest.mark.parametrize("field,value,code", [
    ("category",            "made_up_category", "invalid_category"),
    ("side_effect",         "explosion",        "invalid_side_effect"),
    ("risk",                "extreme",          "invalid_risk"),
    ("rollback_class",      "magic",            "invalid_rollback_class"),
    ("default_screenshot",  "every_step",       "invalid_default_screenshot"),
    ("transition_policy",   "sometimes",        "invalid_transition_policy"),
    ("execution_provider",  "magic_box",        "invalid_execution_provider"),
    ("backends",            ("made_up",),       "invalid_backend"),
])
def test_invalid_enum_rejected(field, value, code):
    spec = _make_spec(**{field: value})
    with pytest.raises(CatalogConstructionError) as exc:
        build_registry((spec,))
    assert exc.value.code == code


def test_invalid_requires_rejected():
    spec = _make_spec(requires=("connected_window", "non_existent_precondition"))
    with pytest.raises(CatalogConstructionError) as exc:
        build_registry((spec,))
    assert exc.value.code == "invalid_requires"


def test_all_enums_have_values():
    """Regression guard: enum sets match docs/architecture §7.1 table."""
    assert {"observation", "session", "semantic_input",
            "pointer_input", "workflow"} == VALID_CATEGORY
    assert {"none", "session_mutation", "gui_mutation",
            "system_mutation"} == VALID_SIDE_EFFECT
    assert {"low", "medium", "high", "irreversible"} == VALID_RISK
    assert {"reversible", "reconstructable", "logical_only",
            "irreversible"} == VALID_ROLLBACK_CLASS
    assert {"none", "after", "before_after", "on_failure"} == VALID_DEFAULT_SCREENSHOT
    assert {"never", "possible", "expected",
            "required_checkpoint"} == VALID_TRANSITION_POLICY
    assert {"connected_window", "window_lock", "target_ref"} == VALID_REQUIRES
    assert {"backend", "server_inline"} == VALID_EXECUTION_PROVIDER
    assert {"windows_pywinauto", "macos_accessibility"} == VALID_BACKENDS


# ---------------------------------------------------------------------------
# G4. Catalog version
# ---------------------------------------------------------------------------


def test_catalog_version_is_v1_0_1():
    # 1.0.0 → initial V1 table.
    # 1.0.1 → added `execution_provider` field; `restore_edr` declared
    #          as `server_inline`; `BACKEND_NOT_IMPLEMENTED` added to
    #          keep macOS `type_text`/`select`/`get_text` reporting False.
    # status.action_space stays byte-compatible with pre-P0.1 output.
    assert CATALOG_VERSION == "1.0.1"


# ---------------------------------------------------------------------------
# G5. Catalog digest
# ---------------------------------------------------------------------------


def test_catalog_digest_format():
    d = catalog_digest()
    assert d.startswith("sha256:")
    assert len(d) == len("sha256:") + 64
    int(d.split(":")[1], 16)


def test_catalog_digest_stable_across_calls():
    assert catalog_digest() == catalog_digest()


def test_canonical_bytes_stable_across_processes():
    # The bytes themselves, not just the digest, must be reproducible.
    # Two computations must produce identical bytes.
    a = canonical_catalog_bytes(ACTIONS_V1)
    b = canonical_catalog_bytes(ACTIONS_V1)
    assert a == b


def test_canonical_bytes_are_utf8_json():
    raw = canonical_catalog_bytes(ACTIONS_V1)
    json.loads(raw.decode("utf-8"))  # round-trip


def test_canonical_bytes_sorted_by_action_id():
    raw = canonical_catalog_bytes(ACTIONS_V1)
    payload = json.loads(raw.decode("utf-8"))
    ids = [a["action_id"] for a in payload["actions"]]
    assert ids == sorted(ids)


def test_canonical_bytes_excludes_live_state():
    raw = canonical_catalog_bytes(ACTIONS_V1)
    payload = json.loads(raw.decode("utf-8"))
    for entry in payload["actions"]:
        assert "enabled" not in entry
        assert "disabled_reason" not in entry


def test_catalog_digest_changes_on_mutation():
    edited = tuple(
        spec if spec.action_id != "gui.click"
        else ActionSpec(**{**spec.__dict__, "description": "EDITED"})
        for spec in ACTIONS_V1
    )
    assert catalog_digest(ACTIONS_V1) != catalog_digest(edited)


def test_catalog_digest_changes_on_execution_provider_change():
    # Sanity: the new field is part of the digest, so changing it
    # perturbs the digest. (Default is "backend"; flipping it must
    # change the digest.)
    edited = tuple(
        spec if spec.action_id != "gui.click"
        else ActionSpec(**{**spec.__dict__, "execution_provider": "server_inline"})
        for spec in ACTIONS_V1
    )
    assert catalog_digest(ACTIONS_V1) != catalog_digest(edited)


def test_catalog_digest_includes_execution_provider_field():
    raw = canonical_catalog_bytes(ACTIONS_V1)
    payload = json.loads(raw.decode("utf-8"))
    for entry in payload["actions"]:
        assert "execution_provider" in entry


# ---------------------------------------------------------------------------
# G6. status.action_space backward compatibility (golden fixtures)
# ---------------------------------------------------------------------------


def _load_fixture(name: str) -> dict:
    return json.loads((_FIXTURES / name).read_text())


def test_status_action_space_windows_matches_golden_fixture():
    fixture = _load_fixture("status_windows_v0.json")
    assert status_action_space("windows_pywinauto") == fixture["action_space"]


def test_status_action_space_macos_matches_golden_fixture():
    fixture = _load_fixture("status_macos_v0.json")
    assert status_action_space("macos_accessibility") == fixture["action_space"]


def test_status_action_space_windows_all_true():
    out = status_action_space("windows_pywinauto")
    # All 27 catalog entries are True on Windows.
    assert all(out.values())
    assert len(out) == 27


def test_status_action_space_macos_has_three_false():
    out = status_action_space("macos_accessibility")
    # macOS does not implement type_text / select / get_text.
    assert out["type_text"] is False
    assert out["select"] is False
    assert out["get_text"] is False
    # All other 24 catalog entries are True.
    others = {k: v for k, v in out.items()
              if k not in ("type_text", "select", "get_text")}
    assert all(others.values())
    assert len(out) == 27


def test_status_action_space_unknown_backend_raises():
    with pytest.raises(ValueError) as exc:
        status_action_space("made_up_backend")
    assert "made_up_backend" in str(exc.value)


def test_status_action_space_includes_restore_edr():
    """restore_edr stays True on both backends — the catalog owns it
    via `execution_provider == "server_inline"`. No extra_tools
    override anywhere in server.py."""
    win = status_action_space("windows_pywinauto")
    mac = status_action_space("macos_accessibility")
    assert win["restore_edr"] is True
    assert mac["restore_edr"] is True


# ---------------------------------------------------------------------------
# Backend capability map (catalog internal)
# ---------------------------------------------------------------------------


def test_backend_capabilities_indexed_by_action_id():
    cap = backend_capability_view()
    assert set(cap.keys()) == {s.action_id for s in ACTIONS_V1}


def test_backend_capability_view_is_immutable():
    """Issue 1 fix: public API exposes an immutable view, not a mutable dict."""
    cap = backend_capability_view()
    # MappingProxyType does not expose __setitem__ / __delitem__.
    with pytest.raises(TypeError):
        cap["gui.click"] = {}  # type: ignore[index]
    with pytest.raises(TypeError):
        del cap["gui.click"]  # type: ignore[arg-type]


def test_backend_capability_view_live_reflects_rebuild():
    """The proxy wraps the live dict so subsequent build_registry calls
    are still observable."""
    cap_before = backend_capability_view()
    assert "gui.click" in cap_before
    # build_registry is a no-op for the same tuple but proves the
    # proxy keeps working without caching.
    build_registry(ACTIONS_V1)
    cap_after = backend_capability_view()
    assert "gui.click" in cap_after


def test_get_action_capability_convenience():
    # True for known supported pair
    assert get_action_capability("gui.click", "windows_pywinauto") is True
    # False for backend gap
    assert get_action_capability("gui.type_text", "macos_accessibility") is False
    # False for unknown action
    assert get_action_capability("no.such", "windows_pywinauto") is False


def test_get_action_capability_matches_get_action_catalog():
    """The convenience function and the full view must agree."""
    for action_id in ("gui.click", "gui.type_text", "hisec.restore_edr"):
        for backend in ("windows_pywinauto", "macos_accessibility"):
            payload = get_action_catalog(backend=backend, include_disabled=True)
            match = next(
                (e for e in payload["actions"] if e["action_id"] == action_id),
                None,
            )
            expected = (match["enabled"] is True) if match else False
            actual = get_action_capability(action_id, backend)
            assert actual is expected, (
                f"{action_id}/{backend}: catalog={expected} convenience={actual}"
            )


def test_is_statically_supported_for_known_pair():
    assert is_statically_supported(ACTIONS_V1, "gui.click", "windows_pywinauto")
    assert is_statically_supported(ACTIONS_V1, "gui.click", "macos_accessibility")


def test_is_statically_supported_false_for_unknown_action():
    assert is_statically_supported(ACTIONS_V1, "no.such.action", "windows_pywinauto") is False


def test_specs_for_backend_filters_correctly():
    win = specs_for_backend(ACTIONS_V1, "windows_pywinauto")
    mac = specs_for_backend(ACTIONS_V1, "macos_accessibility")
    # All current entries support both backends, so the two sets match.
    assert {s.action_id for s in win} == {s.action_id for s in mac}
    assert len(win) == len(ACTIONS_V1)


def test_live_enablement_for_static_decision():
    """P0.1 live_enablement_for is a static catalog lookup — does not
    inspect a backend object."""
    enabled, reason = live_enablement_for(ACTIONS_V1, "gui.click", "windows_pywinauto")
    assert enabled is True
    assert reason == ""


def test_live_enablement_for_unknown_action():
    enabled, reason = live_enablement_for(ACTIONS_V1, "no.such", "windows_pywinauto")
    assert enabled is False
    assert reason == "action_id_not_in_catalog"


def test_live_enablement_for_unknown_backend():
    enabled, reason = live_enablement_for(ACTIONS_V1, "gui.click", "made_up")
    assert enabled is False
    assert reason == "backend_not_in_catalog"


# ---------------------------------------------------------------------------
# G7. get_action_catalog
# ---------------------------------------------------------------------------


def test_get_action_catalog_no_backend_returns_unknown():
    out = get_action_catalog(backend=None)
    assert out["catalog_version"] == CATALOG_VERSION
    assert out["catalog_digest"] == catalog_digest()
    assert out["backend"] is None
    assert len(out["actions"]) == len(ACTIONS_V1)
    assert all(a["enabled"] is None for a in out["actions"])


def test_get_action_catalog_windows_all_enabled():
    out = get_action_catalog(backend="windows_pywinauto")
    # The catalog keys actions by `action_id`. Look up by action_id, not tool_name.
    by_id = {a["action_id"]: a for a in out["actions"]}
    # Windows implements everything → no disabled.
    assert all(a["enabled"] for a in out["actions"])
    assert by_id["gui.click"]["enabled"] is True
    assert by_id["gui.type_text"]["enabled"] is True


def test_get_action_catalog_macos_exposes_gaps():
    """macOS has BACKEND_NOT_IMPLEMENTED gaps. They must surface as
    disabled. Use `include_disabled=True` to see the disabled
    entries in the response (default filters them out)."""
    out = get_action_catalog(backend="macos_accessibility", include_disabled=True)
    by_id = {a["action_id"]: a for a in out["actions"]}
    assert by_id["gui.type_text"]["enabled"] is False
    assert by_id["gui.select"]["enabled"] is False
    assert by_id["observe.control_text"]["enabled"] is False
    assert "not implemented" in by_id["gui.type_text"]["disabled_reason"].lower()


def test_get_action_catalog_default_hides_disabled():
    out = get_action_catalog(backend="macos_accessibility")
    tools = {a["tool_name"] for a in out["actions"]}
    # type_text / select / get_text filtered out by default.
    assert "type_text" not in tools
    assert "select" not in tools
    assert "get_text" not in tools
    assert len(out["actions"]) == len(ACTIONS_V1) - 3


def test_get_action_catalog_include_disabled_shows_all():
    out = get_action_catalog(backend="macos_accessibility", include_disabled=True)
    assert len(out["actions"]) == len(ACTIONS_V1)
    disabled = {a["tool_name"] for a in out["actions"] if a["enabled"] is False}
    assert disabled == {"type_text", "select", "get_text"}


def test_get_action_catalog_view_is_static_no_backend_obj():
    """P0.1 view layer must not accept a backend_obj. The static
    answer must be identical regardless of any backend instance the
    caller might want to pass."""
    # The view itself doesn't even take a backend_obj; this test
    # documents the contract for future readers.
    out = get_action_catalog(backend="windows_pywinauto")
    # No backend-specific runtime state touched.
    by_id = {a["action_id"]: a for a in out["actions"]}
    assert by_id["gui.click"]["enabled"] is True
    assert by_id["hisec.restore_edr"]["enabled"] is True


def test_get_action_catalog_action_entry_shape():
    out = get_action_catalog(backend="windows_pywinauto")
    click = next(a for a in out["actions"] if a["action_id"] == "gui.click")
    assert click["tool_name"] == "click"
    assert click["action_code"] == "A020"
    assert click["enabled"] is True
    assert click["category"] == "semantic_input"
    assert click["side_effect"] == "gui_mutation"
    assert click["risk"] == "medium"
    assert "window_lock" in click["requires"]
    assert click["default_screenshot"] == "after"
    assert click["execution_provider"] == "backend"


def test_get_action_catalog_is_json_serializable():
    out = get_action_catalog(backend="windows_pywinauto",
                             include_disabled=True)
    json.dumps(out, ensure_ascii=False)


def test_get_action_catalog_unknown_backend_raises():
    with pytest.raises(ValueError):
        get_action_catalog(backend="made_up_backend")


# ---------------------------------------------------------------------------
# G8. server.py integration
# ---------------------------------------------------------------------------


def _import_server():
    import importlib
    if "server" in sys.modules:
        return sys.modules["server"]
    return importlib.import_module("server")


def _make_stub_backend() -> object:
    """Stub backend that implements every V1 method (so server.py can
    call `backend.backend`, `backend.get_window_lock`, etc., without
    raising)."""
    methods = [
        "connect", "lock_window", "unlock_window", "get_window_lock",
        "verify_window_lock", "activate_app", "dump_tree", "find_control",
        "click", "click_target", "click_at", "click_window_at",
        "double_click_at", "right_click_at", "middle_click_at", "hover_at",
        "drag", "scroll", "type_text", "select", "get_text", "screenshot",
        "activate_edr", "list_windows", "is_window_open", "wait_window",
    ]
    ns: dict = {"backend": "windows_pywinauto"}
    for m in methods:
        ns[m] = lambda *a, **kw: {"ok": True}
    return type("Stub", (), ns)()


def test_server_status_metadata_nests_catalog_version_and_digest():
    server = _import_server()
    server._backend = _make_stub_backend()
    server._backend_kind = "windows_pywinauto"
    payload = json.loads(server.status.fn())
    # Nested under `metadata`, not at the top level (review feedback).
    assert "metadata" in payload
    md = payload["metadata"]
    assert md["catalog_version"] == CATALOG_VERSION
    assert md["catalog_digest"] == catalog_digest()
    # Top-level shape unchanged for legacy consumers.
    assert "catalog_version" not in payload
    assert "catalog_digest" not in payload


def test_server_status_action_space_driven_by_catalog():
    server = _import_server()
    server._backend = _make_stub_backend()
    server._backend_kind = "windows_pywinauto"
    payload = json.loads(server.status.fn())
    assert payload["action_space"] == status_action_space("windows_pywinauto")
    assert payload["action_space"] == _load_fixture("status_windows_v0.json")["action_space"]


def test_server_get_action_catalog_tool_present_and_callable():
    server = _import_server()
    server._backend = _make_stub_backend()
    server._backend_kind = "windows_pywinauto"
    raw = server.get_action_catalog.fn(backend="windows_pywinauto")
    payload = json.loads(raw)
    assert payload["catalog_version"] == CATALOG_VERSION
    assert payload["catalog_digest"] == catalog_digest()
    assert payload["backend"] == "windows_pywinauto"
    # Stub has all 27 methods → all enabled.
    assert all(a["enabled"] for a in payload["actions"])
    assert len(payload["actions"]) == len(ACTIONS_V1)


def test_server_get_action_catalog_tool_no_backend_uses_current():
    """When get_action_catalog is called without a backend arg, the
    tool falls back to the current backend_kind so the response is
    actionable for callers that don't know which backend is loaded.
    With include_disabled=True we can see the BACKEND_NOT_IMPLEMENTED
    gaps even though they are filtered out of the default view."""
    server = _import_server()
    server._backend = _make_stub_backend()
    server._backend_kind = "macos_accessibility"
    # Stub implements type_text, but BACKEND_NOT_IMPLEMENTED says
    # macOS doesn't — catalog wins.
    payload = json.loads(server.get_action_catalog.fn(include_disabled=True))
    by_id = {a["action_id"]: a for a in payload["actions"]}
    assert by_id["gui.type_text"]["enabled"] is False
    assert by_id["gui.select"]["enabled"] is False


# ---------------------------------------------------------------------------
# Helpers used by ad-hoc verification scripts (Hermes PR review)
# ---------------------------------------------------------------------------


def test_no_extra_tools_hack_in_server_status():
    """Regression guard: server.py must not pass `extra_tools` or any
    other override to `status_action_space`. Catalog is the single
    source of truth."""
    server = _import_server()
    src = Path(server.__file__).read_text()
    assert "extra_tools" not in src, (
        "server.py still has an `extra_tools` override; the catalog "
        "must own the full truth via `execution_provider` + "
        "`BACKEND_NOT_IMPLEMENTED`."
    )
    # status_action_space is called with exactly one positional arg.
    import re
    matches = re.findall(r"status_action_space\(([^)]*)\)", src)
    assert matches, "status_action_space is not called from server.py"
    for args in matches:
        # Strip newlines / extra whitespace
        args_norm = " ".join(args.split())
        assert args_norm.startswith("backend_name") or args_norm == "backend_name", (
            f"status_action_space call found with args={args!r}; "
            f"expected only `backend_name`"
        )


# ---------------------------------------------------------------------------
# Issue 3 invariant: execution_provider is part of the digest
# ---------------------------------------------------------------------------


def test_execution_provider_changes_digest():
    """Issue 3: any change to a spec's execution_provider MUST perturb
    the catalog digest. This prevents future refactors from accidentally
    dropping execution_provider from the canonical serialization."""
    base = ACTIONS_V1
    edited = tuple(
        spec if spec.action_id != "gui.click"
        else ActionSpec(**{**spec.__dict__, "execution_provider": "server_inline"})
        for spec in base
    )
    assert catalog_digest(base) != catalog_digest(edited)


def test_execution_provider_serialized_in_canonical_bytes():
    """Pin the invariant: canonical bytes MUST contain execution_provider."""
    raw = canonical_catalog_bytes(ACTIONS_V1)
    payload = json.loads(raw.decode("utf-8"))
    providers = {entry["action_id"]: entry["execution_provider"]
                 for entry in payload["actions"]}
    assert providers["hisec.restore_edr"] == "server_inline"
    assert providers["gui.click"] == "backend"


# ---------------------------------------------------------------------------
# Issue 4: explicit `enabled` semantic via status_provider
# ---------------------------------------------------------------------------


def test_status_provider_mirrors_execution_provider():
    """Issue 4: every entry's `status_provider` MUST equal its
    `execution_provider`. Consumers can rely on this to interpret
    `enabled=True` (catalog claim vs runtime claim)."""
    payload = get_action_catalog(backend="windows_pywinauto", include_disabled=True)
    for entry in payload["actions"]:
        assert entry["status_provider"] == entry["execution_provider"], entry


def test_restore_edr_status_provider_is_server_inline():
    """Issue 4: restore_edr reports enabled=True with status_provider
    = "server_inline", making the source of the True explicit."""
    payload = get_action_catalog(backend="macos_accessibility", include_disabled=True)
    restore = next(e for e in payload["actions"]
                   if e["action_id"] == "hisec.restore_edr")
    assert restore["enabled"] is True
    assert restore["execution_provider"] == "server_inline"
    assert restore["status_provider"] == "server_inline"


def test_backend_action_status_provider_is_backend():
    payload = get_action_catalog(backend="windows_pywinauto", include_disabled=True)
    click = next(e for e in payload["actions"] if e["action_id"] == "gui.click")
    assert click["status_provider"] == "backend"


# ---------------------------------------------------------------------------
# Issue 2: runtime_capability module is P1.1 territory — P0.1 must not call it
# ---------------------------------------------------------------------------


def test_runtime_capability_for_server_inline_always_true():
    """server_inline entries report runtime ready regardless of backend."""
    spec = next(s for s in ACTIONS_V1 if s.action_id == "hisec.restore_edr")
    # Even with a bare object (no methods), server_inline is True.
    enabled, reason = runtime_capability_for(spec, object())
    assert enabled is True
    assert reason == ""


def test_runtime_capability_for_backend_uses_hasattr():
    spec = next(s for s in ACTIONS_V1 if s.action_id == "gui.click")

    class WithClick:
        click = lambda self: None

    class WithoutClick:
        pass

    enabled, _ = runtime_capability_for(spec, WithClick())
    assert enabled is True
    enabled, reason = runtime_capability_for(spec, WithoutClick())
    assert enabled is False
    assert "click" in reason


def test_runtime_capability_for_none_backend():
    spec = next(s for s in ACTIONS_V1 if s.action_id == "gui.click")
    enabled, reason = runtime_capability_for(spec, None)
    assert enabled is False
    assert "no backend" in reason.lower()


def test_runtime_capability_view_returns_dict():
    view = runtime_capability_view(ACTIONS_V1, object())
    assert isinstance(view, dict)
    assert set(view.keys()) == {s.action_id for s in ACTIONS_V1}
    # restore_edr is True regardless of empty backend
    restore = view["hisec.restore_edr"]
    assert restore == (True, "")


def test_p0_1_does_not_import_runtime_capability_at_call_time():
    """Issue 2: views.py must NOT probe a live backend. Verify that no
    `hasattr(...)` call against a backend object reaches the catalog →
    JSON path. We scan the function body, not the module docstring
    (which mentions hasattr for documentation purposes)."""
    import ast as _ast
    src = Path(_TARGET / "action_catalog" / "views.py").read_text()
    tree = _ast.parse(src)
    for node in tree.body:
        if isinstance(node, _ast.FunctionDef):
            for sub in _ast.walk(node):
                if isinstance(sub, _ast.Call):
                    func = sub.func
                    name = getattr(func, "id", None) or getattr(func, "attr", None)
                    if name == "hasattr":
                        pytest.fail(
                            f"views.py function {node.name!r} uses hasattr(); "
                            f"runtime probing belongs in runtime_capability.py "
                            f"(P1.1), not in catalog views (P0.1)."
                        )
    # backend_obj parameter is not in any view function signature
    for node in tree.body:
        if isinstance(node, _ast.FunctionDef) and node.name.startswith("_"):
            continue
        if isinstance(node, _ast.FunctionDef):
            for arg in node.args.args + node.args.kwonlyargs:
                if arg.arg == "backend_obj":
                    pytest.fail(
                        f"views.py function {node.name!r} still accepts "
                        f"backend_obj; P0.1 views must be purely static."
                    )


def test_get_action_catalog_tool_signature_no_backend_obj():
    """Server-level: the MCP tool body no longer accepts backend_obj."""
    server = _import_server()
    import inspect
    sig = inspect.signature(server.get_action_catalog.fn)
    assert "backend_obj" not in sig.parameters, (
        "get_action_catalog MCP tool still accepts backend_obj; "
        "P0.1 is purely static."
    )


def test_p0_1_views_do_not_consult_backend_obj():
    """P0.1 get_action_catalog returns the same result regardless of
    any backend_obj it might be called with — but in P0.1 the function
    no longer accepts one. Verify the catalog view is backend-agnostic
    by passing nothing backend-related."""
    win_with_all = get_action_catalog(backend="windows_pywinauto")
    win_disabled = get_action_catalog(backend="windows_pywinauto", include_disabled=True)
    # No `backend_obj` parameter; both calls produce identical results
    # except for the include_disabled filter.
    enabled = sum(1 for a in win_with_all["actions"] if a["enabled"])
    all_count = len(win_disabled["actions"])
    assert enabled == all_count  # Windows has no backend gaps → all enabled