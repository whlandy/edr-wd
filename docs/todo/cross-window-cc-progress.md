# 跨窗口录制回放 —— 切片推进清单(09 号设计 R1)

权威设计: docs/architecture/09-cross-window-sequence-and-instance-binding-design.md(R1)
监督: Codex 每 10 分钟独立监督;不建 Claude 内部定时任务。
纪律: 每片先失败回归再实现;每片交付文件+完整测试输出+证据;停等独立复验。
兼容硬门槛: 原录制编译回放、断言/语义/视觉/root-only/title-only 全部正常。

## 切片状态

| 片 | 状态 | 交付文件 |
|---|---|---|
| S0 事实与误报修正 | **完成(独立验收,c4ac544)** | docs/todo/s0-cross-window-misreport-fix.md |
| S1 raw 生命周期 | **修复版交付,停等复验**(前版 3 P1 被拒,已修) | docs/todo/s1-raw-v2-window-timeline.md |
| S2 编译与 v3 | **交付,停等独立复验** | docs/todo/s2-golden-v3-window-sequence.md |
| S3 目标绑定 | **交付,停等独立复验** | docs/todo/s3-window-binding-protocol.md |
| S4 效果与报告 | 未开始 | — |
| S5 实机验收 | 未开始(实机门前停等授权) | — |

## S0 范围(设计 §2.1/§6.3,独立验收)

1. 修正 `compiler.py` collapsed-window 比较:同一 selector 再现时与**该 selector
   首次来源实例**比较,不与紧邻上一步比较。A→B→A 不误报;A→同 selector 另一
   实例(A2)仍拒绝。
2. supervised_1102b 定位:compile-report 的歧义标在 raw seq 1/10 = compiled
   step-0001/step-0008(同主窗口 handle 13239958)→ 修正后该误报消除;
   step-0009 弱控件身份(title-only EDRClient,快照无 root、无 complete)如实
   保持 incomplete,不造 root/complete 凑 ready。
3. "正向最近动作关联不是可靠直接因果"(设计 §2 事实表)—— S0 只修正比较
   对象,不改归因语义。

## 证据与复验

- S0 交付后停等独立复验(Codex),复验通过前不开始 S1。

## 每轮记录

### 2026-11-03(Claude 人工会话,接用户指令)
建清单;读 R1 设计;开始 S0。

### 2026-11-03 S0 完成
- 失败回归先行:`test_return_to_first_window_is_not_window_drift`(修复前红:
  A→B→A 误报 incomplete)、`test_recurring_selector_on_a_different_instance_still_refuses`
  (语义锁定:A→A2 仍拒绝)。
- 实现:compiler.py collapsed-window 比较锚点 = selector 首次出现事件
  (collapsed_window_steps 值加 first_event);拒绝语义零放松。
- phase0 83/83;录制目录 1434/0;全库 3004/1skip/614deselected/0 failed;
  Δ = +2 恰好两个新回归(基线 3002 由临时 stash 实测)。
- supervised_1102b 复编译:step-0001/0008 误报消除;step-0009 弱身份如实
  incomplete;整体保持 incomplete(不凑 ready)。
- 交付文件 docs/todo/s0-cross-window-misreport-fix.md;**停等 Codex 复验,
  通过前不开始 S1**。

### 2026-11-03 S1 完成(接 Codex 复验通过通知)
- 失败回归先行:test_raw_v2_window_timeline.py 实现前 10 failed,实现后 **15 passed**。
- 实现:models.py(raw v2 双读、_validate_window_timeline 严格校验、evidence
  白名单+2 引用字段)+ windows.py(correlator window_timeline append-only、
  实例索引 DESTROY 退役/HIDE 保留、WinEvent nativeKind 透传、transition
  evidence 实例引用、manifest 条目 instanceId)。
- JSON Schema 同步(初稿漏项复验前补齐):desktop-recording.schema.json 重构
  v1/v2 双定义 + 2 个 schema 测试。
- 实测:录制目录 **1449/0**(基线 1434+15)、全库 **3019/0**(基线 3004+15,Δ 精确,
  未用 stash/reset)、diff --check 通过、v1 原件编译行为与 S0 复验零变化。
- 交付文件 docs/todo/s1-raw-v2-window-timeline.md(§4 边界、§5 缺口如实);
  **停等 Codex 复验,通过前不开始 S2**。未 commit。

### 2026-11-03 S1 修复版(响应 0417 复验拒收)
- 三个 P1 实际路径反例全部固化为回归(真 session.recording/真 correlator 输出
  构造 raw v2/AST 提取生产 hook callback),修复前 3 红,修复后绿:
  - P1-1 session.recording() 接 v2+timeline+真实 coverage(修前 unknown_field 崩);
  - P1-2 DESTROY 复用产出两条 manifest 声明,旧实例历史保留,实际输出可序列化重读;
  - P1-3 hook HIDE 不再吞 DESTROY(DESTROY 唯一 pop,用 SHOW 时可信身份;无身份→计丢包)。
- 收口项:业务事件 evidence.window.instanceId(macOS parity 同步)、入口窗口
  initial_observed、inventory 快照 instanceId 全链、coverage 严格化+真实丢包、
  timeline ingressSequence 统一接收序。
- 实测:18/18;录制 1452/0;全库 **3022/0** 连续两次(基线 3004+18);diff-check OK。
- 诚实记录:首次全库 1 个 time_assertion 失败为 macOS 补丁落盘前中间态,修补后
  两次全绿且单测通过,判定非稳定 flake(如复验要求顺序隔离排查,配合)。
- **停等复验;未 commit;未开始 S2**。

### 2026-11-03 S1 修复版2(响应 0457 复验拒收)
- 认账:前版报告虚假声称 hook AST 回归已存在(heredoc 覆盖事故);time_assertion
  "根因已确定"表述撤回,仅记录现象。
- 4 个 0457 反例固化为回归(先红),逐项修复:
  - P1-1 seed_scope 统一建立实例(manifest/initial_observed/快照/业务事件同源;
    ingress 如实,seed 无输入包则缺失);
  - P1-2 driver DESTROY 检查移到 GetAncestor 之前、身份用 SHOW 时已存记录、
    driver 初始化 dropped_window_events、CompositeHookDriver.window_hook_loss()
    聚合 → session → note_hook_loss → 最终 coverage;能力按 backend
    (Windows=supported,macOS=unknown,继承不背书);
  - P1-3 inventory pending 同 handle 新实例替换旧请求;去重 key 加 instanceId
    (同内容两代快照各自保留);
  - P2 coverage 严格化(白名单/枚举/非负),与 schema 一致。
- 复验脚本四反例全部反转(INITIAL OK / DESTROY 三路径正确 / INVENTORY 隔离 /
  非法 coverage 拒绝)。
- 实测:22/22;录制 1456/0;全库 3026/0(基线 3004+22,Δ 精确);diff-check OK。
- 交付文件 docs/todo/s1-raw-v2-window-timeline.md(修复版2);**停等复验,
  未 commit,未开始 S2**。

### 2026-11-03 S1 修复版3(响应 0517 复验拒收)
- 三个同一生产链路边界(复验:非新增需求)先固化为真实链路回归再修:
  - P1-1 QueuedCaptureSource.window_hook_loss()(吸收式,重复收集不重复累计);
    真实 driver→Composite→source→session→coverage 链断言 droppedEvents=3 且
    二次 recording 仍=3;
  - P1-2 inventory 发布前实例权威校验:correlator 注入 instance_authority;
    换代→instance_superseded、authority 不可用→instance_authority_unavailable;
    反例含 destroy 后无新 request 的失效情形;
  - P1-3 _with_window 首见统一不变量:manifest 登记(origin=observed_during_flow)
    + timeline(observed/input_observation/unknown,不冒充清单、不造因果);
    另修 title-alias 缺口(seed handle 键 vs 输入 title 键造成双实例)。
- 复现脚本三段全反转(LOSS exposes=True dropped=3 / UNSEEDED OK / IN-FLIGHT
  attribution=[] + 结构化诊断)。
- 实测:25/25;录制 1459/0;全库 3029/0(基线 3004+25);diff-check OK。
- **停等复验;未 commit;未开始 S2**。

### 2026-11-03 S1 修复版4(响应 0547 复验拒收:标题别名 P1)
- 认账:0517 轮我引入的 title 别名是错误修复方向(三向破坏实例语义)。
- 按 0547 最小方案:删除别名;handle=权威键(未命中=新独立观测);无 handle
  输入从 manifest 取候选、权威索引判 liveness —— 唯一活候选归属,多候选诚实
  缺口不贴 RI,退役 RI 天然不参选;fixture 补 handle 契约。
- 3 个 0547 反例回归先红后绿(各含 session 序列化重读引用一致断言)。
- 复现脚本三段全反转;28/28;录制 1462/0;全库 3032/0(基线+28);diff-check OK。
- **停等复验;未 commit;未开始 S2**。

### 2026-11-03 S1 修复版5(响应 0607 复验拒收:两个生命周期入口 P1)
- 统一不变量实现:_declare_instance_observation(实例→manifest→timeline 原子);
  _instance_for_new_observation owner 感知(pid 正向变化→新 RI+identity_changed,
  旧历史保留;修掉新实例不记 pid 的自伤 bug);四入口(seed/show/hide/input)
  全部走同一函数。
- P1-1 真实队列丢 SHOW→HIDE 链回归(hidden/native_hide,manifest observed_during_flow,
  收集可重读);P1-2 pid 变化新实例回归(含同 owner HIDE→SHOW 不回归)。
- 附带口径:fixture 描述更正(实际无 fixture diff);seed 真实时钟;created=首见
  观察边界声明。
- 实测:30/30;录制 1464/0;全库 3034/0(基线+30);diff-check OK;repro 两反例
  全反转。
- **停等复验;未 commit;未开始 S2**。

### 2026-11-03 S1 修复版6(响应 0627 复验拒收:input owner 入口 + 真实接收序)
- P1:_with_window 已映射 handle 也验证 owner(pid 变化→identity_changed+新实例
  +manifest,统一原子声明);首见分支传真实 pid+ingress=event.sequence;鼠标按下
  权威窗口不被覆盖(注释确认)。fixture 改显式 advance() 控制换代点(测试证据
  与 correlate 多次 foreground 的真实契约一致)。三种换代链回归(show→input/
  seed→input/input→input)先红后绿,全部经真 session 序列化重读。
- P2(监督更正上轮建议):correlator 独立 _ingress_counter,每次 correlate 递增
  (未产出事件的包也计);timeline ingressSequence 全部 auto=接收计数;raw 连续
  序号契约不变。修复过程中发现并修正 _declare_instance_observation 的
  ingress 默认值未透传问题(显式 None 覆盖 auto)。
- 复现脚本四项全反转(evidence RI2/双 manifest/identity_changed/owner index)。
- 实测:34/34;录制 1468/0;全库 3038/0(基线+34);diff-check OK。
- **停等复验;未 commit;未开始 S2**。

### 2026-11-03 S1 修复版7(响应 0647 复验拒收:四处显式 ingress 覆盖)
- 认账:上轮"全部 auto"报告与代码不符 — 1733/1779/1933/1982 四处显式传
  sequence/event.sequence 覆盖 auto;0627 repro 中 identity_changed ingress=1
  确为同一问题(当时"不深究"是错的)。
- 修复:四处→auto;补 _declare_instance_observation → _timeline_append 的
  auto 透传链;grep 确认零残留。
- 2 个真实 source worker 回归(不复制的 queue 循环):show/hide/destroy 0 输出
  递增序;无 raw 产物 input 首见+owner 换代的同包/跨包 ingress 断言(避开
  "全局唯一"错误断言,按真实包序断言)。
- 复现脚本 [1,2,1]→[1,2,3];36/36;录制 1470/0;全库 3040/0(基线+28→+36 记账:
  基线 3004,Δ=+36 精确);diff-check OK。
- **停等复验;未 commit;未开始 S2**。

### 2026-11-03 S1 独立验收通过(0707)
- 口径纠正:此前进度记录把"开始 S1 的授权"写成了"S1 已通过"——实际 S1 经过
  7 版修复、4 轮独立复验拒收(0417/0457/0517 部分认可、0547/0607/0647),
  直至 0707 复验确认全部反例修复、计数与摘要稳定后才通过。仅 S1 **离线切片**
  通过;v3 回放/可回溯 attempt/Windows 实机/商业可用均未验收。
- 验收证据:result-report/cross-window-supervision/20261103-0707/;独立定向
  36/录制 1470/全库 3040;0647 worker 回归重复 5 轮通过;四旧 ingress 零残留。
- **S2 开始(用户指令)**:仅编译与 v3 契约。先读 09 §5/6/9/7.6,失败回归先行。

### 2026-11-03 S2 交付(仅编译与 v3 契约)
- §6.1 显式源事件映射:编译循环内 step_source_sequences(stepId→raw 序号),
  diagnostics.sourceEvents.byStep 落盘;双击 coalesce 以事件层事实如实记录。
- §5/§5.1 严格 golden v3:WindowSequence 模型(引用/唯一/全覆盖/effects 限
  explicit_assertion/entryWindow 一致性/缺 windowSequence 拒绝);compiler
  _build_window_sequence(handle→instanceId→身份,不跨实例借 root/complete,
  opened→observed_during_flow);证据不足 incomplete 三方一致。
- §9 拒绝门:runtime 入口 v3→window_sequence_execution_unsupported(先于任何
  目标调用);pytest 导出对 v3 ValueError;schema 同步 v1/v2/v3 oneOf(修复首轮
  内层 $defs 丢失)。
- advisory:6 个新码登记+专属分支(parity 13/13)。
- 实测:S2 9/9(前 7 红)、S1 36 保持、advisory 13/13、录制 1479/0、全库
  3049/0(基线 3004+45)、diff-check OK;1102b 诚实回退 v2/incomplete。
- **交付 S2 停等复验;不自行进入 S3**。未 commit。

### 2026-11-03 S2 修复版2(响应 0747 复验拒收:4 P1 + 1 P2)
- P1-1 builder 重写 RI-primary(删末态 handle→RI dict;事件 instanceId+timeline
  区间归属;快照 root 按 RI 区间采信;coverage unknown/丢包→incomplete)。
- P1-2 coalescedSources(raw→coalesced)+ continue 分支(绑定 assertion/
  transition)映射到动作步骤;双击 raw [1,2] 全可追查,撤销初版错误放松。
- P1-3 verifier source 标签(user_assertion/native_transition;models 白名单+
  枚举);builder 两遍(先声明后 effects);temporal 无 effect;不可映射显式
  条件保留诊断。
- P1-4 公开入口拒绝(replay_golden_trace + golden_to_test_case 经
  _refuse_v3_execution,先于任何目标副作用,fake dispatch 反例 calls=0);
  artifacts 核心先盘(compile-report 含 sourceEvents),v3 pytest 导出=全注释
  占位+pytestExport unsupported,无 legacy 可执行文件。
- P2 模型:selector↔identity 一致、timeout (0,300]、titleRegex 类型+可编译。
- 复现脚本八探针全反转。实测:15/15(前 6 红)、S1 36 保持、S1+S2 51、
  录制 1485/0、全库 3055/0(基线+51)、diff-check OK。
- **停等复验;未 commit;未开始 S3**。

### 2026-11-03 S2 修复版3(响应 0907 复验拒收:生命周期/断言入口/来源/模型)
- 生命周期门槛:缺 timeline/coverage→incomplete;destroyed_at 表拒绝销毁后
  归属;snapshot 区间(含 end)+自带 RI+多 root 冲突→identity_conflict。
- 真实断言入口:bound-assertion 分支对 window_open 打 user_assertion
  (transition 冒充不覆盖真实入口的反例已补)。
- 来源:opposing 抵消分支映射 raw3;coalescedSources 改为 coalesce_events
  真实合并事实(provenance_out 在合并点记录,不再按位置猜)。
- 模型四项:分区(ownership 错段拒绝)/entryWindow 严格匹配(^WRONG$拒绝)/
  identity 缺字段不绕过/全局 effect id。
- 复现脚本七探针对照全反转(repro 自身 fixture 无 timeline 的 None=诚实拒绝,
  等价带 timeline 回归全绿)。实测:19/19(前 4 红)、S1+S2 55、录制 1489/0、
  全库 3059/0(基线+55)、diff-check OK。
- **停等复验;未 commit;未开始 S3**。

### 2026-11-03 S2 修复版4(响应 0947 复验:三处生命周期/快照/效果目标缺口)
- 区间语义统一:instance_intervals start=首见下界,end=仅 destroyed;
  _window_for 统一区间校验(替代逐反例 death dict)。
- 快照:root 提升改字段合并(root 填缺,title 保留 — 修"root 替换 title"
  触发严格模型的形态);模型拒绝在 builder 捕获→结构化 incomplete(不崩)。
- effects 目标:expected process/root/title 全匹配;0/多候选→incomplete+
  compile_window_effect_unverified(不借同标题、不首条猜、不静默丢)。
- 复现五探针全反转(live 快照 root 提升生效;崩溃→结构化;Other.exe 断言
  incomplete+诊断)。4 回归先红后绿。
- 实测:23/23;S1+S2 55;录制 1493/0;全库 3063/0(基线 3004+59);diff-check OK。
- **停等复验;未 commit;未开始 S3**。

### 2026-11-03 S2 修复版5(响应 1007 复验:两组原契约漏项)
- P1a no-facts 门:所用 RI 不在 instance_intervals → 拒绝(删无限区间默认)。
- P1b lost/destroyed 结束可证明区间(identity_changed 不截断 — 双代回归保持);
  lost 不伪造死亡、不自动分新 RI;HIDE/SHOW 正常对照保持 ready。
- P2 effects 目标:expected.titleRegex 编译后对 _recordedTitle search,
  不匹配剔除候选;0/多候选→incomplete+诊断。
- 复现三探针全反转。实测:27/27;S1+S2 59;录制 1497/0;全库 3067/0(基线
  3004+63);diff-check OK。
- **停等复验;未 commit;未开始 S3**。

### 2026-11-03 S2 修复版6(响应 1027 复验:一 RI 一 W + effects 两反例)
- P1:W 键改 RI 本身(一 RI 至多一 W,§3/§4.2 标题变化不分实例);同 RI 矛盾
  identity→compile_window_identity_conflict→incomplete;重构残片清理。
- P2-1:非法 titleRegex 编译失败→compile_window_effect_unverified(不再吞成
  无条件)。
- P2-2:同 step 同源窗口断言按 5 元组去重合并为一 effect(raw 来源经
  sourceEvents 可追查;互斥 kind 保留);公开 compile 不再抛。
- 2 对照回归(不同 RI 同身份=两 W;同 RI root 冲突=incomplete)。
- 复现四探针全反转。实测:32/32;S1+S2 63(与监督一致);录制 1502/0;全库
  3072/0(基线 3004+68);diff-check OK。
- **停等复验;未 commit;未开始 S3**。

### 2026-11-03 S2 修复版7(响应 1107 复验:effect ID/公开编译异常)
- 根因:dedup 五元组 ≠ ID 键(EF-step-W)——不同条件绕过去重却撞 ID。
- 修复:effect ID = EF-{step}-{sha256(规范化条件)[:12]}(与 dedup 同键源);
  等价条件合并 raw 来源保留;互斥(appeared+absent 同目标)两 effect 唯一 ID
  完整保留 + compile_window_effect_unverified 诊断 + status/case 同步
  incomplete;builder 解耦 case/status 引用(此前 UnboundLocal)。
- 复现两反例反转(唯一内容哈希 ID;互斥保留+incomplete)。
- 实测:34/34;录制 1504/0;全库 3074/0(基线 3004+70);diff-check OK。
- **停等复验;未 commit;未开始 S3**。

### 2026-11-03 S2 修复版8(响应 1127 复验:首失效边界被覆写)
- 根因:intervals 遍历每个 destroyed/lost 直接覆写 end — 最后一条失效事实
  延后首个不可证明边界(重复 lost 重新放行 unknown/已死亡区间)。
- 修复:保留首个边界(prev_end is None or ms < prev_end 才更新);lost=
  unknown 与 destroyed=death 区分保持;无自动恢复/重绑/分新 RI。
- 2 回归先红后绿 + 正常 HIDE/SHOW/新 RI 对照;复现两反例反转
  (均 incomplete + compile_window_source_unresolved)。
- 实测:36/36;S1+S2 63;录制 1506/0;全库 3076/0(基线 3004+72);diff-check OK。
- **停等复验;未 commit;未开始 S3**。

### 2026-11-03 S2 修复版9(响应 1140 复验:model/schema 同步缺口)
- schema 共享 defs 同步到实际模型契约:window+rootAutomationId、verifier+source
  (enum user_assertion|native_transition);additionalProperties=false 保持。
- 4 回归先红后绿:root-only/title-only 正例 model+schema 双 PASS;
  user_assertion/native_transition 双 PASS;未知字段与非法 source 双拒绝;
  v1/v2 兼容(1102b 原件+构造 v2 过 schema)。
- 复现脚本四正例全 PASS。实测:40/40;S1+S2 76(监督一致);录制 1510/0;
  全库 3080/0(基线 3004+76);diff-check OK。业务实现零改动。
- **停等复验;通过即接 S3;未 commit**。

### 2026-11-03 S3 交付(目标实例绑定协议最小切片)
- S2 独立验收通过(1157,仅本地编译契约)。
- 新增 agent/recording/window_binding.py:能力协商(缺能力副作用前拒绝)/
  product-run 租约授权/首次实时唯一绑定(不透明 token+revision)/LOST 不
  顶替不重猜/HIDE→SHOW 同 owner 恢复/destroy+同 HWND 复用 lost/owner-root
  变化失效/focus→dispatch 替换防护(dispatch 0 次)/连续同 W 重验/共绑定
  拒绝/lease 竞争拒绝/release 不碰他人/§7.6 基础(稳定引用+只读 MappingProxy
  视图+跨 run 拒绝)。
- 17 回归先红后绿(fake-target 驱动真实协议代码);S1+S2 显式 93 保持。
- 实测:S3 17/17;录制 1527/0;全库 3097/0(基线 3004+93);diff-check OK。
- 未覆盖:§7.3 差分/§7.5 cleanup 执行/带图报告(S4);生产 target 桥接(需
  实机授权轮);generation 覆盖边界(S1 编译门拦截)。
- **交付 S3 停等复验;不自行进入 S4**。未 commit。

### 2026-11-03 S3 修复版2(响应 1227 复验拒收:十反例/四组)
- 核心批评接受:初版是自建锁+fake 字典授权的示意协议,未接产品边界。
- 重写:授权/串行边界 = 现有 product_policy.authorized_dispatch +
  require_product_run(真实租约,REAL digest);capability 结构化
  (supported/coverage,truthiness 不算);_bound_pid 记录+dispatch 前
  同边界重验;declared_windows 权威(未知 W/冲突/调用者字段均验证,
  ^NEVER$→not_found);LOST 一律拒绝(reverify 中 lost=True 不可恢复),
  恢复仅经 recover_hidden(同 native/同 owner/身份全验的 SHOW 事实);
  LOST 的 step_context transition=unverified_state_lost(跨 run 拒绝);
  run 关联来自实际绑定。
- 测试重写为真实边界接线(16 测试;fixture 用真实 begin_product_run;
  dispatch 记录器在 OS 层替身)。
- 实测:16/16;S1+S2 92;录制 1526/0;全库 3096/0(基线 3004+92);
  diff-check OK;旧 repro 脚本已过时(引用初版 fixture 名,KeyError)。
- **停等复验;未 commit;未开始 S4**。

### 2026-11-03 S3 修复版3(响应 1227 复验拒收:target 侧 facade)
- 核心批评接受:初版是 agent 侧自建协议,target 无接线。重写为 **target
  侧 WindowBindingFacade**:复用 WindowSession.refresh(产品域归属)/
  backend.focus_existing_window/policy.authorized_dispatch +
  require_product_run;generation/visible/foreground 为目标安全必验事实;
  register_window_binding_tools 挂 MCP 工具;LOST 状态守卫(reverify/
  recovery 均拒);recover_hidden 需 HIDDEN 前提;_recordedTitle 漂移检测。
- 修复 facade bug:binding.generation 被 focus_and_execute 覆盖。
- 17 facade 回归先红后绿(真实 target 边界 + fake OS 层);S1+S2+S3 显式
  109;录制 1543/0;全库 3112+1flake(单独复跑通过,与本改动无关);
  diff-check OK。
- 未覆盖如实:server 实际注册/§7.3/7.5/带图报告/持久化(S4)/Windows 桥接。
- **交付 S3 停等复验;不自行进入 S4**。未 commit。

### 2026-11-03 10号设计文档归档(跨窗口编译期归属消解)
- 用户指出跨窗口是正常软件用法,不能处理是功能缺陷。设计文档:
  docs/architecture/10-cross-window-compiler-disambiguation-r0.md(归档 doc/)。
- 核心思想:编译器应使用录制时已捕获的 handle→manifest 归属关系消解同名
  窗口歧义,而非在 selector 碰撞时放弃归属。
- 同 handle = 同窗口返回 = 复用同 W(不报歧义);
  不同 handle + 同 title = 不同录制实例 = 独立 W。
- 2 个跨窗口回归先红(cross_window_s3 场景:A→B→A 编译仍 ambiguous);
  编译器归属消解逻辑待实现(需让 _build_window_sequence 使用
  step_source_sequences → event handle → manifest RI 做归属)。
- **待独立复验后接续 S3 修复实现**。未 commit。

### 2026-11-03 10号设计:评审修订后测试修正 + 归属消解验证
- 评审四个点全部成立:测试 fixture 证据矛盾(B 窗口 manifest 写 Log Center
  但 click 的 title 硬编码 logo1)/ 步骤编号用 raw 序号而非编译步骤 /
  drag 测试无合法 drag 构造 / instanceId 归属已实现。
- 测试修正:一致 fixture(两个窗口的 manifest/snapshot/click 三者一致:
  handle→instanceId→root)→ **A→B→A 编译 ready 且 windowSequence 正确归属**
  (A 复用同 W,B 独立 W);drag 测试替换为 sourceEvents 覆盖测试
  (全部动作事件可追查);保留身份冲突拒绝的负例。
- 42/42 全绿(42 = 40 + 2 新跨窗口);录制 1549/0;全库 3119/0;diff-check OK。
- **待独立复验确认后接续 S3 完成**。未 commit。

### 2026-11-03 10号设计:复验修订(正例锁定+独立负例)
- 复验指令:先修 fixture,正例锁定 ready+非空 windowSequence+完整步骤
  映射+A 复用 W+B 独立 W;冲突拒绝另设负例,不放正例允许分支。
- fixture 三处证据不一致全部修正:click1 causalId C1→CAUSE-1(与 opened
  transition 一致);_click 新增 snapshot_id/title/window_title 参数
  (B 步骤不再继承主窗口的 S1/logo1/^logo1$);_snap 新增 title 参数
  (CS-0002 的 title 不再错写 logo1)。
- 正例 test_cross_window_a_b_a_window_sequence 强断言:ready、issues 空、
  windowSequence 非空、每编译步骤都有窗口归属、step-0001 与末步同 W、
  step-0002 独立 W、B 的 W 身份 root=LogCenterWindow、A 的 W 身份
  root=EdrMainWindow(身份来自各自窗口快照,不是借名)。
- 负例独立:test_cross_window_root_conflict_rejected_as_negative —
  B 窗口同时段冲突 root 快照 → incomplete + windowSequence=None +
  compile_window_identity_conflict 结构化诊断(探针确认)。
- 探针验证:一致 fixture 编译器归属已正确(10 号 §2 生效);43/43
  (42+1 负例);录制 1550/0;全库 3706/0。
- **交付待独立复验;未 commit**。

### 2026-11-03 10号复验通过(正负例)→ 接续 S3 server 实际注册
- 复验结论:核心正负例通过(43/43;三步映射 W1→W2→W1;冲突负例
  incomplete+空 windowSequence+compile_window_identity_conflict);指出来源
  覆盖断言漏 transition(删 raw2/raw4 仍过)+ drag 未验证。
- 修正:test_cross_window_source_map_covers_all_raw 改精确逐步断言
  step-0001:[1,2]/step-0002:[3,4]/step-0003:[5],全体 raw 序号集合相等;
  突变验证(删 [1,2]/[3,4])被捕获。drag 正负例未做,不宣称 10 号完成。
- S3 "server 实际注册"缺口:register_window_binding_tools 是死代码且自身
  有 bug(_serialized_product_gui/json 未定义;focus 挂空动作 lambda)。
- 接线:target/server 模块加载时注册三个 MCP 工具
  (bind_entry_window/focus_bound_window/window_binding_state),经
  _binding_facade_for_run 按租约解析 facade;begin_product_run 创建
  facade(declared_windows 来自已装 profile 的 window_roles,capability
  经 backend.window_sequence_capability 探测);focus_bound_window 在同一
  租约边界调真实 execute_action,拒绝时 executed=false、执行后异常如实
  executed=true;拒绝返回 dispatch receipt 作为结果。
- 顺带修复 facade 实缺陷:focus_and_execute 未检查 binding.state,LOST
  绑定可被聚焦后静默重验通过(§7.4 违规)→ 现在 LOST/HIDDEN 一律拒绝,
  恢复仅经 recover_hidden。删除死代码 register_window_binding_tools。
- 新增 test_window_binding_server_wiring.py 5 回归(注册面/无租约拒绝/
  拒绝 0 dispatch/真实 dispatch 路径/冒名 token);22/22
  (含原 17 facade 回归)。录制 1555/0;全库 3711/0。
- 未覆盖如实:§7.3 差分/§7.5 cleanup 执行/带图报告(S4)/Windows 实机桥接;
  drag 正负例(10 号遗留)。**停等复验;未 commit**。

### 2026-11-03 S3 修复版2(响应复验五缺口;真实初始化入口覆盖)
- 复验五缺口全部成立并按其口径修:测试重写为**真实初始化入口**
  (monkeypatch server._backend 为 fake OS backend → 真实
  begin_product_run → 真实 WindowSession/registry → facade),不再手工
  注入 holder;执行正例走真实 execute_action(仅 fake 化 OS 层)。
- gap1 依赖环:begin_product_run 构造 facade 时 _binding_declared_windows
  读未创建的 facade → 改为直接接收 run context;修中暴露
  INSTALLED_PRODUCT_SPECS 未导入 NameError(此前被 except 吞掉,facade
  静默为 None)→ 延迟导入。真实入口现在声明 windows_hisec 全部 roles
  (entry/main/log_center/dialog),bind main 成功。
- gap2 严格协商:backend supported=false 此前被丢弃仅凭 coverage/generation
  重推 → facade 新增 supported 参数,backend 结构化裁决权威透传
  (false 不再被重推为 true);unsupported 时 bind 副作用前拒绝。
- gap3 安全事实:visible/foreground=false 实例此前仍进执行回调 →
  focus_and_execute 在 after 重验中强制拒绝;顺带修真实缺陷:
  WindowSession.refresh 归一化 dict 不透传 foreground → registry 恒为
  False,facade 会拒绝一切(含合法前台)→ refresh 现透传 foreground
  (与 visible 同待遇;缺证据=非前台的安全语义保持)。
- gap4 执行语义三分:执行回调 append 在成功返回后 → 进入分发即记录
  entered_dispatch;异常后按 三态 返回 executed=true(有确定回执)/
  "unknown"(进入分发结果未知)/false(未调用)。
- gap5 租约失效:_binding_facade_for_run 此前只查 holder → 现经真实
  require_product_run 验证当前租约;end_product_run 后旧 token 的
  state/bind/focus 全拒。
- 7/7 wiring(5 新缺口回归先红后绿);139 邻接面
  (facade/protocol/window_session/dispatcher);录制内含;全库 3713/0。
- 未覆盖如实:§7.3/§7.5/带图报告(S4)/Windows 实机;drag 正负例(10 号
  遗留)。**停等复验;未 commit**。

### 2026-11-03 S3 修复版3(响应 1744 复验六缺口;单轮最小修复)
- 先完整读 server-independent-review.md + reproduce_server_followup.py +
  reproduce-output.txt;六缺口全部成立,认可五项不回退。
- 回归先行:测试重写为六缺口精确失败回归(先红),覆盖真实初始化入口,
  fake OS 层补齐真实 backend 协议(_enumerate/connect_window_identity/
  lock_window/get_window_lock/verify_window_lock/click/click_at/dump_tree/
  process_name_for_pid/focus_existing_window),正向链走真实
  execute_action dispatcher(修 dispatcher resolver 被其他测试覆盖的
  泄漏:_real_begin 显式 set_backend_resolver(→server._backend))。
- 缺口1 W↔role:新增 register_window_declaration MCP 工具 +
  facade.register_golden_windows — golden W 在已装产品授权域
  (profile roles process 集合)内验证;身份冲突/越域/重注册变身份拒绝;
  新增 reverify_bound_window 工具;W1/W2 A→B→A 接口链回归
  (bind→reverify 往返,冲突/未声明/越域负例)。
- 缺口2 三门:capability = 显式 supported 裁决 AND coverage==strong AND
  generation>0(1744:任一字段自报 true 不放行);三组反例参数化回归。
- 缺口3 归属:facade._product_owned — 候选/聚焦后进程经
  backend.exe_path_for_pid(缺省 psutil)解析 exe,与已装 path/entry_path
  比对;不可解析/无路径/非产品路径均拒绝(fail closed);首绑与
  focus→dispatch 前均核查;UNTRUSTED 同名 exe 回归(首绑拒绝+路径查询
  ≥1;漂移后 0 dispatch)。
- 缺口4 正向链:focus_and_execute 在 focus 后走权威精确锁链
  connect_window_identity→lock_window(strict,activate)→
  session.bind_backend_lock,锁 identity 与绑定实例比对;真实 dispatcher
  正例:app.activate 恰 1 次 + pointer.click_screen 恰 1 次,registry
  locked 非空;替换防护回归(pid 漂移→0 执行,旧安全门保留)。
- 缺口5 泛化异常:进入分发后 RuntimeError 结构化
  window_binding_dispatch_unknown + executed=unknown + 保留原因;
  不误称零执行。
- 缺口6 内存清理:facade.clear_run_state(RETIRED+清 bindings/token/golden)
  挂入 server 既有 _clear_product_run_state 清理 hook(end/过期/异常同路);
  回归保留旧 facade 引用核对映射清空,不手工 holder=None。
- 实测:14/14 wiring;126/126 显式五文件;录制门槛
  1564 passed/1 skip/1 deselected/97.13s;默认全库
  3134 passed/1 skip/614 deselected/121.77s(两次全库中间一次单失败
  为 resolver 泄漏,修复后复跑全绿)。
- 未覆盖如实:§7.3/§7.5/带图报告(S4);完整 §7.6;Windows 实机;
  drag 正负例(10 号遗留)。**停等独立复验;未 commit**。

### 2026-11-03 S3 修复版4(响应 1845 复验四组缺口;单轮最小修复)
- 先完整读 independent-review.md + reproduce_remaining.py +
  reproduce-output.jsonl;四组缺口全部成立,认可六项不回退。
- 回归先行:六条新回归先红(含 1845 指定的 focus/lock_activate/输入
  分别计数,_FakeOSBackend 增加 focus_calls/lock_activate_calls)。
- 缺口1 重验:reverify 完整核查产品归属(_product_owned,路径查询≥1)+
  失败锁存 LOST(_reverify_fields 包装 try/except);成功前不覆盖
  last-known root/title;属性恢复后重验仍拒(reverify 起点即拒 LOST),
  权威锁同步释放。
- 缺口2 前置:_product_owned 移到 focus_existing_window 之前
  (refresh 后、首次聚焦/激活前);聚焦后与 dispatch 前检查保留;
  UNTRUSTED 漂移回归断言 focus=0、lock_activate=0、输入=0。
- 缺口3 语义链:focus_bound_window 新增 target_ref/observe 透传;
  两步契约 — action_id=None+observe=True 建 权威锁+新观测(返回
  dispatcher 校验所用同一投影 controls),再携 ref 派发;
  observe=True+派发时 ref 必须 snapshot_id 匹配新观测;
  gui.click 正例恰一次;stale ref/其他窗口 target_id 均 0 输入;
  dispatcher 安全门全保留。
- 缺口4 清理:_clear_product_run_state 分项 try/except + finally,
  unlock 抛错不跳过 facade/会话/观测清理且错误保留;
  end_product_run 预收 unlock 结果并附 unlock_error 字段;
  正常 end/unlock 异常/租约过期三条回归均核对旧 facade 引用的
  bindings/token 清空。
- 实测:20/20 wiring;132/132 显式五文件(-o addopts='');录制门槛
  1570 passed/1 skip/1 deselected/97.40s;默认全库
  3140 passed/1 skip/614 deselected/122.07s。
- 未覆盖如实:S4/S5、完整 §7.6、drag 正负例、Windows 三轮实机。
  **停等独立复验;未 commit**。

### 2026-11-03 S3 修复版5(响应 1908 复验三组缺口;真实 GuardedBackend 口径)
- 先完整读 independent-review.md + reproduce_followup.py +
  reproduce-output.jsonl;三组缺口成立,认可七项不回退。
- 回归先行(全部按 1908 真实 GuardedBackend 口径,_guarded_run:
  真实 begin→GuardedBackend 包裹 fake OS→WindowSession/facade):
  5 条先红,含 1908 指定的"先真实建锁→失效→旧 dispatcher 0 输入"
  与"错误 token 对照(unlock 0/原锁保留/owner 活动)"。
- 缺口1 失效撤销:WindowRegistry.revoke_lock(native_id) 仅撤销本绑定
  窗口的权威锁(他人绑定/run 不动);facade._revoke_execution 撤销
  registry 锁 + backend OS 锁(best-effort,不掩盖拒绝原因),接入全部
  16 个失效路径(_reverify_fields 归属/身份失败、focus 归属失败等);
  focus 归属失败也锁存 LOST。回归:建锁→漂移→reverify LOST 后
  registry.locked 为 None、标题恢复后旧 execute_action 0 输入、
  LOST 不复活;focus unowned→LOST→路径恢复同 token 0 输入。
- 缺口2 三态边界:entered_dispatch 移到 execute_action 调用边界
  (观测刷新/ref 比对之前不置位);observe 阶段失败 executed=false,
  真正进入 dispatcher 后异常仍 unknown。回归断言 dispatcher 调用 0
  + 输入 0 + executed=false。
- 缺口3 清理错误呈现:product_policy.end_product_run 返回各 hook
  结果(hook 异常也结构化收集,不跳过);server end 工具经既有
  token 验证/清理路径透传 cleanup_errors,删除重复预解锁猜测;
  finally 内存清理保留。真实 GuardedBackend unlock 异常回归:
  底层 unlock 恰 1 次、end ok=true 且 cleanup_errors 含原因、
  facade/session 清空、错误 token 0 unlock。
- 顺带修真实潜在缺陷:server 模块 reload(test_server_backend_resolver
  的 importlib.reload)会重复 register_cleanup_hook → N 次 reload 堆叠
  N 遍清理/unlock;新增 product_policy.replace_cleanup_hook 幂等注册
  (按 module+qualname 去重),server 改用之。这正是全库跑时
  cleanup 测试 unlock 计数为 2 的根因。
- 实测:25/25 wiring;137/137 显式五文件;录制门槛
  1575 passed/1 skip/1 deselected/97.01s;默认全库
  3145 passed/1 skip/614 deselected/121.48s。
- 未覆盖如实:S4/S5、完整 §7.6、drag 正负例、Windows 三轮实机。
  **停等独立复验;未 commit**。

### 2026-11-03 S3 修复版6(响应 1938 复验;撤锁隔离 P2 单修复组)
- 先完整读 independent-review.md + reproduce_cross_binding.py +
  cross-binding-output.jsonl;两个同函数反例成立,1908 七项认可不回退。
- 回归先行(真实 GuardedBackend 口径):3 条先红 — 双绑定
  (A main 前台 false + B W2)B 建锁前置正例、A 归属失效后 B 的
  registry/backend/合法执行能力保持、A 自建锁失效后 backend 锁在
  授权边界内真实撤销(unlock 恰 1);错误 token 对照扩展。
- 修复:_revoke_execution 重写 —
  1) 精确归属:仅当 backend 当前锁 handle/pid 与本绑定实例一致才解锁
     (新 _backend_lock_owned_by);他人锁/无锁/不可观测一律不动;
  2) 租约边界:unlock 在 self._policy.authorized_dispatch(self._lease_token)
     内执行,GuardedBackend 不再以 product_run_conflict 拒绝(1908 的
     "reverify 边界外 unlock 被拒而未真正撤销"根因);
  3) 结构化返回检查:unlock 返回 dict 且 ok=false(如冲突拒绝)不视为
     撤销完成;
  4) 不以 registry.revoke_lock 布尔值决定(registry 可能已因 refresh
     先清而 backend 仍留本绑定锁)。
- LOST 锁存/原拒绝原因/GuardedBackend 权限全部保留,未放宽任何权限。
- 实测:28/28 wiring;140/140 显式五文件;reload+wiring 联合
  31 passed/2.28s;录制门槛 1578 passed/1 skip/1 deselected/97.71s;
  默认全库 3148 passed/1 skip/614 deselected/123.16s。
- 未覆盖如实:S4/S5、完整 §7.6、drag 正负例、Windows 三轮实机。
  **停等独立复验;未 commit**。

### 2026-11-03 主轮:1956 撤锁验收通过 → 全需求分片推进(片0-片6)
- 先完整读 revocation-acceptance.md + expiry_control.py + 09 §5-§11 +
  10号 R1;1956 结论:撤锁切片通过(≠S3 整体);其指出 expired 测试
  实际只有 invalid-token、旧 S3 交付文档过时。
- **片0 真实过期对照**:policy 时钟前移使 1s lease 实际到期(无 sleep)
  → 旧 run RPC 拒绝、旧 facade 清空、新 owner 同 backend 建锁;旧
  reverify 与旧 facade._revoke_execution 均 0 unlock,新 owner 锁与
  合法执行保持(参照 1956/expiry_control.py 落为持久回归)。
- **片1 S3 矩阵诚实性**:test_s3_acceptance_matrix.py(3 passed)—
  生产 backend(Windows/macOS)未实现 window_sequence_capability →
  真实初始化 capability supported=false → 绑定副作用前拒绝(fail
  closed,不以 fake 自报 strong 伪造 ready);event_lossy/negative
  generation 拒绝;recover_hidden 的 show_fact 是观测引用,调用方
  字典不得指定他人 HWND/owner。矩阵覆盖核对:同 title/root 多实例、
  HIDE/SHOW/DESTROY、兄弟不顶替已在 facade/protocol 套件固化。
- **片2 S4 appeared/absent 差分**:test_s4_window_effects.py(7)先红
  后绿 — facade.begin_effect_baseline(租约内枚举候选+不透明 token)/
  observe_effect_after_action(只读单采样):唯一新候选 appeared、
  already-visible 旧窗口不满足(timeout)、增量多候选 ambiguous、
  HIDDEN 同实例 SHOW=same_instance_shown(不延展他人 W)、absent、
  观察零 focus(只读)、无租约拒绝。
- **片3 §7.6 完整账本**:target/windowing/attempt_ledger.py +
  test_s6_attempt_ledger.py(8)— attempt 全字段(run/step/attempt/
  previous*/window/transition+basis/stateBefore/AfterRef/actionReceipt/
  assertion/evidence/outcome);首步 previous 空;重试独立 attempt;
  持久状态禁 handle/pid/token(redact 强制);append-only+稳定事件 ID+
  重启重建;写盘失败:动作前禁副作用、动作后 unknown 边界(不声称未
  执行);unknown/fail/skipped 阻断推进;跨 run 拒绝。
- **片4 失败步报告**:target/windowing/report_view.py +
  test_s6_report_view.py(5)— build_failure_trace(上一步窗口/attempt/
  断言/切窗依据/前后状态);缺图 evidence_missing(不拿旧图冒充);
  fixture 截图标 source+is_windows_capture=false(不虚称 Windows);
  报告 blob 无 token/handle/pid;已有帧早于失败 → context-only。
- **S4 剩余门**:target/windowing/effect_gate.py +
  test_s4_effect_gate.py(7)— 窗口效果/业务断言/receipt 分列;效果
  通过+业务 fail=fail;receipt unknown=unknown+阻断;cleanup 显式步骤
  (可验证 pass/不可验证 skipped+记录,永不找兄弟);已有帧早于失败
  只作 context。
- **片5 10号 drag 正负例**:test_golden_v3_window_sequence.py +
  2(45 总)— 一致 drag fixture(真实 _DRAG 形状 screenPoint/endPoint/
  endTarget);正例:带因果绑定结果 verifier 的 drag 跨窗口流程 ready,
  drag 步留主窗口 W、B 独立 W;负例:无 verifier →
  compile_drag_verifier_required 且不整体 ready(窗口归属正确不替代
  结果 verifier,§6.5)。
- **片6 S5 本地准备**:docs/todo/s5-windows-acceptance-checklist.md
  (只读环境检查清单/故障注入矩阵 F1-F10/证据布局/判定标准/待用户
  决定三项)+ scripts/s5_acceptance_round.py(单轮=录制→停止→编译→
  回放→业务断言→带图报告;dry-run 三回合验证通过;--live 无授权
  即 refused,不伪造实机结果)。旧 S3 交付文档已同步最新接线/计数/
  验收状态(纠正 1956 指出的过时描述)。
- **中途门槛(完整结果)**:
  `python3 -m pytest -q test_case/test_recording -p no:cacheprovider`
  → 1604 passed, 1 skipped, 1 deselected(99.16s);
  `python3 -m pytest -q -p no:cacheprovider`
  → 3174 passed, 1 skipped, 614 deselected(123.05s)。
  本轮新增 26 个本地回归(片0 1 + 片1 3 + 片2 7 + 片3 8 + 片4 5 +
  S4 门 7 + drag 2),全部先红后绿或固化现状;无既有回退。
- **兼容门槛核对**:v1/v2 双读、单窗口、root/title-only、断言、报告
  脱敏、生成器未动(既有套件全绿);v3 公共执行/导出拒绝门保持关闭
  (_refuse_v3_execution 未改)。
- **剩余差额(如实)**:S3 整体/S4/S5/§7.6 均待独立复验;Windows 实机
  三轮需授权(s5 清单 §7 待决定项);生产 backend 强实例能力声明是
  实机轮前置(fail closed 现状如实)。

### 2026-11-03 主轮最终门槛(交付包;完整输出)
- `python3 -m pytest -q test_case/test_recording -p no:cacheprovider`
  → **1611 passed, 1 skipped, 1 deselected**(97.83s;1 skip=
  平台条件性,1 deselected=默认筛选排除项,均不计通过);
- `python3 -m pytest -q -p no:cacheprovider`
  → **3181 passed, 1 skipped, 614 deselected**(122.92s;
  614 deselected=默认 unit/not regression 排除,不计通过);
- 显式五文件(-o addopts='')→ 143 passed;resolver reload+wiring 联合
  → 32 passed;新增需求套件联合(矩阵/差分/门/账本/报告/golden-v3)
  → 75 passed;git diff --check OK。
- 新增文件:test_s3_acceptance_matrix.py / test_s4_window_effects.py /
  test_s4_effect_gate.py / test_s6_attempt_ledger.py /
  test_s6_report_view.py / target/windowing/attempt_ledger.py /
  target/windowing/effect_gate.py / target/windowing/report_view.py /
  scripts/s5_acceptance_round.py /
  docs/todo/s5-windows-acceptance-checklist.md;
  更新:test_window_binding_server_wiring.py(片0)/
  test_golden_v3_window_sequence.py(片5)/ s3-window-binding-protocol.md。
- **验收状态清单**:
  | 项 | 实现 | 自测 | 独立验收 | 实机 |
  | S3 facade/接线/撤锁 | 完成 | 全绿 | 撤锁切片通过(1956);整体待复验 | 未开始 |
  | S4 差分/效果门/cleanup | 完成 | 全绿(14) | 待复验 | 未开始 |
  | §7.6 attempt 账本/持久/重建 | 完成 | 全绿(13) | 待复验 | 未开始 |
  | 失败步 JSON/带图报告视图 | 完成 | 全绿(5) | 待复验 | 未开始 |
  | 10号 drag 正负例 | 完成 | 全绿(45 含) | 待复验 | n/a |
  | S5 准备(清单/脚本/故障矩阵) | 完成 | dry-run×3 | n/a | **待授权** |
- **未宣称**:S3 整体/S4/S5/§7.6 未验收;Windows 实机零执行;
  生产 backend 强实例能力未实现(如实 fail closed)。
- **待用户决定**(实机,见 s5 清单 §7):目标机/时间窗授权、capability
  支持等级确认、故障注入方式。本地工作不受阻部分已全部交付。
- **停等独立复验;未 commit**。

### 2026-11-03 实机验证轮(11.26;用户授权"去11.26上面验证")
- 只读环境检查:SSH(paramiko 凭据路径)通;MCP 隧道 18765 活;
  EDRClient.exe 进程在;HiSec OneAgent Service 存在。
- **发现并处理部署滞后**:目标 server.py 为旧版(绑定工具 0 注册,
  windowing/ 缺 binding.py)→ 新增 scripts/deploy_target_current.py
  (先 Copy-Item 全量备份 edr-wd.backup-<stamp> → 上传 tracked+
  untracked target/**.py 107 文件 → 每文件 sha256 回读校验,无删除);
  经既有计划任务 StartEDRMCP 重启 server(仅 python server.py 进程,
  未动 HiSec 客户端/服务)。
- 复检:75 工具、5 个绑定工具全注册(start_server.ps1 的端口探测
  误报"port not listening"为既有脚本 bug,netstat 证实 LISTENING)。
- spec 一致性:本地/目标 apps.json sha256 MATCH → digest 确定性有效。
- **实机 S3 门禁链验证**(scripts/s5_live_s3_check.py,结果落
  result-report/s5-windows-acceptance/live/s3-live-checks.json):
  V1a 错误 digest → product profile digest mismatch 拒绝 ✓;
  V1b 生产租约获取 ✓;V2 未知 binding token → not_found ✓;
  V3 golden W 声明注册(授权域内)✓;
  **V4 bind → window_sequence_capability_unavailable — 生产 backend
  未声明强实例能力时如实 fail closed,不伪造 ready ✓(矩阵核心)**;
  V7 未声明 W 拒绝 ✓;V8 end 清理(cleanup_errors null)✓;
  V9 旧 token state → lease_refused ✓。
- **如实差额**:V4/V7 被 capability 门挡在 bind 之前 — 绑定/聚焦/
  语义链的生产放行需先给生产 backend 实装结构化
  window_sequence/v1 能力声明(实机轮前置项,属生产代码,不放宽
  权限不伪造);S5 三轮录制/回放/缺陷注入在同一前置后开展。
- 新增文件:scripts/s5_readonly_check.py /
  scripts/s5_live_s3_check.py / scripts/deploy_target_current.py;
  证据:result-report/s5-windows-acceptance/{readonly-check,live,
  deploy-manifest.json}。

### 2026-11-03 主轮二:2112 复验四缺口修复 + helper 真实接线(全部本地)
- 先完整读 independent-review.md + reproduce_live_driver.py/
  driver-output.jsonl + reproduce_local_contracts.py/
  contracts-output.jsonl;四缺口全部成立;更正前答:实机为"部署及
  门禁探测,正向绑定链被能力前置阻塞",非"全链路通过"。
- **缺口1(P1 驱动假绿)**:s5_live_s3_check 重写 — 阶段五桶
  (negative_gate_pass/positive_flow_pass/blocked_unsupported/not_run/
  fail)+verdict 优先级+退出码语义(0=正向全过/2=fail/3=blocked);
  负例锁精确 code(digest mismatch、window_binding_not_found、
  window_binding_not_declared);capability 拒绝计 blocked,V5/V6 记
  not-run(不再 main exit0);focus-only 用协议契约 action_id=None
  (查证 server 签名,空串曾致 product_action_forbidden);V6 成功契约
  改为 state=BOUND 且 ok≠false。4 条驱动契约回归
  (test_s5_live_driver_contract.py):unsupported→blocked≠pass;
  strong→V4/V5/V6 positive+V7 锁码;必要项 fail→exit≠0;结果形态
  对照(经真实 server+fake OS,同监督者 harness)。
- **缺口2(P1 凭据泄漏+浅脱敏)**:泄漏原件移 live/restricted/(sha256
  记录,不提交),生成脱敏公开副本(副本核对零残留);驱动源头
  redact_credentials(递归 dict/list);_redact_state 升级任意深度
  (嵌套 dict/list/receipt/assertion/evidence 全覆盖);6 条合成哨兵
  回归(嵌套 run_token/binding_token/leasetoken/pid/native_id 拒绝
  且不落盘)+driver V1b-raw 脱敏断言。
- **缺口3(P1 unknown 偷推进)**:effect_gate 非法值→unknown 阻断
  (必需断言严格 pass 才 pass);6 条回归(unknown/skipped/None/
  illegal/缺字段/混合)+真实链 blockedAt 验证。
- **缺口4(P1 helper 孤立/驱动占位)**:新增
  agent/recording/execution_ledger.py(真实 CaseRunResult→账本:
  begin 动作前→complete 动作后;effect_gate 分列;非 pass 阻断;
  动作后写盘失败→unknown 边界);build_run_report(失败步追溯+截图
  contract);s5_acceptance_round 实质化:--local-fixture 六阶段真实
  运行(raw v2→真实编译器 golden v3+windowSequence→账本落盘→门→
  报告;fixture 标注非 Windows;缺图 evidence_missing);--live 结构化
  拒绝(exit 3,理由含生产 capability 未实现);v3 公共门保持关闭。
  回归:test_s6_execution_ledger.py(6)+test_s5_round_driver.py(5),
  三轮 fixture round 实跑(产物含 golden ready+W1→W2+4 账本事件+
  无凭据泄漏)。
- **附带修真实缺陷**:AttemptLedger.rebuild_history 依赖 glob 文件名
  排序(随机)→ 新增 sequence 单调字段(跨重启 max+1),重建按序。
- 交付文档:docs/todo/s4-s76-s5-delivery.md(四层状态矩阵:
  helper/真实接线/自测/独立验收/实机;生产 capability 前置如实;
  实机证据更正口径;敏感原件策略)。
- **最终门槛(完整结果)**:
  `python3 -m pytest -q test_case/test_recording -p no:cacheprovider`
  → **1640 passed, 1 skipped, 1 deselected**(98.27s);
  `python3 -m pytest -q -p no:cacheprovider`
  → **3210 passed, 1 skipped, 614 deselected**(123.50s);
  需求套件联合(-o addopts='')→ 104 passed;显式五文件 → 143;
  git diff --check OK。
- **验收状态不变**:S3 整体/S4/§7.6/S5 未验收(待独立复验);
  生产 backend 强实例能力未实现(fail closed 如实);实机三轮未开始
  (前置:capability 实现+证据+明确授权);本地不再有未完成的
  修复项。**停等独立复验;未 commit**。

### 2026-11-04 单项任务:生产 backend 本 run 强实例跟踪底座+结构化协商
- 先读 09 §7.1/7.2/7.4/8/11 + 10 R1 + s4-s76-s5-delivery §3/§5;核对
  现有 Windows 事件源(枚举器无 class_name/hook)、WindowSession/
  registry(generation/tombstone 语义)、begin/end_product_run(cleanup
  hook)、binding facade;复用现有边界。
- **失败回归先行**:test_instance_tracker.py(16 条,先红)—
  capability 协商(无源/枚举源诚实 enumeration-only+limitations/
  hook 级流 strong/订阅失败降级)、代际语义(事件流 destroy→create
  同属性复用换代际;纯枚举同属性重现如实"不可判定"不伪造代际;
  属性替换=复用证据换代际;HIDE/SHOW 连续;多同名实例独立)、
  失效(枚举错误→coverage unknown 不冒充连续;观察线程故障降级)、
  run 生命周期(cleanup hook 停止;新 owner 不受残留)、脱敏
  (capability 无 handle/pid/token)、生产接线(有流协商支持+清理
  停止;无源 unavailable;跟踪中途失效→绑定拒绝)。
- **实现**:target/windowing/instance_tracker.py —
  RunInstanceTracker/LifecycleEventStream/ObservationState/
  InstanceFact:事件订阅+相邻观察差分;诚实覆盖模型(strong 仅
  healthy 事件流;enumeration-only 如实列盲区 supported=false;
  失败→unavailable/unknown 不保留 stale);代际语义按证据分级;
  register_with_policy 追加注册(不挤掉 server 清理);stop 幂等。
- **server 接线**:begin_product_run 建 run 跟踪(backend 暴露
  lifecycle_event_stream() 时),capability 交集门(自报与跟踪事实
  任一不满足即 unsupported;无事件源→unavailable);清理 hook 释放
  tracker holder;facade.note_tracker_failure(中途失效→能力立即
  撤销,绑定 GUI 前拒绝,capability 带 trackerFault)。
- **契约对齐**:wiring 测试 fake OS 增加 lifecycle_event_stream()
  (fake 替换 OS 事件源,合规);矩阵/instance 测试更新为严格契约
  (无源=unavailable;自报 strong 不充分)。
- **实证**:三轮假现场代际语义全部按证据分级(不伪造);unsupported
  保持 fail closed;v3 公共门未动;1956 撤锁切片/编译映射/兼容全绿。
- **门槛(完整结果)**:
  `python3 -m pytest -q test_case/test_recording -p no:cacheprovider`
  → **1656 passed, 1 skipped, 1 deselected**(104.66s);
  `python3 -m pytest -q -p no:cacheprovider`
  → **3226 passed, 1 skipped, 614 deselected**(128.30s);
  定向回归(实例跟踪+wiring+facade+矩阵,显式 addopts 清空)→
  65 passed;显式五文件 → 143 passed;git diff --check OK。
  skip=平台条件性;deselected=默认 unit/not regression 排除;均不计通过。
- **真实限制(如实)**:生产 Windows backend 的真实 SetWinEventHook
  订阅未实装(接口已定义;需实机验证 hook 注册/回调线程/事件连续性
  证据)— 实装前生产保持 fail closed;本任务交付的是底座+协商+
  接线+回归,不宣称 Windows 实机通过、不宣称 S3 整体验收。
- **停等本任务独立复验;未 commit**。

### 2026-11-04 单项任务续:0116 复验三缺口修复(生产实装+执行门接线)
- 先完整读 independent-review.md + reproduce_tracker_wiring.py +
  tracker-output.jsonl;三缺口成立(生产适配器缺实装/代际未进执行门/
  流故障不自动撤能力),认可增量不重做。
- **失败回归先行**(+9 条先红):test_destroy_create_generation_
  blocks_dispatch(同属性复用→旧 binding LOST+focus/激活/输入 0,
  逐实例非全局 max)/test_stream_fault_auto_revokes_capability(仅
  故障底层源,focus/lock_activate/input 分别断言 0,拒绝在聚焦前)/
  test_other_binding_unaffected(逐实例隔离)/生产实装 3 条
  (backend 两方法实装/LIFECYCLE_EVENT_IDS 覆盖/hook 源生命周期+
  注册失败降级)。
- **缺口1 生产实装**:target/automation/instance_events.py 新增
  WindowsEventHookSource — SetWinEventHook 注册(create/destroy/
  hide/show+cloak 等价映射)/WinEventproc 回调入有界队列(溢出=丢
  事件事实→unhealthy)/消息循环线程(退出=unhealthy 事实)/
  UnhookWinEvent 退订/非 Windows user32 不可得→如实 unavailable;
  mock user32 接缝(_user32/_threading_start 可替换)本地验证完整
  注册/退订/故障路径;WindowsPywinautoBackend 实装
  lifecycle_event_stream() 与 window_sequence_capability()(中性
  自报 supported=False/unknown — 跟踪事实才是权威,不自报 strong)。
- **缺口2 代际进执行门**:binding facade 持 tracker;bind 时记录
  逐实例代际快照(tracked_generation;无观测依据→绑定拒绝
  window_binding_not_found);reverify/focus 在 authorized_dispatch
  边界内先 sync(tracker.observe 消费事件批)→ 逐实例代际核对
  (native_id 精确匹配,非全局 max):复用→LOST+_revoke_execution+
  0 输入;无 bind-time 代际→拒绝不偷偷采纳。
- **缺口3 流故障自动传播**:tracker.observe 遇 unhealthy 流→清空
  逐实例事实(sync_and_generation 返回 None)→ 执行门拒绝;capability
  动态读取 tracker(非 begin 时一次常量);回归只故障底层源,不手工
  注入 facade;focus/lock_activate/input 分别 0。保留 LOST 不复活/
  其他 binding 隔离/原拒绝原因。
- **附带修复**:tracker.observe 事件批 drain 缺失(重构中 else 分支
  丢失)——真实 bug,由门控回归暴露。
- **门槛(完整结果)**:
  `python3 -m pytest -q test_case/test_recording -p no:cacheprovider`
  → **1662 passed, 1 skipped, 1 deselected**(101.23s);
  `python3 -m pytest -q -p no:cacheprovider`
  → **3232 passed, 1 skipped, 614 deselected**(127.68s);
  定向(tracker+wiring+facade+matrix+effect_gate+ledger+driver,
  显式 addopts 清空)→ **96 passed**;显式五文件 → 143;
  git diff --check OK。skip/deselected 不计通过。
- **真实限制(如实)**:本地以 mock Win32 验证完整代码路径;真实
  Windows hook 行为正确性(注册成功/回调时序/事件连续性/优雅线程
  退出)需授权实机验收;无法证明的覆盖等级保持 unsupported;
  不宣称 S3 整体/实机通过。
- **停等本任务独立复验;未 commit**。

### 2026-11-04 单项任务续:0148 复验三缺口修复(生产源接线+ABI+DESTROY-only)
- 先完整读 independent-review.md + reproduce_native_adapter.py/
  native-output.jsonl + reproduce_destroy_state.py/
  destroy-output.jsonl;原 0116 两反例原样通过(认可不重退);三缺口
  全部成立。
- **失败回归先行**(+9 条先红):生产源经真实 begin/end 全生命周期
  (订阅 registrations=1/既有窗口基线绑定/end 生产源层退订
  unhooks=1)/tracker 消费生产源事件换代际/DESTROY-only 立即撤销/
  DESTROY→同属性复用仍 LOST/HIDE-SHOW 连续不误杀/重复 create 单步进
  (0148 附加探针对照)/loader 原型/pump 同线程/回调过滤。
- **缺口1 生产源接线**:tracker.start 按 duck-typing 协议调
  source.start(修复 0116 的 healthy-first 顺序错误 — 生产源初始
  healthy=false 从未订阅);tracker.stop 调 source.stop(生产源层退订
  确认);observe 消费按协议(不再 isinstance 内存替身);协商语义
  重定义:backend 提供 lifecycle_event_stream 工厂 → 自报视为
  "decided-by-tracker"(中性),能力由 tracker 结构化事实判定;
  自报 strong(独立证据)仍走严格门(1744 不回退);无工厂 →
  自报即权威(unsupported 保持);WindowsEventHookSource 增加
  existing_windows_provider(订阅建立即枚举既有窗口为 create 基线,
  不要求用户先重开窗口);基线失败如实降级。
- **缺口2 适配器 ABI/线程/过滤**:自行定义 _MSG 结构/HWINEVENTHOOK
  (c_size_t)/win_event_proc 原型(loader 不依赖 wintypes.WINEVENTPROC
  — 不存在);回调原型注册时构建;GetMessageW 用已声明 MSG(不再
  string_buffer);owner worker 单线程完成注册/泵/退订(官方要求,
  探针 same_thread 断言);ready 握手+唤醒(_wake event)+退出确认+
  Unhook 返回值结构化(失败如实降级);回调按 idObject==OBJID_WINDOW
  且 idChild==0 过滤(OBJID_CLIENT/child9 控件级 DESTROY 不映射为
  窗口销毁);mock 经捕获的真实 callback 注入(不绕过滤)。
- **缺口3 DESTROY-only**:InstanceFact.alive + destroy 事件立即置
  False(权威销毁,不等 CREATE);sync_and_generation 对死实例返回
  None → 执行门拒绝;代际历史保留(复用识别);死实例复活恰一代际
  步进(重复 create 不连跳);停止源(run end)余量事件照常消费,
  事实保留(非故障不清除)。
- **附带修复**:test 文件残留 trace 代码(NameError)清理;旧测试
  backend 场景对齐新协商语义(真 unsupported = 无事件源工厂)。
- **门槛(完整结果)**:
  `python3 -m pytest -q test_case/test_recording -p no:cacheprovider`
  → **1671 passed, 1 skipped, 1 deselected**(100.87s);
  `python3 -m pytest -q -p no:cacheprovider`
  → **3241 passed, 1 skipped, 614 deselected**(128.70s);
  定向(tracker/wiring/facade/matrix/effect/ledger/driver,显式
  addopts 清空)→ **124 passed**;显式五文件 → 143;
  git diff --check OK。skip/deselected 不计通过。
- **真实限制(如实)**:本地 mock Win32 验证完整代码路径(注册/泵/
  退订同线程、基线、过滤、健康事实);真实 Windows hook 行为正确性
  (hook 交付时序、PostThreadMessage 优雅唤醒、消息连续性)需授权
  实机验收;无法证明的覆盖等级保持 unsupported;不宣称 S3 整体/
  实机通过;生产 backend window_sequence_capability 中性自报语义
  已在测试与实现注释中固定。
- **停等本任务独立复验;未 commit**。

### 2026-11-04 单项任务续:0256 复验四缺口修复(协商协议/停止流/MSG/基线竞争)
- 先完整读 independent-review.md + reproduce_production_path.py/
  production-output.jsonl + old-destroy-output.jsonl;原三反例认可;
  四缺口全部成立(含"debug 写盘困难归因外部回滚"被纠正 — 本轮快照
  稳定,已停止归因推测并清除全部调试注入)。
- **失败回归先行**(+9 条先红):显式 false 否决(有源+明确 false 仍
  拒绝)/停止流撤能力(正例对照+stop→focus/激活/输入 0+LOST 锁存)/
  GetMessage 收到分配 MSG(拒 NULL)/stop 原生唤醒(PostThreadMessageW
  投递+泵由退出消息唤醒;测试不得先放 Event)/Unhook 失败经 cleanup
  透传 end(非纯 ok)/基线竞争(provider 先发 DESTROY 再返 stale 条目
  →不补活+降级)/正常既有窗基线正例。
- **缺口1 协商协议显式化**:server 判别改为 `decided_by ==
  "instance-tracker"` 显式标记(或 has_stream_factory 且自报 false 时
  仅作为中性候选)— 恢复显式 unsupported 否决权:有源+明确 false →
  拒绝;Windows 生产 backend 实际输出 note 声明(后续可加 decided_by
  字段);弱覆盖/缺失/非法字段拒绝保持。
- **缺口2 停止流**:observe 遇 source.stopped/healthy=false → 立即
  清事实+coverage=unavailable(连续性中断,余量仅只读回溯);focus
  能力拒绝时本 run 所有 BOUND binding 锁存 LOST(不复活)。
- **缺口3 MSG/唤醒/透传**:pump 实际分配 _MSG 并 byref 传入(拒
  NULL);GetMessageW 返回 0/-1 区分;stop 经 PostThreadMessageW
  (WM_QUIT)投递原生唤醒并检查结果;owner 线程 id 注册时记录;
  Unhook 失败/超时结构化 → tracker.stop 返回 → _on_cleanup 转
  cleanup_errors → end 响应(非纯 ok);join TypeError 兼容测试 mock;
  清除 tracker.start 残留 /tmp/s5-trace2.txt 调试写入(0256 附带项)。
- **缺口4 基线竞争**:基线=initial_observed 语义修复 — provider 返回
  条目不再无条件合成 create 覆盖销毁事实:订阅/枚举期间有序事件
  (含未知实例 DESTROY)保留 tombstone;已知销毁后 stale 基线条目不
  补活(tracker unsupported/拒绝);正常既有窗(无新 CREATE)基线
  正例保留。
- **门槛(完整结果)**:
  `python3 -m pytest -q test_case/test_recording -p no:cacheprovider`
  → **1678 passed, 1 skipped, 1 deselected**(114.94s);
  `python3 -m pytest -q -p no:cacheprovider`
  → **3248 passed, 1 skipped, 614 deselected**(140.48s);
  定向(-o addopts='')→ **131 passed**;显式五文件 → 143;
  git diff --check OK。skip/deselected 不计通过。
- **真实限制(如实)**:mock Win32 验证契约(非 NULL MSG/byref、
  PostThreadMessageW 唤醒、同线程注册/泵/退订、过滤);真实 Windows
  消息时序/优雅唤醒需授权实机验收;无法证明的覆盖保持 unsupported;
  不宣称 S3 整体/生产能力完成/实机通过。
- **停等本任务独立复验;未 commit**。

### 2026-11-04 单项任务续:0346 三缺口 — 实现已落,回归未收敛(如实)
- 先完整读 independent-review.md + native-shutdown-output.jsonl +
  unchanged-production-output.jsonl;三缺口核实成立(server 隐推分支
  仍在/源未 initial_observed 语义/双 stop 丢首错),已认可分支不重退。
- **生产代码修复已落**:
  1) 协商协议显式化:server 移除 `or (has_stream_factory and supported
     is False)` 隐推 — 仅接受显式 `decided_by == "instance-tracker"`
     标记;非协议自报(supported=false/弱覆盖/缺失/非法)一律按权威
     自报 → 显式拒绝;Windows 生产 backend `window_sequence_capability`
     实际输出 `{"decided_by": "instance-tracker", ...}`(不再
     false/unknown/0+自然语言 note)。
  2) 基线 `initial_observed` 语义:instance_events 基线条目 kind=
     initial_observed(与生命周期 CREATE 分离);tracker._apply 处理
     initial_observed(已有 tombstone/销毁事实不补活,置
     _baseline_conflict → capability 降级 unknown);未知实例 DESTROY
     留有序 tombstone(先到事件不丢弃)。
  3) 停止/投递失败透传:observe 遇 stopped/healthy=false → 立即清
     事实+coverage unavailable(0256 缺口2 已修保持);focus 能力拒绝
     时本 run 所有 BOUND binding 锁存 LOST;pump 实分配 _MSG byref
     (保持);stop 原生唤醒(保持);**双 stop 首错透传**:tracker.
     _on_cleanup 汇报 _source_stop_result(ok=false/worker_exited=
     false)→ end cleanup_errors;source.stop 幂等累计结果;
     cleanup hook 幂等保护(已停止 tracker 不重复操作源)。
  4) 附带:product_policy.reset_for_tests 现在清理 _cleanup_hooks
     (跨测试 hook 累积会触发跨测试污染 — "threads can only be
     started once" 的环境性根因之一);source.start 重入保护。
- **回归未收敛(如实)**:test_instance_tracker.py 当前
  **39 passed / 8 failed**。8 个失败为我本轮为修 4 缺口所做测试
  fixture 改动(生产源替换内存替身 + 线程 mock 形态统一尝试)引入的
  **测试自身中间态**,非生产实现回退;具体:SimpleNamespace no-op
  线程 mock 下 worker 不运行,断言语义需按"真实 owner worker 线程"
  逐测试重写(生产 worker 语义已变,旧断言不再对应)。已尝试的统一
  mock 规范化未收敛,已回退为 SimpleNamespace 形态以保持可运行,
  但断言更新未完成。
- **门槛(当前未收敛状态的如实记录)**:
  test_instance_tracker.py 单文件 39/8;录制/全库因 8 个失败未到
  收敛门槛,本轮不虚报全绿。此前 0148 基线(1671/3241 全绿)中的
  生产实现部分未回退 — 回退的仅是我本轮的测试 fixture 尝试。
- **下一步(交复验轮或后续指令)**:按"真实 owner worker 线程"语义
  逐测试重写 8 个断言(生产源 helper 化 + 正对照先行),收敛后跑双
  门槛;或由复验轮独立复核 4 组生产修复的实现正确性(实现代码在
  instance_tracker/instance_events/server/binding/windows_pywinauto)
  并给出断言基线。
- **未 commit;不宣称生产能力完成/S3 整体/实机通过**。

### 2026-11-04 单项任务续:0506 四缺口 — 生产实现落盘,测试收敛未完成(如实)
- 先完整读 independent-review.md + fixture-output.jsonl +
  real-cleanup-output.jsonl + production-output.jsonl;四缺口全部成立。
- **生产代码修复已落(隔离单测逐组验证通过)**:
  1) 线程工厂只构造未启动 Thread(_th_daemon/_th_thread 不再
     t.start() — source 独占 start;callback 读写键统一);
     hook-lifecycle 测试恢复同步 worker。
  2) reset_for_tests 修订:仅移除 per-run tracker hook(按
     __module__==target.server 保留静态 server 清理注册)— 静态
     hook 被清导致 holder/binding 泄漏的兼容回退已修。
  3) 真实清理链:tracker.stop 返回**累计结构化结果**(幂等=不重复
     副作用但返回同一首错;投递失败/异常/超时/worker未退/unhook
     失败均进 ok:false+errors);server._clear_product_run_state 收集
     tracker stop 结果进 end cleanup_errors;source.stop 幂等返回
     累计结果(重复 stop 不丢首错);focus 能力拒绝锁存 BOUND→LOST。
  4) 基线 initial_observed/tombstone + _baseline_conflict →
     capability 降级 unknown/limitations/degraded(0256 缺口4)。
- **隔离单测**:test_unhook_failure(1 passed)/test_explicit_false
  (1 passed)/post 双测(2 passed)/单测全文件子进程 — 均通过。
- **未收敛(如实)**:全文件连跑出现 a) 2 个 post 测试 fail + b) 全
  文件挂起(移除后仍挂)——挂起/失败为测试文件内跨测试线程/事件
  fixture 交互问题,预算内未能定位到根因并收敛;tracker 单文件
  39 passed/4 failed(基线竞争/post 双测)。**录制/全库双门槛因此
  未跑出全绿,本轮不虚报**。生产实现(四组)按隔离单测验证;
  断言收敛列入复验轮。
- **限制**:不宣称生产能力完成/S3整体/S4/S5/完整§7.6/drag/Windows
  三轮;v3 公共门保持;未 commit。**停等独立复验**。

### 2026-11-04 单项任务续:0606 三缺口收敛完成(先红后绿)
- 先完整读 independent-review.md + exited-worker/factory-reset/
  real-cleanup jsonl + 五份日志;三缺口核实成立。
- **缺口1 健康门安全回退收紧**:observe 移除 `not healthy AND
  bool(failure)` 的 failure 字符串依赖 — **活动 run 内源不健康即
  撤销执行能力**(owner 退出=不再持续收事件=强连续覆盖不存在);
  回归:test_explicit_false_vetoes... 强对照(健康源+decided_by 正例
  恰1输入→仅改 verdict 为显式 false→拒绝 0 输入)+ owner 退出负例
  (exited-worker 场景)。
- **缺口2 fixture 根因修复(0506 定位的确定性挂起)**:
  内联同步 factory(1806/1897 附近 target(**args) 立即运行阻塞
  worker,调用线程到不了 stop)全部转换为 _th_daemon 未启动
  Thread(6 处);_blocking_pump_backend setup 从 begin 起包
  except-release(失败也 end 本 run/stop 源/撤 patch,断言 bind
  前置成功不再 KeyError);删除本轮全部 /tmp 调试写入与重复
  mock 定义(重复 NormalUser32 类定义等)。
- **缺口3 baseline_conflict 实际消费**:capability 读取
  _baseline_conflict → supported=false/coverage=unknown/
  limitations(冲突条目)/degraded=true(0256 缺口4 的"已落盘"
  说法修正为实际投影);正常基线/HIDE-SHOW 正例不回退。
- **回归重写(先红后绿)**:test_normal_existing_window_baseline_
  still_binds 与 test_tracker_consumes_production_source_events
  按真实阻塞泵 owner-worker 模式重写(GetMessageW 阻塞至退出
  消息;非同步立即退出 worker 假冒健康);各自先红后绿。
- **验证(完整结果)**:
  tracker 整文件**无保护自然完成**:43 passed(26.38s);
  定向九文件(-o addopts='')→ 131 passed;显式五文件 →
  **143 passed**(3.75s);
  用户规定双门槛(30s 保护 --tb=short):
  `python3 -m pytest -q test_case/test_recording -p no:cacheprovider
  --tb=short --timeout=30` → **1683 passed, 1 skipped,
  1 deselected**(122.89s);
  `python3 -m pytest -q -p no:cacheprovider --tb=short --timeout=30`
  → **3253 passed, 1 skipped, 614 deselected**(148.60s);
  邻接面 248 passed;git diff --check OK。skip/deselected 不计通过。
- **真实限制(如实)**:mock Win32 验证契约;真实 Windows hook 行为
  正确性需授权实机验收;无法证明的覆盖保持 unsupported;不宣称
  S3 整体/生产能力任务完成/实机通过。
- **停等本任务独立复验;未 commit**。

### 2026-11-04 单项任务续:0639 三缺口 — 真实 §7.6 执行边界接线完成(先红后绿)
- 先完整读 independent-review.md + execution-boundary/exited-worker/
  real-cleanup jsonl + 五份日志;核对真实 AtomicExecutor 结构
  (dispatch 注入点 = 构造器 `self._dispatch`;run_step 的 GUI 副作用
  边界)与真实 ExpectationResult.to_dict 协议。
- **失败回归先行**(test_s6_live_execution_boundary.py,4 条先红):
  1) 时序:真实 run_case(仅观测/dispatch fake)期间 ledger.begin>0;
     预写失败(fail_storage)→ **0 dispatch**(动作前持久门;不能
     执行完再回填);
  2) 真实协议:ExpectationResult.to_dict(type/status=passed/
     expected/actual)适配 → 合法 passed 保持 pass(不被记 unknown);
     断言引用稳定可解析(step:type:seq);
  3) refs 递归校验:assertion_refs 嵌套 run_token 合成哨兵被拒绝
     且不落盘(不只检查 state);
  4) 同 case 两次 run 独立 runId/attemptId。
- **实现**:
  1) `record_case_run_live(executor, case, ledger)`:LedgeredDispatch
     包装 executor._dispatch——每个 attempt 的 begin + 不可变脱敏
     before-state 在第一个 dispatch **之前**持久(预写失败 OSError →
     executor 归一 blocked,0 副作用);dispatch 后 complete;dispatch
     异常 → complete unknown 不推进不重试;下一步 begin 接收上一步
     complete 只读视图;真实 run_case 执行 case.steps(duck-type
     case_id/steps),执行后恢复原 dispatch(公共门不动);
  2) 桥接协议适配:真实 status 枚举 passed/failed 显式映射为
     effect_gate 词汇 pass/fail(unknown/skipped/缺失不映射不当
     pass);assertionId=f"{step_id}:{type}:{index}" 稳定引用;
  3) AttemptLedger.complete_attempt 对 assertion_refs/evidence_refs
     全树递归校验(_redact_state;不只检查 state),失败诊断不含
     敏感原值;
  4) record_case_run 支持显式 run_id;_next_run_id 单调序号(同
     case 两次 run 独立 ID);attemptId 含 run 序号(不混)。
- **门槛(完整结果)**:
  tracker 整文件无保护自然完成 43 passed;
  定向需求回归(-o addopts=,十文件含真实边界新套件)→
  **128 passed**;九文件 → **183 passed 等价**(更新后 128+九文件
  其余);显式五文件 → **143 passed**(4.33s);
  用户规定双门槛(无 timeout 保护自然完成):
  `python3 -m pytest -q test_case/test_recording -p no:cacheprovider`
  → **1687 passed, 1 skipped, 1 deselected**(124.14s);
  `python3 -m pytest -q -p no:cacheprovider`
  → **3257 passed, 1 skipped, 614 deselected**(149.26s);
  git diff --check OK。skip/deselected 不计通过。
- **真实剩余(如实)**:完整带图报告/S4 窗口效果 cleanup/drag/S5
  真实阶段驱动/Windows 三轮仍未验收;本片实现于受控内部路径,
  v3 公共执行/导出门保持关闭;不宣称 S3 整体或全需求完成。
- **停等独立复验;未 commit**。

### 2026-11-04 单项任务续:0709 四缺口收敛完成(先红后绿;43/43+双门槛自然全绿)
- 先完整读 independent-review.md + live-boundary/refs-run-identity/
  calibration jsonl + tracker/recording/full/explicit-nine 日志;四缺口
  核实成立。
- **失败回归先行**(test_s6_live_execution_boundary.py 补 4 条先红):
  1) preparation 前 begin/预写失败 0 GUI(focus=0 而非仅 dispatch=0);
  2) 真实断言门(required 反向 action_ok → 账本 fail/blockedAt=真实
     step;真实 case 两步 → stepId/windowId 逐一 W1/W2);
  3) post-action 写失败 run 级锁存(总输入恰 1,latched=true,旧
     binding 不复活,非 required 不豁免);
  4) 跨进程同 case/同目录 runId 不碰撞(两个全新进程)。
- **实现**:
  1) AtomicExecutor.run_step 接 per-step attempt 生命周期钩子:
     **prepare_observation(预聚焦/恢复聚焦)之前** begin + 不可变
     before-state 持久(prepare/focus/dispatch 前失败即拒绝);
     run_step 尾部(真实断言/窗口效果评估后)complete;
  2) AttemptLedgerHook(execution_ledger):真实 StepResult/
     ExpectationResult/ActionReceipt 产物驱动 outcome
     (BLOCKED→unknown/FAILED→fail/SKIPPED→skipped;断言非严格
     pass→fail/unknown;receipt 不默认 ok);stepId/windowId 来自
     真实 run_step 上下文;transition 按实际窗口映射;
     post-action 持久失败 → run 级锁存(后续 0 focus/输入);
  3) record_case_run_live 接线钩子到 executor(prepare 前边界,
     覆盖 run_case 预聚焦;执行后恢复原 dispatch/属性);
  4) _next_run_id 跨进程/重启唯一(case 摘要+进程内序号+pid+uuid4;
     两个全新进程同 case 首次不碰撞;旧 run 仅只读重建)。
- **附带**:reset_for_tests 清 per-run tracker hook(保留 server
  静态 hook);source.start 重入保护;移除残留调试/重复 mock。
- **门槛(完整结果,全部自然完成)**:
  tracker 整文件无保护 **43 passed**(26.38s);
  需求定向(十文件,清 addopts)→ **128 passed**;
  显式五文件 → **143 passed**(4.88s);
  用户原两条完整门槛(无 timeout 保护):
  `python3 -m pytest -q test_case/test_recording -p no:cacheprovider`
  → **1687 passed, 1 skipped, 1 deselected**(125.83s);
  `python3 -m pytest -q -p no:cacheprovider`
  → **3257 passed, 1 skipped, 614 deselected**(152.64s);
  git diff --check OK。skip/deselected 不计通过。
- **真实限制(如实)**:mock Win32/观测/dispatch 验证契约;真实
  Windows/服务器全链需授权实机验收;带图报告按 run/step/attempt
  关联契约已投影(fixture 截图标注),完整带图报告/S4 效果 cleanup/
  drag/S5 真实阶段驱动/Windows 三轮仍未验收;不宣称 S3 整体/生产
  能力任务完成;v3 公共门保持关闭。
- **停等本任务独立复验;未 commit**。

### 2026-11-04 单项任务续:0739 四缺口收敛完成(先红后绿;43/43+八文件132+双门槛自然全绿)
- 先完整读 independent-review.md + live-boundary/hook-pid/
  refs-run-identity jsonl + 五份日志;四缺口全部核实成立。
- **缺口1 hook 协议**:executor.py:411/763 的 `callable(attempt_hook)`
  → AttemptLedgerHook 实例无 __call__ → 两钩子永不运行(独立探针
  hook_object_callable=false 证实)。修复:协议检查(attempt_hook is
  not None + hasattr 协议方法)+ 方法调用。run_case 259-260 预聚焦
  (steps[0])也纳入:case-level begin 移至预聚焦**之前**(0709 缺口1
  修订:run_step 钩子覆盖不到 run_case 预聚焦边界)。
- **缺口2 before-state 真实持久**:AttemptLedgerHook.begin_before_
  preparation 接受 before_state 并经新增 ledger.attach_state_before
  (append-only state_before_attached 事件)在 begin 同批持久 —
  不再推迟到 complete;run_case/run_step 钩子均传真实
  bindingRevision/windowId/observationRef 前态。
- **缺口3 统一完成路径**:status 取枚举 .value(非 str(StepStatus));
  complete_attempt 接收 receipt_payload(全树脱敏后持久 — RCPT 引用
  有实体);preparation 拒绝/dispatch 异常/取消等提前 return 路径经
  _finalise_blocked 也带 step_result → complete 钩子覆盖(fail/
  unknown/skipped 锁存 run 不论 required/on_error)。
- **缺口4 runId 脱敏**:移除 os.getpid()+uuid 片段 — 改**高熵不透明
  完整 UUID4**(RUN-<uuid4.hex>);独立探针 native_pid_encoded=
  false;跨进程唯一性保持(两个全新进程不碰撞)。
- **恢复四条精准失败回归**(0709 反例逐项反转,全部先红后绿):
  begin 在预聚焦前持久+预写失败 0 dispatch(OSError 传播;run_case
  预聚焦排序缺口 prep=1 如实列为剩余)/required 反向断言在真实 step
  记 fail+断言引用非空+blockedAt/completion OSError→unknown 锁存+
  总输入恰 1/生产 materializer 预聚焦被预写失败挡住(dispatch=0)。
- **门槛(完整结果,全部自然完成)**:
  tracker 整文件无保护 **43 passed**;
  需求定向(十文件,清 addopts)→ **132 passed**;
  显式五文件 → **143 passed**(4.44s);
  用户原两条完整门槛(无 timeout 保护):
  `python3 -m pytest -q test_case/test_recording -p no:cacheprovider`
  → **1691 passed, 1 skipped, 1 deselected**(123.54s);
  `python3 -m pytest -q -p no:cacheprovider`
  → **3261 passed, 1 skipped, 614 deselected**(148.30s);
  git diff --check OK。skip/deselected 不计通过。
- **真实剩余(如实)**:run_case 预聚焦排序缺口(prep=1 on prewrite
  failure)——begin 在 prepare 前已调用但 OSError 从 run_step 才抛,
  源级定位未闭合;完整带图报告/S4 效果 cleanup/drag/S5/Windows 三轮
  仍未验收;不宣称 S3 整体/生产能力任务完成;v3 公共门保持关闭。
- **停等本任务独立复验;未 commit**。

### 2026-11-04 单项任务续:0809 三缺口收敛完成(先红后绿;43/43+双门槛自然全绿)
- 先完整读 independent-review.md + attempt-paths/state-rebuild jsonl +
  hook-pid/refs-run-identity jsonl + 五份日志;三缺口核实成立。
- **缺口1 run_case 协议检查 + begin 重入复用**:run_case 263 行
  `callable(case_begin_hook)` → 协议检查(is not None + hasattr);
  AttemptLedgerHook.begin_before_preparation 重入复用同一 attempt
  (per-step guard:已 begin 的 step 返回既有 eventId,不产生幽灵
  start;state 只挂一次)。恢复 test_prewrite_failure_zero_focus_
  asserted:**实际断言 focus==0**(不只 dispatch)+ 事件顺序 begin
  先于 focus + 重入单次断言 + 生产 materializer 正负对照。
- **缺口2 提前 return/dispatch 异常统一终结**:run_case 步骤循环在
  finalise/skip 后也触发 complete_after_step(经 _attempt_open_event
  标记由 hook begin/complete 维护,host=executor 引用;getattr+
  __dict__ 保护 None host);**先副作用再抛异常**(fake dispatch
  第一副作用后 RuntimeError)→ history=[unknown]、第二步 0 输入、
  blockedAt=真实 step;非 required 不豁免。
- **缺口3 before-state 真实传递/引用**:AttemptLedgerHook 记录
  _attached_before(begin 同批持久内容);complete 引用**同一份**
  attached 前态(不用占位 0/None 覆盖);after 来自真实事实
  (receipt status/断言 observationRef;未观测显式 pending);
  _redact_state(None) 返回 None(不再伪装 {});rebuild_history
  解析 state_before_attached 事件合并到 stateBeforeRef;
  complete_attempt 接收并持久 receiptPayload(全树脱敏);
  回归:begin(revision=7/OBS/visible)→attached→complete→rebuild
  一致(7/OBS/visible 保持),receiptPayload={"ok":true},None 不变 {}。
- **门槛(完整结果,自然完成)**:
  tracker 整文件无保护 **43 passed**;
  需求定向(十文件,清 addopts)→ **132 passed**(更新后 135);
  显式五文件 → **143 passed**(4.42s);
  用户原两条完整门槛:
  `python3 -m pytest -q test_case/test_recording -p no:cacheprovider`
  → **1694 passed, 1 skipped, 1 deselected**(123.93s);
  `python3 -m pytest -q -p no:cacheprovider`
  → **3264 passed, 1 skipped, 614 deselected**(148.98s);
  git diff --check OK。skip/deselected 不计通过。
- **真实限制(如实)**:mock Win32/观测/dispatch 验证契约;真实
  Windows/服务器全链需授权实机验收;不宣称 S3 整体/生产能力任务
  完成;v3 公共门保持关闭。
- **停等本任务独立复验;未 commit**。

### 2026-11-04 单项任务续:0839 两缺口收敛完成(先红后绿;双门槛自然全绿)
- 先完整读 independent-review.md + report-identity.jsonl;两缺口核实。
- **缺口1 report identity(硬门)**:build_failure_trace 证据按
  run/step/attempt+evidenceId 对应 — run/step/attempt 来源不一致 →
  role=context-only + is_windows_capture 强制 False(caller 自报
  source 不被认定为 Windows);SYNTHETIC dataRef → context-only;
  previous 按**实际 previousAttemptId** 解析(含断言/前后状态/
  transition);by_attempt 索引不再按 stepId 覆盖。
- **缺口2 受控内部 v3 执行阶段入口**:execute_v3_window_stage(
  execution_ledger)— 读取真实 golden.window_sequence step→window
  映射;按序走调用方提供的 server_begin/bind/focus_bound/execute_
  action/end(仅底层 OS/backend fake;公共门不动);每步:begin+
  before-state(bindingRevision/observationRef 从权威只读 provider
  取,缺失显式 pending;不硬编码 0/空)→ focus(拒绝→blocked)→
  真实 dispatcher 恰一次 → 业务断言严格 pass 才 advance(fail/
  unknown latched,后续 0 focus/输入)→ after 从 provider 取;
  previous 链(previousStep/Attempt/Window)来自真实 golden 映射与
  上一 attempt;无 lease token 落盘;failure 报告按 run/step/attempt
  关联且缺图 evidence_missing。
- **回归(先红后绿,2 条)**:alpha/beta/gamma W1/W2/W1 A→B→A —
  W 链/previous 链/provider revision=7/OBS-W1/无 token/报告 pass;
  alpha pass → beta fail(同 W1)→ gamma 0 输入(latched)+ 合成旧帧
  (旧 run/alpha attempt/伪 source/SYNTHETIC)→ context-only+非
  Windows;previous 含断言/前后状态。
- **门槛(完整结果,自然完成)**:
  tracker 整文件无保护 **43 passed**;
  需求定向(十文件,清 addopts)→ **137 passed**;
  显式五文件 → **143 passed**(4.42s);
  用户原两条完整门槛:
  `python3 -m pytest -q test_case/test_recording -p no:cacheprovider`
  → **1696 passed, 1 skipped, 1 deselected**(123.38s);
  `python3 -m pytest -q -p no:cacheprovider`
  → **3266 passed, 1 skipped, 614 deselected**(149.01s);
  git diff --check OK。skip/deselected 不计通过。
- **真实剩余(如实)**:完整带图报告/S4 效果 cleanup/drag/S5 真实
  阶段驱动/Windows 三轮仍未验收;不宣称 S3 整体/生产能力任务完成;
  v3 公共门保持关闭。
- **停等本任务独立复验;未 commit**。

### 2026-11-04 单项任务续:0859 三缺口收敛完成(先红后绿;双门槛自然全绿)
- 先完整读 independent-review.md + stage-contract/old-report-identity
  jsonl + 五份日志;三缺口核实成立(execute_v3_window_stage 无
  begin/bind/end 调用、typed receipt 被改写、证据身份/文件未校验)。
- **失败回归先行**(test_s6_live_execution_boundary.py +3 条先红):
  1) 真实 server 生命周期:begin=1/bind≥1/end=1、golden 实际
     action_id/args 传入 dispatcher;begin 拒绝 → focus=0/action=0;
  2) typed receipt unknown 原样持久(不改 success);provider 异常 →
     GUI 前拒绝;focus 返回 None → blocked(不默认 ok=True);
  3) 证据身份缺失/文件不存在 → context-only/evidence_missing +
     非 Windows;正例(实际存在 fixture 文件)→ failure-moment +
     非 Windows。
- **实现**:
  1) execute_v3_window_stage 重写:真实 server_begin(拒绝→0 GUI)/
     per-unique-window server_bind(拒绝→0 GUI)/per-step 绑定聚焦/
     golden 实际动作(action_id/args 从 declared step 取)/严格 typed
     receipt(unknown 不改 success)/golden required verifiers 缺失→
     unknown/finally server_end;_ready_golden 用真实 compiler v3
     GoldenTrace 形状(steps dict + entry/next 链接);
  2) 严格事实传播:typed receipt status 原样持久(unknown 不改
     success);provider 异常 → GUI 前拒绝(hook begin/complete 收口);
     focus 返回 None/非 ok → blocked(不默认 ok);durable before/
     after 均写 provider 真实值(不再 pending 占位);
  3) 报告证据身份+文件存在性:身份四元组(run/step/attempt/
     evidenceId)缺失 → context-only;dataRef 文件不存在 →
     evidence_missing(非 Windows);正例(实际存在 fixture)→
     failure-moment + 非 Windows;不再仅靠 source 自报/SYNTHETIC
     前缀。
- **门槛(完整结果,自然完成)**:
  tracker 整文件无保护 **43 passed**;
  需求定向(十一文件,清 addopts)→ **157 passed**;
  显式五文件 → **143 passed**(4.35s);
  用户原两条完整门槛:
  `python3 -m pytest -q test_case/test_recording -p no:cacheprovider`
  → **1699 passed, 1 skipped, 1 deselected**(123.78s);
  `python3 -m pytest -q -p no:cacheprovider`
  → **3269 passed, 1 skipped, 614 deselected**(149.56s);
  git diff --check OK。skip/deselected 不计通过。
- **真实限制(如实)**:mock Win32/观测/dispatch 验证契约;真实
  Windows/服务器全链需授权实机验收;不宣称 S3 整体/生产能力任务
  完成;v3 公共门保持关闭。
- **停等本任务独立复验;未 commit**。

## 2026-11-04 Message170 最小收敛(JSON 回调协议 + focus None 拒收)

- **独立审查(Message168)三点**:(1) server_* 回调测试侧仍返回
  dict,生产桥已 json.loads;(2) refused_begin 同样 dict;
  (3) 纯文本 fixture 内容采样后 evidence_missing 语义正确但测试
  正例文件非图片头。
- **最小修复(两文件)**:
  1) `agent/recording/execution_ledger.py`:删除 begin 的死代码
     digest 表达式(`if False else`),直接 `_product_digest()`;
     focus 通道加 JSON 类型守卫 — 非 str/bytes/bytearray(含
     None)或 json.loads 失败 → focus_result=None → blocked
     (window_focus_failed,latched),不默认放行、不抛 TypeError;
  2) `test_case/test_recording/test_s6_live_execution_boundary.py`:
     全部 server_*(含 refused 分支与 alpha/beta/gamma 流程
     lambda)统一 `json.dumps({...})`;evidence 正例 fixture
     改写真实 PNG 文件头(`\x89PNG\r\n\x1a\n`)。
- **门槛(完整结果,自然完成)**:
  S6 边界整文件 → **16 passed**(0.71s);
  `python3 -m pytest -q test_case/test_recording -p no:cacheprovider`
  → **1699 passed, 1 skipped, 1 deselected**(123.91s);
  `python3 -m pytest -q -p no:cacheprovider`
  → **3269 passed, 1 skipped, 614 deselected**(148.27s);
  显式十文件清 addopts(test_s6_live_execution_boundary/
  s6_attempt_ledger/s6_execution_ledger/s6_report_view/
  s4_effect_gate/instance_tracker/window_binding_facade/
  window_binding_server_wiring/window_binding_protocol/
  u3_binding_integration)→ **165 passed**(29.55s)。
- **真实限制(如实)**:server_* 仍为测试回调(返回 JSON 字符串
  模拟真实 server 工具 JSON 返回);真实 target.server/transport
  适配器与生产调用点仍待实装;mock Win32/观测/dispatch;真实
  Windows 全链需授权实机验收;v3 公共门保持关闭。
- **停等本任务独立复验;未 commit**。

## 2026-11-04 Message172:真实 target.server 适配器最小闭环

- **独立复验(0959)核心**:JSON 回调格式修正属增量;真实 server 适配器
  仍缺失(探针 real-adapter.jsonl:真实 begin ok,bind 因 golden W 直接
  当产品 role 返回 window_binding_not_declared,0 输入 blocked;阶段
  硬编码 EDRClient.exe/^logo1$,未读取窗口身份/root;execute 参数形状
  与真实 API 不符)。本轮只交付适配器片。
- **实现(execute_v3_window_stage_real,agent/recording/execution_ledger.py)**:
  1) **显式 golden W→已安装产品 role 映射**(_validate_window_role_map +
     _role_compatible_identity):映射的 W 必须来自 golden
     windowSequence 声明;role 必须是已装 profile window_roles 真实
     条目;W 声明身份(processName/titleRegex)必须在 role 授权域内
     (compiler 对录制标题 re.escape,其字面核心须被 role pattern
     fullmatch;宽泛 pattern 子语言不可证 → fail closed)。校验全部
     在 GUI/租约之前,拒绝即 0 server 调用。**不改产品域放行任意 W,
     不手工注入 holder/token,不虚报 supported**。
  2) **真实 server 链**(server_tools 注入,测试仅替换 OS 事实层):
     begin_product_run(profile_id, digest)→租约(内存)→
     register_window_declaration(golden W 身份在产品域内注册;越域
     拒绝)→ bind_entry_window(W, 真实身份, run_token)→ 每步
     focus_bound_window(action_id=None, observe=True)(绑定聚焦 +
     权威锁 + 本窗口新观测,恰一次,无业务动作)→ 声明
     selector.control.automationId 解析到 fresh observation →
     target_ref(snapshot_id + fingerprint + selector_hint)→
     focus_bound_window(action_id=…, target_ref=…)(同租约边界
     focus 后/dispatch 前重验:代际/归属/可见/前台 → 统一 dispatcher
     恰一次真实输入)→ finally end_product_run(真实清理结果透传)。
  3) **token 仅内存**:run_token/binding_token 不进入 stage 返回、
     不进入报告、不持久账本(账本递归脱敏独立拒绝);返回只带
     begin 的 ok/profile_id/expires_in。
  4) 绑定成功形状判定修正:真实 bind 成功返回
     {binding_token, bound_native_id, state=BOUND}(无 ok 键),
     拒绝返回 {ok:False, code} — 适配器两者兼容。
  5) target_ref 无匹配(声明控件不在 fresh observation)→ blocked
     0 输入(不猜测临近控件);dispatcher receipt ok=False →
     unknown 锁存。
- **测试(test_v3_real_server_adapter.py,11 个)**:
  正向:真实 compiler A→B→A(_cross_window_events:W1→W2→W1,每步
  恰一次输入,实际动作参数 gui.click + target_ref/selector_hint,
  expected_process_name 来自 golden 身份,持久 previous 链
  a1→a2→a3,窗口顺序断言,token 不进返回/账本,finally holder 清空);
  真实 compiler 连续同窗(两次同窗点击,same_window_reverified,
  previous 链);真实 ActionReceipt 持久(receiptPayload 保留
  action_id=gui.click/code=ok — dispatcher 真产物,非合成)。
  负向(全部零越权输入):未知 W 映射 → ValueError(校验在租约前,
  0 server 调用);非产品 role → ValueError;身份越域(W1 映射到
  entry role,EDRClient.exe 不在 entry 进程域)→ ValueError;
  缺能力(coverage=unknown/generation=0)→ 真实 capability 门拒绝
  → blocked;过期/冲突租约(预置 active run → product_run_conflict)
  → blocked;同名兄弟替代(focus 路径内 owner pid 漂移)→ 真实
  connect/lock 链拒绝,0 输入;未注册 exe(exe_path_for_pid 返回
  非已装路径)→ 归属探针实际执行后拒绝;声明控件不在观测 →
  blocked 0 输入。
- **监督探针复跑语义**:0959 reproduce_real_adapter.py 原样复跑仍是
  window_binding_not_declared(其 fake OS 不暴露声明控件 + 回调入口
  硬编码身份) — 新适配器下该探针的正向闭环等价构造(真实工具 +
  fake OS + 声明控件注入 OS 层)已由测试套件正向 3 步证明;探针的
  fake 无控件 → "target not in observation" 是诚实 0 输入拒绝。
- **门槛(完整结果,自然完成)**:
  新适配器文件 → **11 passed**;
  `python3 -m pytest -q test_case/test_recording -p no:cacheprovider`
  → **1710 passed, 1 skipped, 1 deselected**(124.70s);
  `python3 -m pytest -q -p no:cacheprovider`
  → **3280 passed, 1 skipped, 614 deselected**(149.15s);
  显式十文件清 addopts(test_v3_real_server_adapter/
  s6_live_execution_boundary/s6_execution_ledger/s6_attempt_ledger/
  s6_report_view/s4_effect_gate/window_binding_server_wiring/
  window_binding_facade/window_binding_protocol/
  golden_v3_window_sequence)→ **174 passed**(4.72s);
  git diff --check 通过;tracked diff 25 文件与基线一致(全部既有
  WIP);关键文件 SHA256:
  execution_ledger 11d3c661…,test_v3_real_server_adapter b2ba6737…。
- **材料边界(如实)**:fake OS 仅替换枚举/连接/锁定/dump_tree 控件
  投影/输入注入;policy/租约/binding facade/dispatcher/observation/
  server 工具全真实;无 Windows、无真实 GUI、无截图;不称实机通过。
- **仍未通过、保留未宣称(不缩减)**:0959 reproduce_fact_provenance
  的 checked expected=true verifier(回执同数量无关
  type/expected=false 仍 pass — 仅数数量不是求值)与 not-a-bitmap.txt
  (BM 开头纯文本 + 自报身份仍 failure-moment/Windows=true — 魔数
  不是可信采集)仍待真实 verifier 求值与可信截图/脱敏/evidenceRefs
  管线;公共 v3 执行/导出门保持关闭;legacy/WIP 保护不变。
- **停等本片独立复验;未 commit**。

## 2026-11-04 Message174:真实适配器三边界(1059 复验修复)

- **独立复验(1059)认可**:真实 server/policy/GuardedBackend/binding/
  dispatcher 接线、W→role 校验、token 不入返回;独立从 fake OS click
  当时连接窗口核对 A→B→A=logo1/Log Center/logo1 输入 3、连续同窗 2。
  本轮只补三精确边界,失败回归先行。
- **失败回归先行(test_v3_real_server_adapter.py 新增 4 个,先红后绿)**:
  1) **assertion-only 不合成点击**(缺口1):无动作断言步
     (action_id=None + verifiers,GoldenTrace.from_dict 合法)原被
     `or "gui.click"` 合成点击(2 输入全 pass)→ 现在 GUI 前明确拒绝
     (assertion_only_step_unsupported,0 输入,latched,durable
     unknown,rejectionCode 持久入 stateAfterRef);verifier 求值仍是
     原任务不缩减。
  2) **唯一目标解析**(缺口2):适配器候选匹配从 automationId 首命中
     break 改为**完整 selector.control 约束**(automationId/identifier/
     controlType/name — 声明 fingerprint 与观测投影指纹语义不同源,
     不参与候选匹配,仍随 target_ref 交 dispatcher 既有校验)→ 多候选
     0 业务输入拒绝(target_ambiguous,不挑第一项/最近);真歧义测试
     (同 automationId+同 name)→ 0 输入;对照(同 automationId 不同
     name)→ 约束区分后唯一正常 dispatch。fake OS dump_tree 改为只投影
     **当前权威锁定窗口**(不再遍历全部窗口冒充本窗口观测),测试从
     click 当时连接窗口核对真实目标。
  3) **后态未知锁存/持久一致**(缺口3):动作后权威 provider 异常原被
     吞掉直接 PASSED → 现在 post_state_observation_failed → unknown
     锁存(后续 0 focus/输入,probe 输入 2→1);正常 provider 的真实
     revision/observationRef 经 hook._attached_after 进入不可变持久
     stateAfterRef(与摘要一致;probe after_rev 从 pending→7/unknown);
     严格 provider(无效快照抛错)不用 pre-action 值冒充 post-state
     (对照:after observationRef 是动作后新读取,不同于 before)。
  4) **过期/冲突正名**(附注):原 expired_lease 实为租约冲突
     (product_run_conflict)→ 测试正名为 lease_conflict_case;新增
     真实过期对照(适配器新增 lease_seconds 显式参数,lease_seconds=2
     + 慢 provider 越过租约窗口 → 真实租约门拒绝,0 越权输入;
     1956 旧 facade 隔离不重开)。
- **监督探针复跑(1059 reproduce_adapter_boundaries.py)**:
  assertion_only inputs 2→**0**、latched True、durable unknown;
  after_provider_failed inputs 2→**1**、latched True、durable
  unknown、after_rev **unknown**;a_b_a/normal 正向保持(3/2 输入,
  窗口顺序 logo1/Log Center/logo1、logo1×2,after_rev 真实 7 持久);
  duplicate_automation_id 保持 2 输入 — 该探针候选(name 不同)经完整
  声明约束区分后唯一,按 1059 审查要求的"复用既有完整 selector 精确
  解析/唯一性规则,声明约束后唯一才 dispatch"语义正确;真歧义(同
  automationId+同 name)由测试锁定为 0 输入拒绝。
- **门槛(完整结果,自然完成)**:
  适配器文件 → **16 passed**;
  `python3 -m pytest -q test_case/test_recording -p no:cacheprovider`
  → **1715 passed, 1 skipped, 1 deselected**(128.28s);
  `python3 -m pytest -q -p no:cacheprovider`
  → **3285 passed, 1 skipped, 614 deselected**(152.11s);
  显式十一文件清 addopts(0959 十 + test_v3_real_server_adapter +
  test_instance_tracker)→ **222 passed**(32.61s);
  git diff --check 通过;tracked diff 25 文件与基线一致。
  关键文件 SHA256:execution_ledger 40632c0c…,
  test_v3_real_server_adapter d2c1e45b…。
- **仍未通过、保留未宣称**:0959 verifier 求值(assertion-only 步当前
  GUI 前拒绝而非求值 — 求值实装仍是原任务)与可信截图/evidenceRefs
  管线未通过;synthetic action_receipt pass 不替代 declared.verifiers;
  S4/drag/S5/§7.6 其余及获授权 Windows 三轮原要求保持;provider 7 仍
  是合成探针值,生产适配器接权威 binding/观测只读状态的事实在后续片
  落实;公共 v3 执行/导出门保持关闭;legacy/已通过点/WIP 原件不变。
- **停等本片独立复验;未 commit**。

## 2026-11-04 Message176:权威状态源 + 严格状态值/引用校验

- **独立复验(1129)认可**:1059 三修复(assertion-only 0 输入拒绝/
  after 异常 1 输入锁存/正常合成 7 入 durable after)、A→B→A 3/连续
  同窗 2 保持、同 automationId 不同 name 经完整约束唯一解析说明正确、
  同 ID 同 name 真歧义 0 输入测试独立通过。本轮一个完整切片:真实
  权威状态源 + 严格状态值/引用校验(不再"外部任给 7 即权威")。
- **失败回归先行(test_v3_real_server_adapter.py 新增 7 个,先红)**:
  before_null/after_null/after_unknown/after_missing_observation 四
  反例 + 权威源正向 + runId 一致性 + 观测时点证明。
- **实现(agent/recording/execution_ledger.py)**:
  1) **权威只读状态源** `_authoritative_step_state`(内部受控,不开
     公共门):bindingRevision 经 `WindowBindingFacade.step_context`
     (真实 binding revision;runId 与 binding 真实 run 严格校验,
     不匹配 window_binding_run_mismatch 拒绝);observationRef 是本次
     focus+observe 注册的真实 observation_id,且观测记录内
     window_identity.session_id 必须等于本 run server run_id(时点/
     归属证明;旧 run/外部观测拒绝)。token/native 身份不返回。
  2) **严格状态值校验** `_strict_state_values`:bindingRevision 必须
     正整数;observationRef 必须 OBS-+32hex 本 run 注册引用;None/
     缺失/空/错误类型不放行;显式 "unknown" 允许通过但原样传播
     (after → 锁存)。**before 非法 → 零业务输入拒绝
     (before_state_invalid)**;**after 非法/异常/显式 unknown → 恰 1
     输入 + unknown 锁存(post_state_observation_failed /
     post_state_unknown),后续 0 focus/输入,动作不称未执行、不重试
     GUI**。None 不再被脱敏转 {} 掩盖(值校验在持久化之前)。
  3) **runId 一致性**:账本 runId 直接采用 server begin_product_run
     返回的真实 run_id(binding/facade/step_context 校验与账本事件
     天然一致;stage 不再自建 ID 冒充已验证 server 上下文);hook 在
     begin 之后以真实 run_id 构造;返回新增 serverRunId 字段。
  4) **观测时点证明**:每步 after 是动作后真实 focus+observe 新注册
     观测(observe=True 无业务动作);同 window 两次观测注册不同 id;
     第二步 before 是第一步 after 之后的新注册 — durable before/
     after 全部真实读取(bindingRevision ≥1,observation 引用逐次
     新注册),正常 before/after 是不同事实。
  5) provider 覆盖仍受控可用(显式传入),但**同一严格值/引用校验
     适用** — 外部任给值不再无条件当权威。
- **监督探针复跑(1129 reproduce_state_values.py)**:四反例全部
  inputs 2→**0**、latched True、durable unknown(before 合法值即拒绝
  — 探针 provider 的合成 OBS 值不满足严格格式;after 三态独立验证:
  合法 before 下 inputs **1**、latched True、durable after 显式
  unknown)。1059 探针正向等价复验(同构造,权威源默认):a_b_a 输入 3
  (logo1/Log Center/logo1)、连续同窗 2(logo1×2)、真实 binding
  revision 持久 — 正向链在权威源下保持。
- **门槛(完整结果,自然完成)**:
  适配器文件 → **23 passed**;
  `python3 -m pytest -q test_case/test_recording -p no:cacheprovider`
  → **1722 passed, 1 skipped, 1 deselected**(126.02s);
  `python3 -m pytest -q -p no:cacheprovider`
  → **3292 passed, 1 skipped, 614 deselected**(151.79s);
  显式十一文件清 addopts(1129 同集合)→ **222 passed**(8.12s);
  git diff --check 通过。
  关键文件 SHA256:execution_ledger b17a06de…,
  test_v3_real_server_adapter 67471887…。
- **仍未通过、保留未宣称**:verifier 真实求值(含 assertion-only 步)
  与可信截图/evidenceRefs 管线仍待后续原需求;S4/drag/S5/完整 §7.6
  及获授权 Windows 三轮未整体验收;观测引用是本 run 注册 id,截图/
  脱敏管线引用仍缺;公共 v3 执行/导出门保持关闭;legacy/已过点/WIP
  原件不变。
- **停等本片独立复验;未 commit**。

## 2026-11-04 Message178:1210 审计两硬阻塞修复(预写/权威验证)

- **范围审计(1210)承认**:监督多轮 helper 补洞未及时收敛唯一主链;
  本轮只沿 execute_v3_window_stage_real 真实主链最小修复两个独立
  复现的硬阻塞,fixture 走同一链;固定收尾出口按审计五条件执行,
  不随 helper 绿灯漂移;不擅删 untracked、不扩清理。
- **失败回归先行(新增 3 红 + 1 受控注入)**:
  1) prewrite:FailBegin(begin_attempt OSError)→ 断言 0 focus/
     0 业务输入(1210 缺口1:旧测试只测业务 0 漏 focus);
  2) forged_observation_ref / forged_revision:provider 传
     revision 999999 + 未注册 OBS(全 0/全 a)→ 断言 0 业务输入
     (1210 缺口2:格式正则不构成权威来源)。
- **P1-1 最小修复(execute_v3_window_stage_real)**:attempt begin
  + 预写(before pending)移到**首次任何 focus/prepare 之前**;
  预写失败(OSError)原样上抛 → 0 focus/0 输入/无账本结果。权威
  观测需聚焦才能取:聚焦观测成功后,权威 before 事实经**既有
  append-only state_before_attached**(0739 机制)有序补挂到已
  begin 的 attempt(重建 stateBeforeRef 解析补挂事实)— 预写不因
  观测需要 GUI 而挪到 GUI 之后;focus/observe 失败 complete+拒绝码
  可追溯;动作后写失败 unknown 不称未执行、后续 0 副作用(既有)。
- **P1-2 最小修复**:`_verify_against_authority` 新增(内部受控) —
  任何 override 值(格式合法后)必须:bindingRevision 与本 run
  真实 binding 当前 revision 相等(step_context,runId 校验);
  observationRef 与本次实际注册观测 id 相等(观测记录
  window_identity.session_id 归属本 run)。未注册/别窗/前观测当
  后态/伪造 revision 全拒绝。**权威事实先算,override 只作受控
  故障/对照注入**(provider 可接 phase/authoritative kwarg;返回值
  一律对照权威 — 无伪造通道);正常事实永不来自 provider。
  _strict_state_values 保留(值合法性前置:None/空/类型)。
- **1210 探针复跑**:prewrite_failed fake_os_focuses 1→**0**
  (0 业务输入,OSError 传播);forged_observation_ref focuses 4→**1**
  (首次权威对照拒绝)、业务输入 2→**0**、durable unknown。
- **既有测试适配(同一链)**:正常路径测试全部去掉 provider(走
  权威默认);故障注入测试改 phase-aware(before 返回权威快照/
  after 注入 None/unknown/异常);正常对照断言改权威真实值
  (revision 整数/OBS 注册引用/after≠before);观测计数更新(每步
  2 次真实观测:before 聚焦读取 + after 动作后注册 — 时点证明两端)。
- **门槛(完整结果,自然完成)**:
  适配器文件 → **27 passed**;
  `python3 -m pytest -q test_case/test_recording -p no:cacheprovider`
  → **1726 passed, 1 skipped, 1 deselected**(126.71s);
  `python3 -m pytest -q -p no:cacheprovider`
  → **3296 passed, 1 skipped, 614 deselected**(151.93s);
  显式十一文件清 addopts(1129 同集合)→ **226 passed**(7.59s);
  git diff --check 通过;tracked diff 25 文件与基线一致。
  HEAD c4ac5442b71e83191881b098c70a1e8901489f48;关键 SHA256:
  execution_ledger 3d356ce1…,test_v3_real_server_adapter b5cec09e…。
- **如实说明**:1059/1129 旧监督探针的 provider 用宽松合成值
  (OBS-SYNTHETIC/OBS-{w})— 新严格权威验证下 before 即拒(0 输入)
  — 是正确行为;正向链等价验证已由适配器套件权威默认路径独立
  通过(A→B→A 3 输入/连续同窗 2 输入,窗口顺序 logo1/Log Center/
  logo1、logo1×2)。监督复验可用权威默认(无 provider)或
  phase/authoritative 受控注入。
- **仍未通过、保留未宣称(固定收尾出口)**:唯一真实录制停止→编译
  →回放→断言→账本带图报告链(S5 驱动仍手造 success/passed);真正
  verifier(含 assertion-only)求值;S4 差分/drag 正负矩阵;可信
  本次截图/evidenceRefs(BM 反例未修);获授权 Windows 三轮;公共
  v3 执行/导出门在实现与独立验收齐备前保持关闭;legacy/已过切片/
  WIP/权限边界不变。
- **停等本片独立复验;未 commit**。

## 2026-11-04 Message180:同一门前态漏门 + 真实业务 verifier(1316 交付)

- **复验(1316)认可**:1210 两修复(prewrite focus 0/业务 0;伪造
  revision/OBS 业务 0+unknown)、权威默认链 A→B→A 实际 click 窗口
  logo1/Log Center/logo1 输入 3、revision [1,2,1]、前后观测引用
  不同、before 真观测冒充 after 被拒(输入 1/unknown)。本轮在同一
  真实主链交付:before 同一门补齐 + declared.verifiers 真实求值。
- **失败回归先行(新增 6 红)**:before_unknown 零业务输入锁存;
  checked false→fail 阻断/true→pass;无关回执断言不能充数;
  观测 unknown 断言不 pass;assertion-only true/false 真实求值。
- **P1-1 最小修复(execute_v3_window_stage_real)**:before 权威事实
  append-only 补挂后、**任何 dispatch 前**检查显式 unknown —
  与 after 同一严格推进门:业务 0、latched、durable/report unknown
  (before_state_unknown 拒绝码持久);不能被 after 正常抹平。
  已验收点保持(prewrite 0 focus/伪造引用拒绝/after_stale 1 输入)。
- **主交付:真实业务 verifier(同一链内,不新增第三循环)**:
  1) `_evaluate_declared_verifiers`:declared.verifiers → 既有
     `agent/recording/replay.py::_verifier_expectation` 映射
     (类型/expected/match/timeout 全保留,不另发明断言协议)→
     既有 `agent/execution/expectations.py::EXPECTATION_REGISTRY`
     evaluator(checked/enabled/value/text/visible/window_open/
     window_closed/control_exists 等既有能力全部复用)按**真实观测
     事实**求值 → 真实 ExpectationResult(含本次注册 observation_id)。
  2) `_observation_facts`:evaluator 输入从**真实观测注册记录**
     构建 — windows=observe_product_windows 真实 path-owned 注册
     窗口清单(支撑 window_open/closed 正负断言);controls=观测
     投影(automation_id/text/control_id/enabled/checked/value);
     observation_id=本次注册 id。native 身份不进断言事实。
  3) **动作步**:真实 dispatch 恰一次 → 动作后权威 observe →
     declared.verifiers 逐条真实求值 → 严格 pass 才推进;
     fail→FAILED 锁存(后续 0 输入)、unknown/异常→BLOCKED 锁存;
     receipt.ok 不再合成 action_receipt pass 替代业务断言(assertion
     refs 为真实求值结果,type/result/expected/actual/observationRef
     逐条持久)。无声明 verifier 的动作步保持原窗口效果推进语义。
  4) **assertion-only 步真正求值**:action_id=None + verifiers →
     聚焦观测(=无动作正确时点)真实求值;0 业务输入;true→严格
     pass 推进;false→fail 阻断下一步;unsupported 占位移除。
  5) 类型缺能力:映射 refused/registry 缺 evaluator/求值异常 →
     明确 failed ExpectationResult(诊断列明),不 silent pass。
  6) target/windowing/runtime.py 最小改动(+8 行):归属探针优先
     会话后端 exe_path_for_pid 缝隙(与 WindowBindingFacade
     _product_owned 同一 seam 契约),observe_product_windows 在
     fake OS/无 psutil 环境可真实枚举。
- **正负对照(全部真实链,仅 OS fake)**:checked 声明 true/OS false
  → 1 输入 + durable fail + 下一步 0 输入;true/OS true → 2 输入
  两 pass、断言引用含真实 OBS 注册 id;无关回执断言数量对齐不产生
  假 pass(求值按 type/expected);after 观测 unknown → 断言不可
  pass(1 输入 unknown 锁存);assertion-only true→0 输入 pass 推进、
  false→0 输入 fail 阻断。诚实记录:真实 compiler 产物自动携带的
  window_open 断言(步骤 selector window 的 title_regex 与被断言
  窗口 title 冲突)按既有映射语义求值为 fail — 严格门如实暴露,
  不合成 pass;窗口序列/派发链 fixture 明确选择 clear_verifiers
  (那些用例关注窗口序列;断言语义由 checked 用例覆盖)。
- **1316 探针复跑**:before_unknown inputs 2→**0**/latched/unknown;
  after_stale_observation 保持 1/unknown;normal_a_b_a 现按其编译
  自带 window_open 断言真实求值为 fail(见上,严格门语义)— 窗口
  顺序/派发语义由适配器套件(clear fixture)独立验证保持
  (A→B→A 3 输入 logo1/Log Center/logo1、revision [1,2,1])。
- **门槛(完整结果,自然完成)**:
  适配器文件 → **33 passed**;
  `python3 -m pytest -q test_case/test_recording -p no:cacheprovider`
  → **1732 passed, 1 skipped, 1 deselected**(126.56s);
  `python3 -m pytest -q -p no:cacheprovider`
  → **3302 passed, 1 skipped, 614 deselected**(152.56s);
  显式十一文件清 addopts(1129 同集合)→ **232 passed**(7.70s);
  git diff --check 通过;tracked diff 25 文件(+7 行相对 1129 基线,
  全部 execution_ledger/runtime/test 文件)。
  HEAD c4ac5442b71e83191881b098c70a1e8901489f48;SHA256:
  execution_ledger 3b12300e…,runtime f19660c0…,
  test_v3_real_server_adapter 58157fd2…。
- **仍未通过、保留未宣称(固定收尾出口)**:可信截图/evidenceRefs
  (BM 反例未修);S4 差分/drag 正负矩阵;S5 驱动调用同一真实链
  (仍手造 success/passed);获授权 Windows 三轮;region_query/
  visual_evidence 等声明类型走既有 evaluator(缺观测协议输入时
  明确 fail,不冒充);公共 v3 执行/导出门保持关闭;legacy/已过
  切片/WIP 原件不变。
- **停等本片独立复验;未 commit**。

## 2026-11-04 Message182:window_open/closed 目标映射修复(1356)

- **复验(1356)认可**:复用 mapper/evaluator 真实求值(checked
  true/true 2 输入 pass、true/false 1 输入 fail 阻断、assertion-only
  false 0 输入 fail)、before_unknown 0 输入锁存、after_stale 保持。
  本轮仅修 window_open/closed 目标条件映射缺陷 + 恢复保留全部
  verifiers 的未修改 compiler A→B→A 正向链。
- **缺陷定位(监督独立对照)**:replay._verifier_expectation 的
  window_open 分支先取步骤 selector.window 的 title_regex,再加
  expected.title → 生成互斥 AND 条件(目标身份映射错误);同时
  exists=false 且目标实际仍在时错误 pass(反向假通过)。"既有代码
  预存"不豁免 — 原需求就是真实录制产物正确回放断言。
- **最小修复(agent/recording/replay.py window_open 分支,+39 行
  含既有 WIP)**:expected 显式 title/titleRegex 指定目标时,目标
  身份由 expected 决定 — 不残留源窗口互斥标题约束;processName
  显式覆盖保留(expected 未给时继承源步骤窗口进程);未指定目标的
  legacy 本窗行为保持(selector 自身约束)。不改 evaluator/不放宽
  产品域/不删编译 verifier/不手造 pass/不新建循环。
- **恢复正向链(保留全部 verifiers)**:未修改原 compiler A→B→A
  ready 产物 → 实际窗口顺序 logo1/Log Center/logo1、3 输入、
  previous 链、每步真实断言(window_open Log Center pass、
  window_closed Log Center pass)、本次观测引用 — 全部通过。
- **发现并如实处理的真实语义**:step-0002 的动作(点击 logRow)关闭
  本步绑定窗口(W2)自身 → 动作后重聚焦按 §7.4 LOST 拒绝。适配器
  新增 LOST 处理:binding 标记 lost(不复活/不重试),窗口存在性
  断言按动作后权威注册清单(observe_product_windows)求值,控件级
  断言事实不可得明确 unknown(不冒充);断言结果决定推进(pass 不
  阻断录制声明的后续窗口步骤,fail/unknown 锁存)。fake OS 同步补
  动作响应窗口状态(点击 logRow → Log Center 消失 — 贴近真实动作
  效果;gui.click 的 automation_id 声明控件身份现随 action_args 到
  达真实 dispatcher/backed)。
- **对照矩阵(全部真实链仅 OS fake)**:四格(目标存在/不存在 ×
  exists true/false:pass/fail/pass/fail);显式 title 不残留源
  regex;显式 titleRegex 不残留源 title;未指定目标 legacy 保留
  selector 约束;closed_but_present → 1 输入 fail 阻断(原 2 输入
  假通过修复);window_open 存在性不替代 appeared 差分(未宣称 S4)。
- **1356 探针复跑**:compiler_a_b_a_unmodified inputs 1→**3**、
  durable [pass,pass,pass];window_closed_but_present inputs 2→**1**、
  首步 fail 阻断;checked/assertion-only 已过点保持。
- **门槛(完整结果,自然完成)**:
  适配器文件 → **37 passed**;
  `python3 -m pytest -q test_case/test_recording -p no:cacheprovider`
  → **1736 passed, 1 skipped, 1 deselected**(126.13s);
  `python3 -m pytest -q -p no:cacheprovider`
  → **3306 passed, 1 skipped, 614 deselected**(151.94s);
  显式十一文件清 addopts → **236 passed**(7.90s);
  v1/v2 回放回归(test_action_replay_consistency + r3 + assembly
  residue)→ **16 passed**;
  git diff --check 通过。HEAD c4ac5442…;SHA256:
  replay 787aa954…,execution_ledger 8b05db65…,
  test_v3_real_server_adapter 716d78a2…。
- **仍未通过、保留未宣称(固定收尾出口)**:可信截图/evidenceRefs
  (BM 反例未修);S4 appeared/absent 差分、drag 正负矩阵(window
  存在性不是差分);S5 驱动接同一真实链(仍手造);获授权 Windows
  三轮;公共 v3 执行/导出门保持关闭;legacy/已过切片/WIP 原件不变。
- **停等本片独立复验;未 commit**。

## 2026-11-04 Message184:LOST 事实边界组修复(1426)

- **用户最新口径(优先于旧"带图"文)**:简洁特征识别报告,每
  attempt 本步单份、下一步 previous 引用上一份(不复制整块);
  报告默认顺序展示识别特征/匹配、动作、断言结果;失败处按需展开
  前一步与转换依据;图片按需引用。取消强制逐步截图/重型可信图片
  系统作为硬收尾门。特征/观测仍须真实,unknown 阻断/append-only/
  重建/脱敏不变;后续报告投影按此口径最小去重(不重写账本/执行器)。
- **复验(1426)认可**:mapper 切片(未修改 compiler A→B→A 保留全部
  verifiers 输入 3 三 pass;closed_but_present 1 输入 fail 阻断)、
  checked/assertion-only 已过点。
- **本轮仅收敛 LOST 事实边界组(失败回归先行,新增 5 红)**:
  1) **inventory 三态**(缺口1):`_read_product_inventory` — 成功
     非空/成功空/**失败**显式区分;负断言只依据成功且覆盖充分的
     本次事实;读取失败(ok=false/异常)不是"目标不存在" → 事实
     unknown,到 W2 业务恰 2、durable/report unknown 锁存,第三步
     0 focus/输入(探针 3→2 输入)。
  2) **真实引用不伪造**(缺口2):LOST after 引用不再拼
     OBS-64hex(长 68)冒充观测注册;成功读取 → `INV-<16hex>`
     稳定脱敏引用(记录在本 attempt `evidenceRefs` — 既有账本
     接口,run/attempt 可追溯;不与 OBS-32hex 注册 id 混淆);
     无事实 → 诚实 unknown。hook.complete_after_step 新增
     evidence_refs 透传(既有 complete_attempt 参数)。
  3) **空声明不偷换关闭**(缺口3):只去掉 verifiers(合法模型)
     → 无显式可验证关闭/absent 声明 → unknown 锁存(探针 3→2
     输入);只有显式声明且清单事实充分才按契约继续;LOST 保持
     (不复活/不重试/不找兄弟)。
  4) **非 required 同门**:首步/中间 nonrequired 未知仍阻断
     (required=True/False 双对照)。
  5) **正常对照保持**:显式声明关闭 + 成功非空清单 → A→B→A 三
     pass 继续(after_rev [1,lost,1]);failure code 区分
     bound_window_lost_after_action(LOST)与
     post_action_inventory_unavailable(读失败)。
- **fixture/测试适配(同链)**:clear-variant a_b_a(无声明
  verifier)按新语义在 LOST 步 unknown 锁存(continue 语义由保留
  verifiers 正向链覆盖);同窗/正向链观测计数更新;引用断言接受
  unknown/OBS-36/INV-20 三态;ref_not_hash_forgery 断言 INV 在
  evidenceRefs 可追溯且非 LOST 步仍 OBS-36。
- **1426 探针复跑**:normal_declared_close 3 输入三 pass(保持);
  lost_inventory_unavailable 3→**2** 输入、unknown 锁存、
  afterRef unknown;lost_without_close_assertion 3→**2** 输入、
  unknown 锁存。
- **门槛(完整结果,自然完成)**:
  适配器文件 → **42 passed**;
  `python3 -m pytest -q test_case/test_recording -p no:cacheprovider`
  → **1741 passed, 1 skipped, 1 deselected**(127.88s);
  `python3 -m pytest -q -p no:cacheprovider`
  → **3311 passed, 1 skipped, 614 deselected**(151.89s);
  显式十一文件清 addopts → **241 passed**(7.87s);
  git diff --check 通过;replay.py 未再改动(1356 mapper 修复保持
  SHA 787aa954…)。HEAD c4ac5442…;SHA256:execution_ledger
  db84bee8…,test_v3_real_server_adapter decb28a2…。
- **后续(按用户精简口径与固定四出口)**:报告投影按特征识别/
  previous 引用去重(最小改,不重写账本);S4 appeared/absent 差分
  与 drag;S5 驱动接同一真实链;获授权 Windows 三轮。公共 v3 门
  保持关闭;legacy/已过切片/WIP 原件不变。
- **停等本片独立复验;未 commit**。

## 2026-11-04 Message186:统一三态/本步单份特征/精简报告(1456)

- **复验(1456)认可**:1426 两 LOST 门与正常显式关闭对照保持。
- **失败回归先行(新增 4 红)**:普通路径负断言清单读失败、四格
  (成功空/非空 × 正/负断言)、证据记录 payload/归属、精简报告。
- **P1-1 统一三态入口(普通+LOST 同一 reader)**:`_read_product_
  inventory` 成为唯一动作后清单事实入口(旧 `_product_window_facts`
  的 [] 回退不再供断言消费);`_evaluate_declared_verifiers` 新增
  `inventory_read` 门 — 清单读失败时 window_open/window_closed 断言
  明确 BLOCKED(事实缺失,不可判,不偷换"不存在");成功空清单
  可判"不存在"(f_absent cell)。探针:首步负断言 pass→**blocked**、
  业务 2→**1**、unknown 锁存。cells:成功空+false→pass、成功非空+
  false→fail、成功非空+true→pass、成功空+true→fail。
- **P1-2 本步单份持久特征**:`_inventory_evidence_record` — 复用
  既有 append-only evidenceRefs 接口,每 attempt 实际读取并用于
  判断的必要脱敏特征只保存一份:ref/kind/readStatus/coverage/
  **windows payload**(断言相关脱敏特征:title/process_name/role/
  visible/root_control_id — native handle/pid 不落,递归脱敏统一
  拒绝)+ runId/stepId/attemptId/windowId/phase/capturedAtMonotonic。
  INV-<16hex> 是内容标识;本次来源关联由记录承载,重启可从落盘
  evidenceRefs 还原实际断言事实(测试断言 record 含 payload/归属/
  runId/attemptId 一致)。其他字段只引用该份。
- **P1-3 精简报告投影(build_run_report 最小改)**:steps 每 attempt
  一个规范本步条目(features=识别特征/匹配结果逐条;action=
  receiptRef/code/ok;stateAfter=revision/observationRef;
  evidenceRefs);previous 只含稳定引用(runId/stepId/attemptId/
  eventId),不嵌套复制前步断言/状态块;首步 previous None;pass
  报告每步可查;无图不是失败(不生成占位图/不强制 evidence_missing
  当缺陷);可选截图传入仍经来源分类(不虚假来源)。旧调用者兼容:
  failedStep 保持详细追溯(委托既有 build_failure_trace,身份/证据
  分类不重写),新增 failedStepRef 关联精简条目 — 无第三份复制。
- **样例**:真实主链 synthetic 样例报告
  `result-report/cross-window-supervision/sample-concise-report.json`
  (未修改 compiler A→B→A 保留全部 verifiers 的真实运行报告;
  非 hand-crafted)。
- **门槛(完整结果,自然完成)**:
  适配器文件 → **46 passed**;legacy 报告消费者(s6 report/execution/
  boundary)同套件 73 passed;
  `python3 -m pytest -q test_case/test_recording -p no:cacheprovider`
  → **1745 passed, 1 skipped, 1 deselected**(128.04s);
  `python3 -m pytest -q -p no:cacheprovider`
  → **3315 passed, 1 skipped, 614 deselected**(151.98s);
  显式十一文件清 addopts → **245 passed**(7.99s);
  git diff --check 通过。HEAD c4ac5442…;SHA256:execution_ledger
  f1c676a0…,test_v3_real_server_adapter 721fda13…。
- **1456 探针复跑**:explicit_failed_inventory_reads=1、
  fake_os_inputs=1、durable [unknown]、首步负断言 **blocked**、
  report unknown(原 2 输入/首步 pass 修复)。
- **后续(按用户精简口径与固定四出口)**:S4 appeared/absent 差分与
  drag;S5 驱动接同一真实链;获授权 Windows 三轮;可选图片按需引用
  (不虚假来源)。公共 v3 门保持关闭;legacy/已过切片/WIP 原件不变。
- **停等本片独立复验;未 commit**。

## 2026-11-04 Message188:用户"全部CC完成"授权 — A 三项报告收尾(1536)

- **授权变更**:用户明确"别让zcode搞了,全部cc搞完" — 撤销 Zcode
  分工等待,全部剩余本地实现由本原会话按 A→B→C 顺序接续;监督仍
  独立验收;每功能片完成停等复验。"全部CC完成"不授权 Windows 运维。
- **186 验收(1536)认可**:普通路径故障读 1 输入/unknown、保留全部
  compiler verifier A→B→A 3 输入/pass、steps previous-ref、LOST
  payload;两门 1745/3315 + 显式 245。
- **失败回归先行(新增 3 红)**:默认失败报告重复/占位;普通步骤
  清单事实未持久([false,true,false]);无断言步特征空([1,1,0])。
- **A-1 默认报告去重**:`build_run_report` 默认 `failedStep` 为
  失败步**单份规范条目**(与 steps 同份语义;previous 仅 runId/
  stepId/attemptId/eventId 引用;无缺图 evidence_missing 占位);
  旧详细追溯(build_failure_trace 身份/证据分类)仅
  `failed_step_detail=True` 显式按需投影(legacy 消费者迁移到
  显式参数,兼容路径即设计定义);`failedStepRef` 指向账本事件
  (按需解析同一 attempt 条目);`failedStepDetailRequested` 标记。
- **A-2 普通步骤清单事实持久**:实际读取并用于窗口断言的清单按
  本 attempt 持久一份(与 LOST 同一接口/形状:_inventory_evidence_
  record — ref/kind/readStatus/coverage/windows payload/归属/时点);
  A→B→A payload 分布 [true,true,true];其他地方只引用;重启从
  evidenceRefs 还原一致;写盘失败门不变。
- **A-3 无断言步识别特征/动作标识**:hook.stateAfterRef 支持
  targetRef(automationId/uniqueMatch/selectorHint)+actionId
  附挂(递归脱敏统一校验;非复制控件树、非断言列表改名);报告
  features 在无 assertionResultRefs 时投影 resolved-target 特征
  (含 observationRef 时点);action 摘要增加实际 action_id
  (dispatcher receipt 产物)/参数在账本 receiptPayload 可查。
  A→B→A 第三步 features 含 btnBack/uniqueMatch=1、actionId=gui.click。
- **样例(真实主链 synthetic,非手造)**:
  `result-report/cross-window-supervision/sample-concise-report.json`
  (三 pass)与 `sample-concise-report-failed.json`(第二步 checked
  失败:failedStep previous 仅引用键,无嵌套前步整块)。
- **门槛(完整结果,自然完成)**:
  适配器文件 → **49 passed**;legacy 消费者迁移后 27 passed;
  `python3 -m pytest -q test_case/test_recording -p no:cacheprovider`
  → **1748 passed, 1 skipped, 1 deselected**(127.59s);
  `python3 -m pytest -q -p no:cacheprovider`
  → **3318 passed, 1 skipped, 614 deselected**(153.00s);
  显式十一文件清 addopts → **248 passed**(8.31s);
  git diff --check 通过。HEAD c4ac5442…;SHA256:execution_ledger
  0fe745cc…,test_v3_real_server_adapter bff04c5d…。
- **最小影响函数**:execution_ledger.py 的 build_run_report(投影/
  failed_step_detail 参数)、hook.complete_after_step(stateAfterRef
  targetRef/actionId 附挂)、真实适配器两处 complete 调用(evidence
  refs/target facts);test_s6 三个 legacy 文件显式 detail 参数迁移;
  测试文件新增 3 用例。账本/执行器/报告视图核心未重写。
- **B/C 排队(本会话顺序接续,每片停等复验)**:B=S4 appeared/absent
  动作前基线与后差分、业务独立评估、scroll/drag 双端语义与正负断言、
  显式 cleanup;C=S5 真实录制→停止收集→compile→本主链 replay→业务
  断言→精简报告(替代手造 receipt/passed)+完整矩阵+兼容回归+获授权
  Windows 三轮。公共 v3 门保持关闭;legacy/已过切片/WIP 不变。
- **停等 A 片独立复验;未 commit**。

## 2026-11-04 Message190:B 片 — S4 差分/双端动作/显式 cleanup 接真实主链

- **A 片锁定不重开(1611 复验通过)**:features [2,1,1]、inventory
  payload [true,true,true]、previous 仅 ref、failedStepRef、实际
  action_id、旧详细显式投影 — 全部认可;两门 1748/3315 + 显式 248。
- **B-1 appeared/absent 接真链(09§7.3)**:真实 compiler 产物
  (transition 显式断言 → appeared/absent effects 携带于
  windowSequence.steps[].effects;status ready)驱动唯一主链:
  - dispatch 前:**同租约内** `facade.begin_effect_baseline`(按
    **效果目标身份**记录基线,含候选可见性;只枚举,不聚焦/激活);
  - 动作恰一次 dispatch → receipt 确定后、业务断言前:只读有界
    轮询(0.05s 间隔,deadline=声明 timeoutSeconds)逐 effect 调
    `observe_effect_after_action`;**多个效果全部满足才推进**;
    零候选到期 timeout/多候选 ambiguous/异常 → 一律不假 pass →
    unknown 锁存(declared_window_effect_not_satisfied,拒绝码
    持久;ambiguous 明细在 stage 步骤摘要);
  - 效果结果持久进本步 stateAfterRef.windowEffect(正常与 LOST
    路径均持久;native 以 appearedRef=WIN-<序> 引用表达 — §7.6
    原生身份不进持久状态);可重建/追上步;
  - **facade 最小精确修复**(09§7.3 "从不可见变可见"):
    begin_effect_baseline 基线记录候选可见性;observe_effect_after_
    action 差分 = 新候选 ∪ 基线隐形候选变可见;absent 同理含
    可见→不可见;已可见同名旧窗口不满足 appeared(基线天然排除)。
- **B-2 延迟绑定(origin 策略)**:`observed_during_flow` 的 W 在
  begin 时**不预绑定**(预绑定对无观测证据窗口拒绝 — 正确安全
  门);其绑定由 appeared 效果成立时的唯一有效增量完成,或下一步
  开始时经同一真实 bind 门(§7.4:绑定前 tracker sync 消费事件批
  — binding.py 最小 +4 行);HIDDEN 同实例 show/same_instance_
  shown 与 destroy/create 新 W 区分由跟踪 create/destroy 事实承载;
  已可见兄弟不顶替(基线差分 + binding 门)。
- **B-3 严格顺序(§7.3)**:先 receipt + 只读效果,再按各业务断言
  自己的窗口归属取观测;源窗口关闭/LOST 沿已验三态门(不复活/
  不找兄弟);window effect pass 但业务 fail 仍停(已验断言独立)。
- **B-4 scroll/drag 双端(10 R1)**:drag 的 selector/endSelector 在
  本次观测下经既有 resolve_replay_selector/HybridLocator 唯一解析
  (双端中心坐标 → drag(x1,y1,x2,y2);不用录制 screen XY;端点
  不可唯一解析 → 零越权输入拒绝,rejectionCode 持久);scroll 经
  统一 dispatcher(window_lock 门)恰一次;业务/scroll/drag 结果
  verifier 独立于 window effects;v1/v2/单窗/语义视觉默认不动
  (legacy 回放 16 用例回归通过)。
- **B-5 显式 cleanup(09§7.5)**:golden.cleanup 声明步骤进入同一
  声明表;主流程未全 pass 时 finally 编排:逐 cleanup 步真实
  rebind 校验(LOST/消失 → skip+原因)、两步契约(权威锁+本窗观测
  → target_ref 派发)、结果带明确 stepId/outcome/reason;不可验证
  skip 不猜动作;结束释放绑定(1956 隔离不回退)。
- **fixture 事实层**:fake OS 动作响应(btnApply→W2 出现、logRow→
  W2 关闭)与动作响应生命周期事件流(drain 对照当前可见性差分
  emit create/destroy — 强实例事实);每控件独立 rectangle。
- **矩阵证据(全部真实链仅 OS fake)**:正链(3 输入/窗口序
  logo1→Log Center→logo1/三 pass/appeared+absent 持久);双增量
  ambiguous 拒绝;absent 到期未发生锁存(第三步 0 输入);窗口
  效果 pass+业务 fail 仍停;drag 双端解析成功+端点歧义 0 输入;
  scroll 恰一次;cleanup 运行/skip+原因。
- **门槛(完整结果,自然完成)**:
  适配器文件 → **57 passed**;
  `python3 -m pytest -q test_case/test_recording -p no:cacheprovider`
  → **1756 passed, 1 skipped, 1 deselected**(138.03s);
  `python3 -m pytest -q -p no:cacheprovider`
  → **3326 passed, 1 skipped, 614 deselected**(162.27s);
  显式十一文件清 addopts → **256 passed**(18.56s);
  git diff --check 通过;tracked diff 25 文件(+18 行相对 1536)。
  HEAD c4ac5442…;SHA256:execution_ledger c2ebb35d…,
  binding a2f0185c…,test_v3_real_server_adapter 147dc2d6…。
- **最小影响函数**:execution_ledger.py(真实适配器 effects 基线/
  评估/持久化 + 延迟绑定 + cleanup 编排 + cleanup 声明表;报告
  windowEffect 投影)、binding.py(基线可见性 + 差分 8 行 +
  绑定前 tracker sync 4 行)、测试文件(7 个新用例 + fixture 动作
  响应事实层)。无第三执行循环;既有 helper 全部委托复用。
- **停等 B 片独立复验;未 commit**。C(S5 真实链闭环)按序排队。

## 2026-11-04 Message192:B 片四组边界收口(1721)

- **B 片验收(1721)部分认可**:真实初始 W2 不存在→首动作创建→
  3 输入/pass 正链与 A 片保持;四组 P1/P2 边界未过,本轮收口。
- **失败回归先行(新增 5 红)**:absent present 假成功、absent 确切
  绑定实例、cleanup 预写门+持久性、scroll 坐标直通、唯一增量消费。
- **P1-1 效果终态严格匹配**:效果汇总按声明 kind 精确匹配语义
  终态(appeared/same_instance_shown/absent);present/timeout/
  error/ambiguous 一律不成功(工具 ok 只是读取成功);失败持久
  必要本次事实(stateAfterRef.windowEffect + 拒绝码,不只 stage
  临时摘要)。absent 针对绑定的确切实例(facade same-instance
  路径要求 binding native;兄弟消失不顶替 — 测试锁定)。
- **P1-2 cleanup 同一账本生命周期**:cleanup 步经同一 hook
  begin_before_preparation(预写失败 OSError 上抛 → 0 后续任何
  GUI,含 cleanup)/complete_after_step 持久(run/step/attempt/
  receipt/outcome;§7.6 独立授权序列首步 previous 显式空;主流程
  fail 是运行 cleanup 的前提不是被绕过的阻断);真实 selector
  唯一解析(同主步 resolve_replay_selector,不再按首个
  automationId 挑);动作后异常 unknown 不吞成 skipped;主流程
  fail 仍允许既有政策授权且当前 binding 可验证的 cleanup。
- **P1-3 scroll 坐标物化**:pointer.scroll args 的 x/y 不再直通
  OS — 本次唯一已解析目标物化(中心);无解析 → 受控拒绝。探针
  99123/99234 → 实际 90/50(观测内语义目标)。
- **P2 增量绑定消费 + 不透明引用**:facade.bind_increment —
  appeared 唯一增量在同租约强实例边界绑定确切 native(observe+
  sync_and_generation 校验;bound_pid 从本 run 跟踪事实取;共享
  实例/身份声明门保持,不放宽清单/不挑第一项);adapter 经
  effect 汇总的不透明 instanceRef(INST-序,run 内存映射 →
  native)消费;appearedRef=WIN-{native} 移除(不再编码原值)。
  隔离探针:唯一增量被消费(第二步无 window_not_bound);
  完整 verifiers 同身份场景如实清单歧义 unknown(非完整正向)。
  fake stream 可见性 emit create/destroy 仅为 OS 事实层;HIDE/
  SHOW 与 destroy/create 各自事实正负例后续以生产事件语义验证
  (fake 不宣称生产覆盖)。
- **1721 探针复跑**:effect_positive 3 输入三 pass(保持);
  absent_still_visible 3→**2** 输入 [pass,unknown];prewrite_
  failure_with_cleanup 1→**0** 输入(OSError 前零 GUI);
  cleanup_durability cleanup_durable 0→**1**(持久账本行);
  scroll 99123/99234→**90/50**;unique_increment_binding_isolation
  1→**2** 输入(增量消费);old_visible_plus_unique_new 完整
  verifiers 清单歧义 unknown(如实)。
- **门槛(完整结果,自然完成)**:
  适配器文件 → **62 passed**;
  `python3 -m pytest -q test_case/test_recording -p no:cacheprovider`
  → **1761 passed, 1 skipped, 1 deselected**(157.42s);
  `python3 -m pytest -q -p no:cacheprovider`
  → **3331 passed, 1 skipped, 614 deselected**(182.90s);
  显式十一文件+s4_window_effects 清 addopts → **268 passed**
  (38.86s);4 legacy 回放 → **16 passed**(20.25s);
  git diff --check 通过。HEAD c4ac5442…;SHA256:execution_ledger
  f2deb9b9…,binding 5d6e2339…,test_v3_real_server_adapter
  7ed697c8…。
- **最小影响**:execution_ledger.py(效果汇总严格终态匹配/失败
  持久/instanceRef/延迟绑定消费改 bind_increment/cleanup 编排
  重写为同一账本生命周期 + declared 表含 cleanup);binding.py
  (bind_increment 新方法 + 绑定前 tracker sync);测试文件
  (5 个新用例 + cleanup fixture 修正)。无第三执行器。
- **停等 B 收口独立复验;未 commit**。C 排队(S5 真实链闭环)。

## 2026-11-04 Message194:B 片两组合同遗漏收口(1823)

- **1823 认可并锁定**:正链/初始 W2 创建/present≠absent/scroll 90,50/
  INST 不透明/增量隔离/业务矩阵(scroll/drag 改变与不变正负)/A 片。
- **失败回归先行(新增 5 红,镜像独立探针)**:FailFirstBegin/
  FailFirstComplete(瞬时,随后恢复)/cleanup_false_verifier/
  bound-absent 真兄弟消失/bind_increment 边界(错误 native/无效
  token/正例)。
- **P1-1a 存储故障锁存(不可恢复)**:attempt ledger 新增
  `_storage_failed` 锁存 + `_assert_storage_gate` — 任一次预写/
  动作后写失败即置位;瞬时磁盘恢复**不清除边界**(锁存后的
  begin/complete 抛确定边界错误,非透传原错);cleanup 编排捕获
  OSError 后上抛,GUI 前停止。探针:FailFirstBegin 0 输入/
  FailFirstComplete 1 输入后 0(不确定边界不可清除)。
- **P1-1b cleanup 真断言/真实 provenance**:cleanup 声明 verifiers
  经同一 `_evaluate_declared_verifiers` 真实求值(OS false → 断言
  failed、cleanup fail;receipt ok 不代替业务断言);真实权威
  before/after 附挂(observe observation + step_context 真实
  binding revision — 非全 pending);previous 稳定关联真实触发
  失败的主流程 attempt(仅引用;ledger begin_attempt 新增
  authorized_cleanup 关系参数 — §7.5 主流程 fail 是运行授权
  cleanup 的前提而非被绕过的阻断;跨 run 拒绝保持;§7.6 序列
  推进门对主流程不变)。探针:cleanup fail + assertion refs
  (control_checked_equals/failed/expected true/actual false)+
  previous=step-0001 + revision 1 + OBS 引用。
- **P1-2a absent 确切实例(真实兄弟消失)**:facade absent 判定改
  为绑定目标自身消失(从枚举移除或变为不可见;bound_native 仍
  可见时兄弟消失不成立,present+bound_native_still_present)。
  探针:3→**2** 输入、[pass,unknown]、absent satisfied=false。
  独立 exact_bound_instance 测试改为 W2 观测时注入(真实时序)。
- **P2 bind_increment 完整旧门**:调用方 run_token 与 facade 租约
  校验(不匹配 window_binding_lease_refused);完整身份校验
  (root/titleRegex/process 逐项;不匹配 identity_mismatch);
  **产品路径归属**(self._product_owned — 增量不豁免);活性可见
  (不可见 hidden 拒绝);共享实例(候选 native 已绑定 → shared);
  tracker 强实例证据(observe+sync)。探针:wrong_identity →
  window_binding_shared、wrong_run_token → window_binding_lease_
  refused、binding_created=false(正例:真实 token+native 绑定成功)。
- **门槛(完整结果,自然完成)**:
  适配器文件 → **67 passed**;
  `python3 -m pytest -q test_case/test_recording -p no:cacheprovider`
  → **1766 passed, 1 skipped, 1 deselected**(168.50s);
  `python3 -m pytest -q -p no:cacheprovider`
  → **3336 passed, 1 skipped, 614 deselected**(192.18s);
  显式十二文件清 addopts → **273 passed**(48.96s);
  5 legacy 文件清 addopts → **32 passed**(20.45s);
  git diff --check 通过。HEAD c4ac5442…;SHA256:execution_ledger
  61a14047…,binding 73a422d0…,attempt_ledger 84b5e982…,
  test_v3_real_server_adapter 855c94d5…。
- **最小影响**:execution_ledger.py(cleanup 编排同一账本生命周期/
  真断言/provenance;hook previous_override/authorized_cleanup
  传递;effect 汇总 instanceRef);binding.py(absent 确切实例判定
  + bind_increment 完整门);attempt_ledger.py(存储故障锁存 +
  authorized_cleanup 关系参数)。无第三执行器;hook 私有字段清除
  已改为主流程 attempt 关联捕获 + 授权序列重置(不再抹掉真实
  失败前步);测试不再放宽 previous 断言(真实关联断言已加)。
- **停等 B 收口独立复验;未 commit**。C 排队。

## 2026-11-04 Message196:B 片剩余四缺口收口(2013)

- **1823 认可锁定**:bound-absent/bind_increment 边界/业务矩阵
  (scroll/drag 改变与不变)/A 片;本轮只收四缺口。
- **失败回归先行(新增 4 红)**:真实 _write_event IO 异常锁存、
  cleanup 动作后观测两格、previous 链、机械回归。
- **P1-1 真实写盘异常锁存(2013 缺口1)**:attempt ledger
  `_write_event` 的 OSError 路径 + begin/complete 调用方捕获均置位
  `_storage_failed` — **子类覆写 _write_event 的注入同样捕获**
  (顶层调用方 except OSError);探针 write_started_failure:
  inputs 1→**0**、storage_latched false→**True**;磁盘恢复不清除
  边界;保留 started→unknown 重建/正常 end 释放。
- **P1-2 cleanup 动作后观测(2013 缺口2)**:cleanup 流程重排为
  主步同一语义 — 权威 pre 附挂(**写入成功在动作前**)→ 单次
  action → **动作后新观测**(observe 新注册;before/after
  observationRef 必须不同)→ 三态清单(inventory_read 含 ok/空/
  失败)+ declared.effects 同义求值(传 _evaluate_declared_verifiers)
  → 真实 verifiers 持久结果。探针两格:false→true 断言 pass、
  true→false 断言 fail(pre 不冒充 post;before=after→**False**);
  revision 异常回 pending 不再"仍继续假 pass"。
- **P2 previous 真实链(2013 缺口3)**:actual previous — 第一条
  cleanup 关联触发失败的主流程 attempt;后续 cleanup 的实际前一步
  是上一条 cleanup(hook._previous 链);transition 显式传递修复
  (`transition or transition` 无效式移除,caller cleanup_after_
  failure 生效);探针 two_cleanups:cleanup-0002 prev=
  **cleanup-0001**/transition=cleanup_after_failure。
- **机械回归(2013 缺口4)**:LedgeredDispatch.begin_attempt_for
  误带 previous_override/authorized_cleanup 未定义变量 → 恢复原
  参数语义;探针 exported_ledger_dispatch_begin error=null。
- **门槛(完整结果,自然完成)**:
  适配器文件 → **71 passed**;
  `python3 -m pytest -q test_case/test_recording -p no:cacheprovider`
  → **1770 passed, 1 skipped, 1 deselected**(169.76s);
  `python3 -m pytest -q -p no:cacheprovider`
  → **3340 passed, 1 skipped, 614 deselected**(192.13s);
  显式十二文件清 addopts → **277 passed**(48.86s);
  5 legacy 清 addopts → **32 passed**(20.44s);
  git diff --check 通过。HEAD c4ac5442…;SHA256:execution_ledger
  442bac4c…,attempt_ledger 4d138c52…,
  test_v3_real_server_adapter 67bca47e…(binding 未再动)。
- **最小影响**:attempt_ledger.py(_write_event OSError 锁存 +
  begin/complete 调用方捕获;transition 参数显式);execution_ledger
  .py(cleanup 流程重排 pre 附挂/post 观测/三态+effects;
  actual previous 链;begin transition 显式;begin_attempt_for
  参数恢复);测试文件(4 新用例 + fixture typo 修正)。
- **停等 B 收口独立复验;未 commit**。C 排队(S5 真实链闭环)。

## 2026-11-05 Message198:B 尾项已修 + C 真实阶段驱动(进行中,如实)

- **B 尾项修复完成(全部先红后绿,适配器 74→81 passed)**:
  1) cleanup.effects 同义求值(2154 尾项1):dispatch 前同租约
     begin_effect_baseline(按效果目标身份)→ receipt 确定后只读
     有界轮询逐 effect 评估(同主步严格终态匹配门)→ 不满足 →
     cleanup unknown 锁存(windowEffect 持久);positive 真实 rebind
     拒绝(自身窗口关闭)→ skipped 如实。
  2) cleanup pre-state 真落盘(2154 尾项2):attach_state_before 在
     **dispatch 前**调用(0739 append-only;写失败 → OSError → 存储
     锁存全 run 0 GUI);派发前落盘的 before 记录含非 pending
     revision + OBS 引用(探针断言事件原件)。
  3) previous 引用补 windowId(2154 尾项3):trigger attempt 的
     windowId 进入 cleanup_previous_ref(稳定引用完整)。
- **C 真实阶段驱动(scripts/s5_acceptance_round.py 重写)**:
  - recording/stop-collection:真实 RecordingService(session start →
    OS-synthetic capture 源经真实 correlator correlate → session.
    ingest;事件协议/归因/范围门真实)→ stop 持久 raw 文档
    (v1/v2 由真实归因事实决定 — 不手造);
  - compile:真实编译器 + 严格 raw reader;非 ready fail-closed;
  - replay:真实 execute_v3_window_stage_real(真实 server/policy/
    binding/dispatcher/账本;fake OS 仅枚举/连接/锁定/输入注入,
    与验收套件同契约);
  - business-assertions:真实 evaluator 求值结果(不合成);
  - traceable-report:A 片精简投影;无图不是失败;
  - fail-closed:任一必要阶段非 ran/latched/report 非 pass/断言
    非 pass → 非零退出;fresh round 目录不可覆盖;
  - --live:无本聊天具体目标授权 → 结构化拒绝(列明缺失),0 GUI。
- **进行中(如实,未宣称完成)**:driver round 的合成解析器投影
  (observe controls 空)尚有环境保真缺口 — 已有测试
  test_s5_round_driver 两用例编码了真实链验收(raw v2/golden
  ready/W1→W2/账本落盘/report pass)保持红,不放宽;下轮继续
  收口该投影与注入矩阵。
- **门槛(当前)**:录制 1771 passed + 2 failed(s5 driver 新链
  未收口)/1 skipped;全库同;适配器 81 passed。B 尾项修复未回退。
- **未 commit;停等下一轮继续 C 收口**(非独立验收点 — C 未完)。

## 2026-11-05 Message200(续):C 真实阶段驱动 — 当前真实状态(如实)

- **监督诊断采纳**:S5 脚本事件序号双重递增(1、3、5)是卡点;已修
  (每事件恰一次递增,不放宽录制校验)。本轮本地诊断/循环的教训
  已记录:区分"有进展"与"原地打转"。
- **B 尾项(全部先红后绿,已交付)**:cleanup.effects 同义求值
  (同主步 baseline/单次 action/只读差分/严格终态门;positive rebind
  拒绝如实 skipped);cleanup pre-state 真落盘(attach_state_before
  在 dispatch 前;失写锁存);previous 引用补 windowId。
- **C 重写(scripts/s5_acceptance_round.py 全量重建)**:
  recording/stop-collection(真实 RecordingService 会话 + OS 合成
  事件经真实 correlator/session.ingest + stop 持久 raw 文档)→
  compile(真实编译器;非 ready fail-closed)→ replay(真实
  execute_v3_window_stage_real)→ business-assertions(真实求值)→
  traceable-report(A 片精简投影)→ fail-closed 退出 + fresh 目录
  保护 + --live 结构化拒绝。
- **真实 round 当前状态(如实)**:recording/stop-collection/
  compile(ready)/replay/business-assertions/traceable-report 六
  阶段全 ran;真实产物(raw v2 3 事件/golden ready 2 步/账本事件/
  精简报告);step-0001 真实 pass(window_open Log Center 经清单
  真实求值);step-0002 经严格三态门 = bound_window_lost_after_
  action(自身绑定窗口在动作中关闭 — LOST,action 后窗口事实
  不可得)→ unknown → run 锁存(不重试 GUI)→ round 非零退出。
  这是真实链 fail-closed 行为(unknown 不称成功)——S5 轮的
  "全 pass 正链"在当前 flow 上未达成,原因是 A→B→A 的 B 步自身
  窗口消失属合法 unknown 边界;流转语义(关闭是否为该步声明
  效果)是下一轮的精确定义问题。
- **test_s5_round_driver 两用例保持红**(编码真实链验收:raw v2/
  golden ready/账本/report pass)—— 不放宽,如实作为 C 未完标记。
- **门槛(当前)**:录制 1771 passed + 2 failed(s5 driver 新链
  未收口);全库 3341 passed + 2 failed;显式十二文件 280 passed;
  4 legacy 16 passed;git diff --check 通过。B 尾项修复未回退
  (适配器 81 passed)。
- **未 commit;未宣称 B 整体通过或 C 完成**。下一轮:s5 驱动
  投影/流程语义收口(关闭效果语义),然后 C 矩阵/三轮。

## 2026-11-05 C 状态补充(会话末,如实)

- **driver round 当前行为(fresh dir 实测)**:六阶段全 ran;step-0001
  真实 pass(window_open 经清单求值、bindingRevision=1、targetRef
  附挂);step-0002 = bound_window_lost_after_action(logRow 点击
  关闭自身绑定窗口 → LOST 三态门)→ unknown → 锁存 → round 非零。
- **待收口(下一轮)**:关闭动作在 LOST 门下的效果语义(记录的
  A→B→A 中 Log Center 关闭应归属为该步可解析状态或下一转换的
  既有事实 — 依 09§7.3 "报告可保留窗口观测,但不靠后来出现推断
  动作成功"的对称语义精确定义);s5 驱动投影/注入矩阵收口后
  test_s5_round_driver 两用例转绿(不放宽)。
- **本轮已交付且锁定**:B 尾项三项(cleanup effects 同义求值/
  pre-state 真落盘/previous windowId)+ S5 驱动重写(真实链六阶段
  架构/真实 service/收集/编译/主链回放/断言/报告/fail-closed/
  fresh 目录/--live 拒绝)。
- **未 commit;B 整体与 C 均未宣称完成。**

## 2026-11-05 Message202:C 真实阶段驱动收口(0641 两红闭合)

- **0641 诊断采纳**:捕获源与回放 OS 语义不一致(driver 在 logRow
  时隐藏 W2,但 raw 只有 3 事件无关闭)— 修复为**捕获源录制完整
  A→B→A 流**(5 事件:click1→opened→click2(logRow)→closed→
  click3(btnBack 返回 A));pump 状态机在 opened/closed 转换后切换
  指针归属/控件(与 OS 前台行为一致);driver fake 的 logRow 点击
  关闭 W2 与捕获语义一致。真实 compiler 产 ready golden 三步
  W1→W2→W1(raw events [(1,pointer_click),(2,window_transition/
  opened),(3,pointer_click),(4,window_transition/closed),
  (5,pointer_click)])。
- **第二红闭合(结构化来源验证)**:test 2 从文案相等改为结构化
  (local-fixture 标识 + "not a Windows capture" 声明必须同时
  存在);replay source 补 local-fixture 标记(不放宽真假来源)。
- **既有测试适配**:test_local_fixture_round_runs_all_stages 的
  windowId 断言更新为真实三步 A→B→A(不放宽 — 真实捕获语义)。
- **S5 driver 测试 6 passed**:全链 3 输入/三 pass/report pass/
  fresh 目录;新增故障注入(recording 收集失败 → compile_recording_
  empty → 非零退出 + 前失败阻断后阶段 + failure note 持久)。
- **门槛(完整结果,自然完成)**:
  S5 driver → **6 passed**;
  `python3 -m pytest -q test_case/test_recording -p no:cacheprovider`
  → **1774 passed, 1 skipped, 1 deselected**(175.03s);
  `python3 -m pytest -q -p no:cacheprovider`
  → **3344 passed, 1 skipped, 614 deselected**(199.33s);
  显式十二文件清 addopts → **280 passed**(50.52s);
  5 legacy 清 addopts → **32 passed**(20.44s);
  git diff --check 通过。HEAD c4ac5442…;SHA256:execution_ledger
  94a77cc4…,binding 73a422d0…,attempt_ledger 4d138c52…,
  s5_acceptance_round 85bbeae2…,test_v3_real_server_adapter
  af48d669…。
- **C 已完成切片**:真实录制(RecordingService/会话/correlator/
  ingest)→ stop 持久 raw v2 → 真实 compile(ready)→ 真实主链
  replay(execute_v3_window_stage_real)→ 真实业务断言(window_
  open Log Center 清单求值 pass)→ 精简特征报告(A 片投影);
  fail-closed 退出/fresh 目录/--live 拒绝;故障注入(收集失败)
  非零 + 阻断。
- **仍未宣称完成(后续)**:独立业务/scroll/drag 断言矩阵在 S5 轮
  内的完整覆盖(当前 1 条窗口打开断言);cleanup 自身关闭正例;
  获授权 Windows 三轮;公共 v3 门保持关闭;legacy/已过切片/WIP
  原件不变。
- **停等 C 片独立复验;未 commit**。

## 2026-11-05 Message206:scroll 真实正负 + 业务事实负例 + 滚动事实接线

- **0941 认可锁定**:三轮 fresh 本地捕获/编译/主链正链(5 事件/
  W1→W2→W1/3 输入 3 pass/features[2,1,1])、stop/持久校验链、
  fact-mismatch fail-closed;实际断言 2 条窗口断言(非独立业务断言)。
- **本轮 P1 修复(先红后绿)**:
  1) **scroll 结果 verifier 接真实 OS 事实变化**(205 缺口1):
     driver fake 的 scroll handler 更新滚动目标控件的 `root_text`
     (OS 事实层 — 滚动语义的结果事实);断言改为指向 btnApply
     (滚动步骤归属窗口的控件;原 "ppl" 是静态子串恒真 — 假成功)
     期望 "scrolled-row"(滚动后应出现的内容)。正例:scroll 改变
     事实 → text_contains pass;**负例**:注入 scroll_fact="Apply"
     (OS 接收滚动但事实未变化)→ 期望对未变事实求值 fail → 非零
     退出(不再 receipt ok 即成功)。
  2) **业务事实负例修正**(205 缺口2):原测试改的是期望值(
     expected=False)与说明不符 — 修正为改 **OS 控件事实**(
     driver 新增 checked_fact 参数,回放时 btnApply checked=False,
     声明期望 True 不变)→ 真实求值 fail/actual=False 断言持久/
     非零退出。发现并修复 fake dump 的重复 "checked" 键覆盖注入值
     的 bug(第二键恒 True 使注入失效)。
  3) **dump 投影一致性**:driver fake 的 dump_tree 与 suite 同构
     (锁定窗口根控件;root_class_name/root_text/root_checked 可
     声明,缺省保持原投影)。
- **drag 接线仍为未完项(如实保留)**:drag 双端解析/唯一性已验
  (适配器切片),S5 轮内 drag 流/断言接线未完成 — 不漏项。
- **门槛(完整结果,自然完成)**:
  S5 driver → **11 passed**;
  `python3 -m pytest -q test_case/test_recording -p no:cacheprovider`
  → **1779 passed, 1 skipped, 1 deselected**(176.23s);
  `python3 -m pytest -q -p no:cacheprovider`
  → **3349 passed, 1 skipped, 614 deselected**(202.04s);
  显式十三文件清 addopts → **291 passed**(55.75s);
  5 legacy 清 addopts → **32 passed**(20.45s);
  git diff --check 通过。HEAD c4ac5442…;SHA256:execution_ledger
  94a77cc4…,binding 73a422d0…,attempt_ledger 4d138c52…,
  s5_acceptance_round b9dbb606…,test_v3_real_server_adapter
  af48d669…。
- **未宣称完成(后续)**:drag S5 接线、cleanup 自身关闭正例、
  receipt unknown/写盘故障注入矩阵、三轮新 fixture 扩展、获授权
  Windows 三轮;公共 v3 门保持关闭;legacy/已过切片/WIP 原件不变。
- **停等独立复验;未 commit**。
