"""
P1.1 acceptance gate — ActionReceipt shape + normalize.

Tests verify that every receipt carries the stable envelope and
that legacy {"ok": bool, "error": str} shapes are normalised.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
_TARGET = _REPO / "target"

if str(_TARGET) not in sys.path:
    sys.path.insert(0, str(_TARGET))


import action_dispatcher as ad          # noqa: E402


def test_receipt_from_ok_envelope():
    r = ad.ActionReceipt.from_ok(
        action_id="gui.click",
        action_code="A020",
        request_id="R1",
        result={"clicked": True},
    )
    assert r.code == ad.CODE_OK
    assert r.ok is True
    assert r.action_id == "gui.click"
    assert r.action_code == "A020"
    assert r.request_id == "R1"
    assert r.server_instance_id.startswith("inst-")
    assert r.result == {"clicked": True}


def test_receipt_from_error_envelope():
    r = ad.ActionReceipt.from_error(
        code=ad.CODE_BACKEND_DISABLED,
        action_id="gui.type_text",
        action_code="A030",
        request_id=None,
        message="not supported",
        disabled_reason="not supported by macos_accessibility",
    )
    assert r.code == ad.CODE_BACKEND_DISABLED
    assert r.ok is False
    assert r.disabled_reason == "not supported by macos_accessibility"


def test_receipt_rejects_unknown_code():
    with pytest.raises(ValueError):
        ad.ActionReceipt(
            code="not_a_code",
            ok=False,
            action_id="x",
        )


def test_normalize_ok_payload():
    r = ad.normalize(
        {"ok": True, "data": {"k": "v"}},
        action_id="gui.click",
        action_code="A020",
        request_id="R1",
    )
    assert r.code == ad.CODE_OK
    assert r.ok is True
    assert r.result == {"ok": True, "data": {"k": "v"}}


def test_normalize_error_payload_uses_backend_error_code():
    r = ad.normalize(
        {"ok": False, "error": "boom"},
        action_id="gui.click",
        action_code="A020",
        request_id="R1",
    )
    assert r.code == ad.CODE_BACKEND_ERROR
    assert r.ok is False
    assert "boom" in r.message


def test_normalize_none_payload():
    r = ad.normalize(
        None,
        action_id="observe.screenshot",
        action_code="A040",
        request_id="R2",
    )
    assert r.code == ad.CODE_OK
    assert r.result is None


def test_normalize_non_mapping_passthrough():
    r = ad.normalize(
        "raw-string-payload",
        action_id="x", action_code=None, request_id=None,
    )
    # Non-mapping payloads are accepted as `extras["value"]`.
    assert r.code == ad.CODE_OK
    assert r.extras.get("value") == "raw-string-payload"


def test_receipt_ok_fixture_round_trip():
    payload = json.loads(
        (_REPO / "test_case" / "fixtures" / "dispatch"
         / "receipt_ok.json").read_text()
    )
    r = ad.ActionReceipt(
        code=payload["code"],
        ok=payload["ok"],
        action_id=payload["action_id"],
        action_code=payload["action_code"],
        request_id=payload["request_id"],
        server_instance_id=payload["server_instance_id"],
        result=payload["result"],
        message=payload["message"],
    )
    assert r.code == "ok"
    assert r.ok is True
    assert r.action_id == "gui.click"


def test_receipt_disabled_fixture_round_trip():
    payload = json.loads(
        (_REPO / "test_case" / "fixtures" / "dispatch"
         / "receipt_disabled.json").read_text()
    )
    r = ad.ActionReceipt(
        code=payload["code"],
        ok=payload["ok"],
        action_id=payload["action_id"],
        action_code=payload["action_code"],
        request_id=payload["request_id"],
        server_instance_id=payload["server_instance_id"],
        result=payload["result"],
        disabled_reason=payload["disabled_reason"],
        message=payload["message"],
    )
    assert r.code == ad.CODE_BACKEND_DISABLED
    assert r.disabled_reason == "not supported by macos_accessibility"