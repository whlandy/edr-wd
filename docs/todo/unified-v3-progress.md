# 统一 v3 轨迹实施进度

权威：[统一v3轨迹设计](../../doc/unified-v3-trajectory-design.md)。实施分支：`mcp_auto_v3.0`。定时任务：`unified-v3-trajectory-impl`（每20分钟，:13/:33/:53）。

## 下一轮任务（最新在顶部）

<!-- 每轮结束把下一轮要改的内容写到本节顶部。格式：文件/函数/测试 + 验收标准 -->

**轮次 0（本会话）已完成**：进度账本建立 + 定时任务创建（回执见下）。首轮实施任务已写入定时 prompt：

- **下一轮（轮次1）任务**：建立单夹布局写入器骨架（设计 §5）——新模块 `agent/recording/trajectory_layout.py`：`TrajectoryLayout.create(record_dir, task_key) -> TRJ-<id>/`，落 `manifest.json`（指向 revisions/REV-0001）、`revisions/REV-0001/`（空 trace.v3.json 占位由后续片填充）、`sources/`、`assets/sha256/`、`runs/`、`recovery/` 目录骨架；同名 task_key 不覆盖（新 TRJ id）、manifest 不可变指向。验收：`test_trajectory_layout.py` 覆盖 创建/防覆盖/manifest指向/幂等重生成；录制库零回归。

## 阶段状态（设计 §8）

| 阶段 | 状态 | 备注 |
| --- | --- | --- |
| A 采集完整/竞态/基线 | 未开始 | 含子菜单枚举证据接线（step-0004 缺口） |
| B 唯一 v3 消费链 | 部分（transport 就绪） | 片① transport+role 推导已并入 mcp_auto_v3.0（e8afe18/19669bb）；待 CLI 路由与报告投影 |
| C 单夹布局+三用途投影 | 轮次1起 | 布局骨架先行 |
| D MAA 转换 v3 | 未开始 | 转换器扩展在 ../edr-cloud-recorder，勿提前 |
| E 去重/兼容/迁移 | 未开始 | |

## 定时任务回执（2026-11-10 创建）

- 任务 ID：`unified-v3-trajectory-impl`；标题「统一v3轨迹实施循环」
- 计划：cron `13,33,53 * * * *`（每20分钟）+ jitter；enabled=true
- 持久性：持久（.claude/scheduled-tasks/ 落盘）；仅 app 开启时运行；无固定有效期
- 权限模式：Bypass permissions（随本会话保存）；prompt 内已含运行时边界（分支锁定 mcp_auto_v3.0、不推送、无 GUI/部署授权、每轮一片、失败先修回归、两轮无进展停等）
- 用户可在 app Scheduled 区修改/停用

## 原则

- 最小修改：每轮一个最小闭环片，反例/测试先行。
- 自审：改完跑 git diff 自查（无伪造 receipt/ready、无放宽断言、无删测试）+ 录制库回归。
- 下轮任务写本文件头部；两轮同一问题无进展 → 写"需用户决策"停等。
- 已锁事实：transport 模块（片①）已就绪；MAA 转换器只认 v1（D 阶段扩展，勿提前）；step-0004 缺口=子菜单枚举证据接线（A 阶段片）。
