"""截图证据零尺寸/空帧拒绝回归（P1 断言假绿，纯新增）。

缺陷（2026-10-30 定时轮发现）：
``persist_screenshot`` 只拒绝空 ``png_bytes`` 与非法签名，接受 IHDR 声明
0×0 的 PNG —— 空截图会被持久化、digest 校验通过、``verify_evidence_on_disk``
返回 True，下游 ``visual_evidence_captured`` 截图断言对"空截图"返回 PASSED，
属于任务定义的断言假绿。

生产路径：目标端截图 → ``mcp_runtime``（只查 PNG 签名，未查尺寸）→
``ReplayEvidenceRecorder.record`` → ``persist_screenshot`` → 证据索引。
本文件从两层锁死拒绝语义：

1. 证据持久层：0×0 / 0×N / N×0 PNG 必须在写入前拒绝（EvidenceIntegrityError），
   磁盘无残留、digest 不变。
2. 回放证据链：``ReplayEvidenceRecorder.record`` 对 0×0 帧必须返回 None 或拒绝，
   绝不能产生一条可通过 digest 校验的"空证据"。

正向对照：正常 8×8 帧仍然持久化成功（防修复过宽）。
"""

from __future__ import annotations

import hashlib
import struct
import sys
import zlib
from pathlib import Path

import pytest


_REPO = Path(__file__).resolve().parents[2]
for p in (str(_REPO / "target"), str(_REPO / "agent"), str(_REPO / "test_case")):
    if p not in sys.path:
        sys.path.insert(0, p)


from trace import (  # noqa: E402
    EvidenceIntegrityError,
    persist_screenshot,
    verify_evidence_on_disk,
)
from fake_target import make_png  # noqa: E402


def _kwargs():
    return dict(
        step_no=1,
        step_id="S001",
        snapshot_id="OBS-1",
        event_id="EVT-1",
        process_name="EDRClient.exe",
        pid=1234,
        window_title="EDR Home",
    )


def _zero_png(width: int, height: int) -> bytes:
    """IHDR 声明零尺寸的真实 PNG（签名/块结构/CRC 全部合法）。"""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    ihdr = (struct.pack(">I", len(ihdr_data)) + b"IHDR" + ihdr_data
            + struct.pack(">I", zlib.crc32(b"IHDR" + ihdr_data) & 0xFFFFFFFF))
    idat_data = zlib.compress(b"")
    idat = (struct.pack(">I", len(idat_data)) + b"IDAT" + idat_data
            + struct.pack(">I", zlib.crc32(b"IDAT" + idat_data) & 0xFFFFFFFF))
    iend = struct.pack(">I", 0) + b"IEND" + struct.pack(
        ">I", zlib.crc32(b"IEND") & 0xFFFFFFFF)
    return sig + ihdr + idat + iend


# ---------------------------------------------------------------------------
# 持久层：零尺寸 PNG 必须拒绝（先失败回归）
# ---------------------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize("width,height", [(0, 0), (0, 6), (8, 0)])
def test_zero_dimension_png_is_rejected_before_write(tmp_path: Path,
                                                     width: int, height: int):
    png = _zero_png(width, height)
    assert png.startswith(b"\x89PNG\r\n\x1a\n"), "fixture 必须是签名合法的 PNG"
    with pytest.raises(EvidenceIntegrityError):
        persist_screenshot(png, "after", trace_dir=tmp_path, **_kwargs())
    # 拒绝必须发生在写入前：screenshots 目录无残留、无 .tmp 垃圾
    screenshots = tmp_path / "screenshots"
    assert not screenshots.exists() or not any(screenshots.iterdir())


@pytest.mark.unit
def test_empty_bytes_still_rejected(tmp_path: Path):
    with pytest.raises(EvidenceIntegrityError):
        persist_screenshot(b"", "after", trace_dir=tmp_path, **_kwargs())


# ---------------------------------------------------------------------------
# 正向对照：正常尺寸帧不受影响（防修复过宽）
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_normal_png_still_persists_and_verifies(tmp_path: Path):
    png = make_png(width=8, height=8)
    rec = persist_screenshot(png, "after", trace_dir=tmp_path, **_kwargs())
    assert (rec.width, rec.height) == (8, 8)
    assert rec.sha256 == "sha256:" + hashlib.sha256(png).hexdigest()
    assert verify_evidence_on_disk(rec, tmp_path) is True


# ---------------------------------------------------------------------------
# 回放证据链：0×0 帧不得产生可通过校验的"空证据"
# ---------------------------------------------------------------------------


def _recorder(tmp_path: Path):
    from agent.recording.replay import ReplayEvidenceRecorder
    from agent.trace import TraceStore

    store = TraceStore(tmp_path)
    store.open(trace_id="TR-ZERO")
    return ReplayEvidenceRecorder(store), store


class _Step:
    step_no = 1
    step_id = "step-0001"


class _ZeroFrameObservation(dict):
    """携带 0×0 截图帧、scope/redacted 全部合法的回放观测。"""


@pytest.mark.unit
def test_zero_frame_never_becomes_verifiable_evidence(tmp_path: Path):
    recorder, store = _recorder(tmp_path)
    observation = _ZeroFrameObservation({
        "snapshot_id": "OBS-ZERO-1",
        "screenshot_bytes": _zero_png(0, 0),
        "screenshot_scope": "window",
        "screenshot_redacted": True,
        "screenshot_sha256": "sha256:" + hashlib.sha256(_zero_png(0, 0)).hexdigest(),
        "active_window": {"process_name": "EDRClient.exe", "pid": 4242,
                          "title": "EDRClient"},
    })
    from agent.execution import StepMaterializationError

    try:
        record = recorder.record(_Step(), observation)  # type: ignore[arg-type]
        # 或"跳过"路径：返回 None 且不落盘
        assert record is None, "record() 返回了证据 —— 必须为 None 或结构化拒绝"
    except StepMaterializationError as exc:
        assert exc.code, "拒绝必须携带结构化错误码"
        assert "0x0" in str(exc) or "zero" in str(exc).lower(), (
            f"诊断信息必须说明空帧: {exc}"
        )
        record = None
    # 无论选择"拒绝"还是"跳过"，绝不能落一条磁盘校验通过的证据。
    # （当前实现选择结构化拒绝：StepMaterializationError("replay_capture_invalid_frame")，
    #   与 replay_capture_not_redacted 等既有拒绝码风格一致。）
    if record is not None:
        assert not verify_evidence_on_disk(record, store.root), (
            "0×0 空帧被持久化且磁盘校验通过 —— visual_evidence_captured 将假绿"
        )
    persisted = list((store.root / "screenshots").glob("*.png")) \
        if (store.root / "screenshots").exists() else []
    assert persisted == [], f"空帧证据文件残留: {persisted}"
