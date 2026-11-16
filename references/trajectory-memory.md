# 轨迹案例记忆（首版，本地证据层）

本功能集成在 `edr-gui-automation` 内，不新增 Skill、模型或 GUI 执行器。
`public` 分支保留无此功能的已提交基线；开发分支为 `feat/trajectory-case-memory`。

## 自动保存什么

既有编译产物成功落盘后，自动记录结构化案例，包括空录制与 incomplete。
v3 原始录制与主轨迹仍在 `trajectories/TRJ-*/`；索引只引用它们。
旧平铺入口和可覆盖的编译报告以内容 hash 保存必要快照，避免同名重录
覆盖历史引用。不会额外复制截图，也不将输入参数、标题、控件文本或密码
加入检索索引。原始来源仍属敏感本地档案，不应上传到 Git 或在线模型。

单夹入口的 v3 CLI 回放结束后，关联真实落盘的 summary。`pass` 仅标为
`reported_run`，不自动变成独立验证的实机成功案例；ready 更不代表 replay pass。
capture 完整性、产品版本和物理/合成来源在无权威字段时都保留 unknown。

## 本地命令（无需 target、SSH 或 MCP）

```bash
edr-ga memory --record-root recordings search compile_transition_unbound
edr-ga memory --record-root recordings search --similar-to CASE-<hash> --limit 3
edr-ga memory --record-root recordings compare CASE-<left> CASE-<right>
edr-ga memory --record-root recordings rebuild
```

历史产物显式导入，不扫描桌面或擅自搬动文件。源文件必须在指定 record root 内：

```bash
edr-ga memory --record-root recordings import \
  --raw recordings/trajectories/TRJ-0001/sources/REC-xxx/recording.json \
  --trace recordings/trajectories/TRJ-0001/revisions/REV-0001/trace.v3.json \
  --report recordings/flow/compile-report.json --dry-run
# 核对后去掉 --dry-run 才写本地案例索引。
```

请根据实际返回路径填写，不猜 TRJ 编号或把别的录制报告拼入本次原件。
若历史 flat 目录被覆盖而原件已缺失，先报告缺口，不编造历史。

## 如何使用结果

- search 仅按结构字段/诊断码排序；similar-to 要求相同已知 backend 与产品上下文，
  版本未知仍明确未知，不构成跨版本复用许可。
- compare 只做顺序位置的结构差异，不证明因果或自动对齐重复步骤。结果明确
  标记 `ordinal_only_not_causal`、`hypothesis_only`；人工再查 source-map/原账本。
- 原件摘要改变或缺件时不推荐该案例；rebuild 从不可变登记回执重建 SQLite，
  遇到来源损坏拒绝，不以陈旧索引掩盖缺口。
- 相似案例是排查线索，不授权修改断言、复用旧 targetId/binding 或重放 unknown。
  修复仍调用现有显式 recover，GUI 动作仍需具体授权和现状验证。

`caseMemory.status=error` 表示未完成入库，原编译/回放结果保持原义；先保留
实际产物，再显式导入修复索引。不得把索引故障写成“录制未执行”。

## 关闭与首版限制

设置 `EDR_GA_CASE_MEMORY=0` 关闭编译/回放自动登记，原能力继续运行。
显式 memory 命令仍可用于管理历史记录；关闭不删除已有档案。

尚未实施：训练/LoRA/DPO/强化学习、向量检索、自动修复或自动重录、独立实机
成功认证、远端保存失败/进程崩溃的全生命周期登记、GUI bridge 的检索提示。
现有 UI 收集经 artifacts 写入口会登记编译案例，但不等于所有开始/失败出口
已经覆盖。不声称整套设计或 Windows 实机验收完成。
