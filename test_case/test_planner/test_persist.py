"""P3.1 acceptance gate — Commit D: persist + sanitisation + audit.

FR-P3.1-09 / acceptance #6: persisted plan events contain no
configured secret string.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
for p in (str(_REPO / "target"), str(_REPO / "agent")):
    if p not in sys.path:
        sys.path.insert(0, p)


from agent.redaction import (  # noqa: E402
    RedactionAuditReport,
    RedactionRegistry,
    default_registry,
)
from agent.trace.redaction import TextRule  # noqa: E402
from planner.persist import (  # noqa: E402
    PLAN_EVENT_TYPES,
    PLANNER_EVENT_SCHEMA_VERSION,
    PlanEvent,
    PlanEventType,
    audit_persisted_plans,
    build_plan_event,
)


# ---------------------------------------------------------------------------
# FR-P3.1-09 / acceptance #6: no secret in persisted payload
# ---------------------------------------------------------------------------


def test_acceptance_6_persisted_plan_audit_no_secrets():
    """Acceptance #6: persisted plan events contain no configured
    secret string."""
    secrets = [
        "alice@example.com",     # email rule
        "AKIA1234567890ABCDEF",   # aws_access_key_id rule
        "555-123-4567",           # phone rule (after we add it)
    ]
    registry = default_registry()
    for s in secrets:
        ev = build_plan_event(
            PlanEventType.PLAN_CREATED,
            plan_id="P-secret",
            snapshot_id="OBS-secret",
            payload={"args": {"selector": f"Send to {s}"},
                     "description": s},
            registry=registry,
        )
        as_json = json.dumps(ev.to_dict())
        assert s not in as_json, (
            f"secret {s!r} leaked into persisted event: {as_json}"
        )


def test_default_registry_handles_email_and_aws():
    registry = default_registry()
    text = "alice@example.com uses AKIAIOSFODNN7EXAMPLE"
    red, ids = registry.apply_text(text)
    assert "alice@example.com" not in red
    assert "AKIAIOSFODNN7EXAMPLE" not in red
    assert "email" in ids
    assert "aws_access_key_id" in ids


# ---------------------------------------------------------------------------
# D16 schema_version
# ---------------------------------------------------------------------------


def test_plan_created_schema_version_locked():
    ev = build_plan_event(
        PlanEventType.PLAN_CREATED, plan_id="P",
        snapshot_id="X", payload={},
    )
    assert ev.schema_version == "plan_created.v1"


def test_plan_validated_schema_version_locked():
    ev = build_plan_event(
        PlanEventType.PLAN_VALIDATED, plan_id="P",
        snapshot_id="X", payload={}, validation_errors=(),
    )
    assert ev.schema_version == "plan_validated.v1"


def test_plan_rejected_schema_version_locked():
    ev = build_plan_event(
        PlanEventType.PLAN_REJECTED, plan_id="P",
        snapshot_id="X", payload={},
        validation_errors=({"code": "plan_schema_invalid"},),
    )
    assert ev.schema_version == "plan_rejected.v1"


def test_replan_created_schema_version_locked():
    ev = build_plan_event(
        PlanEventType.REPLAN_CREATED, plan_id="P",
        snapshot_id="X", payload={},
        replan_id="RP-1", replan_count=2,
    )
    assert ev.schema_version == "replan_created.v1"


def test_event_type_set_is_complete():
    """All four V1 event types are registered."""
    assert PLAN_EVENT_TYPES == frozenset({
        "plan_created", "plan_validated",
        "plan_rejected", "replan_created",
    })


def test_unknown_event_type_rejected():
    with pytest.raises(ValueError, match="unknown event_type"):
        build_plan_event(
            "bogus_event", plan_id="P",
            snapshot_id="X", payload={},
        )


# ---------------------------------------------------------------------------
# Replan id / count propagation
# ---------------------------------------------------------------------------


def test_replan_event_carries_replan_id():
    ev = build_plan_event(
        PlanEventType.REPLAN_CREATED, plan_id="P",
        snapshot_id="X", payload={"replan_reason": "stale target"},
        replan_id="RP-99",
        replan_count=3,
    )
    d = ev.to_dict()
    assert d["replan_id"] == "RP-99"
    assert d["replan_count"] == 3


def test_non_replan_event_omits_replan_fields():
    """plan_created/validated/rejected MUST NOT carry replan
    fields (only `replan_created` does)."""
    for et in (
        PlanEventType.PLAN_CREATED,
        PlanEventType.PLAN_VALIDATED,
        PlanEventType.PLAN_REJECTED,
    ):
        ev = build_plan_event(
            et, plan_id="P", snapshot_id="X", payload={},
            replan_id="RP-1",  # ignored
        )
        d = ev.to_dict()
        assert "replan_id" not in d


# ---------------------------------------------------------------------------
# Validation errors propagation
# ---------------------------------------------------------------------------


def test_plan_rejected_carries_validation_errors():
    errs = (
        {"code": "plan_schema_invalid", "pointer": "/foo"},
        {"code": "dependency_cycle", "cycle": ["A", "B", "A"]},
    )
    ev = build_plan_event(
        PlanEventType.PLAN_REJECTED, plan_id="P",
        snapshot_id="X", payload={}, validation_errors=errs,
    )
    d = ev.to_dict()
    assert d["validation_errors"] == list(errs)


def test_plan_validated_with_no_errors_omits_field():
    ev = build_plan_event(
        PlanEventType.PLAN_VALIDATED, plan_id="P",
        snapshot_id="X", payload={}, validation_errors=(),
    )
    d = ev.to_dict()
    assert "validation_errors" not in d


# ---------------------------------------------------------------------------
# D7 field-type contract: numeric values preserved
# ---------------------------------------------------------------------------


def test_numeric_payload_fields_preserved():
    """D7: numeric values MUST NOT be modified by redaction."""
    ev = build_plan_event(
        PlanEventType.PLAN_CREATED,
        plan_id="P", snapshot_id="X",
        payload={
            "args": {
                "x": 42,
                "y": 3.14,
                "z": -1,
                "flag": True,
                "nothing": None,
            },
        },
    )
    args = ev.to_dict()["payload"]["args"]
    assert args["x"] == 42
    assert args["y"] == 3.14
    assert args["z"] == -1
    assert args["flag"] is True
    assert args["nothing"] is None


def test_nested_dict_and_list_walked():
    ev = build_plan_event(
        PlanEventType.PLAN_CREATED,
        plan_id="P", snapshot_id="X",
        payload={
            "args": {
                "items": [
                    "alice@example.com",
                    "safe text",
                ],
            },
        },
    )
    items = ev.to_dict()["payload"]["args"]["items"]
    assert items[0] == "[REDACTED]"
    assert items[1] == "safe text"


# ---------------------------------------------------------------------------
# Custom registry
# ---------------------------------------------------------------------------


def test_custom_registry_with_no_default_rules():
    """A registry with no rules preserves strings verbatim."""
    reg = RedactionRegistry(rules=())
    ev = build_plan_event(
        PlanEventType.PLAN_CREATED, plan_id="P",
        snapshot_id="X",
        payload={"args": {"email": "alice@example.com"}},
        registry=reg,
    )
    assert ev.to_dict()["payload"]["args"]["email"] == "alice@example.com"


def test_custom_registry_with_proprietary_rule():
    """Custom rules fire; defaults do not (only registered rules
    match)."""
    reg = RedactionRegistry(rules=(
        TextRule(rule_id="internal_id",
                 pattern=r"INTERNAL-[A-Z0-9]{6}"),
    ))
    text = "internal id: INTERNAL-ABC123, no email here"
    red, ids = reg.apply_text(text)
    assert "INTERNAL-ABC123" not in red
    assert ids == ["internal_id"]
    # Email pattern is NOT registered; should NOT fire.
    assert "alice@example.com" in reg.apply_text(
        "alice@example.com"
    )[0]


# ---------------------------------------------------------------------------
# Audit surface (FR-P3.1-09)
# ---------------------------------------------------------------------------


def test_audit_clean_directory_reports_ok(tmp_path: Path):
    """When no secret appears anywhere, audit_artifacts returns
    `ok=True` and `matches_total=0`."""
    # Build a clean run directory.
    trace_dir = tmp_path / "TR-clean"
    trace_dir.mkdir()
    (trace_dir / "events.jsonl").write_text(
        json.dumps({"event_type": "step_completed",
                    "step_id": "S1",
                    "description": "clicked the button"}) + "\n",
        encoding="utf-8",
    )
    reg = default_registry()
    report = audit_persisted_plans(trace_dir, reg)
    assert report.ok
    assert report.matches_total == 0
    assert report.ok is True


def test_audit_detects_email_in_events(tmp_path: Path):
    """The audit MUST catch a secret written into events.jsonl
    after persistence (i.e. simulating a sanitisation bug)."""
    trace_dir = tmp_path / "TR-dirty"
    trace_dir.mkdir()
    (trace_dir / "events.jsonl").write_text(
        json.dumps({"event_type": "step_completed",
                    "step_id": "S1",
                    "description": "send to alice@example.com"}) + "\n",
        encoding="utf-8",
    )
    report = audit_persisted_plans(trace_dir, default_registry())
    assert not report.ok
    assert report.matches_total >= 1
    assert any(
        "email" in match[0]
        for matches in report.matches_per_file.values()
        for match in matches
    )


def test_audit_scans_multiple_file_types(tmp_path: Path):
    """Audit walks events.jsonl, step-results.json,
    manifest.json, trace.md, report.md, and any *.json."""
    trace_dir = tmp_path / "TR-multi"
    trace_dir.mkdir()
    # Plant a secret in each file.
    (trace_dir / "events.jsonl").write_text(
        json.dumps({"description": "alice@example.com"}) + "\n",
        encoding="utf-8",
    )
    (trace_dir / "step-results.json").write_text(
        json.dumps({"step_results": [
            {"description": "alice@example.com"},
        ]}),
        encoding="utf-8",
    )
    (trace_dir / "trace.md").write_text(
        "# trace\nsend to alice@example.com\n",
        encoding="utf-8",
    )
    (trace_dir / "other.json").write_text(
        json.dumps({"note": "alice@example.com"}),
        encoding="utf-8",
    )
    report = audit_persisted_plans(trace_dir, default_registry())
    assert not report.ok
    assert len(report.files_scanned) >= 4
    # Each dirty file should appear in matches_per_file.
    for path in ("events.jsonl", "step-results.json",
                 "trace.md", "other.json"):
        assert any(path in str(p) for p in report.matches_per_file), (
            f"{path} missing from matches_per_file"
        )


def test_audit_handles_missing_directory(tmp_path: Path):
    """Non-existent directory returns ok=True with zero
    matches (no scan, no failure)."""
    report = audit_persisted_plans(tmp_path / "nonexistent",
                                   default_registry())
    assert report.ok
    assert report.matches_total == 0


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_persist_is_deterministic():
    payload = {"args": {"x": "alice@example.com", "n": 1}}
    a = build_plan_event(PlanEventType.PLAN_CREATED,
                          plan_id="P", snapshot_id="X", payload=payload)
    b = build_plan_event(PlanEventType.PLAN_CREATED,
                          plan_id="P", snapshot_id="X", payload=payload)
    assert a.to_dict() == b.to_dict()


# ---------------------------------------------------------------------------
# Backfilling P2.4 deliverable: RedactionRegistry existence
# ---------------------------------------------------------------------------


def test_redaction_registry_is_constructible():
    """P2.4 spec called for `agent/redaction.py` with
    `RedactionRegistry`; this commit ships it."""
    # The raw pattern r"\\d+" is the 4-char regex `\\d+`
    # (literal backslash + d+) — does NOT match digits.
    reg_literal = RedactionRegistry(rules=(
        TextRule(rule_id="r1", pattern=r"\\d+"),
    ))
    red, ids = reg_literal.apply_text("hello 42 world")
    assert red == "hello 42 world"
    assert ids == []
    # Real digit rule: raw `\d+` matches digits.
    reg_digit = RedactionRegistry(rules=(
        TextRule(rule_id="r2", pattern=r"\d+"),
    ))
    red2, ids2 = reg_digit.apply_text("hello 42 world")
    assert red2 == "hello [REDACTED] world"
    assert ids2 == ["r2"]


def test_redaction_audit_report_to_dict():
    """Audit report serialises to a dict for downstream
    consumers."""
    reg = RedactionRegistry(rules=())
    report = RedactionAuditReport(
        files_scanned=("a", "b"),
        matches_per_file={"a": (("r1", "preview"),)},
        rule_ids_registered=("r1",),
        matches_total=1,
    )
    d = report.to_dict()
    assert d["files_scanned"] == ["a", "b"]
        # After to_dict: inner tuples become lists (per the
    # updated RedactionAuditReport.to_dict), so the structure
    # is [[rule_id, preview], ...] per file.
    assert d["matches_per_file"]["a"] == [["r1", "preview"]]
    assert d["matches_total"] == 1
    assert d["ok"] is False