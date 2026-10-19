# End–Cloud Consistency

The Agent orchestrates two read-only providers. `BrowserUseCloudReader` calls
the Browser Use-like Skill's page reader **and** API reader on every poll.
`EDRWDClientReader` calls `MCPObservationProvider.refresh_bundle()` on the
locked EDR-WD window and passes that observation to a product-specific
state/log extractor. Neither provider is inferred from the other; each raw
read is normalized into `agent.consistency.Snapshot` with:

- `source`: `cloud` or `client`;
- exact `device_id` and `policy_id`;
- timezone-aware **event** time in `observed_at` (not fetch time);
- optional timezone-aware `supporting_times` for additional page/API or client
  log event timestamps that must satisfy the same requested time window;
- a small `fields` mapping of comparable business values;
- opaque, redacted `evidence_refs` for page/API and client tree/log captures.

Page and API reads must each contribute at least one distinct evidence
reference; they may also share a session reference. A duplicated single read
cannot prove that both surfaces were checked. Field comparison is recursive
and keeps booleans distinct from numeric values (`true` is not `1`), while
equivalent numeric forms such as `1` and `1.0` remain comparable.

Configure the observation provider with an active product run token,
`use_bundle=True`, and `capture_screenshot=True`; focus/lock the intended
window role before polling. A tree-only provider is deliberately rejected
because it lacks a redacted, window-scoped image digest to prove ownership.

The page and API readers each return a small mapping with `device_id`,
`policy_id`, `observed_at`, `fields`, and nonempty `evidence_refs`; they may
also provide `supporting_times` as a list of timezone-aware ISO timestamps.
The client extractor accepts one validated, redacted, window-scoped EDR-WD
observation bundle and returns the same mapping for client state or a log
entry, including any additional event times used to derive its fields. It may
return `None` while the relevant event has not appeared; either cloud reader
may do the same. Missing events,
page/API field conflicts, and excessive page/API time skew are pending states
that are polled, with evidence retained; cross-device or cross-policy reads
fail closed. The extractor must select an event's own timestamp rather than
the observation capture time.

The caller constructs `ConsistencySpec` with the same device/policy IDs, event
time window, and explicit dotted field names. `check_consistency()` rejects
identity mismatches, waits through pending reads, stale event times and unequal
values up to `max_wait_seconds`, and returns field-level differences plus both
evidence references. Success requires **both** page and API event times, plus
the client event time and every declared supporting event time, inside the
requested window; selected fields must all be present and equal.

The provider contracts and polling are offline-tested. The Browser Use tool
binding and EDRClient-specific state/log selectors still require a configured
cloud session and reachable client target before claiming a live end–cloud
pass. Do not put credentials or raw screenshot/page content into `fields` or
evidence references.
