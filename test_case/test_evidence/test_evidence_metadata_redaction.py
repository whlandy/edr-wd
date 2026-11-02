"""证据元数据源端脱敏回归（D16 契约，纯新增，先失败）。

审计（2026-10-30 定时轮，§6q 遗留「redaction.py 对 trace.md 的源端脱敏
执行路径审计」）发现的红测实证：

1. ``persist_screenshot`` 对 text 规则只计算 fired 与
   ``redaction_applied`` 标志，随后**丢弃脱敏结果**、把原始
   ``window_title`` / ``process_name`` 原文写入 EvidenceRecord ——
   与其自身 docstring「Apply text redaction to the metadata strings」
   契约相反。
2. 生产路径唯一调用方 ``ReplayEvidenceRecorder.record`` 从不传入
   ``redaction_rules``，Layer-1 ``RedactionRegistry`` 在录制/回放链
   完全未接线（仅 planner 路径引用，而 planner 事件无生产写入方）。
   含敏感串的窗口标题经 ``SCREENSHOT_PERSISTED`` 事件原文落盘
   ``events.jsonl``，未来 trace.md 渲染器（markdown.py:223-225 从
   EvidenceRecord 取 provenance）也将渲染原文。

D16 契约：持久化事件必须在持久化前经 ``RedactionRegistry.apply_text``；
FR-P3.1-09 验收 #6：trace 目录审计零命中。

本文件从三层锁死：

1. 持久层：``persist_screenshot`` 传入 text 规则时，EvidenceRecord
   必须携带脱敏后的元数据字符串，而非原文。
2. 回放证据链：``ReplayEvidenceRecorder`` 必须接入 Layer-1 默认注册表，
   ``events.jsonl`` 中的 SCREENSHOT_PERSISTED 不得含注册表命中的敏感串。
3. 审计闭环：录制器产出的 trace 目录经 ``audit_artifacts`` 必须零命中
   （ok=True）。

正向对照：不含敏感串的标题逐字保留、redaction_applied 不误报（防修复过宽）。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
for p in (str(_REPO / "target"), str(_REPO / "agent"), str(_REPO / "test_case")):
    if p not in sys.path:
        sys.path.insert(0, p)


# 注意：本仓库 pythonpath 同时含 agent/ 与 ./，agent/trace 会以 `trace` 与
# `agent.trace` 两个模块名各加载一次（类身份不同）。统一走 agent.* 命名空间，
# 与被测生产代码（agent.recording.replay / agent.trace.evidence）一致。
from agent.redaction import RedactionRegistry, default_registry  # noqa: E402
from agent.trace.evidence import persist_screenshot  # noqa: E402
from agent.trace.redaction import TextRule  # noqa: E402
from fake_target import make_png  # noqa: E402


_SECRET_EMAIL = "alice@example.com"
_CLEAN_TITLE = "EDR Home"


# ---------------------------------------------------------------------------
# 持久层：text 规则命中时，EvidenceRecord 必须携带脱敏后的字符串
# ---------------------------------------------------------------------------


def _persist_kwargs(**overrides):
    kwargs = dict(
        step_no=1, step_id="S001",
        snapshot_id="OBS-1", event_id="EVT-1",
        process_name=f"proc-{_SECRET_EMAIL}", pid=1234,
        window_title=f"{_CLEAN_TITLE} - {_SECRET_EMAIL}",
    )
    kwargs.update(overrides)
    return kwargs


def test_persist_screenshot_persists_redacted_metadata_when_text_rules_fire(
    tmp_path: Path,
):
    rules = [TextRule(rule_id="email",
                      pattern=r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")]
    rec = persist_screenshot(make_png(width=8, height=8), "after",
                             trace_dir=tmp_path,
                             redaction_rules=rules, **_persist_kwargs())
    assert _SECRET_EMAIL not in rec.window_title, (
        f"window_title 原文落入 EvidenceRecord（将随 SCREENSHOT_PERSISTED "
        f"落盘 events.jsonl）: {rec.window_title!r}"
    )
    assert _SECRET_EMAIL not in rec.process_name, (
        f"process_name 原文落入 EvidenceRecord: {rec.process_name!r}"
    )
    assert rec.redaction_applied is True
    assert "email" in rec.redaction_rule_ids


def test_persist_screenshot_clean_metadata_untouched(tmp_path: Path):
    rules = [TextRule(rule_id="email",
                      pattern=r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")]
    rec = persist_screenshot(make_png(width=8, height=8), "after",
                             trace_dir=tmp_path,
                             redaction_rules=rules,
                             process_name="EDRClient.exe", pid=1,
                             window_title=_CLEAN_TITLE,
                             step_no=1, step_id="S001",
                             snapshot_id="OBS-1", event_id="EVT-1")
    assert rec.window_title == _CLEAN_TITLE
    assert rec.process_name == "EDRClient.exe"
    assert rec.redaction_applied is False
    assert rec.redaction_rule_ids == ()


# ---------------------------------------------------------------------------
# 回放证据链：录制器接入 Layer-1 注册表，events.jsonl 零敏感串
# ---------------------------------------------------------------------------


def _recorder(tmp_path: Path, registry: RedactionRegistry | None = None):
    from agent.recording.replay import ReplayEvidenceRecorder
    from agent.trace import TraceStore

    store = TraceStore(tmp_path)
    store.open(trace_id="TR-REDACT")
    if registry is None:
        return ReplayEvidenceRecorder(store), store
    return ReplayEvidenceRecorder(store, redaction_registry=registry), store


class _Step:
    step_no = 1
    step_id = "step-0001"


def _observation(title: str) -> dict:
    png = make_png(width=8, height=8)
    return {
        "snapshot_id": "OBS-R-1",
        "screenshot_bytes": png,
        "screenshot_scope": "window",
        "screenshot_redacted": True,
        "screenshot_sha256": "sha256:" + hashlib.sha256(png).hexdigest(),
        "active_window": {"process_name": "EDRClient.exe", "pid": 4242,
                          "title": title},
    }


def _persisted_payloads(store) -> list[dict]:
    events_path = store.events_path
    payloads = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        event = json.loads(line)
        if event.get("event_type") == "screenshot_persisted":
            payloads.append(event["payload"])
    return payloads


def test_recorder_redacts_window_title_in_persisted_event(tmp_path: Path):
    recorder, store = _recorder(tmp_path)
    recorder.record(_Step(), _observation(f"{_CLEAN_TITLE} - {_SECRET_EMAIL}"))
    payloads = _persisted_payloads(store)
    assert payloads, "录制器未落 SCREENSHOT_PERSISTED 事件"
    for payload in payloads:
        assert _SECRET_EMAIL not in json.dumps(payload, ensure_ascii=False), (
            f"敏感串经 SCREENSHOT_PERSISTED 原文落盘 events.jsonl: "
            f"{payload.get('window_title')!r}"
        )


def test_recorder_run_passes_registry_audit(tmp_path: Path):
    recorder, store = _recorder(tmp_path)
    recorder.record(_Step(), _observation(f"{_CLEAN_TITLE} - {_SECRET_EMAIL}"))
    report = default_registry().audit_artifacts(store.root)
    assert report.ok, (
        f"trace 目录审计命中 {report.matches_total} 处 —— D16/FR-P3.1-09 "
        f"验收 #6 失败: {dict(report.matches_per_file)}"
    )


def test_recorder_injectable_registry_honors_custom_rule(tmp_path: Path):
    registry = RedactionRegistry(rules=(
        TextRule(rule_id="corp_secret", pattern=r"SuperSecret\d+"),
    ))
    recorder, store = _recorder(tmp_path, registry=registry)
    recorder.record(_Step(), _observation("EDR - SuperSecret42"))
    payloads = _persisted_payloads(store)
    assert payloads, "录制器未落 SCREENSHOT_PERSISTED 事件"
    for payload in payloads:
        assert "SuperSecret42" not in json.dumps(payload, ensure_ascii=False), (
            f"自定义规则未生效: {payload.get('window_title')!r}"
        )


# ---------------------------------------------------------------------------
# 正向对照：干净标题逐字保留（防修复过宽）
# ---------------------------------------------------------------------------


def test_recorder_clean_title_verbatim(tmp_path: Path):
    recorder, store = _recorder(tmp_path)
    record = recorder.record(_Step(), _observation(_CLEAN_TITLE))
    assert record is not None
    assert record.window_title == _CLEAN_TITLE
    assert record.redaction_applied is False
