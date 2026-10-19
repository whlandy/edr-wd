"""Business intents are versioned requests with explicit expected outcomes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class IntentRequest:
    profile_id: str
    intent_id: str
    arguments: Mapping[str, Any]
    expected_result: Mapping[str, Any]


@dataclass(frozen=True)
class IntentPlan:
    intent_id: str
    main_role: str
    result_role: str
    selector: Mapping[str, Any]
    click_required: bool
    result_assertions: tuple[Mapping[str, Any], ...]
    visual: Mapping[str, Any] | None = None


def validate_intent_request(request: IntentRequest, profile, adapter=None) -> None:
    if request.profile_id != profile.profile_id:
        raise ValueError("product_profile_mismatch")
    if not isinstance(request.arguments, Mapping):
        raise ValueError("product_intent_arguments_invalid")
    if adapter is None:
        from .registry import get_product_adapter
        adapter = get_product_adapter(profile.profile_id, profile.supported_backends[0])
    try:
        assertions = adapter.expected_result(request.intent_id, dict(request.arguments))
        plan = adapter.resolve_intent(request.intent_id, dict(request.arguments))
    except ValueError as exc:
        raise ValueError("product_intent_unknown") from exc
    if not assertions or not isinstance(request.expected_result, Mapping) or request.expected_result.get("page") != plan.result_role:
        raise ValueError("product_intent_expected_result_missing")
