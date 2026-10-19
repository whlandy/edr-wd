"""Adapters for Browser Use-like page/API reads and EDR-WD MCP observations.

Both external integrations are injected, so this module does not depend on a
particular browser Skill transport or product-specific log selector.
"""

from __future__ import annotations

import re
import math
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from .check import Snapshot, SnapshotPending, _same_value


def _snapshot(source: str, raw: Mapping[str, Any], extra_evidence: tuple[str, ...] = ()) -> Snapshot:
    if not isinstance(raw, Mapping):
        raise ValueError("consistency_snapshot_invalid")
    def parse_moment(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("consistency_snapshot_invalid") from exc

    observed = parse_moment(raw.get("observed_at"))
    supporting = raw.get("supporting_times", ())
    if not isinstance(supporting, (list, tuple)):
        raise ValueError("consistency_snapshot_invalid")
    refs = raw.get("evidence_refs") or ()
    if not isinstance(refs, (list, tuple)):
        raise ValueError("consistency_snapshot_invalid")
    snapshot = Snapshot(
        source=source,
        device_id=raw.get("device_id"), policy_id=raw.get("policy_id"),
        observed_at=observed, fields=raw.get("fields"),
        evidence_refs=tuple(refs) + extra_evidence,
        supporting_times=tuple(parse_moment(value) for value in supporting),
    )
    snapshot.validate(source)
    return snapshot


def _merge_fields(left: Mapping[str, Any], right: Mapping[str, Any], evidence: tuple[str, ...]) -> dict:
    merged = dict(left)
    for key, value in right.items():
        if key not in merged:
            merged[key] = value
        elif isinstance(merged[key], Mapping) and isinstance(value, Mapping):
            merged[key] = _merge_fields(merged[key], value, evidence)
        elif not _same_value(merged[key], value):
            raise SnapshotPending("cloud", "cloud_page_api_conflict", evidence)
    return merged


class BrowserUseCloudReader:
    """Call the browser Skill twice, retaining independent page/API evidence."""

    def __init__(self, read_page: Callable[[], Mapping | None],
                 read_api: Callable[[], Mapping | None],
                 *, max_skew_seconds: float = 120.0) -> None:
        if (isinstance(max_skew_seconds, bool)
                or not isinstance(max_skew_seconds, (int, float))
                or not math.isfinite(max_skew_seconds)
                or not 0 <= max_skew_seconds <= 3600):
            raise ValueError("consistency_skew_invalid")
        self._read_page = read_page
        self._read_api = read_api
        self._max_skew = max_skew_seconds

    def __call__(self) -> Snapshot:
        page_pending = None
        api_pending = None
        try:
            raw_page = self._read_page()
        except SnapshotPending as exc:
            if exc.source != "cloud":
                raise ValueError("consistency_snapshot_invalid") from exc
            raw_page = None
            page_pending = exc
        # A page that has not synchronized must not suppress the independent
        # API read or its evidence from this polling attempt.
        try:
            raw_api = self._read_api()
        except SnapshotPending as exc:
            if exc.source != "cloud":
                raise ValueError("consistency_snapshot_invalid") from exc
            raw_api = None
            api_pending = exc
        page = _snapshot("cloud", raw_page) if raw_page is not None else None
        api = _snapshot("cloud", raw_api) if raw_api is not None else None
        if page is None or api is None:
            evidence = ((page.evidence_refs if page is not None else
                         page_pending.evidence_refs if page_pending is not None else ())
                        + (api.evidence_refs if api is not None else
                           api_pending.evidence_refs if api_pending is not None else ()))
            reasons = []
            if page is None:
                reasons.append(page_pending.reason if page_pending is not None
                               else "cloud_page_event_not_visible")
            if api is None:
                reasons.append(api_pending.reason if api_pending is not None
                               else "cloud_api_event_not_visible")
            raise SnapshotPending(
                "cloud", ";".join(reasons), evidence,
            )
        evidence = (*page.evidence_refs, *api.evidence_refs)
        if page.device_id != api.device_id or page.policy_id != api.policy_id:
            raise ValueError("cloud_page_api_identity_mismatch")
        if (not set(page.evidence_refs).difference(api.evidence_refs)
                or not set(api.evidence_refs).difference(page.evidence_refs)):
            raise ValueError("cloud_page_api_evidence_not_independent")
        skew = abs((page.observed_at.astimezone(timezone.utc)
                    - api.observed_at.astimezone(timezone.utc)).total_seconds())
        if skew > self._max_skew:
            raise SnapshotPending("cloud", "cloud_page_api_time_skew", evidence)
        fields = _merge_fields(page.fields, api.fields, evidence)
        return Snapshot("cloud", page.device_id, page.policy_id,
                        max(page.observed_at, api.observed_at), fields,
                        evidence, (page.observed_at, *page.supporting_times,
                                   api.observed_at, *api.supporting_times))


class EDRWDClientReader:
    """Read one validated locked-window bundle, then extract client/log fields."""

    def __init__(self, observation_provider: Any,
                 extract: Callable[[Mapping[str, Any]], Mapping[str, Any]]) -> None:
        self._provider = observation_provider
        self._extract = extract

    def __call__(self) -> Snapshot:
        observation_id = self._provider.refresh_bundle()
        bundle = self._provider.get_snapshot(observation_id)
        identity = bundle.get("window_identity") if isinstance(bundle, Mapping) else None
        digest = bundle.get("screenshot_sha256") if isinstance(bundle, Mapping) else None
        if (not isinstance(bundle, Mapping)
                or bundle.get("observation_id") != observation_id
                or bundle.get("screenshot_scope") != "window"
                or bundle.get("screenshot_redacted") is not True
                or not isinstance(digest, str)
                or re.fullmatch(r"sha256:[0-9a-f]{64}", digest) is None
                or not isinstance(identity, Mapping) or not identity.get("native_id")
                or not identity.get("generation")):
            raise ValueError("consistency_client_window_unverified")
        extracted = self._extract(bundle)
        if extracted is None:
            raise SnapshotPending("client", "client_event_not_visible",
                                  (f"edrwd:{observation_id}", str(digest)))
        return _snapshot("client", extracted, (f"edrwd:{observation_id}",
                                              str(bundle["screenshot_sha256"])))
