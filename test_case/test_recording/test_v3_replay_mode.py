"""评审 P1-2:CLI --replay-mode 必须真正约束 v3 受控链。

semantic_only:声明纯视觉点击 → replay_selector_semantic_missing 报错,
不静默走视觉派发;visual_only/semantic_first 语义与 legacy 链对齐。

行为级三模式矩阵(断言 resolver 调用次数 + 业务派发次数/方法):
- visual_only:全视觉定位,0 语义派发;纯语义步 GUI 前拒绝
  (visual_only_unsupported);无 resolver 拒绝;
- semantic_first:语义命中不碰 resolver;候选空 → 视觉回退(A023);
- semantic_only:带视觉条件的语义步仍走语义(禁视觉派发)。
"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import io
from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def _clean_lease():
    """与 adapter 模块同款:每测重置产品策略/facade,防跨文件残留。"""
    from target import product_policy
    import target.server as server
    product_policy.reset_for_tests()
    server._binding_facade_holder["facade"] = None
    yield
    product_policy.reset_for_tests()
    server._binding_facade_holder["facade"] = None


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


# ---- 行为级三模式矩阵:resolver 调用次数 × 业务派发次数/方法 ----


class _CountingResolver:
    """计数包装:测试对真实 resolver 的 .resolve 调用次数做断言。"""

    def __init__(self, inner):
        self._inner = inner
        self.resolve_calls = 0

    def resolve(self, action_id, selector, observation):
        self.resolve_calls += 1
        return self._inner.resolve(action_id, selector, observation)


def _wire_visual_frame(backend, tmp_path):
    """红底白十字模板(与 adapter 正例同款唯一匹配构造)+ 800x600 灰底
    帧贴 (0,0):返回 (visual 声明 dict, digest 校验通过的 resolver)。
    模板像素逐点复制,scale=1.0;灰底与模板零重合 → 全帧唯一匹配。"""
    from PIL import Image, ImageDraw
    from agent.recording.runtime import build_visual_resolver
    template = Image.new("RGB", (60, 40), (200, 30, 30))
    draw = ImageDraw.Draw(template)
    draw.line([(30, 5), (30, 35)], fill=(255, 255, 255), width=3)
    draw.line([(10, 20), (50, 20)], fill=(255, 255, 255), width=3)
    buf = io.BytesIO()
    template.save(buf, format="PNG")
    template_bytes = buf.getvalue()
    (tmp_path / "apply.png").write_bytes(template_bytes)
    visual = {"template": "apply.png", "redacted": True,
               "elementSha256": "sha256:"
               + hashlib.sha256(template_bytes).hexdigest(),
               "relativePoint": [0.5, 0.5]}
    frame = Image.new("RGB", (800, 600), (90, 90, 90))
    frame.paste(template, (0, 0))
    fbuf = io.BytesIO()
    frame.save(fbuf, format="PNG")
    frame_bytes = fbuf.getvalue()

    def screenshot(_path=None):
        return {"ok": True, "capture_scope": "window",
                "image_b64": base64.b64encode(frame_bytes).decode("ascii"),
                "origin": [0, 0]}

    backend.screenshot = screenshot
    backend.recorder_ui_rectangles = lambda: []
    backend.protected_rectangles = lambda: []

    def click_window_at(x, y, window_title_re=None):
        backend.dispatch_calls.append({
            "method": "click_window_at", "x": int(x), "y": int(y),
            "window_title_re": window_title_re})
        # 视觉点击镜像 adapter 的效果接线(按命中窗口区分,否则 W2 步
        # 的 absent 效果门恒不满足):主窗点击 → Log Center 出现;
        # Log Center 点击 → 它消失。
        win = backend._gui.main_window
        if win is not None and str(win.handle) == "200":
            backend._windows["200"]["visible"] = False
        else:
            backend._windows["200"]["visible"] = True
        return {"ok": True, "method": "mouse.click",
                "window_relative": {"x": int(x), "y": int(y)}}

    backend.click_window_at = click_window_at
    return visual, _CountingResolver(build_visual_resolver(tmp_path))


def _visual_golden_with_semantic_identity(tmp_path, visual):
    """effect golden 的 step-0001 保留语义身份(btnApply),附加视觉
    条件 — 供"语义优先、视觉回退"与"禁视觉派发"对照。"""
    import test_v3_real_server_adapter as adapter  # noqa: E402
    from agent.recording.models import GoldenTrace  # noqa: E402
    golden = adapter._compile_effect_golden()
    first = dataclasses.replace(
        golden.steps[0],
        selector=dataclasses.replace(golden.steps[0].selector,
                                     visual=visual))
    return GoldenTrace.from_dict(dataclasses.replace(
        golden, steps=(first, *golden.steps[1:])).to_dict())


def test_semantic_only_semantic_step_never_touches_visual(
        monkeypatch, tmp_path):
    """semantic_only + 带视觉条件的语义步:语义身份优先,视觉条件被
    忽略(resolver 0 调用),业务派发是 gui.click(A023 0 次)。"""
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
        server_tools=server, window_role_map=adapter._A_B_A_ROLE_MAP,
        replay_mode="semantic_only")
    assert result["latched"] is False, result["steps"]
    assert result["dispatchInputs"] == 3, result["steps"]
    methods = [c["method"] for c in backend.dispatch_calls]
    assert methods.count("click_window_at") == 0, methods
    assert methods.count("click") == 3, methods


def test_semantic_only_refuses_when_step_carries_visual_condition(
        monkeypatch, tmp_path):
    """semantic_only + 语义身份步带视觉条件 + resolver 注入:仍禁视觉
    派发 — resolver 0 调用(门在候选匹配之前关闭),派发纯语义。"""
    import target.server as server  # noqa: E402
    import test_v3_real_server_adapter as adapter  # noqa: E402
    from agent.recording.execution_ledger import (
        execute_v3_window_stage_real)  # noqa: E402
    from target.windowing.attempt_ledger import AttemptLedger  # noqa: E402
    backend = adapter._fake_backend_for("a_b_a")
    visual, resolver = _wire_visual_frame(backend, tmp_path)
    golden = adapter._compile_effect_golden()
    golden = _visual_golden_with_semantic_identity(tmp_path, visual)
    adapter._install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server, window_role_map=adapter._A_B_A_ROLE_MAP,
        visual_resolver=resolver, replay_mode="semantic_only")
    assert result["latched"] is False, result["steps"]
    assert resolver.resolve_calls == 0, "禁视觉派发:resolver 不得被调"
    assert result["dispatchInputs"] == 3, result["steps"]
    methods = [c["method"] for c in backend.dispatch_calls]
    assert methods.count("click_window_at") == 0, methods
    assert methods.count("click") == 3, methods


def test_semantic_first_semantic_hit_never_calls_resolver(
        monkeypatch, tmp_path):
    """semantic_first:语义命中 → 视觉回退不触发(resolver 0 调用),
    三步全语义派发。"""
    import target.server as server  # noqa: E402
    import test_v3_real_server_adapter as adapter  # noqa: E402
    from agent.recording.execution_ledger import (
        execute_v3_window_stage_real)  # noqa: E402
    from target.windowing.attempt_ledger import AttemptLedger  # noqa: E402
    backend = adapter._fake_backend_for("a_b_a")
    visual, resolver = _wire_visual_frame(backend, tmp_path)
    golden = adapter._compile_effect_golden()
    golden = _visual_golden_with_semantic_identity(tmp_path, visual)
    adapter._install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server, window_role_map=adapter._A_B_A_ROLE_MAP,
        visual_resolver=resolver, replay_mode="semantic_first")
    assert result["latched"] is False, result["steps"]
    assert resolver.resolve_calls == 0
    assert result["dispatchInputs"] == 3, result["steps"]
    methods = [c["method"] for c in backend.dispatch_calls]
    assert methods.count("click_window_at") == 0, methods
    assert methods.count("click") == 3, methods


def test_semantic_first_zero_candidates_falls_back_to_visual(
        monkeypatch, tmp_path):
    """semantic_first 回退:声明控件在 fresh observation 中 0 候选
    → 回退视觉解析(resolver 恰 1 次),派发 pointer.click_window
    (窗口相对坐标 = 模板中心 30,20);语义 gui.click 0 次。"""
    import target.server as server  # noqa: E402
    import test_v3_real_server_adapter as adapter  # noqa: E402
    from agent.recording.execution_ledger import (
        execute_v3_window_stage_real)  # noqa: E402
    from target.windowing.attempt_ledger import AttemptLedger  # noqa: E402
    golden = adapter._compile_effect_golden()
    backend = adapter._fake_backend_for("a_b_a")
    visual, resolver = _wire_visual_frame(backend, tmp_path)
    golden = _visual_golden_with_semantic_identity(tmp_path, visual)
    # 0 语义候选:只移除 step-0001 声明的 btnApply(btnBack 保留给
    # step-0003 的语义派发 — 只让目标步落入回退,不扰动其余步)。
    backend._windows["100"]["extra_controls"] = [
        c for c in backend._windows["100"]["extra_controls"]
        if c.get("automation_id") != "btnApply"]
    adapter._install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server, window_role_map=adapter._A_B_A_ROLE_MAP,
        visual_resolver=resolver, replay_mode="semantic_first")
    assert result["latched"] is False, result["steps"]
    assert [e["outcome"] for e in result["steps"]] == \
        ["pass", "pass", "pass"], result["steps"]
    assert resolver.resolve_calls == 1
    assert result["dispatchInputs"] == 3, result["steps"]
    click_calls = [c for c in backend.dispatch_calls
                   if c["method"] == "click_window_at"]
    assert len(click_calls) == 1, backend.dispatch_calls
    assert click_calls[0]["x"] == 30 and click_calls[0]["y"] == 20
    assert [c["method"] for c in backend.dispatch_calls
            if c["method"] == "click"] == ["click", "click"]


def test_visual_only_pass_all_steps_via_visual(
        monkeypatch, tmp_path):
    """visual_only 正例:全部动作步经视觉定位 — resolver 恰 3 次
    (每动作步一次),0 语义 gui.click,3 次 A023。"""
    import target.server as server  # noqa: E402
    import test_v3_real_server_adapter as adapter  # noqa: E402
    from agent.recording.execution_ledger import (
        execute_v3_window_stage_real)  # noqa: E402
    from target.windowing.attempt_ledger import AttemptLedger  # noqa: E402
    golden = adapter._compile_effect_golden()
    backend = adapter._fake_backend_for("a_b_a")
    visual, resolver = _wire_visual_frame(backend, tmp_path)
    steps = []
    for step in golden.steps:
        if step.action_id in ("gui.click", "gui.click_target"):
            step = dataclasses.replace(
                step, selector=dataclasses.replace(
                    step.selector, control={}, visual=visual))
        steps.append(step)
    from agent.recording.models import GoldenTrace  # noqa: E402
    golden = GoldenTrace.from_dict(dataclasses.replace(
        golden, steps=tuple(steps)).to_dict())
    adapter._install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server, window_role_map=adapter._A_B_A_ROLE_MAP,
        visual_resolver=resolver, replay_mode="visual_only")
    assert result["latched"] is False, result["steps"]
    assert [e["outcome"] for e in result["steps"]] == \
        ["pass", "pass", "pass"], result["steps"]
    assert resolver.resolve_calls == 3, (
        f"每个动作步恰一次视觉解析: {resolver.resolve_calls}")
    assert result["dispatchInputs"] == 3, result["steps"]
    methods = [c["method"] for c in backend.dispatch_calls]
    assert methods.count("click_window_at") == 3, methods
    assert "click" not in methods and "click_target" not in methods, methods
    # 坐标全部落模板中心(相对点 [0.5,0.5] × 60x40)。
    for call in backend.dispatch_calls:
        if call["method"] == "click_window_at":
            assert (call["x"], call["y"]) == (30, 20), call


def test_visual_only_refuses_semantic_step_without_visual(
        monkeypatch, tmp_path):
    """visual_only + 纯语义步:GUI 前拒绝 visual_only_unsupported
    (禁止语义派发);0 派发、0 resolver 调用;ledger 拒绝码可诊断。"""
    import target.server as server  # noqa: E402
    import test_v3_real_server_adapter as adapter  # noqa: E402
    from agent.recording.execution_ledger import (
        execute_v3_window_stage_real)  # noqa: E402
    from target.windowing.attempt_ledger import AttemptLedger  # noqa: E402
    backend = adapter._fake_backend_for("a_b_a")
    visual, resolver = _wire_visual_frame(backend, tmp_path)
    golden = adapter._compile_effect_golden()
    adapter._install_fake_os(monkeypatch, backend)
    result = execute_v3_window_stage_real(
        golden=golden, ledger=AttemptLedger(tmp_path / "h"),
        server_tools=server, window_role_map=adapter._A_B_A_ROLE_MAP,
        visual_resolver=resolver, replay_mode="visual_only")
    assert result["latched"] is True
    assert resolver.resolve_calls == 0
    assert result["dispatchInputs"] == 0, result["steps"]
    entry = result["steps"][0]
    assert entry.get("code") == "visual_only_unsupported", entry
    events = AttemptLedger(tmp_path / "h").rebuild_history(
        run_id=result["runId"])
    assert events[0]["stateAfterRef"].get("rejectionCode") == \
        "visual_only_unsupported", events[0]["stateAfterRef"]
    assert len(backend.dispatch_calls) == 0
