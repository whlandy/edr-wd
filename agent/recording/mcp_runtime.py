"""Adapters that run the atomic executor through a TargetSubAgent MCP session."""

from __future__ import annotations

import hashlib
import json
import base64
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

from target.action_dispatcher import ActionReceipt, normalize
from agent.execution import BackendUnavailable
from agent.trace.events import EventType
from agent.trace.store import TraceStore
from target.windowing.observation import normalize_observed_control


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
        use_bundle: bool = False,
        run_token: str | None = None,
        include_ocr: bool = False,
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
        self._use_bundle = use_bundle
        self._run_token = run_token
        self._include_ocr = include_ocr

    def focus_window(
        self, process_name: str, title_regex: str, root_automation_id: str = "",
    ) -> str:
        """Connect and lock a specific window, then observe it.

        A recorded flow moves between an application's windows. Observation is
        one window's control tree, so replaying a step that happened in another
        window requires focusing that window first — otherwise its controls are
        simply not in the observation and the step resolves to nothing.
        """
        if self._run_token is not None and root_automation_id:
            focused = self._agent.call_tool("focus_window_selector", {
                "process_name": process_name, "title_regex": title_regex,
                "root_control_id": root_automation_id, "run_token": self._run_token,
            }, timeout=self._timeout)
            if not isinstance(focused, Mapping) or not focused.get("ok"):
                raise BackendUnavailable(
                    (focused.get("code") if isinstance(focused, Mapping) else None)
                    or "window_selector_focus_failed"
                )
            snapshot_id = self.refresh_bundle()
            if not self._observes_window(snapshot_id, root_automation_id):
                raise BackendUnavailable("focused window root identity changed")
            return snapshot_id
        connect_args = {"process_name": process_name, "title_re": title_regex,
                        "timeout": 10.0}
        lock_args = {"process_name": process_name, "title_re": title_regex,
                     "strict": True, "activate": True}
        if self._run_token is not None:
            def call_session(action_id: str, args: dict):
                receipt = self._agent.call_tool("execute_action", {
                    "action_id": action_id, "args": args,
                    "request_id": "focus-" + uuid.uuid4().hex,
                    "run_token": self._run_token,
                }, timeout=self._timeout)
                if isinstance(receipt, Mapping):
                    return receipt.get("result") if receipt.get("ok") else receipt
                return receipt
            connected = call_session("session.connect", connect_args)
        else:
            connected = self._agent.call_tool("connect", connect_args, timeout=self._timeout)
        if not isinstance(connected, Mapping) or not connected.get("ok"):
            raise BackendUnavailable(
                (connected.get("error") if isinstance(connected, Mapping) else None)
                or f"cannot connect the window {title_regex!r}"
            )
        if self._run_token is not None:
            locked = call_session("session.window_lock.set", lock_args)
        else:
            locked = self._agent.call_tool("lock_window", lock_args, timeout=self._timeout)
        if not isinstance(locked, Mapping) or not locked.get("ok"):
            raise BackendUnavailable(
                (locked.get("error") if isinstance(locked, Mapping) else None)
                or f"cannot lock the window {title_regex!r}"
            )
        snapshot_id = self.refresh()
        if root_automation_id and not self._observes_window(snapshot_id, root_automation_id):
            raise BackendUnavailable(
                f"focused a window that is not {root_automation_id!r}; the title "
                "matched a different window of the same application"
            )
        return snapshot_id

    def focus_role(self, role: str) -> str:
        if self._run_token is None:
            raise BackendUnavailable("product_run_expired")
        result = self._agent.call_tool("focus_window_role", {
            "role": role, "run_token": self._run_token,
        }, timeout=self._timeout)
        if not isinstance(result, Mapping) or not result.get("ok"):
            raise BackendUnavailable((result.get("code") if isinstance(result, Mapping) else None)
                                     or "window_role_not_found")
        return self.refresh_bundle()

    def _observes_window(self, snapshot_id: str, root_automation_id: str) -> bool:
        """Did focusing actually land on the window the step belongs to?

        Two of this application's processes title a window identically, so a
        title match is not proof. The observed controls name their own window
        in the first component of their automation id.
        """
        snapshot = self._snapshots.get(snapshot_id) or {}
        identity = snapshot.get("window_identity") or {}
        if identity.get("root_control_id"):
            return identity["root_control_id"] == root_automation_id
        prefix = root_automation_id + "."
        for control in snapshot.get("controls", ()):
            identifier = str((control or {}).get("automation_id") or "")
            if identifier == root_automation_id or identifier.startswith(prefix):
                return True
        return False

    def refresh(self) -> str:
        if self._use_bundle:
            return self.refresh_bundle()
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
        if any(not isinstance(source, Mapping) for source in controls):
            raise BackendUnavailable("dump_tree returned an invalid control entry")

        self._counter += 1
        snapshot_id = f"OBS-REPLAY-{self._counter:08d}"
        normalized = []
        for index, source in enumerate(controls, start=1):
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
                "text": None if protected is True else (source.get("text") or source.get("name") or source.get("title")),
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

    def refresh_bundle(self) -> str:
        """Accept only a target-produced, same-window tree/image observation."""
        bundle = self._agent.call_tool("observe_window_bundle", {
            "run_token": self._run_token, "capture_image": self._capture_screenshot,
            "include_ocr": self._include_ocr,
        }, timeout=self._timeout)
        if not isinstance(bundle, Mapping) or not bundle.get("ok"):
            raise BackendUnavailable((bundle.get("code") if isinstance(bundle, Mapping) else None)
                                     or "observe_window_bundle failed")
        observation_id = bundle.get("observation_id")
        identity = bundle.get("window_identity")
        tree = bundle.get("tree")
        if not isinstance(observation_id, str) or not observation_id.startswith("OBS-"):
            raise BackendUnavailable("bundle observation id is invalid")
        if not isinstance(identity, Mapping) or not identity.get("native_id") or not identity.get("generation"):
            raise BackendUnavailable("bundle window identity is invalid")
        if not isinstance(tree, Mapping) or not isinstance(tree.get("controls"), list):
            raise BackendUnavailable("bundle control tree is invalid")
        if any(not isinstance(source, Mapping) for source in tree["controls"]):
            raise BackendUnavailable("bundle control tree has an invalid control entry")
        screenshot_bytes = None
        if self._capture_screenshot:
            if bundle.get("screenshot_scope") != "window" or bundle.get("screenshot_redacted") is not True:
                raise BackendUnavailable("bundle screenshot is not redacted and window scoped")
            rect = identity.get("rect")
            if (not isinstance(rect, (list, tuple)) or len(rect) != 4
                    or not all(isinstance(value, int) and not isinstance(value, bool)
                               for value in rect)
                    or rect[2] <= rect[0] or rect[3] <= rect[1]):
                raise BackendUnavailable("bundle window rectangle is invalid")
            origin = bundle.get("screenshot_origin")
            # UIA's rectangle can exclude the invisible Win32 frame that the
            # screen capture includes. Permit only that small geometry skew;
            # an unrelated or missing origin must never reach the matcher.
            if (not isinstance(origin, (list, tuple)) or len(origin) != 2
                    or not all(isinstance(value, int) and not isinstance(value, bool)
                               for value in origin)
                    or abs(origin[0] - rect[0]) > 32
                    or abs(origin[1] - rect[1]) > 32):
                raise BackendUnavailable("bundle screenshot origin does not match locked window")
            encoded = bundle.get("screenshot_b64")
            if not isinstance(encoded, str):
                raise BackendUnavailable("bundle screenshot is missing")
            try:
                screenshot_bytes = base64.b64decode(encoded, validate=True)
            except Exception as exc:
                raise BackendUnavailable("bundle screenshot base64 is invalid") from exc
            expected_digest = "sha256:" + hashlib.sha256(screenshot_bytes).hexdigest()
            if expected_digest != bundle.get("screenshot_sha256"):
                raise BackendUnavailable("bundle screenshot digest mismatch")
            if not screenshot_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                raise BackendUnavailable("bundle screenshot is not PNG")
        process_name = identity.get("process_name") or "unknown"
        window_title = identity.get("title") or ""
        normalized = []
        for index, source in enumerate(tree["controls"], start=1):
            normalized.append(normalize_observed_control(source, identity, index))
        snapshot = {
            "snapshot_id": observation_id, "observation_id": observation_id,
            "captured_at": _utc_now(), "backend": identity.get("backend"),
            "window_identity": dict(identity), "controls": normalized,
            "windows": [{"process_name": process_name, "title": window_title}],
            "active_window": {"process_name": process_name, "title": window_title,
                              "rect": list(identity.get("rect") or ())},
            "ocr_boxes": list(bundle.get("ocr_boxes") or ()),
            "ocr_status": bundle.get("ocr_status") or "not_requested",
        }
        if screenshot_bytes is not None:
            snapshot.update({
                "screenshot_bytes": screenshot_bytes,
                "screenshot_sha256": bundle["screenshot_sha256"],
                "screenshot_origin": bundle.get("screenshot_origin"),
                "screenshot_scope": "window", "screenshot_redacted": True,
                "screenshot_redactions": bundle.get("screenshot_redactions") or [],
            })
        self._snapshots[observation_id] = snapshot
        self._latest = observation_id
        if self._trace_store is not None:
            trace_snapshot = {key: value for key, value in snapshot.items()
                              if key not in {"screenshot_bytes", "ocr_boxes"}}
            trace_snapshot["ocr_box_count"] = len(snapshot["ocr_boxes"])
            self._trace_store.append_dict(
                EventType.OBSERVATION_RECORDED,
                payload={"snapshot_id": observation_id, "observation": trace_snapshot},
            )
        while len(self._snapshots) > 32:
            self._snapshots.pop(next(iter(self._snapshots)))
        return observation_id

    def refresh_window_inventory(self) -> str:
        """Observe registered windows for window-only assertions.

        This does not require the old lock to remain foreground. The target
        filters windows against the active product profile and executable
        paths; a global desktop list must not satisfy a product assertion.
        """
        if self._run_token is None:
            raise BackendUnavailable("product_run_required_for_window_inventory")
        result = self._agent.call_tool(
            "observe_product_windows", {"run_token": self._run_token}, timeout=self._timeout,
        )
        if not isinstance(result, Mapping) or not result.get("ok"):
            raise BackendUnavailable(
                (result.get("code") if isinstance(result, Mapping) else None)
                or "observe_product_windows failed"
            )
        windows = result.get("windows")
        if not isinstance(windows, list):
            raise BackendUnavailable("product window inventory is invalid")
        normalized = []
        seen = set()
        for item in windows:
            if (not isinstance(item, Mapping) or not isinstance(item.get("native_id"), str)
                    or not item["native_id"] or not isinstance(item.get("process_id"), int)
                    or isinstance(item["process_id"], bool)
                    or not isinstance(item.get("generation"), int)
                    or item["generation"] < 1 or not isinstance(item.get("role"), str)
                    or not item["role"] or not isinstance(item.get("process_name"), str)
                    or not isinstance(item.get("title"), str)):
                raise BackendUnavailable("product window inventory is invalid")
            key = (item["native_id"], item["process_id"], item["generation"])
            if key in seen:
                raise BackendUnavailable("product window inventory is ambiguous")
            seen.add(key)
            normalized.append({
                "native_id": item["native_id"], "process_id": item["process_id"],
                "generation": item["generation"], "role": item["role"],
                "process_name": item["process_name"], "title": item["title"],
            })
        self._counter += 1
        snapshot_id = f"OBS-REPLAY-{self._counter:08d}"
        snapshot = {
            "snapshot_id": snapshot_id, "captured_at": _utc_now(),
            "controls": [], "windows": normalized, "active_window": None,
        }
        self._snapshots[snapshot_id] = snapshot
        self._latest = snapshot_id
        if self._trace_store is not None:
            self._trace_store.append_dict(
                EventType.OBSERVATION_RECORDED,
                payload={"snapshot_id": snapshot_id, "observation": snapshot},
            )
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
        run_token: str | None = None,
        resolution_evidence: Any | None = None,
    ) -> None:
        self._agent = agent
        self._trace_store = trace_store
        self._timeout = timeout
        self._run_token = run_token
        self._resolution_evidence = resolution_evidence

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
            # Consume the materializer's pending resolution provenance
            # exactly once, and only when it belongs to THIS action: a
            # confirmation-refused or otherwise undispatched step must
            # never lend its evidence channel to a later action's event.
            entry = None
            if self._resolution_evidence is not None:
                entry = self._resolution_evidence.latest
                self._resolution_evidence.latest = None
            payload = {
                "action_id": action_id,
                "request_id": request_id,
                "argument_keys": sorted(args),
                "target_ref": dict(target_ref or {}),
                "resolution": (
                    "semantic" if target_ref
                    else "visual" if action_id == "pointer.click_window"
                    else "none"
                ),
            }
            if entry and entry.get("action_id") == action_id and entry.get("evidence"):
                payload["resolution_evidence"] = {
                    **entry["evidence"],
                    "step_id": entry.get("step_id"),
                    "action_id": entry.get("action_id"),
                }
            self._trace_store.append_dict(
                EventType.ACTION_REQUESTED,
                payload=payload,
                call_id=request_id,
            )
        payload = {
            "action_id": action_id,
            "action_code": action_code,
            "args": dict(args),
            "target_ref": dict(target_ref or {}),
            "request_id": request_id,
        }
        if self._run_token is not None:
            payload["run_token"] = self._run_token
        result = self._agent.call_tool("execute_action", payload, timeout=self._timeout)
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
