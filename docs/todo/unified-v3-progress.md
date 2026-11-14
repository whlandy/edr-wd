# 统一 v3 轨迹实施进度

权威：[统一v3轨迹设计](../../doc/unified-v3-trajectory-design.md)。实施分支：`mcp_auto_v3.0`。定时任务：`unified-v3-trajectory-impl`（:13/:33/:53）。

## 评审修复（2026-11-10，待回归确认后提交推送）

外审共 4 项，全部在头处理——评审指出的缺陷已修复：

1. **[P1] 回放模块 3.10 无法导入**：`execution_ledger.py` 跨行 f-string（PEP701 只在 3.12+ 合法）已改为先取局部变量 `observed_id` 再拼接 → `.venv/bin/python(3.10) compileall` 通过、`test_v3_transport/c11/localized/d2_owner` 45 passed。**全库 3.10 回归运行中**。
2. **[P1] V3 回放忽略 replay-mode**：`execute_v3_window_stage_real` 新增 `replay_mode` 参数（非法值 GUI 前拒绝）、`semantic_only` 下纯视觉点击报 `replay_selector_semantic_missing`（legacy 同语义）；CLI 仅在 `semantic_first/visual_only` 时构造 visual_resolver。新增 `test_v3_replay_mode.py` 4 用例（semantic_only 拒绝/非法拒绝/默认语义 pass）。原 5 个 visual 测试（编码旧 bug）改显式 `--replay-mode semantic_first` + adapter 直调加参数。
3. **[P1] 录制返回目录不含主轨迹**：`RecordingArtifacts` 加 `trajectory_root/trace_v3_path` 字段（v1/v2 为 None）；v3 经 `from_manifest` 指向真实主轨迹；CLI stop/compile payload 加 `trajectoryRoot/traceV3`。新增 `test_v3_replay_mode.py::test_v3_artifacts_report_real_trajectory_path`。
4. **[报告待核] 迁移身份一致**：`execute_migration` 修改 —— 源 id 在目标空闲→沿用(manifest 逐字节保真)；冲突→分配新 id 且仅改写 `manifest.trajectoryId`(字段集不变)，逐字节校验排除已改写 manifest 并独立校验其字段。新增 `test_migration_identity_follows_folder_on_collision`（1 空闲 + 1 冲突反例）。

## 下一轮任务（最新在顶部）

**居中待回归 + 提交**：
- 3.10 录制库全绿后,在 `mcp_auto_v3.0` 提交评审修复(暂不推,用户说推才推)。
- 提交范围：`agent/{cli,recording/{artifacts,collection,execution_ledger,trajectory_layout,v3_transport}}.py`、`target/{server.py,windowing/instance_tracker.py,automation/instance_events.py}`、`test_case/test_recording/{test_v3_replay_mode,test_trajectory_layout,test_replay,test_v3_real_server_adapter,test_v3_transport,test_recover_cli,test_golden_v3_window_sequence,test_instance_tracker}.py`、`docs/architecture/{13,14}*.md`、`docs/todo/unified-v3-progress.md`。
- 未跟踪文件(实机原件/复现脚本/hash 清单)保持不入库 —— 外审已提示提交前需逐项确认是否适合公开,不自动 add。下次循环起继续按设计 §8 推进（先 B 阶段唯一 v3 消费链剩余、再 C 单夹布局三用途投影）。

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
