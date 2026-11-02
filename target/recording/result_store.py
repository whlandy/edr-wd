"""Durable, session-bound storage for stopped recordings.

A recording stops on the target — by the operator, by lease expiry, by the
recorder UI — and the Agent-side collector may not be connected at that
moment.  Without durable storage a disconnect silently throws away the raw
document, so "the recording stopped" could never be reported as "the
artifacts are safe".  This store keeps each session's source-redacted result
in its own directory until the Agent collects it.

Design constraints (docs/architecture/05-recording-usability-and-assertion-picker-design.md §5):

- one random sessionId per directory; a different session can never read
  another's result;
- atomic writes (temp file + ``os.replace``) with the recording document
  written once per session — a new save must not overwrite an uncollected
  old result;
- nothing here stores credentials or unredacted frames: the caller passes
  the already-redacted raw document, this module only persists it;
- collection is marked explicitly; results are never auto-deleted.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from .models import RecordingModelError

_SESSION_ID = re.compile(r"^REC-[0-9a-f]{32}$")
_RECORDING_FILE = "recording.json"
_META_FILE = "result-meta.json"


def _validated_session_id(session_id: str) -> str:
    if not isinstance(session_id, str) or not _SESSION_ID.match(session_id):
        raise RecordingModelError(
            "recording_result_session_invalid",
            "recording result must be addressed by a REC-<32 hex> session id",
            path="sessionId",
        )
    return session_id


def _atomic_write(path: Path, data: str | bytes) -> None:
    """Atomically write text or binary (PNG frames must survive byte-for-byte)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    import tempfile

    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        if isinstance(data, bytes):
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        else:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _digest(document: dict) -> str:
    return hashlib.sha256(
        json.dumps(document, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


class RecordingResultStore:
    """Session-bound durable results under ``root/<sessionId>/``."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def _session_dir(self, session_id: str) -> Path:
        return self.root / _validated_session_id(session_id)

    def save_stopped_recording(
        self, session_id: str, recording: dict, *, meta: dict | None = None,
    ) -> dict:
        """Persist one stopped recording. Idempotent for the same document;
        a different document under an existing session id is refused instead
        of silently overwriting a result the Agent has not collected yet."""
        directory = self._session_dir(session_id)
        existing = directory / _RECORDING_FILE
        if existing.exists():
            try:
                if _digest(json.loads(existing.read_text(encoding="utf-8"))) == _digest(recording):
                    return self._meta(directory, saved=False)
            except (OSError, ValueError):
                pass
            raise RecordingModelError(
                "recording_result_exists",
                "a different recording is already stored for this session",
                path="sessionId",
            )
        _atomic_write(existing, json.dumps(recording, ensure_ascii=False, indent=1))
        meta_doc = {
            "sessionId": session_id,
            "collected": False,
            "sha256": "sha256:" + _digest(recording),
            **(meta or {}),
        }
        _atomic_write(directory / _META_FILE, json.dumps(meta_doc, ensure_ascii=False, indent=1))
        return self._meta(directory, saved=True)

    def load_stopped_recording(self, session_id: str) -> dict:
        path = self._session_dir(session_id) / _RECORDING_FILE
        if not path.exists():
            raise RecordingModelError(
                "recording_result_missing",
                "no stored result for this session",
                path="sessionId",
            )
        return json.loads(path.read_text(encoding="utf-8"))

    def save_capture(self, session_id: str, capture_id: str, png_bytes: bytes) -> Path:
        """Persist one source-redacted capture frame beside the raw document.

        Screenshots live in memory only until now: a new recording cleared the
        evidence store, so an uncollected raw could no longer download its own
        evidence.  Persisted captures are verified by sha256 on collection.
        """
        _validated_session_id(session_id)
        if not re.fullmatch(r"CAP-[0-9a-f]{32}", capture_id or ""):
            raise RecordingModelError(
                "recording_result_capture_invalid", "malformed capture id",
                path="captureId",
            )
        if not isinstance(png_bytes, (bytes, bytearray)) or isinstance(png_bytes, str):
            # 拒绝 str/任意非二进制：不 encode 伪造 PNG，宁可失败也不落
            # 冒充截图的字节。
            raise RecordingModelError(
                "recording_capture_bytes_invalid",
                f"capture frame must be raw PNG bytes, got {type(png_bytes).__name__}",
                path="captureId",
            )
        path = self._capture_frame_path(session_id, capture_id)
        _atomic_write(path, bytes(png_bytes))
        return path

    def _capture_frame_path(self, session_id: str, capture_id: str) -> Path:
        """Frame path with symlink/escape guards.

        Session and capture ids are strict regexes (no traversal); the
        captures container must not be a planted symlink (checked **before**
        mkdir, which would follow it), and the resolved frame path must stay
        inside the store root.
        """
        session_dir = self._session_dir(session_id)
        captures_dir = session_dir / "captures"
        if captures_dir.is_symlink():
            raise RecordingModelError(
                "recording_capture_path_unsafe",
                "captures directory is a symbolic link; refusing to write",
                path="captureId",
            )
        captures_dir.mkdir(parents=True, exist_ok=True)
        frame = captures_dir / f"{capture_id}.png"
        if frame.is_symlink():
            raise RecordingModelError(
                "recording_capture_path_unsafe",
                "capture frame path is a symbolic link; refusing to write",
                path="captureId",
            )
        if not str(frame.resolve()).startswith(str(self.root.resolve())):
            raise RecordingModelError(
                "recording_capture_path_unsafe",
                "capture frame escapes the result store root",
                path="captureId",
            )
        return frame

    def read_capture_frame(self, session_id: str, capture_id: str) -> bytes:
        """按 (session, capture) 归属定点读帧：路径安全 + raw 引用与 hash
        元数据验证通过才返回字节。无归属歧义，无跨会话命中。"""
        frame = self._capture_frame_path(session_id, capture_id)
        if not frame.exists() or frame.is_symlink():
            raise RecordingModelError(
                "recording_capture_missing",
                f"no stored capture frame for {capture_id} in {session_id}",
                path="captureId",
            )
        png = frame.read_bytes()
        self._verify_capture_reference(session_id, capture_id, png)
        return png

    def find_capture(self, capture_id: str):
        """无 session 归属的旧调用兼容路径：扫描所有已存会话后**必须恰好
        命中一个**——多会话同名即歧义，明确拒绝，绝不返回排序第一份。
        命中还须通过 raw 文档的引用与 sha256 元数据验证：未经元数据验证的
        文件不当可信脱敏截图。"""
        if not re.fullmatch(r"CAP-[0-9a-f]{32}", capture_id or ""):
            raise RecordingModelError(
                "recording_result_capture_invalid", "malformed capture id",
                path="captureId",
            )
        if not self.root.exists():
            return None
        hits: list[tuple[str, Path]] = []
        for directory in sorted(self.root.iterdir()):
            if not re.fullmatch(r"REC-[0-9a-f]{32}", directory.name):
                continue
            frame = directory / "captures" / f"{capture_id}.png"
            # 归属发现与安全读取分离：planted symlink 也算 owner，读取时被
            # 定点防护显式拒绝——而不是静默跳过让它从待收集清单消失。
            if frame.exists():
                hits.append((directory.name, frame))
        if not hits:
            return None
        if len(hits) > 1:
            sessions = ", ".join(sorted(sid for sid, _ in hits))
            raise RecordingModelError(
                "recording_capture_ambiguous",
                f"capture {capture_id} 存在于 {len(hits)} 个会话（{sessions}），"
                "归属歧义——请带 session_id 查询，不取第一份。",
                path="captureId",
            )
        session_id = hits[0][0]
        # 唯一命中的帧读取统一走定点 read_capture_frame：符号链接/路径边界
        # 与 raw 元数据验证在同一处生效，兼容协议不豁免。
        png = self.read_capture_frame(session_id, capture_id)
        return session_id, png

    def _verify_capture_reference(self, session_id: str, capture_id: str,
                                  png: bytes) -> None:
        """磁盘帧必须能对上该会话 raw 文档里的引用与 sha256 元数据——
        未经元数据验证的文件不当可信脱敏截图。"""
        try:
            raw = self.load_stopped_recording(session_id)
        except RecordingModelError:
            raise RecordingModelError(
                "recording_capture_unreferenced",
                f"capture {capture_id} 无所属会话的 raw 文档，拒绝当可信截图",
                path="captureId",
            )
        expected = None
        for event in raw.get("events") or []:
            for key in ("beforeCapture", "capture"):
                metadata = (event.get("evidence") or {}).get(key)
                if isinstance(metadata, dict) and metadata.get("id") == capture_id:
                    expected = metadata.get("sha256")
                    break
            if expected:
                break
        if not expected:
            raise RecordingModelError(
                "recording_capture_unreferenced",
                f"capture {capture_id} 未被会话 {session_id} 的 raw 文档引用",
                path="captureId",
            )
        actual = "sha256:" + hashlib.sha256(png).hexdigest()
        if expected != actual:
            raise RecordingModelError(
                "recording_capture_hash_mismatch",
                f"磁盘帧 hash 与事件元数据不一致：{actual} != {expected}",
                path="captureId",
            )

    def load_captures(self, session_id: str) -> dict[str, bytes]:
        captures_dir = self._session_dir(session_id) / "captures"
        if not captures_dir.exists():
            return {}
        out: dict[str, bytes] = {}
        for path in sorted(captures_dir.glob("CAP-*.png")):
            out[path.stem] = path.read_bytes()
        return out

    def result_meta(self, session_id: str) -> dict:
        directory = self._session_dir(session_id)
        if not (directory / _RECORDING_FILE).exists():
            raise RecordingModelError(
                "recording_result_missing",
                "no stored result for this session",
                path="sessionId",
            )
        return self._meta(directory, saved=False)

    def mark_collected(self, session_id: str) -> dict:
        directory = self._session_dir(session_id)
        meta_path = directory / _META_FILE
        if not (directory / _RECORDING_FILE).exists() or not meta_path.exists():
            raise RecordingModelError(
                "recording_result_missing",
                "no stored result for this session",
                path="sessionId",
            )
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["collected"] = True
        _atomic_write(meta_path, json.dumps(meta, ensure_ascii=False, indent=1))
        return meta

    def list_pending(self) -> list[str]:
        """Session ids that hold an uncollected result, oldest first."""
        if not self.root.exists():
            return []
        pending = []
        for directory in sorted(self.root.iterdir()):
            if not directory.is_dir() or not _SESSION_ID.match(directory.name):
                continue
            meta_path = directory / _META_FILE
            if not (directory / _RECORDING_FILE).exists() or not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if meta.get("collected") is not True:
                pending.append(directory.name)
        return pending

    @staticmethod
    def _meta(directory: Path, *, saved: bool) -> dict:
        try:
            meta = json.loads((directory / _META_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            meta = {}
        meta["stored"] = True
        meta["overwritten"] = False
        meta["freshSave"] = saved
        return meta
