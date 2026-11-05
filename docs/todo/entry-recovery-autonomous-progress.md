# 回放入口窗口受控恢复 —— 无人值守推进进度

设计文档: docs/architecture/07-replay-entry-recovery-r0.md(归档 doc/)
定时任务: entry-recovery-r0-loop,每 10 分钟一轮
纪律: 只做离线验证;实机验收只登记不执行;不 commit 不 push。

---

## 上一轮审核建议(必先逐条处理)

1. **已修复(并发轮发现)**:lease_entry_path 下 begin 失败时 `_release_acquired_target(unlock_window=legacy_lock)` 会越权调 unlock_window —— 已改为 `legacy_lock and not lease_entry_path`,由并发测试锁定。下轮请复查默认路径(非 lease_entry_path)是否仍需 legacy unlock:是,历史语义保持,批次 12 测试全绿。
2. **_recovery_audit 的 except 分支不一致**:invalid_response 与 disposition 路径的审计失败用 logger.warning 单独报告,但 audit 函数自身内部 append_dict 失败仍会抛 recovery_audit_write_failed —— 两层 try 语义重叠,建议审查职责边界,把"何时允许吞审计失败"收进单一位置,避免语义漂移。
3. **unknown 审计测试依赖 jsonl 裸解析**:payload 存储格式若变测试会脆断;可改用 store 公共 API 读回。
4. **定时任务平台缺陷**:entry-recovery-r0-loop 7 连败(API 400),已禁用。重建时提示词需避免与平台 system message 冲突的结构。

## 已完成(截至 2026-11-02,人工会话)

- [x] 增量 1:acquire_target_window recovery 钩子(默认关;仅 connect_target_not_found;只读重确认;entry_window_absent)。CLI replay --entry-recovery。
- [x] 增量 2:目标端 focus_entry_window(租约授权;只读枚举→唯一匹配才精确连接+严格锁+激活);agent 端租约路径(trace.open→begin_product_run→focus_entry_window);RECOVERY_REQUESTED/RESULT 审计先于回放第一步;默认关=历史顺序不变。
- [x] 测试:test_acquire_target_window.py 13 个、test_entry_recovery_lease.py 9 个;录制目录 1396 passed/1 skipped/0 failed。
- [x] 文档归档 doc/07-replay-entry-recovery-r0.md。
- [x] 验收·异常与审计(2026-11-02 人工续):传输异常→审计 result=unknown 且原异常不带型上抛(结果未知≠未执行);处置审计写失败→单独报告不掩盖真实错误码;请求审计写失败→recovery_audit_write_failed 且零副作用(focus_entry_window 未被调用)。3 个新测试锁定。
- [x] 验收·权限与副作用(2026-11-02 人工续):test_focus_entry_window_side_effects.py 7 个 — 调用白名单锁定行为面(缺失/歧义处置只读零变更;成功路径仅 is_window_open/connect_window_identity/lock_window/get_window_lock;activate_edr/启动应用/服务操作等危险方法触达即红;连接失败不继续锁定;注册表绑定失败释放已取得的锁;无效租约零方法调用)。
- [x] 验收·并发(2026-11-02 人工续):test_entry_recovery_concurrency.py 3 个 — 其他 run 持租约时恢复整体不发生(不 focus 不越权 unlock);begin 阶段传输异常原样上抛+trace 以 aborted 终结;租约在 focus 时过期→product_run_expired 透传且审计如实记 expired。修复:lease_entry_path 失败释放不再越权 unlock_window(runtime.py 一行,测试锁定)。
- [x] 验收·触发与身份(2026-11-02 人工续):test_entry_recovery_identity.py 3 个 — rooted golden(带 rootAutomationId)装配期零恢复调用(legacy_lock=False 排除,测试锁定);标题漂移窗口按完整身份过滤排除(absent,绝不锁错窗口,activate_edr 触达即断言失败);恢复请求身份来自 golden scope 原文(非 EDR 进程同样可用,不被 profile 自报改写)。

- [x] R2 评审修复(2026-11-02 人工续,四项全收):P1-1 handle 关联重写 `_entry_window_identity`——selector/manifest/快照按首步源事件 handle 关联同一录制窗口,同进程兄弟窗口不能背书(两个反例测试锁定);P1-2 entry 证据问题关闭 ready 门(golden+case 均 incomplete,回放门槛真正拒绝);P1-3 lease_entry_path 与 legacy_lock 解耦(rooted v2 golden 也走租约入口获取,root 匹配不再是死路径);P2 声明身份整体作为权威输入(process/title/root 全来自 entryWindow,不与 scope 混拼)。handle 要求仅在 manifest 存在时提出(v1 旧录制兼容矩阵保持)。新增 5 个对抗回归,全量 **1427 passed/0 failed**。真录制冒烟:supervised_1101 v2+身份(incomplete=其他既有诚实门)、hisec_final v1 无诊断、independent_unique_0426 v2+身份 ready。

## 待做(R0 验收门槛,离线可做)

- [ ] 实机监督轮(登记项,严禁无人值守):第一阶段受控恢复 + R2 root 完整匹配端到端。
- [ ] R2 遗留(评审范围收敛时推迟):多窗口清单与生命周期解释;entry_identity_* 诊断是否计入 case status(当前 golden 级,不计入);未知字段/错误码路径实测(评审"版本补充"项)。

## 待监督实机轮(只登记,严禁无人值守执行)

- R0 第一阶段验收「实机」项:确认目标/用户会话/控制者安全后,真实窗口受控恢复 + 严格身份校验 + 原始录制回放;记录请求/响应/时间/截图哈希。可与 §6o/6v/6w/6x/6y/6z/7a 批次并入同一 11.26 监督轮。
- 第二阶段实现(需 R1 评审通过)+ 实机 root 身份完整匹配端到端验证(独立验收,不从第一阶段推出)。

## 待监督实机轮(只登记,严禁无人值守执行)

- R0 验收「实机」项:确认目标/用户会话/控制者安全后,真实窗口受控恢复 + 严格身份校验 + 原始录制回放;记录请求/响应/时间/截图哈希。可与 §6o/6v/6w/6x/6y/6z/7a 批次并入同一 11.26 监督轮。

## 每轮记录

### 2026-11-02(人工会话,建任务)
建立本文件;登记初始状态。无代码改动。
