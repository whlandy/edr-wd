"""
digest.py — Catalog digest (architecture §7.2).

`catalog_digest()` is `sha256:<hex>` over canonical JSON of the full
catalog, sorted by `action_id`, excluding live enablement. It changes
when ANY field of any ActionSpec changes, including non-semantic ones
(description), so consumers must treat it as a content fingerprint,
not a semantic compatibility key.

Determinism guarantees:

* JSON: `sort_keys=True`, `separators=(",", ":")`, `ensure_ascii=False`.
* Bytes: UTF-8.
* Order: by `action_id` ascending.
* Live state (`enabled`, `disabled_reason`) is NOT included.
"""

from __future__ import annotations

import hashlib
import json

from .models import ActionSpec


def canonical_catalog_bytes(specs: tuple[ActionSpec, ...]) -> bytes:
    """Serialize the catalog to deterministic UTF-8 JSON for digesting."""
    sorted_specs = sorted(specs, key=lambda s: s.action_id)
    payload = {
        "actions": [
            {
                "action_id": s.action_id,
                "action_code": s.action_code,
                "tool_name": s.tool_name,
                "description": s.description,
                "category": s.category,
                "input_schema": s.input_schema,
                "result_schema": s.result_schema,
                "backends": list(s.backends),
                "requires": list(s.requires),
                "side_effect": s.side_effect,
                "risk": s.risk,
                "rollback_class": s.rollback_class,
                "default_screenshot": s.default_screenshot,
                "transition_policy": s.transition_policy,
                "preferred_over": list(s.preferred_over),
                "execution_provider": s.execution_provider,
            }
            for s in sorted_specs
        ],
    }
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def catalog_digest(specs: tuple[ActionSpec, ...] | None = None) -> str:
    """Return sha256:<hex> over the canonical catalog bytes.

    When `specs` is None, uses the V1 entries. Tests that want to
    probe digest stability under mutation pass an explicit tuple.
    """
    if specs is None:
        from .actions_v1 import ACTIONS_V1
        specs = ACTIONS_V1
    return "sha256:" + hashlib.sha256(canonical_catalog_bytes(specs)).hexdigest()


__all__ = ["canonical_catalog_bytes", "catalog_digest"]