"""HiSec entry and EDRClient role semantics (first product adapter)."""

from __future__ import annotations

from .models import WindowRole
from .intent import IntentPlan
from target.product_profiles import INSTALLED_PRODUCT_SPECS, get_capability


class EDRClientAdapter:
    profile_ids = frozenset({"windows_hisec", "macos_hisec"})
    activation_window_title_pattern = "^华为HiSec Endpoint$"

    def __init__(self, backend: str) -> None:
        if backend not in {"windows_pywinauto", "macos_accessibility"}:
            raise ValueError("EDRClient backend is unsupported")
        profile_id = "windows_hisec" if backend == "windows_pywinauto" else "macos_hisec"
        capability = get_capability(profile_id, backend)
        self._application = INSTALLED_PRODUCT_SPECS.get((profile_id, backend), {}).get(
            "application", {})
        self._roles = {
            item.role: WindowRole(item.role, item.process_names, item.title_pattern,
                                  item.root_control_ids, item.parent_role)
            for item in capability.window_roles
        }
        # Derive the activation title pattern from the role spec (single source
        # of truth): live EDR builds may show the main window under titles like
        # "logo1" that the class-level fallback does not cover.  The fallback
        # stays for profiles whose main role declares no pattern.
        main_role = self._roles.get("main")
        if main_role is not None and main_role.title_pattern:
            self.activation_window_title_pattern = main_role.title_pattern

    @property
    def window_roles(self) -> tuple[WindowRole, ...]:
        return tuple(self._roles.values())

    def resolve_window_role(self, role: str) -> WindowRole:
        try:
            return self._roles[role]
        except KeyError as exc:
            raise ValueError(f"unknown window role: {role}") from exc

    def activate(self, call_tool) -> dict:
        """Keep the existing product-specific launch sequence behind the adapter."""
        args = {"wait": True, "timeout": 15.0}
        if self._application.get("entry_path"):
            args["exe_path"] = self._application["entry_path"]
        result = call_tool("activate_edr", args)
        if not isinstance(result, dict) or not result.get("ok"):
            return {"ok": False, "code": "product_activation_failed"}
        # Only the EDRClient window is a precondition.  The agent entry window
        # (result["main"]) is not: the target's own activate_edr contract says
        # it is only needed by the edrWidget fallback path, and live targets
        # whose agent UI never surfaces (service-session only) still have a
        # fully usable EDRClient.  Report its absence; do not fail activation.
        if "client" in result and not result.get("client", {}).get("window_found"):
            return {"ok": False, "code": "product_window_missing"}
        if "main" in result and not result.get("main", {}).get("window_found"):
            result = dict(result)
            result["main_window_note"] = (
                "agent entry window is not open; EDRClient main window is available")
        return {**result, "code": "ok"}

    def expected_result(self, intent_id: str, args: dict | None = None) -> tuple[dict, ...]:
        if intent_id != "edrclient.log_center.open" or args:
            raise ValueError("product_intent_unknown")
        process = self._roles["log_center"].process_names[0]
        return (
            {"type": "active_window_owner", "value": process},
            {"type": "control_exists", "value": {"text": "操作日志"}},
        )

    def resolve_intent(self, intent_id: str, args: dict | None = None,
                       *, registry=None, profile=None) -> IntentPlan:
        if intent_id != "edrclient.log_center.open" or args:
            raise ValueError("product_intent_unknown")
        already_open = False
        if registry is not None and profile is not None:
            try:
                registry.resolve_unique("log_center", profile)
                already_open = True
            except ValueError as exc:
                if str(exc) == "window_role_ambiguous":
                    raise
        return IntentPlan(
            intent_id=intent_id, main_role="main", result_role="log_center",
            selector={"name": "日志中心"}, click_required=not already_open,
            result_assertions=self.expected_result(intent_id, args),
        )
