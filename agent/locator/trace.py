"""Persist metadata only; never raw OCR text or image pixels."""

from __future__ import annotations

from typing import Any

from agent.trace.events import EventType


def record_resolution_decision(trace_store: Any, result: Any) -> None:
    payload = {
        "observation_id": result.observation_id,
        "method": result.method,
        "evidence_types": list(result.evidence_types),
        "target_id": result.candidate.target_id if result.candidate else None,
    }
    trace_store.append_dict(EventType.OBSERVATION_RECORDED,
                            payload={"locator_decision": payload})
