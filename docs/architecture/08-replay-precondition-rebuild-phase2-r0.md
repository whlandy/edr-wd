# 回放入口窗口身份清单与受控匹配:第二阶段设计(PAPER-ONLY R1)

状态:PAPER-ONLY,修订版(R0 → R1,响应评审六项)。待评审;本阶段只定义
契约,不实现。代码改动 = 0,测试改动 = 0。实现需单独评审与验收。

> 标题更正(R1):原"通用前置重建"改为"**身份清单与受控匹配**"。入口窗口
> 真不存在时本设计仍只能拒绝 — 保存更多身份信息减少误识别、改善诊断,
> 不能让不存在的窗口出现,也不能恢复页面或产品状态。

第一阶段(07 号,已实现)服务"HiSec 已存在入口窗口的受控激活",其租约恢复
路径针对 **legacy 入口**;本阶段依赖的 root 身份完整匹配与端到端实机验证
**不能**从第一阶段直接推出,实现期需单独验收。

## 0. 数据事实与不确定性边界(R1 修正)

对真实 recording.json 核实的事实:
- `recording.windows[]`:`{title, processName, origin, closed, handle, pid,
  openedBy, reopenedBy}`。**`reopenedBy` 是已实现字段**(models.py:600,
  windows.py:1201:同标题窗口关后重开时记录)。
- 事件 `causalId` 与 `openedBy` 的引用在样本中闭环(6/6)。

核实源码后的**不确定性边界**(R0 未识别,评审指出):
- 因果归因**部分是时间猜测**:`_claim_windows_awaiting_cause`
  (windows.py:1242 区域)对"动作前刚出现的窗口"做 bounded 回溯归因,源码
  自述 "attributing it to that action is **a guess**"。
- `window_root_identity`(models.py:29)是**现有且唯一**的窗口根解析规则:
  优先控件自身 automationId 的首段(`EdrMainWindow.baseWidget...` →
  `EdrMainWindow`),否则**逆序**遍历 ancestry 取首个含点号的段;
  `ancestry[0]` 不是该规则。

**结论(R1)**:第一版**不做因果图**,只做**入口窗口身份清单**。凡涉及
"推导"之处一律改为"清单声明 + 编译期一致性校验"。因果图(可验证节点/边/
来源事件/不确定状态标注)是未来方向,本设计不承诺。

## 1. 契约 C1(R1 重写):入口窗口身份清单

GoldenTrace 顶层新增**版本化严格结构** `entryWindows`(开放问题 1 采纳:
顶层,不塞 environment 自由键):

```
entryWindows: {
  "schema": "edr.entry-windows/v1",
  "declaredBy": "recording_service",          // 声明方,非授权凭据(§C3)
  "windows": [
    {processName, titleRegex, rootAutomationId|null, origin}
  ]
}
```

- **不导出 handle/pid**(禁令不变;HWND 只允许在编译期做录制内关联,
  导出前剔除 — 见 C5)。
- **删除 R0 的 `role` 字段**(agent_entry 随 §C2-5 撤销;entry/其他不再
  分级,清单就是入口候选集合)。
- **删除 R0 的因果链承诺**;清单是录制声明,编译期可校验的是一致性,
  不是因果真值。

## 2. 契约 C2(R1 重写):身份解析统一与入口候选判定

**窗口根身份解析 = 复用 `window_root_identity`(models.py:29),一字不改**:
控件自身 automationId 首段优先,否则逆序 ancestry。R0 的"ancestry[0] 直取"
撤销。

**入口候选判定**(编译期,基于 golden 首个可执行步骤,定义与
`golden_entry_scope` 一致:第一个带 selector 的 steps/cleanup 步骤;
断言事件、窗口 transition、未编译的原始事件**不参与**):

1. 取首个可执行步骤的 `selector.window`(processName + titleRegex)与该步
   关联快照的 `window_root_identity`;两者合并为入口身份三元组。
2. `windows[]` 中身份三元组与之一致(同进程名、标题正则与录制标题可匹配、
   根身份相等)且 `origin=already_open` 的条目 = 入口候选。
3. 证据不一致、缺失或不完整(如快照缺 ancestry、根身份为 None 且
   titleRegex 缺失)→ **拒绝生成可自动恢复身份**
   (`entry_root_unidentified`),清单仍可导出供诊断,但 golden 标记
   不 ready 不可自动恢复。
4. 歧义:多个**不同**身份均满足入口候选条件 → `entry_root_ambiguous`,
   拒绝(R0 只查"同 role 同三元组"不够,评审指出 — 已修)。

**撤销 `agent_entry` 自动顺序**(R0 §2-3 整条删除):"同属 HiSec 且同时
already_open"不构成宿主/前置证据;`EDRClientAdapter`(edrclient.py:52)
明确 agent 窗口非 EDRClient 必要条件;固定顺序激活两个窗口会无必要改变
用户前台。agent 窗口只在清单中如实列出,参与歧义判定,不获得执行顺序
特权。未来若有可验证依赖证据与明确动作授权,另行设计。

## 3. 契约 C3(R1 重写):威胁模型与信任边界

R0 的"target 生成即可信"不成立:`recording.json`/golden 是本地可改文件,
`sourceRecording.sha256` 只是摘要,不构成来源证明。且 R0 中 C3(编译器只
透传)与 C2(编译期推导 role)职责冲突 — R1 明确**编译器负责推导与校验**
(§C2),清单的"declaredBy"只是**出处声明,不是授权凭据**。

**威胁模型(R0 未定义,本版定义,评审二选一取前者)**:

- **选定:防误操作模型**。清单视为**不可信的录制声明**;回放期动作只受
  target 端的真实约束把关:产品白名单、租约授权、实时窗口身份验证
  (§C4)。golden 内容本身**永远不是授权**。
- 防篡改/来源证明模型(摘要绑定、签名)**不在本设计范围**,未来需要时
  另立设计。

## 4. 契约 C4(R1 重写):目标端匹配接口缺口(诚实化)

R0 声称"复用第一阶段 focus_entry_window 家族" — **不成立**,评审指出:
现有 `focus_entry_window`(server.py)只接受 processName + titleRegex,
**没有 rootAutomationId 参数**;`focus_window_selector` 的 root_control_id
是**路径归属根**(path-owned product windows + INSTALLED_PRODUCT_SPECS
约束),与窗口根身份三元组匹配不是同一契约,不能"家族"概括。

**R1 契约**:实现阶段需为目标端新增(或扩展)一个租约授权匹配工具,要求:

1. **同时验证全部声明字段**(processName + titleRegex + rootAutomationId,
   后者存在时);任一字段无法验证 → 拒绝,**不得退回只按标题匹配**。
2. rootAutomationId 缺失(null)路径:按 processName+titleRegex 匹配,
   但匹配数 >1 → 拒绝(不猜)。
3. 检测多候选并返回结构化 `entry_window_ambiguous`(含候选身份)。
4. **枚举与激活之间的替换竞争**:枚举命中后在同一授权分派内重读身份
   (对 HWND 重验 process/pid/title,参照 `focus_existing_window` 的
   same_identity 前后双验模式),窗口被替换 → `entry_window_stale` 拒绝。
5. 以上能力不具备时整体拒绝恢复,不降级。

第一阶段工具保持原样服务 legacy 入口;新接口属实现期交付物。

## 5. 契约 C5(R1 重写):生命周期过滤与录制内消歧

R0 的"closed=true 排除"会丢失重开信息(`reopenedBy` 是真实字段),且无法
判断窗口在第一步前是否存在。R1 规则:

- **编译期允许使用 handle/pid 做**录制内**关联与消歧**(同进程同标题多
  实例靠 handle 区分;closed 后重开靠 reopenedBy 链回最后活跃实例);
  **导出前剔除**,禁止进入 golden(禁令 1 不变:不作跨次回放身份)。
- 入口候选的存在性判定:窗口在首个可执行步骤的时间点已存在
  (origin=already_open,或 origin=opened 且其 openedBy 事件时间早于该步)
  — 时间倒置(openedBy 晚于首步)→ 该条目不作为入口候选并记录诊断。
- 录制期内**同一身份三元组**对应多个 handle(重开/复用)→ 列出全部
  生命周期段,最后活跃段参与 §C2 判定;跨次回放身份仍只是三元组。
- `closed=true` 且无 reopenedBy 链回活跃实例的条目不作为入口候选。

## 6. 契约 C6(R1 重写):schema 与兼容

- golden schema v1 → v2:`entryWindows` 顶层可选严格结构(内嵌自己的
  `schema: "edr.entry-windows/v1"`,独立版本化)。
- **双读语义**:v2 读取方读 v1 文件 = 按无清单处理(第一阶阶层行为);
  v1(旧)读取方遇 v2 文件 → **显式拒绝**
  (`golden_schema_unsupported`),不静默忽略 — 未知字段处理遵循现有
  `_strict` 解析器行为,双读语义在实现期用测试锁定。
- v2 **写入条件**:仅当录制清单满足 §C2 校验(否则写 v2 但
  `entryWindows` 置空/缺省,回退第一阶层行为)。
- "旧 golden 行为逐字节一致"改为可测试表述:**语义与调用序列兼容**
  (相同输入 → 相同处置序列与错误码);原文件字节是否允许重序列化由
  实现期决定并锁定。

## 7. 禁令(继承 R0,R1 补强)

1. 不把 handle/pid 当跨次回放身份(编译期录制内关联允许,导出剔除)。
2. 不在未持有租约时执行任何匹配/激活动作。
3. 不启动进程/服务/点击控件(与第一阶段同边界;启动客户端是独立授权,
   另立设计)。
4. 不把矛盾结果硬判为可恢复;枚举后必须重验身份防替换竞争。
5. 不放宽 locator 的显式歧义拒绝。
6. golden/录制文件内容本身不是授权(C3)。
7. rootAutomationId 无法验证时**不降级**为仅标题匹配(C4)。

## 8. 验收门槛(R1,实现期引用;R0 样本局限已吸收)

**正例**:唯一匹配激活;多实例经录制内 handle 关联后唯一化。

**反例(R0 未列,R1 必须逐项可复现)**:因果引用存在但来源窗口错误;
归因时间倒置(openedBy 晚于首步);因果循环;多个 entry 候选;同标题
不同根;同根不同实例(重开/句柄复用);录制期关闭后重开(reopenedBy);
清单被篡改(身份与事件快照矛盾);旧版读取方遇 v2;枚举与激活之间窗口
被替换;rootAutomationId 存在但目标端无法验证 → 拒绝不降级。

**兼容**:v1 golden 语义与调用序列兼容(可测试表述,§C6);v2 读取 v1
回退;v1 读取 v2 显式拒绝。

**实机**:与第一阶段同一监督轮;root 身份完整匹配与端到端验证**独立
验收**,不从第一阶段推出。离线通过 ≠ 实机通过。

## 9. 开放问题(R0 三项,评审建议已吸收)

1. **字段位置**:GoldenTrace 顶层(采纳;见 C1)。
2. **agent 顺序**:不引入自动顺序(采纳;agent_entry 撤销,见 C2)。
3. **schema 升级**:先明确双读/v2 写入条件/旧读取方拒绝/未知字段处理
   (已写入 C6),发布时点在实现评审时定。
