# S4/§7.6/S5 交付(2112 修订):helper→真实接线→单轮驱动

日期:2026-11-03;状态:**本地实现与真实链路接线完成;S4/§7.6/S5 未经
独立验收;Windows 实机三轮未开始(授权+生产 capability 前置)**。
本文件清楚区分四层状态,取代此前的"目录骨架"描述。

## 0. 状态矩阵(四层区分)

| 项 | helper 实现 | 真实链路接线 | 自测 | 独立验收 | 实机 |
|---|---|---|---|---|---|
| 窗口绑定 facade(S3) | ✅ | ✅ server 注册/生产租约(1956 撤锁切片验收) | 全绿 | 切片通过;整体待复验 | 未开始 |
| appeared/absent 差分(S4 §7.3) | ✅ facade 方法 | ⚠️ helper 级(执行链无效果差分调用点) | 全绿(7) | 未验收 | 未开始 |
| 效果/业务/receipt 分列门(S4) | ✅ effect_gate | ✅ 经 execution_ledger 接真实 CaseRunResult | 全绿(14) | 未验收 | 未开始 |
| §7.6 attempt 账本 | ✅ AttemptLedger | ✅ execution_ledger.record_case_run 真实落盘 | 全绿(15) | 未验收 | 未开始 |
| 失败步报告(§10) | ✅ report_view | ✅ execution_ledger.build_run_report | 全绿(5+6) | 未验收 | 未开始 |
| 单轮驱动(S5) | — | ✅ --local-fixture 六阶段真实运行 | 全绿(5) | 未验收 | **待授权+capability 前置** |
| 本 run 强实例跟踪底座 | ✅ instance_tracker + **生产 WindowsEventHookSource 实装并接线**(0148/0256:tracker.start→source.start 订阅+**既有窗口基线 initial_observed 语义**(基线竞争:已知 DESTROY 后 stale 条目不补活→降级/拒绝);stop→source.stop 退订确认+**结构化失败透传 end/expiry**(cleanup_errors);协商语义**显式协议**:backend 中性自报须 decided_by=instance-tracker 标记(不从函数存在/false 隐推),**显式 unsupported 始终否决**(有源+明确 false 仍拒绝);自报 strong 仍走严格门) | ✅ 逐实例代际+**alive/tombstone**(DESTROY 单独/复用→立即 LOST 0 输入;重复 create 单步进;HIDE/SHOW 连续;隔离)+ **停止流立即撤能力**(active run 源停止→能力撤销+binding LOST 锁存,focus/激活/输入前拒绝;余量仅只读回溯)+ ABI/线程(**_MSG 实分配 byref 传入**(拒 NULL);PostThreadMessageW 原生唤醒;owner worker 同线程;Unhook 返回值) | 全绿(38) | 待复验 | 真实 Windows hook 正确性需授权实机验收(本地 mock Win32 验证完整路径) |

## 1. 真实接线(2112 缺口4 修复)

- `agent/recording/execution_ledger.py`:真实 `CaseRunResult`/
  `StepResult` → `AttemptLedger`(每步 begin(动作前)→complete(动作后)
  顺序落盘;outcome 经 effect_gate 分列语义;非 pass 步骤阻断后续
  begin;动作后写盘失败→unknown 边界);
- `build_run_report`:重建历史→失败步追溯→截图契约(fixture 标注
  `is_windows_capture=false`;缺图 `evidence_missing`);
- `scripts/s5_acceptance_round.py --local-fixture`:六阶段全部真实
  运行(raw v2→真实编译器 golden v3+windowSequence→账本落盘→门→
  报告),产物落 `result-report/s5-windows-acceptance/round-N/`;
  `--live` 无授权结构化拒绝(exit 3),拒绝理由列明生产 capability
  未实现(不是占位 marker);
- **v3 公共执行/导出门保持关闭**(`_refuse_v3_execution` 未动);
  helper 接线在受控内部路径,不提前开门。

## 2. 2112 各缺口修复对照

| 缺口 | 修复 | 回归 |
|---|---|---|
| 1 驱动假绿+参数错 | 阶段五桶(negative_gate_pass/positive_flow_pass/blocked_unsupported/not_run/fail)+verdict 优先级+退出码 0/2/3;负例锁精确 code(digest mismatch/not_declared/not_found);focus-only 用协议契约 `action_id=None`(非空串);capability 拒绝计 blocked 不计通过 | test_s5_live_driver_contract.py(4) |
| 2 证据泄漏+浅脱敏 | `redact_credentials` 递归脱敏在驱动**源头**;`_redact_state` 升级为任意深度(dict/list/receipt/assertion/evidence);泄漏原件移 `live/restricted/`(sha256 记录),生成脱敏公开副本,副本核对零残留 | test_s6_attempt_ledger.py +6(合成哨兵);driver 契约测试含 V1b-raw 断言 |
| 3 unknown 断言偷推进 | effect_gate:必需业务断言**严格 pass** 才推进;unknown/skipped/缺失/非法→unknown(阻断) | test_s4_effect_gate.py +6(含真实链 0 输入) |
| 4 helper 孤立/驱动占位 | execution_ledger 桥(见 §1)+round driver 实质化 | test_s6_execution_ledger.py(6)+test_s5_round_driver.py(5) |

## 3. 生产 capability 前置(2112 任务片:底座已实现,生产订阅待实装)

生产 Windows backend 必须**实现可靠的本 run 实例跟踪**(原生生命周期
事件、generation、丢事件检测)并声明 `window_sequence/v1` 结构化能力;
证据不足维持 unsupported。**不是加一个 supported=true 声明即可** —
那是伪造。

**已完成(2026-11-04 任务片)**:`target/windowing/instance_tracker.py`
— 本 run 跟踪底座:`LifecycleEventStream` 协议(生产 WinEvent hook 订阅
实现此接口即可)、代际语义(事件流 destroy→create 同属性复用换代际;
快照-only 同属性重现如实"不可判定";属性替换=复用证据换代际;
HIDE/SHOW 不换代际)、失效降级(订阅失败/流中断/枚举错误/观察线程
故障→unavailable/unknown,不保留 stale strong)、run 生命周期一致
(经既有 policy cleanup hook;新 owner 不受旧残留影响)、capability
结构化协商(严格 supported+coverage+generation+limitations,脱敏)。
server 接线:backend 暴露 `lifecycle_event_stream()` 时建立跟踪并
协商(交集门:自报与跟踪事实任一不满足即 unsupported);无事件源 →
unavailable(自报 strong 不充分,1744 契约不回退)。

**0116 修复后状态(2026-11-04)**:生产 Windows 事件订阅已实装
(`target/automation/instance_events.py` WindowsEventHookSource —
SetWinEventHook 注册/WinEventproc 回调/消息循环线程/UnhookWinEvent
退订/有界队列溢出=丢事件事实/线程退出=unhealthy);backend 两方法
实装(window_sequence_capability 中性自报 — 跟踪事实才是权威,
不自报 strong);**代际事实进执行门**(focus/reverify 在
authorized_dispatch 边界内 sync+逐实例核对:同属性复用→旧 binding
LOST+0 输入;流故障自动传播至能力撤销与聚焦前拒绝;无观测依据的
绑定拒绝;无 bind-time 代际不偷偷采纳)。**剩余实机件**:真实
Windows hook 行为正确性(注册成功/回调时序/事件连续性)需授权实机
验收 — 本地以 mock Win32 入口验证完整代码路径;无法证明的覆盖等级
保持 unsupported。

## 4. 实机证据现状(2112 审计口径)

- 已有:部署 manifest(107 文件 sha256)、只读检查(75 工具、5 绑定
  工具注册)、live 门禁探测(digest 拒绝/生产租约/声明注册/旧 token
  拒绝/V4 capability fail closed);
- **更正**:此前"实机 S3 全链路通过"表述错误 — 实为"部署及门禁
  探测;正向绑定链被能力前置阻塞"(V4 失败、V5/V6 未运行);
- readonly 证据中 EDR 进程/窗口标志 false、interactive_session 空:
  SSH 可见进程 ≠ MCP 同用户可交互桌面可用 — 环境差额如实列出;
- 敏感原件:`live/restricted/s3-live-checks.json`(含历史 run_token,
  租约已结束仍按受限原件保护,不提交/不转发);公开核对用脱敏副本。

## 5. 剩余差额

1. 生产 backend 强实例能力实现+证据(实机放行前置);
2. appeared/absent 差分在真实执行链的调用点(S4 执行器接线,
   依赖 1);
3. Windows 实机三轮+缺陷注入(依赖 1+2+明确授权);
4. S3 整体/S4/§7.6/S5 独立验收(待复验轮)。
