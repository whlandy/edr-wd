"""端侧录制可观测性回归：空录/租约告警与 verifier 分类指引。

对应 doc/edr-endpoint-recording-hardening.md P1：
  * SessionReceipt 新增 leaseSecondsRemaining 展示字段（RECORDING/PAUSED 计算剩余，
    其余状态 None）；
  * record stop 输出的 recording_stop_advisories：空录（区分租约过期）给重录指引，
    golden incomplete 按真实 issue 分类 —— 只有 verifier_required 才指引
    record assert，选择器歧义/窗口绑定各有修法，未登记码不臆造；
  * with_lease_warning：剩余 <60s 时追加警告，其余原样返回。
"""

from agent.cli import recording_stop_advisories, with_lease_warning
from target.recording.session import CaptureSessionState, SessionReceipt


# ── SessionReceipt.leaseSecondsRemaining ─────────────────────────


def test_receipt_carries_lease_seconds_remaining_when_recording():
    receipt = SessionReceipt(
        "REC-x", CaptureSessionState.RECORDING, 3, 1_000, "", 2,
        lease_seconds_remaining=120.0,
    )
    assert receipt.to_dict()["leaseSecondsRemaining"] == 120.0


def test_receipt_lease_remaining_none_for_stopped():
    receipt = SessionReceipt("REC-x", CaptureSessionState.STOPPED, 0, 1_000, "user", 0)
    assert receipt.to_dict()["leaseSecondsRemaining"] is None


# ── recording_stop_advisories：空录 ───────────────────────────────


def test_empty_recording_with_expired_lease_gets_retry_guidance():
    advisories = recording_stop_advisories(
        {"actionCount": 0, "reason": "recording_lease_expired"},
        [{"code": "compile_recording_empty", "sequence": 0}],
        "incomplete",
    )
    assert any("租约" in a and "recording_lease_expired" in a for a in advisories)
    assert any("record status" in a for a in advisories)   # 续租指引
    # 空录时不给 verifier 指引（没有可补断言的步骤）
    assert not any("record assert" in a for a in advisories)


def test_empty_recording_without_lease_reason_still_warns():
    advisories = recording_stop_advisories(
        {"actionCount": 0, "reason": "user"}, [], "incomplete")
    assert any("没有录到任何动作" in a for a in advisories)


# ── recording_stop_advisories：verifier 分类 ──────────────────────


def test_drag_verifier_required_suggests_record_assert_and_recompile():
    issues = [{"code": "compile_drag_verifier_required", "sequence": 5}]
    advisories = recording_stop_advisories({"actionCount": 10}, issues, "incomplete")
    assert any("record assert" in a and "record compile" in a for a in advisories)
    assert any("第 5 步" in a for a in advisories)
    # 措辞按真实根因：缺的是结果 verifier，不是模板
    assert any("结果 verifier" in a for a in advisories)


def test_selector_ambiguous_does_not_suggest_record_assert():
    issues = [{"code": "compile_selector_ambiguous", "sequence": 3}]
    advisories = recording_stop_advisories({"actionCount": 4}, issues, "incomplete")
    assert any("选择器" in a for a in advisories)
    assert not any("record assert" in a for a in advisories)


def test_weak_identity_ambiguous_advises_stable_semantic_identity():
    issues = [{"code": "compile_weak_or_ambiguous_identity", "sequence": 4}]
    advisories = recording_stop_advisories({"actionCount": 13}, issues, "incomplete")
    assert any("弱身份" in a and "唯一" in a for a in advisories)
    assert not any("record assert" in a for a in advisories)


def test_weak_identity_unresolved_advises_stable_automation_id():
    issues = [{"code": "compile_weak_identity_unresolved", "sequence": 4}]
    advisories = recording_stop_advisories({"actionCount": 13}, issues, "incomplete")
    assert any("弱身份" in a and "唯一" in a for a in advisories)
    assert not any("record assert" in a for a in advisories)


def test_unknown_issue_code_does_not_invent_a_fix():
    issues = [{"code": "some_future_code", "sequence": 7}]
    advisories = recording_stop_advisories({"actionCount": 4}, issues, "incomplete")
    assert any("some_future_code" in a and "以上游" in a for a in advisories)


def test_ready_golden_produces_no_advisories():
    assert recording_stop_advisories({"actionCount": 10}, [], "ready") == []


def test_verifier_guidance_covers_all_unready_codes(tmp_path=None):
    """scroll 与 drag 的 verifier_required 都必须指到 record assert。"""
    for code in ("compile_scroll_verifier_required",
                 "compile_drag_verifier_required"):
        advisories = recording_stop_advisories(
            {"actionCount": 2}, [{"code": code, "sequence": 9}], "incomplete")
        assert any("record assert" in a for a in advisories), code


# ── with_lease_warning ────────────────────────────────────────────


def test_lease_warning_added_when_under_60s():
    out = with_lease_warning({"ok": True, "state": "recording",
                              "leaseSecondsRemaining": 42.3})
    assert out["warnings"] and "42" in out["warnings"][0]
    assert out["state"] == "recording"


def test_lease_warning_not_added_when_plenty_left():
    out = with_lease_warning({"ok": True, "state": "recording",
                              "leaseSecondsRemaining": 250})
    assert "warnings" not in out


def test_lease_warning_not_added_for_stopped_state():
    out = with_lease_warning({"ok": True, "state": "stopped",
                              "leaseSecondsRemaining": 10})
    assert "warnings" not in out
