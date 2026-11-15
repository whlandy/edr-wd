# 统一 v3 轨迹实施进度

权威：[统一v3轨迹设计](../../doc/unified-v3-trajectory-design.md)。
**分支实况更正（2026-11-15）**：评审轮 P1 修复 `abad9f6` 已在 `hermes-remote`（并推到 `origin/hermes-remote`），**不在** `mcp_auto_v3.0`；本轮增量提交 （hermes-remote 顶端 fix(recording) 提交, 未推） 亦在 `hermes-remote`（未推）。账本此处不再以 mcp_auto_v3.0 为实施分支表述。
定时任务：`unified-v3-trajectory-impl`（:13/:33/:53）——**已停用**（用户指示停止定时延伸；`list_scheduled_tasks` 确认 `enabled: false`，lastRun 2026-11-12 后未再运行）。

## 本轮（2026-11-15）：补齐 replay-mode 三模式契约 + 行为级测试

用户指令：本轮不验收/不推送/不设计 §8 新阶段，只补齐 replay-mode。已完成：

1. **visual_only 仅视觉定位**：动作步缺视觉条件或 resolver → GUI 前拒绝 `visual_only_unsupported`，**禁止语义派发**（纯语义步也走此拒绝，不再落语义候选）。
2. **semantic_first 语义优先 + 条件回退**：语义候选为空且步带视觉条件与 resolver → 回退视觉解析（A023 `pointer.click_window`）；**>1 候选的歧义拒绝保持不变**（视觉不得掩盖真歧义）。
3. **semantic_only 保持禁视觉派发**：语义身份步即便带视觉条件仍走语义（resolver 0 调用）。
4. 修复重构残留：补回 `selector`/`control_selector`/`wanted_automation_id` 绑定、`_resolve_visual` 闭包 `nonlocal latched/blocked_at/observation` 传播。
5. **行为级三模式测试**（`test_v3_replay_mode.py` 新增 6 用例，断言 resolver 调用次数 + 业务派发方法/次数）——既有 4 测确实不代表三模式覆盖，已补矩阵。
6. **迁移身份改写仅核对必要内部引用一致性（不扩项）**：探针证实 TRJ id 只承载于 `manifest.json` 与 write-once `README.md` 标题；trace.v3/source-map/views/exports/sources 均不内嵌 id → 冲突改写无悬空旧 id，`from_manifest` 仍可定位主轨迹。新增 `test_migration_rewrite_keeps_internal_references_consistent` 固化该边界。

**最终门槛（3.10，`.venv/bin/python`）**：recording 库 **2105 passed / 1 skipped / 1 deselected，exit 0**（较基线 2098 多 7 = 本轮新增 6+1）；`compileall` 干净；`git diff --check` 干净。提交 （hermes-remote 顶端 fix(recording) 提交, 未推）（hermes-remote，未推）。等待独立复验。

## 评审修复（2026-11-10，abad9f6 已入 hermes-remote）

外审共 4 项，全部在头处理——评审指出的缺陷已修复：

1. **[P1] 回放模块 3.10 无法导入**：`execution_ledger.py` 跨行 f-string（PEP701 只在 3.12+ 合法）已改为先取局部变量 `observed_id` 再拼接 → `.venv/bin/python(3.10) compileall` 通过、`test_v3_transport/c11/localized/d2_owner` 45 passed。**全库 3.10 回归运行中**。
2. **[P1] V3 回放忽略 replay-mode**：`execute_v3_window_stage_real` 新增 `replay_mode` 参数（非法值 GUI 前拒绝）、`semantic_only` 下纯视觉点击报 `replay_selector_semantic_missing`（legacy 同语义）；CLI 仅在 `semantic_first/visual_only` 时构造 visual_resolver。新增 `test_v3_replay_mode.py` 4 用例（semantic_only 拒绝/非法拒绝/默认语义 pass）。原 5 个 visual 测试（编码旧 bug）改显式 `--replay-mode semantic_first` + adapter 直调加参数。
3. **[P1] 录制返回目录不含主轨迹**：`RecordingArtifacts` 加 `trajectory_root/trace_v3_path` 字段（v1/v2 为 None）；v3 经 `from_manifest` 指向真实主轨迹；CLI stop/compile payload 加 `trajectoryRoot/traceV3`。新增 `test_v3_replay_mode.py::test_v3_artifacts_report_real_trajectory_path`。
4. **[报告待核] 迁移身份一致**：`execute_migration` 修改 —— 源 id 在目标空闲→沿用(manifest 逐字节保真)；冲突→分配新 id 且仅改写 `manifest.trajectoryId`(字段集不变)，逐字节校验排除已改写 manifest 并独立校验其字段。新增 `test_migration_identity_follows_folder_on_collision`（1 空闲 + 1 冲突反例）。

## 下一轮任务（最新在顶部）

**已交付，等待独立复验（2026-11-15）**：本轮 replay-mode 三模式契约补齐 + 行为级测试 + 迁移引用一致性核对，已在 `hermes-remote` 提交 （hermes-remote 顶端 fix(recording) 提交, 未推）（未推）。定时任务停用。
- **不在本轮做**（用户指令）：不验收、不推送、不设计 §8 新阶段、不拆/reset/回滚 abad9f6。
- 分支待用户确认：abad9f6 与本轮提交均落在 `hermes-remote`（账本此前误记为 `mcp_auto_v3.0`）。是否回迁/另开实施分支、是否推送，待用户明确后再动 —— 本轮不擅自挪分支。
- 未跟踪文件(实机原件/复现脚本/hash 清单)保持不入库 —— 外审已提示提交前需逐项确认是否适合公开,不自动 add。

## 阶段状态

| 阶段 | 状态 | 备注 |
|---|---|---|
| A 采集完整/竞态/基线 | 部分 | 传输/role 派生已并；子菜单枚举接线待 |
| B 唯一 v3 消费链 | 评审修复中 | CLI 单入口+模式约束+artifact 路径已修 |
| C 单夹布局+三用途 | 布局已投 | 迁移身份契约已修 |
| D MAA 转换 v3 | 未开始 | 转换器在 ../edr-cloud-recorder |
| E 去重/兼容/迁移 | 部分 | 迁移身份已修 |

## 原则
最小修改、反例先行、自审(git diff 无伪造/无放宽/无删测试)、3.10 回归先绿。
