"""Target-safe product capabilities shared with the agent.

Only locally reviewed descriptors live here. Credentials, shell commands, and
runtime window identities must never be part of an app catalog.
"""

from __future__ import annotations

import hashlib
import json
import ntpath
import posixpath
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from .action_catalog import ACTIONS_V1, BACKEND_NOT_IMPLEMENTED, VALID_BACKENDS
except ImportError:  # target-local deployment
    from action_catalog import ACTIONS_V1, BACKEND_NOT_IMPLEMENTED, VALID_BACKENDS


@dataclass(frozen=True)
class ProductWindowRole:
    role: str
    process_names: tuple[str, ...]
    title_pattern: str = ""
    root_control_ids: tuple[str, ...] = ()
    parent_role: str | None = None


@dataclass(frozen=True)
class ProductCapability:
    profile_id: str
    version: str
    backend: str
    allowed_action_ids: frozenset[str]
    window_roles: tuple[ProductWindowRole, ...]

    @property
    def digest(self) -> str:
        payload = [self.profile_id, self.version, self.backend,
                   sorted(self.allowed_action_ids),
                   [(role.role, role.process_names, role.title_pattern,
                     role.root_control_ids, role.parent_role) for role in self.window_roles]]
        return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()


def _catalog_actions(backend: str, *, hisec: bool) -> frozenset[str]:
    if backend not in VALID_BACKENDS:
        raise ValueError(f"unknown backend: {backend}")
    gaps = BACKEND_NOT_IMPLEMENTED.get(backend, {})
    return frozenset(
        spec.action_id for spec in ACTIONS_V1
        if backend in spec.backends and spec.tool_name not in gaps
        and (hisec or not spec.action_id.startswith("hisec."))
    )


BUILTIN_CAPABILITIES: dict[tuple[str, str], ProductCapability] = {
    ("macos_generic", "macos_accessibility"): ProductCapability(
        "macos_generic", "1", "macos_accessibility",
        _catalog_actions("macos_accessibility", hisec=False),
        (ProductWindowRole("main", ()),),
    ),
}

# Installed, reviewed files are the only source of additional target capabilities.
# An MCP caller cannot supply a descriptor or extend this mapping at run time.
INSTALLED_PRODUCT_SPECS: dict[tuple[str, str], dict[str, Any]] = {}
INSTALLED_APPS: dict[str, dict[str, Any]] = {}
DEFAULT_APP_ID: str | None = None


def get_capability(profile_id: str, backend: str) -> ProductCapability:
    try:
        return BUILTIN_CAPABILITIES[(profile_id, backend)]
    except KeyError as exc:
        raise ValueError(f"product_profile_unknown: {profile_id}/{backend}") from exc


def register_local_capability(capability: ProductCapability) -> None:
    """Register a locally installed product descriptor, never an action payload.

    The target must install this descriptor independently of any Agent request;
    a caller-supplied profile name or digest cannot grant new actions.
    """
    key = (capability.profile_id, capability.backend)
    if (not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", capability.profile_id)
            or key in BUILTIN_CAPABILITIES):
        raise ValueError("product_profile_duplicate_or_empty")
    if capability.backend not in VALID_BACKENDS:
        raise ValueError("product_profile_backend_invalid")
    allowed = _catalog_actions(capability.backend, hisec=True)
    if not capability.allowed_action_ids or not capability.allowed_action_ids <= allowed:
        raise ValueError("product_profile_actions_invalid")
    roles = [item.role for item in capability.window_roles]
    if not roles or len(roles) != len(set(roles)) or not all(roles):
        raise ValueError("product_profile_roles_invalid")
    BUILTIN_CAPABILITIES[key] = capability


def register_product_spec(spec: dict[str, Any]) -> ProductCapability:
    """Install a declarative product without modifying the backend or dispatcher.

    Only locally packaged descriptors may call this in the target process.
    The Agent can use the same descriptor for its adapter, but cannot transmit
    it to the target as an action argument.
    """
    if not isinstance(spec, dict) or spec.get("schema_version") != 1:
        raise ValueError("product_spec_version_invalid")
    profile_id, version, backend = (spec.get(key) for key in
                                    ("profile_id", "version", "backend"))
    if (not isinstance(profile_id, str) or not isinstance(version, str)
            or not version or not isinstance(backend, str)):
        raise ValueError("product_spec_version_invalid")
    raw_roles = spec.get("window_roles")
    raw_actions = spec.get("allowed_action_ids")
    if not isinstance(raw_roles, list) or not raw_roles or not isinstance(raw_actions, list):
        raise ValueError("product_spec_capability_invalid")
    roles = []
    for item in raw_roles:
        if not isinstance(item, dict) or not isinstance(item.get("role"), str):
            raise ValueError("product_spec_roles_invalid")
        names = item.get("process_names")
        if not isinstance(names, list) or not names or not all(
            isinstance(name, str) and name for name in names
        ):
            raise ValueError("product_spec_ownership_required")
        title = item.get("title_pattern", "")
        roots = item.get("root_control_ids", [])
        parent = item.get("parent_role")
        if (not isinstance(title, str) or not isinstance(roots, list)
                or not all(isinstance(root, str) for root in roots)
                or (parent is not None and not isinstance(parent, str))):
            raise ValueError("product_spec_roles_invalid")
        try:
            re.compile(title)
        except re.error as exc:
            raise ValueError("product_spec_title_invalid") from exc
        roles.append(ProductWindowRole(item["role"], tuple(names), title,
                                       tuple(roots), parent))
    names = {role.role for role in roles}
    if any(role.parent_role and role.parent_role not in names for role in roles):
        raise ValueError("product_spec_parent_role_invalid")
    if not all(isinstance(action, str) for action in raw_actions):
        raise ValueError("product_spec_actions_invalid")
    application = spec.get("application", {})
    if not isinstance(application, dict) or set(application) - {
        "app_name", "bundle_id", "path", "entry_path"
    }:
        raise ValueError("product_spec_application_invalid")
    app_name, bundle_id = application.get("app_name"), application.get("bundle_id")
    if any(value is not None and (not isinstance(value, str) or not value
                                  or len(value) > 512 or "\n" in value)
           for value in application.values()):
        raise ValueError("product_spec_application_invalid")
    if backend == "windows_pywinauto" and (
        bundle_id or (app_name and not re.fullmatch(r"[A-Za-z0-9_.-]+\.exe", app_name))
    ):
        raise ValueError("product_spec_application_invalid")
    is_absolute = ntpath.isabs if backend == "windows_pywinauto" else posixpath.isabs
    if any(path is not None and not is_absolute(path)
           for path in (application.get("path"), application.get("entry_path"))):
        raise ValueError("product_spec_application_path_invalid")
    intents = spec.get("intents", {})
    if not isinstance(intents, dict):
        raise ValueError("product_spec_intents_invalid")
    for intent_id, intent in intents.items():
        if (not isinstance(intent_id, str) or not isinstance(intent, dict)
                or intent.get("source_role") not in names
                or intent.get("result_role") not in names
                or not isinstance(intent.get("selector"), dict)
                or not intent["selector"]
                or not all(key in {"name", "automationId", "controlType"}
                           and isinstance(value, str) and value
                           for key, value in intent["selector"].items())):
            raise ValueError("product_spec_intents_invalid")
        assertions = intent.get("assertions")
        if (not isinstance(assertions, list) or not assertions
                or not all(isinstance(item, dict) and
                           item.get("type") in {"active_window_owner", "control_exists"}
                           and "value" in item for item in assertions)):
            raise ValueError("product_spec_assertions_invalid")
        visual = intent.get("visual")
        if visual is not None:
            if (not isinstance(visual, dict) or set(visual) - {
                    "template", "elementSha256", "contextTemplate", "contextSha256",
                    "contextOffset", "redacted", "relativePoint"}
                    or not isinstance(visual.get("template"), str)
                    or not visual["template"] or ntpath.isabs(visual["template"])
                    or posixpath.isabs(visual["template"])
                    or visual.get("redacted") is not True
                    or not isinstance(visual.get("elementSha256"), str)
                    or not re.fullmatch(r"sha256:[0-9a-f]{64}", visual["elementSha256"])):
                raise ValueError("product_spec_visual_invalid")
            if visual.get("contextTemplate") is not None:
                context_path = visual["contextTemplate"]
                offset = visual.get("contextOffset")
                if (not isinstance(context_path, str) or not context_path
                        or ntpath.isabs(context_path) or posixpath.isabs(context_path)
                        or not isinstance(visual.get("contextSha256"), str)
                        or not re.fullmatch(r"sha256:[0-9a-f]{64}", visual["contextSha256"])
                        or not isinstance(offset, (list, tuple)) or len(offset) != 2
                        or any(not isinstance(value, int) or isinstance(value, bool)
                               or value < 0 for value in offset)):
                    raise ValueError("product_spec_visual_invalid")
            elif "contextSha256" in visual or "contextOffset" in visual:
                raise ValueError("product_spec_visual_invalid")
            point = visual.get("relativePoint", [0.5, 0.5])
            if (not isinstance(point, (list, tuple)) or len(point) != 2
                    or any(isinstance(value, bool) or not isinstance(value, (int, float))
                           or not 0 <= value <= 1 for value in point)):
                raise ValueError("product_spec_visual_invalid")
    capability = ProductCapability(profile_id, version, backend,
                                   frozenset(raw_actions), tuple(roles))
    register_local_capability(capability)
    INSTALLED_PRODUCT_SPECS[(profile_id, backend)] = spec
    return capability


def register_app_catalog(catalog: dict[str, Any]) -> tuple[ProductCapability, ...]:
    """Install all app variants from one local catalog, or leave state unchanged."""
    global DEFAULT_APP_ID
    if not isinstance(catalog, dict) or catalog.get("schema_version") != 1:
        raise ValueError("app_catalog_version_invalid")
    apps = catalog.get("apps")
    default_app = catalog.get("default_app")
    if not isinstance(apps, list) or not apps or not isinstance(default_app, str):
        raise ValueError("app_catalog_invalid")
    prepared: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    seen_apps: set[str] = set()
    seen_profiles: set[tuple[str, str]] = set()
    for app in apps:
        if not isinstance(app, dict):
            raise ValueError("app_catalog_invalid")
        app_id, name, variants = (app.get(key) for key in ("app_id", "name", "profiles"))
        if (not isinstance(app_id, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", app_id)
                or app_id in seen_apps or not isinstance(name, str) or not name
                or not isinstance(variants, list) or not variants):
            raise ValueError("app_catalog_app_invalid")
        seen_apps.add(app_id)
        for variant in variants:
            if not isinstance(variant, dict) or not isinstance(variant.get("application"), dict):
                raise ValueError("app_catalog_profile_invalid")
            backend = variant.get("backend")
            profile_id = variant.get("profile_id")
            if (not isinstance(profile_id, str) or not isinstance(backend, str)
                    or backend not in VALID_BACKENDS):
                raise ValueError("app_catalog_profile_invalid")
            application = variant["application"]
            if not isinstance(application.get("path"), str) or not application["path"]:
                raise ValueError("app_catalog_path_required")
            windows = variant.get("windows")
            if not isinstance(windows, list) or not windows:
                raise ValueError("app_catalog_windows_required")
            if not all(isinstance(item, dict) and isinstance(item.get("role"), str)
                       and item["role"] for item in windows):
                raise ValueError("app_catalog_windows_invalid")
            by_role = {item["role"]: item for item in windows}
            if len(by_role) != len(windows):
                raise ValueError("app_catalog_windows_invalid")
            for window in windows:
                if (not isinstance(window, dict) or not isinstance(window.get("name"), str)
                        or not window["name"] or not isinstance(window.get("window_path"), str)):
                    raise ValueError("app_catalog_windows_invalid")
                exact_names = window.get("names", [])
                if not isinstance(exact_names, list) or not all(
                    isinstance(value, str) and value for value in exact_names
                ):
                    raise ValueError("app_catalog_windows_invalid")
                parent = window.get("parent_role")
                prefix = by_role.get(parent, {}).get("window_path") if parent else None
                expected_path = f"{prefix}/{window['role']}" if parent else window["role"]
                if ((parent and not isinstance(prefix, str))
                        or window["window_path"] != expected_path):
                    raise ValueError("app_catalog_window_path_invalid")
            key = (profile_id, backend)
            if key in seen_profiles or key in BUILTIN_CAPABILITIES:
                raise ValueError("app_catalog_profile_duplicate")
            seen_profiles.add(key)
            prepared.append((app_id, app, {
                **variant, "schema_version": 1, "app_id": app_id,
                "app_name": name, "window_roles": windows,
            }))
    if default_app not in seen_apps:
        raise ValueError("app_catalog_default_unknown")
    default_backends = [spec["backend"] for app_id, _, spec in prepared
                        if app_id == default_app]
    if len(default_backends) != len(VALID_BACKENDS) or set(default_backends) != set(VALID_BACKENDS):
        raise ValueError("app_catalog_default_backend_invalid")
    added: list[tuple[str, str]] = []
    try:
        capabilities = []
        for _, _, spec in prepared:
            capability = register_product_spec(spec)
            added.append((capability.profile_id, capability.backend))
            capabilities.append(capability)
    except Exception:
        for key in added:
            BUILTIN_CAPABILITIES.pop(key, None)
            INSTALLED_PRODUCT_SPECS.pop(key, None)
        raise
    INSTALLED_APPS.update({app_id: app for app_id, app, _ in prepared})
    DEFAULT_APP_ID = default_app
    return tuple(capabilities)


def default_profile_for_backend(backend: str) -> str:
    """Select the catalog's default application for one concrete backend."""
    if DEFAULT_APP_ID is None:
        raise ValueError("app_catalog_default_unknown")
    profiles = INSTALLED_APPS[DEFAULT_APP_ID]["profiles"]
    matches = [item["profile_id"] for item in profiles if item["backend"] == backend]
    if len(matches) != 1:
        raise ValueError("app_catalog_default_backend_unknown")
    return matches[0]


def load_installed_product_specs(path: Path | None = None) -> tuple[ProductCapability, ...]:
    """Load the one checked-in app catalog from the target package."""
    path = path or Path(__file__).with_name("product_specs") / "apps.json"
    if path.is_dir():
        path = path / "apps.json"
    return register_app_catalog(json.loads(path.read_text(encoding="utf-8")))


load_installed_product_specs()
