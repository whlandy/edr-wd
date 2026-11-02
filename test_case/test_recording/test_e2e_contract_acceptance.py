"""端到端契约验收测试：验证录制→编译→golden→回放主链路能否完全闭环。

专注于「功能能否完全实现」的脱机可验证层面，不重复既有录制测试的分段细节
（那部分由 `test_case/test_recording/` 的大量单元测试覆盖），而是验证三段
跨层契约：

1. 真实录制数据的可复现编译：对 `recordings/<case>/recording.json` 重新离线编译，
   断言得到的 golden 状态与仓库已存的 `golden-trace.json.status` 完全一致（编译是
   确定性的，而不是随执行环境的偶然产物）。
2. 产物结构完整性：编译产出的 golden-trace 能被反序列化回模型、能转成 `TestCase`、
   且每个 step 的 selector 都通过模型严格校验（不泄漏临时标识、视觉模板带摘要）。
3. 安全门槛：`incomplete` 的 golden 绝不能被回放（抛 `golden_trace_incomplete`）；
   `ready` 的 golden 在 catalog/profile 绑定不匹配时也被拒绝，而非半跑。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.recording.compiler import compile_recording
from agent.locator import LocatorRequest
from agent.recording.models import GoldenTrace, ReplaySelector, canonical_json
from agent.recording.replay import golden_to_test_case
from target.protocol_models import TestCase
from target.recording.models import RawRecording, RecordingModelError

# 仓库根：test_case/test_recording/ -> 上两级的仓库根
REPO = Path(__file__).resolve().parents[2]
RECORDINGS = REPO / "recordings"

pytestmark = pytest.mark.unit


def _real_cases() -> list[tuple[str, Path]]:
    """收集既存的 (case 名, recording.json 路径)。"""
    if not RECORDINGS.is_dir():
        return []
    cases = []
    for recording in sorted(RECORDINGS.rglob("recording.json")):
        cases.append((recording.parent.name, recording))
    return cases


REAL_CASES = _real_cases()


def _stored_golden(case_dir: Path):
    golden_path = case_dir / "golden-trace.json"
    if not golden_path.is_file():
        return None
    return json.loads(golden_path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 1. 真实录制数据：可复现编译 + 状态与仓库产物一致
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("case_name,recording", REAL_CASES, ids=[c for c, _ in REAL_CASES])
def test_real_recording_recompiles_matching_stored_golden_status(case_name, recording):
    """对真实 recording.json 离线重编译，golden 状态必须与仓库已存产物一致。

    状态一致说明：编译链是确定性的、当前实现复现了当初产生该 golden 的语义，
    而不是因为实现漂移产生了不同（甚至更宽松/更严格）的结果。
    """
    stored = _stored_golden(recording.parent)
    assert stored is not None, (
        f"{case_name} 有 recording.json 但仓库没有 golden-trace.json，"
        "无法做状态对照"
    )

    raw = RawRecording.from_dict(json.loads(recording.read_text(encoding="utf-8")))
    result = compile_recording(raw)

    assert result.golden.status == stored["status"], (
        f"{case_name}: 重新编译得到 {result.golden.status!r}，"
        f"但仓库 golden 是 {stored['status']!r}"
    )


def test_every_stored_recording_has_a_golden_and_vice_versa():
    """recording.json 与 golden-trace.json 应成对存在（两者都是编译产物）。"""
    recordings = set()
    goldens = set()
    if not RECORDINGS.is_dir():
        pytest.skip("无 recordings/ 目录")
    for rec in RECORDINGS.rglob("recording.json"):
        recordings.add(rec.parent)
    for g in RECORDINGS.rglob("golden-trace.json"):
        goldens.add(g.parent)
    missing_golden = recordings - goldens
    missing_raw = goldens - recordings
    assert not missing_golden, f"有 recording 但没有 golden: {sorted(p.name for p in missing_golden)}"
    assert not missing_raw, f"有 golden 但没有 recording: {sorted(p.name for p in missing_raw)}"


# --------------------------------------------------------------------------- #
# 2. 产物结构完整性：反序列化、转 TestCase、selector 模型校验
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("case_name,recording", REAL_CASES, ids=[c for c, _ in REAL_CASES])
def test_recompiled_golden_roundtrips_and_materializes(case_name, recording):
    """编译产出的 golden：反序列化、转 TestCase、selector 全部通过模型校验。"""
    raw = RawRecording.from_dict(json.loads(recording.read_text(encoding="utf-8")))
    result = compile_recording(raw)
    golden = result.golden

    # golden -> JSON canonically -> back，不能丢字段/不能崩
    reparsed = GoldenTrace.from_dict(json.loads(canonical_json(golden.to_dict())))
    assert reparsed.name == golden.name
    assert reparsed.status == golden.status
    assert len(reparsed.steps) == len(golden.steps)

    # ready 的 golden 必须能完整转成原子 TestCase；incomplete 也应能转（门槛在回放时拦）
    if golden.status == "ready":
        case: TestCase = golden_to_test_case(golden)
        assert case.steps, f"{case_name}: ready golden 转换出 0 个 step"
        # 每个 step 都应有可回放的选择器（编译成 ready 的必要条件）
        for step in golden.steps:
            assert step.selector is not None, f"{case_name}: ready step {step.step_id} 无 selector"


def test_stored_golden_trace_files_load_and_match_model_schema():
    """仓库里已存的 golden-trace.json 也必须能反序列化（防止 schema 漂移破坏旧产物）。"""
    if not RECORDINGS.is_dir():
        pytest.skip("无 recordings/ 目录")
    for golden_path in RECORDINGS.rglob("golden-trace.json"):
        data = json.loads(golden_path.read_text(encoding="utf-8"))
        golden = GoldenTrace.from_dict(data)
        assert golden.name  # schema v1 必须存 name
        assert golden.status in {"ready", "incomplete"}
        # ready 的每个 step 都必须带 selector 且 status==ready（门槛前提）
        if golden.status == "ready":
            for step in golden.steps:
                assert step.status == "ready", f"{golden_path}: ready 但 step {step.step_id} 不是 ready"
                assert step.selector is not None


# --------------------------------------------------------------------------- #
# 3. 安全门槛：incomplete 不可回放、catalog/profile 绑定不匹配即拒绝
# --------------------------------------------------------------------------- #
def test_incomplete_golden_cannot_replay_raises_gate():
    """incomplete 的 golden 必须被回放门槛拦截，绝不半跑（无需真实数据，自包含）。"""
    from agent.recording.models import RecordedStep
    from agent.recording.replay import _run_golden_trace

    selector = ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"},
        control={"automationId": "btnApply", "controlType": "Button", "name": "应用"},
    )
    step = RecordedStep("step-0001", "gui.click", {}, selector)
    golden = GoldenTrace(
        name="incomplete-demo",
        source_recording={"schema": "edr.desktop-recording/v1", "sha256": "sha256:raw"},
        catalog={"version": "1.0.0", "digest": "sha256:catalog"},
        environment={"backend": "windows_pywinauto", "target": "win-dev", "profile": "windows_hisec"},
        steps=(step,),
        status="incomplete",
        cleanup=(),
    )

    runtime = _stub_runtime()
    with pytest.raises(RecordingModelError) as exc_info:
        _run_golden_trace(runtime, golden)
    assert exc_info.value.code == "golden_trace_incomplete"


def test_ready_golden_catalog_mismatch_is_rejected_before_playback():
    """ready 的 golden 若 catalog 绑定与运行时不一致，必须拒绝，而非用错规则半跑。"""
    from agent.recording.models import RecordedStep
    from agent.recording.replay import _run_golden_trace

    selector = ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"},
        control={"automationId": "btnApply", "controlType": "Button"},
    )
    golden = GoldenTrace(
        name="catalog-mismatch",
        source_recording={"schema": "edr.desktop-recording/v1", "sha256": "sha256:raw"},
        catalog={"version": "9.9.9", "digest": "sha256:different"},
        environment={"backend": "windows_pywinauto", "target": "win-dev", "profile": "windows_hisec"},
        steps=(RecordedStep("step-0001", "gui.click", {}, selector),),
        status="ready",
        cleanup=(),
    )
    with pytest.raises(RecordingModelError) as exc_info:
        _run_golden_trace(_stub_runtime(), golden)
    assert exc_info.value.code == "golden_catalog_mismatch"


def test_ready_golden_profile_mismatch_is_rejected_before_playback():
    """ready 的 golden 若 profile 与执行上下文不一致，必须拒绝。"""
    from agent.recording.models import RecordedStep
    from agent.recording.replay import _run_golden_trace

    selector = ReplaySelector(
        window={"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"},
        control={"automationId": "btnApply", "controlType": "Button"},
    )
    golden = GoldenTrace(
        name="profile-mismatch",
        source_recording={"schema": "edr.desktop-recording/v1", "sha256": "sha256:raw"},
        catalog={"version": "1.0.0", "digest": "sha256:catalog"},
        environment={"backend": "windows_pywinauto", "target": "win-dev", "profile": "macos_hisec"},
        steps=(RecordedStep("step-0001", "gui.click", {}, selector),),
        status="ready",
        cleanup=(),
    )
    with pytest.raises(RecordingModelError) as exc_info:
        _run_golden_trace(_stub_runtime(), golden)
    assert exc_info.value.code == "golden_profile_mismatch"


def _stub_runtime():
    """构造一个只够触发门槛检查的哑回放运行时（不真正执行任何动作）。"""
    from types import SimpleNamespace
    executor = SimpleNamespace()
    return SimpleNamespace(
        executor=executor,
        catalog_version="1.0.0",
        catalog_digest="sha256:catalog",
        execution_context=SimpleNamespace(profile="windows_hisec"),
        persist_replay_screenshots=False,
        trace_store=None,
    )


# --------------------------------------------------------------------------- #
# 4. 回放作用域契约：ready golden 必须能被 golden_entry_scope 解析出作用域
#    （回归：编译器首选产出 rootAutomationId-only 的 window selector，而
#    golden_entry_scope 只认 titleRegex——二者不一致会让 ready golden 无法回放）
# --------------------------------------------------------------------------- #
def test_ready_golden_with_root_automation_id_selector_is_playable():
    """window selector 为 rootAutomationId（编译器首选形式）的 ready golden，
    golden_entry_scope 应能解析出回放作用域，否则这条 ready golden 无法实际回放。"""
    from agent.recording.models import RecordedStep
    from agent.recording.runtime import golden_entry_scope

    selector = ReplaySelector(
        # 编译器合成的首选窗口身份：root 控件 id，无 titleRegex
        window={"processName": "EDRClient.exe", "rootAutomationId": "EdrMainWindow"},
        control={"automationId": "EdrMainWindow.baseWidget.pageWidget.scanPage", "controlType": "Group"},
    )
    golden = GoldenTrace(
        name="root-automation-id-golden",
        source_recording={"schema": "edr.desktop-recording/v1", "sha256": "sha256:raw"},
        catalog={"version": "1.0.0", "digest": "sha256:catalog"},
        environment={"backend": "windows_pywinauto", "target": "win-dev", "profile": "windows_hisec", "application": "EDRClient.exe"},
        steps=(RecordedStep("step-0001", "gui.click", {}, selector),),
        status="ready",
        cleanup=(),
    )

    scope = golden_entry_scope(golden)
    assert scope.process_name == "EDRClient.exe"


# --------------------------------------------------------------------------- #
# 5. Windows 回放观测 ancestry 契约
#    （缺陷登记：录制侧对无强 ID 控件生成 6 层 ancestry fallback selector，
#    而 Windows 回放观测（pywinauto_client 建树）补上同序同字段 ancestry 前，
#    候选全部因 len(actual)<len(expected) 被剔除 → candidate_count=0）
# --------------------------------------------------------------------------- #
def _ancestry_selector_controls():
    """6 个同一 Table 下、祖先链相同的 Details DataItem（复现 11.26 实测）。"""
    anc = [
        {"controlType": "Table"},
        {"controlType": "Group", "automationId": ("EdrMainWindow.baseWidget.pageWidget."
            "scanPage.VirusScanWindow.scanDetailWidget.tablePage")},
        {"controlType": "Custom", "automationId": ("EdrMainWindow.baseWidget.pageWidget."
            "scanPage.VirusScanWindow.scanDetailWidget")},
        {"controlType": "Group", "automationId": ("EdrMainWindow.baseWidget.pageWidget."
            "scanPage.VirusScanWindow")},
        {"controlType": "Group", "automationId": ("EdrMainWindow.baseWidget.pageWidget.scanPage")},
        {"controlType": "Custom", "automationId": "EdrMainWindow.baseWidget.pageWidget"},
    ]
    return [
        {"target_id": f"T{i}", "process_name": "EDRClient.exe", "window_title": "logo1",
         "text": "Details", "control_type": "DataItem",
         "rect": [1056, 403 + i * 40, 1118, 443 + i * 40],
         "ancestry": list(anc)}  # 6 行祖先链相同
        for i in range(6)
    ]


def _ancestry_request():
    # 与录制侧由此无强 ID 控件生成的 selector 等价的 control selector（含 6 层 ancestry）
    return LocatorRequest(
        "gui.click",
        {"processName": "EDRClient.exe"},
        {"controlType": "DataItem", "name": "Details",
         "ancestry": _ancestry_selector_controls()[0]["ancestry"]},
    )


def test_no_ancestry_observation_fails_not_found():
    """观测控件没有 ancestry 时，带 6 层 ancestry 的 selector 必然 not_found
    （固化缺陷：Windows 观测补 ancestry 前 candidate_count=0）。"""
    from agent.locator import HybridLocator, LocatorError, LocatorPolicy

    controls = _ancestry_selector_controls()
    for c in controls:
        c = dict(c)  # noqa: 保持原样
        c["ancestry"] = []  # 无 ancestry 的观测
    bundle = {"observation_id": "OBS", "window_identity": {}, "active_window": {},
              "controls": [{**c, "ancestry": []} for c in controls]}
    with pytest.raises(LocatorError) as e:
        HybridLocator().resolve(_ancestry_request(), bundle, LocatorPolicy(mode="semantic_only"))
    assert e.value.code == "replay_target_not_found"


def test_same_ancestry_same_name_rows_are_ambiguous_not_silent():
    """同一祖先链的多个同名控件：必须明确歧义(locator_ambiguous)，绝不静默错点。"""
    from agent.locator import HybridLocator, LocatorError, LocatorPolicy

    bundle = {"observation_id": "OBS", "window_identity": {}, "active_window": {},
              "controls": _ancestry_selector_controls()}
    with pytest.raises(LocatorError) as e:
        HybridLocator().resolve(_ancestry_request(), bundle, LocatorPolicy(mode="semantic_only"))
    assert e.value.code == "locator_ambiguous"
    assert e.value.candidate_count == 6
