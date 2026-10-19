"""Product adapter registration and profile construction."""

from __future__ import annotations

from target.product_profiles import (BUILTIN_CAPABILITIES, ProductCapability,
                                     get_capability, register_local_capability)

from .edrclient import EDRClientAdapter
from .generic import GenericProductAdapter
from .models import ProductProfile, WindowRole


_adapters: dict[str, type] = {profile_id: EDRClientAdapter
                              for profile_id in EDRClientAdapter.profile_ids}


def normalize_backend(backend: str) -> str:
    """Map the legacy Windows status labels to the catalog backend ID."""
    return "windows_pywinauto" if backend in {"uia", "win32"} else backend


def backend_for_product(profile_id: str, *, backend_kind: str | None = None,
                        platform: str | None = None) -> str:
    if backend_kind:
        return normalize_backend(backend_kind)
    if platform:
        try:
            return {"windows": "windows_pywinauto",
                    "macos": "macos_accessibility"}[platform]
        except KeyError as exc:
            raise ValueError("product_platform_unknown") from exc
    matches = {backend for candidate_id, backend in BUILTIN_CAPABILITIES
               if candidate_id == profile_id}
    if len(matches) != 1:
        raise ValueError("product_backend_ambiguous_or_unknown")
    return next(iter(matches))


def register_product_adapter(profile_id: str, adapter: type,
                             capability: ProductCapability) -> None:
    if not profile_id or profile_id in _adapters or capability.profile_id != profile_id:
        raise ValueError("profile_id is empty or already registered")
    if not all(callable(getattr(adapter, name, None)) for name in
               ("resolve_window_role", "resolve_intent", "expected_result")):
        raise ValueError("product_adapter_contract_invalid")
    register_local_capability(capability)
    _adapters[profile_id] = adapter


def get_product_adapter(profile_id: str, backend: str):
    backend = normalize_backend(backend)
    get_product_profile(profile_id, backend)
    adapter = _adapters.get(profile_id)
    return adapter(backend) if adapter else GenericProductAdapter(profile_id, backend)


def get_product_profile(profile_id: str, backend: str) -> ProductProfile:
    backend = normalize_backend(backend)
    capability = get_capability(profile_id, backend)
    roles = tuple(WindowRole(role.role, role.process_names, role.title_pattern,
                             role.root_control_ids, role.parent_role)
                  for role in capability.window_roles)
    profile = ProductProfile(
        profile_id, capability.version, (backend,), tuple(roles),
        capability.allowed_action_ids, capability.digest,
    )
    profile.validate()
    return profile
