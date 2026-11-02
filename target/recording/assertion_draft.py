"""Typed assertion drafts for the Chinese assertion wizard (U2).

The wizard's contract with the user is: pick a component, read its current
value, choose a verification, edit the expectation, save.  Everything below is
pure logic so the awkward failure modes the design calls out are testable
without a GUI:

- text expectations stay strings — ``"123"``/``"true"`` are never coerced to
  numbers/booleans just because they look like them;
- ``visible``/``checked``/``enabled`` expectations are real booleans, not
  JSON the user has to type;
- a read that failed or returned nothing usable is ``unknown`` — it is never
  rendered as a passing preview and never prefilled as an empty value;
- ``False``, empty string and ``0`` are legitimate observed/expected values
  and are compared with equality, never with truthiness.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Mapping

from .models import RecordingModelError

TEXT_TYPES = ("text_equals", "text_contains")
VALUE_TYPES = ("value_equals",)
BOOL_TYPES = ("visible", "checked", "enabled")
WINDOW_TEXT_TYPES = ("window_text_contains",)
IDENTITY_FREE_TYPES = frozenset({
    "window_open", "window_text_contains", "window_text_contains_time",
    "window_text_time_range",
})
#: Assertion types that read and compare the control's *content*.  Against a
#: protected (password) field — or one whose protection is unknown — these
#: must be refused, mirroring the text_commit boundary: the secret's value
#: would be persisted verbatim as the assertion's `expected`.
CONTENT_ASSERTION_TYPES = frozenset(
    {"text_equals", "text_contains", "value_equals"}
)

_LABELS = {
    "text_equals": "组件文字等于",
    "text_contains": "组件文字包含",
    "value_equals": "输入框/组件值等于",
    "visible": "组件可见",
    "checked": "组件已勾选",
    "enabled": "组件可用",
    "window_open": "窗口已打开",
    "window_text_contains": "窗口包含文字",
}


def available_assertion_types(
    observed: Mapping[str, Any] | None, *,
    read_capabilities: frozenset[str] | set[str] | None = None,
) -> list[dict[str, Any]]:
    """Assertion choices the wizard offers for one observed component.

    ``read_capabilities`` carries what the platform backend can actually read
    (e.g. ``{"checked", "enabled"}``); booleans whose read is unavailable are
    hidden instead of offered and then failing.  ``None`` means capability is
    unknown — everything is offered with a hint, never silently disabled.
    """
    has_control = bool(observed) and not _is_identity_free_only(observed)
    offered = []
    for assertion_type, label in _LABELS.items():
        needs_control = assertion_type not in IDENTITY_FREE_TYPES
        if needs_control and not has_control:
            continue
        capability_note = None
        if assertion_type in BOOL_TYPES and read_capabilities is not None:
            if assertion_type not in read_capabilities:
                continue
        elif assertion_type in BOOL_TYPES:
            capability_note = "以平台实际读取能力为准"
        kind = ("bool" if assertion_type in BOOL_TYPES
                else "value" if assertion_type in VALUE_TYPES
                else "text")
        offered.append({
            "type": assertion_type, "label": label, "valueKind": kind,
            "needsControl": needs_control, "capabilityNote": capability_note,
        })
    return offered


def _is_identity_free_only(observed: Mapping[str, Any]) -> bool:
    return not any(observed.get(key) for key in
                   ("automationId", "automation_id", "identifier"))


@dataclass(frozen=True)
class AssertionDraft:
    """One wizard editing session.  Each draft owns its own window and
    (optional) action binding — two drafts never share target state."""

    draft_id: str
    assertion_type: str
    typed_expected: Any
    observed: dict[str, Any] = field(default_factory=dict)
    observed_at: str | None = None
    read_status: str = "unknown"          # ok | unknown | error
    read_error: str | None = None
    protection_status: str | None = None  # None/未知沿用既有保守策略
    timeout_seconds: float = 10.0
    binding: tuple[int, str] | None = None  # (sequence, causalId) 显式绑定
    expected_was_edited: bool = False
    window: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "draftId": self.draft_id, "assertionType": self.assertion_type,
            "typedExpected": self.typed_expected, "observed": dict(self.observed),
            "observedAt": self.observed_at, "readStatus": self.read_status,
            "readError": self.read_error,
            "protectionStatus": self.protection_status,
            "timeoutSeconds": self.timeout_seconds,
            "binding": list(self.binding) if self.binding else None,
            "expectedWasEdited": self.expected_was_edited,
            "window": dict(self.window),
        }


def build_assertion_draft(
    observed: Mapping[str, Any] | None, assertion_type: str, typed_expected: Any, *,
    draft_id: str | None = None, read_status: str = "unknown",
    read_error: str | None = None, protection_status: str | None = None,
    observed_at: str | None = None, timeout_seconds: float = 10.0,
    binding: tuple[int, str] | None = None, window: Mapping[str, Any] | None = None,
) -> AssertionDraft:
    """Create a draft with the expectation **typed by kind**.

    Text kinds reject non-strings at the door (a float ``123.0`` or a bool
    ``True`` cannot sneak in); boolean kinds reject everything but real bools.
    ``unknown`` reads are allowed through — the expectation is the user's
    intent, not the observation — but they carry readStatus so the preview
    stays honest.
    """
    if assertion_type in TEXT_TYPES or assertion_type in WINDOW_TEXT_TYPES:
        if not isinstance(typed_expected, str):
            raise RecordingModelError(
                "assertion_expected_typed", f"{assertion_type} 期望必须是字符串"
                "（文本 123/true 保持字符串）", path="draft.typedExpected",
            )
    elif assertion_type in BOOL_TYPES:
        if not isinstance(typed_expected, bool):
            raise RecordingModelError(
                "assertion_expected_typed", f"{assertion_type} 期望必须是布尔选择",
                path="draft.typedExpected",
            )
    elif assertion_type == "value_equals":
        if not isinstance(typed_expected, str):
            raise RecordingModelError(
                "assertion_expected_typed", "value_equals 期望必须是字符串",
                path="draft.typedExpected",
            )
    if read_status not in {"ok", "unknown", "error"}:
        raise RecordingModelError(
            "assertion_read_status_invalid", f"unknown read status {read_status!r}",
            path="draft.readStatus",
        )
    # A protected field's (or an unknown-sensitivity field's) live value must
    # never become the initial expectation: the draft would carry the secret
    # verbatim into the saved assertion.  The operator types their own.
    if protection_blocks_content(assertion_type, protection_status, observed):
        live_value = (observed or {}).get("value")
        if live_value:
            observed = {**observed, "value": None}
            if typed_expected == str(live_value):
                typed_expected = ""
    return AssertionDraft(
        draft_id=draft_id or uuid.uuid4().hex,
        assertion_type=assertion_type,
        typed_expected=typed_expected,
        observed=dict(observed or {}),
        observed_at=observed_at,
        read_status=read_status,
        read_error=read_error,
        protection_status=protection_status,
        timeout_seconds=float(timeout_seconds),
        binding=binding,
        expected_was_edited=False,
        window=dict(window or {}),
    )


#: Control types whose value *is* sensitive content the operator types into
#: (a password field is an Edit).  Unknown protection blocks content
#: assertions only for these: static captions ("Text"), checkboxes, panes
#: and the like were never redactable content, and blocking them would
#: break every picker-drafted assertion.
_VALUE_BEARING_TYPES = frozenset({"edit", "combobox"})


def protection_blocks_content(
    assertion_type: Any, protection_status: Any,
    observed: Mapping[str, Any] | None = None,
) -> bool:
    """Whether a content-reading assertion must be refused for this target.

    ``protection_status`` accepts the draft's string form ("True"/"False"/
    "None"), a real bool/None, or falls back to the observed mapping's
    ``protected`` value.  An explicit ``True`` always blocks.  Unknown
    protection blocks only when the target is a value-bearing control type
    (the case where a live secret could have been read into the draft's
    value).  Only an explicit ``False`` — or a non-value-bearing control
    whose protection the picker simply did not read — lets content
    assertions save.
    """
    if assertion_type not in CONTENT_ASSERTION_TYPES:
        return False
    if isinstance(protection_status, str):
        if protection_status == "False":
            resolved: Any = False
        elif protection_status == "True":
            resolved = True
        else:
            resolved = None
    else:
        resolved = protection_status
    if resolved is None and observed is not None:
        resolved = (observed or {}).get("protected")
    if resolved is not None:
        return resolved is not False
    # Unknown protection.  The target-side producers only read a live value
    # when protection was explicitly False (windows `_describe`, macOS
    # secure-field boundary), so an unknown-protection draft has no secret
    # to carry — EXCEPT for the control types whose value is inherently the
    # sensitive content, where the conservative posture matches text_commit.
    control_type = str(
        (observed or {}).get("controlType")
        or (observed or {}).get("control_type") or ""
    ).lower()
    return bool(control_type) and control_type in _VALUE_BEARING_TYPES


def validate_assertion_draft(draft: "AssertionDraft | Mapping[str, Any]") -> dict[str, Any]:
    """Turn a draft into the existing ``add_assertion`` payload.

    Mapping, not a second validator: the session's assertion rules (identity
    requirements, timeouts, time formats) stay the single authority.
    """
    if isinstance(draft, AssertionDraft):
        draft = draft.to_dict()
    observed = draft.get("observed") or {}
    payload: dict[str, Any] = {
        "assertion": draft.get("assertionType") or draft.get("assertion"),
        "expected": draft.get("typedExpected", draft.get("expected")),
        "timeoutSeconds": draft.get("timeoutSeconds", 10.0),
        "requestId": draft.get("requestId") or draft.get("draftId"),
        "window": draft.get("window") or None,
        "bindPrevious": bool(draft.get("binding")),
    }
    binding = draft.get("binding")
    if binding:
        payload["bindSequence"] = binding[0]
        payload["bindCausalId"] = binding[1]
    if payload["assertion"] not in IDENTITY_FREE_TYPES:
        payload["automationId"] = observed.get("automationId") or observed.get("automation_id")
        payload["identifier"] = observed.get("identifier")
        payload["controlType"] = observed.get("controlType") or observed.get("control_type")
        payload["name"] = observed.get("name") or observed.get("text")
        payload["value"] = observed.get("value")
        # Carry the frozen protection status to the save boundary so
        # add_assertion's content-assertion gate sees it (the gate's last
        # line of defense against a scrubbed prefill being typed back).
        # An unknown read (None) must survive as "None" — distinct from a
        # draft that never carried protection metadata at all.
        frozen = draft.get("protectionStatus")
        payload["protectionStatus"] = (
            frozen if frozen is not None
            else ("None" if "protected" in observed
                  and observed.get("protected") is None else None)
        )
    return payload


def render_assertion_summary(draft: AssertionDraft) -> str:
    """One plain-Chinese sentence of what will be verified, and how."""
    target = (draft.observed.get("name") or draft.observed.get("text")
              or draft.observed.get("automationId") or "所选组件")
    timeout = f"等待最多 {draft.timeout_seconds:g} 秒，"
    kind = draft.assertion_type
    expected = draft.typed_expected
    if kind == "text_equals":
        body = f"验证『{target}』的文字等于「{expected}」"
    elif kind == "text_contains":
        body = f"验证『{target}』的文字包含「{expected}」"
    elif kind == "value_equals":
        body = f"验证『{target}』的值等于「{expected}」"
    elif kind == "visible":
        body = f"验证『{target}』{'可见' if expected else '不可见'}"
    elif kind == "checked":
        body = f"验证『{target}』{'已勾选' if expected else '未勾选'}"
    elif kind == "enabled":
        body = f"验证『{target}』{'可用' if expected else '不可用'}"
    elif kind == "window_open":
        window = draft.window.get("title") or "所选窗口"
        body = f"验证窗口『{window}』{'已打开' if expected is not False else '已关闭'}"
    elif kind == "window_text_contains":
        window = draft.window.get("title") or "所选窗口"
        body = f"验证窗口『{window}』的可访问文字包含「{expected}」"
    else:
        body = f"验证 {kind}"
    binding = ""
    if draft.binding is not None:
        binding = f"（作为第 {draft.binding[0]} 个动作的结果验证）"
    if draft.read_status != "ok":
        body += "（当前值未能读取：此断言基于你输入的期望）"
    return f"在所选窗口中，{timeout}{body}{binding}。"


PreviewReader = Callable[[], tuple[str, Any]]


def preview_assertion(draft: AssertionDraft, reader: PreviewReader) -> dict[str, Any]:
    """Trial-run the expectation with the replay comparison semantics.

    The reader returns ``(read_status, actual)``.  ``unknown``/``error`` reads
    produce ``undetermined`` — never a pass.  ``False``, empty string and 0
    are real values: comparison is equality, not truthiness.  The trial is
    read-only; it never saves or clicks.
    """
    try:
        read_status, actual = reader()
    except Exception as exc:  # noqa: BLE001 - a raising reader is unknown
        return {"result": "undetermined", "readStatus": "error",
                "readError": str(exc), "actual": None}
    if read_status not in {"ok"}:
        return {"result": "undetermined", "readStatus": read_status,
                "actual": actual}
    # 按类型校验实际值：None/缺失/类型不符不是合法读取，一律 undetermined
    # —— 绝不用 bool(None)==False、str(None) 之类的宽松比较制造假通过。
    if draft.assertion_type in TEXT_TYPES:
        if not isinstance(actual, str):
            return {"result": "undetermined", "readStatus": "ok",
                    "readError": f"实际值不是文本：{actual!r}", "actual": actual}
        matches = (actual == draft.typed_expected
                   if draft.assertion_type == "text_equals"
                   else draft.typed_expected in actual)
    elif draft.assertion_type == "value_equals":
        if actual is None or not isinstance(actual, (str, int, float)):
            return {"result": "undetermined", "readStatus": "ok",
                    "readError": f"实际值不可读：{actual!r}", "actual": actual}
        matches = str(actual) == draft.typed_expected
    elif draft.assertion_type in BOOL_TYPES:
        if not isinstance(actual, bool):
            return {"result": "undetermined", "readStatus": "ok",
                    "readError": f"实际值不是布尔：{actual!r}（unknown 不等于 False）",
                    "actual": actual}
        matches = bool(actual) == bool(draft.typed_expected)
    else:
        return {"result": "undetermined", "readStatus": "ok",
                "readError": f"类型 {draft.assertion_type} 不支持在线试验", "actual": actual}
    return {"result": "pass" if matches else "fail",
            "readStatus": "ok", "actual": actual}


__all__ = [
    "AssertionDraft", "build_assertion_draft", "validate_assertion_draft",
    "available_assertion_types", "render_assertion_summary", "preview_assertion",
    "protection_blocks_content",
    "TEXT_TYPES", "VALUE_TYPES", "BOOL_TYPES", "CONTENT_ASSERTION_TYPES",
]
