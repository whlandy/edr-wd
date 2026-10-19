"""Browser-Use/EDR-WD provider contract tests without external services."""

from datetime import datetime, timedelta, timezone

import pytest

from agent.consistency import (
    BrowserUseCloudReader, ConsistencySpec, EDRWDClientReader,
    Snapshot, SnapshotPending, check_consistency,
)


pytestmark = pytest.mark.unit
NOW = datetime(2026, 10, 16, 10, 10, tzinfo=timezone.utc)


def _snapshot(source, enabled, *, device="device-1", policy="policy-1", at=NOW):
    return Snapshot(source, device, policy, at,
                    {"policy": {"enabled": enabled}}, (f"{source}:evidence-1",))


def _spec(**overrides):
    values = dict(device_id="device-1", policy_id="policy-1",
                  start_at=NOW - timedelta(seconds=10), end_at=NOW + timedelta(seconds=10),
                  field_names=("policy.enabled",), max_wait_seconds=5, poll_interval_seconds=1)
    values.update(overrides)
    return ConsistencySpec(**values)


def test_poll_waits_for_matching_client_state_and_preserves_evidence_refs():
    states = iter([False, False, True])
    current = [0.0]
    result = check_consistency(
        _spec(), fetch_cloud=lambda: _snapshot("cloud", True),
        fetch_client=lambda: _snapshot("client", next(states)),
        clock=lambda: current[0], sleep=lambda seconds: current.__setitem__(0, current[0] + seconds),
    )
    assert result.passed and result.attempts == 3
    assert result.cloud_evidence == ("cloud:evidence-1",)
    assert result.client_evidence == ("client:evidence-1",)


def test_timeout_reports_field_diff_with_both_values():
    current = [0.0]
    result = check_consistency(
        _spec(max_wait_seconds=2), fetch_cloud=lambda: _snapshot("cloud", True),
        fetch_client=lambda: _snapshot("client", False),
        clock=lambda: current[0], sleep=lambda seconds: current.__setitem__(0, current[0] + seconds),
    )
    assert not result.passed and result.code == "consistency_timeout"
    assert result.differences[0] == {
        "field": "policy.enabled", "cloud": True, "client": False,
        "cloud_present": True, "client_present": True,
    }


def test_boolean_and_numeric_state_are_not_considered_consistent():
    result = check_consistency(
        _spec(max_wait_seconds=0), fetch_cloud=lambda: _snapshot("cloud", True),
        fetch_client=lambda: _snapshot("client", 1),
        clock=lambda: 0.0, sleep=lambda _: pytest.fail("should not sleep"),
    )
    assert not result.passed
    assert result.differences[0]["field"] == "policy.enabled"


def test_nested_boolean_mismatch_is_detected_but_numeric_forms_can_match():
    def read(source, enabled):
        return Snapshot(source, "device-1", "policy-1", NOW,
                        {"policy": {"rules": [{"enabled": enabled, "weight": 1}]}},
                        (f"{source}:1",))

    mismatch = check_consistency(
        _spec(field_names=("policy.rules",), max_wait_seconds=0),
        fetch_cloud=lambda: read("cloud", True),
        fetch_client=lambda: read("client", 1),
        clock=lambda: 0.0, sleep=lambda _: pytest.fail("should not sleep"),
    )
    assert not mismatch.passed

    numeric = check_consistency(
        _spec(field_names=("policy.rules",), max_wait_seconds=0),
        fetch_cloud=lambda: read("cloud", 1),
        fetch_client=lambda: Snapshot("client", "device-1", "policy-1", NOW,
                                      {"policy": {"rules": [{"enabled": 1.0,
                                                              "weight": 1.0}]}},
                                      ("client:1",)),
        clock=lambda: 0.0, sleep=lambda _: pytest.fail("should not sleep"),
    )
    assert numeric.passed


def test_cloud_pending_still_reads_client_and_keeps_both_evidence_sets():
    calls = []

    def cloud():
        calls.append("cloud")
        raise SnapshotPending("cloud", "cloud_page_event_not_visible", ("page:pending",))

    def client():
        calls.append("client")
        return _snapshot("client", True)

    result = check_consistency(
        _spec(max_wait_seconds=0), fetch_cloud=cloud, fetch_client=client,
        clock=lambda: 0.0, sleep=lambda _: pytest.fail("should not sleep"),
    )
    assert calls == ["cloud", "client"]
    assert result.differences == ({"field": "cloud", "reason": "cloud_page_event_not_visible"},)
    assert result.cloud_evidence == ("page:pending",)
    assert result.client_evidence == ("client:evidence-1",)


def test_both_pending_report_each_reason_and_evidence():
    def cloud():
        raise SnapshotPending("cloud", "cloud_api_event_not_visible", ("page:1",))

    def client():
        raise SnapshotPending("client", "client_event_not_visible", ("edrwd:OBS-1",))

    result = check_consistency(
        _spec(max_wait_seconds=0), fetch_cloud=cloud, fetch_client=client,
        clock=lambda: 0.0, sleep=lambda _: pytest.fail("should not sleep"),
    )
    assert result.differences == (
        {"field": "cloud", "reason": "cloud_api_event_not_visible"},
        {"field": "client", "reason": "client_event_not_visible"},
    )
    assert result.cloud_evidence == ("page:1",)
    assert result.client_evidence == ("edrwd:OBS-1",)


def test_timeout_keeps_last_nonempty_evidence_when_both_reads_turn_pending():
    current = [0.0]
    attempts = [0]

    def cloud():
        attempts[0] += 1
        if attempts[0] > 1:
            raise SnapshotPending("cloud", "page_temporarily_unavailable")
        return _snapshot("cloud", True)

    def client():
        if attempts[0] > 1:
            raise SnapshotPending("client", "log_temporarily_unavailable")
        return _snapshot("client", False)

    result = check_consistency(
        _spec(max_wait_seconds=1), fetch_cloud=cloud, fetch_client=client,
        clock=lambda: current[0],
        sleep=lambda seconds: current.__setitem__(0, current[0] + seconds),
    )

    assert not result.passed and result.code == "consistency_timeout"
    assert result.attempts == 2
    assert result.differences == (
        {"field": "cloud", "reason": "page_temporarily_unavailable"},
        {"field": "client", "reason": "log_temporarily_unavailable"},
    )
    assert result.cloud_evidence == ("cloud:evidence-1",)
    assert result.client_evidence == ("client:evidence-1",)


def test_identity_mismatch_fails_closed_without_waiting():
    result = check_consistency(
        _spec(), fetch_cloud=lambda: _snapshot("cloud", True),
        fetch_client=lambda: _snapshot("client", True, device="device-2"),
        clock=lambda: 0.0, sleep=lambda _: pytest.fail("should not sleep"),
    )
    assert result.code == "consistency_identity_mismatch" and result.attempts == 1


@pytest.mark.parametrize("field,value", [
    ("max_wait_seconds", float("nan")),
    ("max_wait_seconds", float("inf")),
    ("max_wait_seconds", True),
    ("poll_interval_seconds", float("nan")),
    ("poll_interval_seconds", float("-inf")),
    ("poll_interval_seconds", False),
    ("device_id", 123),
    ("policy_id", True),
    ("field_names", "policy.enabled"),
])
def test_invalid_correlation_or_polling_spec_is_rejected_before_fetch(field, value):
    def no_fetch():
        pytest.fail("invalid spec must not read either side")

    with pytest.raises(ValueError, match="consistency_spec_invalid"):
        check_consistency(_spec(**{field: value}), fetch_cloud=no_fetch,
                          fetch_client=no_fetch)


@pytest.mark.parametrize("skew", [float("nan"), float("inf"), True, "120"])
def test_browser_reader_rejects_nonfinite_or_nonnumeric_skew(skew):
    with pytest.raises(ValueError, match="consistency_skew_invalid"):
        BrowserUseCloudReader(lambda: None, lambda: None,
                              max_skew_seconds=skew)


def test_stale_event_time_is_polled_then_rejected_with_evidence():
    current = [0.0]
    result = check_consistency(
        _spec(max_wait_seconds=1), fetch_cloud=lambda: _snapshot("cloud", True),
        fetch_client=lambda: _snapshot("client", True, at=NOW - timedelta(days=1)),
        clock=lambda: current[0], sleep=lambda seconds: current.__setitem__(0, current[0] + seconds),
    )
    assert result.code == "consistency_timeout"
    assert result.differences[0]["reason"] == "outside_time_window"


def _cloud_raw(fields, evidence, *, at=NOW, device="device-1", policy="policy-1"):
    return {"device_id": device, "policy_id": policy, "observed_at": at.isoformat(),
            "fields": fields, "evidence_refs": [evidence]}


def test_browser_reader_combines_page_and_api_without_dropping_evidence():
    reader = BrowserUseCloudReader(
        lambda: _cloud_raw({"policy": {"enabled": True}}, "page:1"),
        lambda: _cloud_raw({"policy": {"name": "P"}}, "api:1"),
    )
    snapshot = reader()
    assert snapshot.fields == {"policy": {"enabled": True, "name": "P"}}
    assert snapshot.evidence_refs == ("page:1", "api:1")
    assert snapshot.supporting_times == (NOW, NOW)


def test_browser_reader_preserves_page_and_api_event_times():
    page = _cloud_raw({"policy": {"enabled": True}}, "page:1")
    api = _cloud_raw({"policy": {"enabled": True}}, "api:1")
    page_event = NOW - timedelta(seconds=3)
    api_event = NOW - timedelta(seconds=2)
    page["supporting_times"] = [page_event.isoformat()]
    api["supporting_times"] = [api_event.isoformat()]

    snapshot = BrowserUseCloudReader(lambda: page, lambda: api)()
    assert snapshot.supporting_times == (NOW, page_event, NOW, api_event)


def test_browser_reader_conflicting_page_and_api_values_are_pending():
    reader = BrowserUseCloudReader(
        lambda: _cloud_raw({"policy": {"enabled": True}}, "page:1"),
        lambda: _cloud_raw({"policy": {"enabled": False}}, "api:1"),
    )
    with pytest.raises(SnapshotPending, match="cloud_page_api_conflict") as exc:
        reader()
    assert exc.value.evidence_refs == ("page:1", "api:1")


def test_browser_reader_does_not_merge_boolean_with_numeric_field():
    reader = BrowserUseCloudReader(
        lambda: _cloud_raw({"policy": {"enabled": True}}, "page:1"),
        lambda: _cloud_raw({"policy": {"enabled": 1}}, "api:1"),
    )
    with pytest.raises(SnapshotPending, match="cloud_page_api_conflict"):
        reader()


def test_browser_reader_rejects_cross_device_page_and_api():
    reader = BrowserUseCloudReader(
        lambda: _cloud_raw({}, "page:1"),
        lambda: _cloud_raw({}, "api:1", device="device-2"),
    )
    with pytest.raises(ValueError, match="cloud_page_api_identity_mismatch"):
        reader()


def test_browser_reader_requires_independent_page_and_api_evidence():
    reader = BrowserUseCloudReader(
        lambda: _cloud_raw({"policy": {"enabled": True}}, "browser:one-read"),
        lambda: _cloud_raw({"policy": {"enabled": True}}, "browser:one-read"),
    )
    with pytest.raises(ValueError, match="cloud_page_api_evidence_not_independent"):
        reader()


def test_browser_reader_allows_shared_session_ref_with_distinct_read_refs():
    page = _cloud_raw({"policy": {"enabled": True}}, "page:1")
    api = _cloud_raw({"policy": {"enabled": True}}, "api:1")
    page["evidence_refs"].insert(0, "browser:session")
    api["evidence_refs"].insert(0, "browser:session")

    snapshot = BrowserUseCloudReader(lambda: page, lambda: api)()
    assert snapshot.evidence_refs == (
        "browser:session", "page:1", "browser:session", "api:1",
    )


def test_browser_reader_rejects_page_api_time_skew_and_retains_evidence():
    reader = BrowserUseCloudReader(
        lambda: _cloud_raw({}, "page:1"),
        lambda: _cloud_raw({}, "api:1", at=NOW + timedelta(minutes=3)),
        max_skew_seconds=60,
    )
    with pytest.raises(SnapshotPending, match="cloud_page_api_time_skew") as exc:
        reader()
    assert exc.value.evidence_refs == ("page:1", "api:1")


def test_browser_reader_waits_for_api_event_and_keeps_page_evidence():
    api_calls = [0]

    def read_api():
        api_calls[0] += 1
        return None if api_calls[0] == 1 else _cloud_raw(
            {"policy": {"enabled": True}}, "api:1")

    reader = BrowserUseCloudReader(
        lambda: _cloud_raw({"policy": {"enabled": True}}, "page:1"), read_api)
    current = [0.0]
    result = check_consistency(
        _spec(), fetch_cloud=reader,
        fetch_client=lambda: _snapshot("client", True),
        clock=lambda: current[0], sleep=lambda seconds: current.__setitem__(0, current[0] + seconds),
    )
    assert result.passed and result.attempts == 2
    assert result.cloud_evidence == ("page:1", "api:1")


def test_browser_reader_keeps_api_evidence_when_page_is_pending():
    reader = BrowserUseCloudReader(
        lambda: None,
        lambda: _cloud_raw({"policy": {"enabled": True}}, "api:1"),
    )
    with pytest.raises(SnapshotPending, match="cloud_page_event_not_visible") as exc:
        reader()
    assert exc.value.evidence_refs == ("api:1",)


def test_browser_reader_reads_both_sources_when_both_are_pending():
    calls = []
    reader = BrowserUseCloudReader(
        lambda: calls.append("page"),
        lambda: calls.append("api"),
    )
    with pytest.raises(SnapshotPending, match="cloud_page_event_not_visible"):
        reader()
    assert calls == ["page", "api"]


def test_browser_reader_keeps_api_evidence_when_page_raises_pending():
    calls = []

    def read_page():
        calls.append("page")
        raise SnapshotPending("cloud", "page_loading", ("page:loading",))

    def read_api():
        calls.append("api")
        return _cloud_raw({"policy": {"enabled": True}}, "api:1")

    with pytest.raises(SnapshotPending, match="page_loading") as exc:
        BrowserUseCloudReader(read_page, read_api)()
    assert calls == ["page", "api"]
    assert exc.value.evidence_refs == ("page:loading", "api:1")


def test_browser_reader_reports_both_pending_reasons_and_evidence():
    calls = []

    def pending(source, reason, ref):
        def read():
            calls.append(source)
            raise SnapshotPending("cloud", reason, (ref,))
        return read

    reader = BrowserUseCloudReader(
        pending("page", "page_loading", "page:loading"),
        pending("api", "api_loading", "api:loading"),
    )
    with pytest.raises(SnapshotPending) as exc:
        reader()
    assert calls == ["page", "api"]
    assert exc.value.reason == "page_loading;api_loading"
    assert exc.value.evidence_refs == ("page:loading", "api:loading")


class _BundleProvider:
    def __init__(self, *, valid=True):
        self.valid = valid

    def refresh_bundle(self):
        return "OBS-1"

    def get_snapshot(self, observation_id):
        assert observation_id == "OBS-1"
        return {"observation_id": "OBS-1", "screenshot_scope": "window",
                "screenshot_redacted": self.valid,
                "screenshot_sha256": "sha256:" + "a" * 64,
                "window_identity": {"native_id": "42", "generation": 1},
                "controls": []}


def test_client_reader_requires_locked_redacted_bundle_and_adds_evidence():
    extracted = lambda bundle: _cloud_raw({"policy": {"enabled": True}}, "client:log:1")
    reader = EDRWDClientReader(_BundleProvider(), extracted)
    snapshot = reader()
    assert snapshot.source == "client"
    assert snapshot.evidence_refs == ("client:log:1", "edrwd:OBS-1", "sha256:" + "a" * 64)
    with pytest.raises(ValueError, match="consistency_client_window_unverified"):
        EDRWDClientReader(_BundleProvider(valid=False), extracted)()


def test_client_event_outside_window_cannot_be_hidden_by_observation_time():
    client_raw = _cloud_raw({"policy": {"enabled": True}}, "client:log:1")
    client_raw["supporting_times"] = [(NOW - timedelta(days=1)).isoformat()]
    current = [0.0]
    result = check_consistency(
        _spec(max_wait_seconds=0),
        fetch_cloud=lambda: _snapshot("cloud", True),
        fetch_client=EDRWDClientReader(_BundleProvider(), lambda _bundle: client_raw),
        clock=lambda: current[0], sleep=lambda _: pytest.fail("should not sleep"),
    )
    assert result.code == "consistency_timeout"
    assert result.differences[0]["reason"] == "outside_time_window"


def test_client_reader_rejects_naive_supporting_event_time():
    client_raw = _cloud_raw({"policy": {"enabled": True}}, "client:log:1")
    client_raw["supporting_times"] = ["2026-10-19T10:10:00"]
    with pytest.raises(ValueError, match="consistency_snapshot_invalid"):
        EDRWDClientReader(_BundleProvider(), lambda _bundle: client_raw)()


@pytest.mark.parametrize("mutation", [
    {"observation_id": "OBS-OTHER"},
    {"screenshot_sha256": "sha256:abc"},
    {"window_identity": {"native_id": "42"}},
])
def test_client_reader_rejects_unbound_or_incomplete_bundle(mutation):
    class InvalidBundle(_BundleProvider):
        def get_snapshot(self, observation_id):
            return {**super().get_snapshot(observation_id), **mutation}

    with pytest.raises(ValueError, match="consistency_client_window_unverified"):
        EDRWDClientReader(InvalidBundle(), lambda bundle: _cloud_raw({}, "client:1"))()


def test_client_reader_missing_event_is_pending_and_checker_polls():
    calls = [0]

    def extract(bundle):
        calls[0] += 1
        if calls[0] < 3:
            return None
        return _cloud_raw({"policy": {"enabled": True}}, "client:log:1")

    current = [0.0]
    result = check_consistency(
        _spec(), fetch_cloud=lambda: _snapshot("cloud", True),
        fetch_client=EDRWDClientReader(_BundleProvider(), extract),
        clock=lambda: current[0], sleep=lambda seconds: current.__setitem__(0, current[0] + seconds),
    )
    assert result.passed and result.attempts == 3
    assert result.client_evidence[-2:] == ("edrwd:OBS-1", "sha256:" + "a" * 64)


def test_client_pending_keeps_redacted_bundle_digest_on_timeout():
    current = [0.0]
    result = check_consistency(
        _spec(max_wait_seconds=0), fetch_cloud=lambda: _snapshot("cloud", True),
        fetch_client=EDRWDClientReader(_BundleProvider(), lambda _bundle: None),
        clock=lambda: current[0], sleep=lambda _: pytest.fail("should not sleep"),
    )
    assert result.differences == (
        {"field": "client", "reason": "client_event_not_visible"},
    )
    assert result.cloud_evidence == ("cloud:evidence-1",)
    assert result.client_evidence == ("edrwd:OBS-1", "sha256:" + "a" * 64)


def test_checker_rejects_cloud_page_event_outside_window_even_if_api_is_current():
    cloud = Snapshot("cloud", "device-1", "policy-1", NOW,
                     {"policy": {"enabled": True}}, ("page:1", "api:1"),
                     (NOW - timedelta(days=1), NOW))
    current = [0.0]
    result = check_consistency(
        _spec(max_wait_seconds=1), fetch_cloud=lambda: cloud,
        fetch_client=lambda: _snapshot("client", True),
        clock=lambda: current[0], sleep=lambda seconds: current.__setitem__(0, current[0] + seconds),
    )
    assert result.code == "consistency_timeout"
    assert result.differences[0]["reason"] == "outside_time_window"
