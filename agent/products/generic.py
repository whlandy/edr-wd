"""Declarative adapter for installed, non-EDR desktop products."""

from __future__ import annotations

from typing import Any, Mapping

from target.product_profiles import INSTALLED_PRODUCT_SPECS, get_capability

from .intent import IntentPlan
from .models import WindowRole


class GenericProductAdapter:
    def __init__(self, profile_id: str, backend: str) -> None:
        capability = get_capability(profile_id, backend)
        self.profile_id = profile_id
        self.backend = backend
        self._spec = INSTALLED_PRODUCT_SPECS.get((profile_id, backend), {})
        self._roles = {
            role.role: WindowRole(role.role, role.process_names, role.title_pattern,
                                  role.root_control_ids, role.parent_role)
            for role in capability.window_roles
        }

    def resolve_window_role(self, role: str) -> WindowRole:
        try:
            return self._roles[role]
        except KeyError as exc:
            raise ValueError(f"unknown window role: {role}") from exc

    def activate(self, call_tool) -> dict:
        application = self._spec.get("application") or {}
        if not application:
            return {"ok": True, "code": "activation_not_configured"}
        if application.get("path"):
            result = call_tool("activate_product_app", {"profile_id": self.profile_id})
            if not isinstance(result, dict) or not result.get("ok"):
                return {"ok": False, "code": (result.get("code")
                        if isinstance(result, dict) and result.get("code")
                        else "product_activation_failed"),
                        "error": result.get("error") if isinstance(result, dict) else None}
            return result
        args = {key: application[key] for key in ("app_name", "bundle_id")
                if application.get(key)}
        if not args:
            return {"ok": True, "code": "activation_not_configured"}
        result = call_tool("activate_app", args)
        if not isinstance(result, dict) or not result.get("ok"):
            return {"ok": False, "code": (result.get("code")
                    if isinstance(result, dict) and result.get("code")
                    else "product_activation_failed"),
                    "error": result.get("error") if isinstance(result, dict) else None}
        return result

    def _intent(self, intent_id: str, args: Mapping[str, Any]) -> dict:
        intents = self._spec.get("intents") or {}
        if args or intent_id not in intents:
            raise ValueError("product_intent_unknown")
        return intents[intent_id]

    def expected_result(self, intent_id: str,
                        args: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
        intent = self._intent(intent_id, args)
        return tuple(intent["assertions"])

    def resolve_intent(self, intent_id: str, args: Mapping[str, Any],
                       *, registry=None, profile=None) -> IntentPlan:
        intent = self._intent(intent_id, args)
        result_role = intent["result_role"]
        already_open = False
        if (result_role != intent["source_role"]
                and registry is not None and profile is not None):
            try:
                registry.resolve_unique(result_role, profile)
                already_open = True
            except ValueError as exc:
                if str(exc) == "window_role_ambiguous":
                    raise
        return IntentPlan(
            intent_id=intent_id,
            main_role=intent["source_role"],
            result_role=result_role,
            selector=dict(intent["selector"]),
            click_required=not already_open,
            result_assertions=tuple(intent["assertions"]),
            visual=dict(intent["visual"]) if intent.get("visual") else None,
        )
