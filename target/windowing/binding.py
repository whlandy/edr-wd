"""目标端窗口绑定 facade(S3;09 号设计 §7/§8)。

复验 1227 修订:绑定协议必须实现在 **target 侧**,复用现有
WindowSession/registry/产品路径归属/真实 dispatcher,而非 agent 侧自建
锁与可变字典。本模块提供 target facade:

- ``WindowBindingFacade``:在现有 product-run 租约内,把 golden 声明的
  窗口(W)绑定到本次运行的真实窗口实例,维护 UNBOUND/BOUND/HIDDEN/LOST/
  RETIRED 状态;候选解析走 WindowSession.registry 的产品域归属;
- 所有变更经 ``product_policy.authorized_dispatch(run_token)``(现有租约/
  GUI 串行栅栏);
- capability 经 target 实际实例跟踪能力声明(本 run 的覆盖,非录制覆盖);
- MCP 工具 (bind_entry_window / focus_bound_window /
  window_binding_state) 由 target/server 在模块加载时注册,复用
  server 的 ``_serialized_product_gui`` 串行化与真实 execute_action;
- dispatcher 在同一租约边界内执行,且执行前由 facade 重验 binding/
  revision/generation/可见性/前台/完整声明。

fake 测试替换 OS 事实层(backend.refresh/focus),**不替代** product_policy
与 WindowSession 边界本身。
"""

from __future__ import annotations

import re as _re
import secrets
import threading
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Callable, Mapping

from .models import WindowIdentity, WindowIdentityError

__all__ = ["WindowBindingFacade", "WindowBindingRefusal"]

_CAPABILITY_NAME = "window_sequence/v1"
_STRONG_COVERAGE = "strong"


class WindowBindingRefusal(ValueError):
    """Stable-code refusal raised by the target binding facade."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


@dataclass
class _WindowBinding:
    window_id: str
    token: str
    native_id: str
    identity: dict
    bound_pid: int
    bound_root: str | None
    bound_title: str | None
    revision: int
    run_id: str
    generation: int
    state: str = "BOUND"
    # 绑定时刻该 native 实例的本 run 代际快照(§7.4;tracker 事实)。
    tracked_generation: int | None = None


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


def _field(obj, key):
    """Read a field from either a dict or an attribute object."""
    try:
        return obj[key]
    except (TypeError, KeyError, IndexError):
        return getattr(obj, key, None)


class WindowBindingFacade:
    """Target-side window binding state machine under the product lease."""

    def __init__(self, *, session, policy, lease_token: str,
                 declared_windows: Mapping[str, Mapping],
                 tracked_generation: int = 0,
                 coverage: str = _STRONG_COVERAGE,
                 supported: bool | None = None,
                 installed_paths: tuple[str, ...] = (),
                 tracker=None):
        """``session`` is the live target WindowSession (owns the backend,
        registry and the GUI mutex); ``policy`` is target.product_policy;
        ``lease_token`` is the ACTIVE run token. ``tracked_generation`` is
        the target's own instance-tracking generation for THIS run; a value
        <= 0 or unknown coverage refuses strong instance capability.
        ``supported`` is the backend's own structured verdict (§8 strict
        negotiation) — when given, it is authoritative and never re-derived
        from coverage or generation."""
        self._session = session
        self._policy = policy
        self._lease_token = lease_token
        self._declared = dict(declared_windows)
        # Golden windows declared for THIS run (registered via the
        # declaration API). Distinct from the profile role identities:
        # a golden W is a reference from the compiled trace, never an
        # authorization — every entry is validated against the installed
        # product domain (review 1744: W vs role must not be conflated,
        # and same-role instances must be addressable as separate W's).
        self._golden_windows: dict[str, dict] = {}
        self._allowed_processes: frozenset[str] = frozenset(
            name.lower()
            for declared in self._declared.values()
            for name in [declared.get("processName", "")] if name
        )
        self._lock = threading.RLock()
        self._installed_paths = tuple(installed_paths)
        # S4 effect baselines (§7.3): baseline token → snapshot; run-scoped.
        self._effect_baselines: dict[str, dict] = {}
        # 本 run 强实例跟踪事实(§8):非 None = 跟踪失效 → 能力撤销。
        self._tracker_fault: str | None = None
        # 本 run 逐实例代际权威(§7.4):focus/reverify/bind 在租约边界内
        # 消费事件批并核对绑定窗口的代际(动态,非 begin 时一次)。
        self._tracker = tracker
        self._bindings: dict[str, _WindowBinding] = {}
        self._token_to_window: dict[str, str] = {}
        self._revision = 0
        self._generation = tracked_generation
        self._coverage = coverage
        self._supported = supported
        self._run_id = None

    # ---- capability (§8: structured, per-run coverage) ----

    def note_tracker_failure(self, reason: str) -> None:
        """跟踪事实失效(观察线程故障/覆盖不足/丢事件):本 run 的强实例
        能力立即撤销(§8:任何降级事实撤销 supported;不保留 stale)。
        后续 bind/reverify 在 GUI 副作用前拒绝。"""
        with self._lock:
            self._tracker_fault = reason
            self._coverage = "unavailable"

    def _tracker_healthy(self) -> bool:
        return not self._tracker_fault

    def capability(self) -> dict:
        # §8 strict negotiation: EVERY necessary condition must hold — the
        # backend's explicit verdict AND strong coverage AND a positive
        # generation (review 1744: supported=true must not override
        # coverage=unknown / generation=0; self-reported true alone never
        # grants the capability). Review 2112/task: a mid-run tracker
        # failure revokes the strong capability immediately.
        if self._supported is not None:
            verdict = self._supported is True
        else:
            verdict = True
        tracker_ok = self._tracker_healthy()
        tracker_generation = 0
        if self._tracker is not None:
            tracker_cap = self._tracker.capability()
            tracker_ok = tracker_ok and tracker_cap.get("supported") is True
            tracker_generation = int(tracker_cap.get("generation") or 0)
        supported = (verdict
                     and tracker_ok
                     and self._coverage == _STRONG_COVERAGE
                     and self._generation > 0)
        # Review 1227/1317: supported must be a STRICT bool — a string
        # 'false' or any non-bool value is refused (no truthiness).
        if not isinstance(supported, bool):
            supported = False
        return {"supported": supported,
                "coverage": (self._coverage if tracker_ok
                             else "unavailable"),
                "generation": max(self._generation, tracker_generation),
                "trackerFault": self._tracker_fault or None}

    def _require_capability(self):
        cap = self.capability()
        if cap["supported"] is not True:
            raise WindowBindingRefusal(
                "window_sequence_capability_unavailable",
                f"target capability is not strictly supported "
                f"(coverage={self._coverage!r}, generation={self._generation})")

    def _lease_context(self):
        try:
            context = self._policy.require_product_run(self._lease_token)
        except ValueError as exc:
            raise WindowBindingRefusal(
                "window_binding_lease_refused", str(exc)) from exc
        if context is None:
            raise WindowBindingRefusal(
                "window_binding_lease_refused",
                "no active product-run lease under this token")
        return context

    def _backend_lock_owned_by(self, binding: _WindowBinding) -> bool:
        """True when the backend's CURRENT OS lock is held on THIS binding's
        exact native instance (handle/pid). A foreign binding's lock is
        never touched by this binding's revocation; a stale/absent lock is
        nothing to revoke."""
        backend = getattr(self._session, "backend", None)
        get_lock = getattr(backend, "get_window_lock", None)
        if not callable(get_lock):
            return False
        try:
            lock = (get_lock().get("lock") or {})
        except Exception:  # noqa: BLE001 — unobservable means not provably ours
            return False
        if not isinstance(lock, dict) or not lock:
            return False
        handle = lock.get("handle") or (lock.get("snapshot") or {}).get("handle")
        pid = lock.get("pid") or (lock.get("snapshot") or {}).get("pid")
        if handle is None or pid is None:
            return False
        return (str(handle) == str(binding.native_id)
                and int(pid) == int(binding.bound_pid))

    def _sync_tracker_generation(self, binding: _WindowBinding) -> None:
        """租约/GUI 串行边界内:消费本 run 事件批(自动观察,不靠调用方
        手工注入)并核对绑定窗口的逐实例代际(§7.4;0116 反例1/2)。
        - 逐实例代际高于绑定时刻快照 = 同 handle 复用 → binding LOST;
        - 跟踪事实失效(流故障/枚举失败 → None)= unknown → 门禁拒绝;
        - 只核对**本绑定窗口**的代际(非全局 max),其他 binding 不受影响。
        不修改 LOSt/HIDDEN 之外的状态语义;LOST 不复活。"""
        if self._tracker is None:
            return
        current = self._tracker.sync_and_generation(str(binding.native_id))
        if current is None:
            binding.state = "LOST"
            self._revoke_execution(binding)
            raise WindowBindingRefusal(
                "window_binding_lost",
                f"{binding.window_id}: this run's instance tracking is "
                "unavailable or the window is unobserved; the binding "
                "cannot be re-verified (§7.4)")
        tracked = getattr(binding, "tracked_generation", None)
        if tracked is None:
            # 绑定时未记录代际(如旧 run 数据/手工构造):按未知处理 →
            # 拒绝,不偷偷采纳(§7.4;不冒充连续)。
            binding.state = "LOST"
            self._revoke_execution(binding)
            raise WindowBindingRefusal(
                "window_binding_lost",
                f"{binding.window_id}: no recorded bind-time generation; "
                "the binding cannot be verified against this run's "
                "tracking")
        if current > tracked:
            binding.state = "LOST"
            self._revoke_execution(binding)
            raise WindowBindingRefusal(
                "window_binding_lost",
                f"{binding.window_id}: the bound native instance was "
                f"destroyed and recreated under the same handle "
                f"(generation {tracked} -> {current}); the old binding "
                "must not be revived (§7.4)")

    def _revoke_execution(self, binding: _WindowBinding) -> None:
        """A failed verification must not leave the binding's execution
        capability alive. Scope (review 1938):
        - the authoritative registry lock is revoked for THIS binding's
          window only (other bindings/runs untouched);
        - the backend OS lock is unlocked ONLY when the current lock is
          held on THIS binding's exact native instance, and ONLY inside
          THIS run's authorized-dispatch boundary (otherwise GuardedBackend
          refuses it and the lock would silently survive);
        - the unlock's structured return is checked — a refusal
          (product_run_conflict) is not silently treated as a completed
          revocation. Best-effort overall: the LOST latch is the contract
          and a failing backend call must not mask the original refusal."""
        binding.state = "LOST"
        try:
            self._session.registry.revoke_lock(binding.native_id)
        except Exception:  # noqa: BLE001 — latch already applied
            pass
        backend = getattr(self._session, "backend", None)
        unlock = getattr(backend, "unlock_window", None)
        if not callable(unlock):
            return
        if not self._backend_lock_owned_by(binding):
            # No lock, an unobservable lock, or another binding's lock:
            # revoking it would break an unrelated binding's execution
            # capability without touching the owner's refusal reason.
            return
        try:
            with self._policy.authorized_dispatch(self._lease_token):
                result = unlock()
        except Exception:  # noqa: BLE001 — preserved above the latch
            return
        # A structured refusal means the unlock did NOT happen; do not
        # pretend the backend capability was revoked.
        if isinstance(result, dict) and result.get("ok") is False:
            return

    def register_golden_windows(self, declarations: Mapping[str, Mapping]) -> dict:
        """Register THIS run's golden window declarations (§7.1: the W ids
        from the compiled trace are references, validated inside the
        installed product domain — never an authorization by themselves).

        Every entry must carry a processName that belongs to the installed
        profile's window-role process set, plus a root or title identity.
        Declaring outside the product domain is refused; re-registration
        of the same W id with a different identity is refused."""
        import re as _re
        with self._lock:
            self._lease_context()
            for window_id, identity in dict(declarations).items():
                if not isinstance(window_id, str) or not window_id:
                    raise WindowBindingRefusal(
                        "window_declaration_invalid",
                        "golden window ids must be non-empty strings")
                if not isinstance(identity, Mapping):
                    raise WindowBindingRefusal(
                        "window_declaration_invalid",
                        f"{window_id}: declaration must be a mapping")
                process = identity.get("processName") or ""
                if process.lower() not in self._allowed_processes:
                    raise WindowBindingRefusal(
                        "window_declaration_outside_product_domain",
                        f"{window_id}: processName {process!r} is not in "
                        "the installed product's authorized window roles")
                if not identity.get("titleRegex") and not identity.get(
                        "rootAutomationId"):
                    raise WindowBindingRefusal(
                        "window_declaration_invalid",
                        f"{window_id}: identity needs root or title")
                regex = identity.get("titleRegex")
                if regex:
                    try:
                        _re.compile(regex)
                    except _re.error as exc:
                        raise WindowBindingRefusal(
                            "window_declaration_invalid",
                            f"{window_id}: invalid titleRegex: {exc}") from exc
                prior = self._golden_windows.get(window_id)
                if prior is not None and dict(prior) != dict(identity):
                    raise WindowBindingRefusal(
                        "window_declaration_conflict",
                        f"{window_id}: re-registration with a different "
                        "identity is refused")
            self._golden_windows.update(
                {k: dict(v) for k, v in dict(declarations).items()})
            return {"ok": True,
                    "declared": sorted(self._golden_windows)}

    def golden_declaration(self, window_id: str) -> dict | None:
        with self._lock:
            return self._golden_windows.get(window_id)

    def _require_declared(self, window_id: str, identity: Mapping) -> dict:
        declared = (self._declared.get(window_id)
                    or self._golden_windows.get(window_id))
        if declared is None:
            raise WindowBindingRefusal(
                "window_binding_not_declared",
                f"{window_id} is not in the golden window declaration")
        for key, value in declared.items():
            if key in identity and identity[key] != value:
                raise WindowBindingRefusal(
                    "window_binding_not_declared",
                    f"{window_id}: caller identity conflicts with declared")
        import re as _re
        for source in (declared, identity):
            regex = source.get("titleRegex")
            if regex:
                try:
                    _re.compile(regex)
                except _re.error as exc:
                    raise WindowBindingRefusal(
                        "window_binding_not_declared",
                        f"{window_id}: invalid titleRegex: {exc}") from exc
        effective = {**declared,
                     **{k: v for k, v in identity.items()
                        if k in ("titleRegex", "rootAutomationId")}}
        if not effective.get("titleRegex") and not effective.get("rootAutomationId"):
            raise WindowBindingRefusal(
                "window_binding_not_declared",
                f"{window_id}: identity needs root or title")
        return effective

    # ---- candidate resolution via the live registry ----

    # ---- product path ownership (§7.1: the lease is not proof that a
    # candidate process belongs to the installed product) ----

    def _product_owned(self, item) -> bool:
        """Check the candidate window's owning process resolves to the
        installed application path. The probe is the session backend's
        ownership seam when present, else psutil at this layer; when the
        owning path cannot be resolved the check fails closed — the lease
        is never ownership proof."""
        pid = _field(item, 'process_id')
        backend = getattr(self._session, "backend", None)
        probe = getattr(backend, "exe_path_for_pid", None)
        if callable(probe):
            actual = probe(pid)
        else:
            try:
                import psutil
                actual = psutil.Process(int(pid)).exe()
            except Exception:
                actual = None
        if not isinstance(actual, str) or not actual:
            raise WindowBindingRefusal(
                "window_binding_ownership_unverifiable",
                f"candidate pid {pid}: owning process path could not be "
                "resolved; the lease is not ownership proof")
        paths = tuple(self._installed_paths or ())
        if not paths:
            # Without a declared installed path, refuse rather than trust
            # name equality (fail closed).
            raise WindowBindingRefusal(
                "window_binding_ownership_unverifiable",
                "no installed product path is available to verify "
                "candidate ownership")
        import ntpath
        normalize = lambda p: ntpath.normcase(ntpath.normpath(p))
        normalized = normalize(actual)
        for path in paths:
            if normalized == normalize(path):
                return True
        raise WindowBindingRefusal(
            "window_binding_unowned_process",
            f"candidate pid {pid} runs {actual!r}, which is not the "
            "installed product path; same-name processes are not "
            "authorized by title or process-name match")

    def _resolve_unique(self, identity: Mapping) -> WindowIdentity:
        candidates = []
        for item in self._session.refresh():
            if _field(item, 'process_name') != identity.get("processName"):
                continue
            root = identity.get("rootAutomationId")
            if root and _field(item, 'root_control_id') != root:
                continue
            title_regex = identity.get("titleRegex")
            if title_regex:
                try:
                    pattern = _re.compile(title_regex)
                except _re.error:
                    continue
                if not pattern.search(_field(item, 'title') or ""):
                    continue
            elif not root:
                continue
            # §7.1: a matching name/title does not authorize an unrelated
            # same-name process — verify the executable path ownership.
            self._product_owned(item)
            candidates.append(item)
        if not candidates:
            raise WindowBindingRefusal(
                "window_binding_not_found",
                "no live window matches the declared identity")
        if len(candidates) > 1:
            # Design 11 D2: a declared ownerWindowId narrows coexisting
            # same-titled windows through the live owner chain — the submenu
            # of the settings dialog is owned by THAT dialog's live HWND.
            # Narrowing requires the owner window to be ALREADY BOUND in
            # this run: no owner binding, no narrowing (fail closed, no
            # guess). The owner's live native handle must still be present
            # in the refreshed inventory; a stale/hidden owner refuses.
            owner_wid = identity.get("ownerWindowId")
            if isinstance(owner_wid, str) and owner_wid:
                owner_binding = self._bindings.get(owner_wid)
                owner_usable = (
                    owner_binding is not None
                    and owner_binding.state == "BOUND")
                if owner_usable:
                    live = self._session.refresh()
                    owner_live = [
                        item for item in live
                        if _field(item, 'native_id') == owner_binding.native_id]
                    owner_handle = _field(
                        owner_live[0], 'handle') if owner_live else None
                    if owner_handle is not None:
                        owned = [
                            item for item in candidates
                            if _field(item, 'owner_handle') == owner_handle]
                        if len(owned) == 1:
                            return owned[0]
            raise WindowBindingRefusal(
                "window_binding_ambiguous",
                f"{len(candidates)} live windows match the declared identity")
        return candidates[0]

    def _structure_narrow(self, candidates: list, identity: Mapping,
                          control_selector: Mapping | None) -> WindowIdentity:
        """父上下文约束下独立识别子菜单（design 11 D2 续）。

        多个同名候选并存时，逐候选在其**自身控件树**内应用声明的
        结构选择器（menu-layout/v1 等）：恰好一个候选内唯一解析 → 该
        候选胜出；零个或多个候选唯一解析 → 拒绝，不选"最新窗口"。
        选择器来自当前步骤的声明（golden），不来自时间邻近或
        causalId。菜单类型/进程/可见性作为候选前置过滤。
        """
        if not isinstance(control_selector, Mapping) \
                or not control_selector:
            raise WindowBindingRefusal(
                "window_binding_ambiguous",
                "structure narrowing requires a declared control selector")
        backend = getattr(self._session, "backend", None)
        dump = getattr(backend, "dump_tree_for_window", None)
        if not callable(dump):
            raise WindowBindingRefusal(
                "window_binding_ambiguous",
                "structure narrowing unavailable: no per-window tree reader")
        try:
            from agent.locator import HybridLocator, LocatorRequest, LocatorError
        except ImportError:  # target-local deployment: vendored locator
            from locator import HybridLocator, LocatorRequest, LocatorError
        locator = HybridLocator()
        resolved_candidates = []
        for item in candidates:
            handle = _field(item, 'handle')
            if handle is None:
                continue
            tree = dump(int(handle), max_depth=15)
            if not isinstance(tree, Mapping) or not tree.get("ok"):
                continue
            controls = tree.get("controls")
            if not isinstance(controls, list):
                continue
            # Normalize to the observation projection: dump rows carry the
            # Mapping rectangle form, which the locator's rect reader
            # (list/tuple only) would drop — the projection converts it to
            # [l, t, r, b] and adds the stable fingerprint/target ids.
            try:
                from .observation import normalize_observed_control
                controls = [normalize_observed_control(c, {}, i + 1)
                            for i, c in enumerate(controls)
                            if isinstance(c, Mapping)]
            except ImportError:
                from windowing.observation import normalize_observed_control
                controls = [normalize_observed_control(c, {}, i + 1)
                            for i, c in enumerate(controls)
                            if isinstance(c, Mapping)]
            try:
                hits = locator.collect_candidates(
                    LocatorRequest("gui.click", {}, dict(control_selector)),
                    {"observation_id": f"bind-narrow-{_field(item, 'native_id')}",
                     "controls": controls})
            except (LocatorError, ValueError):
                hits = ()
            if len(hits) == 1:
                resolved_candidates.append(item)
        if len(resolved_candidates) == 1:
            return resolved_candidates[0]
        raise WindowBindingRefusal(
            "window_binding_ambiguous",
            f"{len(candidates)} live windows match the declared identity; "
            f"structure narrowing resolved {len(resolved_candidates)}")

    def _reverify_fields(self, binding: _WindowBinding, observed: Mapping) -> None:
        """§7.2 step 3: full declared-field re-verification. ANY failure
        latches the binding LOST — a failed verify never leaves the state
        BOUND, and a later attribute recovery never silently revives it
        (review 1845: recovery requires the declared transition) — and
        revokes the binding's authoritative execution capability
        (review 1908: a LOST binding must not keep a live registry lock
        that the plain dispatcher would still honour)."""
        try:
            self._reverify_fields_inner(binding, observed)
        except WindowBindingRefusal:
            binding.state = "LOST"
            self._revoke_execution(binding)
            raise

    def _reverify_fields_inner(self, binding: _WindowBinding,
                               observed: Mapping) -> None:
        identity = binding.identity
        declared_root = identity.get("rootAutomationId")
        declared_title = identity.get("titleRegex")
        if declared_root and _field(observed, 'root_control_id') != declared_root:
            raise WindowBindingRefusal(
                "window_binding_lost",
                f"{binding.window_id}: root drifted "
                f"({_field(observed, 'root_control_id')!r} != {declared_root!r})")
        if declared_title:
            pattern = _re.compile(declared_title)
            if not pattern.search(_field(observed, 'title') or ""):
                raise WindowBindingRefusal(
                    "window_binding_lost",
                    f"{binding.window_id}: title drifted outside the "
                    f"declared identity ({_field(observed, 'title')!r})")
        if _field(observed, 'process_id') != binding.bound_pid:
            raise WindowBindingRefusal(
                "window_binding_lost",
                f"{binding.window_id}: owner (pid) changed "
                f"({binding.bound_pid} -> {_field(observed, 'process_id')})")

    # ---- public protocol ----

    def bind(self, window_id: str, identity: Mapping,
             run_token: str | None = None,
             control_selector: Mapping | None = None) -> dict:
        with self._lock:
            self._require_capability()
            token = run_token or self._lease_token
            declared = self._require_declared(window_id, identity)
            context = self._policy.require_product_run(token)
            if context is None:
                raise WindowBindingRefusal(
                    "window_binding_lease_refused",
                    "no active product-run lease")
            prior = self._bindings.get(window_id)
            if prior is not None and prior.state == "LOST":
                raise WindowBindingRefusal(
                    "window_binding_lost",
                    f"{window_id} lost its binding; no declared transition "
                    "allows re-guessing (§7.4)")
            if prior is not None:
                return self.reverify(window_id, prior.token,
                                     run_token=token)
            with self._policy.authorized_dispatch(token):
                try:
                    resolved = self._resolve_unique(declared)
                except WindowBindingRefusal as exc:
                    # 父上下文约束下独立识别子菜单：多候选且声明了
                    # 结构选择器时，逐候选树内唯一解析（design 11 D2 续）。
                    if "ambiguous" not in str(getattr(exc, "code", "")) \
                            or not control_selector:
                        raise
                    live_now = self._session.refresh()
                    candidates = [
                        item for item in live_now
                        if _field(item, 'process_name')
                        == declared.get("processName")
                        and not _field(item, 'lost')]
                    # A window already BOUND in this run belongs to its own
                    # W and is never another W's instance — offering it as a
                    # candidate lets the structural matcher re-resolve the
                    # SAME window (its tree contains nested submenu rows when
                    # a child popup is open) and end in the shared-instance
                    # refusal instead of the real match. Exclude bound
                    # natives before narrowing.
                    bound_natives = {b.native_id for wid, b in
                                     self._bindings.items()
                                     if wid != window_id
                                     and b.state == "BOUND"}
                    narrowed_candidates = [
                        c for c in candidates
                        if _field(c, 'native_id') not in bound_natives]
                    resolved = self._structure_narrow(
                        narrowed_candidates, declared, control_selector)
            native_id = _field(resolved, 'native_id')
            if self._tracker is not None:
                # §7.4:绑定目标代际必须来自覆盖充分的**本次**实例
                # 跟踪 — 先在租约边界内消费事件批(窗口 SHOW 后
                # create 事实进入跟踪),再校验观测依据。
                self._tracker.sync_and_generation(str(native_id))
            for other_id, other in self._bindings.items():
                if (other_id != window_id and other.native_id == native_id
                        and other.state == "BOUND"):
                    raise WindowBindingRefusal(
                        "window_binding_shared",
                        f"{window_id} cannot share the running instance "
                        f"bound to {other_id}")
            self._revision += 1
            wb_token = "WB-" + secrets.token_hex(16)
            binding = _WindowBinding(
                window_id=window_id, token=wb_token, native_id=native_id,
                identity=declared,
                bound_pid=_field(resolved, 'process_id'),
                bound_root=_field(resolved, 'root_control_id'),
                bound_title=_field(resolved, 'title'),
                revision=self._revision, run_id=context.run_id,
                generation=_field(resolved, 'generation') or 1)
            self._bindings[window_id] = binding
            self._token_to_window[wb_token] = window_id
            # 绑定时刻的逐实例代际快照(§7.4):后续执行门以此比对 —
            # 同 handle 复用(DESTROY→CREATE)后代际上升 → 旧 binding LOST。
            bound_generation = self._generation
            if self._tracker is not None:
                instance_gen = self._tracker.instance_generation(
                    str(native_id))
                if instance_gen is None:
                    # 无观测依据:绑定目标窗口未在本 run 事实中 —
                    # 拒绝(§7.4;不靠伪造事实补)。
                    raise WindowBindingRefusal(
                        "window_binding_not_found",
                        f"{window_id}: the target window is not observed "
                        "by this run's instance tracking; binding without "
                        "observed evidence is refused")
                binding.tracked_generation = instance_gen
                bound_generation = instance_gen
            return {"binding_token": wb_token, "bound_native_id": native_id,
                    "revision": binding.revision, "state": "BOUND",
                    "run_id": context.run_id,
                    "generation": bound_generation}

    def bind_increment(self, window_id: str, identity: Mapping, *,
                       native_id: str, run_token: str | None = None) -> dict:
        """Bind the specific native instance observed as the run's unique
        appeared increment (§7.3: exactly one new candidate → binding
        reference). The caller passes the native observed by the effect
        diff (run-memory only); inventory ambiguity elsewhere is not a
        reason to widen the candidate set — this W's increment WAS the
        unique diff result. Strong-instance evidence still required."""
        import re as _re
        import ntpath as _ntpath
        with self._lock:
            self._require_capability()
            # 调用方 run 令牌必须与本 facade 租约一致(1823:run_token
            # 赋局部未校验的旧缺口;真实授权边界)。
            if run_token is not None and run_token != self._lease_token:
                raise WindowBindingRefusal(
                    "window_binding_lease_refused",
                    "caller run token does not match this run's lease")
            context = self._lease_context()
            token = run_token or self._lease_token
            declared = self._require_declared(window_id, identity)
            if self._bindings.get(window_id) is not None:
                raise WindowBindingRefusal(
                    "window_binding_shared",
                    f"{window_id} already has a binding under this run")
            increment_pid = None
            increment_root = None
            increment_visible = True
            if self._tracker is not None:
                # 同租约边界内消费事件批(强实例事实)。
                self._tracker.observe()
                instance_gen = self._tracker.sync_and_generation(
                    str(native_id))
                if instance_gen is None:
                    raise WindowBindingRefusal(
                        "window_binding_not_found",
                        f"{window_id}: increment instance {native_id} is "
                        "not observed by this run's tracking; binding "
                        "without observed evidence is refused")
                # 增量实例的绑定进程(本 run 跟踪事实;焦点/锁定用)。
                fact = self._tracker._facts.get(str(native_id))
                if fact is not None:
                    increment_pid = (fact.identity or {}).get("pid")
                    increment_root = (fact.identity or {}).get(
                        "root_control_id")
            # 完整身份/归属/活性校验(1823:与旧 bind 同门 — 增量
            # native 必须满足声明身份;已绑定 W 的 native 不得冒充)。
            for other_id, other in self._bindings.items():
                if str(other.native_id) == str(native_id):
                    raise WindowBindingRefusal(
                        "window_binding_shared",
                        f"candidate native is already bound as {other_id}")
            from datetime import datetime as _dt
            declared_root = declared.get("rootAutomationId")
            declared_regex = declared.get("titleRegex")
            live = [item for item in self._session.refresh()
                    if str(_field(item, 'native_id')) == str(native_id)]
            if not live:
                raise WindowBindingRefusal(
                    "window_binding_not_found",
                    f"{window_id}: increment native not in live inventory")
            item = live[0]
            if declared_root and _field(item, 'root_control_id') != \
                    declared_root:
                raise WindowBindingRefusal(
                    "window_binding_identity_mismatch",
                    f"{window_id}: increment native root "
                    f"{_field(item, 'root_control_id')!r} does not match "
                    f"declared {declared_root!r}")
            if declared_regex:
                pattern = _re.compile(declared_regex)
                if not pattern.search(_field(item, 'title') or ""):
                    raise WindowBindingRefusal(
                        "window_binding_identity_mismatch",
                        f"{window_id}: increment native title does not "
                        "match the declared identity")
            process = identity.get("processName")
            if process and _field(item, 'process_name') != process:
                raise WindowBindingRefusal(
                    "window_binding_identity_mismatch",
                    f"{window_id}: increment native process mismatch")
            # 产品路径归属(与旧 bind 同门 — 增量不豁免)。
            self._product_owned(item)
            # 活性可见性(§7.3:增量必须真实出现/可见;基线后
            # destroy/create 复用或覆盖 → 拒绝)。
            if _field(item, 'visible') is False:
                raise WindowBindingRefusal(
                    "window_binding_hidden",
                    f"{window_id}: increment native is not visible")
            increment_visible = True
            # 身份声明仍必须满足(产品域/identity 校验不因增量放宽)。
            self._revision += 1
            wb_token = "WB-" + secrets.token_hex(16)
            binding = _WindowBinding(
                window_id=window_id, token=wb_token, native_id=native_id,
                identity=declared,
                bound_pid=increment_pid,
                bound_root=increment_root
                or declared.get("rootAutomationId"),
                bound_title=_field(item, 'title'),
                revision=self._revision, run_id=context.run_id,
                generation=instance_gen or 1)
            self._bindings[window_id] = binding
            self._token_to_window[wb_token] = window_id
            binding.tracked_generation = instance_gen
            return {"binding_token": wb_token,
                    "bound_native_id": native_id,
                    "revision": binding.revision, "state": "BOUND",
                    "run_id": context.run_id,
                    "generation": instance_gen or 1}

    def reverify(self, window_id: str, binding_token: str, *,
                 run_token: str | None = None) -> dict:
        with self._lock:
            self._require_capability()
            token = run_token or self._lease_token
            binding = self._bindings.get(window_id)
            if binding is None or binding.token != binding_token:
                raise WindowBindingRefusal(
                    "window_binding_not_found",
                    f"no binding for {window_id} under this token")
            if binding.state in ("LOST", "HIDDEN"):
                # A LOST binding stays lost. A HIDDEN binding recovers ONLY
                # through an explicit observed-SHOW fact (recover_hidden) —
                # reverify alone never recovers a hidden window (§7.4,
                # reviews 1127/0907).
                raise WindowBindingRefusal(
                    "window_binding_lost" if binding.state == "LOST"
                    else "window_binding_hidden",
                    f"{window_id}: binding is {binding.state}; no silent "
                    "recovery by reverify or inventory re-discovery")
            with self._policy.authorized_dispatch(token):
                self._sync_tracker_generation(binding)
                live = self._session.refresh()
            current = next((item for item in live
                            if _field(item, 'native_id') == binding.native_id), None)
            if current is None:
                # HWND gone: do NOT update bound_root/bound_title from the
                # missing inventory — the last-known identity is retained
                # for diagnosis (review 1317: evidence must not be
                # fabricated from absent data).
                binding.state = "LOST"
                self._revoke_execution(binding)
                raise WindowBindingRefusal(
                    "window_binding_lost",
                    f"{window_id}: the bound HWND is gone; a same-identity "
                    "sibling must not substitute (§7.4)")
            # Instance tracking generation: if the target's generation has
            # advanced since the bind, the HWND may have been destroyed and
            # recreated (review 0907/1127) — the same attributes don't prove
            # it's the same instance.
            tracked_gen = _field(current, "generation")
            if tracked_gen is not None and int(tracked_gen) != binding.generation:
                binding.state = "LOST"
                self._revoke_execution(binding)
                raise WindowBindingRefusal(
                    "window_binding_lost",
                    f"{window_id}: instance tracking generation advanced "
                    f"({binding.generation} -> {tracked_gen}); the same "
                    "HWND may have been recreated")
            if _field(current, "lost") or _field(current, "visible") is False:
                # Lost or hidden in the live inventory: continuity is
                # unknown, never treated as proven (review 1127/0907).
                binding.state = "LOST"
                self._revoke_execution(binding)
                raise WindowBindingRefusal(
                    "window_binding_lost",
                    f"{window_id}: instance lost/hidden (continuity "
                    "unknown); recovery requires an explicit observed SHOW")
            pid = current["process_id"] if isinstance(current, Mapping) \
                else _field(current, 'process_id')
            if int(pid or 0) != binding.bound_pid:
                binding.state = "LOST"
                self._revoke_execution(binding)
                raise WindowBindingRefusal(
                    "window_binding_lost",
                    f"{window_id}: owner (pid) changed under the same HWND")
            # §7.1/§7.2: a read-only re-verify checks the FULL fact set —
            # product path ownership included — before the binding counts
            # as verified. The lease is not ownership proof. Any failure
            # latches LOST and revokes execution capability.
            try:
                self._product_owned(current)
            except WindowBindingRefusal:
                binding.state = "LOST"
                self._revoke_execution(binding)
                raise
            # Last-known identity is retained for diagnosis until the
            # re-verify SUCCEEDS (review 1845: overwriting root/title
            # before validation let a failed verify report lost but stay
            # BOUND, and a restored attribute silently revive it).
            self._reverify_fields(binding, current)
            binding.bound_root = _field(current, 'root_control_id')
            binding.bound_title = _field(current, 'title')
            binding.state = "BOUND"
            return {"binding_token": binding.token,
                    "bound_native_id": binding.native_id,
                    "revision": binding.revision, "state": "BOUND"}

    def mark_hidden(self, window_id: str, binding_token: str) -> None:
        with self._lock:
            binding = self._bindings.get(window_id)
            if binding is None or binding.token != binding_token:
                raise WindowBindingRefusal(
                    "window_binding_not_found",
                    f"no binding for {window_id}")
            binding.state = "HIDDEN"

    def mark_lost(self, window_id: str, binding_token: str) -> None:
        with self._lock:
            binding = self._bindings.get(window_id)
            if binding is None or binding.token != binding_token:
                raise WindowBindingRefusal(
                    "window_binding_not_found",
                    f"no binding for {window_id}")
            binding.state = "LOST"

    def recover_hidden(self, window_id: str, binding_token: str, *,
                       show_fact: Mapping, run_token: str | None = None) -> dict:
        """Explicit observed-SHOW fact: the SAME native instance re-appeared
        with the same owner and identity. Any field mismatch or a different
        native_id is refused; the binding must be HIDDEN."""
        with self._lock:
            token = run_token or self._lease_token
            binding = self._bindings.get(window_id)
            if binding is None or binding.token != binding_token:
                raise WindowBindingRefusal(
                    "window_binding_not_found",
                    f"no binding for {window_id}")
            if str(show_fact.get("native_id")) != str(binding.native_id):
                raise WindowBindingRefusal(
                    "window_binding_lost",
                    f"{window_id}: SHOW fact names a different HWND; a "
                    "sibling must not substitute (§7.4)")
            if int(show_fact.get("process_id") or 0) != binding.bound_pid:
                raise WindowBindingRefusal(
                    "window_binding_lost",
                    f"{window_id}: SHOW fact owner differs from bound")
            binding.bound_root = show_fact.get(
                "root_control_id", binding.bound_root)
            binding.bound_title = show_fact.get("title", binding.bound_title)
            self._reverify_fields(binding, WindowIdentity(
                session_id="recovery", backend="fake",
                native_id=binding.native_id,
                process_id=binding.bound_pid,
                process_name=binding.identity.get("processName", ""),
                title=binding.bound_title or "",
                rect=(0, 0, 0, 0),
                root_control_id=binding.bound_root or ""))
            if binding.state != "HIDDEN":
                # Recovery is ONLY defined for HIDDEN bindings: a LOST/BOUND
                # binding must not be recovered by a caller-supplied show
                # fact (review 1027/1107: recovery requires the declared
                # HIDDEN transition).
                raise WindowBindingRefusal(
                    "window_binding_lost" if binding.state == "LOST"
                    else "window_binding_not_hidden",
                    f"{window_id}: binding is {binding.state}; recovery is "
                    "only defined for HIDDEN")
            was_hidden = True
            binding.state = "BOUND"
            return {"binding_token": binding.token,
                    "bound_native_id": binding.native_id,
                    "revision": binding.revision, "state": "BOUND",
                    "recovered_from_hidden": was_hidden}

    def focus_and_execute(self, window_id: str, binding_token: str, *,
                          run_token: str | None = None,
                          dispatch_action: Callable[[], dict]) -> dict:
        """聚焦 + 同一租约/GUI 串行边界内重验全部声明字段后 dispatch。"""
        with self._lock:
            try:
                self._require_capability()
            except WindowBindingRefusal:
                # 能力失效(跟踪停止/故障/覆盖未知)→ 本 run 的所有
                # 活跃 binding 锁存 LOST(不复活;其他 run 隔离)。
                for active in self._bindings.values():
                    if active.state == "BOUND":
                        active.state = "LOST"
                raise
            token = run_token or self._lease_token
            binding = self._bindings.get(window_id)
            if binding is None or binding.token != binding_token:
                raise WindowBindingRefusal(
                    "window_binding_not_found",
                    f"no binding for {window_id} under this token")
            if binding.state in ("LOST", "HIDDEN"):
                # §7.4: a LOST/HIDDEN binding is never silently re-verified
                # by focusing; recovery requires the declared transition
                # (recover_hidden) first. Refuse before any focus/dispatch.
                raise WindowBindingRefusal(
                    "window_binding_lost" if binding.state == "LOST"
                    else "window_binding_hidden",
                    f"{window_id}: binding is {binding.state}; focus must "
                    "not silently re-verify it")
            with self._policy.authorized_dispatch(token):
                self._sync_tracker_generation(binding)
                live = self._session.refresh()
                current = next((item for item in live
                                if str(_field(item, 'native_id')) == str(binding.native_id)), None)
                if current is None:
                    binding.state = "LOST"
                    self._revoke_execution(binding)
                    raise WindowBindingRefusal(
                        "window_binding_lost",
                        f"{window_id}: bound instance gone before focus")
                tracked_gen = _field(current, "generation")
                if tracked_gen is not None and int(tracked_gen) != binding.generation:
                    binding.state = "LOST"
                    self._revoke_execution(binding)
                    raise WindowBindingRefusal(
                        "window_binding_lost",
                        f"{window_id}: instance tracking generation advanced")
                # §7.2 step 3: product ownership and binding integrity are
                # verified BEFORE the first focus/connect/activation — an
                # unowned or drifted instance is refused without ever being
                # focused or brought to the foreground (review 1845: the
                # pre-focus activation was a real OS side effect ahead of
                # the refusal). Post-focus and pre-dispatch checks remain.
                # A failure here latches LOST like every other loss path
                # (review 1908: a refused focus must not stay BOUND).
                try:
                    self._product_owned(current)
                except WindowBindingRefusal:
                    binding.state = "LOST"
                    self._revoke_execution(binding)
                    raise
                focus_result = self._session.backend.focus_existing_window(
                    handle=int(binding.native_id), pid=binding.bound_pid,
                    title=binding.bound_title or "",
                    process_name=binding.identity.get("processName", ""))
                if not focus_result.get("ok"):
                    binding.state = "LOST"
                    self._revoke_execution(binding)
                    raise WindowBindingRefusal(
                        "window_binding_lost",
                        "bound instance disappeared during focus; actions "
                        "must not execute")
                # §7.2 step 3: the authoritative precise lock — exact
                # connect to the bound HWND, strict lock with activation,
                # and WindowSession.bind_backend_lock() so the registry's
                # authoritative binding is established. Without this the
                # unified dispatcher's window_lock gate refuses every
                # action (review 1744: focus_existing_window alone only
                # activates; it never establishes the session lock).
                backend = self._session.backend
                exact_connect = getattr(backend, "connect_window_identity",
                                        None)
                if callable(exact_connect):
                    connected = exact_connect(int(binding.native_id),
                                              binding.bound_pid)
                else:
                    connected = backend.connect(
                        pid=binding.bound_pid,
                        title_re="^" + _re.escape(binding.bound_title or "")
                        if binding.bound_title else None,
                        process_name=binding.identity.get("processName", ""))
                if not connected.get("ok"):
                    binding.state = "LOST"
                    self._revoke_execution(binding)
                    raise WindowBindingRefusal(
                        "window_binding_lost",
                        "exact connect to the bound instance failed after "
                        "focus; actions must not execute")
                locked = backend.lock_window(
                    pid=binding.bound_pid,
                    title_re="^" + _re.escape(binding.bound_title or "")
                    if binding.bound_title else None,
                    process_name=binding.identity.get("processName", ""),
                    strict=True, activate=True)
                if not locked.get("ok"):
                    binding.state = "LOST"
                    self._revoke_execution(binding)
                    raise WindowBindingRefusal(
                        "window_binding_lost",
                        "strict lock of the bound instance failed; actions "
                        "must not execute")
                try:
                    locked_identity = self._session.bind_backend_lock()
                except ValueError as exc:
                    binding.state = "LOST"
                    self._revoke_execution(binding)
                    raise WindowBindingRefusal(
                        "window_binding_lost",
                        f"authoritative binding failed after lock: {exc}",
                    ) from exc
                if locked_identity is None or str(_field(
                        locked_identity, 'native_id')) != str(binding.native_id):
                    binding.state = "LOST"
                    self._revoke_execution(binding)
                    raise WindowBindingRefusal(
                        "window_binding_lost",
                        "the locked instance differs from the bound "
                        "instance; actions must not execute")
                live_after = self._session.refresh()
                after = next((item for item in live_after
                              if str(_field(item, 'native_id')) == str(binding.native_id)), None)
                if (after is None
                        or int(_field(after, 'process_id') or 0) != binding.bound_pid
                        or (binding.identity.get("rootAutomationId")
                            and _field(after, 'root_control_id')
                            != binding.identity["rootAutomationId"])):
                    binding.state = "LOST"
                    self._revoke_execution(binding)
                    raise WindowBindingRefusal(
                        "window_binding_lost",
                        "the bound instance was replaced between focus and "
                        "dispatch; action execution count must be zero")
                # §7.2 step 3: visibility and foreground are target safety
                # facts verified on THIS run's observation, not implied by
                # the focus result — an invisible/background instance never
                # reaches the dispatch callback.
                if _field(after, "visible") is False:
                    binding.state = "LOST"
                    self._revoke_execution(binding)
                    raise WindowBindingRefusal(
                        "window_binding_lost",
                        f"{window_id}: bound instance is not visible after "
                        "focus; actions must not execute")
                if _field(after, "foreground") is False:
                    binding.state = "LOST"
                    self._revoke_execution(binding)
                    raise WindowBindingRefusal(
                        "window_binding_lost",
                        f"{window_id}: bound instance is not foreground "
                        "after focus; actions must not execute")
                # §7.2 step 3: product path ownership is re-checked after
                # focus and before dispatch — the lease never proves the
                # running process still belongs to the installed product.
                self._product_owned(after)
                self._reverify_fields(binding, after)
                # Post-focus generation check (§7.2 step 3): a generation
                # advance between bind and dispatch means the HWND may have
                # been destroyed and recreated — the same attributes don't
                # prove it's the same instance.
                after_gen = _field(after, "generation")
                if after_gen is not None and int(after_gen) != binding.generation:
                    binding.state = "LOST"
                    self._revoke_execution(binding)
                    raise WindowBindingRefusal(
                        "window_binding_lost",
                        f"{window_id}: instance tracking generation advanced "
                        f"({binding.generation} -> {after_gen}) between "
                        "focus and dispatch")
                # Lease revocation between focus and dispatch: the policy
                # may have ended the run (end_product_run called from a
                # focus callback). Re-check before dispatch.
                try:
                    self._policy.require_product_run(self._lease_token)
                except (ValueError, Exception):
                    raise WindowBindingRefusal(
                        "window_binding_lease_refused",
                        "the product-run lease was revoked between focus "
                        "and dispatch; actions must not execute")
                # §7.2: the recorded title is a target safety fact — drift
                # from it invalidates the binding even when no titleRegex is
                # declared (root-only declarations cannot detect title drift
                # without this check).
                recorded_title = binding.bound_title
                if recorded_title and _field(after, 'title') != recorded_title:
                    binding.state = "LOST"
                    self._revoke_execution(binding)
                    raise WindowBindingRefusal(
                        "window_binding_lost",
                        f"{window_id}: window title drifted from the "
                        f"recorded fact ({_field(after, 'title')!r} != {recorded_title!r})")
                binding.bound_root = _field(after, 'root_control_id')
                binding.bound_title = _field(after, 'title')
                return dispatch_action()

    # ---- S4:appeared/absent 前后差分(09 §7.3) ----

    def begin_effect_baseline(self, window_id: str, identity: Mapping, *,
                              binding_token: str | None = None) -> dict:
        """动作前基线:在租约内枚举匹配当前身份的候选与(可选)绑定状态,
        返回不透明 baseline token。差分只枚举 — 不聚焦、不激活。"""
        import secrets as _secrets
        with self._lock:
            self._require_capability()
            self._lease_context()
            declared = (self._declared.get(window_id)
                        or self._golden_windows.get(window_id))
            effective = dict(declared or {})
            for key in ("titleRegex", "rootAutomationId", "processName"):
                if key in identity:
                    effective[key] = identity[key]
            if not effective.get("titleRegex") and not effective.get(
                    "rootAutomationId"):
                raise WindowBindingRefusal(
                    "window_effect_baseline_invalid",
                    f"{window_id}: baseline identity needs root or title")
            candidates = []
            candidate_visibility = {}
            for item in self._session.refresh():
                if _field(item, 'process_name') != effective.get("processName"):
                    continue
                root = effective.get("rootAutomationId")
                if root and _field(item, 'root_control_id') != root:
                    continue
                regex = effective.get("titleRegex")
                if regex:
                    try:
                        pattern = _re.compile(regex)
                    except _re.error:
                        continue
                    if not pattern.search(_field(item, 'title') or ""):
                        continue
                native = str(_field(item, 'native_id'))
                candidates.append(native)
                # §7.3:appeared 含"从不可见变可见" — 基线记录基线
                # 时刻可见性(隐形基线候选 show 后按同实例出现评估)。
                candidate_visibility[native] =                     _field(item, 'visible') is not False
            token = "EB-" + _secrets.token_hex(12)
            self._effect_baselines[token] = {
                "window_id": window_id,
                "identity": effective,
                "candidates": candidates,
                "candidate_visibility": candidate_visibility,
                "binding_native": (self._bindings[window_id].native_id
                                   if binding_token and window_id in self._bindings
                                   and self._bindings[window_id].token == binding_token
                                   else None),
            }
            return {"baseline_token": token,
                    "candidate_count": len(candidates),
                    "candidates": candidates}

    def observe_effect_after_action(self, window_id: str, baseline_token: str,
                                    *, effect: Mapping,
                                    binding_token: str | None = None) -> dict:
        """动作后只读差分:对已消失/新增候选做结构化判定。
        appeared:恰一个新候选 → 绑定引用;同实例(HIDDEN→可见)满足
        "same_instance_shown" 且不延展给其他 W;零候选/多候选 →
        timeout/ambiguous。absent:基线候选全部消失 → absent。
        只读轮询语义由调用方在截止时间内驱动;本方法单次采样。"""
        with self._lock:
            self._require_capability()
            self._lease_context()
            baseline = self._effect_baselines.get(baseline_token)
            if baseline is None or baseline["window_id"] != window_id:
                raise WindowBindingRefusal(
                    "window_effect_baseline_unknown",
                    f"{window_id}: unknown or mismatched baseline token")
            kind = effect.get("kind")
            if kind not in ("appeared", "absent"):
                raise WindowBindingRefusal(
                    "window_effect_kind_invalid",
                    f"effect kind must be appeared/absent, got {kind!r}")
            current = []
            for item in self._session.refresh():
                effective = baseline["identity"]
                if _field(item, 'process_name') != effective.get("processName"):
                    continue
                root = effective.get("rootAutomationId")
                if root and _field(item, 'root_control_id') != root:
                    continue
                regex = effective.get("titleRegex")
                if regex:
                    try:
                        pattern = _re.compile(regex)
                    except _re.error:
                        continue
                    if not pattern.search(_field(item, 'title') or ""):
                        continue
                current.append(str(_field(item, 'native_id')))
            before = set(baseline["candidates"])
            after = set(current)
            baseline_visibility = baseline.get("candidate_visibility") or {}
            current_visibility = {}
            for item in self._session.refresh():
                current_visibility[str(_field(item, 'native_id'))] = \
                    _field(item, 'visible') is not False
            # §7.3 appeared = 新候选 或 基线隐形候选变可见。
            appeared = sorted(
                (after - before)
                | {native for native in (after & before)
                   if not baseline_visibility.get(native, True)
                   and current_visibility.get(native, True)})
            disappeared = sorted(
                (before - after)
                | {native for native in (before & after)
                   if baseline_visibility.get(native, True)
                   and not current_visibility.get(native, False)})
            if kind == "absent":
                # absent 针对该绑定的**确切实例**(09§7.3):只有绑定
                # 目标自身消失才成立 — 同身份兄弟消失不顶替(1823:
                # 绑定实例仍可见时,兄弟消失不是本 W 的 absent)。
                bound = self._bindings.get(window_id)
                bound_native = None
                if bound is not None and (
                        not binding_token or
                        bound.token == binding_token):
                    bound_native = str(bound.native_id)
                if bound_native is not None:
                    # 绑定实例"消失" = 从枚举移除 或 变为不可见
                    # (§7.3 absent = 指定绑定不可见/不存在)。
                    bound_gone = (
                        bound_native in disappeared
                        and not current_visibility.get(bound_native, True)) \
                        or bound_native not in current
                    if bound_gone and not appeared:
                        return {"ok": True, "outcome": "absent",
                                "disappeared": [bound_native]}
                    return {"ok": True, "outcome": "present",
                            "candidates": current,
                            "bound_native_still_present":
                                bound_native in current and
                                current_visibility.get(bound_native, True)}
                # 无绑定上下文(声明窗口尚无确切实例):回退到
                # disappeared 非空(基线候选全部消失语义)。
                if disappeared and not appeared:
                    return {"ok": True, "outcome": "absent",
                            "disappeared": disappeared}
                return {"ok": True, "outcome": "present",
                        "candidates": current}
            # appeared
            if appeared and binding_token:
                bound = self._bindings.get(window_id)
                if (bound is not None and bound.token == binding_token
                        and bound.state in ("HIDDEN", "BOUND")
                        and appeared == [str(bound.native_id)]
                        and bound.native_id not in before):
                    # 同实例重新显示:只对本 binding 的 W 有意义。
                    bound.state = "BOUND"
                    return {"ok": True, "outcome": "same_instance_shown",
                            "native_id": bound.native_id}
            if len(appeared) == 1:
                return {"ok": True, "outcome": "appeared",
                        "native_id": appeared[0]}
            if len(appeared) > 1:
                return {"ok": False, "outcome": "ambiguous",
                        "appeared": appeared}
            return {"ok": False, "outcome": "timeout",
                    "appeared": [], "candidates": current}

    def release(self, binding_token: str) -> None:
        with self._lock:
            window_id = self._token_to_window.pop(binding_token, None)
            if window_id is not None:
                binding = self._bindings.pop(window_id, None)
                if binding is not None:
                    binding.state = "RETIRED"

    def clear_run_state(self) -> None:
        """Drop every binding, token reference and golden declaration for
        THIS run. Called from the run's cleanup hook on end/expiry/fault —
        clearing memory is not an automatic-recovery mechanism; LOST/RETIRED
        bindings are never resurrected by it."""
        with self._lock:
            for binding in self._bindings.values():
                binding.state = "RETIRED"
            self._bindings.clear()
            self._token_to_window.clear()
            self._golden_windows.clear()
            self._effect_baselines.clear()

    def step_context(self, *, run_id: str, step_id: str, attempt_id: str,
                     window_id: str, binding_token: str,
                     previous: Mapping | None = None) -> dict:
        with self._lock:
            binding = self._bindings.get(window_id)
            if binding is None or binding.token != binding_token:
                raise WindowBindingRefusal(
                    "window_binding_not_found",
                    f"no binding for {window_id} under this token")
            if run_id != binding.run_id:
                raise WindowBindingRefusal(
                    "window_binding_run_mismatch",
                    f"caller run {run_id!r} does not match the binding's "
                    f"actual run {binding.run_id!r}")
            previous_data = dict(previous) if previous is not None else None
            if previous_data is not None:
                if previous_data.get("runId") != binding.run_id:
                    raise WindowBindingRefusal(
                        "window_binding_run_mismatch",
                        "previous state belongs to another run; never reuse "
                        "cross-run state (§7.6)")
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
