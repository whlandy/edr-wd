"""租约内目标实例绑定协议(S3,agent 侧;09 号设计 §7/§8)。

复验边界(1227):本协议必须经**现有 target 端授权/串行边界**执行,不得
自建锁或用可变字典代替产品授权:

- **租约/产品域**:所有绑定/重验/聚焦/执行都在
  ``target.product_policy.authorized_dispatch(run_token)`` 内进行 ——
  即现有 ``require_product_run`` 租约栅栏(到期/竞争/他人运行由 policy
  拒绝);本协议不自建租约比较;
- **capability 严格协商**:目标 capability 是结构化响应
  ``{"supported": bool, "coverage": "strong"|...}`` — truthiness 不算;
  supported 假或 coverage 非 "strong" 一律在副作用前拒绝强实例能力
  (§8:工具名齐全不等于能力齐备;S1 录制覆盖不替代本次 run 的跟踪);
- **声明为权威**:declared_windows 是 golden 窗口声明;未知 W、调用者
  identity 冲突、非法 regex 一律拒绝;root/title 同时声明则都必须满足;
- **生命周期状态机**(§7.1):UNBOUND/BOUND/HIDDEN/LOST/RETIRED;只有
  明确同实例 SHOW 事实(同 native_id + 同 owner + 身份一致)恢复 HIDDEN;
  LOST 不重猜、兄弟不顶替;同 pid 同 HWND 复用 = 新实例(旧 binding LOST);
  generation/visible/foreground 声明或事实缺失 → 拒绝;
- **focus→dispatch 替换防护**(§7.2 步骤 3-4):聚焦成功后、dispatch 前,
  同一 authorized 边界内重验全部声明字段;替换 = 0 动作;
- **§7.6 基础**:step_context 的 run/binding/transition 依据来自实际验证
  结果(跨 run 拒绝;LOST 不产生 same_window_reverified);上一状态为只读
  视图。append-only 持久化在 S4。

生产接线:protocol 注入 backend(生产=实际 backend 对象,测试=替身 OS 层)
与 target.product_policy(租约/串行边界本身来自现有产品代码,不被 fake
替代)。
"""

from __future__ import annotations

import re as _re
import secrets
import threading
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

__all__ = ["WindowBindingProtocol", "WindowBindingError"]

_CAPABILITY_NAME = "window_sequence/v1"
_STRONG_COVERAGE = "strong"


class WindowBindingError(ValueError):
    """Stable-code refusal for the window-binding protocol."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


@dataclass
class _Binding:
    window_id: str
    token: str
    native_id: str
    identity: dict
    bound_pid: int
    bound_root: str | None
    bound_title: str | None
    revision: int
    run_id: str
    state: str = "BOUND"


@dataclass
class _StepContext:
    run_id: str
    step_id: str
    attempt_id: str
    window_id: str
    previous_step_id: str | None
    previous_attempt_id: str | None
    previous_window_id: str | None
    binding_revision: int
    transition: str

    def to_readonly_view(self) -> Mapping:
        # §7.6 field names are part of the contract (camelCase per design).
        # The view is read-only: the next step reads prior state but cannot
        # rewrite it.
        return MappingProxyType({
            "runId": self.run_id,
            "stepId": self.step_id,
            "attemptId": self.attempt_id,
            "previousStepId": self.previous_step_id,
            "previousAttemptId": self.previous_attempt_id,
            "previousWindowId": self.previous_window_id,
            "windowId": self.window_id,
            "bindingRevision": self.binding_revision,
            "transition": self.transition,
        })


def _reconcile_intervals(timeline_events):
    """Keep the FIRST unprovable boundary (review 1127) — shared lifecycle
    semantics with the compiler's gate. start = first fact time; end =
    first destroyed/lost fact time (identity_changed does not end the old
    instance's own interval)."""
    intervals: dict[str, tuple[int | None, int | None]] = {}
    for fact in timeline_events:
        ri = fact.get("instanceId")
        ms = fact.get("monotonicMs")
        if not ri or ms is None:
            continue
        start, _ = intervals.get(ri, (None, None))
        if start is None or ms < start:
            intervals[ri] = (start, intervals.get(ri, (None, None))[1])
        if ri not in intervals:
            intervals[ri] = (ms, None)
    for fact in timeline_events:
        kind = fact.get("kind")
        ri = fact.get("instanceId")
        if kind in {"destroyed", "lost"} and ri:
            start, end = intervals.get(ri, (None, None))
            ms = int(fact.get("monotonicMs") or 0)
            if end is None or ms < end:
                intervals[ri] = (start, ms)
    return intervals


class WindowBindingProtocol:
    """Agent-side binding protocol executing through the target's own
    lease/serialization boundaries (product_policy + backend)."""

    def __init__(self, *, backend, policy, lease_token: str,
                 declared_windows: Mapping[str, Mapping],
                 lifecycle_events: list | None = None):
        """``backend`` is the production backend object (focus/inventory
        owner); ``policy`` is ``target.product_policy`` (lease authority).
        ``lease_token`` must be the ACTIVE product run's token (replay
        acquires it via begin_product_run before assembly).
        ``declared_windows`` is the golden's authoritative declaration.
        ``lifecycle_events`` are the recorded timeline facts (raw v2)."""
        self._backend = backend
        self._policy = policy
        self._lease_token = lease_token
        self._declared = dict(declared_windows)
        self._lock = threading.RLock()
        self._bindings: dict[str, _Binding] = {}
        self._token_to_window: dict[str, str] = {}
        self._revision = 0
        self._lifecycle = list(lifecycle_events or ())

    # ---- capability: structured negotiation (§8) ----

    def _capability(self) -> dict:
        getter = getattr(self._backend, "window_sequence_capability", None)
        if callable(getter):
            response = getter()
            if isinstance(response, Mapping):
                return dict(response)
        return {"supported": False, "coverage": "unknown"}

    def _require_capability(self):
        capability = self._capability()
        if not capability.get("supported"):
            raise WindowBindingError(
                "window_sequence_capability_unavailable",
                "target does not support window_sequence/v1")
        if capability.get("coverage") != _STRONG_COVERAGE:
            raise WindowBindingError(
                "window_sequence_capability_unavailable",
                f"target instance-tracking coverage is "
                f"{capability.get('coverage')!r}; strong instance binding "
                "requires 'strong' coverage (S1 recording coverage never "
                "substitutes for the target run's own tracking)")

    def _require_declared(self, window_id: str, identity: Mapping) -> dict:
        declared = self._declared.get(window_id)
        if declared is None:
            raise WindowBindingError(
                "window_binding_not_declared",
                f"{window_id} is not in the golden window declaration; "
                "caller-supplied identities are refused")
        for key, value in declared.items():
            if key in identity and identity[key] != value:
                raise WindowBindingError(
                    "window_binding_not_declared",
                    f"{window_id}: caller identity {key}={identity[key]!r} "
                    f"conflicts with the declared {value!r}")
        import re as _re
        # ALL caller-declared fields are authoritative for resolution and
        # must be valid (review 1227: declared_windows is the authority; an
        # identity the caller adds — e.g. titleRegex — must still verify
        # against the live inventory, never be silently dropped).
        for key in ("titleRegex", "rootAutomationId"):
            value = identity.get(key)
            if value is None:
                continue
            if not isinstance(value, str):
                raise WindowBindingError(
                    "window_binding_not_declared",
                    f"{window_id}: identity.{key} must be a string")
            if key == "titleRegex":
                try:
                    _re.compile(value)
                except _re.error as exc:
                    raise WindowBindingError(
                        "window_binding_not_declared",
                        f"{window_id}: identity.titleRegex is invalid: {exc}",
                    ) from exc
        for source in (declared, identity):
            regex = source.get("titleRegex")
            if regex:
                try:
                    _re.compile(regex)
                except _re.error as exc:
                    raise WindowBindingError(
                        "window_binding_not_declared",
                        f"{window_id}: titleRegex is invalid: {exc}",
                    ) from exc
        effective = {**declared,
                     **{k: v for k, v in identity.items()
                        if k.startswith("_") or k in ("titleRegex",
                                                      "rootAutomationId")}}
        if not effective.get("titleRegex") and not effective.get("rootAutomationId"):
            raise WindowBindingError(
                "window_binding_not_declared",
                f"{window_id}: identity needs root or title")
        return effective

    def _lease_context(self):
        # 现有产品租约栅栏:require_product_run 校验 token/到期/竞争。
        try:
            context = self._policy.require_product_run(self._lease_token)
        except ValueError as exc:
            raise WindowBindingError(
                "window_binding_lease_refused", str(exc)) from exc
        if context is None:
            raise WindowBindingError(
                "window_binding_lease_refused",
                "no active product-run lease under this token")
        return context

    def _lifecycle_death(self, instance_id: str) -> int | None:
        for fact in reversed(self._lifecycle):
            if (fact.get("instanceId") == instance_id
                    and fact.get("kind") in {"destroyed", "lost"}):
                return int(fact.get("monotonicMs") or 0)
        return None

    def _lifecycle_alive(self, instance_id: str) -> bool:
        # lost/destroyed 后该 RI 不可证明为运行中窗口的本体(首边界保留,
        # review 1127);identity_changed 是变更点,不伪造死亡。
        return self._lifecycle_death(instance_id) is None

    # ---- identity re-verification over the live backend (§7.2 step 3) ----

    def _reverify_fields(self, binding: _Binding) -> None:
        """重验全部声明字段(§7.2:首绑与复验同一套完整契约)。"""
        identity = binding.identity
        declared_root = identity.get("rootAutomationId")
        declared_title = identity.get("titleRegex")
        if declared_root and binding.bound_root != declared_root:
            raise WindowBindingError(
                "window_binding_lost",
                f"{binding.window_id}: root drifted "
                f"({binding.bound_root!r} != {declared_root!r})")
        if declared_title:
            pattern = _re.compile(declared_title)
            if not isinstance(binding.bound_title, str) or not pattern.search(
                    binding.bound_title):
                raise WindowBindingError(
                    "window_binding_lost",
                    f"{binding.window_id}: title drifted outside the "
                    f"declared identity ({binding.bound_title!r})")


    def _resolve_unique(self, identity: Mapping) -> tuple[str, Mapping] | None:
        """授权域内按声明身份在 live inventory 中唯一解析(§7.1 首次绑定)。"""
        live = self._backend.refresh_inventory()
        candidates = []
        for item in live:
            if item.get("lost"):
                continue  # LOST 实例不参与候选(§7.4)
            if item.get("process_name") != identity.get("processName"):
                continue
            root = identity.get("rootAutomationId")
            if root and item.get("root_control_id") != root:
                continue
            title_regex = identity.get("titleRegex")
            if title_regex:
                try:
                    pattern = _re.compile(title_regex)
                except _re.error:
                    continue
                if not isinstance(item.get("title"), str) or not pattern.search(
                        item["title"]):
                    continue
            elif not root:
                continue
            candidates.append(item)
        if not candidates:
            return None
        if len(candidates) > 1:
            raise WindowBindingError(
                "window_binding_ambiguous",
                f"{len(candidates)} live windows match the declared identity")
        return candidates[0]["native_id"], candidates[0]

    # ---- public protocol ----

    def bind(self, window_id: str, identity: Mapping) -> dict:
        with self._lock:
            self._require_capability()
            declared = self._require_declared(window_id, identity)
            context = self._lease_context()
            prior = self._bindings.get(window_id)
            if prior is not None and prior.state == "LOST":
                raise WindowBindingError(
                    "window_binding_lost",
                    f"{window_id} lost its binding; no declared transition "
                    "allows re-guessing (§7.4)")
            if prior is not None:
                return self.reverify(window_id, prior.token)
            # Candidate resolution inside the lease boundary (product
            # domain owned by the active run).
            with self._policy.authorized_dispatch(self._lease_token):
                resolved = self._resolve_unique(declared)
            if resolved is None:
                raise WindowBindingError(
                    "window_binding_not_found",
                    "no live window matches the declared identity")
            native_id, snapshot = resolved
            recorded_ri = identity.get("_recordedInstanceId")
            if recorded_ri and not self._lifecycle_alive(recorded_ri):
                raise WindowBindingError(
                    "window_binding_lost",
                    "the recorded instance was destroyed or lost in the "
                    "capture timeline; the running window is a new instance")
            for other_id, other in self._bindings.items():
                if (other_id != window_id and other.native_id == native_id
                        and other.state == "BOUND"):
                    raise WindowBindingError(
                        "window_binding_shared",
                        f"{window_id} cannot share the running instance "
                        f"bound to {other_id} without evidence")
            self._revision += 1
            token = "WB-" + secrets.token_hex(16)
            binding = _Binding(
                window_id=window_id, token=token, native_id=native_id,
                identity={**declared,
                          "_recordedInstanceId": recorded_ri},
                bound_pid=snapshot.get("process_id"),
                bound_root=snapshot.get("root_control_id"),
                bound_title=snapshot.get("title"),
                revision=self._revision, run_id=context.run_id)
            self._bindings[window_id] = binding
            self._token_to_window[token] = window_id
            return {"binding_token": token, "bound_native_id": native_id,
                    "revision": binding.revision, "state": "BOUND",
                    "run_id": context.run_id}

    def reverify(self, window_id: str, binding_token: str) -> dict:
        with self._lock:
            self._require_capability()
            self._lease_context()
            binding = self._bindings.get(window_id)
            if binding is None or binding.token != binding_token:
                raise WindowBindingError(
                    "window_binding_not_found",
                    f"no binding for {window_id} under this token")
            with self._policy.authorized_dispatch(self._lease_token):
                live = self._backend.refresh_inventory()
            current = next((item for item in live
                            if item["native_id"] == binding.native_id), None)
            if current is None:
                # HWND gone: destroy/handle-reuse -- a NEW instance, never
                # this binding's recovery (§7.4).
                binding.state = "LOST"
                raise WindowBindingError(
                    "window_binding_lost",
                    f"{window_id}: the bound HWND is gone; a same-identity "
                    "sibling must not substitute")
            if current.get("lost"):
                # lost=True in the inventory: continuity UNKNOWN. Neither
                # HIDDEN state nor an agent-side marker proves an actual
                # SHOW -- recovery happens ONLY through an explicit
                # observed-SHOW fact (protocol.recover_hidden, which the
                # target supplies when it sees the instance re-appear with
                # matching owner/identity). reverify always refuses here
                # (review 1127/0907: unknown continuity is not proven).
                binding.state = "LOST"
                raise WindowBindingError(
                    "window_binding_lost",
                    f"{window_id}: instance lost (continuity unknown); "
                    "recovery requires an explicit observed SHOW fact")
            if int(current.get("process_id") or 0) != binding.bound_pid:
                binding.state = "LOST"
                raise WindowBindingError(
                    "window_binding_lost",
                    f"{window_id}: owner (pid) changed under the same HWND")
            binding.bound_root = current.get("root_control_id")
            binding.bound_title = current.get("title")
            self._reverify_fields(binding)
            recovered = binding.state == "HIDDEN"
            binding.state = "BOUND"
            return {"binding_token": binding.token,
                    "bound_native_id": binding.native_id,
                    "revision": binding.revision, "state": "BOUND",
                    "recovered_from_hidden": recovered}

    def recover_hidden(self, window_id: str, binding_token: str, *,
                       show_fact: Mapping) -> dict:
        """Explicit observed-SHOW fact: the SAME native instance re-appeared
        with the same owner and identity (review 0947/1127 semantics). Any
        field mismatch or a different native_id is refused -- this is a
        declared HIDDEN→BOUND transition for THIS binding, never a re-guess
        or a sibling substitution."""
        with self._lock:
            self._require_capability()
            self._lease_context()
            binding = self._bindings.get(window_id)
            if binding is None or binding.token != binding_token:
                raise WindowBindingError(
                    "window_binding_not_found",
                    f"no binding for {window_id} under this token")
            if show_fact.get("native_id") != binding.native_id:
                raise WindowBindingError(
                    "window_binding_lost",
                    f"{window_id}: the SHOW fact names a different HWND; "
                    "a sibling must not substitute (§7.4)")
            if int(show_fact.get("process_id") or 0) != binding.bound_pid:
                raise WindowBindingError(
                    "window_binding_lost",
                    f"{window_id}: SHOW fact owner differs from the bound "
                    "instance")
            binding.bound_root = show_fact.get("root_control_id",
                                               binding.bound_root)
            binding.bound_title = show_fact.get("title",
                                                binding.bound_title)
            self._reverify_fields(binding)
            was_hidden = binding.state == "HIDDEN"
            binding.state = "BOUND"
            return {"binding_token": binding.token,
                    "bound_native_id": binding.native_id,
                    "revision": binding.revision, "state": "BOUND",
                    "recovered_from_hidden": was_hidden}

    def mark_hidden(self, window_id: str, binding_token: str) -> None:
        """Record a HIDE fact for the binding; only an explicit
        same-instance SHOW recovers it."""
        with self._lock:
            binding = self._bindings.get(window_id)
            if binding is None or binding.token != binding_token:
                raise WindowBindingError(
                    "window_binding_not_found",
                    f"no binding for {window_id} under this token")
            binding.state = "HIDDEN"

    def focus_and_execute(self, window_id: str, binding_token: str, *,
                          dispatch, action: Mapping) -> dict:
        with self._lock:
            self._require_capability()
            self._lease_context()
            binding = self._bindings.get(window_id)
            if binding is None or binding.token != binding_token:
                raise WindowBindingError(
                    "window_binding_not_found",
                    f"no binding for {window_id} under this token")
            # Focus + field re-verification + dispatch inside the SAME
            # authorized (lease-held, GUI-serialized) boundary.
            with self._policy.authorized_dispatch(self._lease_token):
                live = self._backend.refresh_inventory()
                current = next((item for item in live
                                if item["native_id"] == binding.native_id
                                and not item.get("lost")), None)
                if current is None:
                    binding.state = "LOST"
                    raise WindowBindingError(
                        "window_binding_lost",
                        f"{window_id}: bound instance gone before focus; "
                        "action count must be zero")
                focus_result = self._backend.focus(binding.native_id)
                if not focus_result.get("ok"):
                    binding.state = "LOST"
                    raise WindowBindingError(
                        "window_binding_lost",
                        "the bound instance disappeared during focus; "
                        "actions must not execute")
                live_after = self._backend.refresh_inventory()
                after = next((item for item in live_after
                              if item["native_id"] == binding.native_id
                              and not item.get("lost")), None)
                if after is None:
                    binding.state = "LOST"
                    raise WindowBindingError(
                        "window_binding_lost",
                        "the bound instance was replaced during focus; "
                        "action execution count must be zero")
                if int(after.get("process_id") or 0) != binding.bound_pid:
                    binding.state = "LOST"
                    raise WindowBindingError(
                        "window_binding_lost",
                        f"{window_id}: owner changed between focus and "
                        "dispatch; action count must be zero")
                self._reverify_fields(binding)
                binding.bound_root = after.get("root_control_id")
                binding.bound_title = after.get("title")
                self._reverify_fields(binding)
                if (binding.identity.get("rootAutomationId")
                        and after.get("root_control_id")
                        != binding.identity["rootAutomationId"]):
                    binding.state = "LOST"
                    raise WindowBindingError(
                        "window_binding_lost",
                        f"{window_id}: root changed between focus and "
                        "dispatch; action count must be zero")
                if binding.identity.get("titleRegex"):
                    pattern = _re.compile(binding.identity["titleRegex"])
                    title = after.get("title")
                    if not isinstance(title, str) or not pattern.search(title):
                        binding.state = "LOST"
                        raise WindowBindingError(
                            "window_binding_lost",
                            f"{window_id}: title drifted between focus and "
                            "dispatch; action count must be zero")
                declared_visible = binding.identity.get("visible")
                if declared_visible is not None and \
                        bool(after.get("visible")) is not bool(declared_visible):
                    binding.state = "LOST"
                    raise WindowBindingError(
                        "window_binding_lost",
                        f"{window_id}: visibility changed between focus and "
                        "dispatch")
                declared_foreground = binding.identity.get("foreground")
                if declared_foreground is not None and \
                        bool(after.get("foreground")) is not bool(
                            declared_foreground):
                    binding.state = "LOST"
                    raise WindowBindingError(
                        "window_binding_lost",
                        f"{window_id}: foreground changed between focus and "
                        "dispatch")
                return dispatch(
                    action_id=action.get("action_id"),
                    args=action.get("args"),
                    target_ref=action.get("target_ref"),
                    run_token=self._lease_token)

    def release(self, binding_token: str) -> None:
        with self._lock:
            window_id = self._token_to_window.pop(binding_token, None)
            if window_id is not None:
                binding = self._bindings.pop(window_id, None)
                if binding is not None:
                    binding.state = "RETIRED"

    # ---- §7.6 基础 ----

    def step_context(self, *, run_id: str, step_id: str, attempt_id: str,
                     window_id: str, binding_token: str,
                     previous: Mapping | None = None) -> dict:
        with self._lock:
            binding = self._bindings.get(window_id)
            if binding is None or binding.token != binding_token:
                raise WindowBindingError(
                    "window_binding_not_found",
                    f"no binding for {window_id} under this token")
            # run 关联来自实际绑定(调用者自报 foreign run 拒绝)。
            if run_id != binding.run_id:
                raise WindowBindingError(
                    "window_binding_run_mismatch",
                    f"caller run {run_id!r} does not match the binding's "
                    f"actual run {binding.run_id!r}")
            previous_data = dict(previous) if previous is not None else None
            if previous_data is not None:
                if previous_data.get("runId") != binding.run_id:
                    raise WindowBindingError(
                        "window_binding_run_mismatch",
                        "previous state belongs to another run; never reuse "
                        "cross-run state (§7.6)")
            # 转换依据来自实际状态:LOST/HIDDEN 绑定不产生
            # same_window_reverified(没有发生重验)。
            state = binding.state
            if previous_data is None:
                transition = "first_bind"
            elif state == "BOUND":
                transition = ("same_window_reverified"
                              if previous_data.get("windowId") == window_id
                              else "switched_bound_window")
            else:
                transition = f"unverified_state_{state.lower()}"
            context = _StepContext(
                run_id=binding.run_id, step_id=step_id, attempt_id=attempt_id,
                window_id=window_id,
                previous_step_id=(previous_data or {}).get("stepId"),
                previous_attempt_id=(previous_data or {}).get("attemptId"),
                previous_window_id=(previous_data or {}).get("windowId"),
                binding_revision=binding.revision, transition=transition)
            return context.to_readonly_view()
