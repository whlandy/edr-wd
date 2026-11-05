# S5 Windows 实机验收 — 本地准备与执行清单

日期:2026-11-03;状态:**本地准备完成,实机执行待授权**。
本清单覆盖 09 号设计 §11 验收矩阵的实机部分。所有本地可完成的准备
(fixture、脚本、清单、证据布局)在本轮交付;真实 Windows 连续三轮
与人工缺陷注入需要明确的实机授权,不能以本地 fake 充数。

## 0. 授权边界(当前状态)

- 当前**无**实机授权:本聊天未明确授权任何 Windows GUI 动作。
- 实机执行前需用户明确授权目标/动作范围;授权后仍按片执行、
  逐片留存证据。
- 本轮交付的全部本地工件不触碰真实 Windows。

## 1. 环境(只读检查清单 — 实机执行前运行)

| 检查 | 方式 | 期望 |
|---|---|---|
| 目标可达 | edr-wd-target 服务 status | ok,backend=windows_pywinauto |
| HiSec 客户端进程 | 只读进程枚举 | EDRClient.exe 存在 |
| UI 服务 | 只读 sc query(不 start) | RUNNING;stopped 时**报告并停**,不自动启动 |
| 产品 profile | INSTALLED_PRODUCT_SPECS 含 (windows_hisec, windows_pywinauto) | path/entry_path 与实机一致 |
| capability | backend.window_sequence_capability() | 必须返回结构化 supported/coverage/generation;不支持 → S3 门禁如实拒绝(不伪造) |

## 2. 无凭据专用 fixture(已交付)

- `test_case/test_recording/test_window_binding_server_wiring.py` 的
  `_FakeOSBackend`:匹配真实 backend 对外协议(枚举/连接/锁定/输入注入),
  **无凭据**;真实 GuardedBackend/product_policy/WindowSession/dispatcher
  全链路在环。
- `test_case/test_recording/test_s4_window_effects.py` 的
  `_EffectOSBackend`:差分用窗口出现/消失驱动。
- 实机 fixture(win26 专用):沿用既有 `scripts/recording_native_fixture.ps1`
  (只读检查其存在与语法,本轮不执行)。

## 3. 受控故障注入矩阵(实机轮执行)

| # | 注入 | 预期(既定契约) | 证据文件 |
|---|---|---|---|
| F1 | focus 后 owner pid 漂移 | dispatch 0 次,window_binding_lost | `evidence/F1-*` |
| F2 | 聚焦后 title 漂移 | dispatch 0 次 | `evidence/F2-*` |
| F3 | 同 HWND destroy+复现 | LOST,不复活,兄弟不顶替 | `evidence/F3-*` |
| F4 | visible/foreground=false | 0 执行 | `evidence/F4-*` |
| F5 | 动作回执 unknown | 不推进、不重试点击 | `evidence/F5-*` |
| F6 | 上一断言失败 | 下一步阻断,不绕过 | `evidence/F6-*` |
| F7 | 断线/取消 | unknown 边界标记 | `evidence/F7-*` |
| F8 | 状态写盘失败 | 动作前禁副作用;动作后 unknown | `evidence/F8-*` |
| F9 | 同名第二实例出现 | 增量多候选拒绝 | `evidence/F9-*` |
| F10 | 租约过期 | 新 owner 不受旧 facade 影响 | `evidence/F10-*` |

本地已覆盖 F1–F10 的 fake 等价回归(wiring/facade/S4/ledger 套件);
实机轮在真实 OS 事件源上重放同一矩阵。

## 4. 三轮验收运行脚本(已交付)

`scripts/s5_acceptance_round.py`:单轮 = 新原件录制 → 停止收集 → 编译 →
回放 → 业务断言 → 可回溯带图报告;产物目录布局见 §5。
**当前模式**:本地 dry-run(校验脚本与目录布局,不做 GUI);实机模式
需 `--live` 显式开启(本轮不授权)。

## 5. 证据布局

```
result-report/s5-windows-acceptance/
  round-1/
    recording/          # 新原件 raw(v2)
    compile-report.json # 编译诊断(含 raw 来源索引)
    golden/             # v3 产物
    replay/             # 执行账本(events/*.json,§7.6)
    report.json         # 失败步可追溯视图
    screenshots/        # 带图报告(缺失标 evidence_missing)
    summary.md          # 本轮结论(通过/失败与原因)
  round-2/ ... round-3/ ...
  injected-defects/     # F1–F10 实机证据
  acceptance.md         # 三轮汇总 + 人工缺陷正确失败声明
```

## 6. 验收判定(与 09 §11 一致)

- 三轮全部 ready 编译、回放业务断言 pass、可回溯报告齐备;
- 人工注入缺陷(至少 3 类:F 类任选)必须**正确失败**并留下可追溯证据;
- 缺任意一项即未通过;本地 fake 结果不充数。

## 7. 待用户决定(实机阻塞项)

1. 授权在真实 Windows 目标执行 §3 矩阵与 §4 三轮(明确目标机/时间窗);
2. 确认实机 capability 支持等级(不支持则按 §1 如实拒绝,不扩权限);
3. 注入故障的方式(人工操作 or 授权脚本化注入)。
