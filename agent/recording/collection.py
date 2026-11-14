"""Agent-side collection of a stopped recording's artifacts.

This used to live inline in ``agent/cli.py``'s ``record stop`` branch: fetch
every source-redacted capture, verify each digest, compile the recording and
write the artifact directory.  The recorder UI bridge (U1) needs exactly the
same behavior without a human-issued CLI command, so the chain now lives here
and both callers share it — one implementation of "fetch, verify, compile,
persist", so a UI stop and a CLI stop produce the same artifacts.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import time
from pathlib import Path
from typing import Callable

from agent.recording.artifacts import write_compilation_artifacts
from agent.recording.compiler import compile_recording
from target.recording.models import RawRecording


class CollectionRetryError(RuntimeError):
    """收集链路断连预算耗尽——与 UIBridgeError 同语义，但面向 CLI 调用方。"""


class RetryingCollectionAgent:
    """收集链路（拉结果、逐帧取证）的瞬态断连重试。

    CLI ``record stop`` 与 UI 桥共用同一条收集链，断连恢复语义也必须一致：
    每个 call_tool 一次独立的连续失败预算——预算内重试，耗尽抛
    CollectionRetryError（UI 桥路径由 §6y 的 _RetryingAgent 包一层
    UIBridgeError 语义，行为不变）。
    """

    def __init__(self, agent, *, interval: float = 1.0, retries: int = 3,
                 log: Callable[[str], None] | None = None):
        self._agent = agent
        self._interval = interval
        self._budget = retries
        self._log = log

    def call_tool(self, name, arguments=None, timeout=None):
        misses = 0
        while True:
            try:
                return self._agent.call_tool(name, arguments, timeout=timeout)
            except Exception as exc:  # noqa: BLE001 - transient tunnel failures
                misses += 1
                if misses > self._budget:
                    raise CollectionRetryError(
                        f"target connection lost during collection after "
                        f"{misses} attempts: {exc}"
                    ) from exc
                if self._log:
                    self._log(f"收集链路连接中断，重试 {misses}/{self._budget} …")
                time.sleep(self._interval)


def _fetch_captures(agent, recording: RawRecording, timeout, session_id: str | None = None) -> tuple[dict[str, bytes], list[dict]]:
    """Fetch each referenced capture and verify its digest.

    A capture that fails to fetch, decode, or match the hash recorded on the
    event becomes a ``recording_capture_fetch_failed`` issue instead of
    silently dropping the evidence or pretending it arrived.
    """
    captures: dict[str, bytes] = {}
    seen_capture_ids: set[str] = set()
    capture_issues: list[dict] = []
    for event in recording.events:
        for evidence_key in ("beforeCapture", "capture"):
            metadata = event.evidence.get(evidence_key)
            capture_id = metadata.get("id") if isinstance(metadata, dict) else None
            if not isinstance(capture_id, str) or capture_id in seen_capture_ids:
                continue
            seen_capture_ids.add(capture_id)
            arguments = {"capture_id": capture_id}
            if session_id:
                arguments["session_id"] = session_id
            fetched = agent.call_tool(
                "get_recording_capture", arguments, timeout=timeout,
            )
            encoded = fetched.get("image_b64") if isinstance(fetched, dict) else None
            try:
                payload = base64.b64decode(encoded, validate=True) if isinstance(encoded, str) else None
            except (binascii.Error, ValueError):
                payload = None
            digest = (
                "sha256:" + hashlib.sha256(payload).hexdigest()
                if payload is not None else None
            )
            if (
                not isinstance(fetched, dict)
                or not fetched.get("ok")
                or payload is None
                or digest != metadata.get("sha256")
                or digest != fetched.get("sha256")
            ):
                capture_issues.append({
                    "code": "recording_capture_fetch_failed",
                    "sequence": event.sequence,
                    "evidence": evidence_key,
                    "captureId": capture_id,
                })
                continue
            captures[capture_id] = payload
    return captures, capture_issues


def collect_recording_artifacts(
    agent, result: dict, output_root: Path | str | None, profile: str | None, *,
    timeout=None, session_id: str | None = None,
):
    """Turn a ``stop_recording`` result into compiled on-disk artifacts.

    Returns ``(artifacts, capture_issues)``; callers keep assembling their own
    operator-facing payload.  Raises the same exception classes the CLI
    persisted-error path already handles (KeyError/OSError/ValueError).

    ``result["resultStore"]`` — the target's persistence fact, present on the
    direct stop-response channels (CLI ``record stop``, rerecord loop) but
    not on the durable read-back channel the UI bridge uses — flows through
    as the receipt's optional ``persistence`` key; when absent the key is
    simply not written (a missing fact is never forged).
    """
    recording = RawRecording.from_dict(result["recording"])
    compilation = compile_recording(recording, profile=profile)
    owner = session_id if session_id is not None else result.get("sessionId")
    captures, capture_issues = _fetch_captures(
        agent, recording, timeout, session_id=owner)
    root = Path(output_root).expanduser() if output_root else Path("recordings")
    artifacts = write_compilation_artifacts(
        root,
        recording,
        compilation,
        captures=captures,
        artifact_issues=capture_issues,
        persistence=result.get("resultStore"),
    )
    return artifacts, capture_issues
