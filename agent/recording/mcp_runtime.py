"""Adapters that run the atomic executor through a TargetSubAgent MCP session."""

from __future__ import annotations

import hashlib
import json
import base64
from datetime import datetime, timezone
from typing import Any, Mapping

from target.action_dispatcher import ActionReceipt, normalize
from agent.execution import BackendUnavailable
from agent.trace.events import EventType
from agent.trace.store import TraceStore


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _rect(value: Any) -> list[int] | None:
    if isinstance(value, (list, tuple)) and len(value) == 4:
        return [int(item) for item in value]
    if isinstance(value, Mapping):
        if all(key in value for key in ("left", "top", "right", "bottom")):
            return [int(value[key]) for key in ("left", "top", "right", "bottom")]
        if all(key in value for key in ("x", "y", "w", "h")):
            x, y = int(value["x"]), int(value["y"])
            return [x, y, x + int(value["w"]), y + int(value["h"])]
    return None


class MCPObservationProvider:
    """Build a fresh observation-local target set from lock + control tree."""

    def __init__(
        self,
        agent: Any,
        *,
        max_depth: int = 12,
        trace_store: TraceStore | None = None,
        capture_screenshot: bool = False,
        timeout: float | None = None,
    ) -> None:
        self._agent = agent
        self._max_depth = max_depth
        # Observation is several tool calls plus, in visual modes, a redacted
        # window capture. The per-call default is sized for a single cheap
        # call and times out well before that finishes on a live target.
        self._timeout = timeout
        self._counter = 0
        self._latest: str | None = None
        self._snapshots: dict[str, dict] = {}
        self._trace_store = trace_store
        self._capture_screenshot = capture_screenshot

    def refresh(self) -> str:
        verified = self._agent.call_tool("verify_window_lock", {"activate": False}, timeout=self._timeout)
        if not isinstance(verified, Mapping) or not verified.get("ok"):
            raise BackendUnavailable(
                (verified.get("error") if isinstance(verified, Mapping) else None)
                or "window ownership verification failed"
            )
        lock_result = self._agent.call_tool("get_window_lock", {}, timeout=self._timeout)
        tree = self._agent.call_tool("dump_tree", {"max_depth": self._max_depth}, timeout=self._timeout)
        if not tree.get("ok"):
            raise BackendUnavailable(tree.get("error") or "dump_tree failed")
        lock = lock_result.get("lock") if lock_result.get("ok") else None
        lock = lock if isinstance(lock, Mapping) else {}
        lock_snapshot = lock.get("snapshot") if isinstance(lock.get("snapshot"), Mapping) else {}
        process_name = (
            lock.get("process_name") or lock_snapshot.get("process_name")
            or tree.get("process_name") or "unknown"
        )
        window_title = lock_snapshot.get("title") or tree.get("window_title") or ""
        window_rect = _rect(lock_snapshot.get("rectangle") or lock_snapshot.get("rect"))
        controls = tree.get("controls")
        if not isinstance(controls, list):
            raise BackendUnavailable("dump_tree returned no controls array")

        self._counter += 1
        snapshot_id = f"OBS-REPLAY-{self._counter:08d}"
        normalized = []
        for index, source in enumerate(controls, start=1):
            if not isinstance(source, Mapping):
                continue
            protected = source.get("protected")
            if protected is None:
                protected = source.get("is_password")
            identity = {
                "process_name": process_name,
                "window_title": window_title,
                "control_type": source.get("control_type") or source.get("role") or source.get("class_name"),
                "automation_id": source.get("automation_id") or source.get("auto_id"),
                "control_id": source.get("control_id"),
                "class_name": source.get("class_name"),
                "identifier": source.get("identifier"),
                "text": source.get("text") or source.get("name") or source.get("title"),
                "rect": _rect(source.get("rect") or source.get("rectangle")),
            }
            digest = hashlib.sha256(
                json.dumps(identity, ensure_ascii=False, sort_keys=True, default=str).encode()
            ).hexdigest()
            target = {
                "target_id": f"T{index:04d}",
                **identity,
                "fingerprint": "sha256:" + digest,
                "value": None if protected is not False else source.get("value"),
                "checked": source.get("checked", source.get("toggle_state")),
                "enabled": source.get("enabled", source.get("is_enabled")),
                "protected": protected,
                "ancestry": source.get("ancestry") or [],
            }
            normalized.append(target)
        snapshot = {
            "snapshot_id": snapshot_id,
            "captured_at": _utc_now(),
            "backend": tree.get("backend"),
            "controls": normalized,
            "windows": [{"process_name": process_name, "title": window_title}],
            "active_window": {
                "process_name": process_name,
                "title": window_title,
                "rect": window_rect,
            },
        }
        if self._capture_screenshot:
            # replay_capture is the target-side source-redacted path.  Using it
            # rather than the raw screenshot tool is what makes a runtime frame
            # safe to persist into an execution report.
            screenshot = self._agent.call_tool("replay_capture", {}, timeout=self._timeout)
            if not isinstance(screenshot, Mapping):
                raise BackendUnavailable("replay_capture response is invalid")
            if screenshot.get("capture_scope") != "window":
                raise BackendUnavailable(
                    "visual replay requires a locked-window screenshot"
                )
            if screenshot.get("redacted") is not True:
                raise BackendUnavailable(
                    "visual replay requires a source-redacted capture"
                )
            encoded = screenshot.get("image_b64") or screenshot.get("image_base64") or screenshot.get("image")
            if not screenshot.get("ok") or not isinstance(encoded, str):
                raise BackendUnavailable(screenshot.get("error") or "screenshot capture failed")
            if encoded.startswith("data:"):
                encoded = encoded.partition(",")[2]
            try:
                screenshot_bytes = base64.b64decode(encoded, validate=True)
            except Exception as exc:
                raise BackendUnavailable("screenshot payload is not valid base64") from exc
            if not screenshot_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                raise BackendUnavailable("screenshot payload is not a PNG")
            snapshot["screenshot_bytes"] = screenshot_bytes
            snapshot["screenshot_sha256"] = "sha256:" + hashlib.sha256(screenshot_bytes).hexdigest()
            origin = screenshot.get("origin")
            if (
                not isinstance(origin, (list, tuple))
                or len(origin) != 2
                or not all(
                    isinstance(value, int) and not isinstance(value, bool)
                    for value in origin
                )
            ):
                raise BackendUnavailable("screenshot origin is invalid")
            snapshot["screenshot_origin"] = [int(origin[0]), int(origin[1])]
            snapshot["screenshot_scope"] = "window"
            snapshot["screenshot_redacted"] = True
            snapshot["screenshot_redactions"] = [
                list(rect) for rect in (screenshot.get("redactions") or [])
            ]
        self._snapshots[snapshot_id] = snapshot
        self._latest = snapshot_id
        if self._trace_store is not None:
            trace_snapshot = {
                key: value for key, value in snapshot.items()
                if key != "screenshot_bytes"
            }
            self._trace_store.append_dict(
                EventType.OBSERVATION_RECORDED,
                payload={"snapshot_id": snapshot_id, "observation": trace_snapshot},
            )
        # Observations are per-step and short-lived; bound agent memory.
        while len(self._snapshots) > 32:
            self._snapshots.pop(next(iter(self._snapshots)))
        return snapshot_id

    def latest_snapshot_id(self) -> str | None:
        return self._latest

    def get_snapshot(self, snapshot_id: str) -> dict | None:
        return self._snapshots.get(snapshot_id)


class MCPActionDispatch:
    def __init__(
        self,
        agent: Any,
        *,
        trace_store: TraceStore | None = None,
        timeout: float | None = None,
    ) -> None:
        self._agent = agent
        self._trace_store = trace_store
        self._timeout = timeout

    def __call__(
        self,
        *,
        action_id: str,
        action_code: str | None,
        args: Mapping[str, Any],
        target_ref: Mapping[str, Any] | None,
        request_id: str,
    ) -> ActionReceipt:
        if self._trace_store is not None:
            self._trace_store.append_dict(
                EventType.ACTION_REQUESTED,
                payload={
                    "action_id": action_id,
                    "request_id": request_id,
                    "argument_keys": sorted(args),
                    "target_ref": dict(target_ref or {}),
                    "resolution": (
                        "semantic" if target_ref
                        else "visual" if action_id == "pointer.click_window"
                        else "none"
                    ),
                },
                call_id=request_id,
            )
        result = self._agent.call_tool("execute_action", {
            "action_id": action_id,
            "action_code": action_code,
            "args": dict(args),
            "target_ref": dict(target_ref or {}),
            "request_id": request_id,
        }, timeout=self._timeout)
        if not isinstance(result, Mapping) or "code" not in result:
            receipt = normalize(
                result,
                action_id=action_id,
                action_code=action_code,
                request_id=request_id,
            )
        else:
            receipt = ActionReceipt(
                code=str(result["code"]),
                ok=bool(result.get("ok")),
                action_id=result.get("action_id", action_id),
                action_code=result.get("action_code", action_code),
                request_id=result.get("request_id", request_id),
                server_instance_id=str(result.get("server_instance_id") or "unknown"),
                result=result.get("result"),
                disabled_reason=result.get("disabled_reason"),
                missing=tuple(result["missing"]) if result.get("missing") else None,
                message=str(result.get("message") or ""),
                extras=dict(result.get("extras") or {}),
            )
        if self._trace_store is not None:
            self._trace_store.append_dict(
                EventType.ACTION_RESULT,
                payload={
                    "action_id": action_id,
                    "request_id": request_id,
                    "ok": receipt.ok,
                    "code": receipt.code,
                },
                call_id=request_id,
            )
        return receipt


__all__ = ["MCPActionDispatch", "MCPObservationProvider"]
