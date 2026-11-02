# 录制入口与可视化断言向导设计

日期：2026-10-22（Asia/Shanghai）。状态：设计完成，待 ZCode 分阶段实施及独立验收。
实施仓库：`/Users/edr-test/ai-projects/edr-wd`；工作分支以执行时实际分支为准，不自动切换、提交或推送。

## 1. 要交付的用户体验

用户不需要再发一轮自然语言请求才能打开录制界面或找到被录制窗口。安装后的桌面入口能打开录制面板；面板中选择目标窗口，点击“定位窗口”“开始录制”，录制中可暂停、选组件加断言、停止并查看产物。Agent 端也提供确定性的 `record ui` 命令，复用同一目标端面板，不创建另一套录制状态。

断言操作变为“选组件 → 高亮确认 → 看当前内容 → 选验证方式 → 编辑期望 → 保存”。组件 ID、窗口根 ID 和 JSON 不出现在默认操作路径。高级选择器仍能展开查看。每一步都有中文提示、取消入口和失败说明。

“采集到了当前值”不等于“业务结果正确”。当前观测只作为用户可编辑的候选期望；保存必须经用户确认，试验通过也不自动授予业务 oracle、AW 批准或整条用例 ready。

## 2. 代码现状与确认的问题

核查基线：2026-10-22，本地 HEAD `8a5374a`。执行前重新核对；当前 `agent/cli.py`、`target/recording/session.py` 和新增 `test_recording_visibility.py` 有既有 WIP，需要保留。

| 现有位置 | 现状 | 体验缺口 |
|---|---|---|
| `agent/cli.py::_cmd_record()` | start 要填名称、进程、标题；connect → lock → verify → start | 没有空闲态前台入口，依赖 Agent 帮忙找窗口、填参数 |
| `target/recording/service.py::RecordingService.start()` | 会话建立后才创建 indicator | 录制前没有面板；浮窗销毁后也没有可恢复入口 |
| `target/recording/indicator.py::TkRecordingIndicator._run()` | 活跃态浮窗，Pause / Add assertion / Stop | 没有“定位被录制窗口”；按钮文字、提示、错误反馈不足 |
| 同文件 `_show_assertion_editor()` | 普通 Entry 手填 assertion、expected、controlType、ID、name；expected 尝试 JSON 解析 | 文本 `123` 可能变数字；不知道支持哪些断言、name 与 value 的区别 |
| 同文件 Add assertion 按钮 | 直接调用 `_show_assertion_editor()` | 绕过 `RecordingSession.request_assertion_editor()` 的选中目标及窗口上下文准备；无法可靠获取新目标 |
| `session.py::_open_assertion_editor()` | 用当前指针命中，失败后取 last_observed_target；保留前序事件窗口 | 点击浮窗时指针已离开业务组件；回退目标没有明确提示、重选和确认 |
| `session.py::request_assertion_editor()` / `ingest()` | 编辑器允许 PAUSED 打开，但 ingest 只接受 RECORDING/STOPPING | 暂停时填写的断言可能到保存才失败；需区分管理断言与暂停的业务输入 |
| `source.py::QueuedCaptureSource.current_observed_target()` | 已有 UIA/AX 当前目标读取 | 可复用，但尚无明确拾取模式、取消与选中反馈 |
| `indicator.py::SubprocessTkIndicator._consume()` | 子进程请求回调；保存表单后立即关闭 | 缺少带 requestId 的成功/失败 ACK；异常可能无法呈现给用户 |
| `session.py::stop()` 与 CLI stop 路径 | 目标端 stop 返回 raw；Agent CLI 负责抓截图、编译、落盘 | 仅点击浮窗 Stop 不能证明 Agent 产物已下载，不能把“停止”写成“报告已生成” |

已有取值预填、窗口归属、脱敏、回放证据与租约展示能力继续复用。旧 `doc/assertion-value-prefill-design.md` 的“没有 value”是历史现状，不作为本轮事实。既有加固文档中的 stop 后“补 record assert”不能照搬：当前 editor 只接受活跃/暂停会话，停止后修复需单独的带序号编辑能力；本轮只承诺录制中补断言。

## 3. 范围、默认决策与界面

### 3.1 常驻入口与目标窗口定位

1. 在 Agent 端增加 `python3 -m agent.cli --target <name> record ui`，打开/恢复该目标交互桌面的面板，并维持录制管理连接和产物收集。不调用 LLM。
2. 提供目标端启动脚本及 Windows 桌面快捷方式、macOS `.command` 安装说明，双击显示同一面板。启动脚本复用现有安装/认证配置；服务未运行则展示启动指引或明确的启动按钮，不假报成功。
3. 面板是目标端服务级 UI，空闲时也存在。Windows Session 0 / 锁屏 / RDP 已断开时报告 `interactive_session_unavailable`，不在不可见桌面后台录制。显示“此面板位于 11.26 的交互桌面”，避免误以为它会出现在 Agent Mac 本机。
4. 窗口列表显示产品、进程、标题、PID；UIA 根 ID 与 HWND 在详情中。允许刷新和从当前前台窗口选择；有多个候选时必须由人确认。自动建议从现有 product spec 来，不能把 `EDRClient.exe` 或 `HiSecEndpointAgent.exe` 写死成唯一真值。
5. “定位窗口”先核对目标 HWND/PID/进程及根身份，再恢复、前置并验证；目标销毁或重建后提示重新选择，不能按同名窗口静默切换。此操作属于管理动作，不能写进业务录制。
6. 未开始时显示“就绪，尚未录制”；录制时显示红色状态、时长、实际动作数、租约剩余、未绑定结果验证数量。“已停止 / 待下载 / 已保存 / 编译不完整 / 下载失败”是不同状态。
7. 新常驻面板关闭按钮在录制时先显示“收起面板 / 停止并保存 / 取消”，收起后保留可见迷你状态条及桌面恢复入口。旧会话浮窗的关闭即停止行为保持兼容，不能因改 UI 产生无提示的隐藏录制。

示意（普通用户默认界面）：

```text
EDR-WD 录制面板                     目标：11.26 / admin
窗口：[HiSec 主窗口 · 当前实际进程 ▼] [刷新] [定位窗口]
名称：[日志检查_自动时间后缀]          状态：就绪，尚未录制
[开始录制]  [暂停]  [选择组件加断言]  [停止并保存]
已录 8 个动作 · 2 条断言 · 租约 242 秒
第 8 个动作是滚动，尚无结果验证 → [为该动作添加验证]
保存：等待录制结束；Agent 连接：在线 / 不在线
```

### 3.2 组件拾取与文本选择

默认使用“指向组件，再按确认键”的方式，避免为了选择一个开关而改变业务状态。按键候选为 F8 确认、Esc 取消；上线前验证 Windows/RDP/macOS 冲突，不把无冲突当既成事实。界面常显操作说明。

拾取模式临时拦截业务事件持久化，保留专门的拾取输入通道；不能直接复用 `pause()` 后依靠已被 `_enqueue()` 丢弃的事件。鼠标移动只在工作线程限频做 UIA/AX 命中，原生 hook 回调仍只做有界入队。轮询频率上限 10Hz、单次读取超时 1s；后台异常清理 overlay，不堵录制和 MCP 服务。

命中组件时画穿透输入的高亮框；显示窗口、组件类型、可读名称和当前内容。框、迷你条、编辑器、通知都使用 `RECORDER_UI_MARKER`/统一 UI 身份注册，被输入归属过滤、窗口发现、截图脱敏共同排除。不能只排除原浮窗矩形。

F8 确认后固定组件身份及窗口上下文，再打开表单；Esc / 断连 / 窗口销毁时移除高亮，恢复之前 RECORDING 或 PAUSED 状态。确认键、取消键及打开菜单等管理输入不进入业务 trace，也不触发业务快捷键。原生键事件若不能可靠吞掉，则该平台禁用该确认键，提供基于组件树列表的选择入口；不能用真实业务点击兜底。

拾取结果必须属于本次录制 scope。包括既有逻辑已接纳的日志子窗口及兄弟进程窗口；范围外显示“该窗口不属于当前录制”，不自动扩 scope。不确定/多个匹配时列候选并要求重选，不能取第一个。

UIA/AX 不可读时提供“组件列表”搜索，数据来自最新窗口控制树/既有 inventory。不能将无语义的屏幕坐标包装成组件断言。本轮不引入 OCR；纯画布文字不可访问时明确提示。用户可以主动改选“窗口包含文字”，但它验证整个窗口可访问文本，不声称验证某个像素区域。

### 3.3 中文断言向导

| 用户选项 | 既有断言类型 | 期望控件与提示 |
|---|---|---|
| 组件文字等于 / 包含 | `text_equals` / `text_contains` | 字符串输入；展示实际读取的是 name/text；`123` 保持字符串 |
| 输入框/组件值等于 | `value_equals` | 字符串；展示 value；缺失时显示“读取失败/不支持”，不能预填空串假装空值 |
| 组件可见 / 已勾选 / 可用 | `visible` / `checked` / `enabled` | 布尔选择，不要求输入 JSON；只对支持相应读取能力的组件开放 |
| 窗口已打开 | `window_open` | 已选窗口，打开/未打开；高级对象沿用后端既有契约 |
| 窗口包含文字 | `window_text_contains` | 明示验证范围是选中窗口的可访问文字 |
| 当前时间/时间区间 | 既有 `*_time`、`*_time_range` | 高级选项，显示示例和格式说明，沿用现有解析器 |

向导显示“当前观测”和“期望结果”两块；二者不因用户编辑期望而混淆。可点击“重新读取”更新当前观测，已经被用户修改的期望不静默覆盖。受保护控件禁止抓取/预填/展示敏感值，也不写日志和截图；未知保护状态沿用现有保守策略。

保存前显示自然语言摘要：“在日志中心，等待最多 10 秒，验证『操作日志』已勾选。”高级区折叠显示 selector、窗口身份、来源与观测时间。未选组件时显示“请先选择组件”，不能默认生成 controlType=Text 的空白目标。

“立即试验”使用与回放一致的读取、比较与超时逻辑，结果分为通过、不满足、不可判定/读取失败。读取异常不进入比较器；false、空串、0 是合法值，不能用 truthy 判断丢弃。试验只读，不产生业务点击、不新增断言。失败试验可以经用户明确选择“按此期望保存”，保存的是断言意图，不能把失败结果标成通过；无合法目标或类型错误禁止保存。

保存请求带 requestId，只有服务端成功落入会话后才关闭编辑器并展示“已保存 1 条断言”。错误保留表单、给错误码及可读修复说明。重试相同 requestId 不重复新增事件；返回 sequence，方便从录制时间线核对。

### 3.4 结果验证绑定

在录制中发现 drag/scroll 缺结果 verifier 时，面板提示具体动作序号、动作描述、对应窗口与“选择结果组件”按钮。提示依据现有真实事件和绑定情况生成，最终 ready 仍由编译器决定。

从“为该动作添加验证”进入时显式携带该动作的 causalId/sequence，而不是读取提交瞬间的全局 last action。保存时重验动作仍存在、会话一致、支持绑定；作用窗口按断言选中窗口确定，不能拿动作源窗口替代新打开的结果窗口。普通新增断言默认独立；只有用户确认绑定才写已有 causalId。当前结果不能证明滚动/拖拽有效时，如实保持待验证。

录制停止后禁用拾取及活动会话保存，显示“此录制已停止；请在重新录制时添加验证”。本轮不冒充支持事后插入某步、修改历史 golden；如后续需要，另设计版本化离线编辑。

## 4. 分层与函数依赖

下表新增名字均为拟实现 API，不是已存在的函数。允许 ZCode 基于实际代码合并命名，但须在阶段报告给最终路径/函数映射，并保持分层。

| 文件/模块 | 函数或类 | 职责与依赖 |
|---|---|---|
| 新 `target/recording/ui_controller.py` | `RecorderUIController.show()` / `hide()` / `snapshot()` | 服务级面板生命周期；视图态与 CaptureSessionState 分离；从 RecordingService 读取事实 |
| 同模块 | `list_recordable_windows()` / `focus_selected_window(selection_id)` | 复用 backend 窗口列表与现有 connect/lock/verify 身份规则；不复刻一套进程匹配 |
| 同模块 | `start_from_selection(selection_id, name)` | 预检交互桌面、唯一归属、现有会话及持有者，再调用服务 start；同名产物自动唯一后缀 |
| 同模块 | `apply_ui_request(request)` / `complete_ui_request(request_id, result)` | 管理命令分发、ACK、requestId 去重；接回 UI 主线程 |
| `target/recording/indicator.py` | 扩展 `IndicatorCallbacks`、`SubprocessTkIndicator._consume()`、`_indicator_child_main()` | 从直接打开编辑器改为向会话请求目标；增加双向 response/error 消息；Tk 保持子进程主线程 |
| 同模块或新 `target/recording/recorder_panel.py` | `RecorderPanel.render(snapshot)` / `show_assertion_wizard(draft)` | 只负责界面、中文提示、禁用状态及保存 ACK，不直接改 session 私有成员 |
| 新 `target/recording/picker.py` | `PickerController.begin()` / `update_pointer()` / `confirm()` / `cancel()` | 维护临时拾取模式、窗口/组件快照、overlay 与状态恢复；管理输入与业务输入分流 |
| `target/recording/source.py` | `QueuedCaptureSource.begin_pick()` / `end_pick()` / `inspect_at()` | 原生包入队后的管理路由；复用现有 correlator/resolver，所有 UIA/AX 工作在 worker |
| `target/recording/windows.py` / `macos.py` | `resolve_pick_target()`、平台确认/取消键过滤 | 返回完整目标与窗口；不改变既有 action 关联、scope 和源窗口捕获语义 |
| 新 `target/recording/assertion_draft.py` | `build_assertion_draft()` / `available_assertion_types()` / `validate_assertion_draft()` / `render_assertion_summary()` | 类型化、可测的纯逻辑；区别 text/value/boolean；沿用已有断言类型及时间解析 |
| 同模块 | `preview_assertion(draft, reader)` | 复用现有回放/verifier 比较逻辑或抽共享纯函数，禁止写第二套含义不同的比较器 |
| `target/recording/session.py` | `request_assertion_editor()`、`add_assertion()`；新增 `commit_assertion_draft()` / `pending_verifiers()` | 会话内校验、显式窗口/动作绑定、原子保存、更新租约/计数；兼容旧 payload |
| `target/recording/service.py` | 新 `ui_snapshot()` / `open_ui()` / `get_result(session_id)` | 活跃状态无 session 时也可读；按 sessionId 查询停止结果，不返回别人的会话 |
| 新 `target/recording/result_store.py` | `save_stopped_recording()` / `load_stopped_recording()` / `mark_collected()` | 原子持久化 source-redacted raw 与截图，服务重启可恢复；现有严格 schema 不变 |
| `target/server.py` | 新 MCP `open_recording_ui()` / `get_recording_result(session_id)` | 复用既有连接认证、GUI冲突门及序列化错误；旧 MCP 工具签名保持兼容 |
| 新 `agent/recording/collection.py` | `collect_recording_artifacts(agent, result, output_root, profile)` | 抽取 CLI stop 已有抓图、hash 校验、compile、write_compilation_artifacts；CLI 与 UI bridge 共用 |
| 新 `agent/recording/ui_bridge.py` | `run_recording_ui_bridge()` / `poll_recording_ui()` / `collect_finished_session()` | 确定性轮询、会话绑定、租约、停止结果下载、重连、退出；不依赖模型会话存活 |
| `agent/cli.py` | parser 新 `record ui`，`_cmd_record()` 分发 | 打开目标 UI 并运行 bridge；旧 start/status/assert/stop/compile 保持 |
| 新 `scripts/open_recorder.py` + 安装说明 | `main()` / `show_existing_recorder()` | 双击入口；复用安装配置，已运行就显示同一面板，不开启重复捕获 |

依赖链：

```text
桌面入口 / agent record ui
  → open_recording_ui → RecorderUIController → RecorderPanel（Tk 子进程）
  → 选择窗口 / 开始 → 既有 connect/lock/verify → RecordingService → RecordingSession

选择组件 → PickerController → source 管理队列 → UIA/AX resolver → frozen draft
  → 中文向导 → 类型/归属/观测校验 → session 原子保存 → requestId ACK → 时间线反馈

停止 → 源脱敏 result_store → Agent bridge 按 sessionId 收集
  → collection → compile_recording → write_compilation_artifacts → HTML/Markdown 状态报告
```

## 5. 运行期数据、并发与生命周期

`WindowSelection` 为运行期 DTO，至少包含 selectionId、nativeId、PID、processName、root identity、generation、title、backend、interactiveSession。用户选择不等于已获取独占录制权；start 时仍执行唯一会话门与窗口复验。

`AssertionDraft` 为运行期 DTO：draftId、sessionId、windowSelection、observedTarget、observedAt、readStatus、protectionStatus、assertionType、typedExpected、timeoutSeconds、可选 binding(sequence/causalId)、expectedWasEdited。每个 draft 保存自己的窗口及动作，禁止使用共享 `_assertion_window` 在两个编辑器之间串目标。已有 `ObservedTarget` 不必增加 UI 专用字段；checked/enabled/visible 的读状态可放 DTO 中。

UI 状态为 IDLE/SELECTING/RECORDING/PAUSED/PICKING/EDITING/STOPPING/STOPPED/ERROR；PICKING 与 EDITING 是视图/管理子状态，不改 raw schema 中会话状态枚举。明确允许 PAUSED 会话通过管理提交路径保存用户确认的断言，原生业务输入仍不接受；不能先偷偷 resume 再保存。该行为修正编辑器可打开但 ingest 拒绝断言的不一致，须加暂停保存与暂停输入丢弃的对照测试。取消回到进入前状态，停止优先于未完成的拾取/保存；迟到请求收到 session_stopped，不能复活会话。

目标 UI 与 Agent 桥不能互相无限续租。每次已确认的人机操作更新租约；bridge 仅在自身存活、面板仍附着且当前 sessionId 一致时续租；目标端 PID 活着或 Tk 定时器转动不算用户活动。无桥且无用户操作仍按原 TTL 过期，面板在 <60s 和已过期时显式提示。Agent 退出/断连不立即丢 raw，目标端 result_store 保留可恢复结果；新录制不得覆盖尚未下载的旧结果。

持久化使用专用结果目录和随机 sessionId 子目录，原子写入、hash 校验、限制文件名及路径；不保存凭据、不落未脱敏帧、不因读取失败伪造完整证据。默认不自动删除未收集结果；列出占用，清理由用户主动触发。Agent 本地使用唯一录制目录，并在 `result-report/<本地时间到毫秒>/` 输出录制摘要及阶段验证报告。面板显示目标端缓存位置和 Agent 落盘状态，二者分开。

## 6. 实施阶段与验收门

各阶段顺序执行，只允许一个执行器写本仓库。每阶段完成先更新 progress 与报告，再进入下一阶段；失败可在本阶段最小修复，仍失败则停止后续阶段并报告。不得把“已编写测试”替代“实际通过测试”。

| 阶段 | 交付 | 最小验收（完成才勾选） |
|---|---|---|
| U0：管理与保存链路 | collection 抽取、UI ACK、会话绑定 result_store、服务级 UI lifecycle 骨架 | 原 CLI start/stop 兼容；保存错误可见且表单保留；重复保存只落一条；停止后结果可恢复；不同 session 不串 |
| U1：可见入口 | 双击入口、`record ui`、窗口列表/定位、开始暂停停止、状态与下载反馈 | 不经 LLM 能开面板并完成录制；重复打开不重复钩子；歧义窗口要求选择；旧 HWND 不误聚焦；停止并收集后显示真实路径 |
| U2：拾取与向导 | 高亮拾取、组件列表、中文断言、类型化期望、当前值/试验/保存 | 默认界面无需 ID/JSON；不会点击业务开关来选组件；数值文本仍字符串；false/空值区别 unknown；异常不会试验通过；ACK 后才计数 |
| U3：绑定与使用验收 | drag/scroll 指定动作引导、集成报告、截图与操作说明 | 绑定准确 causalId/窗口；录制器 UI/按键不进入 trace；窗口切换与取消不串；独立断言与绑定断言编译正确；缺 verifier 继续 incomplete |

有支持的平台才能标完成。至少 macOS 本地真实 Tk 交互验收及 Windows 测试夹具/平台专用单测；11.26 未部署或不可交互时 Windows 实机项标“待验”，不能用 mock 测试冒充用户路径完成。需要升级目标端部署时单独记录具体部署范围与证据，不在无人值守任务里覆盖当前运行服务。

## 7. 测试矩阵

沿用 pytest。优先测试会导致误录、错目标、假成功或丢录制的行为；不为每个 label 写机械测试。

1. 入口与窗口：空闲可见、重复打开幂等、服务未运行/无交互桌面、标题相同但 PID/根不同、窗口销毁重建、启动预检失败后可重新选择。
2. 拾取：管理键被消费、业务控件状态不变、picker 输入不落 raw、RDP 坐标/DPI 与多屏负坐标、高亮穿透与不遮挡、scope 外拒绝、UI 自身拒绝、超时/取消/断连清理及前态恢复。
3. 断言类型：text `123`、`true`、空串保持字符串；value 缺失不等于空；布尔 false；checked 能力缺失；受保护值；重读不覆盖已编辑期望；每种对外类型的保存→编译→现有回放语义一致。
4. 异常与 ACK：读取抛错必须 unknown/error；不满足可保留但不报通过；非法类型禁止保存；保存失败保留表单；双击同 requestId 只加一次；同一会话两份 draft 不串窗口；stop 与保存并发。
5. 绑定：明确绑定旧动作后又来新动作，不误绑 last；主窗操作打开子窗后对结果子窗断言；没有动作不能绑定；跨会话/过期 draft 拒绝；未绑定 drag/scroll 不被自动标 ready。
6. 收集与回归：先 UI stop 后 Agent 重连能收集、raw 与截图 hash 不符可见失败、采集失败不写成功状态、产物命名不覆盖、既有 CLI stop 与 bridge 共用同一收集行为。
7. 真实 GUI：在可控示例窗口中选择文字、输入框和复选框，各保存一条断言；暂停/恢复；一次取消；一次错误期望试验。每个场景记录操作 trace、组件/窗口身份、预期/实际、结果截图和报告链接。尽量不操作真实 EDR 配置或写业务数据。

每阶段运行相关专项；U3 再跑录制回归集，确认未改回放 gate。涉及共享契约时跑对应检查；旧 SHA 漂移等既有失败独立记录，不重冻基线或宽松化 matcher/verifier。全仓测试只在实际改动面需要时运行。

## 8. ZCode 定时执行与阶段呈现

本需求是新的、明确授权的录制易用性批次，不恢复先前已收口的 AW、跨仓 oracle 或 DSH 功能任务。原生 ZCode 创建一个一次性任务，默认下发后 30 分钟启动，U0→U1→U2→U3 顺序执行；不创建四个可能并发抢文件的定时作业，不无限循环开发。

每阶段产物写到仓库 `docs/todo/recording-usability-progress.md`，同时在 Agent 本地 `result-report/<时间>/recording-usability/` 输出 `U0.md`…`U3.md`、可读 HTML 索引、截图和必要原始日志。截图与所属步骤/阶段放在一起，注明采集时机（初始化/操作后）、窗口及断言；没有真实截图就标缺失。可复用前一步 after 作为下一步 before，但必须引用同一证据及时间，不能冒充重拍。

阶段记录必须含：阶段状态、实际函数/文件、原有 WIP 与本轮差异、测试命令/结果/耗时、可点击证据、用户可执行的入口、平台验证范围、未完成项。执行器每阶段在 ZCode 对话中发一次中文阶段结果，明确“实现 / 离线验证 / 实机验证”的区别。

监督任务每 10 分钟读阶段记录及执行状态。阶段完成时进行独立复核并在当前聊天呈现成果/报告；无变化静默。已复现的本批次缺陷可在原执行器空闲、无排队时单次接续最小修复；运行期间只观察，不插入并发改写。全部完成或确认需要用户决定后停止此批次并暂停监督。不能把 ZCode 自报完成直接当独立验收。

## 9. 保护已有成果

保留既有 WIP；不自动切分或提交别人的修改。禁止为本需求修改历史 golden、降低 ready/verifier/视觉唯一性门、伪造 oracle、重冻契约基线。保持 Tk 子进程主线程和原生 hook 快速返回约束。不自动 git push、部署远端服务、改 EDR 策略或恢复其他已收口开发路线。以上是本轮任务边界，不影响用户后续明确授权。

## 10. 验收清单

- [ ] U0：ACK、会话隔离和结果收集真实可恢复。
- [ ] U1：存在可见启动入口与“定位窗口”，无需自然语言回合。
- [ ] U2：可选中并高亮组件，能读懂/配置文本、值、布尔断言。
- [ ] U2：成功/失败/unknown、非法输入、保护数据和重复提交正确。
- [ ] U3：结果断言准确绑定指定动作；缺 verifier 的旧门槛保持。
- [ ] 阶段进度、HTML/Markdown、截图与真实执行记录可直接阅读。
- [ ] Windows/macOS 的已验/待验范围分别列清；无人值守未部署项不冒报完成。
- [ ] 独立复核后才将本批次标为完成。
