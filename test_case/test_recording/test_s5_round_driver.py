"""2112 缺口4:S5 单轮驱动实质化(真实链,非占位)。

- --local-fixture 六阶段全部真实运行:raw v2 → 真实编译器(golden v3 +
  windowSequence)→ §7.6 attempt 账本落盘 → effect gate → 可追溯报告;
- 产物落盘:recording/raw.json、golden/golden.v3.json、
  compile-report.json、replay/events/EVT-*.json、report.json;
- fixture 截图缺失 → evidence_missing(不生成假图);
- 账本无凭据泄漏;
- --live 无授权 → 结构化拒绝(exit 3),不伪造实机结果;
- 生产 capability 诚实性注记(未实现 = fail closed)。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]


def _load_driver():
    sys.path.insert(0, str(REPO))
    spec = importlib.util.spec_from_file_location(
        "s5_acceptance_round", REPO / "scripts" / "s5_acceptance_round.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_local_fixture_round_runs_all_stages(tmp_path, monkeypatch):
    """--local-fixture:六阶段全 ran,产物真实落盘。"""
    monkeypatch.setattr(sys, "argv", ["s5", "--round", "1",
                                      "--local-fixture"])
    module = _load_driver()
    module.EVIDENCE_ROOT = tmp_path
    exit_code = module.main()
    assert exit_code == 0
    round_dir = tmp_path / "round-1"
    raw = json.loads((round_dir / "recording" / "raw.json").read_text())
    assert raw["schema"] == "edr.desktop-recording/v2"
    golden = json.loads((round_dir / "golden" / "golden.v3.json").read_text())
    assert golden["schema"] == "edr.desktop-golden-trace/v3"
    assert golden["status"] == "ready"
    ws = golden["windowSequence"]
    # 真实捕获 A→B→A 流(含 closed 转换)→ 编译三步 W1→W2→W1。
    assert [s["windowId"] for s in ws["steps"]] == ["W1", "W2", "W1"]
    events = list((round_dir / "replay" / "events").glob("EVT-*.json"))
    assert events, "§7.6 账本必须真实落盘"
    report = json.loads((round_dir / "report.json").read_text())
    assert report["outcome"] == "pass"
    summary = json.loads((round_dir / "summary.json").read_text())
    assert all(v.get("status") == "ran"
               for v in summary["stages"].values())


def test_fixture_round_artifacts_labeled_not_windows(tmp_path, monkeypatch):
    """fixture 轮所有产物明确标注 local-fixture,不虚称 Windows 截图。"""
    monkeypatch.setattr(sys, "argv", ["s5", "--round", "2",
                                      "--local-fixture"])
    module = _load_driver()
    module.EVIDENCE_ROOT = tmp_path
    module.main()
    round_dir = tmp_path / "round-2"
    summary = json.loads((round_dir / "summary.json").read_text())
    assert summary["mode"] == "local-fixture"
    # 结构化来源验证(1536 第二红):标签文案可扩充,但必须包含
    # local-fixture 标识 + 明确非 Windows 声明(不放宽真假来源)。
    source_label = summary["stages"]["recording"]["source"]
    assert "local-fixture" in source_label, source_label
    assert "not a Windows capture" in source_label, source_label
    assert "local-fixture" in summary["stages"]["replay"]["source"]
    report = json.loads((round_dir / "report.json").read_text())
    blob = json.dumps(report)
    assert "is_windows_capture" not in blob or report["failedStep"] is None


def test_fixture_round_ledger_has_no_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["s5", "--round", "3",
                                      "--local-fixture"])
    module = _load_driver()
    module.EVIDENCE_ROOT = tmp_path
    module.main()
    for path in (tmp_path / "round-3" / "replay" / "events").glob("*.json"):
        blob = path.read_text().lower()
        for forbidden in ("run_token", "binding_token", '"handle"',
                          '"pid"', '"native_id"'):
            assert forbidden not in blob, (path.name, forbidden)


def test_live_mode_refused_without_authorization(tmp_path, monkeypatch,
                                                 capsys):
    """--live 无授权 → 结构化拒绝,exit 3;不生成伪实机产物。"""
    monkeypatch.setattr(sys, "argv", ["s5", "--round", "4", "--live"])
    module = _load_driver()
    module.EVIDENCE_ROOT = tmp_path
    exit_code = module.main()
    assert exit_code == 3
    captured = capsys.readouterr().out
    payload = json.loads(captured.strip().splitlines()[-1])
    assert payload["status"] == "refused"
    assert "instance tracking" in payload["reason"] or \
        "authorization" in payload["reason"]
    # 拒绝路径不写 round 产物(无伪结果)。
    assert not (tmp_path / "round-4" / "summary.json").exists()


def test_capability_note_records_fail_closed_honesty(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["s5", "--round", "5",
                                      "--local-fixture"])
    module = _load_driver()
    module.EVIDENCE_ROOT = tmp_path
    module.main()
    summary = json.loads((tmp_path / "round-5" / "summary.json").read_text())
    note = summary["capability"]
    assert "fail closed" in note["note"]
    assert "generation" in note["note"] or "events" in note["note"]


def test_fixture_round_recording_failure_exits_nonzero(tmp_path,
                                                       monkeypatch):
    """故障注入(recording 阶段失败)→ 非零退出;后续阶段未运行。"""
    monkeypatch.setattr(sys, "argv", ["s5", "--round", "10",
                                      "--local-fixture"])
    module = _load_driver()
    module.EVIDENCE_ROOT = tmp_path

    import target.recording.service as service_mod
    original_start = service_mod.RecordingService.start

    def failing_start(self, **kwargs):
        result = original_start(self, **kwargs)
        if result.get("ok"):
            # 模拟录制数据丢失(受控注入):pump 不送达任何事件,使 driver
            # 的单一 stop 产出空 raw → compile 无法 ready。不再用再入式
            # self.stop()——driver 现自配 durable store,重复 stop 会触发
            # recording_result_exists 持久冲突,把"空录制"误判成存储失败。
            session = self.manager.current()
            if session is not None:
                source = getattr(session, "_source", None)
                if source is not None:
                    source.pump = lambda max_packets=None: None
                session._events.clear()
        return result

    monkeypatch.setattr(service_mod.RecordingService, "start",
                        failing_start)
    exit_code = module.main()
    assert exit_code != 0, "收集失败必须非零退出(fail-closed)"
    summary = json.loads((tmp_path / "round-10" / "summary.json")
                         .read_text())
    # 前失败阻断:compile 未达 ready → 无 replay/report 阶段。
    assert summary["stages"].get("replay") is None, summary["stages"]
    assert summary["stages"]["compile"]["goldenStatus"] == "incomplete"
    assert summary.get("failure"), summary


def _rebuild_history(module, tmp_path, run_id):
    from target.windowing.attempt_ledger import AttemptLedger
    return AttemptLedger(
        tmp_path / "round-1" / "replay" / "events").rebuild_history(
        run_id=run_id)


def test_business_assertion_flag_round_passes(tmp_path, monkeypatch):
    """--business-assertion 正例:6 事件(末为 assertion)、compile ready、
    三步全 pass——业务断言 control_checked_equals 与两条窗口断言
    分开并存,均由真实 evaluator 动作后观测求值。"""
    monkeypatch.setattr(sys, "argv", ["s5", "--round", "1",
                                      "--local-fixture",
                                      "--business-assertion"])
    module = _load_driver()
    module.EVIDENCE_ROOT = tmp_path
    exit_code = module.main()
    assert exit_code == 0
    raw = json.loads((tmp_path / "round-1" / "recording" / "raw.json")
                     .read_text())
    events = raw["events"]
    assert len(events) == 6
    assert events[-1]["type"] == "assertion"
    # 时间线单调非递减(fixture 时间推进后管理断言不得早于末事件)。
    monos = [e["monotonicMs"] for e in events]
    assert all(a <= b for a, b in zip(monos, monos[1:])), monos
    golden = json.loads((tmp_path / "round-1" / "golden" / "golden.v3.json")
                        .read_text())
    assert golden["status"] == "ready"
    assert [s["windowId"] for s in golden["windowSequence"]["steps"]] == \
        ["W1", "W2", "W1"]
    report = json.loads((tmp_path / "round-1" / "report.json").read_text())
    assert report["outcome"] == "pass"
    # 断言归属:窗口断言两条 + 业务断言一条(control_checked_equals)。
    types = [f["type"] for step in report["steps"]
             for f in (step.get("features") or [])
             if f.get("assertionId")]
    assert types.count("window_open") == 1
    assert types.count("window_closed") == 1
    checked = [f for f in types if f == "control_checked_equals"]
    assert checked, types


def test_business_assertion_real_fact_mismatch_fails(tmp_path, monkeypatch):
    """负例:声明 checked expected=True,仅改 **OS 控件事实**(回放时
    btnApply checked=False)→ 真实求值 fail、report fail、非零退出——
    求值响应当前控件事实,不是固定返回成功。"""
    monkeypatch.setattr(sys, "argv", ["s5", "--round", "1",
                                      "--local-fixture",
                                      "--business-assertion"])
    module = _load_driver()
    module.EVIDENCE_ROOT = tmp_path
    original_round = module.run_local_fixture_round

    def fact_flip_round(round_no, round_dir, **kwargs):
        kwargs["checked_fact"] = False
        return original_round(round_no, round_dir, **kwargs)

    monkeypatch.setattr(module, "run_local_fixture_round", fact_flip_round)
    exit_code = module.main()
    assert exit_code != 0, "事实与期望不符必须真实 fail(fail-closed)"
    summary = json.loads((tmp_path / "round-1" / "summary.json").read_text())
    report = json.loads((tmp_path / "round-1" / "report.json").read_text())
    assert report["outcome"] == "fail"
    assert summary["stages"]["traceable-report"]["failedStep"] is not None \
        or report.get("failedStep"), summary
    checked_results = [
        (f.get("result"), f.get("actual"))
        for step in report["steps"]
        for f in (step.get("features") or [])
        if f.get("type") == "control_checked_equals"]
    assert checked_results and all(r == "fail" for r, _ in checked_results), \
        checked_results
    # actual 必须是真实观测到的控件事实(False)——求值响应当前事实,
    # mismatch 由期望(True)与事实(False)相悖产生,不是固定返回成功。
    assert any(a is False for _, a in checked_results), checked_results


def test_scroll_assertion_round_passes(tmp_path, monkeypatch):
    """--scroll-assertion 正例:真实 wheel 包经 correlator 合成单个
    scroll_commit,bindPrevious 结果 verifier 折入该步;回放真实分派
    pointer.scroll,text_contains 由真实 evaluator 求值 pass。"""
    monkeypatch.setattr(sys, "argv", ["s5", "--round", "1",
                                      "--local-fixture",
                                      "--scroll-assertion"])
    module = _load_driver()
    module.EVIDENCE_ROOT = tmp_path
    exit_code = module.main()
    assert exit_code == 0
    raw = json.loads((tmp_path / "round-1" / "recording" / "raw.json")
                     .read_text())
    scrolls = [e for e in raw["events"] if e["type"] == "scroll_commit"]
    assert len(scrolls) == 1, [e["type"] for e in raw["events"]]
    assert scrolls[0]["input"]["delta"] == 240
    monos = [e["monotonicMs"] for e in raw["events"]]
    assert all(a <= b for a, b in zip(monos, monos[1:])), monos
    golden = json.loads((tmp_path / "round-1" / "golden" / "golden.v3.json")
                        .read_text())
    assert golden["status"] == "ready"
    report = json.loads((tmp_path / "round-1" / "report.json").read_text())
    assert report["outcome"] == "pass"
    summary = json.loads((tmp_path / "round-1" / "summary.json").read_text())
    assert summary["stages"]["replay"]["dispatchInputs"] == 4
    types = [f["type"] for step in report["steps"]
             for f in (step.get("features") or [])
             if f.get("assertionId")]
    assert types.count("control_text_contains") == 1, types
    assert types.count("window_open") == 1 and types.count("window_closed") == 1


def test_scroll_verifier_required_fails_closed(tmp_path, monkeypatch):
    """负例:scroll 手势无绑定结果 verifier → compile_scroll_verifier_
    required、incomplete、非零退出、无 replay。"""
    monkeypatch.setattr(sys, "argv", ["s5", "--round", "1",
                                      "--local-fixture",
                                      "--scroll-assertion"])
    module = _load_driver()
    module.EVIDENCE_ROOT = tmp_path
    import target.recording.session as session_mod
    original_add = session_mod.RecordingSession.add_assertion

    def drop_scroll_assertion(self, payload):
        if payload.get("requestId", "").startswith("fixture-scroll-"):
            return {"ok": True, "requestId": payload.get("requestId"),
                    "suppressed": True}
        return original_add(self, payload)

    monkeypatch.setattr(session_mod.RecordingSession, "add_assertion",
                        drop_scroll_assertion)
    exit_code = module.main()
    assert exit_code != 0
    summary = json.loads((tmp_path / "round-1" / "summary.json").read_text())
    assert summary["stages"].get("replay") is None
    assert summary["stages"]["compile"]["goldenStatus"] == "incomplete"
    assert "compile_scroll_verifier_required" in \
        summary["stages"]["compile"]["issueCodes"]


def test_scroll_no_change_fails_closed(tmp_path, monkeypatch):
    """负例(205 缺口1):OS 接收滚动但**事实未变化**(scroll_fact
    参数传回原文本 — OS 滚动后事实未变)→ text_contains 期望(滚动
    后内容)对未变事实求值 fail → 非零退出(不是 receipt ok 即成功)。"""
    monkeypatch.setattr(sys, "argv", ["s5", "--round", "12",
                                      "--local-fixture",
                                      "--scroll-assertion"])
    module = _load_driver()
    module.EVIDENCE_ROOT = tmp_path
    original_round = module.run_local_fixture_round

    def frozen_scroll_round(round_no, round_dir, **kwargs):
        kwargs["scroll_fact"] = "Apply"
        return original_round(round_no, round_dir, **kwargs)

    monkeypatch.setattr(module, "run_local_fixture_round",
                        frozen_scroll_round)
    exit_code = module.main()
    assert exit_code != 0, "滚动未变化必须 fail(fail-closed)"
    report = json.loads((tmp_path / "round-12" / "report.json")
                        .read_text())
    results = [(f.get("type"), f.get("result"))
               for step in report["steps"]
               for f in (step.get("features") or [])
               if f.get("type") == "control_text_contains"]
    assert results and all(r == "fail" for _, r in results), results



def test_drag_assertion_round_passes(tmp_path, monkeypatch):
    """--drag-assertion 正例:真实 pointer_down/up 经 correlator 合成
    drag_commit(start/end 双端控件解析),bindPrevious 结果 verifier
    折入 drag 步;回放真实分派 drag,drag 事实(text)经 fake OS 更新,
    text_contains 由真实 evaluator 求值 pass。"""
    monkeypatch.setattr(sys, "argv", ["s5", "--round", "1",
                                      "--local-fixture",
                                      "--drag-assertion"])
    module = _load_driver()
    module.EVIDENCE_ROOT = tmp_path
    exit_code = module.main()
    assert exit_code == 0
    raw = json.loads((tmp_path / "round-1" / "recording" / "raw.json")
                     .read_text())
    drags = [e for e in raw["events"] if e["type"] == "drag_commit"]
    assert len(drags) == 1, [e["type"] for e in raw["events"]]
    golden = json.loads((tmp_path / "round-1" / "golden" / "golden.v3.json")
                        .read_text())
    assert golden["status"] == "ready"
    report = json.loads((tmp_path / "round-1" / "report.json").read_text())
    assert report["outcome"] == "pass"
    types = [f["type"] for step in report["steps"]
             for f in (step.get("features") or [])
             if f.get("assertionId")]
    assert types.count("control_text_contains") == 1, types


def test_drag_no_effect_fails_closed(tmp_path, monkeypatch):
    """负例(与 scroll 未变化负例同构):OS 接收 drag 但**事实未变化**
    (drag_fact 传回原文本 — OS 拖拽后事实未变)→ text_contains 期望
    (拖拽后内容)对未变事实求值 fail → 非零退出(dispatch ok ≠ 成功)。"""
    monkeypatch.setattr(sys, "argv", ["s5", "--round", "13",
                                      "--local-fixture",
                                      "--drag-assertion"])
    module = _load_driver()
    module.EVIDENCE_ROOT = tmp_path
    original_round = module.run_local_fixture_round

    def unchanged_drag_round(round_no, round_dir, **kwargs):
        kwargs["drag_fact"] = "drag-original"
        return original_round(round_no, round_dir, **kwargs)

    monkeypatch.setattr(module, "run_local_fixture_round",
                        unchanged_drag_round)
    exit_code = module.main()
    assert exit_code != 0, "drag 未变化必须 fail(fail-closed)"
    report = json.loads((tmp_path / "round-13" / "report.json")
                        .read_text())
    results = [(f.get("type"), f.get("result"))
               for step in report["steps"]
               for f in (step.get("features") or [])
               if f.get("type") == "control_text_contains"]
    assert results and all(r == "fail" for _, r in results), results


def test_restart_readonly_rebuild_traceable_no_revival(tmp_path,
                                                       monkeypatch):
    """端到端重启追溯:真轮跑完后,**新 AttemptLedger 实例**(模拟进程
    重启)只读重建历史 → 报告与原报告逐字段一致(outcome/断言特征/
    dispatchInputs)、append-only 文件集不变、**不自动恢复**——重建只
    读盘,不重试动作、不复活 binding(无新 dispatch、无新事件)。"""
    monkeypatch.setattr(sys, "argv", ["s5", "--round", "1",
                                      "--local-fixture",
                                      "--drag-assertion"])
    module = _load_driver()
    module.EVIDENCE_ROOT = tmp_path
    assert module.main() == 0
    original_report = json.loads(
        (tmp_path / "round-1" / "report.json").read_text())
    events_dir = tmp_path / "round-1" / "replay" / "events"
    before_files = sorted(p.name for p in events_dir.glob("EVT-*.json"))
    summary = json.loads((tmp_path / "round-1" / "summary.json").read_text())
    run_id = summary["stages"]["replay"]["runId"]

    from target.windowing.attempt_ledger import AttemptLedger
    # 模拟重启:全新实例,只读 rebuild。
    fresh = AttemptLedger(events_dir).rebuild_history(run_id=run_id)
    assert fresh, "重启后必须能从盘上事件只读重建完整历史"
    outcomes = [e["outcome"] for e in fresh]
    assert "unknown" not in outcomes or all(
        e.get("stateAfterRef") for e in fresh if e["outcome"] == "unknown"), \
        outcomes
    # 重建历史重放出与原报告一致的可追溯结论。
    from agent.recording.execution_ledger import build_run_report
    rebuilt = build_run_report(fresh, screenshots={})
    assert rebuilt["outcome"] == original_report["outcome"]
    rebuilt_types = [f["type"] for step in rebuilt["steps"]
                     for f in (step.get("features") or [])
                     if f.get("assertionId")]
    original_types = [f["type"] for step in original_report["steps"]
                      for f in (step.get("features") or [])
                      if f.get("assertionId")]
    assert rebuilt_types == original_types, (rebuilt_types, original_types)
    # 不自动恢复:重建后盘上文件集不变(无新事件写入)。
    after_files = sorted(p.name for p in events_dir.glob("EVT-*.json"))
    assert after_files == before_files, "重建必须只读,不得追加事件"


def test_explicit_causal_binding_folds_into_named_step(tmp_path,
                                                       monkeypatch):
    """bindCausalId 显式绑定:scroll 与 drag 双手势组合下,drag 断言
    (bindCausalId→drag 动作)必须折入 **drag 步**而非最近 scroll 步;
    scroll 断言(bindPrevious→最近动作)折入 scroll 步。两步各自携带
    自己的 verifier,回放两动作都真实分派、两断言都真实求值 pass。"""
    monkeypatch.setattr(sys, "argv", ["s5", "--round", "1",
                                      "--local-fixture",
                                      "--scroll-assertion",
                                      "--drag-assertion"])
    module = _load_driver()
    module.EVIDENCE_ROOT = tmp_path
    assert module.main() == 0
    golden = json.loads((tmp_path / "round-1" / "golden" /
                         "golden.v3.json").read_text())
    assert golden["status"] == "ready"
    # 折叠归属:scroll verifier 在 scroll 步,drag verifier 在 drag 步。
    body_steps = [v for v in (golden.get("steps") or {}).values()
                  if isinstance(v, dict)]
    scroll_steps = [s for s in body_steps
                    if any(v.get("type") == "text_contains" and
                           (v.get("match") or {}).get("automation_id") ==
                           "btnApply"
                           for v in s.get("verifiers") or [])]
    drag_steps = [s for s in body_steps
                  if any(v.get("type") == "text_contains" and
                         (v.get("match") or {}).get("automation_id") ==
                         "dragTarget"
                         for v in s.get("verifiers") or [])]
    assert len(scroll_steps) == 1 and len(drag_steps) == 1, \
        (scroll_steps, drag_steps)
    assert scroll_steps[0]["stepId"] != drag_steps[0]["stepId"], \
        "两个动作的 verifier 不得折入同一步"
    report = json.loads((tmp_path / "round-1" / "report.json").read_text())
    assert report["outcome"] == "pass"
    types = [f["type"] for step in report["steps"]
             for f in (step.get("features") or [])
             if f.get("assertionId")]
    assert types.count("control_text_contains") == 2, types
    assert types.count("window_open") == 1 and \
        types.count("window_closed") == 1, types


def test_three_rounds_fresh_fixture(tmp_path, monkeypatch):
    """--three-rounds:三轮独立 session/run/目录,全绿 exit0;每轮断言
    特征齐备(business+scroll+drag+两窗口断言),轮间 session 互不相同。"""
    monkeypatch.setattr(sys, "argv", ["s5", "--round", "1",
                                      "--local-fixture", "--three-rounds",
                                      "--business-assertion",
                                      "--scroll-assertion",
                                      "--drag-assertion"])
    module = _load_driver()
    module.EVIDENCE_ROOT = tmp_path
    assert module.main() == 0
    sessions = set()
    for n in (1, 2, 3):
        summary = json.loads((tmp_path / f"round-{n}" /
                              "summary.json").read_text())
        assert all(v.get("status") == "ran"
                   for v in summary["stages"].values()), summary
        sessions.add(summary["stages"]["recording"]["sessionId"])
        report = json.loads((tmp_path / f"round-{n}" /
                             "report.json").read_text())
        assert report["outcome"] == "pass"
        types = [f["type"] for step in report["steps"]
                 for f in (step.get("features") or [])
                 if f.get("assertionId")]
        assert types.count("control_checked_equals") == 1, types
        assert types.count("control_text_contains") == 2, types
        assert types.count("window_open") == 1, types
        assert types.count("window_closed") == 1, types
    assert len(sessions) == 3, "三轮必须 fresh session(互不相同)"
