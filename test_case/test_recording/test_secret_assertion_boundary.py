"""Secret-assertion boundary: a protected field's live value must never reach
an assertion's expected/payload.

The text_commit path is already fail-closed (RawCaptureEvent rejects plaintext
``input.value`` unless ``protected is False``, compiler issues
``recording_secret_review_required``). The assertion path had no such gate:
``add_assertion`` hard-codes ``protected=False`` on its ObservedTarget and
persists ``expected`` verbatim, so a wizard draft frozen on a password field
carried the live secret into the recording document.

Contract locked here:

* ``build_assertion_draft`` must never prefill the live value of a field whose
  protection is True/None — the draft ships with an empty expectation and the
  operator types their own.
* ``add_assertion`` must refuse an assertion whose observed target is
  protected (True or unknown) when the assertion type reads content
  (``text_equals``/``text_contains``/``value_equals``) — same conservative
  posture as text_commit.  ``protected=False`` saves normally.
* The refusal is a typed RecordingModelError, not a silent scrub.
"""

from __future__ import annotations

import pytest

from target.recording.assertion_draft import (
    build_assertion_draft,
    validate_assertion_draft,
)
from target.recording.models import CaptureScope, RecordingModelError
from target.recording.session import RecordingSession

PROTECTED_OBSERVED = {
    "automationId": "txtPassword",
    "identifier": None,
    "controlType": "Edit",
    "name": "Password",
    "text": "Password",
    "value": "SuperSecret123",
    "protected": True,
}

SCOPE = CaptureScope(target="EDRClient", backend="windows",
                     process_name="EDRClient.exe", window_title="EDR")


def _payload_from(protected_observed: dict, expected: object) -> dict:
    """Mirror the wizard freeze -> panel save pipeline exactly.

    ``_freeze_editor_draft`` (target/recording/session.py) always stamps a
    protection string: "True"/"False"/"None" (unknown).
    """
    protection = protected_observed.get("protected")
    draft = build_assertion_draft(
        protected_observed, "text_equals",
        str(protected_observed.get("value"))
        if protected_observed.get("value") is not None else "",
        read_status="ok",
        protection_status=(
            "None" if protection is None else str(protection)
        ),
    )
    payload = validate_assertion_draft(draft.to_dict())
    payload["requestId"] = "REQ-secret-boundary"
    if expected is not None:
        payload["expected"] = expected
    return payload


class _RecordingSessionTest(RecordingSession):
    """Session with source hooks the constructor requires for start()."""


@pytest.fixture()
def session():
    session = _RecordingSessionTest("S-boundary", "probe", SCOPE)
    session.start()
    try:
        yield session
    finally:
        try:
            session.stop("user")
        except Exception:
            pass


@pytest.mark.parametrize("protection", [True, None])
def test_wizard_freeze_never_prefills_protected_or_unknown_value(protection):
    observed = {**PROTECTED_OBSERVED, "protected": protection}
    # _freeze_editor_draft (target/recording/session.py) builds the draft with
    # the observed value as the initial expectation. For protected/unknown
    # fields that initial expectation must be empty, not the live value.
    initial = (
        str(observed.get("value")) if observed.get("value") is not None else ""
    )
    draft = build_assertion_draft(
        observed, "text_equals", initial,
        read_status="ok",
        protection_status=(
            str(observed.get("protected"))
            if observed.get("protected") is not None else None
        ),
    )
    data = draft.to_dict()
    assert data["typedExpected"] == ""
    assert data["observed"].get("value") in (None, "")
    assert "SuperSecret123" not in repr(data)


def test_non_protected_field_still_prefills_live_value():
    observed = {**PROTECTED_OBSERVED, "protected": False}
    draft = build_assertion_draft(
        observed, "text_equals", str(observed.get("value")), read_status="ok",
    )
    assert draft.to_dict()["typedExpected"] == "SuperSecret123"


@pytest.mark.parametrize("protection", [True, None])
def test_add_assertion_refuses_content_assertion_on_protected_target(
    session, protection,
):
    # Build the payload through the real wizard pipeline so it carries
    # protectionStatus provenance exactly as the panel save does.
    observed = {**PROTECTED_OBSERVED, "protected": protection}
    payload = _payload_from(observed, "SuperSecret123")
    assert payload.get("protectionStatus") is not None
    with pytest.raises(RecordingModelError) as excinfo:
        session.add_assertion(payload)
    assert excinfo.value.code == "recording_secret_plaintext"


def test_add_assertion_saves_non_protected_target_normally(session):
    observed = {**PROTECTED_OBSERVED, "protected": False,
                "automationId": "lblStatus", "controlType": "Text",
                "name": "Status", "text": "Status", "value": "Ready"}
    payload = _payload_from(observed, "Ready")
    saved = session.add_assertion(payload)
    assert saved.get("ok") is True
    doc = session.recording().to_dict()
    import json as _json
    serialized = _json.dumps(doc, ensure_ascii=False)
    assert "SuperSecret123" not in serialized


@pytest.mark.parametrize(
    "assertion_type,expected",
    [("visible", True), ("checked", True), ("enabled", True),
     ("window_open", True), ("window_text_contains", "状态"),
     ("window_text_contains_time", "%Y-%m-%d")],
)
def test_identity_free_and_bool_assertions_on_protected_target_stay_allowed(
    session, assertion_type, expected,
):
    """Non-content assertions verify state/existence, not the secret's value;
    they stay saveable against a protected target."""
    payload = {
        "requestId": f"REQ-bool-{assertion_type}",
        "assertion": assertion_type,
        "expected": expected,
        "timeoutSeconds": 10.0,
        "automationId": "txtPassword",
        "controlType": "Edit",
        "name": "Password",
    }
    from target.recording.assertion_draft import protection_blocks_content
    assert protection_blocks_content(assertion_type, "True", None) is False
    saved = session.add_assertion(payload)
    assert saved.get("ok") is True


def test_frozen_draft_commit_path_is_blocked(session):
    """The wizard's commit_assertion_draft path must hit the same gate."""
    draft = build_assertion_draft(
        PROTECTED_OBSERVED, "text_equals", "",
        read_status="ok", protection_status="True",
    )
    data = draft.to_dict()
    data["typedExpected"] = "SuperSecret123"  # operator typed the secret back
    session._pending_drafts[data["draftId"]] = data
    with pytest.raises(RecordingModelError) as excinfo:
        session.commit_assertion_draft(data)
    assert excinfo.value.code == "recording_secret_plaintext"
