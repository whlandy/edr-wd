"""本 run 强实例跟踪底座(09 §7.1/§7.4/§8)。

目标:为生产 Windows backend 提供**本 run 原生事实**的实例跟踪 —
生命周期事件订阅、实例代际(generation)、来源/覆盖状态与可检测失效。

诚实覆盖模型(设计约束:不能用 polling 伪称原生覆盖,不能硬编码
supported=true):
- ``LifecycleEventStream``:原生生命周期事件流(WinEvent hook 级)。
  事件连续性可证明时 coverage="strong"。
- 纯枚举源(相邻两次快照):coverage="enumeration-only"。其已知盲区
  (两次枚举之间的短暂窗口)如实列在 capability limitations 里;
  **supported=false** — 快照差分能检测已观测到的复用,但不能证明
  没有漏掉中间的销毁/重建。
- 订阅失败/流中断/枚举错误/观察线程故障 → coverage="unavailable" 或
  "unknown",supported=false(失败后不保留 stale strong)。

代际语义(§7.4):
- 同 handle 一次观察中以**不同关键属性**(pid/root/class)重现 →
  直接换代际(属性替换是复用证据);
- 事件流:DESTROY 后同属性 CREATE → 换代际(事件级复用证据);
- HIDE/SHOW 同实例(同 handle/同 owner/身份)不换代际;
- 快照-only 的同属性重现不可判定(无事件源区分 DESTROY+CREATE 与
  HIDE+SHOW)— 如实限制,绑定重验侧按未知处理(拒绝)。

run 生命周期:经既有 product_policy cleanup hook — end/expiry/异常
都停止跟踪并清理事实;新 owner 的新跟踪不受旧 run 残留影响。

持久/对外状态脱敏:capability/覆盖状态不含 handle/pid/token。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

__all__ = ["RunInstanceTracker", "LifecycleEventStream",
           "ObservationState", "InstanceFact"]

_COVERAGE_STRONG = "strong"
_COVERAGE_ENUMERATION = "enumeration-only"
_COVERAGE_UNKNOWN = "unknown"
_COVERAGE_UNAVAILABLE = "unavailable"

# 关键身份属性:任一不同即视为新实例(§7.4 属性替换证据)。
_IDENTITY_KEYS = ("pid", "root_control_id", "class_name")


@dataclass
class InstanceFact:
    """一个原生实例的本 run 跟踪事实(内部;对外投影脱敏)。"""
    native_id: str
    generation: int
    identity: dict
    identity_changed: bool | None  # None = 不可判定(快照-only 重现)
    last_seen_order: int
    # 权威销毁事实(§7.4):destroy 事件 → alive=False(立即不可执行,
    # 不等 CREATE);代际历史保留以识别后续同 handle 新实例。
    alive: bool = True
    # initial_observed 基线事实(0256/0346:与生命周期 CREATE 分离)。
    observed_baseline: bool = False


@dataclass
class ObservationState:
    """一次观察的快照事实(内部)。"""
    instances: tuple[InstanceFact, ...]
    coverage: str
    degraded: bool
    limitations: tuple[str, ...] = ()


class LifecycleEventStream:
    """原生生命周期事件源(WinEvent hook 级)的替身/生产接口。

    生产 Windows backend 用真实的 SetWinEventHook 订阅实现本协议;
    测试注入受控事件序列。``healthy=False`` 表示订阅/流故障
    (hook 注册失败、回调线程退出等) — 跟踪器据此降级。
    """

    def __init__(self, *, events: Iterable[Mapping] | None = None,
                 healthy: bool = True, failure: str = ""):
        self._events = [dict(e) for e in (events or [])]
        self._healthy = healthy
        self.failure = failure
        self._cursor = 0

    @property
    def healthy(self) -> bool:
        return self._healthy

    def drain_events(self) -> list[dict]:
        """取走自上次调用以来累积的生命周期事件。"""
        batch = self._events[self._cursor:]
        self._cursor = len(self._events)
        return batch


class RunInstanceTracker:
    """本 run 的原生实例跟踪:事件订阅(如有)+ 相邻观察差分。

    - ``start()`` 订阅事件源并建立基线;``stop()`` 反订阅并清理
      (幂等;由 policy cleanup hook 在 run end/expiry/异常时调用);
    - ``observe()`` 取走事件批 + (无事件源时)拉一次快照,更新代际
      事实,返回 ObservationState;
    - ``capability()`` 严格按本 run 事实协商 window_sequence/v1:
      supported 只在 healthy 事件流(连续性可证明)+ 有效代际 +
      覆盖完整时为 True;任何降级事实都会撤销 supported。
    """

    def __init__(self, *, event_source,
                 run_token: str,
                 snapshot_fallback: Callable[[], list[dict]] | None = None):
        self._event_source = event_source
        self._run_token = run_token
        self._snapshot_fallback = snapshot_fallback
        self._lock = threading.RLock()
        self._started = False
        self.stopped = False
        self._facts: dict[str, InstanceFact] = {}
        self._order = 0
        self._coverage = _COVERAGE_UNAVAILABLE
        self._degraded = False
        self._limitations: list[str] = []
        self._faults: list[str] = []
        self._baseline_conflict = False
        self._policy = None  # register_with_policy 时绑定

    # ---- lifecycle ----

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self.stopped = False
            source = self._event_source
            if source is None:
                self._coverage = _COVERAGE_UNAVAILABLE
                self._limitations = ["no run event source configured"]
                return
            healthy = getattr(source, "healthy", None)
            has_stream_protocol = all(
                callable(getattr(source, name, None))
                for name in ("start", "stop", "drain_events"))
            # 生产源协议(duck-typing):start/stop/healthy/drain_events。
            if has_stream_protocol and source is not self:
                # 订阅(0116/0148:tracker.start 必须调 source.start)。
                # 幂等保护:已启动的源不重复 start(重复 start 对真实
                # 线程句柄是非法操作 — Windows 句柄/线程只启动一次)。
                try:
                    if not bool(getattr(source, "started", False)) \
                            and getattr(source, "_thread", None) is None:
                        source.start()
                except Exception as exc:  # noqa: BLE001
                    self._faults.append(f"source start failed: {exc}")
                    self._coverage = _COVERAGE_UNAVAILABLE
                    self._limitations = ["event source start failed"]
                    return
            healthy = getattr(source, "healthy", None)
            if healthy is False:
                self._coverage = _COVERAGE_UNAVAILABLE
                self._faults.append(getattr(source, "failure", "")
                                    or "event source unhealthy")
                self._limitations = ["event source unhealthy at start"]
                return
            if self._is_event_stream_source():
                # 事件流协议源(内存替身或生产 hook 源):强覆盖。
                self._coverage = _COVERAGE_STRONG
                # 基线观察:消费已有事件。
                self.observe()
                return
            # 快照-only 源:诚实声明 enumeration-only,不做强声明。
            self._coverage = _COVERAGE_ENUMERATION
            self._limitations = [
                "snapshot enumeration only: the brief lifetime of a "
                "window between two enumerations is not observable; "
                "DESTROY+CREATE reuse of the same handle cannot be "
                "proven, only suspected"]

    def stop(self) -> dict:
        """停止跟踪并退订源。返回累计结构化结果(幂等:重复调用返回
        同一累计结果,不重复副作用、不丢首错 — 0346 缺口3)。"""
        with self._lock:
            self.stopped = True
            self._started = False
            self._facts.clear()
            self._coverage = _COVERAGE_UNAVAILABLE
            self._limitations = ["tracker stopped"]
            # 生产源退订(0148/0256:同 owner 清理;源层确认退订
            # 退出;结构化失败不吞 — 经 cleanup 透传给 end/expiry)。
            source = self._event_source
            if source is not None:
                source_stop = getattr(source, "stop", None)
                if callable(source_stop):
                    try:
                        self._source_stop_result = source_stop()
                    except Exception as exc:  # noqa: BLE001
                        self._source_stop_result = {
                            "ok": False,
                            "error": f"source stop failed: "
                                     f"{type(exc).__name__}: {exc}"}
            return self._stop_result()

    def register_with_policy(self, policy) -> None:
        """把 stop 挂到既有 run 清理路径(end/expiry/异常同路)。
        追加注册(不是 replace — 不得挤掉 server 已注册的清理)。"""
        self._policy = policy
        policy.register_cleanup_hook(self._on_cleanup)

    def _stop_result(self) -> dict:
        """累计的结构化停止结果(幂等;重复 stop 返回同一首错)。"""
        source_result = getattr(self, "_source_stop_result", None)
        errors = []
        if isinstance(source_result, dict):
            if source_result.get("ok") is False:
                errors.append(f"event source stop failed: "
                              f"{source_result.get('error') or source_result}")
            if source_result.get("worker_exited") is False:
                errors.append("event source worker did not exit on stop")
        if not errors:
            return {"ok": True}
        return {"ok": False, "errors": errors}

    def _on_cleanup(self):
        # 幂等 = 不重复副作用(stop 幂等),但**返回累计首错** —
        # 0346 缺口3:幂等不得把首轮真实失败吞成空错误。
        result = self.stop()
        if isinstance(result, dict) and result.get("ok") is False:
            return {"errors": result.get("errors") or []}
        return {"errors": []}

    def report_watcher_fault(self, reason: str) -> None:
        """观察线程/订阅回调故障:事实失效,能力降级(不保留 stale)。"""
        with self._lock:
            self._faults.append(reason)
            self._coverage = _COVERAGE_UNAVAILABLE
            self._degraded = True
            self._limitations = [f"watcher fault: {reason}"]

    # ---- observation ----

    def observe(self) -> ObservationState:
        with self._lock:
            if not self._started or self.stopped:
                return ObservationState(
                    instances=(), coverage=_COVERAGE_UNAVAILABLE,
                    degraded=True, limitations=("tracker stopped",))
            source = self._event_source
            events = []
            has_stream_protocol = all(
                callable(getattr(source, name, None))
                for name in ("drain_events",))
            if has_stream_protocol:
                stopped = bool(getattr(source, "stopped", False))
                # 0606 缺口1(安全回退收紧):活动 run 内源**不健康即
                # 撤销执行能力** — owner worker 退出 = 不再持续收事件
                # = 强连续覆盖不存在;不要求 failure 字符串非空。
                # (测试的同步 worker mock 用"存活阻塞线程 + stop 释放"
                # 表达活源,不用"立即退出"冒充健康。)
                if not source.healthy:
                    if not stopped:
                        self.report_watcher_fault(
                            source.failure or "event source owner "
                            "exited/unhealthy; continuity lost")
                    self._facts.clear()
                    self._coverage = _COVERAGE_UNAVAILABLE
                    self._degraded = True
                    reason = (source.failure
                              or "event source unhealthy/exited; "
                                 "continuity lost")
                    return ObservationState(
                        instances=(), coverage=_COVERAGE_UNAVAILABLE,
                        degraded=True, limitations=(reason,))
                events = source.drain_events()
            snapshot = self._take_snapshot()
            if snapshot is None and not self._is_event_stream_source():
                self._coverage = _COVERAGE_UNKNOWN
                self._degraded = True
                return ObservationState(
                    instances=tuple(self._facts.values()),
                    coverage=_COVERAGE_UNKNOWN, degraded=True,
                    limitations=("enumeration failed; coverage unknown",))
            self._apply(events, snapshot)
            return ObservationState(
                instances=tuple(self._facts.values()),
                coverage=self._coverage, degraded=self._degraded,
                limitations=tuple(self._limitations))

    def _is_event_stream_source(self) -> bool:
        """事件流协议源:内存替身或生产 hook 源(duck-typing)。"""
        source = self._event_source
        if isinstance(source, LifecycleEventStream):
            return True
        return all(callable(getattr(source, name, None))
                   for name in ("drain_events",))

    def _take_snapshot(self) -> list[dict] | None:
        if self._is_event_stream_source():
            return None  # 事件流模式:代际由事件驱动
        if self._snapshot_fallback is not None:
            try:
                return self._snapshot_fallback()
            except Exception:  # noqa: BLE001 — 覆盖事实获取失败
                return None
        snapshot = getattr(self._event_source, "snapshot", None)
        if callable(snapshot):
            try:
                return snapshot()
            except Exception:  # noqa: BLE001
                return None
        return None

    def _apply(self, events: list[dict],
               snapshot: list[dict] | None) -> None:
        self._order += 1
        for event in events:
            kind = event.get("kind")
            native = str(event.get("native_id"))
            if kind == "initial_observed":
                # 基线(0256/0346):initial_observed — 已存在窗口的
                # 观测事实,不是新 CREATE。已有 tombstone/销毁事实的
                # 实例不得补活(订阅/枚举边界竞争 → 协调边界:先到
                # 事件优先,顺序/完整性未知 → 不声称连续)。
                fields = {k: event.get(k) for k in _IDENTITY_KEYS}
                existing = self._facts.get(native)
                if existing is None:
                    self._facts[native] = InstanceFact(
                        native_id=native, generation=1, identity=fields,
                        identity_changed=None, last_seen_order=self._order,
                        alive=True, observed_baseline=True)
                elif not existing.alive:
                    # 已知销毁(有序 tombstone 在先)→ 不补活;标记
                    # 覆盖冲突(覆盖不完整 → 能力降级由 capability
                    # 按 faults/coverage 判定)。
                    self._baseline_conflict = True
                else:
                    existing.last_seen_order = self._order
                continue
            if kind in ("create", "destroy", "hide", "show"):
                existing = self._facts.get(native)
                if kind == "create":
                    fields = {k: event.get(k) for k in _IDENTITY_KEYS}
                    if existing is not None and not existing.alive:
                        # 死实例复活(DESTROY 后 CREATE,无论重复投递
                        # 几次):恰一代际步进(0148 附加探针:重复
                        # create 不得连跳)。
                        self._facts[native] = InstanceFact(
                            native_id=native,
                            generation=existing.generation + 1,
                            identity=fields, identity_changed=True,
                            last_seen_order=self._order)
                    elif existing is None:
                        self._facts[native] = InstanceFact(
                            native_id=native, generation=1,
                            identity=fields, identity_changed=None,
                            last_seen_order=self._order)
                    # 已 alive 的重复 create:幂等(不换代际)。
                elif kind == "destroy":
                    if existing is not None:
                        existing.last_seen_order = self._order
                        existing.identity_changed = True
                        # 权威销毁:立即不可执行(0148 缺口3)。
                        existing.alive = False
                    else:
                        # 未知实例的 DESTROY(0256/0346 缺口4):留有序
                        # tombstone — 订阅/枚举边界竞争下,先到事件
                        # 不被丢弃;后续基线条目不得补活该实例。
                        self._facts[native] = InstanceFact(
                            native_id=native, generation=1,
                            identity={k: None for k in _IDENTITY_KEYS},
                            identity_changed=True,
                            last_seen_order=self._order, alive=False)
                else:  # hide/show:同实例可见性转换,不换代际。
                    if existing is not None:
                        existing.last_seen_order = self._order
                    elif kind == "show":
                        # 菜单/子菜单 HWND 通常没有自己的 CREATE —
                        # SHOW 事件是本 run 观测到该实例存在的原生证据
                        # (与未知 native 的 create 同信任模型),不落事实
                        # 会被绑定门以"未观测"拒绝(子菜单枚举缺口)。
                        # HIDE 对未知实例仍无意义(无身份可保留),照旧忽略。
                        fields = {k: event.get(k) for k in _IDENTITY_KEYS}
                        self._facts[native] = InstanceFact(
                            native_id=native, generation=1, identity=fields,
                            identity_changed=None,
                            last_seen_order=self._order)
        if snapshot is not None:
            for window in snapshot:
                native = str(window.get("handle"))
                fields = {k: window.get(k) for k in _IDENTITY_KEYS}
                existing = self._facts.get(native)
                if existing is None:
                    self._facts[native] = InstanceFact(
                        native_id=native, generation=1, identity=fields,
                        identity_changed=None,
                        last_seen_order=self._order)
                elif not self._identity_equal(existing.identity, fields):
                    # 属性替换 = 复用证据 → 换代际。
                    self._facts[native] = InstanceFact(
                        native_id=native,
                        generation=existing.generation + 1,
                        identity=fields, identity_changed=True,
                        last_seen_order=self._order)
                else:
                    existing.last_seen_order = self._order

    def _had_destroy(self, native: str, existing: InstanceFact) -> bool:
        """事件流模式:该 handle 在上次观察后是否有 destroy 事实。"""
        if not isinstance(self._event_source, LifecycleEventStream):
            return False
        # create-after-destroy 判定:destroy 事件把 identity_changed
        # 置 True(见 _apply);已有的同代实例 + destroy 事实 = 复用。
        return existing.identity_changed is True

    @staticmethod
    def _identity_equal(a: Mapping, b: Mapping) -> bool:
        return all((a.get(k) or "") == (b.get(k) or "")
                   for k in _IDENTITY_KEYS)

    # ---- 逐实例代际(执行门消费;§7.4) ----

    def instance_generation(self, native_id: str) -> int | None:
        """指定原生实例的本 run 代际;unknown(未观测到)时 None。
        执行门用精确 native_id(不是全局 max)。死实例的代际历史保留
        (供复用识别),但执行门必须另查 alive。"""
        with self._lock:
            fact = self._facts.get(str(native_id))
            return fact.generation if fact is not None else None

    def instance_alive(self, native_id: str) -> bool | None:
        """实例存活事实;unknown(未观测到)时 None。"""
        with self._lock:
            fact = self._facts.get(str(native_id))
            return fact.alive if fact is not None else None

    def sync_and_generation(self, native_id: str) -> int | None:
        """在调用方租约/GUI 串行边界内消费事件批(自动观察)后返回该
        实例的当前代际。流故障/枚举失败/实例已死 → None(未知/不可
        执行 → 门禁拒绝)。"""
        self.observe()
        with self._lock:
            fact = self._facts.get(str(native_id))
            if fact is None or not fact.alive:
                return None
            return fact.generation

    # ---- capability(§8 严格协商) ----

    def capability(self) -> dict:
        with self._lock:
            limitations: list[str] = []
            if not self._started or self.stopped:
                supported = False
                coverage = _COVERAGE_UNAVAILABLE
            elif self._baseline_conflict:
                # 0256/0346 缺口4(0606 修订:实际消费投影):基线竞争
                # (订阅/枚举边界顺序或完整性未知)→ 不声称强实例能力,
                # coverage 降级 unknown + limitations + degraded。
                supported = False
                coverage = _COVERAGE_UNKNOWN
                limitations.append(
                    "baseline conflict: destroy observed before/around "
                    "baseline enumeration; ordering/completeness unknown")
            elif self._coverage == _COVERAGE_STRONG and not self._faults:
                supported = True
                coverage = _COVERAGE_STRONG
            else:
                supported = False
                coverage = (self._coverage
                            if self._coverage in (_COVERAGE_ENUMERATION,
                                                  _COVERAGE_UNKNOWN)
                            else _COVERAGE_UNAVAILABLE)
            # 本 run 代际计数:至少 1(基线);复用证据时随实例代际上升。
            generation = max((fact.generation for fact
                              in self._facts.values()), default=1)
            if self._faults:
                limitations.extend(f"fault: {f}" for f in self._faults)
            elif self._baseline_conflict:
                pass  # limitations 已含冲突条目
            else:
                limitations.extend(self._limitations)
            degraded = (self._degraded or bool(self._faults)
                        or self._baseline_conflict)
            # 脱敏:不含 handle/pid/token(§7.6/普通报告纪律)。
            return {"supported": supported,
                    "coverage": coverage,
                    "generation": generation,
                    "observedInstances": len(self._facts),
                    "limitations": limitations,
                    "degraded": degraded}
