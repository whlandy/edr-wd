"""Prompt-safe projection of exactly the locator's candidate set."""

from __future__ import annotations

from typing import Mapping

from agent.locator import HybridLocator, LocatorRequest


def candidate_view(bundle: Mapping, request: LocatorRequest) -> list[dict]:
    return [{
        "target_id": item.target_id,
        "observation_id": item.observation_id,
        "window_native_id": item.window_native_id,
        "rect": list(item.rect) if item.rect else None,
        "control_type": item.source.get("control_type"),
        "has_stable_id": bool(item.source.get("automation_id") or item.source.get("identifier")),
    } for item in HybridLocator().collect_candidates(request, bundle)]
