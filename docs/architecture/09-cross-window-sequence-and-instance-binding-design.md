# 跨窗口录制回放：步骤窗口序列与运行实例绑定设计

版本：R1；日期：2026-11-03；状态：分片实施中。S0、S1、S2已本地独立验收；S3–S5及实机尚未验收，详见§13。

本文件取代会话中的 R0 草案。沿用 07/08 的产品归属、租约、严格匹配和防误操作边界；扩展窗口序列语义的部分以本文件为准。已有 v1/v2 golden 的行为保持兼容。本轮仅新增设计文档，不修改业务代码、不部署、不发布。

## 1. 目标与非目标

目标：每个可执行步骤明确属于哪个录制窗口实例；回放时将它绑定到本次运行的真实窗口实例，并在后续步骤持续验证。返回主窗口、同名窗口共存、关闭后重新出现等场景必须有明确处置。证据不足就保留缺口，不能通过编号把歧义包装成唯一。

成功标准是“能正确回放并通过断言发现产品缺陷”，不是“尽量让录制 ready”。窗口链证明执行对象，业务断言证明行为结果，两者不可相互替代。

本版不做：自动启动程序/服务、登录、重建页面、自动关闭干扰窗口、失败后重复点击、任意因果图执行、依据前台/时间/枚举顺序猜窗口、自动修改已有录制使其 ready。不要新增 `openedNewWindow` 布尔冗余字段。

## 2. 已核实事实与 R0 修正

核实对象为当前源码与本地 `recordings/supervised_1102b/` 的 recording、golden、compile-report。样本结论仅适用于这份历史材料，不能当作新实现的验收结果。

| 事实 | 位置/影响 |
| --- | --- |
| 每步 selector 已有自己的 process/root 或 titleRegex | `agent/recording/selectors.py::window_for_event`，已有跨窗口能力 |
| 有 root 时 selector 通常省略 titleRegex | root-only 身份必须贯穿解析、工具参数与运行匹配 |
| 回放通过 `focus_window_selector` 聚焦 root 窗口 | `agent/recording/mcp_runtime.py::focus_window`，不能把所有窗口类型概括为这个路径；title-only 使用另一条 session 路径 |
| 聚焦缓存按 process/title/root 三元组命中 | `agent/recording/replay.py::_focus_for`，相同 selector 不等于同一实例，新增实例绑定后不能继续只用此缓存 |
| manifest 以 handle 为键并原地改 closed/reopenedBy | `target/recording/windows.py::_register_window/_close_window`，末态摘要不能重建所有生命周期段 |
| SHOW/HIDE/DESTROY 被投影为 opened/closed | 同文件窗口事件 hook；现有 closed 不能直接等同物理销毁，reopenedBy 也不能证明新实例 |
| openedBy 正向来自最近动作；回溯归因源码称为 guess | `_window_transition/_claim_windows_awaiting_cause`；“存在 causalId 引用”仅证明关联，不证明直接因果 |
| manifest 写 openedBy 在 transition 的时间归因拒绝之前 | 即使 transition 最终无绑定，manifest 也可能保留最近动作引用，不能任选一份当高可信证据 |
| 目标已有会话内 binding/generation/tombstone | `target/windowing/models.py/registry.py/runtime.py`；可复用，但轮询遗漏的同 HWND 同属性替换仍须能力说明 |
| raw 与 golden 模型使用严格字段白名单 | 新可选字段也会被旧读取方拒绝，不能称“加字段不破坏 v1 schema” |

### 2.1 supervised_1102b 的精确结论

该样本有 15 个 raw 事件、9 个 compiled steps、4 个 manifest 条目：

| 录制窗口 | handle（仅此表用于关联证据） | manifest | 步骤归属 |
| --- | --- | --- | --- |
| 主窗口，EdrMainWindow | 13239958 | already_open，末态未关闭 | step-0001、step-0008；raw sequence 1、10 |
| 日志中心，LogCenterWindow | 722784 | opened，末态 closed | step-0002 至 step-0007 |
| EDRClient，title-only | 788320 | opened，末态 closed | step-0009；raw sequence 12 |
| EDRClient，title-only | 5768856 | opened，末态 closed | 无独立操作步骤，仅 transition |

两个 EDRClient 窗口先后出现，但前一个在后一个出现后才关闭，存在重叠。只能称不同窗口，不能认定为替换关系。

现有 compile-report 的窗口歧义标在 raw sequence 1/10，即 compiled step-0001/step-0008，并非“compiled 步骤 1/10”。两步实际使用相同主窗口 handle。`compiler.py` 的 collapsed-window 检查将当前动作与紧邻的上一个动作比较，而不是与该 selector 首次出现的源窗口比较，因而主窗口 → 日志中心 → 主窗口被误报。此问题应独立修正比较对象，不能依赖新增 windowRef 兜底。

step-0009 的 `compile_weak_identity_unresolved` 是控件身份缺口。快照 CS-0003/CS-0005 均未提供 EDRClient 窗口 root，相关历史帧也没有 `complete=True`。窗口序列不能补出 root、完整控件树或唯一控件；不承诺样本整体 ready。

历史样本 MAA 导出还明确拒绝 golden v2；这是导出器兼容缺口，不能静默降级。v3 需显式拒绝或提供等价实现，不能丢失窗口契约后宣称导出成功。

## 3. 三种身份，严格分工

1. **录制实例**：本次录制中的窗口实例，例如 `RI-0001`。原生 handle/pid 只留在 raw 和受控本地复现材料；不同实例可以有相同标题、root，甚至复用 handle。
2. **golden 引用**：`W1/W2/...` 是对录制实例的确定性别名，表达步骤归属。它不定位回放窗口，不是身份充分性的证明。
3. **回放绑定**：目标端持有 `W1 → 本次运行 WindowIdentity.binding`。binding 包含本次 session/native-id/process-id/root/generation，绝不来自历史 handle。

golden 只保存稳定身份声明与 W 引用；本次原生身份保存在目标内存中。agent 使用不透明绑定 token，不把 token、handle、pid 当可跨运行持久化身份。常规报告仅记录窗口 W 引用、绑定修订号、候选数和脱敏证据。

标题与 root 都存在时验证两者；只有 root 时不补标题限制。title-only 仍是合法弱窗口身份，但首次绑定必须实时唯一，后续必须沿同一绑定验证。

## 4. 录制协议：先保存事实，再表达关联

### 4.1 raw 版本与记录结构

新增 `edr.desktop-recording/v2`；新读取方兼容 v1，新录制器写 v2。保留 windows 的末态摘要供展示；v2 新增以下严格结构：

```json
{
  "schema": "edr.desktop-recording/v2",
  "windows": [
    {"instanceId": "RI-0001", "handle": 13239958, "pid": 1984,
     "processName": "EDRClient.exe", "title": "logo1", "origin": "already_open"}
  ],
  "windowTimeline": {
    "schema": "edr.recorded-window-timeline/v1",
    "events": [
      {"id": "WE1", "order": 1, "monotonicMs": 1000,
       "instanceId": "RI-0001", "kind": "initial_observed",
       "source": "initial_inventory"},
      {"id": "WE2", "order": 3, "monotonicMs": 1200,
       "instanceId": "RI-0002", "kind": "shown", "source": "native_show",
       "associatedActionCausalId": "CAUSE-X",
       "association": "temporal_forward"}
    ],
    "coverage": {"destroyEvents": "supported", "droppedEvents": 0}
  }
}
```

上例为新增字段投影，省略 raw 其他必填字段。业务事件和 snapshot 的 window 记录均增加 instanceId；窗口 transition 也引用 instanceId 与 timeline 事件 ID。统一 ingress order 记录接收顺序，monotonicMs 记录观测时间；二者不一致时保留信息，不据此伪造动作因果。点击可能在 mouse-up 才生成，不能强制“点击完成时间 < 窗口显示时间”。

`kind` 枚举：initial_observed、created、shown、hidden、destroyed、identity_changed、lost、observed。source 为固定枚举；lost 表示枚举缺失或跟踪丢失，不等于 destroyed。coverage 缺失、事件丢失或队列溢出必须进入诊断；不能宣称该区间连续性已证明。

### 4.2 实例与生命周期规则

- 首次观察分配实例 ID；命名按录制内首次可信观测顺序生成，不能由标题生成。
- HIDE → SHOW 在未观察销毁/原生身份改变且跟踪完整时仍为同一实例；表示同实例的新可见阶段。
- DESTROY → 同 handle 再出现，或 pid/进程启动身份/窗口身份明确变化，分配新实例 ID。老实例保留历史，禁止覆盖。
- 枚举短暂消失不能直接判新实例。若无法确认连续性，标记 unknown；需要实例连续性的步骤保持 incomplete。
- 同进程同标题同时存在两个窗口，分配两个实例。标题变化只是属性观测；root 改变需记录 identity_changed 并阻止旧身份绑定继续使用，不凭 title 匹配掩盖变化。
- macOS 需声明等价 AX/原生实例能力；能力不足时拒绝强连续性功能，不伪装支持。

### 4.3 关联级别与直接因果

不采用 `causal / temporal_guess / unattributed` 三分法，因为它会把现有正向时间归因误标 causal。统一使用：

| association | 来源 | 可自动生成强 opens 效果 |
| --- | --- | --- |
| explicit | 用户确认的绑定窗口断言，或未来有可验证动作协议直接返回窗口实例 | 可生成声明的后置条件，但仍不声称物理直接因果 |
| temporal_forward | 最近动作后观察到窗口 | 否 |
| temporal_backfill | 显示后按时间回溯补动作 | 否 |
| unknown | 无来源、旧录制仅有 openedBy，或证据矛盾 | 否 |

字段表达关联的来源，不是“置信度百分比”。显式绑定断言表达用户要验证的结果，也不是产品内部因果证明。旧 openedBy/reopenedBy 兼容读取，但禁止补填 explicit。一个 causeConfidence 字段不足以补全生命周期与事件级来源，因此不单独扩展旧 manifest 充当实现。

## 5. golden 协议：v3 窗口序列

新增 `edr.desktop-golden-trace/v3`，新增严格结构 `windowSequence`。其语义改变执行对象与门槛，必须升级顶层版本；仅在 v2 添加可选结构虽会被严格旧读方拒绝，但不能清楚表达 v3 所要求的运行行为。

示例只展示窗口契约，其他 golden 字段沿用既有定义：

```json
{
  "schema": "edr.desktop-golden-trace/v3",
  "windowSequence": {
    "schema": "edr.window-sequence/v1",
    "windows": [
      {"id": "W1", "identity": {"processName": "EDRClient.exe",
         "rootAutomationId": "EdrMainWindow"}, "origin": "already_open"},
      {"id": "W2", "identity": {"processName": "EDRClient.exe",
         "rootAutomationId": "LogCenterWindow"}, "origin": "observed_during_flow"}
    ],
    "steps": [
      {"stepId": "step-0001", "windowId": "W1",
       "effects": [{"id": "EF1", "kind": "appeared", "windowId": "W2",
         "verification": "explicit_assertion", "timeoutSeconds": 15}]},
      {"stepId": "step-0002", "windowId": "W2"},
      {"stepId": "step-0008", "windowId": "W1"}
    ],
    "cleanup": []
  }
}
```

上例是结构片段；完整文件的 steps/cleanup 映射必须完整覆盖 golden 的全部执行节点。无需存 previousWindowId：由编译后的步骤顺序派生，不从 raw 相邻事件派生。所有实际打开/关闭关系放 effects 数组，支持一个动作出现多个窗口；不采用单值 opensWindow，避免遗漏弹框/辅助窗口。

effects.kind 第一版只支持 appeared 与 absent。appeared 表示**相对动作前基线新出现或从不可见变可见**，不等于新建物理窗口；absent 表示指定绑定不可见/不存在，绝不等于进程退出或窗口被物理销毁。新实例身份与可见状态分别处理。

`timeoutSeconds` 采用现有验证器规则并设硬上限；样本等待时间可作为人工建议，不能凭延迟延长为无限等待。每个效果有稳定 ID，同源 window_open 断言合并为一个结果，不重复计数。

### 5.1 严格校验

- windows.id、stepId、effect.id 唯一；所有引用存在；每步和 cleanup 恰有一个归属。未知字段、重复、悬空引用全部拒绝。
- identity 必须有非空 processName，以及 rootAutomationId/titleRegex 至少一项；root/title 均声明时不能任意删弱字段来凑匹配。
- selector.window 与对应 identity 必须完全符合相同规范化规则。windowRef 不加入 selector，避免两套权威身份以及所有 locator 消费方同步扩展。
- v3 必须有 windowSequence；不允许“v3 缺字段回退为 v2”。无法生成序列时保留 incomplete 与诊断，不标为可执行 v3。
- entryWindow 如存在，须与首个有 selector 的主步骤所引用窗口一致；冲突拒绝。没有主步骤时不从 cleanup 发明入口。
- 不在 effects 中引入重复执行动作的指令。对一个后置条件无法证明唯一关联的情况，不生成强效果。
- 黄金文件不携带 raw handle/pid、绑定 token、录制 instanceId。原始来源索引和具体冲突证据保存在本地 compile-report，应用既有脱敏规则。

## 6. 编译算法与 ready 门槛

### 6.1 显式维护源事件映射

编译器生成每一步时同步维护 `stepId → source event(s)`，涵盖点击合并、导航键合并、独立断言、绑定断言、transition 折叠、cleanup。禁止靠两个列表 zip 猜对应关系。一个动作可对应多个来源事件，一个绑定断言可投影为该步效果而不单独占步骤。

### 6.2 窗口归属

对每步源事件：instanceId → raw 窗口实例 → 同实例、有效生命周期内的 snapshot/root → 稳定身份。processName 按后端既有规范化规则处理；不跨进程聚合，不跨实例借快照，不用录制末态 closed 反推早期窗口不存在。

有明确 root 且同实例证据无冲突时使用 root；root 无法从事件或对应快照可靠得到时保留 title-only。snapshot 缺 complete 不自动否定窗口属性的单条正证据，但绝不能用它证明“无其他控件/窗口”；控件唯一性仍要求既有完整性契约。

同实例同时段出现多个冲突 root，或时间/实例无法关联，保持 incomplete。允许标题随时间变化；不能跨实例或跨代挑一个“刚好匹配”的标题/root。保留结构化证据说明冲突来源。

### 6.3 修正已有歧义误报

先修 collapsed-window 算法：同一个 selector 再出现时，与该 selector 首次来源的实例比较，而非与紧邻的上一个动作比较。主窗口 A → 对话框 B → 主窗口 A 不应误报；A → 同 selector 的另一实例 A2 仍须证明独立实例与绑定策略。

这项修复独立验收，不以 windowSequence 的存在为放行条件。历史 v1 若仅有同一录制 handle 的明确正证据，可消除“不同窗口”这一特定误报；不能因此证明所有生命周期区间都完整。

### 6.4 何时可以解除实例歧义

W 编号本身不够。只有以下全部成立，才能让原窗口实例歧义变为可执行：录制实例关联完整；稳定字段一致；每次首次绑定有可执行的唯一匹配策略；后续可验证同一运行绑定；两个 W 不会无依据映射到同一个本次实例。需要“动作后新出现”才能区分的窗口，还必须有 explicit 效果或等价可信前后观测契约。

若录制中同标题/同 root 实例共存：允许先唯一绑定 W1，之后通过明确 appeared 效果的增量候选唯一绑定 W2；两个实例在回放首次绑定前就共存且无更强证据，拒绝。不得按窗口 z-order/最近前台/第几个枚举结果选取。

### 6.5 业务断言不能被窗口断言替代

序列只修复窗口归属。step-0009 这类控件弱身份仍要通过已有 locator、完整 tree 或明确 visual 模式契约。窗口出现只验证窗口变化，不能满足 scroll/drag 的内容或位置结果断言，更不能代替文字、行数、时间范围等业务断言。

编译中任何阻断问题都同时关闭 case/golden ready。v3 不允许编译报告报错而 artifact ready。问题需定位 step、源事件、windowId、证据缺项，不能只给全局字符串。

## 7. 回放：租约内绑定状态机

### 7.1 状态与入口

每个 W 在目标端状态为 UNBOUND、BOUND、HIDDEN、LOST、RETIRED；保存绑定时的 WindowIdentity、关联租约 run 和观测修订号。LOST 不是可重新猜选的 UNBOUND；无已声明转换时直接停止。

开始回放时校验 schema、引用和目标能力，然后获取现有 product-run 租约。v3 无所需能力时在首个 GUI 副作用前拒绝 `window_sequence_capability_unavailable`。租约拒绝后不得枚举激活、裸调 legacy connect 或释放他人的锁。

首次访问 already_open 窗口时，在授权产品域内枚举完整候选并唯一绑定。首次访问流程中出现的窗口，若无 explicit 效果，可在其第一步按稳定身份实时唯一绑定，但只证明“当前唯一”；若场景必须区分同身份多个实例而无效果证据，则编译 incomplete，不延迟猜测。

### 7.2 每步执行顺序

1. 校验预期上一步执行结果和当前 sequence 游标，不要求切换前的前台仍是上一步窗口；弹框可能已合法改变前台。
2. 目标端刷新实例 inventory，验证 W 的现有绑定或唯一完成首次绑定。
3. 聚焦并严格锁定该绑定的精确原生实例；聚焦后重新校验产品归属、binding、可见性、前台和全部声明字段。
4. 在该窗口新观测下解析控件；目标 action dispatcher 在同一租约/序列化 GUI 操作边界再次验证绑定，随后执行。检查与执行之间不能释放校验边界，agent 端“看过一次前台”不构成原子防护。
5. 校验动作 receipt、窗口效果和已有业务断言。仅全部满足后提交 sequence 游标，进入下一步。

同 windowId 连续两步也必须重验绑定；缓存只可减少重复激活，不能跳过失效检查。窗口切换允许有必要的现有聚焦副作用；观察 appeared/absent 时只枚举，不为验证效果额外激活窗口。

### 7.3 appeared 的前后差分

动作前在租约内记录匹配候选与绑定基线。动作只执行一次；后续只读轮询，直到满足声明效果或截止时间：

- already visible 的同名旧窗口不能满足 appeared。
- 新候选须在产品域内满足完整身份；差分仅一个才能绑定。多个新候选拒绝，零候选到期失败。
- HIDDEN 的既有绑定重新显示可以满足“同实例出现”；新实例 W 必须对应新本次实例，不能沿用老绑定。
- 轮询无法观察短生命周期窗口时返回无法判定/超时，不能当 pass。需要捕获该场景的 profile 必须提供生命周期事件覆盖。
- transport 中断或 receipt 不确定：动作结果 unknown，停止自动推进；不重试点击。报告可保留窗口观测，但不靠“后来出现了窗口”推断动作成功。

动作期间的前台自然改变不能导致旧窗口刷新先失败而跳过效果。需调整现有 post-action observation 顺序：先读取动作 receipt 和只读窗口效果，再按各断言自己的窗口归属取观测。不得将源窗口控件断言静默迁往新窗口。

### 7.4 同名窗口与重开

首次绑定 W 后，即使出现同身份兄弟窗口，也继续只使用原 binding；若原 binding 消失，不选兄弟顶替。若产品允许关闭后重新打开同一物理对象，必须有明确的同实例 HIDE/SHOW 观测。真实销毁重建使用新的 W，并需独立绑定策略。

目标 generation 必须来自覆盖充分的本次实例跟踪。现有 registry 依靠枚举与属性比较，不足以保证捕获“两个枚举之间 HWND 销毁并以相同 pid/root/class 复用”；需要将原生 DESTROY/CREATE 事实接入 run 生命周期，或明确拒绝此强实例能力。不能在设计中默认现有 generation 已覆盖所有替换。

### 7.5 cleanup 与失败

cleanup 也是显式映射的步骤，遵守相同绑定规则。主流程失败后，仅执行既有政策授权且当前绑定可验证的 cleanup；无法验证则跳过并记录，不自动找替代窗口。结束、异常、租约过期均清理本次内存 binding、基线和 token，使用既有租约释放路径。

### 7.6 可回溯的步骤状态（用户追加验收要求）

跨窗口时，下一步必须能读取上一步的确定状态，不能只从前台窗口或 selector 缓存推断。录制端、编译端和回放端分别维护原始事件顺序、编译步骤来源映射、本次执行记录；三者通过稳定引用关联，不能把录制结果当本次执行结果。

本次运行的步骤上下文至少包含 runId、stepId、attemptId、previousStepId、previousAttemptId、windowId、previousWindowId、transitionId、stateBeforeRef、stateAfterRef、actionReceiptRef、assertionResultRefs、evidenceRefs 和 outcome。首步 previous 引用显式为空；后续引用实际执行的前一步 attempt，不能引用失败重试之前或其他 run 的旧状态。若发生重试，必须单独记录 attempt，沿用既有允许的重试规则，不因此新增 GUI 动作重试能力。

stateBeforeRef/stateAfterRef 指向不可变的脱敏状态记录，含序列游标、窗口绑定修订号、可见性/窗口效果结果和观测引用；原生 handle/pid、租约 token 不进入此持久状态。上一动作执行成功、断言失败、后置状态未知必须分别表达。outcome 至少区分 pass、fail、unknown、skipped，unknown 不能推进为成功。

next-step preparation 接收上一状态记录的只读视图，并显式记录“继续同实例 / 切换已绑定实例 / 首次绑定新实例”的转换与依据。上一结果不允许推进时，下一步不能靠重新聚焦绕过失败。状态历史采用 append-only 事件和稳定事件 ID；游标提交、步骤结果与状态引用须按可检测的顺序持久化，写入失败停止后续副作用。动作后写入失败则标记不确定边界，不能声称动作未执行。

验收必须能从任意失败步在 JSON 与报告中追溯：上一步是什么、在哪个窗口、哪个 attempt、执行与断言结果、为何切窗、前后状态与截图证据。重启后可从已落盘记录重建执行历史；本版不提供自动恢复执行或复用失效 binding。特别覆盖 A→B→A、同名两实例、上一断言失败、动作回执 unknown、断线/取消和状态写入失败。

## 8. 接口与能力边界

新增 target capability `window_sequence/v1`，声明支持的实例跟踪等级、root/title-only 完整匹配、按绑定聚焦、动作前绑定校验、只读窗口差分与事件丢失检测。安装了工具名不等于能力齐备。

逻辑接口职责：

- `bind/resolve`：身份声明、windowId、run_token → 唯一候选及不透明 binding token。
- `focus_bound`：token、run_token → 精确聚焦锁定并重验；复用现有产品窗口约束。
- `observe_effects`：基线 token、效果声明、run_token → 只读观测与结构化结果。
- action dispatch：携带 binding token/预期修订号，执行前拒绝失效实例。

实施可扩展现有工具或新增 facade，不能让 agent 用历史 HWND 控制目标，不能把黄金 W ID 当授权凭据。root 窗口和 title-only 窗口都要经同样产品域检查与精确实例路径；title-only 无法证明归属时拒绝。

原先“零新增副作用能力”应精确表述：不新增动作类别和产品授权域，但会新增按实例受控聚焦/校验协议，需要独立能力审查和实机验收。已有工具内部的锁、租约、路径归属约束必须完整保留。

## 9. 版本兼容、迁移和导出

| 输入/目标 | 行为 |
| --- | --- |
| 新 reader + raw v1 | 兼容读取；仅有 openedBy 不推断 explicit；有确定窗口归属时可生成诊断序列 |
| raw v1 缺生命周期证据 | 不伪造实例代际；需要强实例语义时 incomplete，保留缺口 |
| 新 reader + golden v1/v2 | 保持既有执行语义与调用序列，不自动升级 |
| 新 compiler + raw v2 | 默认生成 v3；缺证据仍产出可诊断 incomplete artifact |
| 老 reader + raw v2/golden v3 | 显式拒绝；具体 unsupported-schema/unknown-field 错误顺序由实际解析器测试固定 |
| v3 + 不支持 window_sequence 的目标 | 首次 GUI 副作用前拒绝，不降级为只看 selector |
| generated pytest / MAA /其他 exporter | 保留完整等价契约，或显式 unsupported；禁止丢 windowSequence 后成功导出 |

不原地覆写用户旧 recording/golden。迁移输出写新目录，记录源摘要、编译器版本、配置、缺口和差异；摘要用于复现关联，不是来源签名。schema/model/JSON schema/工具能力/生成器需一并完成版本测试。

## 10. 诊断与带图片报告

报告按步骤显示：前一步窗口 → 本步窗口、稳定身份的脱敏摘要、首次绑定/复用/失败原因、窗口效果的前后候选数、业务断言结果、耗时与缺失证据。窗口效果和业务断言分列，不把动作成功写成业务通过。

正常步骤沿用现有截图证据开关；开启带图报告时捕获脱敏的动作前/后图片，失败步优先保留可取得的截图，标注实际截图对应的 W 与观测时间。已有帧可以作为上下文，但不能冒充失败瞬间；无法截图明确记 evidence_missing，不能制作或复用无关图片。控件差异仅在有同一观测坐标基准时标注。

报告及审计不持久化 token。窗口标题、进程名、OCR、控件文本、用户目录与截图继续使用已有源端脱敏和落盘清洗；新增序列不能绕过这些规则。compile-report 的 raw 来源信息按受控本地材料处理，发布报告只引用稳定步骤/窗口 ID。

关键错误分类：

| 阶段 | 建议 code | 含义 |
| --- | --- | --- |
| compile | compile_window_source_unresolved | 步骤到实例无法可靠关联 |
| compile | compile_window_lifecycle_unknown | 所需生命周期区间无法确认 |
| compile | compile_window_identity_conflict | 同实例身份矛盾 |
| compile | compile_window_binding_unprovable | 缺少可执行唯一绑定策略 |
| compile | compile_window_effect_unverified | 关联不足以生成所要求的强效果 |
| runtime | replay_window_sequence_invalid | 引用、顺序或入口契约非法 |
| runtime | replay_window_binding_ambiguous | 首次/增量候选不唯一 |
| runtime | replay_window_binding_lost | 原实例绑定失效，不允许替代 |
| runtime | replay_window_effect_timeout | 截止时间未满足 appeared/absent |
| runtime | replay_window_effect_undetermined | 事件丢失、观测/动作结果不确定 |

沿用既有 permission、lease、transport 失败，不统一包装成 chain_broken；保留原因才能判断产品缺陷、录制缺口或基础设施故障。

## 11. 实施切片与验收

| 切片 | 交付 | 放行条件 |
| --- | --- | --- |
| S0 事实与误报修正 | 冻结脱敏样本/摘要；修正同 selector 比较对象；独立反例 | A→B→A 不误报；A→A2 仍拒绝；step9 弱控件问题保持 |
| S1 raw 生命周期 | raw v2、instanceId、时间线、事件来源与覆盖声明 | HIDE/SHOW、DESTROY/复用、乱序、丢事件、双击合并、时间归因均如实落盘 |
| S2 编译与 v3 | 源事件映射、完整序列、严格解析、ready 门槛、版本/导出契约 | 正反例、model/schema 一致；确定性编译；历史 v1/v2 兼容 |
| S3 目标绑定 | 能力协商、租约下本次实例绑定/聚焦、dispatch 前校验 | 替换、同名多实例、权限失败、租约过期均阻止错误动作 |
| S4 效果与报告 | appeared/absent 差分、独立业务断言、带图报告 | 旧窗口不冒充新出现；不重复点击；窗口断言不冒充内容断言 |
| S5 实机验收 | Windows 专用 fixture 与真实产品受控回放 | 全链结果、截图、实例事件与审计闭环，无误操作证据 |

每片先补失败回归再实现；独立评审只核对该片契约。S0 不需等整个 v3 完成；S2 只有 schema/序列输出不能宣称回放能力交付；S3/S4 完成前 v3 保持禁止执行。

### 11.1 必测矩阵

1. 主窗口→日志中心→同一主窗口，正确复用 W1；原 collapsed-window 反例稳定通过。
2. 同进程两个同标题不同 root；同 root 两实例；首次即多候选均拒绝。
3. W1 先绑定、W2 后出现；增量唯一才能绑定 W2；基线旧窗口不满足 appeared。
4. HIDE/SHOW 复用同实例；DESTROY/相同 handle 重建为新实例；轮询遗漏时不可虚称连续。
5. W1 消失后同名兄弟仍在，不能顶替；focus 与 dispatch 之间发生替换，动作执行次数为零。
6. 事件/快照来自错误 handle、错误实例或错误生命周期段；root/title 冲突；process 归属错误。
7. 双击、HOME/END 合并、绑定断言、跨窗口断言及 transition 折叠，source 映射不偏移。
8. temporal_forward/backfill/旧 openedBy 不能生成 explicit；延迟太大或多动作干扰不提升置信度。
9. 窗口效果通过但业务文字错误，整步失败；scroll/drag 只有窗口效果仍缺业务 verifier。
10. 一步多个窗口效果；窗口瞬开瞬关；动作 receipt unknown；等待可取消且有上限。
11. root-only 和 title-only 参数；缺目标能力、非法 token、租约竞争/到期，不出现越权 GUI 动作。
12. v1/v2 行为兼容、v3 严格拒绝缺字段、未知字段、循环/前向非法引用；生成器和导出器不静默丢契约。
13. cleanup 原绑定丢失、报告落盘失败、脱敏截图失败、敏感标题/token 不泄漏。

### 11.2 supervised_1102b 的验收定位

它是历史诊断 fixture：可证明主窗口返回误报应消除、四个录制窗口不能合并、title-only 缺 root 与控件证据仍应如实报告。不能作为 raw v2 事件覆盖或 strong appeared 的正例，不能通过手填 complete/root/explicit 补成 ready。

真正的 live 正例需新录制：连续三轮 fixture 录制→编译→回放→业务断言→报告；再执行受控产品流程。每轮记录版本、schema、能力、步骤结果与证据路径。连续成功仅验证该环境/样例，发布结论同时列清能力不足和未覆盖情形。

## 12. 本版已定决策与完成定义

- 不加布尔 openedNewWindow；使用窗口归属序列与 effects 数组。
- 猜测归因仅诊断；现有正向最近动作关联也不升级为直接因果。
- 先修跨窗口返回误报，再按可执行绑定证据解除真实歧义；不以某样本 ready 为交付目标。
- raw 升 v2，golden 升 v3；不把行为契约变化藏在 v2 可选字段中。
- windowId 不放进 selector；序列表为权威关联，selector 与身份声明严格一致。
- 本次运行实例绑定是必要条件；“前台身份三元组相同”不够，必须防同名兄弟与实例替换。
- 窗口变化断言不是免费业务断言；不改变原断言和录制/生成/回放语义的真实性要求。

完成定义：S0–S5 各自有代码、失败回归、独立复验与证据；v3 完整执行且业务断言能发现人工注入缺陷；历史行为兼容；报告如实呈现失败和缺口。在此之前分别标记“设计完成”“离线实现通过”“Windows fixture 通过”“产品实机通过”，不提前宣称商业可用或 Windows 已全面验收。

## 13. 独立实施进度（2026-11-03）

- S0：本地独立验收通过；A→B→A误报修复，另一实例仍拒绝，1102b第9步弱控件缺口保留。
- S1：07:07检查轮本地独立验收通过。定向36 passed；录制1470 passed / 1 skipped / 1 deselected；全库3040 passed / 1 skipped / 614 deselected。真实worker与监督历史反例复验、5轮worker稳定性、diff-check及文件摘要验证已完成。
- 证据：`result-report/cross-window-supervision/20261103-0707/s1-independent-acceptance.md`；HEAD仍为`c4ac5442b71e83191881b098c70a1e8901489f48`加未提交WIP，不是新冻结基线。
- S2：11:57检查轮本地独立验收通过（修复版9）。真实v3四类model/schema双通过、非法source/未知字段双拒绝、五次编译稳定；定向76 passed；录制1510 passed / 1 skipped / 1 deselected；全库3080 passed / 1 skipped / 614 deselected。证据：`result-report/cross-window-supervision/20261103-1157/s2-independent-acceptance.md`。接续S3目标实例绑定；S3/S4未完成前完整v3仍禁止执行，§7.6持久attempt与带图报告仍是后续硬门槛。
- 边界：S1的`created/native_show`是首见观测，不证明物理创建；同PID同HWND或root/进程启动身份变化覆盖不足不能虚报。macOS destroy未知、丢包/缺实例/多候选/矛盾时S2须保留incomplete，不能靠W编号放行。
- S3–S5及Windows连续三轮、人工缺陷注入均未验收；尚未获本聊天对具体Windows目标/GUI/部署/重启的新增授权，不宣称Windows或商业可用全面验收。
