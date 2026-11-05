# edr-wd 无障碍优先的电脑操作与录制迭代设计

日期：2026-10-23；状态：下一轮实施设计，尚未实现或验收。

目标：尽可能接近本次使用 Codex 操作桌面时的体验——先读懂窗口和控件，再精确操作、验证结果，并留下逐步骤证据。不是复制 Codex 私有实现，也不是把截图识别替换成另一套未经校验的点击坐标。

适用范围：Windows UIA、macOS AX、现有远端 target/MCP、录制组件选择和断言、回放证据。浏览器 DOM 驱动为后续扩展，不将网站全部接口发现、隧道、AW/DSH 重构纳入本轮。

## 1. 结论与官方能力边界

edr-wd 已有无障碍树和快照基础。建议补齐统一状态契约、快照引用校验、可视化组件选择、类型化断言和证据闭环，而不是另造 UIA/AX 底层。

必须区分以下三类事实：

| 类别 | 已核实内容 | 本项目的使用方式 |
| --- | --- | --- |
| 官方产品能力 | Codex/ChatGPT Computer Use 可操作桌面；有应用授权、操作系统权限和前台会话要求 | 参考其交互与权限边界，不假定其内部驱动可导入 |
| 官方开发接口 | Computer Use、Browser/Computer 驱动有公开接入说明 | 可选适配，不是本轮核心功能的必要依赖 |
| 本次工具的可观察行为 | 可以取得无障碍树，以当前观察中的控件引用操作，然后重新观察 | 作为体验目标；`getAXState()` 不能据此认定为公开 Codex SDK API |

公开文档没有证明本次桌面封装的树遍历、缓存、索引分配和动作实现细节；不得在实现或宣传中称为“复用 Codex 内部无障碍引擎”。后续本机检查已找到 OpenAI 发布包的 JavaScript 客户端实现，确认其调用原生服务取得 `skyshot.text`；原生树算法源码仍未取得，详见 [getAXState 本机实现取证](getAXState-local-source-investigation.md)。

### 1.1 官方资料与对应关系

核查日期：2026-10-23。下表仅概述公开说明，其后的结构、函数和验收条款均为 edr-wd 设计。

| 官方资料 | 可确认的对应能力 | 不应推导的结论 |
| --- | --- | --- |
| [Computer Use 产品说明](https://learn.chatgpt.com/docs/computer-use) | 桌面 GUI 操作；macOS 屏幕录制/辅助功能权限；Windows 活动桌面；适合时优先结构化集成 | 远端锁屏或断开的 RDP 必然可用；所有桌面程序都有完整控件树 |
| [Computer use API 指南](https://developers.openai.com/api/docs/guides/tools-computer-use) | 截图动作工具与结构化浏览器工具是不同入口；应用提供驱动并管理会话 | API 自动实现本地 OS 无障碍读取 |
| [Browser and desktop drivers](https://developers.openai.com/api/docs/guides/tools-computer-use-sdk-drivers) | 驱动负责引用有效性、隐私与执行时目标复核；浏览器支持结构化读取 | 浏览器的结构化读取契约等同于原生 AX 树契约 |
| [Integration recipes](https://developers.openai.com/api/docs/guides/tools-computer-use-integration) | 可先对自定义驱动做本地固定动作检查，再选择模型驱动 | 模型回复完成就代表业务操作和断言已成功 |

若未来接入上述 SDK，必须单独锁定依赖版本并做导入、协议和端到端验证；本设计不保证当前仓库安装版本已有这些 helper。核心录制、选择与断言不需要新增模型调用或 API key。

## 2. 当前仓库基础与明确缺口

代码观察基线：`hermes-remote`，HEAD `8a5374afff5180041fd9cfe41584191ec3790fc7`，存在正在实施的未提交修改。以下是设计时快照，不是对当前工作区最终验收结论。

| 现有文件 / 函数 | 复用职责 | 本轮需要处理 |
| --- | --- | --- |
| `target/pywinauto_client.py`：`WindowsGUI.dump_tree`、`dump_tree_for_window`、`_build_tree` | Windows 控件树及部分 checked 状态 | 增加可读取状态与动作能力声明，不伪造缺失值 |
| `target/automation/macos_accessibility.py`：`dump_tree` | macOS 树；部分路径使用原生 AX，亦有其他实现路径 | 统一属性错误和能力映射，不能宣称所有路径均为原生 AX |
| `target/recording/windows.py`：`WindowsUIAResolver` | 命中、刷新与关联 | 接入统一身份和当前状态读取 |
| `target/recording/macos.py`：`MacOSAXResolver` | AX 命中与属性读取 | 同上，保留受保护字段处理 |
| `target/windowing/observation.py`：`normalize_observed_control`、`observe_window_bundle` | 窗口身份复核、树与脱敏截图组合 | 作为统一观察入口基础，而非新增第二套窗口绑定 |
| `target/observations/snapshot.py`：`build_snapshot`；`assignment.py`：`assign_target_ids` | 快照、target ID、摘要 | 扩展兼容字段与版本；不重复建设 ID 注册器 |
| `target/observation_bridge/invalidation.py` | 观察失效机制 | 接入选择、预览、执行的统一失效判断 |
| `target/recording/inventory.py`：`normalize_controls`、`content_hash`、`ControlInventory` | 异步控件清单、去重 | 状态保真、覆盖度声明、身份与状态摘要分离 |
| `target/recording/service.py`：`list_controls`、`freeze_component_draft` | 已出现组件清单和冻结入口 | 当前为迭代中接口，不能据函数存在认定用户路径已通过 |
| `target/recording/ui_controller.py`、`recorder_panel.py` | 录制管理面板 | 复用当前开发成果，补齐选择、刷新、ACK 和错误提示 |

已复核的具体缺口：`_build_tree` 会产生 `checked`，但 `inventory.normalize_controls` 的字段白名单没有保留 `checked/value`。不能只新增 UI 复选框，而不修复采集→归一化→持久化→选择→断言整条数据链。

关联资料：

- [上一轮录制易用性设计](../docs/architecture/05-recording-usability-and-assertion-picker-design.md)
- [R2 独立审查快照](../docs/todo/recording-usability-independent-review-r2-20261023.md)
- [当前并行任务边界](../docs/todo/recording-usability-parallel-tasks-20261023.md)

本设计不覆盖或宣布完成上一轮 R2-A～F；实施前先核查其实际合并和验收状态，避免重复修改。

## 3. 目标体验

用户打开录制器后，直接看到当前会话、目标应用/窗口和连接状态，不必再让模型找一次入口。

1. 点击“选择断言组件”，显示当前绑定窗口的可搜索组件树。
2. 可按名称、角色、automation ID 过滤；选择只高亮组件，不点击业务按钮。
3. 面板显示组件类型、可读取状态、支持的断言及不可用原因。
4. 用户选择如“复选框已选中”，预览从原组件重新读取，不跟随当前鼠标或任意焦点。
5. 点击确认后等待服务端实际写入 ACK；成功才关闭草稿，失败保留编辑内容。
6. 回放报告按步骤展示目标、动作、状态变化、断言、截图和失败原因。
7. 录制器内提供绑定窗口的截图小窗预览；查看预览不应把目标窗口强制置顶或抢走用户焦点。预览同时显示窗口身份、采集时间及是否为最新帧。

类型提示至少覆盖：文本框 value、静态文本 name/text、复选框 checked、单选框 selected、列表 selected、通用 enabled/visible/exists。仅在后端声明支持时提供；自定义画布不显示虚假的语义断言选项。

## 4. 总体结构

```text
Windows UIA / macOS AX       后续浏览器 DOM/CDP
          │                         │
          └──── 后端能力适配 ─────────┘
                         │
             统一观察与属性归一化
                         │
        现有窗口身份 + 快照/target ID + 失效管理
                         │
              解析 / 执行 / 重新观察 / 验证
                         │
          录制器组件树 / MCP / 回放断言
                         │
            步骤 trace + 脱敏截图 + 本地报告
```

通道选择顺序：原生桌面优先 UIA/AX；网页有专用驱动时优先 DOM；语义不可用时才考虑视觉定位。必须记录 `route=semantic|visual|mixed`、降级原因、定位证据。不同通道不能通过换一个名称假装获得相同可靠性。

## 5. 数据契约

以下为拟议逻辑模型；落地须扩展现有严格模型并提供版本迁移，不另立与现有 `ObservationSnapshot` 冲突的协议。

```text
ControlObservation
  schema_version
  session_id / host / desktop_id
  window_identity: pid, native_id(HWND/AX identity), generation, process_name
  snapshot_id / revision / captured_at / expires_at
  target_id / parent_target_id / ancestry
  identity: automation_id, native_control_id, role, class_name
  name / text / value                     # 不把三者混成一个字段
  states: checked, selected, enabled, visible, expanded, focused
  properties[field]: status, value, source, observed_at, error_code
  capabilities: readable_properties[], actions[]
  rect / coordinate_space / scale
  coverage: complete, truncated, omitted_reasons[], node_count
```

`properties[field].status` 使用 `known|unknown|unsupported|read_error|redacted`；只有 known 的 value 可用于确定性断言。`checked=false` 是有效读数；缺少 checked 不能转为 false；三态复选框 mixed 也不能转成布尔 false。不得对读取结果直接使用 `bool(None)`。

身份与状态分离：可变文本、坐标、checked、enabled 不能独自决定稳定身份。现有 fingerprint 若包含可变字段，保留兼容语义并新增明确的身份/状态摘要，避免静默改变既有回放规则。重复名称不得取第一个匹配。

`target_id` 只在对应 snapshot 内有效，不存成永久控件地址；长期回放保存选择器约束和窗口身份规则。跨快照重解析必须唯一匹配并记录映射，不能只重用 T0001。

保护规则：密码等敏感属性在 target 侧读取/归一化时拦截；截图在传输前脱敏；日志、断言草稿、差量和错误堆栈都不能绕过保护。保护状态不明时默认不暴露 value。

## 6. 函数划分与依赖

下表中“新增”均为拟议名称，不代表已存在。实施者应先搜索当前迭代同职责实现；若已有函数则扩展并在 PR 中注明对应关系。

| 文件 | 函数 / 状态 | 输入 → 输出与依赖 |
| --- | --- | --- |
| `target/observations/control_properties.py`（新增） | `normalize_property(raw, *, supported, protected, source)` | 原始属性 → 带状态属性；零 UI 副作用 |
| 同上 | `normalize_control_properties(raw, capabilities)` | 各后端数据 → 统一字段；被 inventory 与 window observation 共用 |
| `target/windowing/observation.py`（扩展） | `observe_window_bundle(...)` | 已锁窗口 → 一致观察；复用前后身份检查、脱敏捕获 |
| `target/observations/control_query.py`（新增） | `query_controls(snapshot, query, *, parent_ref, limit)` | 当前快照 → 树片段和覆盖度；不重新编造 target ID |
| 同上 | `resolve_control_ref(snapshot_id, target_id, binding)` | 窗口绑定 + 引用 → 唯一当前控件或明确错误；依赖既有失效管理 |
| `target/automation/control_state.py`（新增） | `read_control_properties(resolved, fields, deadline)` | 重新读取 UIA/AX 属性 → 带状态结果；不使用旧 inventory 值冒充实时值 |
| 同上 | `get_control_capabilities(resolved)` | UIA patterns / AX 可用属性动作 → 能力声明 |
| `target/automation/semantic_action.py`（新增） | `execute_control_action(resolved, action, policy, deadline)` | 复核身份后执行允许动作 → 执行回执；依赖能力声明与权限策略 |
| `target/recording/inventory.py`（扩展） | `normalize_controls`、`content_hash` | 采用共用属性；区分身份摘要/状态摘要；保留显式 unknown 与覆盖度 |
| `target/recording/service.py`（扩展） | `list_controls`、`freeze_component_draft` | 清单携带快照绑定；冻结引用不等于冻结状态 |
| `target/recording/assertion_draft.py`（扩展） | `suggest_assertions(control)`（新增） | 能力/类型 → 断言选项、值类型和不可用原因 |
| 同上 | `preview_component_assertion(draft, binding)`（新增） | 引用重新解析→实时属性→现有断言规则；输出 observed/expected/verdict |
| `target/recording/ui_controller.py`（扩展） | `select_component_ref(...)`（新增） | UI 请求 → 服务端验证引用并生成草稿；禁止客户端提交可信属性 |
| `target/recording/recorder_panel.py`（扩展） | 组件树搜索、局部展开、高亮、预览视图 | 只消费服务端 DTO；确认等待匹配 request_id 的持久化 ACK |
| `agent/recording/mcp_runtime.py`（扩展） | `MCPObservationProvider` | 保留现有远端传输，将统一观察用于模型与确定性执行 |

关键依赖链：

```text
选择：list_controls → query_controls → resolve_control_ref → freeze_component_draft
预览：draft → resolve_control_ref → read_control_properties → assertion evaluator
执行：resolve_control_ref → policy/capability check → execute_control_action
      → observe_window_bundle → postcondition evaluator → evidence/report
```

不要让管理 UI 直接调用 `session.add_assertion` 绕过草稿校验，也不要让组件树选择触发真实 Invoke/AXPress。

## 7. 操作与断言生命周期

统一状态机：`observed → resolved → authorized → executing → reobserved → verified → evidence_saved`；允许 `failed/cancelled`，但不能在动作未执行或验证未完成时返回业务成功。

- observe：限定 session/window，验证桌面与权限，捕获控件、覆盖度和初始截图。
- resolve：检查 snapshot、generation、目标唯一性与动作能力；过期返回 `snapshot_stale`，歧义返回 `target_ambiguous`。
- authorize：复用现有产品策略；高风险动作仍需要业务允许，控件标签不能成为授权来源。
- execute：UIA Invoke/Toggle/Value 等或 AX 对应能力；不是所有控件都支持相同 pattern/action。不可用则明确失败或经允许降级。
- reobserve：动作后刷新，重新绑定可能重建的窗口/控件；必要时有界等待业务状态稳定。
- verify：点击成功只是输入已发送；必须用配置的后置条件判断。无后置条件只记 `executed_unverified`，不得输出“断言通过”。
- evidence：即使失败也尽可能抓取证据；证据失败单独记录，不能吞异常或伪装完整报告。

坐标降级必须再次验证所属窗口、几何、缩放及目标唯一性；布局变化后不得使用旧坐标。视觉/OCR断言与语义断言分别声明，不能把 OCR 未识别当成文本不存在。

断言结果为 `pass|fail|inconclusive|error`：只有属性已知且满足规则才 pass；未知、脱敏、覆盖不完整情况下的“不存在”通常 inconclusive；读取异常为 error。普通字符串 `123` 不得自动变数字。

请求使用 session_id + request_id 关联并支持幂等重试。入队回执是 accepted，不是 committed；错误/迟到/其他 request_id 的 ACK 不能关闭当前草稿。服务端持久化失败必须可见。

## 8. 树读取效率与交互隔离

提供 `interactive_only`、子树展开、搜索与分页。设置 max_depth、max_nodes、max_duration 和结果字节预算；达到预算要返回 truncated/omitted，不宣称读取了整个页面。虚拟列表、懒加载、折叠内容不可见时明确标记，必要时用户授权后展开或滚动。

复用现有 inventory 后台采集，避免阻塞实时命中与录制关联线程。UI 可以显示缓存，但预览和执行必须验证实时身份与状态。取消、退出、超时后终止或隔离剩余读取工作，不让失控后端线程持续持有会话资源。

差量观察包含 base_revision/revision；客户端基线不匹配时回退完整快照。差量仅是传输优化，不赋予旧引用执行权限。

每个交互桌面建立独占操作 lease；同一前台会话不可由多个 agent/录制器同时操纵。代码编写和隔离测试可并行，真实 GUI 操作排队。lease 需有 owner、超时、释放与失主清理。

Windows 预检活动桌面、会话连接、窗口可见性、权限等级和锁定身份；RDP 重建后重新观察，不能沿用旧 HWND。macOS 分别检查辅助功能与截图权限。检查失败时面板给出可执行提示，不自动确认关机等危险对话框。

## 9. 截图、trace 与本地报告

### 9.1 窗口截图小窗预览

本次 Codex 桌面操作中可观察到：工具能在小窗展示某个窗口的截图，而无需先将该窗口置于桌面最上层。edr-wd 将此作为体验目标，不据此推断 OpenAI 原生服务使用了哪种截图 API，也不预设 Windows 与 macOS 的后台窗口捕获能力相同。

预览应绑定已核验的原生窗口标识与会话，而非当前前台窗口或一块固定屏幕坐标。采集与展示分离：打开、刷新或缩放预览只读取目标窗口图像，不调用窗口激活、置顶、点击或业务操作。每帧携带窗口身份、采集时间、尺寸、缩放、脱敏状态和错误状态；窗口重建、权限丢失、最小化或不可捕获时明确显示不可用/陈旧，不把旧帧伪装成当前画面。截图脱敏须在传给预览 UI 与落盘前完成。

先复用现有窗口绑定与 `observe_window_bundle`/截图路径，在 macOS 和 Windows 分别验证“目标窗口被其他窗口遮挡时可预览且不抢焦点”。最小化、跨桌面、RDP 断连及受保护窗口单列能力结果；若平台后端无法可靠捕获，应返回明确原因，不以临时置顶作为静默降级。预览图仅帮助选择与核对，不代替实时 AX/UIA 状态或业务断言。

沿用此前约定：首步骤 init 截一次 before；每步骤操作与验证后生成 after。仅当窗口身份、连续性、时间与证据完整性满足规则时，前一步 after 可引用为下一步 before；用户插入操作、换窗口或断连后重新捕获，不能机械复用。

每步骤按同一 step_id 组织：

```text
step_id / request_id / session_id
before: observation_id, snapshot_id, evidence_id, reused_from_step
target: native_window_id(HWND), pid, generation, role, locator_summary
operation: route, action, started_at, finished_at, receipt
verification: expected, observed, property_status, verdict, error_code
after: observation_id, snapshot_id, evidence_id
capture_status / redaction_status / timings
```

HTML 和 Markdown 每个步骤内排列“操作 trace → 断言结果 → before/after”，不把所有操作和截图分成无法对应的两块。显示动作时间、截图时间，不能声称截图就是点击瞬间。

agent 仓库本地输出 `result-report/<时间戳>/report.html`、`report.md`、结构化 trace 与图片。PNG 用二进制原子写入；保存后验证解码和摘要；服务重启后图片仍可读。只有报告与附件全部落盘后才标记 collected；部分失败保留待补收状态。HTML 转义控件文本、阻止脚本注入与越界附件路径。

## 10. MCP 与官方接口接入策略

本轮优先扩展现有 MCP 服务的观察、组件查询和断言能力，保持既有 target 部署方式。建议新增的逻辑操作为 `inspect_controls`、`read_control_state`、`preview_assertion`；最终工具名需遵从现有注册规范，不能把设计名当成已可调用工具。

可选下一阶段：实现独立 OpenAI driver adapter，将既有执行引擎包装为公开工具接口。浏览器结构化读取可映射 name/role/ref/checked；原生 AX 不应伪装为网页。适配器负责显式方法白名单、审批、调用期限、清理与结果验证。

不读取或复用 Codex 私有凭证，不依赖 app 私有对象，不要求录制器为了基本选择断言调用模型。用户界面和网页中的文字均为不可信数据，不得用于改变权限策略或执行任意脚本。

## 11. 实施顺序与阶段性产物

| 阶段 | 内容 | 退出条件与产物 |
| --- | --- | --- |
| E0 基线 | 独立核实当前 R2 修复、用户路径和已有函数 | 基线审查记录；列明通过/失败/未验证，不以测试数量代替结论 |
| E1 契约 | 属性状态、身份/状态分离、inventory 保真、版本迁移 | schema + 真实后端形状契约测试；checked/value 全链路不丢失 |
| E2 组件选择 | 搜索树、局部展开、高亮、类型提示、冻结引用 | Windows/macOS 用户路径证据；选择不触发业务操作 |
| E3 执行断言 | 实时重读、过期拒绝、后置验证、关联 ACK | 反例回归及确定性集成测试；未知不会误通过 |
| E4 证据兼容 | 生命周期截图、窗口小窗预览、持久化、HTML/MD、预算与并发 lease | 遮挡窗口预览不抢焦点；不可捕获状态明确；本地报告可重开；旧录制兼容报告；延迟/覆盖度实测 |

每阶段提交修改文件清单、实际命令/退出码、失败码分布、截图路径和剩余问题；未完成的阶段不得写“全部完成”。真实 Windows 与 macOS 验收分别记录，单平台成功不能替代另一平台。

## 12. 测试与验收矩阵

| 场景 | 必须验证 |
| --- | --- |
| 状态投影 | true/false/unknown/mixed/read_error/redacted 经采集、序列化和加载不混淆；0 与空字符串语义保留 |
| 身份变化 | 同名控件、控件重排、窗口重建、HWND 复用、PID/generation 变化均不会点错目标 |
| 选择路径 | 打开录制器→选择窗口→搜索组件→高亮→配置→预览→提交，无额外模型轮次；无业务点击副作用 |
| 实时断言 | 草稿建立后切换 checked，预览读到新值；鼠标移开不换目标；不支持的断言不可提交 |
| 动作验证 | 驱动返回“已发送”但业务没变化时不能 pass；能力不支持时错误明确 |
| ACK | 延迟、丢失、错 request_id、重复请求、存储失败、取消后迟到 ACK 均不误报成功 |
| 快照 | 过期引用拒绝；差量基线错位恢复全量；截断树不能证明控件不存在 |
| 安全 | 密码及保护未知状态不泄漏；页面文本不执行；报告 HTML 转义；拒绝附件路径穿越 |
| 持久证据 | PNG 实际解码、重启可读、附件不全不 collected；每步 trace 与图一一对应 |
| 窗口预览 | 目标窗口被遮挡时小窗截图绑定正确窗口且不激活/置顶；窗口切换、最小化、权限失败和陈旧帧如实提示；预览与报告均不泄漏受保护内容 |
| 会话/性能 | GUI lease 排他；断连/锁屏提示；超时取消清理；大树返回覆盖度和实测耗时 |

测试分层：纯归一化单元测试 → 真实生产 DTO 形状契约测试 → 服务/IPC/存储集成测试 → 两平台真实 GUI 验收。mock 只证明逻辑，不证明原生 UIA/AX 或用户入口可用。

建议最小真实验收流：启动录制器；绑定测试应用；选普通文本框断言；选复选框并切换其状态；打开第二窗口记录步骤；停止录制；重启服务后回放并打开报告。逐次记录 HWND/原生窗口标识、角色、快照、失败码与断言。使用无危险副作用的专用测试应用，不擅自操作生产业务设置。

性能不预先编造毫秒承诺：先在小/中/大树样本记录节点数、P50/P95 耗时、截断率和 UI 响应，再确定预算。验收要求有界返回、覆盖度诚实、命中线程不被全树采集阻塞。

## 13. 可直接交给下一轮实施者的任务说明

按本设计 E0→E4 实施无障碍优先的录制与回放体验。先阅读现有录制易用性设计、最新独立审查和当前未提交改动；尊重文件所有权，优先复用 ObservationSnapshot、窗口绑定、失效管理和现有组件选择实现。先交基线与契约，再做 UI 和执行链路；每阶段交可复现测试与用户路径证据。

禁止：复制假定存在的 Codex 私有接口；另建重复 target ID 系统；未知状态转 false；以入队 ACK 冒充持久化成功；组件选择触发业务点击；静默坐标降级；测试通过数量代替用户路径验证；未经要求推送、部署或改变生产策略。

本次文档交付只新增设计文件，不代表上述功能已经开发，不修改正在进行的代码工作。
