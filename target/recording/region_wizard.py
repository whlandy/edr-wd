"""区域断言向导（A轨）：「查找什么 / 怎样满足」表单 → RegionAssertionDraft v2.

设计依据 06 §2.4–§2.5、§3、§4：

- 表单产出契约 shape 的 draft dict，校验**只调用 B 轨的
  ``region_assertion.validate_region_draft``（经 ``build_region_draft``）**，
  不自建第二套 schema；
- 预览只读匹配结果，比较器**只调用 B 轨的 ``evaluate_region_conditions``**
  （05 §3.3：禁止第二套语义不同的比较器），喂冻结观测样本；
- 界面持续显示完整中文摘要（B 的 ``render_region_summary`` 经 build 计算）；
- 内容时间默认「每次检查当下最近 5 秒」、观察期默认 60 秒首次匹配通过，
  两个阈值独立可编辑（秒/分钟输入），绝不写死 10 秒；
- 存在性两种模式可表达：「存在」＝first_match 首次匹配即通过；「不存在」＝
  absent_full_period 整个观察期都不出现才通过；
- 绑定只接受显式给定的 (sequence, causalId)（06 §2：不允许保存时静默绑定
  当前最后一个动作）；没有候选数据源时保持「独立检查点」。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

try:  # target-local deployment keeps modules importable standalone
    from .region_assertion import (
        KIND, SCHEMA_VERSION, RegionDraftError,
        build_region_draft, evaluate_region_conditions,
    )
except ImportError:  # pragma: no cover - flat deployment layout
    from region_assertion import (  # type: ignore[no-redef]
        KIND, SCHEMA_VERSION, RegionDraftError,
        build_region_draft, evaluate_region_conditions,
    )

DEFAULT_RECENT_SECONDS = 5.0     # 内容时间默认阈值（06 §4）
DEFAULT_DEADLINE_SECONDS = 60.0  # 观察期默认时长（06 §4）

INDEPENDENT_CHECKPOINT_LABEL = "独立检查点"

FIELD_LABELS = {
    "name": "名称", "text": "文字", "value": "值",
    "checked": "勾选状态", "enabled": "可用状态", "time": "时间",
}
OPERATOR_LABELS = {
    "equals": "等于", "contains": "包含", "not_contains": "不包含",
    "regex": "匹配正则", "bool_equals": "状态等于", "exists": "可读取（存在）",
    "not_exists": "不存在/不可读取", "time_recent": "位于每次检查当下最近N秒",
    "count": "行数",
}
OPERATORS_BY_FIELD = {
    "name": ("contains", "equals", "not_contains", "regex", "exists", "not_exists"),
    "text": ("contains", "equals", "not_contains", "regex", "exists", "not_exists"),
    "value": ("equals", "contains", "not_contains", "regex", "exists", "not_exists"),
    "checked": ("bool_equals", "exists", "not_exists"),
    "enabled": ("bool_equals", "exists", "not_exists"),
    "time": ("time_recent", "exists", "not_exists"),
}
BOOL_FIELDS = frozenset({"checked", "enabled"})
# controlType → 默认读取字段（用户可在表单里改）。
DEFAULT_FIELD_BY_CONTROL_TYPE = {
    "Edit": "value", "CheckBox": "checked", "ToggleButton": "checked",
    "RadioButton": "checked", "DateTime": "time", "Custom": "text",
}


def parse_duration_seconds(text: Any, *, default: float) -> float:
    """Parse a user-entered duration: ``90``、``90s``、``1.5m``、``2分30秒``.

    无单位数字按秒；「分/分钟/m/min」按分钟。解析失败返回 default
    （表单提示未生效，不静默造数）。
    """
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return float(text) if text > 0 else default
    raw = str(text or "").strip().lower()
    if not raw:
        return default
    total = 0.0
    matched = False
    number = ""
    unit = ""

    def flush() -> None:
        nonlocal total, matched, number, unit
        if number:
            total += _unit_seconds(number, unit or "s")
            matched = True
        number, unit = "", ""

    for char in raw:
        if char.isdigit() or char == ".":
            if unit:
                flush()
            number += char
        elif char in "s秒" and number:
            flush()
        elif char in "m分" and number:
            unit = "m"
        elif char == "钟":
            continue
        elif char.strip():
            flush()          # 未知字符截断当前 token（如「90x」按 90 秒）
        # 空白字符直接跳过
    flush()
    return total if matched and total > 0 else default


def _unit_seconds(number: str, unit: str) -> float:
    try:
        value = float(number)
    except ValueError:
        return 0.0
    return value * 60.0 if unit == "m" else value


class RegionWizardForm:
    """One wizard editing session over the frozen region selection.

    纯逻辑、无 Tk：字段配置以简单 dict 传入，产出经 B 校验的 v2 draft；
    预览调用 B 的共享比较器。所有失败以 :class:`RegionDraftError` 抛出
    （稳定码 ``region_draft_invalid``），表单保留重试。
    """

    def __init__(self, selection: Mapping[str, Any]) -> None:
        self._selection = dict(selection or {})
        self._window = _clean_window(self._selection.get("window") or {})
        container = self._selection.get("container")
        self.target_mode = "container_rows" if container else "fixed_components"
        self.container_identity = dict((container or {}).get("identity") or {})
        # 组件字段配置：默认按 controlType 推断读取字段，可在表单中修改。
        self.components: list[dict[str, Any]] = []
        for index, component in enumerate(self._selection.get("components") or []):
            identity = dict(component.get("identity") or {})
            control_type = str(component.get("controlType") or "")
            default_field = DEFAULT_FIELD_BY_CONTROL_TYPE.get(control_type, "text")
            self.components.append({
                "key": str(component.get("key") or f"c{index}"),
                "name": str(component.get("name") or "（未命名组件）"),
                "controlType": control_type,
                "identity": identity,
                "field": default_field,
                "sampleValue": component.get("sampleValue"),
                "readStatus": str(component.get("readStatus") or "unknown"),
                "readError": component.get("readError"),
            })
        # 列表容器行的字段读取者：容器模式下组件即示例行的单元格读取者。
        # P2e（审查终局）：去掉构造器静默轮换（text→name/value/time）——
        # 同 field 不同身份合法（P2d），读取语义由组件身份锚定；无界 while
        # 挂死根因随之消灭。

    # ── 表单描述（供 Tk 渲染与离线检查） ────────────────────────────

    def describe(self) -> dict[str, Any]:
        return {
            "targetMode": self.target_mode,
            "quantifierOptions": ["any", "all"],
            "quantifierLabels": {
                "any": "任意一行满足" if self.target_mode == "container_rows"
                else "任一组件满足",
                "all": "所有行满足" if self.target_mode == "container_rows"
                else "所有组件满足",
            },
            "fields": dict(FIELD_LABELS),
            "operatorsByField": {k: list(v) for k, v in OPERATORS_BY_FIELD.items()},
            "components": [
                {"key": c["key"], "name": c["name"], "controlType": c["controlType"],
                 "field": c["field"], "readStatus": c["readStatus"],
                 "contentText": ("无法读取" if c["readStatus"] != "ok"
                                 else _text(c["sampleValue"]))}
                for c in self.components
            ],
            "window": dict(self._window),
            "defaults": {
                "recentSeconds": DEFAULT_RECENT_SECONDS,
                "deadlineSeconds": DEFAULT_DEADLINE_SECONDS,
                "observationMode": "first_match",
                "quantifier": "any",
                "conditionOp": "all",
            },
        }

    # ── 草稿构建（B 校验是唯一权威） ────────────────────────────────

    def build_draft(
        self, *,
        quantifier: str = "any",
        condition_op: str = "all",
        leaves: Sequence[Mapping[str, Any]] = (),
        count: Mapping[str, Any] | None = None,
        recent_seconds: float | str = DEFAULT_RECENT_SECONDS,
        content_time_enabled: bool = True,
        deadline_seconds: float | str = DEFAULT_DEADLINE_SECONDS,
        observation_mode: str = "first_match",
        binding: Mapping[str, Any] | tuple[int, str] | None = None,
        draft_id: str | None = None,
        observed_at: str | None = None,
    ) -> dict:
        """按契约 shape 构建并校验 draft；失败抛 RegionDraftError。"""
        window = dict(self._window)
        if not window.get("processName") or not window.get("title"):
            raise RegionDraftError(
                "目标窗口身份缺失（processName/title），无法保存区域断言",
                path="draft.window",
            )
        children = [dict(leaf) for leaf in leaves]
        if count is not None:
            children.append({
                "operator": "count", "field": "rows", "count": dict(count),
            })
        if not children:
            raise RegionDraftError(
                "请至少配置一个「查找什么」条件", path="draft.conditions")
        recent = parse_duration_seconds(recent_seconds, default=DEFAULT_RECENT_SECONDS)
        deadline = parse_duration_seconds(
            deadline_seconds, default=DEFAULT_DEADLINE_SECONDS)
        has_binding = binding is not None
        return build_region_draft(
            window=window,
            target_mode=self.target_mode,
            container=self.container_identity or None,
            components=[
                {
                    "key": component["key"],
                    "field": component["field"],
                    "identity": component["identity"],
                    # sampleValue 只携带真实读取值；读取失败不补空值。
                    **({"sampleValue": component["sampleValue"]}
                       if component["readStatus"] == "ok" else {}),
                }
                for component in self.components
            ],
            quantifier=quantifier,
            conditions={"op": condition_op, "children": children},
            content_time={"anchor": "each_check_now", "recentSeconds": recent,
                          "enabled": bool(content_time_enabled)},
            observation={
                "mode": observation_mode,
                "deadlineSeconds": deadline,
                "start": "after_bound_action" if has_binding else "immediate",
            },
            binding=binding,
            draft_id=draft_id,
            observed_at=observed_at,
            read_status=_overall_read_status(self.components),
        )

    # ── 预览（只读，共用 B 的比较器） ───────────────────────────────

    def preview(
        self, draft: Mapping[str, Any], *,
        now_wall: datetime | None = None,
    ) -> dict:
        """只读匹配结果：标明匹配行与未满足条件；绝不落事件、绝不点击。"""
        conditions = draft.get("conditions") or {}
        content_time = draft.get("contentTime") or {}
        recent = float(content_time.get("recentSeconds") or DEFAULT_RECENT_SECONDS)
        now = now_wall or datetime.now(timezone.utc)
        rows = self.frozen_rows()
        return evaluate_region_conditions(
            conditions, rows, now, recent,
            quantifier=str(draft.get("quantifier") or "any"),
        )

    def frozen_rows(self) -> list[dict[str, Any]]:
        """冻结观测样本行：同帧合取的行快照（缺读字段如实为 None）。"""
        values: dict[str, Any] = {}
        # P2c：双通道——values 按 field（条件查找契约）；componentValues
        # 按组件稳定键（身份与读取属性独立映射，不同身份同 field 样本
        # 不互相覆盖）。
        component_values: dict[str, Any] = {}
        for component in self.components:
            sample = (component["sampleValue"]
                      if component["readStatus"] == "ok" else None)
            values[component["field"]] = sample
            component_values[str(component.get("key")
                                 or component["field"])] = sample
        return [{"rowIndex": 0, "values": values,
                 "componentValues": component_values}]


def _overall_read_status(components: Sequence[Mapping[str, Any]]) -> str:
    statuses = [str(c.get("readStatus") or "unknown") for c in components]
    if statuses and all(status == "ok" for status in statuses):
        return "ok"
    if "error" in statuses:
        return "error"
    return "unknown"


def _clean_window(window: Mapping[str, Any]) -> dict[str, Any]:
    """draft.window 只保留契约字段；rootAutomationId 等证据字段不进入。"""
    return {
        key: window[key]
        for key in ("processName", "title", "handle", "pid")
        if window.get(key) is not None
    }


def _next_field(field: str) -> str:
    order = [item for item in ("text", "name", "value", "time")
             if item in FIELD_LABELS]
    if field not in order:
        return "text"
    index = order.index(field)
    return order[(index + 1) % len(order)]


def _text(value: Any) -> str:
    return "" if value is None else str(value)


class RegionWizardDialog:
    """Tk 向导窗（live desktop；离线用假件驱动）。

    打开时销毁遮罩；保存先在本地构建 draft（B 校验），再经
    ``on_save(draft)`` 发送 region_save 请求——**收到服务端成功回执才由
    面板关闭本窗**；失败保留表单并显示错误。Esc/取消走 ``on_cancel``。
    """

    def __init__(
        self,
        tk_modules: Mapping[str, Any],
        selection: Mapping[str, Any],
        *,
        on_save: Callable[[dict], str],
        on_cancel: Callable[[], None],
    ) -> None:
        self._tk = tk_modules["tk"]
        self._ttk = tk_modules["ttk"]
        self._messagebox = tk_modules.get("messagebox")
        self._parent = tk_modules.get("parent")
        import sys as _sys
        import traceback as _tb
        print("[wizard-ctor] form-start", file=_sys.stderr, flush=True)
        try:
            self._form = RegionWizardForm(selection)
        except Exception as _fexc:
            # F79（F62 纪律）：阻塞 vs 异常分辨——form 调用在 try 外，
            # 异常上抛=render 中断=表象同阻塞；此处钉死真实原因。
            print(f"[wizard-ctor] form-EXC {type(_fexc).__name__}: {_fexc}",
                  file=_sys.stderr, flush=True)
            _tb.print_exc(file=_sys.stderr)
            raise
        print("[wizard-ctor] form-done", file=_sys.stderr, flush=True)
        self._on_save = on_save
        self._on_cancel = on_cancel
        self.dialog = None
        self._widgets: dict[str, Any] = {}
        self._save_request_id: str | None = None
        self._build()

    # ── 构建 ────────────────────────────────────────────────────────

    def _build(self) -> None:  # pragma: no cover - live desktop
        import sys as _sys
        tk, ttk = self._tk, self._ttk
        print("[wizard-build] describe-start", file=_sys.stderr, flush=True)
        description = self._form.describe()
        print("[wizard-build] describe-done", file=_sys.stderr, flush=True)
        print("[wizard-build] toplevel-start", file=_sys.stderr, flush=True)
        dialog = tk.Toplevel(self._parent)
        print("[wizard-build] toplevel-done", file=_sys.stderr, flush=True)
        self.dialog = dialog
        dialog.title("区域断言向导")
        dialog.attributes("-topmost", True)
        dialog.protocol("WM_DELETE_WINDOW", self.cancel)
        dialog.bind("<Escape>", lambda _event: self.cancel())

        row = 0
        window = description.get("window") or {}
        ttk.Label(dialog, text=(
            f"目标窗口：{window.get('processName') or '未知进程'} · "
            f"{window.get('title') or '未知标题'}"
        )).grid(row=row, column=0, columnspan=4, sticky="w")
        row += 1

        ttk.Label(dialog, text="所选目标（管理选择，不入业务事件）").grid(
            row=row, column=0, columnspan=4, sticky="w")
        row += 1
        for component in (description.get("components") or []):
            ttk.Label(dialog, text=(
                f"· {component['controlType']} · {component['name']} · "
                f"内容：{component['contentText']}"
            )).grid(row=row, column=0, columnspan=4, sticky="w")
            row += 1
        if self._form.container_identity:
            ttk.Label(dialog, text=(
                f"· 整个容器：{self._form.container_identity.get('controlType') or '?'}"
                f" · {self._form.container_identity.get('name') or '?'}"
            )).grid(row=row, column=0, columnspan=4, sticky="w")
            row += 1

        quantifier_var = tk.StringVar()
        quantifier_box = ttk.Combobox(
            dialog, state="readonly", textvariable=quantifier_var,
            values=[description["quantifierLabels"][q]
                    for q in description["quantifierOptions"]])
        quantifier_box.current(0)
        quantifier_by_label = {
            description["quantifierLabels"][q]: q
            for q in description["quantifierOptions"]
        }
        ttk.Label(dialog, text="匹配范围").grid(row=row, column=0, sticky="w")
        quantifier_box.grid(row=row, column=1, sticky="w")
        row += 1

        op_var = tk.StringVar()
        op_box = ttk.Combobox(dialog, state="readonly", textvariable=op_var,
                              values=["同时满足（全部）", "任一满足（任意）"])
        op_box.current(0)
        ttk.Label(dialog, text="条件组合").grid(row=row, column=0, sticky="w")
        op_box.grid(row=row, column=1, sticky="w")
        row += 1

        # 每个所选组件一行：读取字段 / 判断方式 / 期望值
        component_rows: list[dict[str, Any]] = []
        field_labels = description["fields"]
        for component_index, component in enumerate(description.get("components") or []):
            field_var = tk.StringVar()
            field_box = ttk.Combobox(
                dialog, state="readonly", textvariable=field_var,
                values=[f"{v} {FIELD_LABELS[v]}" for v in OPERATORS_BY_FIELD])
            field_box.current(
                list(OPERATORS_BY_FIELD).index(component["field"]))
            operator_var = tk.StringVar()
            operator_box = ttk.Combobox(
                dialog, state="readonly", textvariable=operator_var,
                values=[OPERATOR_LABELS[o] for o in
                        OPERATORS_BY_FIELD[component["field"]]])
            operator_box.current(0)
            expected_entry = ttk.Entry(dialog, width=24)
            expected_entry.insert(0, component.get("contentText")
                                  if component["readStatus"] == "ok" else "")
            bool_var = tk.StringVar(value="")
            bool_box = ttk.Combobox(dialog, state="readonly", width=21,
                                    textvariable=bool_var, values=["是", "否"])
            sample = self._form.components[component_index].get("sampleValue")
            if (component["field"] in BOOL_FIELDS and component["readStatus"] == "ok"
                    and isinstance(sample, bool)):
                bool_var.set("是" if sample else "否")
            ttk.Label(dialog, text=component["name"]).grid(
                row=row, column=0, sticky="w")
            field_box.grid(row=row, column=1, sticky="w")
            # P2b：字段变更→operator 候选同步（readonly 下拉仍可选）
            def _on_field_change(_evt=None, _fb=field_box, _ob=operator_box,
                                 _fallback=component["field"], _entry=expected_entry,
                                 _bool_box=bool_box, _row=row):
                _f = (_fb.get().split(" ")[0] if _fb.get()
                      else _fallback)
                _ob["values"] = [OPERATOR_LABELS[o]
                                 for o in OPERATORS_BY_FIELD[_f]]
                _ob.current(0)
                _entry.grid_remove() if _f in BOOL_FIELDS else _bool_box.grid_remove()
                (_bool_box if _f in BOOL_FIELDS else _entry).grid(
                    row=_row, column=3, sticky="w")
            field_box.bind("<<ComboboxSelected>>", _on_field_change)
            operator_box.grid(row=row, column=2, sticky="w")
            (bool_box if component["field"] in BOOL_FIELDS else expected_entry).grid(
                row=row, column=3, sticky="w")
            component_rows.append({
                "field_var": field_var, "operator_var": operator_var,
                "expected_entry": expected_entry, "bool_var": bool_var,
                "bool_box": bool_box, "field": component["field"],
            })
            row += 1

        count_enabled = tk.BooleanVar(value=False)
        count_op_var = tk.StringVar()
        count_op_box = ttk.Combobox(dialog, state="readonly",
                                    textvariable=count_op_var,
                                    values=["等于", "至少", "至多"])
        count_op_box.current(1)
        count_value = ttk.Entry(dialog, width=6)
        ttk.Checkbutton(dialog, text="数量条件（满足条件的行数）",
                        variable=count_enabled).grid(row=row, column=0, sticky="w")
        count_op_box.grid(row=row, column=1, sticky="w")
        count_value.grid(row=row, column=2, sticky="w")
        row += 1

        recent_enabled = tk.BooleanVar(value=True)
        recent_entry = ttk.Entry(dialog, width=10)
        recent_entry.insert(0, f"{DEFAULT_RECENT_SECONDS:g}")
        ttk.Checkbutton(dialog, text="内容时间：每次检查当下最近（秒）",
                        variable=recent_enabled).grid(row=row, column=0, sticky="w")
        recent_entry.grid(row=row, column=1, sticky="w")
        row += 1

        mode_var = tk.StringVar()
        mode_box = ttk.Combobox(dialog, state="readonly", textvariable=mode_var,
                                values=["存在：首次匹配即通过",
                                        "不存在：整个观察期都不出现才通过"])
        mode_box.current(0)
        ttk.Label(dialog, text="存在性").grid(row=row, column=0, sticky="w")
        mode_box.grid(row=row, column=1, sticky="w")
        deadline_entry = ttk.Entry(dialog, width=10)
        deadline_entry.insert(0, f"{DEFAULT_DEADLINE_SECONDS:g}")
        ttk.Label(dialog, text="观察最多（秒）").grid(row=row, column=2, sticky="w")
        deadline_entry.grid(row=row, column=3, sticky="w")
        row += 1

        # 动作绑定候选（06 §2）：默认独立检查点；候选由面板按 recent_actions
        # 回执填充（apply_recent_actions），用户显式选中某动作才绑定。
        binding_var = tk.StringVar(value=INDEPENDENT_CHECKPOINT_LABEL)
        binding_box = ttk.Combobox(
            dialog, state="readonly", textvariable=binding_var,
            values=[INDEPENDENT_CHECKPOINT_LABEL])
        binding_box.bind(
            "<<ComboboxSelected>>", lambda _event: self.refresh_summary())
        binding_by_label: dict[str, Any] = {INDEPENDENT_CHECKPOINT_LABEL: None}
        binding_status = tk.StringVar(
            value="绑定：独立检查点（正在获取最近动作…）")
        ttk.Label(dialog, text="动作绑定").grid(row=row, column=0, sticky="w")
        binding_box.grid(row=row, column=1, columnspan=2, sticky="w")
        row += 1
        ttk.Label(dialog, textvariable=binding_status, foreground="#555",
                  wraplength=460, justify="left").grid(
            row=row, column=0, columnspan=4, sticky="w")
        row += 1

        summary_var = tk.StringVar(value="配置条件后显示中文摘要")
        summary_label = ttk.Label(dialog, textvariable=summary_var,
                                  foreground="#333", wraplength=460,
                                  justify="left")
        summary_label.grid(row=row, column=0, columnspan=4, sticky="w")
        row += 1

        preview_label = ttk.Label(dialog, text="", foreground="#1b5e20",
                                  wraplength=460, justify="left")
        preview_label.grid(row=row, column=0, columnspan=4, sticky="w")
        row += 1

        submit_button = ttk.Button(dialog, text="保存", command=self.save)
        import sys as _sys1  # 探针作用域自足（F97 _sys 在后段）
        # F101（监督方案）：事件分发探针——Button-1 行现而 command 未触发=
        # 绑定/状态问题；两行皆无=点击未达 Tk（输入桌面问题，与
        # input-desktop-reattach ok=False 吻合）。
        try:
            submit_button.bind(
                "<Button-1>", lambda _e: print(
                    "[save-btn] Button-1 event", file=_sys.stderr,
                    flush=True))
            submit_button.bind(
                "<ButtonRelease-1>", lambda _e: print(
                    "[save-btn] ButtonRelease-1 event", file=_sys.stderr,
                    flush=True))
        except Exception:
            pass  # 离线假件无 bind（live-only 探针）
        submit_button.grid(row=row, column=1, sticky="e")
        import sys as _sys2  # 绑定 lambda 作用域自足

        # F97（监督方案）：Tk 自证坐标——免疫 DPI/坐标系换算误差。
        # live-only（离线假件无 winfo/after——try 全包）。
        import sys as _sys

        def _emit_save_btn_geo():
            print("[save-btn] emit-start", file=_sys.stderr, flush=True)
            try:
                dialog.update_idletasks()
                print(
                    f"[save-btn] rootx={submit_button.winfo_rootx()} "
                    f"rooty={submit_button.winfo_rooty()} "
                    f"w={submit_button.winfo_width()} "
                    f"h={submit_button.winfo_height()}",
                    file=_sys.stderr, flush=True)
            except Exception as _geo_exc:
                print(f"[save-btn] emit-EXC {type(_geo_exc).__name__}: "
                      f"{_geo_exc}", file=_sys.stderr, flush=True)
        _emit_save_btn_geo()
        try:
            dialog.after(300, _emit_save_btn_geo)
        except Exception:
            pass
        ttk.Button(dialog, text="预览匹配（只读）",
                   command=self.run_preview).grid(row=row, column=2, sticky="w")
        ttk.Button(dialog, text="取消（Esc）", command=self.cancel).grid(
            row=row, column=3, sticky="w")

        self._widgets = {
            "quantifier_var": quantifier_var,
            "quantifier_by_label": quantifier_by_label,
            "op_var": op_var,
            "component_rows": component_rows,
            "count_enabled": count_enabled,
            "count_op_var": count_op_var,
            "count_value": count_value,
            "recent_enabled": recent_enabled,
            "recent_entry": recent_entry,
            "mode_var": mode_var,
            "deadline_entry": deadline_entry,
            "binding_var": binding_var,
            "binding_box": binding_box,
            "binding_by_label": binding_by_label,
            "binding_status": binding_status,
            "summary_var": summary_var,
            "preview_label": preview_label,
            "submit_button": submit_button,
        }
        self.refresh_summary()

    # ── 表单读取与提交 ──────────────────────────────────────────────

    def _collect(self) -> dict:
        widgets = self._widgets
        quantifier = widgets["quantifier_by_label"].get(
            widgets["quantifier_var"].get(), "any")
        condition_op = "any" if "任一" in widgets["op_var"].get() else "all"
        count_op_labels = {"等于": "equals", "至少": "at_least", "至多": "at_most"}
        count = None
        if bool(widgets["count_enabled"].get()):
            raw = widgets["count_value"].get()
            try:
                value = int(str(raw).strip())
            except ValueError:
                raise RegionDraftError("数量条件必须是整数行数",
                                       path="draft.conditions.count.value")
            count = {"op": count_op_labels.get(widgets["count_op_var"].get(),
                                               "at_least"), "value": value}
        leaves = []
        for _ci, component in enumerate(widgets["component_rows"]):
            # P2b（审查复验）：编辑必须全链生效——field_var 为真实读取
            # 属性来源，**回写模型 _form.components**（build_draft 的
            # target/frozen_rows/preview/compile 同源）；行序=模型序。
            _sel = component["field_var"].get().strip()
            field = _sel.split(" ")[0] if _sel else component["field"]
            # P2d：真回写模型组件（P2b 版本改的是行 dict=无效——审查复验
            # exit1 实锤 conditionField=value/modelField=text）。行序=
            # describe 同源=模型序。
            if _ci < len(self._form.components):
                self._form.components[_ci]["field"] = field
            operator = _operator_by_label(component["operator_var"].get(), field)
            leaf: dict[str, Any] = {
                "field": field, "operator": operator,
            }
            if self._form.target_mode == "fixed_components":
                # 固定组件按身份区分；容器行按字段读取，不能使用组件键。
                leaf["componentKey"] = str(
                    self._form.components[_ci].get("key") or f"c{_ci}")
            if operator in ("equals", "contains", "not_contains", "regex"):
                leaf["expected"] = component["expected_entry"].get()
            elif operator == "bool_equals":
                selected = component["bool_var"].get()
                if selected not in ("是", "否"):
                    raise RegionDraftError("请选择布尔状态：是或否",
                                           path="draft.conditions.expected")
                leaf["expected"] = selected == "是"
            leaves.append(leaf)
        mode = ("absent_full_period" if "不存在" in widgets["mode_var"].get()
                else "first_match")
        selected_label = widgets["binding_var"].get()
        # 绑定只来自用户在下拉里的显式选择；默认（独立检查点）→ None。
        binding = widgets["binding_by_label"].get(selected_label)
        return {
            "quantifier": quantifier,
            "condition_op": condition_op,
            "leaves": leaves,
            "count": count,
            "recent_seconds": widgets["recent_entry"].get(),
            "content_time_enabled": bool(widgets["recent_enabled"].get()),
            "deadline_seconds": widgets["deadline_entry"].get(),
            "observation_mode": mode,
            "binding": binding,
        }

    def apply_recent_actions(self, result: Any) -> None:
        """服务端 recent_actions 回执 → 候选展示（06 §2 语义）。

        - 回执失败/缺字段：如实显示「无法获取最近动作」，仍可保存独立检查点；
        - 空清单：显示无候选；
        - 有候选：默认仍是「独立检查点」——绝不静默选中最近一条；
          用户显式选中某动作并保存，才把 {sequence, causalId} 写进 draft。
        """
        widgets = self._widgets
        actions: list | None = None
        if isinstance(result, Mapping):
            if result.get("ok") is False:
                actions = None
            else:
                raw = result.get("recentActions")
                if isinstance(raw, list):
                    actions = raw
                elif raw is None:
                    actions = []
        elif isinstance(result, list):
            actions = result
        if actions is None:
            widgets["binding_status"].set(
                "无法获取最近动作：仍可保存为独立检查点")
            return
        labels = [INDEPENDENT_CHECKPOINT_LABEL]
        mapping: dict[str, Any] = {INDEPENDENT_CHECKPOINT_LABEL: None}
        for action in actions[:5]:
            if not isinstance(action, Mapping):
                continue
            sequence = action.get("sequence")
            causal_id = str(action.get("causalId") or "")
            if sequence is None or not causal_id:
                continue
            kind = str(action.get("type") or "action")
            target = " · ".join(str(part) for part in (
                action.get("controlType"), action.get("name")
                or action.get("automationId")) if part)
            wall = str(action.get("wallTime") or "")
            label = (f"#{sequence} {kind}"
                     + (f" · {target}" if target else "")
                     + (f" · {wall}" if wall else ""))
            labels.append(label)
            mapping[label] = {"sequence": int(sequence), "causalId": causal_id}
        widgets["binding_by_label"].clear()
        widgets["binding_by_label"].update(mapping)
        try:
            widgets["binding_box"].configure(values=labels)
            widgets["binding_box"].current(0)   # 默认仍是独立检查点
        except Exception:
            pass
        if len(labels) == 1:
            widgets["binding_status"].set("当前没有最近动作：将保存为独立检查点")
        else:
            widgets["binding_status"].set(
                f"最近动作 {len(labels) - 1} 条：默认独立检查点，显式选择后才绑定")

    def build_from_form(self) -> dict:
        """收集表单 → B 校验的 draft。失败抛 RegionDraftError。"""
        return self._form.build_draft(**self._collect())

    def refresh_summary(self) -> None:  # pragma: no cover - live desktop
        try:
            draft = self.build_from_form()
        except RegionDraftError as exc:
            self._widgets["summary_var"].set(f"摘要（当前配置未完成）：{exc}")
            return
        self._widgets["summary_var"].set(draft.get("summaryZh") or "")

    def run_preview(self) -> None:  # pragma: no cover - live desktop
        try:
            draft = self.build_from_form()
        except RegionDraftError as exc:
            self._widgets["preview_label"].configure(
                text=f"无法预览：{exc}", foreground="#c62828")
            return
        result = self._form.preview(draft)
        matched = result.get("matchCount") or 0
        total = result.get("rowCount") or 0
        text = {
            "pass": "预览：冻结样本满足 ✅",
            "fail": "预览：冻结样本不满足（可按此期望保存）",
            "undetermined": "预览：不可判定（存在未读取成功/时间无法解析的字段）",
        }[result["result"]]
        detail = f"匹配 {matched}/{total} 行"
        self._widgets["preview_label"].configure(text=f"{text}（{detail}）",
                                                 foreground="#1b5e20")

    def save(self) -> None:  # pragma: no cover - live desktop
        # F87（审查代码级实锤）：messagebox 键缺失=showerror 分支静默吞
        # 错无弹窗无日志（F85 零请求零弹窗根因实锤）——错误必须可见：
        # preview_label 红字为唯一可靠通道（不依赖 messagebox 注入）。
        import sys as _sys
        # F89：stderr 通道死（panel log=stdout 合并可见/stderr 0 字节）
        # ——save 节点日志改 stdout=panel log 可见（F88 通道不对称实锤）。
        print("[wizard-save] entry", file=_sys.stderr, flush=True)
        try:
            draft = self.build_from_form()
        except RegionDraftError as exc:
            print(f"[wizard-save] RegionDraftError: {exc}",
                  file=_sys.stderr, flush=True)
            try:
                self._widgets["preview_label"].configure(
                    text=f"无法保存：{exc}", foreground="#c62828")
            except Exception:
                pass
            if self._messagebox is not None:
                self._messagebox.showerror("无法保存", str(exc),
                                           parent=self.dialog)
            return
        button = self._widgets["submit_button"]
        button.configure(state="disabled")
        # on_save 返回 requestId；成功与否以服务端按同一 requestId 的回执
        # 为准——面板收到 ok 才关闭本窗，失败恢复按钮并保留表单。
        try:
            self._save_request_id = self._on_save(draft)
        except Exception as exc:
            # 发送异常可能发生在请求已写出之后；不能安全地重新提交。
            self._widgets["preview_label"].configure(
                text=f"保存结果未知：{exc}。请检查录制结果后再操作。",
                foreground="#c62828")
            return
        request_id = self._save_request_id
        # A missing acknowledgement must be visible, but blindly issuing a
        # new request could duplicate an assertion that was already saved.
        # Keep the form and the pending id so a late acknowledgement can still
        # complete it; do not automatically resubmit.
        if request_id and callable(getattr(self.dialog, "after", None)):
            self.dialog.after(10000, lambda: self._show_pending_save(request_id))

    def _show_pending_save(self, request_id: str) -> None:
        if self._save_request_id == request_id:
            self._widgets["preview_label"].configure(
                text="尚未收到保存回执，请检查录制连接和结果。",
                foreground="#c62828")

    def cancel(self) -> None:  # pragma: no cover - live desktop
        self._on_cancel()

    # ── 回执（面板按 requestId 转交） ───────────────────────────────

    @property
    def save_request_id(self) -> str | None:
        return self._save_request_id

    def apply_save_ack(self, ack: Mapping[str, Any]) -> bool:
        """服务端保存回执；成功返回 True（面板据此关闭向导并恢复面板）。"""
        if self._save_request_id is None:
            return False
        if ack.get("requestId") not in (None, self._save_request_id):
            return False
        self._save_request_id = None
        if ack.get("ok") is True:
            return True
        button = self._widgets["submit_button"]
        button.configure(state="normal")
        self._widgets["preview_label"].configure(
            text=f"保存失败：{ack.get('error') or '请重试'}", foreground="#c62828")
        if self._messagebox is not None:
            self._messagebox.showerror("保存失败",
                                       str(ack.get("error") or "保存失败"),
                                       parent=self.dialog)
        return False


def _operator_by_label(label: str, field: str) -> str:
    for operator, text in OPERATOR_LABELS.items():
        if text == label and operator in OPERATORS_BY_FIELD.get(field, ()):
            return operator
    operators = OPERATORS_BY_FIELD.get(field) or ("equals",)
    return operators[0]


__all__ = [
    "RegionWizardForm", "RegionWizardDialog", "RegionDraftError",
    "parse_duration_seconds", "DEFAULT_RECENT_SECONDS",
    "DEFAULT_DEADLINE_SECONDS", "FIELD_LABELS", "OPERATOR_LABELS",
    "OPERATORS_BY_FIELD", "KIND", "SCHEMA_VERSION",
]
