"""评审 P1-2:CLI --replay-mode 必须真正约束 v3 受控链。

semantic_only:声明纯视觉点击 → replay_selector_semantic_missing 报错,
不静默走视觉派发;visual_only/semantic_first 语义与 legacy 链对齐。
"""
from __future__ import annotations

import dataclasses
from types import SimpleNamespace

import pytest


def _visual_only_golden():
    """Reuse the adapter's effect golden; blank the control identity and
    attach a visual selector so the first step is a visual-only click."""
    import test_v3_real_server_adapter as adapter  # noqa: E402
    from agent.recording.models import GoldenTrace  # noqa: E402
    golden = adapter._compile_effect_golden()
    first = dataclasses.replace(
        golden.steps[0],
        selector=dataclasses.replace(
            golden.steps[0].selector, control={},
            visual={"template": "tmpl-demo", "redacted": True,
                       "elementSha256": "sha256:" + "0" * 64}))
    return GoldenTrace.from_dict(dataclasses.replace(
        golden, steps=(first, *golden.steps[1:])).to_dict())


def _run(golden, tmp_path, monkeypatch, replay_mode, visual_resolver=None):
    import target.server as server  # noqa: E402
    import test_v3_real_server_adapter as adapter  # noqa: E402
    from agent.recording.execution_ledger import (
        execute_v3_window_stage_real)  # noqa: E402
    from target.windowing.attempt_ledger import AttemptLedger  # noqa: E402
    backend = adapter._fake_backend_for("a_b_a")
    adapter._install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=adapter._A_B_A_ROLE_MAP,
        visual_resolver=visual_resolver,
        replay_mode=replay_mode)
    return result, backend


def test_semantic_only_refuses_visual_click(monkeypatch, tmp_path):
    """semantic_only + 纯视觉点击步 → replay_selector_semantic_missing,
    GUI 前 0 业务输入(不静默走视觉派发)。"""
    result, backend = _run(_visual_only_golden(), tmp_path, monkeypatch,
                           "semantic_only")
    assert result["latched"] is True
    assert result["dispatchInputs"] == 0, result["steps"]
    entry = result["steps"][0]
    assert entry["reason"] == "semantic_only but visual-only selector", entry
    # The durable rejection code rides error_payload in the ledger event.
    import test_v3_real_server_adapter as adapter  # noqa: E402
    from target.windowing.attempt_ledger import AttemptLedger  # noqa: E402
    events = AttemptLedger(tmp_path / "h").rebuild_history(
        run_id=result["runId"])
    assert events[0]["stateAfterRef"].get("rejectionCode") == \
        "replay_selector_semantic_missing", events[0]["stateAfterRef"]
    assert len(backend.dispatch_calls) == 0


def test_invalid_mode_refuses_before_any_side_effect(tmp_path, monkeypatch):
    """非法模式 → 在 begin/租约之前拒绝(0 副作用)。"""
    from agent.recording.execution_ledger import (
        execute_v3_window_stage_real)  # noqa: E402
    import target.server as server  # noqa: E402
    from target.windowing.attempt_ledger import AttemptLedger  # noqa: E402
    import test_v3_real_server_adapter as adapter  # noqa: E402
    golden = adapter._compile_effect_golden()
    backend = adapter._fake_backend_for("a_b_a")
    adapter._install_fake_os(monkeypatch, backend)
    with pytest.raises(ValueError, match="replay_mode_invalid"):
        execute_v3_window_stage_real(
            golden=golden, ledger=AttemptLedger(tmp_path / "h"),
            server_tools=server,
            window_role_map=adapter._A_B_A_ROLE_MAP,
            replay_mode="hover_and_pray")
    assert len(backend.dispatch_calls) == 0
    assert backend._enumerate_top_windows_windows.__self__ is backend


def test_semantic_first_default_passes_semantic_steps(monkeypatch, tmp_path):
    """semantic_first(默认):语义控件齐全的步照常语义派发(既有行为
    不回退)。"""
    import target.server as server  # noqa: E402
    import test_v3_real_server_adapter as adapter  # noqa: E402
    from agent.recording.execution_ledger import (
        execute_v3_window_stage_real)  # noqa: E402
    from target.windowing.attempt_ledger import AttemptLedger  # noqa: E402
    golden = adapter._compile_effect_golden()
    backend = adapter._fake_backend_for("a_b_a")
    adapter._install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server,
        window_role_map=adapter._A_B_A_ROLE_MAP,
        replay_mode="semantic_first")
    assert result["dispatchInputs"] >= 1
    assert result["steps"][0]["outcome"] == "pass", result["steps"][0]


def test_v3_artifacts_report_real_trajectory_path(tmp_path):
    """评审 P1-3:write_compilation_artifacts 对 v3 主轨迹(单夹布局)必须
    在返回对象里给出真实 trajectory_root/trace_v3_path — directory 仍是
    平铺资产根,但主轨迹不在那里;golden_trace 保持 None-v3 语义(不双写)。
    连续同名第二次编译分配新 TRJ,两次返回各自指向自己的修订。"""
    from pathlib import Path as _Path
    import target.recording.models as _trm
    from agent.recording import artifacts as _art
    from agent.recording.compiler import compile_recording
    import importlib.util as _ilu
    spec = _ilu.spec_from_file_location(
        "tg", str(_Path(__file__).resolve().parents[1] /
                   "test_recording" / "test_golden_v3_window_sequence.py"))
    tg = _ilu.module_from_spec(spec)
    spec.loader.exec_module(tg)
    recording = tg._with_timeline(tg._recording(
        [tg._click(1, 100, "CAUSE-1", 100, instance_id="RI-0001")],
        windows=[tg._MAIN]), instances=("RI-0001",))
    out = tmp_path / "record"

    first = _art.write_compilation_artifacts(out, recording,
                                             compile_recording(
                                                 recording,
                                                 profile="windows_hisec"))
    assert first.trajectory_root is not None
    assert first.trace_v3_path is not None
    assert first.trace_v3_path.is_file()
    assert first.trace_v3_path.name == "trace.v3.json"
    assert first.trajectory_root in first.trace_v3_path.parents
    # 平铺根不再持有主轨迹(唯一主轨迹 §5)。
    assert not (first.directory / "golden-trace.json").exists()

    second = _art.write_compilation_artifacts(out, recording,
                                              compile_recording(
                                                  recording,
                                                  profile="windows_hisec"))
    assert second.trajectory_root != first.trajectory_root, (
        "同名连续录制分配新 TRJ(§5 同名不覆盖)")
    assert second.trace_v3_path.is_file()
    assert first.trace_v3_path.is_file(), "首次修订不可变"
