# 轨迹案例记忆首版交付 — 2026-11-17

## 分支与范围

- `public`：`ddedb77c5fa0d9fc382294adb7b44e0a926572b9`，本次开发前的已提交代码，不包含案例记忆功能。保留原有 recovery，未改变 GitHub 仓库可见性。
- `feat/trajectory-case-memory`：首版功能提交 `fb647ea`，独立 worktree `/Users/edr-test/ai-projects/edr-ga-case-memory`。本报告作为后续文档提交，不改变受测运行代码。
- 原 `/Users/edr-test/ai-projects/edr-wd` 仍在 hermes-remote；菜单修复及其他未提交 WIP 未合入本功能，也未被移动、覆盖或清理。

## 已实现

1. 编译产物落盘后自动登记案例，包含 ready、incomplete 和空录制；CLI stop/compile 与 UI 收集返回 caseMemory 状态。
2. v3 CLI 单夹回放结束后，关联已落盘 summary，并返回同上下文结构参考；不触发第二次执行或修改断言。
3. 同一 Skill 下提供本地 memory import（支持 dry-run）/search/compare/rebuild。无需 target、SSH、LLM 或新的依赖。
4. 结构索引不保存动作参数、控件文本、截图或令牌。v3 原件复用现有位置；旧平铺可覆盖 JSON 以 hash 快照保存。相同源重复导入幂等，同名不同 session 独立。
5. raw→golden 源摘要校验、相对引用、来源变化后不推荐、不可变登记回执、同 run 不覆盖、不同轨迹的 run 不混入、整夹复制后索引可重建。
6. `EDR_GA_CASE_MEMORY=0` 可关闭自动登记，原录制/回放仍可运行。索引失败单列，不篡改原业务结果。

## 真实历史材料试导入

仅在隔离 worktree 的录制副本上测试，未写原仓库的录制/索引。扫描 22 个平铺原件集：8 组通过来源/状态验证并登记，14 组因输入校验未通过而拒绝；不放宽检查、不重新编译冒充原结果。拒绝不等于业务流程有缺陷，意味着目前不能证明这组历史材料可作为配对案例使用。

这些数据、截图、SQLite 和测试日志都被忽略，未纳入提交。后续正式接管历史材料需显式 dry-run 审核路径与缺件，不能把本次试导入说成历史全集已整理完成。

## 验证与已知基线失败

运行解释器：原仓库 `.venv/bin/python`（Python 3.10）。完整输出位于本 worktree `result-report/case-memory-implementation/`。

- 新增功能与安全边界测试：21 项（new-tests.log）。
- 修复后的新增功能+UI兼容相关测试：40 passed。
- 相邻编译/案例测试：97 passed（运行时在最终新增 run 隔离测试之前；最终全库覆盖了新增用例）。
- 最终默认全库：**3695 passed / 10 failed / 6 skipped / 614 deselected**，307.05 秒，exit 1。不是全绿，也不是完整实机验收。
- 显式 `-m regression`：**586 passed / 3739 deselected**，21.17 秒，exit 0。
- `compileall`、Skill 校验、diff --check 通过。

录制库中间态回归为 2122 passed / 12 failed / 6 skipped / 1 deselected，其中 2 项是本次 UI 可选字段兼容问题，已修复并由最终全库及 40 项定向回归覆盖；不把中间态日志冒充最终通过。

另建 public 的 detached worktree，复制完全相同的录制样本，独立跑失败模块与 UI 相关模块：**75 passed / 10 failed**。10 个失败 node ID 与最终全库逐项完全一致，主要是旧平铺成对性契约扫描 v3 sources、历史 policy_flow 状态不一致，以及旧 golden_to_test_case API 的 v3 能力拒绝。没有移动失败样本或修改这些断言来取得绿灯；本批不顺手修改这些既有问题。

新 worktree 初始缺少 Git 忽略的历史录制与一个审核测试 helper；复制所需测试材料后完成上述验证。helper/测试材料也未入库。原件一直保留在原位置。

## 尚未实施与授权边界

本次是案例记忆与诊断线索基座，不是已经完成的训练/强化学习系统。没有：向量模型、SFT/LoRA/DPO、bandit 策略训练、自动修复/重录、在线探索、Windows 部署/重启/GUI 或独立实机成功认证。

首次采集、服务端保存失败、异常退出等全生命周期入口尚未全部登记；本次自动入口是成功获得本地编译产物之后。旧平铺截图完整性及全部历史版本迁移也未承诺完成。capture 完整性、产品版本、输入来源无权威字段时均标 unknown。

比较结果仅作位置级结构对照，不证明根因、不自动重排业务步骤。reported run pass 与独立实机业务验证严格分开。安全/unknown/授权门继续由已有执行链裁决。

## 使用

在功能 worktree 运行（无需改变原环境的 editable install）：

```bash
cd /Users/edr-test/ai-projects/edr-ga-case-memory
/Users/edr-test/ai-projects/edr-wd/.venv/bin/python -m agent.cli \
  memory --record-root recordings search compile_transition_unbound
```

完整命令与限制见 `references/trajectory-memory.md`。后续先独立审核当前首版，不将未实现的学习阶段标为完成，不自动开启定时任务。
