"""Recording management facade used by MCP tools and tests."""

from __future__ import annotations

import base64
import hashlib
import uuid
from typing import Callable

from .models import CaptureScope, RecordingModelError
from .session import (
    CaptureSessionState, CaptureSource, RecordingSession, RecordingSessionManager,
)
from .indicator import IndicatorCallbacks, NullRecordingIndicator, RecordingIndicator
from .evidence import RecordingEvidenceStore
from .result_store import RecordingResultStore


def unavailable_source_factory(
    scope: CaptureScope,
    sink: Callable,
) -> CaptureSource:
    del sink
    raise RecordingModelError(
        "recording_capture_unavailable",
        f"desktop capture source is not available for backend {scope.backend!r}",
        path="scope.backend",
    )


class RecordingService:
    def __init__(
        self,
        *,
        target_name: str,
        backend: str,
        source_factory: Callable[[CaptureScope, Callable], CaptureSource] = unavailable_source_factory,
        manager: RecordingSessionManager | None = None,
        indicator_factory: Callable[[str, CaptureScope, IndicatorCallbacks], RecordingIndicator] | None = None,
        backend_object=None,
        evidence_store: RecordingEvidenceStore | None = None,
        result_store: "RecordingResultStore | None" = None,
    ) -> None:
        self.target_name = target_name
        self.backend = backend
        self.source_factory = source_factory
        self.manager = manager or RecordingSessionManager()
        self.indicator_factory = indicator_factory or (
            lambda _name, _scope, _callbacks: NullRecordingIndicator()
        )
        self.evidence_store = evidence_store or (
            RecordingEvidenceStore(backend_object) if backend_object is not None else None
        )
        # Durable per-session results for stopped recordings: an Agent that
        # disconnects before collecting must not cost the operator their raw
        # document.  None simply disables the durability layer.
        self.result_store = result_store

    def start(self, *, name: str, process_name: str, window_title: str, lease_seconds: float = 300.0) -> dict:
        if not all(isinstance(x, str) and x.strip() for x in (name, process_name, window_title)):
            raise RecordingModelError("recording_scope_not_unique", "name, process_name, and window_title are required", path="scope")
        scope = CaptureScope(self.target_name, self.backend, process_name, window_title)
        evidence_initialization = None
        session = RecordingSession(
            "REC-" + uuid.uuid4().hex,
            name,
            scope,
            evidence_attacher=(self.evidence_store.attach if self.evidence_store else None),
            lease_seconds=lease_seconds,
        )
        source = self.source_factory(scope, session.ingest)
        session.attach_source(source)
        # F52 attach 诊断：session 持有 source 引用——status 可报
        # captureDiagnostics（事件到达计数/队列深度/启动错误），actions=0
        # 时区分「钩子没起」vs「钩子起了但事件没来」，避免盲目重启。

        def read_target():
            current = getattr(source, "current_observed_target", None)
            return current() if callable(current) else None

        def mark_then(action):
            """Stamp the correlator before running a panel-button callback.

            The button's own down/up packets reach the hook right after this
            fires; the correlator reads the mark window to tell the recorder
            operating itself from the panel swallowing a click aimed at the
            target (C08).  Failure to stamp must never block the action —
            the action owns recording state, the mark is only diagnostics.
            """
            def run(*args, **kwargs):
                correlator = getattr(source, "_correlator", None)
                mark = getattr(correlator, "mark_recorder_control", None)
                if callable(mark):
                    try:
                        mark()
                    except Exception:
                        pass
                return action(*args, **kwargs)
            return run

        indicator = self.indicator_factory(
            name,
            scope,
            IndicatorCallbacks(
                pause=mark_then(session.pause),
                resume=mark_then(session.resume),
                # 面板/浮窗的 Stop 必须走与 CLI stop 相同的持久化链
                # （result_store），否则浮窗停止后 Agent 断连即丢录制。
                stop=mark_then(self.stop),
                add_assertion=session.add_assertion,
                read_target=read_target,
            ),
        )
        session.attach_indicator(indicator)
        def initialize_evidence() -> None:
            nonlocal evidence_initialization
            # 上一轮过期会话的产物必须先落盘再被替换——start() 即将换掉
            # manager 里的会话对象并 evidence_store.clear()。
            self._reconcile_expired_result()
            if self.evidence_store is not None:
                self.evidence_store.clear()
                evidence_initialization = self.evidence_store.initialize()
                if not evidence_initialization.get("ok"):
                    raise RecordingModelError(
                        "recording_capture_evidence_unavailable",
                        str(evidence_initialization.get("error") or "INIT screenshot unavailable"),
                        path="recording.evidenceInitialization",
                    )

        result = {
            "ok": True,
            **self.manager.start(session, before_start=initialize_evidence).to_dict(),
            "scope": scope.to_dict(),
            # Which of the application's existing windows the capture admitted.
            # An empty list next to a busy application is the visible symptom
            # of input being silently refused as out of scope.
            "seededScope": list(getattr(source, "seeded_scope", ()) or ()),
            # Which processes count as this application. An empty list beside
            # a multi-process product is the visible symptom of a flow that
            # crosses into a sibling process being refused.
            "scopeProcessNames": list(
                getattr(getattr(source, "_correlator", None), "scope_process_names", ())
                or ()
            ),
            "seededScopeError": getattr(
                getattr(source, "_correlator", None), "scope_seed_error", None,
            ),
        }
        if evidence_initialization is not None:
            result["evidenceInitialization"] = evidence_initialization
        return result

    def _reconcile_expired_result(self) -> None:
        """把停在终态的过期会话的录制产物落进 result_store。

        lease 过期在 session 内部完成（watchdog 或懒过期），service 的
        user-stop/UI-stop 持久化链路帮不到它——下一次 start() 会替换会话
        对象并 evidence_store.clear()，raw 文档与脱敏帧同时消失。result_store
        的存在理由就是"Agent 不在线时录制停止也不能丢"，过期是三种停止
        方式里唯一没接入的。这里在 service 的观察点上补齐：终态 STOPPED
        会话落盘 raw + 事件引用的帧；落盘失败不吞（status 如实报错，不能
        让 operator 以为已安全保存）。FAILED 会话不落盘——source 收尾失败
        的文档不能冒充干净结果，错误已由过期路径如实抛出。
        """
        if self.result_store is None:
            return
        try:
            session = self.manager.current()
        except RecordingModelError:
            return
        if session.state is not CaptureSessionState.STOPPED:
            return
        if getattr(session, "_reason", "") != "recording_lease_expired":
            return
        session_id = session.session_id
        document = session.recording().to_dict()
        try:
            self.result_store.save_stopped_recording(
                session_id, document, meta={"name": session.name},
            )
            self._persist_captures(session_id, document)
            failures = getattr(self, "_last_capture_failures", [])
            if failures:
                raise RecordingModelError(
                    "recording_result_store_failed",
                    f"lease-expired recording persisted with {len(failures)} "
                    f"capture failures; first: {failures[0]}",
                    path="sessionId",
                )
        except RecordingModelError:
            raise
        except Exception as exc:
            raise RecordingModelError(
                "recording_result_store_failed",
                f"lease-expired recording could not be persisted: {exc}",
                path="sessionId",
            ) from exc

    def status(self, *, heartbeat: bool = False) -> dict:
        self._reconcile_expired_result()
        session = self.manager.current()
        receipt = session.heartbeat() if heartbeat else session.status()
        result = {"ok": True, **receipt.to_dict(), "scope": session.scope.to_dict(), "name": session.name}
        # F52：attach 诊断进 status——captureDiagnostics=source.diagnostics()。
        source = getattr(session, "_source", None)
        if source is not None and callable(getattr(source, "diagnostics", None)):
            try:
                result["captureDiagnostics"] = source.diagnostics()
            except Exception:
                pass
        return result

    def _mark_recorder_control(self) -> None:
        """C08 stamp for EVERY stop/pause/resume entry point.

        The panel's stop (RecorderUIController.apply_ui_request → service)
        previously bypassed the indicator's mark_then wrapper, so the stop
        click's own down/up packets classified as recorder_ui — the panel
        "swallowing input aimed at the target" — and flagged every honest
        capture with a false occlusion advisory (live REC-079b4008 and the
        three rounds before it). Same stamp, every door."""
        session = self.manager.current()
        source = getattr(session, "_source", None)
        correlator = getattr(source, "_correlator", None)
        mark = getattr(correlator, "mark_recorder_control", None)
        if callable(mark):
            try:
                mark()
            except Exception:
                pass

    def pause(self) -> dict:
        self._mark_recorder_control()
        return {"ok": True, **self.manager.current().pause().to_dict()}

    def resume(self) -> dict:
        self._mark_recorder_control()
        return {"ok": True, **self.manager.current().resume().to_dict()}

    def stop(self) -> dict:
        self._mark_recorder_control()
        receipt, recording = self.manager.current().stop()
        result = {"ok": True, **receipt.to_dict(), "recording": recording.to_dict()}
        # Persist before handing the document back: the caller may disconnect
        # right after receiving it, and a durable copy is what makes "stopped"
        # different from "lost".  A store failure is reported on the result —
        # it must not turn a successful stop into an error.
        if self.result_store is not None:
            try:
                stored = self.result_store.save_stopped_recording(
                    receipt.session_id, result["recording"],
                    meta={"name": result["recording"].get("name")},
                )
                captures_saved = self._persist_captures(receipt.session_id, result["recording"])
                failures = getattr(self, "_last_capture_failures", [])
                result["resultStore"] = {
                    "ok": not failures, "freshSave": stored.get("freshSave"),
                    "collected": stored.get("collected"),
                    "captures": captures_saved,
                    **({"captureErrors": failures} if failures else {}),
                }
            except Exception as exc:  # noqa: BLE001 - durability is best-effort
                result["resultStore"] = {"ok": False, "error": str(exc)}
        return result

    def _persist_captures(self, session_id: str, recording: dict) -> int:
        """把当前 evidence_store 里的脱敏帧随 raw 一起落盘。

        写盘/解码失败**不吞**：失败帧计数返回，stop 结果据此置
        resultStore.ok=False 并列出 captureErrors —— 缺证据的录制必须留在
        pending，不能报干净成功。"""
        if self.evidence_store is None:
            return 0
        saved = 0
        failures: list[dict] = []
        for event in recording.get("events") or []:
            for key in ("beforeCapture", "capture"):
                metadata = (event.get("evidence") or {}).get(key)
                capture_id = metadata.get("id") if isinstance(metadata, dict) else None
                if not isinstance(capture_id, str) or not capture_id:
                    continue
                try:
                    fetched = self.evidence_store.get(capture_id)
                    if not (isinstance(fetched, dict) and fetched.get("ok")):
                        failures.append({"captureId": capture_id, "evidence": key,
                                         "error": "capture frame unavailable in memory"})
                        continue
                    encoded = fetched.get("image_b64")
                    self.result_store.save_capture(
                        session_id, capture_id, base64.b64decode(encoded, validate=True))
                    saved += 1
                except Exception as exc:  # noqa: BLE001 - surfaced, not swallowed
                    failures.append({"captureId": capture_id, "evidence": key,
                                     "error": str(exc)})
        self._last_capture_failures = failures
        return saved

    # -- Service-level UI lifecycle (U0 skeleton; visible panel lands in U1) --

    def ui_snapshot(self) -> dict:
        """What the recorder panel should display, readable with no session.

        Idle is a real state, not an error: the panel exists on the target's
        interactive desktop even before anything is recorded.  Facts come from
        the live session when one exists; pending durable results are listed
        so the panel can show that a stop happened while the Agent was away.
        """
        # 过期会话可能仍未落盘：面板是 operator 不经 Agent 就能看到的观察点。
        # 先对账再取 pending，面板第一次刷新就能看到这份结果。
        self._reconcile_expired_result()
        pending = self.result_store.list_pending() if self.result_store else []
        try:
            session = self.manager.current()
        except RecordingModelError:
            session = None
        if session is None:
            return {
                "ok": True, "viewState": "IDLE", "session": None,
                "pendingResults": pending,
                "interactiveDesktop": self.backend,
            }
        receipt = session.status()
        try:
            pending_verifiers = session.pending_verifiers()
        except Exception:  # noqa: BLE001 - display-only hint
            pending_verifiers = []
        view_state = {
            CaptureSessionState.RECORDING: "RECORDING",
            CaptureSessionState.PAUSED: "PAUSED",
            CaptureSessionState.STOPPING: "STOPPING",
            CaptureSessionState.STOPPED: "STOPPED",
            CaptureSessionState.FAILED: "ERROR",
        }.get(receipt.state, "IDLE")
        return {
            "ok": True,
            "viewState": view_state,
            "session": {
                "sessionId": receipt.session_id,
                "name": session.name,
                "actionCount": receipt.action_count,
                "reason": receipt.reason,
                "leaseSecondsRemaining": receipt.lease_seconds_remaining,
                "scope": session.scope.to_dict(),
                "pendingVerifiers": pending_verifiers,
            },
            "pendingResults": pending,
            "interactiveDesktop": self.backend,
        }

    def open_ui(self) -> dict:
        """Show or restore the recorder panel.  U0 returns the lifecycle
        skeleton's view; the visible Tk panel and window picker land in U1."""
        snapshot = self.ui_snapshot()
        return {"ok": True, "panel": "recorder", "snapshot": snapshot}

    def begin_pick(self, binding=None) -> dict:
        return {"ok": True, **self.manager.current().begin_pick(binding=binding)}

    def cancel_pick(self) -> dict:
        return {"ok": True, **self.manager.current().cancel_pick()}

    def list_controls(self) -> list[dict]:
        """Flattened component list from this session's control snapshots."""
        return self.manager.current().control_inventory()

    def freeze_component_draft(self, component: dict, *, binding=None) -> dict:
        return self.manager.current().freeze_component_draft(component, binding=binding)

    def list_pending_results(self) -> list[str]:
        return self.result_store.list_pending() if self.result_store else []

    def mark_result_collected(self, session_id: str) -> dict:
        if self.result_store is None:
            raise RecordingModelError(
                "recording_result_unavailable",
                "no durable result store is configured on this target",
                path="sessionId",
            )
        return self.result_store.mark_collected(session_id)

    def get_result(self, session_id: str) -> dict:
        """Return one stored stopped result by its own session id.

        Session binding is the storage key itself, so a caller can never read
        another session's recording; an unknown id is a clean miss, not an
        error that could be mistaken for data loss.
        """
        if self.result_store is None:
            raise RecordingModelError(
                "recording_result_unavailable",
                "no durable result store is configured on this target",
                path="sessionId",
            )
        recording = self.result_store.load_stopped_recording(session_id)
        meta = self.result_store.result_meta(session_id)
        return {"ok": True, "sessionId": session_id,
                "recording": recording, "meta": meta}

    def assertion(self) -> dict:
        return {"ok": True, **self.manager.current().request_assertion_editor().to_dict()}

    # -- Region / multi-component assertion forwarding (Ctrl+Alt+Q entry) ---

    def begin_region_assertion(self) -> dict:
        return self.manager.current().begin_region_assertion()

    def add_region_assertion(self, draft: dict) -> dict:
        return self.manager.current().add_region_assertion(draft)

    def cancel_region_assertion(self) -> dict:
        return self.manager.current().cancel_region_assertion()

    def recent_region_actions(self, limit: int = 5) -> list[dict]:
        """最近动作候选数据源（供 region 向导「绑定到最近动作」展示）。"""
        return self.manager.current().recent_actions(limit)

    def _memory_capture_owner(self) -> str | None:
        """内存 evidence_store 里的帧属于唯一的活跃（或最近）会话；无会话
        记录时内存帧没有可证明的归属。CAP id 的随机唯一性不是授权。"""
        manager = getattr(self, "manager", None)
        if manager is None:
            return None
        try:
            return manager.current().session_id
        except RecordingModelError:
            return None

    def capture(self, *, capture_id: str, session_id: str | None = None) -> dict:
        """取一帧脱敏截图：内存命中与磁盘恢复都必须验证**显式会话归属**。

        内存帧属于唯一活跃（或最近）会话：显式 session_id 必须与之匹配，
        否则拒绝；无会话归属记录的内存帧不可信。磁盘路径带 session_id 定点
        恢复，旧无 session 调用仅在唯一合法归属时兼容（歧义明确拒绝）。
        """
        fetched = None
        if self.evidence_store is not None:
            fetched = self.evidence_store.get(capture_id)
        if isinstance(fetched, dict) and fetched.get("ok"):
            owner = self._memory_capture_owner()
            if owner is None:
                raise RecordingModelError(
                    "recording_capture_unowned",
                    f"memory frame {capture_id} has no provable session ownership",
                    path="captureId",
                )
            if session_id is not None and session_id != owner:
                raise RecordingModelError(
                    "recording_capture_session_mismatch",
                    f"capture {capture_id} belongs to session {owner}, "
                    f"not {session_id}",
                    path="sessionId",
                )
            return {**fetched, "sessionId": owner}
        if not (isinstance(fetched, dict) and fetched.get("ok")) and self.result_store is not None:
            if session_id is not None:
                png = self.result_store.read_capture_frame(session_id, capture_id)
                digest = "sha256:" + hashlib.sha256(png).hexdigest()
                return {"ok": True, "image_b64": base64.b64encode(png).decode(),
                        "sha256": digest, "sessionId": session_id}
            found = self.result_store.find_capture(capture_id)
            if found is not None:
                stored_session, png = found
                digest = "sha256:" + hashlib.sha256(png).hexdigest()
                return {"ok": True, "image_b64": base64.b64encode(png).decode(),
                        "sha256": digest, "sessionId": stored_session}
        if not (isinstance(fetched, dict) and fetched.get("ok")):
            raise RecordingModelError(
                "recording_capture_missing", "recording capture evidence is unavailable"
            )
        return fetched


def error_result(exc: Exception) -> dict:
    if isinstance(exc, RecordingModelError):
        return {"ok": False, "code": exc.code, "error": str(exc), "path": exc.path}
    return {"ok": False, "code": "recording_internal_error", "error": str(exc)}


def source_factory_for_backend(
    backend: str,
    backend_object=None,
) -> Callable[[CaptureScope, Callable], CaptureSource]:
    if backend == "windows_pywinauto":
        from .windows import windows_source_factory

        return windows_source_factory(backend_object)
    if backend == "macos_accessibility":
        from .macos import macos_source_factory

        return macos_source_factory(backend_object)
    return unavailable_source_factory
