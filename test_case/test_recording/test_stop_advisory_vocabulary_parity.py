"""record stop 指引与编译器 issue 词汇的一致性回归（批次 1 可观测性收口）。

缺口（2026-10-30 18:4x 轮登记）：`recording_stop_advisories`（agent/cli.py，
2026-10-28 13:03 的 WIP）落后于编译器的 issue 词汇 —— 编译器在此后新增的
`compile_weak_identity_tree_undercount`（§6e）、`compile_weak_identity_volatile_label`
（§6w）、`recording_recorder_ui_events` / `recording_unidentified_target_events`
（§6u）没有专属指引，也不在 `known` 集合里；另有一个 WIP 自身不一致
（`compile_weak_or_ambiguous_identity` / `compile_weak_identity_unresolved` 有
专属指引却不在 `known`，会同时命中专属指引和「未登记」兜底）。操作者会看到
误导性的「还有未登记的编译缺口」而不是修法。

锁定两个契约：
1. 每个编译器真实会发出的 issue code 都有**专属**修法指引（不落兜底）；
2. `known` 集合必须覆盖全部已指引码（指引与兜底互斥）。

变异承重：从 `recording_stop_advisories` 删掉任一分支 → 对应测试变红。
"""

from agent.cli import recording_stop_advisories
from agent.recording.compiler import CompileIssue


def _issues(codes):
    return [{"code": c, "sequence": i + 1, "message": "m"}
            for i, c in enumerate(codes)]


def _stop(codes):
    return recording_stop_advisories(
        {"actionCount": 9}, _issues(codes), "incomplete")


# ── 每个未指引码都必须有专属指引（不得落「未登记」兜底） ────────────


def test_weak_identity_tree_undercount_advises_recapture():
    advisories = _stop(["compile_weak_identity_tree_undercount"])
    assert any("树" in a and ("唯一" in a or "重" in a) for a in advisories)
    assert not any("未登记" in a for a in advisories)


def test_weak_identity_volatile_label_advises_stable_identity():
    advisories = _stop(["compile_weak_identity_volatile_label"])
    assert any("标签" in a and "回放" in a for a in advisories)
    assert not any("未登记" in a for a in advisories)


def test_recorder_ui_events_advises_stop_touching_recorder_panel():
    advisories = _stop(["recording_recorder_ui_events"])
    assert any("录制面板" in a or "指示器" in a for a in advisories)
    assert not any("未登记" in a for a in advisories)


def test_unidentified_target_events_advises_reclick_nameable_target():
    advisories = _stop(["recording_unidentified_target_events"])
    assert any("无法命名" in a or "未识别" in a or "空白" in a for a in advisories)
    assert not any("未登记" in a for a in advisories)


def test_assertion_missing_expected_advises_expected_value():
    advisories = _stop(["compile_assertion_missing_expected"])
    assert any("expected" in a for a in advisories)
    assert not any("未登记" in a for a in advisories)


def test_action_unsupported_advises_unrecorded_action_type():
    advisories = _stop(["compile_action_unsupported"])
    assert any("不支持" in a or "动作类型" in a for a in advisories)
    assert not any("未登记" in a for a in advisories)


def test_region_container_unresolved_advises_stable_container_identity():
    advisories = _stop(["compile_region_container_unresolved"])
    assert any("容器" in a for a in advisories)
    # 安全语义不变：绝不指引降级到坐标
    assert not any("降级为坐标" in a and "不会" not in a for a in advisories)
    assert not any("未登记" in a for a in advisories)


# ── WIP 自身不一致：有专属指引的码不得再命中「未登记」兜底 ──────────


def test_weak_identity_ambiguous_not_double_reported():
    advisories = _stop(["compile_weak_or_ambiguous_identity"])
    assert any("弱身份" in a for a in advisories)
    assert not any("未登记" in a for a in advisories)


def test_weak_identity_unresolved_not_double_reported():
    advisories = _stop(["compile_weak_identity_unresolved"])
    assert any("弱身份" in a for a in advisories)
    assert not any("未登记" in a for a in advisories)


# ── 词汇全量一致性：真实编译器发出的每个码都被登记 ─────────────────


def test_every_compiler_issue_code_is_catalogued():
    """对编译器字面发出的全部 issue code 调一次 stop 指引：任何没有专属指引
    的码都会出现在「未登记」兜底文案里 —— 该测试随即失败，把词汇漂移在
    当轮拦住，而不是等操作者实机遇到。"""
    compiler_codes = [
        "compile_window_identity_conflict",
        "compile_window_effect_unverified",
        "recording_window_coverage_incomplete",
        "compile_window_source_unresolved",
        "compile_scroll_verifier_required",
        "compile_drag_verifier_required",
        "compile_selector_ambiguous",
        "compile_assertion_selector_ambiguous",
        "compile_assertion_missing_expected",
        "compile_action_unsupported",
        "compile_weak_identity_unresolved",
        "compile_weak_or_ambiguous_identity",
        "compile_weak_identity_tree_undercount",
        "compile_weak_identity_volatile_label",
        "compile_transition_unbound",
        "compile_window_identity_ambiguous",
        "compile_recording_empty",
        "compile_region_container_unresolved",
        "recording_packets_dropped",
        "recording_correlation_errors",
        "recording_recorder_ui_events",
        "recording_unidentified_target_events",
        "recording_secret_review_required",
        "recording_capture_evidence_unavailable",
    ]
    # 直接从编译器源码提取字面量，防手抄清单过期。
    import re as _re
    from pathlib import Path
    src = Path("agent/recording/compiler.py").read_text(encoding="utf-8")
    emitted = sorted(set(_re.findall(
        r'"((?:compile|recording)_[a-z_]+)"', src)))
    emitted = {c for c in emitted if c not in {
        # 非编译器产物：trace 事件类型/函数名等不含 CompileIssue 词汇的常量
        "compile_recording",
    }}
    assert emitted, "词汇提取不应为空（手抄清单+源码双保险）"
    assert set(compiler_codes) >= emitted, (
        f"compiler.py 出现了清单外的新 issue code: {set(emitted) - set(compiler_codes)}")

    # 两种沉寂都要拦：未登记码落「未登记」兜底；已登记却无专属分支的码
    # （§7n 曾把 window_identity 塞进 known 却不给指引）输出空 guidance。
    not_catalogued = []
    for code in compiler_codes:
        advisories = _stop([code])
        if not advisories or any("未登记" in a for a in advisories):
            not_catalogued.append(code)
    assert not not_catalogued, (
        f"这些编译器 issue code 没有专属指引（无输出或落「未登记」兜底）: {not_catalogued}")


def test_known_set_code_without_branch_is_caught():
    """登记进 known 却没有专属分支的码输出空指引 —— 这是 §7n 轮的真实缺口
    （batch 20 把 compile_window_identity_ambiguous 加进 known 但没写分支，
    操作者遇到同标题窗口编译缺口时一条建议都拿不到）。锁定：known 里每个
    真实编译器码单独调用时都必须至少产出一条指引。"""
    from agent.cli import _stop_advisory_known_codes
    for code in sorted(_stop_advisory_known_codes()):
        if code in {"compile_scroll_verifier_required", "compile_drag_verifier_required"}:
            continue  # 既有分支由上方语义测试锁定（record assert 指引）
        advisories = _stop([code])
        assert advisories, f"known 集合里的 {code} 没有任何专属指引（空输出）"
        assert not any("未登记" in a for a in advisories)


# ── 多码并发：专属指引逐码齐全，兜底不出现 ─────────────────────────


def test_mixed_issues_all_get_their_own_advisory():
    advisories = _stop([
        "compile_weak_identity_volatile_label",
        "recording_recorder_ui_events",
        "compile_drag_verifier_required",
    ])
    assert any("标签" in a for a in advisories)
    assert any("录制面板" in a or "指示器" in a for a in advisories)
    assert any("record assert" in a for a in advisories)
    assert not any("未登记" in a for a in advisories)


# ── 既有契约不回归 ────────────────────────────────────────────────


def test_existing_advisory_semantics_unchanged():
    # verifier 仍指 record assert；ready 仍无指引；未知码仍不臆造修法。
    assert any("record assert" in a for a in _stop(
        ["compile_drag_verifier_required"]))
    assert recording_stop_advisories({"actionCount": 10}, [], "ready") == []
    unknown = recording_stop_advisories(
        {"actionCount": 4}, [{"code": "some_future_code", "sequence": 7}],
        "incomplete")
    assert any("some_future_code" in a and "以上游" in a for a in unknown)
