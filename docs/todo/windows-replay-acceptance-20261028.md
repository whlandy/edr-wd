# Windows 回放 observation/selector 信息不对称缺陷 · 实机复现登记（2026-10-28）

- 编制：实机验收（在 11.26 上执行回放，非离线绿）
- 性质：**缺陷登记**——已复现「Windows 回放 observation 缺 ancestry、而录制 selector 带 ancestry」的不对称；**尚未完成成功回放验收**
- 目标：`11.26-desktop-s917l7s-win10`（170.170.11.26，Windows 10 / EDRClient.exe）
- 分支：`hermes-remote`（已部署最新 target/ 代码 + 重启服务，部署备份 `edr-wd.backup-20261028_020633`）
- 可复核证据：`docs/todo/windows-replay-failure-evidence-20261028/`
- 前置：先修复并验证过「rootAutomationId-only 入口作用域」缺陷（`ReplayScope.root_automation_id` + `golden_entry_scope`），本登记聚焦**控件层**的新发现

## 1. 实机复现步骤（真实操作，非离线）

1. 锁定 logo1（EDRClient.exe, pid 1984, handle 590820）。
2. 启动录制 → 真实鼠标点击扫描表格第一行 **Details**（DataItem；表格共 6 行同名 Details）→ stop 编译 → `status=ready`（golden：`evidence_022646`）。
3. 用 `edr-wd replay golden-trace.json --profile windows_hisec --persist-screenshots` 在 11.26 上执行回放。

## 2. 复现结果（保留完整 trace）

- `taskSuccess=False`，`semanticResolutionRate=0.0`
- step-0001 `replay_target_not_found`，details `candidate_count=0`，strategy=semantic
- trace 目录：`docs/todo/windows-replay-failure-evidence-20261028/trace/`
  - `events.jsonl`（179KB，含 3 次 observation_recorded：active=logo1/EDRClient.exe，119 控件，6 个 Details）
  - `evaluation.json`、`screenshots/001-step-0001-before.png`
- golden + recording：`.../golden/`

## 3. 根因（代码级，都已核实）

1. **录制 selector 带 ancestry**：`Details` 无 automationId → 编译器对无强 ID 控件生成 6 层 ancestry 兜底（见 `golden/golden-trace.json`，ancestry 6 层：Table → tablePage → scanDetailWidget → VirusScanWindow → scanPage → pageWidget）。
2. **回放 observation 无 ancestry**：`target/pywinauto_client.py:992-1004` 的 Windows 树构建结果**只有** class_name/text/control_id/rectangle/is_visible/is_enabled/depth/automation_id/control_type/is_password/checked，**没有 ancestry**；`agent/recording/mcp_runtime.py:289` 回放观测取 `source.get("ancestry") or []` → 恒为空。
3. **HybridLocator 严格要求 ancestry**：`agent/locator/hybrid.py:112-123` `len(actual_ancestry) < len(expected_ancestry)` 即剔除候选 → 实际 0 < 6 → 全部剔除 → `candidate_count=0`。
4. **fingerprint 方案当前不可用**：`hybrid.py:98-123` 匹配只对比 automationId/identifier/controlType/name + ancestry，**完全未使用** selector/观测里的 fingerprint；且回放 fingerprint 含窗口标题、矩形等字段，稳定性与录制侧算法一致性未验证。故「忽略 ancestry / 依赖 fingerprint」**不能直接采用**（P1）。

## 4. 关键反例：补 ancestry 仍会歧义（定量实证）

审核方预判「补回 ancestry 后 6 个同名 Details 可能变为 candidate_count=6 歧义」。用真实数据验证：

- 取回放 trace 里的真实观测（6 个 Details，ancestry 为空）+ 录制侧真实 6 层 ancestry（`recording.json` 的 pointer_click observedTarget.ancestry），喂 `HybridLocator`.

| 场景 | 结果 |
|---|---|
| A：观测无 ancestry（当前真实状态） | `replay_target_not_found`，candidate_count=**0** |
| B：6 个 Details 补上同款 6 层 ancestry | `locator_ambiguous`，candidate_count=**6** |

**结论**：6 行 Details 是同一 Table 的 6 行，祖先链完全相同，仅补 ancestry 只能把 `0` 变成 `6`、`not_found` 变成 `ambiguous`，**仍不能成功回放**。真正修复必须额外区分"多行同名"（行号/相对位置/行内实际内容控件），不能只补观测 ancestry。

## 5. 审核方修复顺序（采纳）

1. **首选修复观测生产端**：让 Windows 回放 observation 提供与录制侧**同序、同字段**的 ancestry（`pywinauto_client` 构建树时填充，或复用录制侧 ancestry 来源）。
2. 增加端到端失败回归：`录制 selector → Windows dump_tree → normalize → HybridLocator`，断言不再 `not_found`/`ambiguous`。
3. **用 6 个同名 Details 实测**是否仍在祖先链相同下歧义（本登记 §4 已证明会歧义）→ 需同时解决行级区分，或在语义不强时降级为视觉/坐标并显式声明。
4. **保留 golden、trace、截图、版本与 SHA** 后，再做一次真实 Windows **成功**回放为最终验收证据。

## 6. 结论

**本条登记为：已复现 Windows 回放 observation/selector 信息不对称缺陷；尚未完成成功回放验收。** 不能作为"实机验收通过"的证据。修复与重新成功回放后再行判定。

## 6b. 修复进展（2026-10-28 追加）

**第一步（观测生产端补 ancestry）已完成并实机验证**：
- `target/pywinauto_client.py` 新增 `_build_ancestry`（921 行）：与录制侧 `windows.py` 同序同字段，向上 6 层取 `controlType`/`automationId`/`name`，空值过滤；`_build_tree` 每个节点补 `"ancestry": self._build_ancestry(ctrl)`（1042 行）。
- 已部署 11.26，`dump_tree` 实测每个 Details 控件带 6 层、与 golden selector 完全一致的 ancestry。
- 补 ancestry 后续跑回放：`replay_target_not_found`(candidate=0) → **`locator_ambiguous`(candidate=6)**。**实证：仅补 ancestry 不够，6 行同名 Details 祖先链相同仍歧义**（审核预判成立）。
- 端到端失败回归已加：`test_e2e_contract_acceptance.py` 新增
  - `test_no_ancestry_observation_fails_not_found`（无 ancestry → not_found，固化原缺陷）
  - `test_same_ancestry_same_name_rows_are_ambiguous_not_silent`（同祖先同名多行 → ambiguous 且 candidate=6，明确歧义不静默错点）
- 本地 `test_case/test_recording test_case/test_hybrid_locator.py`：**1142 passed** 无回归。

**待决策：多行同名歧义**（产品层）：无强 ID 的多行同名控件（表格行）在无行级唯一信息时，语义上无法安全区分为唯一目标。需定消歧策略（行号/相对位置/降级视觉或坐标），或声明本类动作为不支持回放。

## 6c. 2026-10-28 追加：ancestry 最小闭环收口 + fingerprint 不对称实证

**ancestry 最小闭环已完成并锁定**（本会话核实，离线代码级 + 既有 11.26 部署证据）：
- 观测生产端已补 ancestry（`target/pywinauto_client.py::_build_ancestry`，与录制侧 `windows.py` 同序同字段：就近父优先、`controlType`/`automationId`/`name`、空值过滤、上限 6 层）。
- 实测（11.26 已部署）`dump_tree` 每个 Details 带 6 层、与 golden selector 一致。
- 回归测试 `test_case/test_recording/test_ancestry_closed_loop.py`（8 条，含本会话新增）：
  - `test_dump_tree_emits_same_order_and_fields_as_recording_side` / `test_normalized_observation_propagates_ancestry`：观测 ancestry 与录制侧同序同字段。
  - `test_full_loop_resolves_a_no_strong_id_control`：单行无强 ID + 唯一祖先链 → 成功解析（此前 candidate=0）。
  - `test_recording_selector_and_replay_observation_agree_field_for_field`：selector 与观测逐字段一致。
  - `test_mismatched_observed_chain_is_still_safely_refused`：观测链不一致仍 `replay_target_not_found`（安全不松）。
  - `test_six_identical_details_are_explicitly_ambiguous_not_guessed`：6 行同祖先同名 → `locator_ambiguous`(candidate=6)，**不猜测、不静默用录制坐标**。
  - `test_six_identical_details_with_distinct_chain_resolve_to_single`：祖先链确实不同 → 唯一解析。
  - `test_recording_and_replay_fingerprints_do_not_agree_yet`：**fingerprint 双端算法不对称**（见下）。
- 回归守卫实证：移除 `_build_ancestry` 的 ancestry 输出 → 4/5 闭环用例 FAILED（非假绿）；恢复 → 全绿。

**fingerprint 不对称（新实证，强化禁用理由）**：
- 录制侧 `windows.py::_observed` 指纹哈希 `{controlType, automationId, identifier, text, rect}`；
- 回放观测 `observation.py::normalize_observed_control` 指纹哈希 `{process_name, window_title, control_type, automation_id, control_id, class_name, identifier, text, rect}`；
- **字段集完全不同** → 同一逻辑控件双端指纹必然不同；且两侧都含 `rect`（布局即变，不稳定）。
- 结论：**在证明双端算法一致、字段稳定、能唯一定位以前，不得启用 fingerprint 匹配，也不得以"缺 ancestry 忽略 ancestry"绕过安全匹配**。已有测试锁定该不一致，防止被静默"打开"。

**6 行同名 Details 验收结论**：6 行是同一 Table 的 6 行、祖先链**完全相同**，无强 ID、fingerprint 不可用 → 语义上无法安全唯一区分 → 代码**正确**返回 `replay_target_not_found`(0，无 ancestry 时) 或 `locator_ambiguous`(6，有 ancestry 时)。**这正是要求的明确歧义，不是缺陷**；`replay.py:128` 将其映射为 `replay_target_ambiguous` 并带 `candidate_count` 细节，操作方可据此处置。

**剩余风险/下一步**：多行同名（如 6×Details 表格行）**无法在无行级唯一信息时安全回放**，属真实功能边界。选项：(a) 录制侧为表格行附加行号/相对位置等**相对**消歧符（非绝对坐标）；(b) 声明该类动作为"不支持回放"并给出明确错误指引；(c) 降级视觉/坐标但**必须显式声明**，不静默。倾向 (a)+(b) 组合：语义唯一时正常，语义歧义时给出可行动指引。此决策不改安全行为（歧义仍拒绝）。

待办：
- [x] 修复观测生产端补 ancestry（P1）——已实现、部署、实机验证（0→6）
- [x] 端到端失败回归用例——已加（`test_e2e_contract_acceptance.py` + `test_ancestry_closed_loop.py`）
- [x] 6 行同名歧义明确返回（`locator_ambiguous`=6，不猜测）——已锁定测试 + 实机验证
- [x] fingerprint 不对称禁用（算法不一致实证 + 锁定测试）——已登记
- [ ] **多行同名消歧策略**（行号/相对位置/降级显式声明）——待后续批次，不改安全行为
- [ ] 真实 Windows **成功**回放（依赖：语义唯一目标，或消歧策略落地后）——待授权受控环境

## 6d. 2026-10-28 追加：6 行 Details 祖先链「真正相同」已实机只读确认

§6c 的「6 行是同一 Table 的 6 行、祖先链完全相同」此前为 fixture/推断，本轮用**只读 `dump_tree`**（11.26 logo1，不点击/不锁窗/不修改）实测确认：

- 119 控件，6×`DataItem` 名 `Details`，`automation_id=''`；
- 6 行 `ancestry` **逐字节一致**（6 层：Table → tablePage → scanDetailWidget → VirusScanWindow → scanPage → pageWidget），仅 `rect.y`（403/443/483/523/563/603）不同。

直接回答任务 §5：**祖先链完全相同** → 补 ancestry 后从 `not_found(0)` 变 `locator_ambiguous(6)` 并继续因歧义阻塞（非唯一化）。实机只读观测 → 生产 `HybridLocator`/`compile_recording` 对拍：`locator_ambiguous(6)`（不猜测）+ 编译 `incomplete`（`compile_weak_or_ambiguous_identity`，无虚假 ready）。证据：`result-report/20261028_133756-commercial-batch/`（`10-readonly-dump_tree.log`、`20-verify_real_target.py`、`21-verify_real_target.out`）。

结论不变且证据升级：语义上 6×Details 无行级唯一信息时不可安全唯一回放，代码**正确**返回明确歧义；多行同名消歧仍为待决策产品批次，本轮不改安全行为。

## 6e. 2026-10-28 14:02 追加：6×Details 全链路闭环端到端回归（独立验证 + 可稳定失败回归）

定时任务自动轮（无现场人工监督），独立核验前几轮声明 + 新增端到端失败回归：
- **新增** `test_case/test_recording/test_six_details_closed_loop.py`（4 条，未跟踪、纯增量，未改任何业务代码）：把 11.26 实测的 6×同名 Details 场景一次贯穿生产入口——录制 `synthesize_selector`(6 层 ancestry) → 编译 `compile_recording`(incomplete + `compile_weak_or_ambiguous_identity`) → 观测 `pywinauto_client._build_tree` + `normalize_observed_control`(ancestry 逐字节一致) → `HybridLocator`(`locator_ambiguous` candidate=6) → `replay_golden_trace`(阻止派发、`replay_target_ambiguous`、remediation 无裸坐标)。
- **变异-回退证明可稳定失败**：把 `pywinauto_client._build_tree` 的 ancestry 输出变异为 `[]`（复现原缺陷）→ 本测试 1 条 + 既有 `test_ancestry_closed_loop.py` 7 条共 **8 failed**；恢复 → **15 passed**。证明回归 pin 住「observation 必须提供与录制侧同序同字段 ancestry」契约，非假绿。
- **全量**：聚焦回归 **179 passed**；全 recording 单元 **1153 passed, 1 skipped**。
- 结论不变：6×Details 祖先链相同 → 补 ancestry 仍 `locator_ambiguous(6)`（明确歧义不猜测）；编译诚实 incomplete；回放 remediation 可行动且无坐标。**尚未达成受控 Windows 成功回放闭环**（需消歧或用语义唯一目标）；**商业发布未宣称**。
- 证据：`result-report/20261028_140152-six-details-closed-loop/`（00-environment/10-focused-regression/20-test_six_details_closed_loop/README）。未提交、未推送、未部署。

待办更新：
- [x] 6×Details 全链路闭环**端到端**失败回归（本批新增）
- [ ] 多行同名消歧策略（行号/相对位置/降级显式声明）——待后续批次，不改安全行为
- [ ] 真实 Windows **成功**回放（依赖：语义唯一目标，或消歧策略落地后）——待授权受控环境

## 6f. 2026-10-28 14:26 追加：稀疏树低估守卫（编译期假 ready 修复，代码级已验）

**新确认缺陷**：录制侧点击时刻实时统计的直接兄弟数 `same_name_siblings`（`windows.py:663`）在去抖快照树低估时未作为守卫使用 → 编译器把稀疏树判 1 候选为 `ready`，回放全量观测出现全部同名行必歧义。这是 11.26「编译 ready、回放歧义」的机制层根因。

- **修复**：`agent/recording/compiler.py` 弱身份分支，树候选 ==1 时核对 `same_name_siblings`；已知整数 >1 → 发 `compile_weak_identity_tree_undercount`（incomplete）。不改任何安全边界。
- **回归**：`test_weak_identity_honest_incomplete.py` 新增 4 条（稀疏兄弟大数 → 不 ready；兄弟 1/未知且树唯一 → 仍 ready；变异守卫消失 → 该情形退回 ready）。
- **变异证明非假绿**：守卫条件变异为 `if False` → 2 条新回归 FAILED；恢复 → 全绿。
- **测试**：聚焦 33 passed；全量 recording+locator+expectation **1232 passed, 1 skipped, 1 deselected**（基线 1228，+4 净新增）。
- **证据**：`result-report/20261028_142611-sparse-tree-undercount-guard/`（10-test-output / 20-mutation-proof / 30 测试副本 / 40-compiler.diff / README）。未提交、未推送、未部署；实机不受影响（未触碰）。
- 结论不变：**尚未达成受控 Windows 成功回放闭环**；**商业发布未宣称**。

## 6g. 2026-10-28 16:55 追加：调度轮续作——ancestry 闭环复验 + 多窗口/同标题只读实机批次

定时任务自动轮（无现场人工监督），先完整阅读上一轮输出（`已粘贴的文本.txt`）与仓库状态后：
- **复验 ancestry 最小闭环已收口**：全仓 `-m 'unit and not regression'` **2712 passed, 1 skipped, 614 deselected**；聚焦 recording+execution+hybrid_locator+action_catalog+recovery **1485 passed**。6×Details 闭环 / weak-identity honest-incomplete / ancestry 闭环 / fingerprint 禁用锁定测试全绿。
- **新增只读实机批次：多窗口/同标题窗口不误操作**（任务批次点 1 的观测定向）。经既有 tunnel 对 11.26 只读探针（`initialize`/`tools/list`/`recording_status`/`get_window_lock`/`list_windows`，零写）确认：
  - **2 个同标题 `logo1` 窗口并存**，可经原生身份判别：`HiSecEndpointAgent.exe` pid **7020** handle **393844**（x=80 y=14 920×600）vs `EDRClient.exe` pid **1984** handle **590820**（x=230 y=35 922×602）。
  - 窗口锁 `locked=false`（无锁）；录制会话 `state=stopped`、`workerAlive=false`（无活跃/残留）。
  - 同标题窗口**防误操作（聚焦/激活绑定精确原生身份、同标题同进程判歧义、按 root AutomationId 解析）**的**行为级**离线覆盖由 `test_window_session.py` 全绿（依托 window-registry/window-session 既有实现，属并发 WIP，本批未改动）。
- **零业务代码改动**：未 reset/checkout/清理；未点亮类、不点击、不录制、不改策略/数据、不提交/不推送/不部署。
- 结论分层：ancestry 闭环 **代码级 + 实机（上一批 16:14 已验）通过**；多窗口/同标题**只读实机判别通过**；**真实多动作/断言实机闭环与商业发布未宣称**。
- 证据：`result-report/20261028_165527-multiwindow-sametitle-batch/`（00-environment / evidence/live_readonly_probe_multiwindow.out / evidence/offline_full_baseline_unit.out / README / 99-sha256.txt）。

## 6h. 2026-10-28 17:28 追加：独立审查跟进——正向完整性（complete）迁移 + null 严格性（P1/P2 落地）

独立/调度审查指出「只完成 2.5 项、仍 1 P1 + 1 P2、不能判定闭环」，本轮直接落地两项修复并留存证据（`result-report/20261028_172824-complete-integrity-migration-review-followup/`）：
- **[P1] 正向完整性完整迁移**：
  - 生产端 `pywinauto_client._build_tree`：`children()` 枚举异常 → `_tree_truncated=True`（枚举失败 = 未知区域，不当作证明空）；`dump_tree`/`dump_tree_for_window` 输出 `complete = not _tree_truncated`（只在遍历明确完整时 `True`）。
  - inventory `inventory.py`：快照写入 `complete = tree.get("complete") is True`（树缺字段/非 True → `complete=False`，旧快照默认不完整）。
  - 编译器 `compiler.py::_weak_selector_candidate_count`：弱身份单候选进入 `ready` 必须以 `frame.get("complete") is True` 为前提；字段缺失/None/False（截断、children 异常、深度/数量上限）→ 默认不完整 → `compile_weak_identity_unresolved`；**强身份不受影响**。
  - UIA 虚拟化：后端不能证明全行物化即不得 `complete=True`；虚拟化低估由既有 `compile_weak_identity_tree_undercount` 兜底，语义不变。
- **[P2] null 严格化**：`models.py` 对 `complete`/`truncated` 字段存在即严格要求 `bool`，`None`/`"false"`/`0` 均 REJECT（对齐 schema `"type":"boolean"`）。
- **回归**：新增 `test_complete_integrity_migration.py` 15 条（编译期完备门×5、生产端完备×5、inventory 传播×3、P2 null×2）；既有 3 个夹具把「完整性前置」显式化（`_snapshot` 补 `complete=True`，`_snapshot_truncated` 补 `complete=False`），未改任何断言语义。
- **变异证明非假绿**：编译器门临时变异为「非 True 也放行」→ `test_missing_complete...`/`test_complete_false...` FAILED（退回 ready）；恢复 → 全绿。
- **测试**：聚焦 48 passed；全仓 `-m 'unit and not regression'` **2727 passed, 1 skipped, 614 deselected**（较上一批 2712 净 +15，无回归）。
- 未提交、未推送、未部署；实机（11.26）未触碰；未 reset/checkout/清理并发 WIP。
- 结论分层：P1/P2 **代码级落地 + 回归 + 变异证明通过**；**受控实机多场景闭环与商业发布未宣称**。

## 6i. 2026-10-28 17:54 追加：独立审查跟进 2——虚拟化完整性 + 并发串扰（2×P1 落地）

上一批审查判「暂不通过，仍 2 个 P1」，本轮直接落地并留证（`result-report/20261028_175412-p1-virtualization-concurrency-fix/`）：
- **[P1] 虚拟化树误判完整**：`children()` 完成不代表穷尽。新增 `_children_enumeration_is_proven_complete`：非虚拟化容器（pane/group/button）`children()` 即穷尽 → 完整；虚拟化容器（table/datagrid/list/tree）必须正证明物化（`_materialized_row_hint` 报告总行数 == 物化子数）才 `complete=True`；拿不到可靠总数（无法证明物化）→ 不完整。弱身份在「仅物化部分行」的表/列表里不再 `ready`（改 `unresolved`）。强身份不受影响。
- **[P1] 完整性并发串扰**：`_tree_truncated` 实例属性被并发 dump 互相重置。改为**每次调用独立累计器** `_incomplete=[bool]` 贯穿 `_build_tree` 递归；`dump_tree*` 各建本地累计器，`complete = not incomplete[0]`，不再读写实例属性。
- **失败回归**（`test_complete_integrity_migration.py` 15→19 条，新增 4）：
  - `test_partially_materialized_virtualized_table_is_incomplete`：Table `item_count=10`/物化 1 → `complete=False` + 编译 unresolved。
  - `test_unproven_virtualized_container_is_incomplete`：无可靠总数 → 不完整。
  - `test_fully_materialized_virtualized_table_can_be_complete`：正证明一致（count==materialized）→ `complete=True`。
  - `test_concurrent_dumps_do_not_crosstalk_completeness`：并发 A 截断 / B 完整，B 不串扰。
- **变异证明非假绿**（`evidence/mutation_proofs.log`）：「无法证明物化当完整」→ 虚拟化回归 FAILED；「重建共享实例标志」→ 并发回归 FAILED；恢复全绿。
- **测试**：迁移文件 **19 passed**；全仓 **2731 passed, 1 skipped, 614 deselected**（较上一批 2727 净 +4，无回归）。
- 未提交、未推送、未部署；实机未触碰；未 reset/checkout/清理并发 WIP。
- 结论分层：2×P1 **代码级落地 + 失败回归 + 变异证明通过**；**真实虚拟化表正向行数证明依赖 UIA `item_count`/`row_count` 暴露，实机待验**；**受控实机多场景闭环与商业发布未宣称**。

## 6j. 2026-10-28 19:0x 追加：ancestry 部署态只读复验轮（现场状态确认）

定时任务自动轮（无人类现场监督），复验 ancestry 闭环在 11.26 **当前部署态**仍然在位，并确认现场无残留：
- **实机只读探针**（零写：仅 `tools/list`/`get_window_lock`/`recording_status`/`list_windows`/`dump_tree`）：
  - `get_window_lock` → `locked:false`（无锁）；`recording_status` → 旧 16:05 会话对象 `stopped`/`workerAlive=false`（无活跃会话）；
  - `dump_tree(^logo1$)` → 29 控件，**`controls WITHOUT ancestry = 0`**——每个控件都带非空 `ancestry`
    （如 Security Center CheckBox 5 层：tabContentWidget→tab_widget→MainWidget→SafraUIMainWindow→Desktop 1 Pane），
    即 `pywinauto_client._build_tree` 补 ancestry 的修复在部署态**仍然在位**；
  - 当前连接窗口为 7020（安全防护中心首页，Protection checked=True），故本次无 Details 表行；6×Details
    祖先链字节级相同、仅 rect.y 不同已由既有批次 `133756/6d` 实机只读确认，不重复。
- **代码级 fresh 证据**：聚焦 closed-loop/弱身份/parity/e2e/完整性 **99 passed**；全仓 **2738 passed, 1 skipped,
  614 deselected**，余 4 failed 全部为跨轨过期夹具 `test_compile_vs_replay_field_parity.py`
  （`ObservedTarget` 数字/空名不可达 + 测试助手解引用 bug，非生产缺陷；由承重回归
  `test_field_parity_numeric_contract.py` 5 passed 锁定当前正确契约，已在
  `docs/todo/field-parity-stale-fixture-20261028.md` 登记）。
- **零业务代码改动**：未 reset/checkout/清理；未点击/录制/改策略数据/重启会话；本批只读。
- **结论分层**：ancestry 观测生产端 **代码级 + 部署态只读确认通过**；受控 Windows 单场景闭环=既有
  `160522`（强 ID）；**商业发布未宣称**（多行同名消歧/多场景实机/批次 9 发布材料未全部落地）。
- 证据：`result-report/20261028_185859-live-ancestry-verify/`（00-environment /
  10-readonly-probe / 20-focused-closed-loop / 21-full-repo-baseline / README / 99-sha256）。

## 6d. 独立再核验（2026-10-28 19:22 上海 · 定时任务 edrwd-recording-code-review 本轮）

- 独立重跑（不依赖 185859 结论）：
  - `test_ancestry_closed_loop` + `test_e2e_contract_acceptance` → **53 passed**。
  - recording + `test_hybrid_locator` + `test_execution_unit` + `test_execution` → **1357 passed / 1 skipped / 6 deselected**（排除项仅为 field-parity 跨轨夹具）。
  - locator + execution + replay → **249 passed**。
- **与 185859 全仓基线交叉印证**：两轮独立结论一致——4 条红测 `test_compile_vs_replay_field_parity.py` 为跨轨过期夹具（`ObservedTarget` 数字/空名不可达 + 测试助手解引用 bug），非生产缺陷；承重回归 `test_field_parity_numeric_contract.py` 5 passed 锁定正确契约。
- **对抗性复读安全不变量**：`replay.py` 语义路径每阻塞点均 `StepMaterializationError`；`test_replay_six_rows_ambiguous_never_dispatches` 锁定 6 行歧义不派发、remediation 无坐标。
- **WIP 保护**：field-parity 夹具曾误改后**字节还原**（重跑确认 4 红复现一致），不代它轨决裁；业务共享代码零改动。
- **并发边界**：`target/pywinauto_client.py` 为另一轨 ≈9 分钟前活动的 493 行虚拟化 WIP——多行稳定行键消歧横切含该文件，**本轮停止实施**，改只读审查。
- **结论分层不变**：ancestry 代码级核实通过；受控 Windows 成功回放与商业发布未完成，需受控环境 + 稳定行键消歧。
- 证据：`result-report/20261028_192208-scheduled-independent-verify/`。

## 6k. 2026-10-28 22:00 追加：独立复验 ancestry 闭环 + batch-2 P0 动作回放一致性回归

定时任务自动轮（edrwd-recording-code-review，无现场人工监督），先完整阅读上一轮输出与仓库状态：

- **ancestry 最小闭环独立复验**（不依赖前轮结论）：
  - 观测生产端契约逐字段核对（`pywinauto_client._build_ancestry/_build_tree` vs 录制侧 `windows.py`：就近父优先、camelCase、空字段丢弃、上限 6 层、任一层失败整链清空）；`normalize_observed_control` 透传 ancestry。
  - 聚焦 `test_ancestry_closed_loop + test_six_details_closed_loop + test_weak_identity_honest_incomplete + test_hybrid_locator` = **57 passed**。
  - **稳定失败实证**（独立脚本，用完即删）：模拟 pre-fix「回放观测无 ancestry」→ 带 6 层 ancestry 的 selector → `replay_target_not_found` candidate_count=0（=11.26 缺陷）；恢复观测侧 ancestry → 唯一解析。证明闭环测试 pin 住观测生产端，非假绿。
  - 全仓 unit-基线 **2746 passed, 1 skipped, 614 deselected**，无回归。
- **batch-2 动作矩阵独立审计**（8 类动作 录制→编译→回放 全链路）：`gui.click` FULL；`type_text/scroll/drag/keyboard/window_transition` PARTIAL；**`gui.select` 与 `toggle/check` 两个 P0 零覆盖或非闭环**。
- **补齐 2 个 P0（纯新增）**：新 `test_case/test_recording/test_action_replay_consistency.py`（6 条，全从 `RawCaptureEvent` 走 `synthesize_selector→compile_recording→replay_golden_trace`）：
  - `gui.select`：编译出 `item` 参数 + 强身份 ready；回放派发 `gui.select` 带录制 item + 重解析 target_ref（新鲜观测，无录制坐标）；目标缺失→安全拒绝不派发。
  - `toggle_change`：编译带 `checked` verifier；回放重观测 `control_checked_equals` 通过；状态相悖→诚实失败（pass_rate=0）。
  - 重跑 recording+locator **1230 passed, 1 skipped, 1 deselected**（净 +6，无回归）。
- **零业务代码改动**：`git status` 与开轮一致（仅新增测试文件为纯增量）；未 reset/checkout/清理；未点击/录制/改策略数据/重启会话；`/tmp` 探针已删；未提交/未推送/未部署。
- **结论分层**：ancestry 最小闭环 **代码级 + 稳定失败实证 + 全仓基线通过**；batch-2 两个 P0 动作一致性 **离线端到端补齐**；受控 Windows 多场景闭环与商业发布**未宣称**（多行同名消歧/其余实机批次/发布材料待后续受控轮）。
- 证据：`result-report/20261028_220000-scheduled-batchadvance/`（README + evidence）。

## 6g. 2026-10-28 23:52 追加：独立复核 + 一手突变举证（定时轮，无现场监督）

独立复核上一轮 23:32 结论，并对「观测生产端必须提供同序同字段 ancestry」这一契约做**第一手突变举证**
（非转述）。分支/HEAD 与上轮一致：`hermes-remote` / `8a5374a`；无 commit。

- **实机只读复核（11.26，本轮最新取证）**：`result-report/20261028_234910-scheduled-ancestry-verified-mutation/evidence/live-readonly-probe.txt`
  - 84 控件 `controls WITHOUT ancestry = 0`（全带非空 ancestry；观测生产端修复在真实部署生效）。
  - 6× `DataItem "Details"`：`automation_id=''`，6 层祖先链逐字节一致（distinct chains=1）→ 信息对称后
    仍 `locator_ambiguous`(6)/诚实 `incomplete`，不猜测。
  - 目标空闲无锁（locked=false）、无活动录制（state=stopped）。
- **一手突变举证**（`evidence/mutation-proof.md`，自控可逆局部变异）：
  - 把 `pywinauto_client._build_tree` 的 `"ancestry": list(my_ancestry)` 变异为 `[]` →
    重跑 3 个闭环测试文件 **8 failed, 50 passed**（8 个守护用例 red）。
  - 恢复字节一致（shasum 前/后均 `5a0ca397…`）→ 同一组 **58 passed**。
  - 证明：ancestry 守护**真实依赖**生产端发射，删除即稳定失败，非空绿。
- 全 recording 离线套 **1223 passed, 1 skipped, 1 deselected**（无回归）。
- **零业务代码改动**：被读 WIP 文件字节不变（`target/pywinauto_client.py` shasum==开轮）；本轮唯一新增为
  `result-report/20261028_234910-scheduled-ancestry-verified-mutation/`（gitignore 约定证据目录）。
  未提交/未推送/未部署；目标端无残留锁/会话/进程。
- **结论分层**：ancestry 最小闭环 **代码级 ✔ + 稳定失败实证 ✔（一手）+ 全离线基线 ✔ + 实机单场景复核 ✔**；
  **正向 ready→回放→断言通过 闭环 ⛔**（本受控扫描表视图无可证明无害的强 ID 可回放动作：唯一强 ID
  分属窗口 chrome 与业务改写按钮 Trust/Handle/Feedback/"Handle later"，均不可安全点击）；**商业发布未宣称**。
- 剩余风险与下一批：正向 strong-ID 实机闭环（需安全 NAV 型强 ID 控件视图）、6 同名 Details 消歧（产品
  决策）、key_command 快捷键编译/回放（明文 unsupported，需产品决策）、断连/恢复/脱敏/并发/发布材料实机批次。

## 6k. 2026-10-29 00:00 追加：ancestry 闭环独立复验 + 实机只读无残留确认

定时任务自动轮（无现场无人监督）。先完整阅读上一轮输出与仓库现状，独立复验 ancestry 闭环未回归、
非空绿，并做只读实机确认。**零业务代码改动、未提交/推送/部署、目标端零写。**
- **离线全绿复验（本轮 fresh）**：聚焦闭环五文件（ancestry-closed-loop / six-details-closed-loop /
  weak-identity-honest-incomplete / e2e-contract / hybrid-locator）**99 passed**；全
  recording+locator+execution 切 **1388 passed, 1 skipped, 1 deselected**。
- **实机只读探针（零写，evidence/live_readonly_probe.out）**：
  - `recording_status -> state=stopped, workerAlive=false`；`get_window_lock -> locked=false`——目标
    空闲、无锁、无活动录制，无残留。
  - `dump_tree(^logo1$)` → **64 控件，controls WITHOUT ancestry = 0**——部署态观测生产端
    `_build_ancestry/_build_tree` 补 ancestry 契约仍在位。
  - **6× `DataItem "Details"`：distinct ancestry chains = 1**（Table→tablePage→scanDetailWidget→
    VirusScanWindow→scanPage→pageWidget，逐字节一致，仅 rect.y 403/443/483… 不同，
    evidence/details_chain_detail.out）→ 信息对称后仍 `locator_ambiguous`(6)，不猜测、不静默用录制坐标。
- **结论分层**：ancestry 最小闭环 **代码级 ✔（本轮 fresh 全绿）+ 实机单场景只读 ✔ + 无残留确认 ✔**；
  正向 ready→回放→断言通过闭环 = 既有轮 3-live（`20261028_193726`）已实机复现，本轮不重复实机写操作；
  **商业发布未宣称**。
- 证据：`result-report/20261029-000010-scheduled-rereverify/`（env / 实机只读探针 / 6×Details 链明细 /
  聚焦闭环完整输出 / 全切输出 / SHA-256）。
- 剩余风险与下一批（不变）：多行同名消歧（产品）、key_command 快捷键（明文 unsupported，需产品决策）、
  恢复矩阵实机、recorder panel 实机、脱敏端到端、并发/长稳/发布材料。

## 6l. 2026-10-29 01:0x 追加：ancestry 闭环独立复验 + 恢复/脱敏覆盖审计 + 实机只读无残留（本轮）

定时任务自动轮（edrwd-recording-code-review，无现场人工监督）。先完整阅读上一轮输出与仓库现状，
独立复验 ancestry 闭环，并对任务批次 5（恢复矩阵）与批次 7（脱敏）做只读覆盖审计。**零业务代码改动、
未提交/推送/部署、目标端零写。**
- **离线全绿复验（本轮 fresh）**：聚焦闭环五文件 **99 passed**；recording+locator+execution+action
  切 **1462 passed, 1 skipped, 1 deselected**；全仓 `test_case/` **2771 passed, 1 skipped,
  614 deselected（0 failed）**。
- **恢复/脱敏覆盖审计（只读审查，不虚构缺口）**：`QueuedCaptureSource.stop()` 截断诚实门
  （`test_capture_source_recovery.py`，变异证明承重）、回放 stale 锁在 dump 前拒绝且仅一次调用、executor
  至多一次 relock（`recover_pre_action_observation`/`recover_post_action_observation`）、跨窗口 relock、
  脱敏数据边界（`recording_secret_plaintext` / 未知敏感性 honest-incomplete / 未脱敏与全屏截图拒绝写 trace）
  均已被增量回归 pin 住——未发现「已实现但无回归」的离线缺口可安全补。仍未落地项需产品决策
  （多行同名消歧、key_command）或需受控实机+人工监督（正向多场景实机、发布材料）。
- **实机只读探针（零写，evidence/live_readonly_probe_*.out）**：
  - `edr-wd status` 全 ok（SSH / Python 3.11.9 Windows / deps / identity 验证通过）。
  - `get_window_lock -> locked=false`；`recording_status -> state=stopped, workerAlive=false,
    started=false, queueDepth=0`——目标空闲、无锁、无活动录制，无残留。
  - `list_windows` → 4 窗口，其中 2 个同标题 `logo1`（EDRClient.exe pid 1984 handle 590820 vs
    HiSecEndpointAgent.exe pid 7020 handle 393844），原生身份可判别，不误操作。
  - `dump_tree(^logo1$)` → **84 控件，controls WITHOUT ancestry = 0**——部署态观测生产端补 ancestry
    契约仍在位（链长分布 6 层×79）。
  - **6× `DataItem "Details"`：distinct ancestry chains = 1**（6 层逐字节一致，仅 rect.y
    403/443/483/523/563/603 不同，evidence/live_readonly_probe_part4_details_and_close.txt）。
    → 信息对称后仍 `locator_ambiguous`(6)，不猜测、不静默用录制坐标（任务 §5 答案：祖先链真正相同）。
- **WIP 保护**：`target/pywinauto_client.py` sha256 `5a0ca397…` 与本会话/前轮一致——无并发写者改动、
  无字节漂移；`git status` 与开轮一致。
- **结论分层**：ancestry 最小闭环 **代码级 ✔（本轮 fresh 全绿）+ 实机单场景只读 ✔ + 无残留确认 ✔**；
  正向 ready→回放→断言通过闭环 = 既有轮 3-live（`20261028_193726`）已实机复现，本轮不重复实机写操作；
  **商业发布未宣称**。
- 证据：`result-report/20261029_010400-scheduled-verify-010423/`（README / env / 聚焦闭环 / 全切 /
  全仓 / 实机只读探针×4 / 6×Details 链明细 / 目标 status / SHA-256）。
- 剩余风险与下一批（不变）：正向 strong-ID 实机闭环（受控环境+人工）、多行同名消歧（产品）、key_command
  快捷键（明文 unsupported，需产品决策）、断连/恢复矩阵实机、recorder panel 实机、脱敏端到端、并发/长稳、
  发布材料。

## 6m. 2026-10-29 审核后文档同步（修正已删除的正向完整性证明描述）

审核指出：公开验收文档 §6i 仍描述“虚拟化容器可通过 `_materialized_row_hint` + 子项数量相等获得
`complete=True`”，但**当前源码已删除该正向旁路**，设计说明与实现相反，公开发布前必须同步。本轮核实并将
本文件与实现对齐：

- **实现现状（重读源码核实）**：`target/pywinauto_client.py::_children_enumeration_is_proven_complete`
  对虚拟化容器（table/datagrid/list/tree）**一律保守 `incomplete`（返回 False）**，不再依赖
  `_materialized_row_hint` 报告总行数与物化子数相等来证明完整；`_materialized_row_hint` 已全部删除
  （源码 grep 无命中）。即：无法证明全行物化的虚拟化容器不进入弱身份 `ready`（改 `unresolved`），强身份不受影响。
- **§6i 第 168/173 行更正**：原「虚拟化容器必须正证明物化（`_materialized_row_hint` 报告总行数 == 物化子数）
  才 `complete=True`」及「`test_fully_materialized_virtualized_table_can_be_complete`：正证明一致 → `complete=True`」
  的描述**已过时、予以废弃**。当前行为以本段为准：虚拟化容器一体不完整，无“全物化可 complete”正向例。
- **测试现状**：`test_case/test_recording/test_complete_integrity_migration.py` 现含
  `test_partially_materialized_virtualized_table_is_incomplete`（部分物化 → incomplete）与
  `test_unproven_virtualized_container_is_incomplete`（无可靠总数 → incomplete）；原正向
  `test_fully_materialized_virtualized_table_can_be_complete` 已随死代码一并移除，不再存在。
- **§6j/010400 轮尾注的 SHA 亦同步**：该轮“WIP 保护”所记 `5a0ca397…` 为旧基线；
  当前 `target/pywinauto_client.py` SHA-256 为 `701b3d54…`（与 recovery-residue 轮证据一致），
  是审核批准的“虚拟化保守 incomplete + 删除 `_materialized_row_hint`”后的**预期哈希**，非异常覆盖。
- 本轮仅同步文档，未改动源码工作树。结论分层不变：ancestry 最小闭环 + 虚拟化/并发/歧义/HybridLocator
  **代码级通过**；受控实机多场景闭环与商业发布仍未宣称。

## 6n. 2026-10-30 13:15 追加：视觉回放帧一致性缺陷（窗口移动后旧帧错点）修复轮

上一轮 02:35 报告 §八.2 延后维度中的「窗口移动/错窗口/旧截图」一项本轮落地（发现→回归→修复→变异→实机只读探针）：

- **新 P1 缺陷**：`SafeVisualResolver` 不校验 `screenshot_origin`（捕获时刻窗口左上角）与当前锁窗
  `active_window.rect` 一致——窗口移动后旧帧投影坐标仍可落进新矩形并通过全部既有安全门 → 点错位置。
  真实 trace 交叉核对：02:35 轮 3 份回放 `origin == rect[:2]`（[80,14]）——不变量真实成立但从未被强制。
- **修复**：`agent/recording/visual.py` 新增 `_verify_frame_origin`，偏差 >32px（复用 bundle 契约的
  UIA 隐形边框容差）→ `visual_frame_window_mismatch`；`resolve`/`collect_evidence` 两条路径同门；
  无 origin 观测保持既有契约。+38 行，该文件开轮干净、无并发写者。
- **回归**：新增 `test_visual_frame_consistency.py` 8 条（修复前 5 failed→修复后 8 passed；
  含 02:35 真实 trace 几何、负坐标多显示器、32px 边界、无 origin 旧契约、collect_evidence 同门）。
- **变异验证**：门条件 → `if False` → 3 failed；恢复 SHA 逐字节一致 → 8 passed（非空洞）。
- **宽回归**：recording+evidence+execution_unit+hybrid_locator **1424 passed / 1 skipped / 0 failed**。
- **实机只读探针**（零写）：locked=false、recording stopped（无残留）；dump_tree 9 控件 0 缺
  ancestry（部署态契约在位）；window [80,14,920,600] 与 origin 不变量一致。
- 未提交/未推送/未部署；108 项既有 WIP 零触碰。证据：`result-report/20261030_131500-visual-frame-consistency/`。
- 剩余：provider 边界（bundle 已有 ±32px 校验；旧 refresh 路径为 WIP 文件本轮未动）可选加固；
  遮挡/最小化、动态内容、脱敏报告边界等延后维度；多行同名消歧与 key_command（产品决策）；
  受控实机多场景与商业发布未宣称。

## 6o. 2026-10-30 13:45 追加：最小化窗口截图/捕获源头拒绝（P1 修复轮）

02:35 报告 §八.4 登记的「目标端源头拦截」延后加固点实质化（发现→回归→修复→双变异→宽回归→实机只读探针）：

- **P1 缺陷**：锁窗最小化后 `GetWindowRect` 停靠于 ≈(-32000,-32000)，`pywinauto_client.py::screenshot`
  三条捕获路径（capture_as_image/ImageGrab/GDI PrintWindow）均无最小化检查，产出非真实 UI 视图的帧；
  且**观测与截图报告同一停靠矩形** → `SafeVisualResolver._verify_frame_origin`（§6n 的帧一致性门）
  因两值一致而无法拒绝——观测/截图同源一致的坏几何骗过全部既有门。
- **修复**：`pywinauto_client.py` 新增 `_is_minimized_rect`（两轴同时 <-30000 才判停靠，多屏负坐标 -3840 不误伤）
  与 `is_minimized`（Win32 与 capture origin 同源，不依赖 UIA）；`_capture_geometry` 抛
  `window_minimized`；`screenshot` 入口显式 `{"ok": False, "code": "window_minimized"}`，
  任何帧捕获/文件写入之前拒绝。传播链已核实：`capture_redacted_window_frame` → `replay_capture`
  → agent 侧 fail-closed。+40 行，3 处纯新增 Edit，107 项既有 WIP 零触碰。
- **回归**：`test_minimized_window_capture.py` 5 条（修复前 4 failed；含 -3840 多屏负坐标、
  单轴停靠、最小化时不落盘、可见窗口不受影响）。
- **双变异**：判定函数→`return False` →2 failed；入口门→`if False and` →2 failed；每次恢复 SHA
  逐字节一致 `92aee21a…` → 5 passed（非空洞）。
- **宽回归**：recording+execution_unit+background_capture+window_guard **1353 passed / 1 skipped / 0 failed**。
- **实机只读探针**（零写）：locked=false、无活跃录制残留、dump_tree 29 控件 0 缺 ancestry、
  窗口 [80,14,920,600]。目标端未部署本轮修复（纪律使然）；最小化拒绝的实机单场景待受控窗口期。
- 未提交/未推送/未部署。证据：`result-report/20261030_134500-minimized-window-capture-refusal/`。
- 剩余：部署+最小化拒绝实机单场景；最小化下语义观测表现；§6n 延后维度余项（遮挡组合、动态内容、
  脱敏报告边界）；多行同名消歧与 key_command（产品决策）；受控实机多场景与商业发布未宣称。

## 6p. 2026-10-30 14:12 追加：legacy 观测路径帧来源校验（§6n 延后 provider 边界加固）

- **缺陷**：`MCPObservationProvider.refresh()`（无 product-run token 的回放路径）在
  `capture_screenshot=True` 时存储截图与 `screenshot_origin`，但只校验 int-pair，
  从不与观测窗口矩形比对；`refresh_bundle`（:394-403）早有 ±32px 门。窗口移动后
  或来自其他窗口的陈旧帧在 semantic_only + persist_screenshots 回放中被持久化为
  「证据」，无解析器把关（visual resolver 仅在非 semantic_only 模式构建）。
- **修复**：legacy `refresh()` 截图块补同语义门：origin 与窗口矩形偏移 >32px 拒绝；
  窗口矩形缺失拒绝。+17 行纯新增。修复后 SHA `7ca72010`（WIP `1611df42` + 门）。
- **回归**：`test_legacy_refresh_frame_origin.py` 5 条（容差内接受/超偏移拒绝不落盘/
  无矩形拒绝/非 int origin 拒绝/semantic_only 不受影响）。
- **双变异**：比较门停用 → 条件2 失败；None-rect 门停用 → 条件3 失败（退化为
  TypeError 仍被捕获）；每次恢复逐字节一致 `7ca72010`。
- **宽回归**：recording+execution_unit+window_guard **1355 passed / 1 skipped /
  0 failed**。region_ui/region_assertion_model 在宽集合中的历史顺序性脆弱与本轮
  无关（单独运行全绿、mtime 10-27、未触碰）。
- **实机只读探针**（零变更）：locked=false、recording stopped 无 worker、dump_tree
  22 控件 0 缺 ancestry、窗口 [80,14,920,600]。
- **事故披露**：恢复操作误用 `git checkout -- agent/recording/mcp_runtime.py` 一度
  清掉并行 WIP（147 行）；已从会话记录重建 patch 恢复至逐字节一致（`1611df42`，
  affected 405 测试全绿）后再叠加本批门。最终 stat：164 insertions（147 WIP+17 门）。
  教训：恢复/校验一律 Edit+shasum，禁止 git checkout。
- 未提交/未推送/未部署。证据：`result-report/20261030_141200-legacy-refresh-frame-origin/`。
- 剩余：部署+实机拒绝单场景（移动帧/最小化）；§6n 延后余项（遮挡组合、动态内容、
  脱敏报告边界）；多行同名消歧与 key_command（产品决策）；受控实机多场景与商业发布
  未宣称。

## 6q. 2026-10-30 14:45 追加：脱敏审计轮——受保护字段内容断言明文持久化（P1 修复轮）

对任务清单第 7 项（密码控件、凭据、截图、OCR、日志、trace 的源端脱敏）做全链路审计
（键盘钩子→text_commit→inventory/dump_tree 归一化→截图源端脱敏→OCR→trace/golden
持久化→回放 env-only），确认既有链条后，发现并修复一条绕过脱敏边界的断言路径：

- **P1 缺陷（实证）**：断言保存路径对受保护字段无门。`add_assertion` 硬编码
  `protected=False` 并把 `expected` 原样写入 recording 文档；向导冻结
  （`_freeze_editor_draft`）把观测值原样预填进 `typedExpected`。探针
  （`result-report/20261030_144500-secret-assertion-leak/probe_leak.py`）：
  `protected=True, value="SuperSecret123"` 的 text_equals 断言保存成功，
  明文落盘于 `events[0].assertion.expected` —— 与 text_commit 路径
  `recording_secret_plaintext` 模型级拒绝形成不对称。注意：生产端两平台
  （windows `_describe`、macOS secure-field 边界）只在 protection 明确 False
  时才读 value，故实机向导路径预填受保护值需要未知注入路径；但
  `add_assertion` 是管理面入口（CLI/上游载荷可直接到达），纵深防御要求
  与 text_commit 同级边界的门。
- **修复**（两处，均在长期 WIP 文件，改前记录 SHA-256，Edit-only，无 git 操作）：
  1. `target/recording/assertion_draft.py`：新增 `protection_blocks_content`
     （显式 True 恒拒；未知保护仅对 value-bearing 类型 Edit/Combobox 拒——
     生产端契约保证未知保护时值未被读取，静态标题/复选框不误伤）；
     `build_assertion_draft` 冻结侧清除受保护/未知字段的值预填；
     `validate_assertion_draft` 把冻结 protectionStatus 显式带入载荷
     （未知为字符串 "None"，与「从未携带元数据」区分）。
  2. `target/recording/session.py`：`add_assertion` 对携带保护来源
     （protectionStatus/protected 键）的内容断言
     （text_equals/text_contains/value_equals）按上述判定拒绝，
     错误码 `recording_secret_plaintext`（与 text_commit 同码）；无保护来源
     的经典编辑器路径保持既有契约不变。
- **回归**：新增 `test_case/test_recording/test_secret_assertion_boundary.py`
  13 条（冻结侧 True/None 不预填、False 正常预填、保存门 True/None 拒绝、
  False 正常保存、identity-free/布尔断言不受影响、冻结草稿提交路径同门）。
  修复前首条即失败（prefill='SuperSecret123'）。
- **双变异**：保存门 `if False and` → 3 failed；冻结清除 `if False and` →
  2 failed；每次恢复 shasum 逐字节一致（终态 `33fb2330`/`5e9f9962`）。
- **兼容性**：修复过程中 strict 版本误伤 3 个既有 wizard/u3 集成测试
  （picker 草稿无 controlType/保护元数据）；按生产端契约收窄为
  value-bearing-only 后全绿。宽回归 recording+evidence **1322 passed /
  1 skipped / 0 failed**；test_hybrid_locator 23 passed；test_replay+r2
  94 passed（含既有 recording_secret_plaintext / golden_secret_missing 契约）。
- **实机只读探针**（零变更）：tunnel 200、locked=false、recording stopped
  无 worker、dump_tree 15 控件 0 缺 ancestry、窗口 [80,14,920,600]。
- 未提交/未推送/未部署。证据：`result-report/20261030_144500-secret-assertion-leak/`
  （WIP 前后 SHA、探针脚本与输出、diff-stat、双变异记录、SHA-256 manifest）。
- 剩余：脱敏审计其余维度（报告渲染层转义、trace.md 路径）未在本轮展开；
  多行同名消歧与 key_command（产品决策）；受控实机多场景与商业发布未宣称。

## 6r. 2026-10-30 15:29 追加：trace.md 渲染层转义一致性（§6q 遗留「报告渲染层转义」收敛）

- **缺陷（红测实证）**：`agent/trace/markdown.py::_escape_markdown`
  （trace.md 渲染器唯一转义出口）与仓库规范助手
  `markdown_escape.escape_markdown`（recovery_markdown.py:58 与
  render_report.py 的正确组合）契约不一致：`\n`/`\r` 不折叠、`|` 不转义、
  输入反斜杠不翻倍。window_title/process_name/target_profile/diagnostic
  均经此出口落盘；红测渲染出
  `| Target / profile | win| injected |row |` —— header 表行被真实打断。
  多行 window_title 可在 provenance 行/diagnostic 项注入伪结构。
- **修复**：`_escape_markdown` 对齐规范契约（C0 控制字符剥离、`|`→`\|`、
  `\`→`\\`、CR/LF→单空格、CRLF→单空格防双空格；`html.escape` 前置与
  `_MARKDOWN_SPECIALS` 前缀逻辑不变）。+32 行单文件，修复后 SHA
  `2c1cc5cc`（改前基线 `2cf7c337`，git 干净无并发 WIP）。
- **回归**：新增 `test_markdown_escape_consistency.py` 10 条（单元 6 +
  渲染器级 4：header 表、provenance 行、diagnostic 项三条注入路径），
  修复前 8 failed；既有 `test_markdown.py` 回归表反斜杠契约行同步更正
  （旧行把「反斜杠按数据保留」锁成契约，与转义 `*` 组合时可被消耗，
  属契约缺陷）。修复后 29 passed。
- **双变异**：管道门停用 → 4 failed；换行折叠停用 → 4 failed；
  每次 cp 恢复后 SHA 逐字节一致 `2c1cc5cc`。全程未用 git checkout/restore。
- **宽回归**：test_evidence 76；evidence+runs+e2e_report+checkpoints+
  eval+recovery+ocr_redacted 234；recording+execution_unit+window_guard
  **1368 passed / 1 skipped / 0 failed**。
- **实机说明**：纯离线渲染层修复，零目标机交互，无实机验收对象。
- 未提交/未推送。证据：`result-report/20261030_152951-trace-md-escape/`
  （BATCH-REPORT.md、round.diff、SHA-256 manifest、wide-regression.txt、
  mutation-log.md、注入演示 red-probe-demo.md）。
- 剩余：step_results `status` 字段未转义（当前来源为内部枚举，风险低，
  上游若透传外部字符串需补门）；redaction.py 对 trace.md 的源端脱敏
  执行路径审计（§6q 遗留另一项）列为下一批候选；多行同名消歧与
  key_command（产品决策）；受控实机多场景与商业发布未宣称。

## 6s. 2026-10-30 16:05 追加：证据元数据源端脱敏接线（§6q 遗留「redaction.py 执行路径审计」收敛）

- **审计（红测实证）**：Layer-1 `RedactionRegistry`（D16）在录制/回放生产路径
  是死代码 —— 唯一接线点 `build_plan_event` 无生产调用方。截图/受保护字段链
  完整（§6q 已锁），但 text 面缺两个门：`persist_screenshot` 对 text 规则只
  计数 fired 后**丢弃脱敏结果**、EvidenceRecord 携带原文 window_title/
  process_name；生产唯一调用方 `ReplayEvidenceRecorder.record` 从不传规则。
  含敏感串的窗口标题经 `SCREENSHOT_PERSISTED` 原文落盘 events.jsonl，
  trace.md provenance 行（markdown.py:223-225）将渲染原文 —— §6r 转义防注入
  不防泄露。D16 验收 #6（audit_artifacts 零命中）对回放证据链不成立。
- **修复**：`replay.py::ReplayEvidenceRecorder` 接入注册表（`redaction_registry`
  注入参数，默认 `default_registry()`），持久化前对 window_title/process_name
  过注册表；`evidence.py::persist_screenshot` text 命中时 EvidenceRecord 携带
  `[REDACTED]` 后的字符串，`redaction_rule_ids` 纳入 text 规则 id。不放宽
  HybridLocator、不动 ancestry、不改 golden schema。
- **回归**：新增 `test_evidence/test_evidence_metadata_redaction.py` 6 条
  （持久层脱敏+规则 id、clean 逐字对照、录制器 events.jsonl 零敏感串、
  audit_artifacts 全目录零命中、自定义注册表注入）。修复前 4 failed /
  2 passed（正向对照先绿，失败归因明确）。
- **双变异**：录制器门停用 → 3 failed；持久层门停用 → 1 failed；
  cp 恢复逐字节一致（终态 SHA `938731c6` replay.py / `eceaaa4f` evidence.py）。
- **命名空间陷阱（记录）**：pythonpath 同含 `agent/` 与 `./`，`agent/trace`
  以 `trace` 与 `agent.trace` 双名各加载一次（类身份不同），跨层测试必须
  统一 `agent.*` 命名空间，否则 `isinstance(TextRule)` 假失败。
- **宽回归**：evidence+replay+planner 380；recording 全量 1257 passed /
  1 skipped；execution+recovery+eval 226。全绿 0 failed。
- **实机只读探针**（零 GUI 动作）：tunnel 200、backend uia、catalog 1.0.1、
  locked=false 无残留。纯 agent 侧修复，无需部署。
- 未提交/未推送。证据：`result-report/20261030_154500-evidence-metadata-redaction/`
  （BATCH-REPORT.md、round.diff、mutation-log.md、sha256-manifest.txt、
  wide-regression-*.txt、live-probe-readonly.out）。
- 剩余：planner 事件与 write_case_trace_atomic 仍无生产调用方（启用时需同
  样接线）；OCR/普通输入文本分级脱敏（产品决策）；多行同名消歧与
  key_command（产品决策）；受控实机多场景与商业发布未宣称。

## 6t. 2026-10-30 16:20 追加：回放中断后 trace 目录缺终结事件（批次 5 恢复轮）

对任务清单第 5 项（断连、超时、取消后的恢复）与第 8 项（失败后不残留）审计
`replay_golden_trace` 的异常路径，发现并修复一条证据持久化缺口：

- **缺陷（红测实证）**：`run_case` 把类型化失败（`BackendUnavailable` /
  `StepMaterializationError`）吸收为 blocked 步骤，trace 正常终结。但**非类型化**
  异常（证据落盘 OSError、操作员 KeyboardInterrupt、未包 typed 的传输故障）会
  穿透 `run_case` 处理器、直接逃出 `_run_golden_trace` —— trace 目录永久停在
  「无 trace_completed / 无 trace_aborted」，证据文件与「写到一半死机」不可区分。
  红测：`exactly one terminal event expected, got []`。
- **修复**：`replay_golden_trace`（唯一对外入口，含 `end_product_run` 释放点）
  增加中断守卫：`except BaseException` → `trace_store.finalize(TRACE_ABORTED,
  {reason: "replay_interrupted", error_type: <异常类名>})` 后原样 re-raise；
  trace 已终结（如运行完成、仅 evaluation.json 落盘失败）则不重复终结；
  finalize 自身失败不吞原始异常。+23 行，文件为长期 WIP（改前 SHA `938731c6`，
  改后 `bf2aa529`，Edit-only，无 git checkout/restore）。
- **回归**：`test_replay.py` 追加 2 条 —— 中断守卫（注入非类型化 RuntimeError，
  断言恰好 1 个终结事件且为 trace_aborted、payload 齐全）与
  完成后失败不重复终结（mkstemp 注入 OSError，断言保持唯一 trace_completed
  且原始异常不吞）。修复前第一条红、第二条绿（先绿条目失败归因明确）。
- **双变异**：守卫停用（`if False`）→ 1 failed；`error_type` 字段劣化为常量
  → 1 failed；每次 cp 恢复 SHA 逐字节一致 `bf2aa529`。全程未用 git checkout。
- **宽回归**：recording + evidence + trace + execution_unit + hybrid_locator +
  cli_replay_errors **1519 passed / 1 skipped / 0 failed**（基线 1464 + 新增
  2 条 + test_trace 全套 53 条首纳入）。
- **实机只读探针**（零 GUI 写）：tunnel 200、recording stopped 无活跃会话、
  locked=false 无窗口锁残留、dump_tree 4 控件 0 缺 ancestry（部署态 ancestry
  契约在位）。本轮纯 agent 侧修复，无需目标端部署。
- 未提交/未推送。证据：`result-report/20261030_162000-interrupted-replay-trace-termination/`
  （round.diff、mutation-log.md、sha256-manifest.txt、wide-regression.txt、
  live-probe-readonly.out、evidence-sha256.txt）。
- 剩余：多行同名消歧与 key_command（产品决策）；受控实机多场景（含最小化拒绝、
  窗口移动帧一致性等已修项的单场景验收）与商业发布未宣称。

## 6u. 2026-10-30 16:51 追加：ancestry 部署态复验 + 吞输入编译诚实缺口（批次 1 审计轮）

按任务要求先重新核验 ancestry 信息不对称（上轮遗留的第 1–6 条），再推进下一最小批次：

- **ancestry 部署态复验（只读实机）**：tunnel 可达（edr-wd 3.4.2）、recording stopped
  无活跃会话、locked=false 无窗口锁残留；dump_tree 10 控件 0 缺 ancestry，
  层数随树深递增（nearest-parent-first 契约在位）。离线 19 条 ancestry 回归
  （closed-loop / empty-field-parity / six-details）全绿；6×Details 同链歧义
  `locator_ambiguous(6)` + `candidate_rects` 诊断行为由回归锁定不变。
  fingerprint 门（两侧哈希字段集不一致 + rect 不稳定 → 不作为匹配器）回归
  `test_recording_and_replay_fingerprints_do_not_agree_yet` 保持通过。
- **批次 1 审计（窗口锁/会话归属/同标题）**：梳理 `connect_target_ambiguous`
  （connect 多候选拒绝）、`window_selector_ambiguous`（root 选择歧义）、
  `bind_backend_lock` 唯一绑定 + generation 校验、capture 侧
  `test_two_windows_sharing_a_title_are_told_apart_by_handle`、
  面板侧 `test_focus_refuses_same_title_rebuilt_window`（同名重建拒跟随）、
  `test_capture_session_attribution.py` 20 条会话归属。55 条相关回归全绿，
  未发现需修复的缺口；本批次暂不动代码。
- **发现并修复：吞输入编译诚实缺口（批次 2/8 诚实性）**：录制文档持久化
  `recorderUiEvents`（用户瞄准目标但被录制器自带面板吞掉的输入）与
  `unidentifiedTargetEvents`（落在应用无法命名区域的点击），capture 侧注释明确
  「never silently dropped」，但 compiler（compiler.py 原第 889 行附近）只把
  `droppedPackets` / `correlationErrorCount` 转为 compile issue——后两个计数器
  不产生任何 issue，吞掉输入的录制可编译为 ready。修复：两计数器非零时追加
  `recording_recorder_ui_events` / `recording_unidentified_target_events`
  （compile issue → golden incomplete），与既有 `recording_packets_dropped`
  契约对齐。+14 行；文件为长期 WIP（navigation-merge 并行轨未触碰；
  改前 SHA `acea54ba`，改后 `337c64fe`，Edit-only，无 git checkout/restore）。
- **回归**：新增 `test_compile_swallowed_input_honesty.py` 3 条（两计数器各自
  → incomplete + 明确 issue code；全零 → 仍 ready）。修复前两条红。
- **双变异**：recorderUiEvents 门置零 → 1 failed；unidentifiedTargetEvents 门
  置零 → 1 failed；每次 cp 恢复 SHA 逐字节一致 `337c64fe`。
- **宽回归**：recording + evidence + execution_unit + hybrid_locator + eval
  **1469 passed / 1 skipped / 0 failed**（基线 1466 + 新增 3）。
- 结论分层：ancestry 闭环 = 代码级 + 部署态只读复验（dump_tree 契约在位）；
  6×Details 歧义 = 代码级锁定（语义上不可安全唯一回放，产品消歧待决策）；
  吞输入诚实缺口 = 代码级修复 + 失败回归 + 双变异。受控实机多场景、
  多行同名消歧、key_command（产品决策）与商业发布仍未宣称。
- 未提交/未推送。证据：`result-report/20261030_165100-ancestry-recheck-batch1-audit/`
  （dump-tree-probe-readonly.sse、probe-summary.txt、red-green-run.txt、
  round-diff-this-round-only.diff、wide-regression.txt、sha256-manifest.txt、
  versions.txt、head.txt、git-status.txt、compiler-pre-fix-sha.txt）。
- 剩余：多行同名消歧与 key_command（产品决策）；受控实机多场景（最小化拒绝、
  窗口移动帧一致性等已修项单场景验收）；OCR/普通输入文本分级脱敏（产品决策）；
  商业发布材料。批次 2–9 其余维度按任务清单继续最小批次推进。

## 6v. 2026-10-30 17:27 追加：录制租约孤儿窗口看门狗（批次 5/6/8 恢复轮）

**缺陷（审计发现）**：录制租约过期是**惰性的**——`RecordingSession._expire_before_operation`
只在下一次会话操作（ingest/heartbeat/stop/status/state）到达时执行。若 Agent
录制中断连（进程死/网络断）且操作员不再触碰录制 UI，**永远不会有操作到达**：
录制器浮窗与原生捕获钩子无限期滞留，会话永远停在 RECORDING，无任何 deadline。
service 层无重扫线程，indicator 的 poll 只处理 UI 命令，recorder_panel 的
heartbeat 是区域向导 valve——全库确认无既有看门狗。批次 5（断连恢复）、
批次 6（孤儿窗口）、批次 8（失败后不残留）各欠此项。

**修复**（`target/recording/session.py`，Edit-only，长期 WIP 文件；
改前 SHA `33fb2330`，改后 `e1fddc60`）：
- `start()` 成功后启动 daemon 守护线程
  `recording-lease-watchdog-<sessionId>`，每 0.25s 轮询注入的 monotonic 时钟，
  到期即走**同一条** `_expire_before_operation` 路径——reason 仍为
  `recording_lease_expired`，下游无需区分触发机制，编译诚实性不变。
- `_expire_before_operation` 触发时置 `_watchdog_stop`（懒路径/看门狗路径
  均置），看门狗下个 tick 退出。
- `stop()` 三个出口（并发停止汇合、失败、成功）均 `_shutdown_watchdog`
  （set + join≤2s，超时仅解除引用，daemon 线程不阻塞进程退出）；
  `start()` 失败路径本就未启动看门狗（`_watchdog_thread=None`，实测验证）。
- 附加边界实测：看门狗过期后再 stop() 返回同一文档（source 只 stop 一次，
  无双重 stop）；正常 stop 后长时钟推进不改变终态（reason 仍为 user）。

**回归**：新增 `test_case/test_recording/test_lease_watchdog.py` 4 条：
1. `test_abandoned_session_expires_itself_with_no_operations_at_all`——
   start 后**零操作零心跳**，仅时钟推进，会话自行 STOPPED、source/indicator
   均 stop、reason=recording_lease_expired、看门狗线程 join 干净（无泄漏）；
2. `test_watchdog_does_not_fire_while_the_lease_is_being_renewed`——
   0.25s 间隔心跳续租跨 3 个租约期不误杀（断言读 `_state` 而非 status()，
   因 status() 会走惰性路径使测试空转）；续租停止后同一看门狗终止捕获；
3. `test_watchdog_exits_when_the_session_stops_normally`——用户 stop 后
   看门狗退出，后续长时钟推进不改变终态；
4. `test_paused_session_is_still_under_the_lease`——PAUSED 仍在租约管辖
   （断言读 source.calls，避免 status() 空转）。
红-绿：修复前 4 红（另做空转校准：pre-fix 下用 status() 轮询的两条会被
惰性路径染绿，已改为 source/`_state` 断言）。测试内 join 断言初版误用
`Thread.join() is not None`（CPython join 成功返回 None）——测试自身
bug，修正后 4 绿。

**双变异**（均 cp 恢复逐字节一致 `e1fddc60`，无 git checkout）：
- 变异 1：`_start_watchdog` 置 return（看门狗不启动）→ 4 failed；
- 变异 2：看门狗循环内 `if False and self._expire_before_operation()`
  （线程在、但永不执法）→ 2 failed。

**宽回归**：recording + evidence + execution_unit + eval + execution
**1492 passed / 1 skipped / 0 failed**；hybrid_locator 23 passed。
含多个 1–30s 真实 lease 用例（test_management/test_ui_ack_and_result_store）
无互相干扰。

**实机只读探针**（11.26，零 GUI 变更）：tunnel 可达，
recording_status state=stopped、reason=user、无活跃捕获；
get_window_lock locked=false 无残留；tools/list 15 个录制/锁工具清单不变。
**注意**：本轮修复是 target 侧 session.py，**尚未部署**到 11.26 服务——
探针证明本轮改动前后目标无捕获/会话/锁残留，但**不能**演示看门狗实机行为。
看门狗实机验收（Agent 断连 → 目标自动过期 → 无残留）留给有监督的受控轮；
无人值守轮保持只读。

结论分层：本项 = 代码级修复 + 离线回归 + 双变异 + 部署前残留只读探针；
**不是**受控实机验收。商业发布门槛未变。
未提交/未推送。证据：`result-report/20261030_172000-recording-lease-watchdog/`
（branch/head/git-status、session-pre/post-fix-sha、round-diff-session.diff、
red-run/green-run/mutation1-run/mutation2-run、wide-regression、
live-probe-readonly.out、sha256-manifest、test 文件副本）。
- 剩余：看门狗实机部署+受控验收（本项）；多行同名消歧与 key_command（产品
  决策）；受控实机多场景（最小化拒绝、窗口移动帧一致性等已修项单场景验收）；
  OCR/普通输入文本分级脱敏（产品决策）；商业发布材料。批次 2–9 其余维度
  按任务清单继续最小批次推进。

## 6w. 2026-10-30 18:0x 追加：弱身份动态标签诚实编译（批次 4：动态文本）

**任务**：定时轮按上轮遗留清单先重新核验 ancestry（37 条相关回归全绿，
ancestry/parity/watchdog/honesty 闭环在当前 WIP 树上成立），随后从批次 4
（动态文本/无强 ID）选最小缺口推进。

**缺陷（代码级，新发现）**：`agent/locator/hybrid.py:105-112` 自己注释
"Recorded labels (notably clocks, log rows and status text) may legitimately
change by replay"，且只对**强 ID** 控件放弃 name 比较——弱身份控件的 name
就是回放身份。但编译器弱身份门（`_weak_selector_candidate_count`）只在
**事件绑定单帧**上数候选：若同一结构位置（controlType+ancestry）在录制中
另一帧显示过不同标签（时钟、状态行），该步仍编译 `ready`；回放时观测中
name 不同 → `candidate_count=0` → `replay_target_not_found` 必然 blocked。
录制端 `ControlInventory` 本就持久化了去重的多帧 control_snapshots——编译器
从未交叉比对。这是 11.26 实机回放失败（编译 ready、回放 blocked）的同族
盲区，属批次 4「动态文本」。

**修复**（`agent/recording/compiler.py`，Edit-only，长 WIP 文件；
改前 SHA `337c64fe`，改后 `3635e6d1`；并发核查：compiler.py mtime 17:07
为本轮 §6u，无同文件并行任务，唯一运行中会话在 edr-cloud-recorder）：
新增 `_weak_label_volatile`（compiler.py:625-672）：当同一窗口（与
`_weak_selector_candidate_count` 同一套 `_frame_window_matches` 窗口匹配）
任一 **complete** 帧在**事件时刻或之前**显示同结构位置不同文本 →
`compile_weak_identity_volatile_label` + incomplete。三个安全过滤：
- complete=True 帧才作数（截断树缺失替代标签不构成易变证据）；
- 事件**之前**的帧才作数——事件后的帧是流程自身造成的（Start 点击后
  变 Stop），回放解析 selector 发生在任何动作之前；
- 其他窗口的同形树不构成证据。
挂接点：弱身份唯一（candidate_count==1）分支的 elif，与
`compile_weak_identity_tree_undercount` 并列，单帧唯一性计数逻辑不变。
自审发现并修正：初版扫全部帧（含事件后）会误杀 Start→Stop 类合法标签
变化，补 `capturedAtMs > event_ms` 守卫 + 负例测试。

**回归**：新增 `test_case/test_recording/test_weak_identity_volatile_label.py`
6 条：
1. 跨帧弱身份标签变化 → incomplete + volatile_label issue + case 级传播；
2. 强 ID（事件目标带 automationId）标签变化仍 ready（定位器按设计放弃
   name 比较）；
3. 全帧标签稳定弱身份仍 ready（无误杀）；
4. 仅不完整帧含替代标签 → 不触发（截断树无信号）；
5. 其他窗口帧 → 不触发；
6. 事件后帧（Start→Stop）→ 不触发。

**红-绿**：预轮重建文件（SHA c2d37412，与记录的改前 SHA 337c64fe 仅差
本轮自增函数自身 docstring——自审守卫发生在 SHA 记录之后，行级隔离
diff 已核：624a625,671 + 870a918,927 两段为本轮全部改动）→
1 红（ready!=incomplete）/ 5 绿（含事件后负例，守卫本就不存在）；
修复后 6/6 绿。证据 `red-run-preround.txt`。

**三变异**（cp 逐字节恢复，最终 SHA 3635e6d1 核验；恢复完整性事故与
纠正已记录 mutations.md——首次恢复误用了守卫前备份，SHA 校验当场
发现并纠正）：
- 变异 1：门禁禁用 → 1 红（volatile 正例）；
- 变异 2：complete 过滤禁用 → 1 红（负例 4）；
- 变异 3：窗口匹配禁用 → 1 红（负例 5）。

**宽回归**：recording + evidence + execution_unit
**1456 passed / 1 skipped / 0 failed**（含守卫修正后终态）。

**结论分层**：本项 = 代码级修复 + 离线红绿 + 三变异。**不是**实机验收；
动态标签控件的受控实机单场景（录制时钟/状态行 → 编译 incomplete →
重新以稳定身份捕获 → ready → 回放通过）留给有监督受控轮。商业发布
门槛未变。未提交/未推送。

- 剩余：多行同名消歧与 key_command（产品决策）；受控实机多场景（含
  本项动态标签单场景）；OCR/普通输入文本分级脱敏（产品决策）；商业
  发布材料。批次 2–9 其余维度按任务清单继续最小批次推进。

## 6x. 2026-10-30 18:4x 追加：record stop 指引与编译器 issue 词汇漂移修复（批次 1 可观测性收口轮）

**重验上一轮登记项**：ancestry 信息不对称缺陷已在 §6c–6w 闭环——本轮以证据
重验：ancestry 回归 15/15 绿；全仓基线 2842 passed / 0 failed；
11.26 只读探针（MCP v3.4.2 可达、locked=false、无会话残留、无孤儿窗口）；
上一轮遗留 field-parity 4 红已被拥有方修复（6/6 绿）。

**本轮缺陷（代码实证）**：`recording_stop_advisories`（agent/cli.py，
2026-10-28 13:03 并行轨 WIP）落后于编译器词汇——7 个码无专属指引且不在
known（`compile_weak_identity_tree_undercount`、`compile_weak_identity_volatile_label`、
`recording_recorder_ui_events`、`recording_unidentified_target_events`、
`compile_assertion_missing_expected`、`compile_action_unsupported`、
`compile_region_container_unresolved`），操作者只见「未登记」兜底；另有 2 码
（`compile_weak_or_ambiguous_identity`、`compile_weak_identity_unresolved`）
有指引却不在 known，指引+兜底双重误报。

**修复**（agent/cli.py，Edit-only；并发核查：WIP mtime 10-28 13:03 未动，
唯一运行会话在 edr-cloud-recorder；改前 WIP SHA `283a112b`，改后 `78f5f539`）：
7 个新码各给专属中文修法指引；known 补齐 9 码使指引与兜底互斥；
未知码「以上游为准」契约不变。

**回归**：新增 `test_stop_advisory_vocabulary_parity.py` 12 条（含源码字面量
提取的全量词汇锁）。红（prefix 等效态）11 红/1 过；修复后 12/12 绿，
advisory 相关 42 passed。

**四变异**（cp 逐字节恢复，SHA 每次核验）：禁 tree_undercount → 1 红；
禁 volatile → 2 红；known 移除两码 → 4 红；禁兜底 → 2 红（含既有契约）。

**宽回归**：recording+execution_unit+evidence **1468 passed / 1 skipped**；
全仓 **2854 passed / 1 skipped / 0 failed**（0 回归）。

**结论分层**：代码级修复 + 离线红绿 + 四变异。CLI 输出层，不改编译/回放
安全语义；实机展示留受控轮顺带核验。未提交/未推送。
证据 `result-report/20261030_184242-stop-advisory-vocabulary-parity/`。

- 剩余：多行同名消歧与 key_command（产品决策）；受控实机多场景（动态标签、
  最小化拒绝、看门狗部署——需监督在场）；OCR/文本分级脱敏（产品决策）；
  商业发布材料。批次 2–9 其余维度按任务清单继续最小批次推进。

## 6y. 2026-10-30 19:0x 追加：收集链路断连恢复（批次 5 恢复轮）

**重验上一轮登记项**（证据重验，非自报）：ancestry 闭环 15/15、field-parity
6/6、volatile-label 6/6、advisory 词汇 12/12、看门狗 4/4 合计 43/43 绿；
11.26 只读探针（MCP 可达、state=stopped 无活跃会话、租约已释放、pending
清单可列——保留收集语义在线）。

**本轮缺陷（代码审计实证）**：`run_recording_ui_bridge` 的重连预算只包住
`recording_status` 心跳；收集链路（`get_recording_result`、逐帧
`get_recording_capture` 取证）在循环外裸跑——隧道在收集期抖动一次即把整个
桥打死，操作者拿不到产物路径，与心跳路径同一类故障却无同一恢复语义。目标端
数据不丢（pending 保留 + §6v 看门狗兜底），但 Agent 侧收集链路是批次 5
恢复维度的真实缺口。并发核查：ui_bridge.py/collection.py 为未跟踪并行轨
WIP（mtime 10-23 静置 7 天），唯一运行会话在 edr-cloud-recorder，无同文件
冲突；改前记录 SHA `6b8e78dd…`。

**修复**（agent/recording/ui_bridge.py，Edit-only）：新增 `_RetryingAgent`
包装器——每次 call_tool 独立连续失败预算，语义与心跳循环一致（预算内重试、
耗尽抛 `UIBridgeError`）；两处 `_collect_one` 调用点（历史 pending 收集 +
stopped 会话收集）统一走包装器。收集门槛语义不变：有取证缺口仍不 mark。

**回归**：新增 `test_collection_reconnect_recovery.py` 3 条（瞬态抖动恢复
且产物一致、持续失败预算耗尽抛错且有限次尝试、恢复路径不改 mark 门槛）。
红 3/3 → 绿 3/3。双变异（cp 逐字节恢复 + SHA 每次核验 `36ec4ff0…`）：
预算置 0 → 3 红；mark 门槛移除 → 1 红（精确命中门槛用例）。

**宽回归**：桥相邻套件 52 passed；全仓 **2857 passed / 1 skipped /
0 failed**（基线 2854 + 3 新，0 回归）。

**结论分层**：代码级修复 + 离线红绿 + 双变异。**未做实机验收**——断连恢复
的受控实机场景（录制中拔隧道→收集期恢复）需监督在场，列入下一受控轮；
11.26 仅只读探针。未提交/未推送。
证据 `result-report/20261030_190500-collection-reconnect-recovery/`（修复态
文件副本+SHA、红/绿/变异逐段输出、目标探针 JSON、HEAD/版本）。

- 剩余（不变）：多行同名消歧与 key_command（产品决策）；受控实机多场景
  （动态标签、最小化拒绝、看门狗部署、**本轮断连恢复**——需监督在场）；
  OCR/文本分级脱敏（产品决策）；商业发布材料。

## 6z. 2026-10-30 19:3x 追加：product-run 租约清理恢复语义（批次 5 恢复轮）

**重验上一轮登记项**（证据重验）：ancestry 闭环 + field-parity +
collection 断连恢复合计 24/24 绿。

**本轮缺陷（代码审计实证）**：`replay_golden_trace` 的 `finally` 块裸调用
`end_product_run()` 释放目标端 GUI 租约——与 §6y 收集链路同类的"裸调用
无恢复语义"缺口，但后果更重：finally 中的清理异常会**掩盖回放的真实
错误**（§6t 守住的原始异常仍被 finally 异常顶掉），或把**已全部通过的
回放报告成失败**。目标端有惰性过期兜底（`product_policy.py::
_expire_if_needed`），故 Agent 侧清理预算耗尽后放弃是安全的——前提是
失败被记录而非重抛。并发核查：replay.py/runtime.py 的未提交 WIP 为
前序定时轮与并行轨产物（mtime 16:42/19:04），唯一运行会话在
edr-cloud-recorder；改前 SHA `699f619c…`/`55ccb613…` 逐字备份核验。

**修复**：`agent/recording/replay.py` 新增 `_release_product_run_lease` —
1s×3 连续失败预算（与 §6y 收集链路同语义），预算内重试、耗尽
`logger.warning` 后**返回不抛**；`replay_golden_trace` finally 与
`build_replay_runtime` 装配失败路径统一走包装（装配路径保留"原始装配
失败是 actionable error"语义并补齐重试）。

**回归**：新增 `test_product_run_lease_cleanup_recovery.py` 3 条（瞬态
失败重试后回放仍成功、预算耗尽不翻转已通过结果、评估持久化 OSError
不被清理异常掩盖）。红 3/3 → 绿 3/3。三变异（cp 逐字节恢复 + SHA 每次
核验 `699f619c…`）：裸调用恢复 3 红、耗尽后重抛 2 红（精确命中）、
预算置 0 3 红。

**宽回归**：相邻套件 111 passed；全仓 **2860 passed / 1 skipped /
0 failed**（基线 2857 + 3 新，0 回归）。

**结论分层**：代码级修复 + 离线红绿 + 三变异。**未做实机验收**——
回放清理期拔隧道的受控场景需监督在场，列入下一监督轮（与 §6v/§6o/
§6w/§6x/§6y 实机项一并成批）。未提交/未推送。
证据 `result-report/20261030_193000-product-run-lease-cleanup-recovery/`
（修复态副本+SHA、红/绿/变异逐段输出、全仓输出、HEAD/版本）。

- 剩余（不变）：多行同名消歧与 key_command（产品决策）；受控实机多场景
  （动态标签、最小化拒绝、看门狗部署、断连恢复 §6y、**清理恢复 §6z**——
  需监督在场）；OCR/文本分级脱敏（产品决策）；商业发布材料。

## 7a. 2026-10-30 19:5x 追加：CLI record stop 收集链断连恢复 + ancestry 实机重验（批次 5 恢复轮续）

**重验上一轮登记项（含实机只读探针）**：9 个先前轮回归套件 54/54 绿。
11.26 只读 dump_tree（max_depth=30，无 GUI 变更）：84/84 控件带非空
ancestry——ancestry 生产端契约实机有效；6×同名 Details 实机确认共享
字节级相同祖先链（distinct chains=1，Table[id=None] 末级）——
candidate 0→6 → locator_ambiguous(6) 显式歧义，安全行为与设计一致。
分层：代码级兼容 + 只读实机观察，非闭环验收。

**本轮缺陷（同族审计实证）**：`agent/cli.py` `record stop` 分支裸传 agent
给 `collect_recording_artifacts`——逐帧 `get_recording_capture` 无重连
恢复语义（§6y 修了 UI 桥路径，CLI 主路径漏掉）。一条隧道抖动 →
`recording_persistence_failed`，产物留目标端 pending，无重试。CLI 路径
收集链是逐帧 get_recording_capture（recording 在 stop_recording 载荷内，
无独立 get_recording_result）。

**修复**：`agent/recording/collection.py`（未跟踪并行轨 WIP，改前 SHA
`934d4760…`）新增 `CollectionRetryError` + `RetryingCollectionAgent`——
每 call_tool 独立连续失败预算（interval=1.0×retries=3，与 §6y/§6z 同
语义），耗尽抛 CollectionRetryError 不静默吞。`agent/cli.py`（前序 WIP
SHA `78f5f539…`，Edit-only）stop 分支包 retrying，except 集合加
CollectionRetryError。改前记录 SHA；ui_bridge/replay/runtime SHA 未变
（36ec4ff0/699f619c/55ccb613）。

**回归**：新增 `test_cli_stop_collection_reconnect.py` 3 条（瞬态抖动
重试后产物完整、持续失败预算耗尽结构化失败、§6y 桥语义锁）。修复前
稳定红 2/2 → 绿 3/3。双变异（cp 逐字节恢复 + SHA 核验 `363068dc…`）：
裸调用恢复 2 红、预算置 0 2 红。

**宽回归**：录制套件 1293 passed / 1 skipped（§6y 3 条继续绿）；全仓
**2863 passed / 1 skipped / 0 failed**（基线 2860 + 3 新，0 回归）。

**结论分层**：代码级修复 + 离线红绿 + 双变异 + 只读实机探针。**未做
实机验收**——CLI stop 收集期拔隧道的受控场景需监督在场，列入下一监督轮
（与 §6v/§6o/§6w/§6x/§6y/§6z 实机项一并成批）。未提交/未推送。
证据 `result-report/20261030_195000-cli-stop-collection-reconnect/`
（探针原始输出、红/绿/变异输出、修复态副本+SHA、HEAD/版本、范围说明）。

- 剩余（不变）：多行同名消歧与 key_command（产品决策）；受控实机多场景
  （动态标签、最小化拒绝、看门狗部署、断连恢复 §6y、清理恢复 §6z、
  **CLI stop 断连恢复 §7a**——需监督在场）；OCR/文本分级脱敏（产品决策）；
  商业发布材料。

## 7b. 2026-10-30 20:1x 追加：批次 9 文档部分——根 README + 录制/回放用户指南（定时轮）

**上一轮任务书 6 项重验**：ancestry 闭环各回归本轮重跑 25/25 绿；全仓 2863/1/0。
结论不变：observation 生产端契约已修、HybridLocator 未放宽、6×Details 显式歧义、
fingerprint 保持禁用（回归锁定）。

**本轮交付（批次 9 文档部分，唯一可无人值守批次）**：
- 新增根 `README.md`（此前不存在）：安装/配置/验证/录制回放快速开始/安全注记。
- 新增 `references/recording.md`：record 全子命令、断言编辑器 12 类型表、
  stop 产物 7 类、编译诚实门槛逐条、回放 4 道硬门 + 3 模式、鲁棒性边界、
  脱敏与 live 门——全部对照 agent/cli.py、artifacts.py、models.py、hybrid.py 核实。
- `SKILL.md` 任务表 + References、`docs/README.md` Daily Use 表加入入口。
- 验证：CLI help 实跑比对；就绪检查脚本对 16 条真实 golden 实测（4 ready /
  12 incomplete 逐条 issues）；4 文件 SHA-256 留存。
- 纯文档 + 索引行，0 行运行时行为改动；126 条既有 WIP 全保留。

**结论分层**：商业发布材料的文档部分交付（代码级产物 + 离线验证）。不宣称商业可用。

**下一批次**：批次 9 其余——packaging/pyinstaller 契约的 mcp.spec/build/smoke 脚本
（纯新增）。需监督轮与产品决策项不变（§6o/6v/6w/6x/6y/6z/7a 实机合并轮；
多行同名消歧、key_command、OCR 分级脱敏）。

证据 `result-report/20261030_201500-batch9-release-materials/`。未提交/未推送。

## 7c. 2026-10-30 20:4x 追加：批次 9 其余——packaging/pyinstaller 契约（定时轮）

**上一轮任务书重验**：ancestry 闭环 + fingerprint 锁 + 6 个前序轮回归
套件本轮重跑 15+31 绿（见 result-report/00-contract-recheck.txt）。结论不变：
observation 生产端契约已修（§6u/§7a 实机 84/84 探针）、HybridLocator 未放宽、
6×Details 显式歧义、fingerprint 禁用锁定。

**本轮交付（批次 9 打包契约，纯新增文件 + 2 行 .gitignore）**：
- `packaging/pyinstaller/mcp.spec`：入口 `agent/cli.py`（与 `edr-wd`
  console-script 同一 main，根 agent/+target/ 唯一事实源，无重复包树）；
  输出名 `mcp`（Windows 得 mcp.exe）；onedir COLLECT（产物可发布前审计）；
  路径全部由 SPECPATH 派生（无盘符字面量）。
- datas：`target/` 全量部署载荷（冻结环境无 git 时 scp_dir_to 回退枚举需要）
  + `config/targets.example.json`（仅文档示例）。双重过滤：source 过滤
  （endswith targets.local.json）+ post-Analysis `_is_forbidden`（tests/
  tmp/logs/screenshots/__pycache__/.git/.pyc/credentials）。
- hiddenimports 覆盖 importlib 动态派发（agent.lifecycle.{windows,macos}）
  与懒加载（maa_export、generate_pytest）；excludes 剔除 agent 端仅出现在
  远端验证命令字符串里的目标侧 GUI 依赖（pywinauto/PyAutoGUI/tkinter/pyobjc），
  Pillow 保留（agent 录制代码 import PIL——已逐一核实无目标侧 GUI 库的
  agent 模块级 import）。
- `build.sh`/`build.ps1`：--clean 支持，输出 build-packaging/（新增
  gitignore 条目），缺 PyInstaller 时结构化报错。
- `smoke.sh`/`smoke.ps1`：5 项——--help 启动、config --list 免崩溃、
  无写探测（status --target 不触发 deploy/install）、products 离线列表、
  产物排除断言（无 targets.local.json/logs/pid/.git/__pycache__/tests）。
- `packaging/pyinstaller/README.md` 更新为已交付契约说明。

**回归**：新增 `test_case/test_recording/test_packaging_contract.py` 16 条
（目录契约、spec AST/datas/双重过滤/portable 路径/onedir、smoke 无写探测、
build 脚本无 deploy/install、stub 执行真实跑 spec 过滤逻辑断言 108 项
datas 0 违禁 + 7 类必需部署文件在包、hiddenimports 动态派发覆盖）。
三变异均精准红：M1 纵深过滤集清空→filter-names 红；M2 source 过滤移除→
source-filter 红（初版测试 grep 弱点——字符串仍存在于纵深集——已强化为
锁定 endswith 过滤构造本身后红绿闭环）；M3 _is_forbidden 禁用→
grep 类测试全绿暴露盲区→新增 stub 执行测试后精准红。恢复后 16/16 绿。

**宽回归**：录制套件 1309 passed / 1 skipped（基线 1305 + 16 新，0 回归）；
全仓 **2879 passed / 1 skipped / 614 deselected / 0 failed**（基线 2863 + 16 新）。

**结论分层**：代码级打包契约交付 + 离线 stub 执行验证 + 三变异。
**未做**：真实 PyInstaller 构建（本机无 PyInstaller；实机 mcp.exe 构建与
smoke 五项需 Windows/CI 或受控监督轮执行——新增 `pyinstaller` 依赖后
`packaging/pyinstaller/build.sh` 即可）。不宣称商业可用。

证据 `result-report/20261030_201900-batch9-packaging-contract/`
（契约重验、修复态 spec+SHA、stub 执行验证输出、三变异红绿输出、
环境/版本/SHA 清单、全仓统计）。未提交/未推送。

- 剩余：真实构建+smoke 执行（需 Windows 环境）；多行同名消歧与
  key_command（产品决策）；受控实机多场景批次（§6o/6v/6w/6x/6y/6z/7a，
  需监督在场）；OCR/文本分级脱敏（产品决策）。

## 7d. 2026-10-30 21:0x 追加：批次 10——真实 PyInstaller 构建+smoke 执行 + CI 工作流（定时轮）

**上一轮重验**：§7c 契约测试 16/16 绿（彩排轮内含）；无并行 edr-wd 会话
（仅 edr-cloud-recorder 会话活跃，不同仓库）。config/targets.local.json
彩排期间临时移走、事后 SHA-256 核验字节一致恢复。

**本轮交付**：
- **真实构建验证（POSIX 侧闭环）**：pyinstaller 6.22.3 装入项目 .venv
  （仅新增 altgraph/macholib/pyinstaller/hooks-contrib 4 包）；
  `build.sh --clean` exit 0 → 46MB onedir bundle、171 文件、target 载荷
  107 文件、0 违禁内容；`smoke.sh` 5/5 PASS。mcp SHA `d6e929e4…`。
  §7c 登记的"真实构建"在 macOS 侧完成（Windows mcp.exe 仍需 CI/受控轮）。
- **CI 工作流** `.github/workflows/ci.yml`（纯新增）：macOS job = 干净
  安装 + 全量离线套件 + 构建 + smoke；Windows job = packaging 契约回归 +
  mcp.exe 构建 + smoke。不跑 integration/e2e（自带 skipif）；不部署。
  未提交/未推送。
- **CI 彩排抓到真实缺口**：`test_case/conftest.py` 顶层 `import httpx`，
  pyproject.toml 未声明（共享 venv 里是传递依赖，干净环境必挂）。修复：
  test extra 加 `"httpx>=0.27"`。变异验证：干净 venv 移除声明 → conftest
  导入即红；恢复后字节一致（SHA `74e78136…`）。
- **CI 等价全量彩排**：一次性 venv、无本地 config、离线——
  **2879 passed / 1 skipped / 614 deselected / 0 failed**，与共享 venv
  基线一致；随后构建+smoke 再 5/5 PASS。
- **抓到并修复真实 flaky**（与本轮改动无关，CI 每轮全量跑故属本批次）：
  `test_tunnel_concurrent_streams.py::test_persistent_stream…` 两个
  `_start_forward` worker 竞争 open_channel 使 peers[i]↔clients[i] 顺序
  不定，实测失败率约 10–20%。修复：逐个启动并等待 `len(peers)==i+1`，
  映射确定化。修复前 11 次 2 红；修复后 50/50 绿。改前 SHA `94acffae…`。
- `packaging/pyinstaller/README.md` 加 CI 段落。

**结论分层**：代码级交付（CI/依赖声明/flaky 修复/文档）+ 真实构建验证
（POSIX 侧已验收；Windows 侧待 CI 首跑或受控轮）。不宣称商业可用。

证据 `result-report/20261030_210000-real-build-and-ci/`。未提交/未推送。

- 剩余：推送后观察 CI 首跑（Windows ps1 首次真实执行）；受控实机多场景
  批次（§6o/6v/6w/6x/6y/6z/7a，需监督在场）；多行同名消歧与 key_command
  （产品决策）；OCR/文本分级脱敏（产品决策）。

## 7e. 2026-10-30 21:3x 追加：批次 11——回放入口窗口获取拒绝语义回归（定时轮）

**上一轮重验**：§7d 基线 2879/1/0 复跑一致；唯一活跃会话为
edr-cloud-recorder（不同仓库）；edr-wd 业务文件 mtime 早于本轮，无并发。

**缺口**：`runtime.py::acquire_target_window`（connect →
lock_window(strict, activate) → verify_window_lock，任一 ok=False →
`replay_target_unavailable`）零直接测试。它是批次 1（同标题窗口不得
误操作——strict 锁拒绝歧义）与批次 5（目标窗口消失立即拒绝）在获取
阶段的交集；CLI replay 与生成 pytest 共用（cli.py:1149）。

**交付（纯新增）**：`test_case/test_recording/test_acquire_target_window.py`
6 条——strict+activate 参数锁定、connect 失败后续零调用、lock 失败不
verify、verify 失败拒绝、非 dict 拒绝、传输异常原样上抛不吞。
**未改任何业务文件**；runtime.py 三变异均为临时验证后 SHA 恢复
（`55ccb6134945b7e6`）。

**变异 3/3 精准红**：M1 strict=False→1 红；M2 错误码→4 红；
M2b 拒绝条件失效→4 红（含"失败后不得继续调用"）。

**宽回归**：新 6/6 绿；相关（test_replay、test_product_run_lease_cleanup_
recovery）95/95 绿。全轮无业务代码改动，无回归面。

**结论分层**：代码级交付 + 离线变异验证。非实机验收；不宣称商业可用。
证据 `result-report/20261030_213000-acquire-window-refusal-semantics/`。
未提交/未推送。

- 剩余（不变）：推送后观察 CI 首跑；受控实机批次（§6o/6v/6w/6x/6y/6z/7a
  + 本条 acquire 拒绝场景，一次监督轮合并）；多行同名消歧与 key_command、
  OCR 分级脱敏（产品决策）。后续离线候选：build_replay_runtime 层
  acquire 失败无 lease/trace 泄漏的边界锁定。

---

## §7f 装配失败残留（批次 12，2026-10-30 22:15 定时轮）

**缺口**：`build_replay_runtime` 旧装配顺序 acquire（锁窗）→ trace_store.open()
→ begin_product_run → 装配。保护只有最后一段 try（仅租约）：
1. legacy 路径（无 product-run 能力）acquire 锁窗后装配失败无人 unlock —— 锁
   滞留后端（lock_window 内部 verify 失败时锁已写入）；
2. trace_store.open() 在所有保护之外，任何后续失败留下无终结事件孤儿 trace
   （永远 "in progress"）；
3. begin 失败（run_token=None）同样不 finalize。

**修复（Edit-only，runtime.py pre-SHA `55ccb613…` → post `90d85f76…`）**：
acquire 后整段纳入 try/except BaseException；失败路径 `_release_acquired_target`
——product-run 租约走 `_release_product_run_lease`（§6z 语义）；legacy 显式
unlock_window（仅当确实 legacy acquire 过；product-run 入口无 legacy 锁，
unlock 属越权操作，M3 锁定）；两种路径都把已打开 trace finalize 为
trace_aborted（finalize 失败不掩盖原始错误）。装配拆
`_begin_product_run_if_available` / `_assemble_replay_runtime`，行为等价。

**回归（纯新增）**：`test_case/test_recording/test_replay_runtime_assembly_residue.py`
4 条——legacy 装配失败必须 unlock、legacy 装配失败必须 finalize trace、
product-run begin 失败拒绝且不越权 unlock、begin 失败也 finalize trace。
**三变异全精准红**：M1 禁 legacy unlock→1 红；M2 禁 finalize→2 红；M3 去越权
守卫→1 红。恢复后 SHA 复核一致。

**回归范围**：录制目录 1319 passed/1 skipped；全仓 **2889 passed / 1 skipped /
0 failed**（基线 2885 + 4，Δ=+4 精确，0 回归）。

**结论分层**：代码级兼容（offline 回归 + 突变验证）。非实机验收；装配失败
路径实机复现需受控注入，留待受控轮。证据
`result-report/20261030_221500-replay-runtime-assembly-residue/`。未提交/未推送。

- 剩余（不变）：推送后观察 CI 首跑；受控实机批次（§6o/6v/6w/6x/6y/6z/7a
  一次监督轮合并）；多行同名消歧与 key_command、OCR 分级脱敏（产品决策）。

## §7g verify 拒绝后窗口锁残留（批次 13，2026-10-30 22:39 定时轮）

批次 12 "How to apply" 候选 (a)：后端级回归锁定 lock_window 的
"verify 被拒但锁已写入" 残留。缺口确认：`lock_window` 先写
`self._window_lock = lock`（windows:291 / macos:558），再
`verify_window_lock(activate)`；verify 返回 ok=False 时锁字段从不清除 ——
`get_window_lock` 继续报 `locked=True`（status 遥测虚报"已锁定"），且前台
后来切回匹配时一次后续 verify 会把这把调用方从未确认过的锁复活。CLI
（cli.py:743）、ui_controller（录制所有权链）、replay legacy 路径均以
ok=False 放弃，无人 unlock。批次 12 只护住 replay 装配段 teardown，后端
本身的账面未护。

**修复（最小 Edit，两后端对称）**：verify 拒绝时 `self._window_lock = None`
并返回 `{"ok": False, code, error, lock, verify}` —— 拒绝详情与尝试的锁
身份保留（P0.2 `TestLockRecordsHandle` 契约：失败响应必须含 lock 字段），
仅清后端账面。成功路径、unlock 对账、criteria-mismatch 提前拒绝（本来
就不写锁）全部不变。pre-SHA windows `bd227d48…` / macos `9f95f62c…` →
post `f4e573e6…` / `05030583…`。

**回归（纯新增）**：`test_case/test_recording/test_lock_verify_refusal_residue.py`
7 条 —— 双后端同语义：verify 拒绝清锁（windows 走 connected=匹配 /
active=不匹配的分离假体复现真实"锁成验败"路径）、成功保留锁、unlock 对账、
复活路径账面锁定。**四变异全红**：M1 禁清锁→3 红；M2 win 去响应 lock 身份
→P0.2 既有测试红（契约交叉锁定）；M2b macos 去详情→1 红（中间形态时）；
M3 成功也误清→4 红。全部 cp 恢复 SHA 复核一致。

**回归范围**：lock 相关定向套件 156 passed；全仓 **2896 passed / 1 skipped /
0 failed**（基线 2889 + 7，Δ=+7 精确，0 回归）。

**结论分层**：代码级兼容（offline 回归 + 突变验证）。非实机验收。实机上
"verify 真实被拒"（RDP 前台抢夺）注入属受控场景，与 §6o/6v/6w/6x/6y/6z/7a
合并进同一监督轮。证据
`result-report/20261030_223943-lock-verify-refusal-residue/`。未提交/未推送。

- 剩余（不变）：推送后观察 CI 首跑；受控实机批次（§6o/6v/6w/6x/6y/6z/7a
  + 7g 注入，一次监督轮合并）；多行同名消歧与 key_command、OCR 分级脱敏
  （产品决策）。

## §7h connect 换目标时旧窗口锁残留（批次 14，2026-10-30 23:1x 定时轮）

批次 13 "How to apply" 候选 (a)：`connect` 不重置上一会话的
`_window_lock`。缺口确认：所有权链 connect → lock_window → verify
（录制 `ui_controller.py::_run_ownership_chain`；回放 legacy
`runtime.py::acquire_target_window`）在 lock_window 成功后调用方
崩溃/放弃且未显式 unlock 时，旧锁留在后端进程；下一次 `connect`
成功换绑新窗口后旧锁仍生效 —— `get_window_lock` 报 `locked=True`
但身份指向上一个窗口（遥测对当前会话说谎），后续 verify 可因前台
恰好匹配旧锁身份而复活从未被本会话确认过的锁；新链 lock_window
写入前 `_ensure_window_lock` 拿旧锁判定，旧窗口在前台时注入进入
错误的所有权上下文。§7g 只堵了 verify 拒绝一条路，connect 换绑
这条路未堵。

**修复（最小 Edit，两后端对称）**：`connect()` 真实尝试连接前清除
既有锁；成功 → result 附 `released_window_lock`
（handle/pid/title_re/process_name）对账；失败/异常 → 恢复原锁
（只有真实换绑丢锁；参数校验拒绝不动账）；macos 把原 dispatch 抽为
`_connect_dispatch`（行为等价，try/except BaseException 恢复）。
安全论证：无锁时指针注入在 WindowGuard 边界 fail-closed
（`_guarded_pointer` 空 snapshot → `WindowIdentity.from_native` 抛错），
清锁绝不放宽授权；产品路径 `connect_window_identity` 绕过 connect()
且紧随 lock_window 重建所有权，不受影响（回归专项锁定）。pre-SHA
windows `f4e573e6…` / macos `05030583…`（= §7g post，文件未被动过）
→ post `830c219a…` / `fb2c543c…`。

**回归（纯新增）**：`test_case/test_recording/test_connect_resets_stale_window_lock.py`
9 条 —— 双后端同语义（成功 connect 清旧锁、对账 released 身份、失败
connect 不动账）、无参参数校验拒绝不动账、清锁后 connect→lock→verify
链不变、connect_window_identity 不在此清锁（产品路径）。**先红后绿**
（修复前 5 红，均在成功路径）。**四变异精确红**：M1b win 清除禁用→2 红；
M2 win 丢对账→1 红；M3 macos 清除禁用→1 红；M4 win 失败恢复删除→1 红。
全部 cp 恢复 SHA 逐字一致。

**次生修复**：首版用 `self._window_lock` 直读，全仓跑出
`test_scroll/test_connect_criteria.py` 6 个既有测试红（直接构造 backend
不走 `__init__` → AttributeError）→ 改 `getattr(self, "_window_lock", None)`
（同 §7g macos 后端防御读法），connect_criteria 7/7 恢复绿。

**回归范围**：ancestry 闭环重验 37/37 绿（ancestry×2 + field-parity×2 +
batch-12/13 回归）；connect_criteria 7/7；全仓 **2905 passed / 1 skipped /
0 failed**（基线 2896 + 9，Δ=+9 精确，0 回归）。

**结论分层**：代码级兼容（offline 回归 + 突变验证）。非实机验收。
实机复现（lock_window 成功后杀调用方进程 → 重连 → get_window_lock
对账）并入下一次受控 11.26 监督轮。证据
`result-report/20261030_231407-connect-resets-stale-window-lock/`。
未提交/未推送。

- 剩余（不变）：推送后观察 CI 首跑；受控实机批次（§6o/6v/6w/6x/6y/6z/7a
  + 7g/7h 注入，一次监督轮合并）；多行同名消歧与 key_command、OCR 分级
  脱敏（产品决策）。

---

## §7i 换绑点（activate_edr / legacy connect 别名）旧窗口锁残留（批次 15，2026-10-31 00:0x 定时轮）

**缺口**（批次 14 "How to apply" 候选 (a)）：§7h 只堵了 backend
`connect()` 一条路；`activate_edr` 在 **`_gui` 层内部换绑**会话的已连接
窗口，三个 Windows legacy 别名直接换绑，macOS `activate_edr` 直接重写
`_connected_*` —— 全部绕过 §7h 对账：

1. Windows：`WindowsPywinautoBackend.activate_edr` →
   `_gui.activate_edr` 内部 `connect_by_process`（HisecEndpointAgent
   回退 pywinauto_client.py:769、EDRClient 收尾 :842）——按**进程**连
   "本进程第一个可连顶层窗口"，EDRClient.exe 拥有多个顶层窗口时可能落
   到与调用方 connect/lock 不同的窗口；
2. Windows：legacy 别名 `connect_by_title` / `connect_by_process` /
   `connect_by_pid`（windows_pywinauto.py:1459-1466）→ `_gui` 直接换绑
   main_window；
3. macOS：`MacOSAccessibilityBackend.activate_edr` 经
   `_mark_edr_client_connected`（三处）与 swift helper 路径直接写
   `_connected_pid/_connected_app/_connected_window_snapshot`
   （快照 handle 会丢，pid 可回退 pgrep 值）。

残留后果与 §7h 相同：`get_window_lock` 对新会话谎报 `locked=True`
（锁身份指向旧窗口），下一次 verify 可"复活"从未被本会话确认过的锁。

**修复**（三文件最小 Edit）：

- `target/automation/base.py`：新增共享
  `release_window_lock_if_window_changed(backend)` —— 换绑后已连接窗口
  对既有锁做**可观测字段对账**（pid/handle/title；沿用
  `lock_criteria_mismatch` 的"无法观测 = 不能反驳"哲学），漂移 → 清锁
  并返回身份对账；匹配 → 保留（already_open 路径不打断所有权链）；
  连接状态 `ok=False` → 保留。清锁只移除授权（后续注入在
  `_ensure_window_lock` 被拒），绝不放宽。
- Windows：`activate_edr` 对账**后置**（调用后 reconcile；result 追加
  `released_window_lock`）；三个别名用 §7h **同形**：尝试前清除、
  成功（真实换绑）→ 报告 released、失败 → 恢复。
- macOS：`activate_edr` 原函数体改名 `_activate_edr_impl`，包装器调用
  后 reconcile（同 Windows activate_edr 形状）。
- 产品运行路径不动：`connect_window_identity` → `connect_by_window`
  是解析后精确绑定且紧随 `lock_window`（批次 14
  `TestProductRunPathUnaffected` 锁定）。

**回归**：`test_case/test_recording/test_rebind_point_lock_accounting.py`
16 例（Windows activate_edr 6：换他窗清账对账/同窗保留/handle-only 漂移/
无锁无键/不可观测保留/title-only 降级形态；Windows 别名 6：title
成功清账、失败不动账、process、pid、无锁无键；macOS 5：快照重写漂移
清账、匹配保留、pid-only 漂移、无锁无键、不可观测保留）。
红 7 → 绿 16。

**突变验证**（每次 cp 恢复 SHA 核对）：

| 突变 | 结果 |
|---|---|
| M1 Windows activate_edr 对账禁用 | 红 1（精确） |
| M2 Windows title 别名对账移除 | 红 1（精确） |
| M3 macOS 对账禁用 | 红 1（精确） |
| M4a helper pid 检查禁用 | 红 1（macOS pid-only 形态暴露） |
| M4b helper handle 检查禁用 | 红 1（Windows handle-only 形态暴露） |
| M4c helper title 检查禁用 | 红 1（pid/handle 不可观测 + title-only 形态暴露） |
| M5 helper 整体禁用 | 红 4（四个 drop 形态全红，pass 侧不动） |

教训：第一批突变里 M4 全绿——初版漂移测试每次同时漂移多字段，单字段
检查被同伴遮蔽；补齐 pid-only / handle-only / title-only（降级观测）
三个**单字段漂移**形态后每个比较分支都有独立杀伤。单字段形态同时是
真实场景：macOS 快照重写丢 handle 只剩 pid 可比；RDP 降级观测只剩
title 可比。

**回归范围**：recording+scroll 1672 passed / 1 skipped；全仓
**2921 passed / 1 skipped / 0 failed**（基线 pre 2905 + 新回归 16 =
2921，Δ 精确，0 回归；§7e–§7h 既有回归 33/33 绿）。pre-SHA windows `830c219a` / macos `fb2c543c`
（= §7h post，未动）→ post windows `0966ecb2` / macos `751baa2e` /
base `1db12ae8`。

**结论分层**：代码级兼容（offline 回归 + 突变验证）。非实机验收。
实机复现（activate_edr 后 kill 调用方 → 重连 → get_window_lock 对账；
或同进程双窗口 + legacy 别名换绑）并入下一次受控 11.26 监督轮。证据
`result-report/20261031_000000-rebind-point-lock-accounting/`。
未提交/未推送。

- 剩余（更新）：推送后观察 CI 首跑；受控实机批次（§6o/6v/6w/6x/6y/6z/7a
  + 7g/7h/7i 注入，一次监督轮合并）；多行同名消歧与 key_command、OCR
  分级脱敏（产品决策）。

---

## §7j stop 收尾 capture-source 无条件停止（批次 16，2026-10-31 01:2x 定时轮）

**候选排查**：§7i 后对 target/ 全量换绑点扫描（`connect`/`activate_edr`/
别名/`connect_window_identity`/windowing runtime/macos 内部递归）——候选
关闭，零新增。转检查任务域 5/6/8 收尾对称性发现真实缺陷。

**缺口**：`RecordingSession` 用户 `stop()` 与 lease 过期
`_expire_before_operation()` 共用一个 try 块先停 indicator 再停 source ——
indicator.stop() 抛错（`SubprocessTkIndicator._send` 的 OSError、
`TkRecordingIndicator.stop` 的 join/queue 异常）→ source.stop() 被跳过，
会话进入 FAILED 终态后无任何路径再停 capture hook（UIA hook 线程 /
event tap），目标机无限期残留。`start()` 早已对 source 做 unwind，stop
侧不对称。

**修复**（session.py 单文件两处收尾块）：indicator 单独 try/except 记
`indicator_error` 后继续 source.stop()；source 异常为主体
（`raise exc from indicator_error`）；仅 indicator 失败时用户 stop 路径
FAILED+raise（保持重试 stop 收回录制契约）、过期路径 FAILED+reason+True。
所有出口 `_shutdown_watchdog()` 不变。

**回归**：`test_stop_source_cleanup_unconditional.py` 4 条，pre-fix 重放
4 红 → post 4 绿。突变 3 组精确红（M1 过期恢复旧行为→1；M2 用户 stop 吞
错→2；M3 过期吞错→1），cp 恢复 SHA
`86135b35…` 核对。全仓 **2925/1/0**（基线 2921+4，Δ 精确 0 回归）。
pre-SHA session.py `e1fddc60…` → post `86135b35…`。

**结论分层**：代码级兼容。非实机验收。实机复现（录制中杀 indicator 管道
→ stop → hook 残留审计）并入受控 11.26 监督轮。证据
`result-report/20261031_012054-stop-source-cleanup-unconditional/`。
未提交/未推送。

- 剩余（更新）：推送后观察 CI 首跑；受控实机批次（§6o/6v/6w/6x/6y/6z/7a
  + 7g/7h/7i/7j 注入，一次监督轮合并）；多行同名消歧与 key_command、OCR
  分级脱敏（产品决策）。

## §7k lease 过期录制产物持久化（批次 17，2026-10-31 01:45 定时轮）

任务域 5/6/8（断连恢复、孤儿残留）service 层 teardown 对称性审计（batch 16
"下一步候选"落点）发现真实缺陷。

**缺口**：`result_store.py` 模块文档宣称覆盖三种停止方式（用户 stop、lease
过期、recorder UI stop），但唯一调用 `save_stopped_recording` 的
`service.stop()` 只被用户 stop 与 UI stop（`callbacks.stop = service.stop`）
经过；lease 过期在 session 内部完成（watchdog §6v 或懒过期把会话直接驱动到
终态），service 层无感知。后果链：过期会话的 raw 文档只存在于会话对象内存、
脱敏帧只存在于 evidence_store 内存；下一次 `service.start()` 替换 manager 会话
对象并 `evidence_store.clear()`（batch 12 已核 evidence clear 时序），两者同时
消失且无恢复路径——恰是 result_store 设计要防的"Agent 断连丢录制"，过期路径
却是三种停止方式里唯一没接入的。repeat-stop 幂等与 collected 不复活已核验
成立（`RawRecording.to_dict` 确定性 + 同文档重存 `freshSave=False`），user-stop
双写无副作用，未列为缺陷。

**修复**（service.py 单文件，+`_reconcile_expired_result`）：终态 STOPPED 且
`reason=="recording_lease_expired"` 的会话在三个 service 观察点（status /
ui_snapshot / start 的 before_start）对账落盘 raw 文档 + `_persist_captures`
帧；落盘/帧失败如实抛 `recording_result_store_failed`（不能让 operator 以为
已安全保存）；FAILED 会话不落盘（source 收尾失败文档不得冒充干净结果）；
面板观察点先对账再取 pending。幂等由 result_store 同文档重存保障。

**回归**：`test_lease_expiry_result_persistence.py` 8 条（status 落盘、帧定点
恢复 sha256 相符、collected 不复活、start 替换前落盘、重复观察幂等、user-stop
语义不变、面板单独触发、FAILED 不落盘）。突变：M1 摘 status 点→3 红、
M3 摘 start 点→1 红、M4 摘面板点→1 红（均 cp 恢复 SHA `dc3c0bda…` 核对）；
M2 摘 reason 过滤语义无害（幂等重存）不锁定。pre-SHA service.py
`ae6ccead…` → post `dc3c0bda…`。全仓 **2933/1/0**（2925+8，Δ 精确 0 回归）。

**结论分层**：代码级兼容。非实机验收。实机场景（录制中拔 Agent 连接→lease
到期→重新连接收集 pending）并入受控 11.26 监督轮（与 §6o/6v/6w/6x/6y/6z/
7a/7g/7h/7i/7j 合并）。证据
`result-report/20261031_014500-lease-expiry-result-persistence/`。
未提交/未推送。

## §7l indicator 子进程生命周期：stdin-EOF 孤儿窗口（批次 18，2026-10-31 02:50 定时轮）

批次 16 遗留候选（a）ui_bridge/indicator 子进程生命周期审计的落实。审计发现
真实缺陷：录制指示器子进程（SubprocessTkIndicator，Aqua Tk 拥有主线程）的
关闭驱动全部隐含"父进程存活"——父进程崩溃/被杀后 stdin 写端关闭，pump 线程
EOF 静默退出而 Tk 主循环继续，always-on-top 录制窗口成为**永久孤儿且不可
自救**（X/Stop 只向死管道回报）；父进程侧 §6v watchdog 帮不上忙。任务域
6（孤儿窗口）/8（失败后不残留后台进程）的反例。

**修复**（indicator.py 子进程入口三处，Edit-only）：`_child_emit()` emit 返回
送达成败（BrokenPipeError/ValueError→False）；`_child_callbacks` stop 回调在
回报失败时向自身队列投 stop 自行终止（活管道不自救，防窗口-会话失同步）；
pump EOF 投 stop 复用 poll destroy 路径（≤~0.2s 退出）。优雅路径不变。

**回归**：`test_indicator_child_lifecycle.py` 4 条（端到端真子进程 EOF 自杀、
优雅路径锁、死管道自救（真实 emit+关闭 stdout，非 mock）、活管道不自救）。
红基线 3 红/1 绿；突变 M1 摘 EOF 自终止→红（15.5s 超时等待回归）、M2 摘死管
道自救→红、M3 emit 谎报送达→红，SHA 校验恢复 `67af56e4…`。pre-SHA
`3750e0bf…`（本轮起点）→ post `67af56e4…`。全仓 **2937/1/0**（2933+4，
Δ 精确 0 回归）。同轮 ancestry 闭环 4 套件 26/26 重验绿。

**结论分层**：代码级兼容。非实机验收。实机场景（父进程 kill -9 →录制窗口
≤1s 自行消失）并入受控 11.26 监督轮合批。审计余项：`_consume` reader 线程
残留（daemon，低风险）列为下轮候选。证据
`result-report/20261031_025051-indicator-child-lifecycle/`。未提交/未推送。

## §7m 父侧 reader 线程回调韧性（批次 19，2026-10-31 03:1x 定时轮）

批次 18 遗留候选（a）`_consume` reader 线程残留的落实。审计发现真实缺陷，
与 §7l 同级但根因不同——**父进程活着**，因此子进程 EOF 自杀（§7l）与会话
watchdog（§6v）都救不了它：

**缺口**：`SubprocessTkIndicator._consume`（indicator.py）把子进程回报分发
给会话回调（pause/resume/stop）并应答断言 ACK，但对 pause/resume/stop 是
**裸调用**。会话一旦不在回调要求的状态（lease 过期瞬间点击、双击 Pause
竞态、Stop 与 watchdog 过期竞态、断言表单开着时会话收尾）即抛
`recording_state_invalid`——异常就地杀死 reader 线程。后果：子进程 Tk 窗口
仍在（父 stdin 仍开，EOF 自杀不触发），但按钮回报再无人消费、断言 ACK 永不
回投 → 活着的孤儿窗口、每个按钮都是死的。add_assertion 分发原本已有
try/except（§6 断言 ACK 语义），pause/resume/stop 三路漏防。

**修复**（indicator.py 单处，Edit-only）：回调分发整体包一层
try/except-continue——单个回调异常不越出 reader 线程循环；add_assertion 的
失败-ACK 语义原样保留。防御性注释说明为何不能裸调用（活孤儿不可自救）。

**回归**：`test_reader_thread_resilience.py` 4 条，红基线 2 红（幸存性）
/2 绿（既有行为锁）：
1. pause 抛真 `recording_state_invalid`（真实 RecordingSession IDLE 拒绝，
   非 mock 异常）后，后续 add_assertion 报告仍被消费且失败 ACK 带 requestId
   送达——红基线复现 reader 死亡。
2. stop 抛真 `recording_state_invalid` 后同上。
3. 健康路径顺序分发 + 真实会话状态迁移（RECORDING→PAUSED→RECORDING）。
4. 拒绝的断言保存产出带 requestId 的失败 ACK（表单保留不挂死）。
分发路径全部真实（真 `_consume` 循环、真会话、真 RecordingModelError），
仅替换 OS 管道两端（`_ProcessDouble`，os.pipe 真文件对象 + ACK drain
解析 `_send` 的 `{"command":"assert_ack","value":{...}}` 包裹）——Tk 子进程
UI 本身才是被替换的底层设施，被测的分发逻辑零 mock。

**突变**：M1 摘外层防护（还原裸分发）→2 红精确；M2 continue 改 re-raise
→2 红精确；M3 摘 requestId 回投 →3 红精确；全部 cp 恢复并 SHA 断言
`9a756861…`。pre-SHA `67af56e4…`（= §7l post，起点未动）→ post
`9a756861…`。全仓 **2941/1/0**（2937+4，Δ 精确 0 回归）；录制套件
1371/1/0。

**结论分层**：代码级兼容。非实机验收。实机场景（lease 过期瞬间点浮窗
按钮 → 浮窗按钮必须仍工作/窗口必须关闭，不得出现死按钮孤儿）并入受控
11.26 监督轮合批。**How to apply：batch 18 遗留候选（a）关闭；indicator
生命周期审计（§7l+§7m）两条根因（EOF 静默退出、回调异常杀死 reader）均已
闭环。下一候选：录制入口审计（任务域 1：会话归属/多窗口切换的同标题
误操作防护）或 `_expire_before_operation` 与 user-stop 的竞态窗口再审。**
证据 `result-report/20261031_031803-reader-thread-resilience/`。未提交/未
推送。

## §7n 同标题窗口动作漂移编译诚实（批次 20，2026-10-31 03:5x 定时轮）

任务域 1 审计（录制入口/会话归属/同标题误操作防护）。回放侧防御链完整
（strict lock `connect_target_ambiguous`、root-selector
`window_selector_ambiguous`、帧绑定 handle 精确归窗）；录制侧每事件
evidence.window 带 handle+root。**真实缺口在编译器**：断言侧有
`compile_window_identity_ambiguous` 防护（test_phase0:452），动作步骤没有——
两个不同 handle、回放窗口身份坍缩相同的连续动作编译 `ready`；回放只锁一个
同标题窗口，整条流程跑在错误的窗里 = 同标题误操作（现场两个 logo1
pid 1984/7020 真实存在）。修复：编译循环动作注册处按
`canonical_json(selector.window)` 分组，同身份第二动作与前一动作 handle 不同
→ 整组（回溯首成员）`compile_window_identity_ambiguous` + incomplete。
诚实语义：同 handle 重复身份（窗口替换重开）不标——拒绝的安全侧论证见
测试 docstring。会话竞态（expiry 排空 vs user-stop）审计无缺口：
STOPPING-wait 分支归还同一文档，既有测试锁定。

回归 `test_phase0_recording.py` +4（红基线 3 红/1 绿）。突变 3 组精确红：
M1 检测块禁用→3 红；M2 去回溯标记→2 红；M3b 去 handle 证据要求→4 红
（含既有 scroll-run 误伤，证明 handle 门必要）；cp 恢复 SHA `5a637c6a…`
各验一次。pre-SHA compiler `3635e6d1…` → post `5a637c6a…`。全仓
**2945/1/0**（2941+4，Δ 精确 0 回归）。

**结论分层**：代码级兼容。非实机验收。实机场景（跨两个同标题窗口录一条
流程 → 编译必须 incomplete 列 issue）并入受控 11.26 监督轮合批。下一
候选：recording.md compile-issue 文档核对，或 start 时 seededScope 同标题
多窗的披露。证据
`result-report/20261031_044500-same-title-action-drift/`。未提交/未推送。

## §7o stop 指引沉寂缺口 + recording.md 词汇收口（批次 21，2026-10-31 04:0x 定时轮）

§7n 登记的两个候选里的第一个：recording.md compile-issue 文档核对。核对
方法：正则提取 compiler.py 字面 issue code（20 个）与 references/recording.md
反引号码对比——发现 batch 9b 之后文档只覆盖 4 个，且 §6x 一致性测试存在
**真实盲点**：`test_every_compiler_issue_code_is_catalogued` 只检查「未登记」
兜底文案，而 §7n 把 `compile_window_identity_ambiguous` 塞进 `known` 集合却
**没写专属分支**——操作者遇到同标题窗口编译缺口时 `record stop` 输出**空
指引**（连兜底都没有），测试照样绿。同形态沉寂还有 5 个码：
`compile_recording_empty`、`recording_packets_dropped`、
`recording_correlation_errors`、`recording_secret_review_required`、
`recording_capture_evidence_unavailable`（均在 known、均无分支、均空输出）。

修复（Edit-only agent/cli.py）：
1. 6 个专属中文指引分支（window_identity 指引稳定窗口根身份重锁；
   capture_evidence 指引重录该段；secret_review 指引环境变量 textSource、
   明确明文不落盘；packets/correlation/empty 为会话级建议，不带 seqs）。
2. `known` 集合提为模块级 `_STOP_ADVISORY_KNOWN_CODES` + 只读视图
   `_stop_advisory_known_codes()`（`record stop` 主路径语义不变）。
3. parity 回归加固：catalogued 测试改为「无输出或落兜底都算未登记」；
   新增 `test_known_set_code_without_branch_is_caught`——遍历 known 全集，
   逐码断言至少一条专属指引（登记即有指引，防再次沉寂）。
4. recording.md 诚实清单补齐至全词汇：window-identity gate（§7n 编译器
   变更的文档面）、weak-identity 四码家族、文档级捕获诚实五码、步骤级
   五码（unsupported/selector_ambiguous/assertion_selector_ambiguous/
   assertion_missing_expected/region_container）。核对脚本确认：
   compiler 20 码全部入档，doc 独有的 3 码
   （`recording_persistence_failed`/`recording_secret_plaintext`/
   `recording_session_active`）确属 stop/运行层码且文档在他节合法存在。

突变 3 组精确红（M1 window_identity 分支禁用→2 红；M2 secret_review 禁用
→2 红；M3 六分支全禁→2 红；均在加固后的 parity 套件上致命；cp 恢复各验
SHA `b70e9269…`）。pre-SHA cli.py `363068dc…`（=§7a post）→ post
`b70e9269…`。parity 套件 12→13（加固 1 个 + 新增 1 个，既有语义测试全绿
——record assert 指引、ready 无指引、未知码兜底契约不变）。全仓
**2946/1/0**（2945+1，Δ 精确 0 回归）。recording.md 无并发改动（mtime
10-30 20:08，batch 9b 后无人动过）。

**结论分层**：代码级兼容。非实机验收——`record stop` 指引的实机显示
（同标题窗口流程触发 window_identity 空指引→现应有中文指引）并入受控
11.26 监督轮合批（§6x advisory display 同批）。下一候选：start 时
seededScope 同标题多窗披露，或下一任务域审计（断言域：普通/区域/多组件/
时间范围断言的选择-预览-保存-编译-回放一致性）。证据
`result-report/20261031_040900-stop-advisory-silent-gaps/`。未提交/未推送。
