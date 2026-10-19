"""Correlate Browser-Use cloud evidence with EDR-WD client observations.

The caller supplies two read-only fetch functions. The cloud fetcher is the
Browser-Use Skill boundary; the client fetcher is the EDR-WD MCP boundary.
This module never stores raw page content, credentials, or screenshots.
"""

from __future__ import annotations

import time
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping


class SnapshotPending(RuntimeError):
    """A read-only provider has not yet observed the correlated event."""

    def __init__(self, source: str, reason: str, evidence_refs: tuple[str, ...] = ()):
        super().__init__(reason)
        self.source = source
        self.reason = reason
        self.evidence_refs = evidence_refs


@dataclass(frozen=True)
class Snapshot:
    source: str
    device_id: str
    policy_id: str
    observed_at: datetime
    fields: Mapping[str, Any]
    evidence_refs: tuple[str, ...] = ()
    supporting_times: tuple[datetime, ...] = ()

    def validate(self, expected_source: str) -> None:
        if (self.source != expected_source or not self.device_id or not self.policy_id
                or not isinstance(self.observed_at, datetime) or self.observed_at.tzinfo is None
                or not isinstance(self.fields, Mapping)
                or not self.evidence_refs
                or not all(isinstance(ref, str) and ref for ref in self.evidence_refs)
                or any(not isinstance(moment, datetime) or moment.tzinfo is None
                       for moment in self.supporting_times)):
            raise ValueError("consistency_snapshot_invalid")


@dataclass(frozen=True)
class ConsistencySpec:
    device_id: str
    policy_id: str
    start_at: datetime
    end_at: datetime
    field_names: tuple[str, ...]
    max_wait_seconds: float = 60.0
    poll_interval_seconds: float = 2.0

    def validate(self) -> None:
        if (not isinstance(self.device_id, str) or not self.device_id
                or not isinstance(self.policy_id, str) or not self.policy_id
                or not isinstance(self.field_names, tuple) or not self.field_names
                or any(not isinstance(name, str) or not name for name in self.field_names)
                or not isinstance(self.start_at, datetime) or not isinstance(self.end_at, datetime)
                or self.start_at.tzinfo is None or self.end_at.tzinfo is None
                or self.end_at < self.start_at
                or isinstance(self.max_wait_seconds, bool)
                or not isinstance(self.max_wait_seconds, (int, float))
                or not math.isfinite(self.max_wait_seconds)
                or not 0 <= self.max_wait_seconds <= 3600
                or isinstance(self.poll_interval_seconds, bool)
                or not isinstance(self.poll_interval_seconds, (int, float))
                or not math.isfinite(self.poll_interval_seconds)
                or not 0 < self.poll_interval_seconds <= 60):
            raise ValueError("consistency_spec_invalid")


@dataclass(frozen=True)
class ConsistencyResult:
    passed: bool
    code: str
    attempts: int
    differences: tuple[Mapping[str, Any], ...]
    cloud_evidence: tuple[str, ...]
    client_evidence: tuple[str, ...]


def _field(fields: Mapping[str, Any], name: str) -> tuple[bool, Any]:
    value: Any = fields
    for part in name.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return False, None
        value = value[part]
    return True, value


def _same_value(left: Any, right: Any) -> bool:
    """Compare JSON-like business fields without Python's bool == int alias."""
    if isinstance(left, bool) or isinstance(right, bool):
        return isinstance(left, bool) and isinstance(right, bool) and left is right
    if isinstance(left, Mapping) or isinstance(right, Mapping):
        return (isinstance(left, Mapping) and isinstance(right, Mapping)
                and left.keys() == right.keys()
                and all(_same_value(left[key], right[key]) for key in left))
    if isinstance(left, (list, tuple)) or isinstance(right, (list, tuple)):
        return (isinstance(left, (list, tuple)) and isinstance(right, (list, tuple))
                and len(left) == len(right)
                and all(_same_value(a, b) for a, b in zip(left, right)))
    return left == right


def _compare(cloud: Snapshot, client: Snapshot, names: tuple[str, ...]) -> tuple[Mapping[str, Any], ...]:
    differences = []
    for name in names:
        cloud_found, cloud_value = _field(cloud.fields, name)
        client_found, client_value = _field(client.fields, name)
        if not cloud_found or not client_found or not _same_value(cloud_value, client_value):
            differences.append({
                "field": name,
                "cloud": cloud_value if cloud_found else None,
                "client": client_value if client_found else None,
                "cloud_present": cloud_found,
                "client_present": client_found,
            })
    return tuple(differences)


def check_consistency(
    spec: ConsistencySpec, *, fetch_cloud: Callable[[], Snapshot],
    fetch_client: Callable[[], Snapshot], clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> ConsistencyResult:
    """Poll for synchronization; never accept a mismatched correlation key.

    Timestamps are compared in UTC. A snapshot may arrive slightly after the
    requested window while synchronization is pending, but it must still
    describe that window: callers should set ``observed_at`` to the product
    event time, not the fetch time.
    """
    spec.validate()
    deadline = clock() + spec.max_wait_seconds
    attempts = 0
    last_cloud_evidence: tuple[str, ...] = ()
    last_client_evidence: tuple[str, ...] = ()
    while True:
        attempts += 1
        cloud: Snapshot | None = None
        client: Snapshot | None = None
        pending: list[SnapshotPending] = []
        try:
            cloud = fetch_cloud()
            if not isinstance(cloud, Snapshot):
                raise ValueError("consistency_snapshot_invalid")
            cloud.validate("cloud")
            last_cloud_evidence = cloud.evidence_refs
        except SnapshotPending as not_ready:
            if not_ready.source != "cloud":
                raise ValueError("consistency_snapshot_invalid") from not_ready
            last_cloud_evidence = not_ready.evidence_refs
            pending.append(not_ready)
        # Read both sides on every attempt: a pending cloud page must not
        # erase the client evidence needed to explain a synchronization timeout.
        try:
            client = fetch_client()
            if not isinstance(client, Snapshot):
                raise ValueError("consistency_snapshot_invalid")
            client.validate("client")
            last_client_evidence = client.evidence_refs
        except SnapshotPending as not_ready:
            if not_ready.source != "client":
                raise ValueError("consistency_snapshot_invalid") from not_ready
            last_client_evidence = not_ready.evidence_refs
            pending.append(not_ready)
        for snapshot in (cloud, client):
            if snapshot is not None and (
                snapshot.device_id != spec.device_id or snapshot.policy_id != spec.policy_id
            ):
                return ConsistencyResult(False, "consistency_identity_mismatch", attempts, (),
                                         last_cloud_evidence, last_client_evidence)
        if pending:
            remaining = deadline - clock()
            if remaining <= 0:
                return ConsistencyResult(False, "consistency_timeout", attempts,
                                         tuple({"field": item.source, "reason": item.reason}
                                               for item in pending),
                                         last_cloud_evidence, last_client_evidence)
            sleep(min(spec.poll_interval_seconds, remaining))
            continue
        assert cloud is not None and client is not None
        in_window = all(
            spec.start_at.astimezone(timezone.utc) <= moment.astimezone(timezone.utc)
            <= spec.end_at.astimezone(timezone.utc)
            for snapshot in (cloud, client)
            for moment in (snapshot.observed_at, *snapshot.supporting_times)
        )
        differences = _compare(cloud, client, spec.field_names) if in_window else ({
            "field": "observed_at", "cloud": cloud.observed_at.isoformat(),
            "client": client.observed_at.isoformat(), "reason": "outside_time_window",
        },)
        if not differences:
            return ConsistencyResult(True, "consistent", attempts, (),
                                     cloud.evidence_refs, client.evidence_refs)
        remaining = deadline - clock()
        if remaining <= 0:
            return ConsistencyResult(False, "consistency_timeout", attempts, differences,
                                     cloud.evidence_refs, client.evidence_refs)
        sleep(min(spec.poll_interval_seconds, remaining))
