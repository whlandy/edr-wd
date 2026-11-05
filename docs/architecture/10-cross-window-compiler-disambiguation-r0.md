# 跨窗口录制回放：编译期窗口归属消解设计（R1）

版本：R1；日期：2026-11-03；状态：审核修订稿，实施与验收待完成。
保留原 r0 文件名以兼容已有链接，R1 正文取代 R0 的无条件 handle 等价、
确定因果及“补 drag verifier 即 ready”承诺。

## 0. 定位与边界

本设计细化 09 号设计 §6.1–6.4 的编译归属规则，不替代或放松 09 的
raw v2/golden v3、目标实例绑定、能力与租约、§7.6 可回溯状态要求。

需要区分三个问题：

1. **来源归属**：录制证据说明动作发生在哪个录制窗口。
2. **回放定位**：稳定身份和本次运行的唯一绑定策略能否找到正确实例。
3. **可执行性**：生命周期、控件定位、业务 verifier、目标能力等门槛是否全部满足。

来源归属能够修复时应修复，不能因 selector 碰撞丢弃已有证据；但来源
可区分不等于回放可唯一绑定，更不等于整条轨迹 ready。W 编号只是引用。

## 1. cross_window_s3 的核实基线

本节基于 `recordings/cross_window_s3/` 当前保存的原始文件与既有编译
报告，属于静态核对，**不是本次重新编译或回放通过**。

- raw：`edr.desktop-recording/v1`，23 个事件、8 个 manifest 窗口。
- 既有 golden：v2，9 个编译步骤；源映射取自 compile-report。
- raw SHA-256：`0b39c2663be8844c787f612e66636c2f447b3431c9db08a1332ce4760cc5d876`。
- compile-report SHA-256：`bf6846489033ed51d96859cc6df6f217d202ee8b6ef4c532416f19e5ab3f0fef`。
- golden SHA-256：`89ea5d8acff00e7cf893c2ccfd4a333860030c074b8d3394350779d6a4701c7c`。

文档只使用主窗口、日志中心、弹窗等别名，不复制原生 handle/pid。

| 编译步骤 | raw 来源 | 动作所在录制窗口与注意事项 |
| --- | --- | --- |
| step-0001–0003 | 1；2；3/4 | 主窗口；step-0003 点击 Log Center，raw4 是关联打开 transition |
| step-0004–0005 | 5；6 | 日志中心，root 证据为 LogCenterWindow |
| step-0006 | 7/8 | raw7 窗口 handle 指向日志中心，但 root 为 EdrMainWindow；raw8 是日志中心关闭 transition，归属/root 存在冲突 |
| step-0007 | 9 | 主窗口返回 |
| step-0008 | 10–16 | raw10 动作在主窗口；其余为 EDRClient 弹窗 transitions |
| step-0009 | 17–23 | raw17 动作仍在主窗口；其余为另一组 EDRClient 弹窗 transitions |

因此，step-0008/0009 **不是在弹窗内执行的动作**；动作的效果窗口不能
倒灌为动作自身归属。日志中心关联的是 step-0003，而非 step-0001。
已有报告的歧义 issue sequence 1/7 指 raw 序号，不能直接当 compiled step ID。

### 1.1 已知证据缺口

- v1 没有 instanceId、原生生命周期 timeline 及其覆盖声明，不能证明全程
  无销毁、句柄复用或漏事件。manifest 的 closed 是末态，不是死亡时间线。
- openedBy 是关联动作 ID，不是已经证明的直接因果；现有正向最近动作关联
  和时间回填均可能产生它，旧值无来源分级时按 unknown 处理。
- 主窗口/日志中心有 root 正证据，但 raw7 与日志中心快照 CS-0002 的 root
  冲突尚待解释；不能挑一份“最完整快照”掩盖冲突。
- EDRClient 弹窗快照 CS-0003/0005/0006/0008/0009 没有 root；所读快照
  元数据未声明 complete。不能补写 root/complete 或据此证明控件唯一性。
- 本样本的 native_transition verifier 不是用户显式绑定窗口断言；
  多个同名窗口还共享 openedBy，不能由同一关联 ID 推出唯一实例。

## 2. 编译归属算法

### 2.1 先维护完整来源映射

沿用 09 §6.1，在生成步骤时维护 stepId → 全部 source event 引用，
保留合并动作、绑定断言、transition 折叠与 cleanup 的映射，禁止 zip 猜对应。

来源须区分角色：动作来源决定动作窗口；assertion/transition 可声明其他
效果窗口。不能只看首个来源后忽略其他合并动作的窗口证据，也不能要求效果
transition 必须与源动作同窗。多个合并动作归属不一致时拒绝合并或明确 incomplete。

### 2.2 v2 优先按实例与有效时间段关联

使用事件自身 instanceId → manifest 实例 → 同实例、有效生命周期段的
事件/快照 → 稳定身份；禁止用末态 handle 字典覆盖同 handle 的多代实例。
HIDE/SHOW、DESTROY/复用、lost/覆盖不足按 09 §4/§6 的既定规则处理。

### 2.3 v1 只能做有界的证据归组和误报消除

- 同一录制 handle 且无相矛盾证据，可作为“这两次动作并无明确不同窗口
  证据”的正向线索，消除 A→B→A 的特定 collapsed-window 误报。
- **不得把同 handle 无条件等同同物理实例**。存在身份变化、销毁/重现、
  时间关联不明等情况时保留 unknown/incomplete；不伪造 instanceId/timeline。
- 不同 handle 的观测不可仅因 title/root 相同合并；但这既不证明窗口共存，
  也不证明回放时有唯一绑定策略。需要生命周期区分时仍保留缺口。
- v1 的归组结果作为 compile-report 中有来源、有限置信边界的诊断证据；
  不将弱归组冒充满足 v3 强连续性契约的可执行 W 实例。
- 原 v1/v2 golden 的已有读取/执行兼容性不因新增 v3 门槛被一刀切禁止。

### 2.4 快照选择与冲突处理

快照必须关联同录制 session、窗口/实例、有效时间段及来源引用。不能按
controlCount 最大、有 root 或“信息最多”优先跨时间、跨实例借证据。

root/title/process 的一致性按 09 规范核对；root 与 title 同时声明就都保留。
同时间段冲突 root、错误窗口快照、时间/代际无法确定时，输出定位到 step、
source、snapshot 的结构化诊断，保持 incomplete，不静默覆盖。

缺 complete 不抹掉窗口属性的单条正证据，但不能证明“没有其他控件”或
控件唯一性；无可靠 root 时保留合法 title-only 身份，不凭空补 root。
没有完整稳定身份时不生成看似可执行的 windowSequence。

### 2.5 W 分配与声明

对已证明的录制实例按首次可信归属确定性分配 W；同一有效实例的返回复用 W，
不同实例即使稳定身份相同也不能合并。只有效果来源、没有动作来源的窗口
是否进入声明，由显式效果契约及 09 的完整引用规则决定，不把 manifest
全部窗口机械编号后声称歧义解除。

解除原实例歧义仍须满足 09 §6.4：录制归属完整、稳定身份一致、每次首次
绑定有可执行唯一策略、后续绑定可验证、不同 W 不会无依据绑定同一运行实例。

## 3. 效果解析与本次运行绑定

### 3.1 关联不升级因果

保留 explicit / temporal_forward / temporal_backfill / unknown 分级。
旧 openedBy、native transition、时间近邻均只能辅助诊断，不能自动升级为
explicit appeared。用户显式窗口断言表达待验证后置条件，也不宣称物理因果。

### 3.2 效果目标使用完整身份和来源约束

window_open 目标核对 process、已声明 root/title 及合法关联范围，不使用
“next 首条”“同标题第一条”或事件最近者。完整条件仍匹配多个 W 时，
保留多候选诊断/incomplete，不任意挑选、补 root 或删除身份字段。

### 3.3 两种允许的首次绑定策略

- 当前唯一：授权产品域内按完整稳定身份实时唯一绑定，只证明当前唯一，
  不证明由某动作创建。该策略必须符合编译声明，不能拿来绕过明确 appeared。
- 显式 appeared：动作前保存候选/绑定/可见性基线，动作只执行一次；动作后
  只读差分，唯一新增实例或有可信同实例 HIDE/SHOW 的可见性转换才能满足
  声明。already-visible 旧窗口不算 appeared；增量多候选拒绝。

录制时的 handle、openedBy 不用于选择本次运行原生实例。缺少必要绑定策略
属于编译 incomplete；编译契约成立但现场候选变多、消失或能力不足属于本次
运行失败/拒绝，不倒改历史编译结论，更不靠“因果顺序”猜窗口。

后续操作沿原不透明 binding，在目标租约/GUI串行边界验证 generation、
完整身份、产品归属、可见性和前台。原 binding 丢失不找同名兄弟替代。
动作 receipt unknown、断言失败等不偷推进，沿用 09 §7.6 状态/证据要求。

## 4. ready 与兼容性

- 修复特定误报，不代表其他阻断问题自动消失。窗口归属不能替代 drag/scroll
  结果 verifier、弱控件定位、完整性约束或业务断言。
- 历史 v1 样本缺少 v3 所需生命周期证明时，v3 编译保持 incomplete；
  不因补 drag verifier、W 编号或手填显式效果而 ready。
- raw v1/v2 双读、已有 golden v1/v2 行为保持兼容；不把历史弱数据伪迁移为
  raw v2 强证据。需要强契约时重新录制，输出新目录，保护原件。
- v3 完整门槛同时约束 compile-report/case/golden；目标缺能力在 GUI
  副作用前拒绝。导出不支持等价契约时明确拒绝，不静默降级丢失 windowSequence。

## 5. 验收矩阵

### 5.1 历史 cross_window_s3：只验事实修复

- 按本设计§1核对 23 raw/9 compiled/8 manifest 和全部 source 映射。
- step-0001/0007/0008/0009 的主窗口返回不因紧邻日志中心动作误报为另一窗口；
  这不等同已证明完整生命周期、已生成可执行 v3 或已回放通过。
- step-0006 raw7 的 handle/root 冲突必须明确诊断；在解释/重录前不能借
  主窗口或日志中心快照强行消解。
- 弹窗 transitions 与源动作分离；弹窗缺 root/complete、关联等级、生命周期
  缺口及控件证据如实保留。不要求所有 manifest 窗口都生成可执行 W。
- drag verifier 补齐后只解除 drag 对应缺口，不预设整体 ready。

### 5.2 新录制 raw v2：验证强契约正反例

- A→B→A、同实例连续动作；同标题不同 root、同 root 多实例。
- 明确同代 HIDE/SHOW 保持实例；DESTROY 后相同 handle/pid 重建为新实例；
  丢事件/覆盖不足/乱序/错误快照不能假称连续性完整。
- 合并动作多个来源、绑定断言、transition/cleanup 映射正确；root/title
  冲突拒绝；快照时段/实例错误拒绝。
- 旧 openedBy 和 native transition 不提升 explicit；显式 appeared 使用
  前后差分，旧窗口不能满足，增量多候选拒绝。
- 首绑多候选拒绝，focus 到 dispatch 替换执行0次；原绑定丢失不找兄弟，
  两 W 无依据共绑一个运行实例拒绝。
- 生命周期/身份/绑定策略/业务 verifier 全部满足才验收 v3 ready；窗口
  效果通过而业务断言错误仍失败。执行状态与截图可回溯沿用 09 §7.6。
- v1/v2兼容、v3严格解析、model/schema一致、报告脱敏与导出拒绝门不回退。

实机闭环仍按09的授权与验收范围执行：连续三轮新原件录制→停止收集→编译→
回放→业务断言→可回溯带图报告，并有人工注入缺陷正确失败。本文不宣称已完成。

## 6. 实施约束与修订记录

仅细化编译归属与诊断，复用09已有协议与切片；不另起旧R0实现路线，不扩张
动作类别/权限/恢复重试，不绕过正在进行的S3–S5独立验收。当前文档修改本身
不授权打断原执行轮、并发编辑/测试共享工作树、提交推送、部署或真实GUI操作。

2026-11-03 R1：修正样本数量与步骤/效果归属；限定v1 handle证据用途；
移除openedBy确定因果和最完整快照假设；补来源角色/冲突与绑定策略；拆分
历史误报验收与新v2强契约ready/实机验收。仅修订设计，未改业务实现或原件。
