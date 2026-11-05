# 编译「ready」但对多行同名控件必然歧义：诚实性缺口登记（2026-10-28）

- 性质：**独立验证轮发现的诚实性缺口**（代码实证）；**编译期 honest-incomplete 块已在 2026-10-28 批次实施**（见文末「实施状态」），录制的多行同名同名弱身份目标现会诚实 `incomplete` 而非虚假 `ready`。
- 关联：`windows-replay-acceptance-20261028.md` §6c（ancestry 闭环已收口）；本缺口是其「多行同名消歧待办」在**编译期**的另一面
- 分支/HEAD：`hermes-remote` / `8a5374a`

## 1. 代码实证（真实 `compile_recording`）

对「无 automationId、带 6 层 ancestry 的 `DataItem`（name=`Details`）」的 `pointer_click` 编译：

```
CASE STATUS = ready
STEP STATUS = ready
ISSUES      = ()
SELECTOR    = {controlType: DataItem, name: Details, fingerprint: sha256:x,
               ancestry: [Table->… 6 层 ]}
```

## 2. 缺口

该步骤编译为 `ready`、零 issue；但若目标 `Details` 是某表格中的**多行同名之一**（6×Details 同链），回放**必然** `replay_target_ambiguous`(candidate=6)。即 `ready` 被承诺而步骤语义上本就不可唯一解析，违背「编译得到诚实的 ready/incomplete 结论」验收项。

## 3. 根因（代码级核实）

`compile_recording` 只按单个录制事件 `synthesize_selector(event)` 生成 selector（`compiler.py:489-594`），**不接触观测控件树**，无法在编译期得知该 ancestry 链下同名候选数。`compile_selector_ambiguous` 仅在 `synthesize_selector` 返回 `None`（无强 ID **且** 无 ancestry **且** 无 text）时触发（`compiler.py:587`），对「有 ancestry 的弱身份 + 多行同名」不触发。

→ 这不是编译器孤立可修的 bug：编译器看不到行数/重复度。必须与「多行同名消歧批次」一起闭合。

## 4. 为何本轮未直接修（并发保护，符合工作区 WIP 纪律）

`compiler.py`/`selectors.py`/`models.py` 均为**并发 WIP**（region-A/B 轨 + 录制易用性轨 + 本 ancestry 轨共同修改）；把弱身份控件一律改 `incomplete` 是**契约变更**，会连带大量既有 ancestry `ready` golden 语义、与并行轨耦合，须由用户/主会话裁决后实施。本轮仅登记并留存实机无关的可复核证据。

## 5. 建议下一最小批次（与 `windows-replay-acceptance-20261028.md` §6c 待办合并）

按商业稳健性优先排序（审核方 R 修正：相对行号/位置对会排序、过滤或刷新的表格**不稳定**，可能把「明确失败」退化成「点错行」，因此不作首选）：

(a) **稳定行键 / 行内容组合 / 用户确认的语义标识**——优先。录制端为表格行捕获其**稳定业务键**（如行内实际单元格内容、唯一行选择器、用户确认的语义身份），语义唯一时解析、仍歧义时报 `replay_target_ambiguous`；
(b) 对「无强 ID + 非唯一 ancestry 链」的步骤编译期声明为**不支持回放**（`incomplete` + `compile_weak_or_ambiguous_identity` issue + 可行动中文指引）——兜底，保证「编译结论诚实」；
(c) 显式降级视觉/坐标（**必须显式声明**，不静默）；仅当目标行有稳定相对上下文（固定布局、无排序过滤）且经用户确认时才可考虑相对位置，且须在结果中明示。

倾向 (a)+(b)：语义唯一正常、语义歧义给指引，**不改安全行为**（歧义仍拒绝）。该批次落地时应新增回归：`compile` 对多行同名弱身份目标给出诚实非 `ready` 结论，且 `replay` 端报 `replay_target_ambiguous` 而非静默错点。

## 6. 证据

命令与输出、`guard_verify.py`（A/B/C 对抗守卫）、SHA-256 见 `result-report/20261028_113208_ancestry-closed-loop/`。

## 7. 实施状态（2026-10-28 批次；编译期 honest-incomplete 已落地）

原 §5( b)「编译期 honest-incomplete」已实现并复验（含录制端兄弟计数证据）：

- **录制端捕获歧义证据**：`target/recording/windows.py` 新增 `_same_name_sibling_count`；`WindowsUIACorrelator._observed` 对**弱身份**控件经 `_nativeElement` 如实统计同父下同 `(control_type, text)` 的兄弟数，写入 `ObservedTarget.same_name_siblings`；强身份不填充。
- **`ObservedTarget` 建模 + 持久化**：`target/recording/models.py` 增 `same_name_siblings: int | None`（`from_dict`/`to_dict` + 校验）；`test_case/schema/desktop-recording.schema.json` observedTarget 增 `sameNameSiblings`（nullable positive int）。
- **编译期诚实判定**：`agent/recording/compiler.py::_compile_event`——弱身份且 `same_name_siblings > 1` → 步骤 `incomplete` + `compile_weak_or_ambiguous_identity` + 可行动中文指引；唯一/未知/强 ID 仍 `ready`（不过度拦截）。
- **回归**：新增 `test_case/test_recording/test_weak_identity_honest_incomplete.py`（7 条）；
  - 6 同链同名 Details → `compile_weak_or_ambiguous_identity` + `incomplete`（不再虚假 ready）
  - 唯一弱身份 → `ready`；兄弟数未知(None) → `ready`；强 ID 多同名 → `ready`
  - `_same_name_sibling_count` 计数正确 + 无 `_nativeElement` 返回 None + `_observed` 仅弱身份填充
- **复验**：`test_recording/ + test_execution_unit` **1236 passed**（较此前 1227 增 9）；全仓基线见批后日志。

安全行为不变：歧义仍拒绝、不猜测、不静默用录制坐标。剩余：稳定行键的可选增强（行内容/用户确认语义标识）与受控 Windows 成功回放验收。
