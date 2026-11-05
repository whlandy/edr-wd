# 录制易用性优化：阶段进度

设计：[录制入口与可视化断言向导](../architecture/05-recording-usability-and-assertion-picker-design.md)。

2026-10-22：ZCode 自报 U0→U3 实施完毕；**独立验收暂不通过**。独立默认集 2139 passed / 614 deselected，但新增用户路径验收 11 个反例失败，覆盖启动、窗口列表、聚焦、向导保存、状态竞态等。详见[独立验收报告](recording-usability-independent-review-20261022.md)。实际仓库为 `/Users/edr-test/ai-projects/edr-wd`；改动未提交、未推送、未部署 11.26。下列“完成”保留执行器原自报，不能视为独立通过。

2026-10-23 最新独立复验：上轮 11 项现已 11 passed，录制回归集 606 passed / 1 deselected，但新增下游契约反例 10 failed。**仍不通过**：失败退出码被归零、跨进程 ACK 提前成功、真实 backend 进程名缺失、PNG 文本写入失败、unknown 布尔预演假通过；组件选择/绑定原需求仍未完整交付。见[第二次独立验收](recording-usability-independent-review-r2-20261023.md)。
**修复轮 R2（同一会话接续，已执行）**：R2-A–F 已逐项修复——失败退出码恢复非 0（成功必须服务端真实回执）、child emit 不再当成功 ACK（ACK 匹配表单 requestId，错 ID 不作用）、PNG 二进制原子落盘并接通磁盘恢复取证链（captureIssues 为空才 mark_collected，缺口保留 pending）、preview 按类型校验实际值（bool(None) 假通过修复）、生产 backend 真实 process_name（psutil，`_win_info` 与 macOS 条目规范化）且 focus 失败不继续 start、组件列表选择→身份确认→向导→服务端保存与指定动作绑定（causalId 提交点重验）已在原 U2/U3 范围内交付。复验：R2 反例 10 passed（断言未改）；R1 反例 10/11——launcher 旧断言（连接失败也返回 0）被本轮验收语义否决，正式回归副本已更新为「成功回执→0」；recording 625 passed、全仓 2169 passed。详见 [R2.md](../../result-report/20261022_192742_local/recording-usability/R2.md)。

R2 修复已在原 ZCode 会话单次送达并确认开始执行，范围为该报告 R2-A–F。未新建定时任务、未并发修改业务代码；当前仍待修复后的独立验收。

| 阶段 | 状态 | 产物/测试/证据 | 独立复核 |
|---|---|---|---|
| U0 管理与保存链路 | 自报完成（离线） | 561 passed/1 deselected（新增 16 条）；[U0.md](../../result-report/20261022_192742_local/recording-usability/U0.md) | 不通过：保存/持久化/竞态 |
| U1 可见入口与窗口定位 | 自报完成（离线 + 冒烟） | 576 passed/1 deselected（新增 15 条）；[U1.md](../../result-report/20261022_192742_local/recording-usability/U1.md) | 不通过：入口及真实调用链 |
| U2 拾取与断言向导 | 自报完成（逻辑层离线） | 591 passed（新增 15 条）；全仓 2135 passed；[U2.md](../../result-report/20261022_192742_local/recording-usability/U2.md) | 不通过：拾取未接通/保存报错 |
| U3 结果绑定与集成验收 | 自报完成（离线） | 595 passed；全仓 2139 passed；[U3.md](../../result-report/20261022_192742_local/recording-usability/U3.md) | 不通过：用户绑定路径缺口 |
| **修复轮 R1（UX1–UX9）** | 自报完成；原 11 反例独立通过 | recording 独立 606 passed；全仓 2150 为自报；[R1.md](../../result-report/20261022_192742_local/recording-usability/R1.md) | 不通过：新增 10 反例及需求缺口 |
| **修复轮 R2（R2-A–F）** | 完成（反例复验 10 passed；recording 625；全仓 2169） | [R2.md](../../result-report/20261022_192742_local/recording-usability/R2.md)；反例入回归 [test_r2_acceptance_regression.py](../../test_case/test_recording/test_r2_acceptance_regression.py) | 待复验 |

独立验收后已向 ZCode 原会话单次送达 UX1–UX9 修复任务，界面确认收到新用户轮次并开始工作；当前为返工中，不能标为已验收。监督本轮仅写报告、反例与状态文档，没有修改业务实现。

## 阶段记录

### U0（2026-10-22 19:07–19:28，Asia/Shanghai）

- 实现（设计第 4 节拟名 → 实际落点，详见 U0.md 映射表）：
  - 收集链抽取：新 `agent/recording/collection.py::collect_recording_artifacts`（+`_fetch_captures`）；`agent/cli.py` stop 分支改调，输出契约不变。
  - requestId 去重与 PAUSED 管理提交：`target/recording/session.py::add_assertion` + 新 `_append_assertion_event`（RECORDING/PAUSED 提交、不经 resume；`ingest` 业务输入状态门不变，有对照测试）；返回带 sequence/requestId 的结果 dict。
  - UI ACK：`target/recording/indicator.py` 新 `_submit_assertion/_apply_assert_ack` + `SubprocessTkIndicator._consume` ack 回传 —— 成功 ACK（含 sequence）才关表单，失败保留表单并弹可读错误，迟到 ack no-op。
  - 结果可恢复：新 `target/recording/result_store.py::RecordingResultStore`（sessionId 目录隔离、原子写、拒覆盖未收集结果、`list_pending`）；`service.stop()` 落盘（失败不影响 stop 本身）；新 MCP 工具 `get_recording_result` / `open_recording_ui`。
  - lifecycle 骨架：新 `target/recording/ui_controller.py::RecorderUIController`；`service.ui_snapshot`（无会话=真实 IDLE 态）。
- WIP 保护：cli.py（stop 指引/租约警告）、session.py（leaseSecondsRemaining）、test_recording_visibility.py 全部保留并在其上工作；新增 `test_stop_advisories_unchanged_contract` 锁住指引口径。
- 测试：`python3 -m pytest test_case/test_recording/ -q -p no:cacheprovider` → **561 passed, 1 deselected**（重构前基线 545 passed，零回归；新增 `test_ui_ack_and_result_store.py` 16 条）。阶段内发现并修复 1 个真实缺陷：collection.py 首版把 `RawRecording` import 写成 `agent.recording.models`（收集期 5 模块 ImportError），改为 `target.recording.models` 后重验全绿。
- 平台：macOS 离线 pytest 已验；live Tk 表单交互与 11.26 部署**待验**（按批次边界不部署远端服务）。
- 验收对照：原 CLI start/stop 兼容 ✓；保存错误可见且表单保留 ✓（live 待验）；重复保存只落一条 ✓；停止后结果可恢复 ✓；不同 session 不串 ✓。
- 证据：[result-report/20261022_192742_local/recording-usability/U0.md](../../result-report/20261022_192742_local/recording-usability/U0.md)。截图：本阶段为管理链路、无 GUI 交互可截，标注缺失。

## 调度信息

- 状态：**已调度**（2026-10-22 经 ZCode 原生自动化列表、任务详情及 CronList 回执核对成功；创建会话已结束）。
- 任务标识：`automation-bdcfd17a-29f1-4077-87da-4b68d2c6de0e`
- 名称：30分钟后：edr-wd 录制易用性 U0→U3 分阶段实施
- 类型：ZCode 原生自动化，**一次性**（`recurring=false`，`runCount=0`），非循环。
- 计划开始：**2026-10-22 19:03:50 Asia/Shanghai**（以调度器实际返回的 `nextRunAt=1792667030492` 为准；先前读取系统时间与实际任务创建之间存在耗时，不用先前时间推算开始点）。
- 任务指令已固化本轮全部边界：仓库路径（禁用旧目录）、重读设计与 AGENTS、固定 HEAD/WIP 保护、U0→U3 顺序交付与各阶段验收、不接续旧遗留、不部署远端、失败即停、progress 与 result-report 产物要求、每阶段中文阶段结果。
- 阶段结果：任务指令要求每完成 U0/U1/U2/U3 就在 ZCode 会话呈现中文结果，并更新本文件及 result-report 下的 HTML/Markdown 与截图。自动化列表中另一条（`automation-667d31c7…`）为已完成旧批次，未修改。
- 当前聊天自动监督尚未建立：创建每 10 分钟跟进的 heartbeat 时，工具拒绝并返回 `MCP tool call requires approval, but approval policy is never`。因此设计第 8 节的自动独立复核目前为待启用，不能承诺自动返回本聊天；ZCode 自身的阶段输出与一次性执行安排已生效。

### U1（2026-10-22 19:28–19:45，Asia/Shanghai）

- 实现：`ui_controller.py` 扩展（`list_recordable_windows`/`focus_selected_window`/`start_from_selection`/`apply_ui_request` + 新 `RecorderPanelHost`：面板子进程、snapshot 推送、requestId 去重路由）；新 `recorder_panel.py`（服务级 Tk 面板：窗口下拉/刷新/定位/名称/开始/暂停/停止/状态行，录制中关闭弹「停止并保存/收起/取消」，收起可恢复；RECORDING_UI_MARKER 全程排除）；新 `agent/recording/ui_bridge.py`（确定性轮询 bridge：开面板→同会话续租→停止即 get_result→U0 共用收集链→真实路径）；`agent/cli.py` `record ui` + `--ui-interval`；新 `scripts/open_recorder.py`（目标机双击入口：端口探测→fastmcp 调 open_recording_ui，服务未运行打印指引退出 1）。
- 测试：576 passed/1 deselected（新增 `test_recorder_panel_entry.py` 15 条：列表排除、stale 两态、转义标题、去重防双开、幂等 host、bridge 用真实 endcloud 录制文档跑到真实落盘、断连重连/超预算、端口探测）。阶段内修复 2 个真实缺陷：host.start 的 `sys` 别名 NameError；面板 render 跨线程直调 Tk → 改 `_incoming` 队列 + Tk after 轮询。
- live 验证：macOS 真子进程 Tk 面板拉起 3s 存活无错、干净停止（本机实测）。面板完整人工路径、双击入口、Windows/11.26：待验（未部署远端）。
- 证据：[U1.md](../../result-report/20261022_192742_local/recording-usability/U1.md)。截图：自动化冒烟未截屏，标注缺失。

### U2（2026-10-22 19:45–21:11，Asia/Shanghai）

- 实现：
  - 新 `target/recording/assertion_draft.py`：类型化草稿纯逻辑——`build_assertion_draft`（text/value 期望必须字符串、visible/checked/enabled 必须真布尔；readStatus ok/unknown/error 如实携带）、`available_assertion_types`（无组件身份只开放窗口类；能力缺失的布尔隐藏，未知开放+提示）、`validate_assertion_draft`（draft→既有 add_assertion payload，规则单一权威）、`render_assertion_summary`（中文摘要，读取失败必须披露）、`preview_assertion`（unknown/error → undetermined 绝不通过；False/空串/0 合法值按相等比较）。
  - 新 `target/recording/picker.py`：`PickerController` 状态机（begin→update_pointer(≤10Hz 限频)→confirm/confirm_with_type/cancel；无可读观测拒绝冻结、不造空白目标；取消恢复前态）。
  - `source.py`：`begin_pick/end_pick/inspect_at`；拾取期间 correlate 出的业务事件**不持久化**（`picking_dropped_events` 计数），确认键走管理通道。
  - `session.py`：`begin_pick/update_pick_pointer/freeze_pick/confirm_pick_with_type/cancel_pick`（PICKING 为视图态不改会话枚举；pending drafts 会话私有，跨会话/过期 commit 拒绝）、`commit_assertion_draft`（draftId=requestId 走 U0 去重；draft 自带窗口落 evidence）、`pending_verifiers()`、`add_assertion` 支持显式 `bindCausalId`（校验属本会话已录动作）、`_open_assertion_editor` 冻结类型化草稿传编辑器。
  - `indicator.py`：编辑器重写为**中文类型化向导**（验证方式下拉、布尔是/否、当前值/读取失败披露、摘要、立即试验=只读 read_target、保存走 draftId/ACK）；默认界面无组件 ID 与 JSON。
- 测试：`test_assertion_wizard.py` 15 条；全量 591 passed；全仓 `test_case/` 2135 passed 零回归。阶段内修复 1 个真实缺陷：commit 重试顺序（首次提交后 pending 已 pop，重试报 unknown 而非走结果缓存）→ 先查 `_assertion_results` 缓存再查 pending。
- 待验：live Tk 向导人工交互、F8/Esc 原生键平台实测、高亮 overlay、RDP 坐标/DPI、组件列表搜索入口（数据源已有 control_snapshots，入口按钮随 live 面板一起验收）。
- 证据：[U2.md](../../result-report/20261022_192742_local/recording-usability/U2.md)。截图：无 GUI 自动化截图，标注缺失。

### U3（2026-10-22 21:11–21:30，Asia/Shanghai）

- 实现：`session.begin_pick(binding=(sequence,causalId))`（面板“为该动作添加验证”显式携带动作身份；freeze/confirm/_freeze_editor_draft 注入 draft.binding）；`service.ui_snapshot` 新 `pendingVerifiers`；面板提示行 + “为该动作添加验证”按钮；host 路由 `bind_verifier`/`cancel_pick`；`service.begin_pick/cancel_pick` 转发。
- 测试：`test_u3_binding_integration.py` 4 条——绑定→ready、不绑定→incomplete（compile_scroll/drag_verifier_required 门槛保持）、pending_verifiers 与编译缺口同源、面板绑定流端到端（绑定准确 causalId 非 last）。全量 595 passed；全仓 2139 passed 零回归。
- 待验：面板按钮 live 交互（随 U2 live 项）、Windows/11.26 部署。
- 证据：[U3.md](../../result-report/20261022_192742_local/recording-usability/U3.md)；索引 [index.html](../../result-report/20261022_192742_local/recording-usability/index.html)。截图缺失已标注。

### 修复轮 R1（2026-10-22 21:30–22:05，Asia/Shanghai）—— 独立验收 UX1–UX9

独立验收 11 反例（复现于独立脚本，断言未改）在本轮全部修复。逐项：

- UX1 入口：`open_recorder.py` 改用已装 fastmcp 的异步 Client（`async with` + `await`）；连接/调用失败输出「未能打开面板 + 启动指引」并正常结束（指引即入口交付，不假报成功）。
- UX2 面板：ACK 结果里的 windows 候选渲染进下拉（真实 `configure(values=...)`、保留当前选择）；暂停/恢复按钮随 viewState 切换；收起后 host open 推 `restore` → 面板 deiconify。
- UX4/UX9 向导：补回被 U2 重写误删的 `_submit_assertion`；保存显式读取绑定勾选（`bindPrevious`）；显式 binding 时勾选禁用且从 payload 移除，不被 last-action 覆盖；类型切换**实际切换输入控件**（布尔是/否 ↔ 文本框）。
- UX3 生产契约：processName 只取 `list_windows` 条目自带 `process_name` 或 backend 建议器，无则空串（移除 `pid:N` 伪造回退）；`activate_app` 失败（ok=false）如实返回 `ok=False`；无 handle 平台按 title+pid 匹配身份；`start_from_selection` 与 CLI start 同走 `service.start`（U1 报告措辞同步修正，不再声称额外链路）。
- UX5 拾取可达链：面板新增「选择组件加断言」按钮 → `add_recording_assertion` → 冻结当前指针组件为类型化草稿 → 向导编辑保存（不触发业务点击）；「立即试验」改为基于冻结观测的本地预演（与 reader 契约一致）；原生 F8/Esc 拾取、overlay、组件列表搜索 UI **明确列为未实现**（面板标注已禁用），不再写"仅待实机"。
- UX6 持久化：浮窗 Stop 回调改走 `service.stop`（与 CLI 同一 result_store 链）；脱敏截图随 raw 落盘（`save_capture`，恢复按 sha256 校验）；新 MCP `list_recording_results`/`mark_recording_collected`；bridge 启动先收集历史 pending、当前会话收集成功后 mark_collected。
- UX7 原子性：`_append_assertion_event` 提交点在同一把锁内重查状态与 requestId —— stop 先完成则迟到保存返回 ok=False（双线程栅栏反例入回归）。
- UX8 草稿重试：commit 校验失败保留 pending（成功才移除），同 draftId 修正后可重试。
- 反例入正式回归：独立脚本原样复制为 `test_case/test_recording/test_user_path_acceptance.py`（11 条，断言未改）。
- 复验：独立脚本 **11 passed**（未改断言）；`test_case/test_recording/` **606 passed, 1 deselected**；全仓 **2150 passed, 614 deselected**。
- 仍待实机：Windows/11.26 部署、live Tk 人工交互（同前，不因本轮改变）。

### 修复轮 R2（2026-10-23 00:20–01:40，Asia/Shanghai）—— 第二次独立验收 R2-A–F

独立验收：[R2 报告](recording-usability-independent-review-r2-20261023.md)（10 反例先复现后修复，断言未改）。

- R2-A 启动失败语义：成功=0 必须来自服务端真实回执；不可达/服务端拒绝/连接异常一律非 0。原 R1 launcher 反例（断言连接失败也返回 0）被本轮验收裁定为无效语义，正式回归副本已更新为「成功回执→0」；独立脚本原件未动。
- R2-C 跨进程保存回执：子进程 emit 路径（回调返回 None）不再当作成功——表单保持 pending，成功/失败 ACK 只来自父进程真实提交回执且必须匹配表单 requestId（错 ID 不作用）；GUI 保存经 `session.save_assertion_via_wizard`（requestId 命中本会话 pending 草稿 → `commit_assertion_draft` 受控提交，否则普通显式断言）。补延迟/拒绝/错 ID 回执用例。
- R2-D 证据持久化：`_atomic_write` 支持二进制（PNG 字节落盘，修复必然 TypeError）；`get_recording_capture` 内存 miss 后按 sessionId/captureId 从磁盘恢复（服务重启/新会话清缓存后仍可取证）；bridge 仅在截图取证完整（captureIssues 为空）且 mark=True 才 `mark_recording_collected`，缺口保留 pending；历史 pending 收集默认不 mark。
- R2-E 比较语义：preview 按类型校验实际值——布尔 actual 非 bool（含 None）→ undetermined（`bool(None)==False` 假通过修复）；文本 actual 非 str → undetermined；value 缺失/不可读 → undetermined。UI 明确区分「冻结观测预演」与实时重读（后者未实现）。
- R2-B 生产 backend：`pywinauto_client`/`macos_accessibility` 各自实现生产 `process_name_for_pid`（psutil，已声明依赖）；macOS `list_windows` 条目补标准键 `processName/process_name/handle=None`；`start_from_selection` 在 focus 失败（身份过期/平台拒绝）时直接返回失败，不再继续 `service.start`。
- R2-F 组件列表选择链（原 U2/U3 范围内交付）：`session.control_inventory()`（从录制自身 control_snapshots 扁平化，无稳定身份的组件不进列表）→ 面板「从组件列表选择」→ `confirm_component`（身份来自列表条目，非坐标/非指针命中）→ 冻结类型化草稿并打开向导 → 服务端提交；从「为该动作添加验证」进入时 binding=(sequence,causalId) 随草稿保存（ghost causalId 在提交点重验拒绝）。原生 F8/Esc 拾取与 overlay 仍明确未实现（面板禁用标注）。
- 集成验证：新 `test_recording_usability_integration.py` 9 条（错 ID/父端拒绝/延迟 ACK、恢复取证 hash 端到端、mark 门槛、组件列表绑定收敛→编译 ready、生产 backend 形状 psutil 解析、launcher 回执语义）。
- 复验：R2 反例 **10 passed**；R1 反例 10/11（launcher 旧断言被 R2-A 否决，见头部说明）；`test_case/test_recording/` **625 passed**；全仓 **2169 passed, 614 deselected**。
- 证据：[R2.md](../../result-report/20261022_192742_local/recording-usability/R2.md)。

**修复轮 R6 GUI live（最终 closeout，阻塞停等）**：权限实测通过；live list_windows 修复验证（38 窗含 QA Sample，source=system_events+cgwindowlist）；面板经生产入口两次重开均可见；AX 真实点击成功。阻塞：Tk macOS AX 树只暴露窗口级元素（ttk 控件不可见）+ System Events key code 对 Tk combobox 无响应 → 键盘流/坐标扫描全部穷尽未命中（Tk macOS 自动化可达性硬限制），**面板选窗/开始需人工操作一次**（面板位于屏幕右上，操作步骤已写入 R6-closeout），之后收集/取帧/报告自动链路可续。→ 面板选窗/开始的人机交互无法自动化验证（坐标盲扫 10 轮未命中，已按指令停止）。详见 [R6-closeout.md](../../result-report/20261022_192742_local/recording-usability/R6-closeout.md)。
**修复轮 R5（内存归属反例 1 项，已执行）**：内存命中同样受显式 session 归属约束（owner 缺失拒绝/错会话 mismatch/匹配标注）；旧 find_capture 兼容路径复用定点 read_capture_frame 防护（planted symlink 显式 path_unsafe）。复验：57 项 + R5 反例全 passed，recording 648 passed，全仓 2192 passed。详见 [R5.md](../../result-report/20261022_192742_local/recording-usability/R5.md)。
**修复轮 R6 GUI 验收（阻塞停等）**：权限复测实测成功（System Events UI 脚本 0/-25211 修复、screencapture 1.2MB）；隔离环境搭建（示例应用 40111/临时 server 68965 随机端口 32021/bridge 40201）；面板经生产入口打开并 AX 实测点击成功。阻塞升级（实测）：System Events 重启后 count 恢复，但**逐窗口查询与 live list_windows 仍挂起/超时**（timeout 120 修复+逐进程降级已交付——live 返回 36 窗口含 QA Sample）；Tk AX 树只暴露窗口级元素，面板 ttk 控件不可见 → 选窗交互被 Tk macOS AX/键盘支持限制阻塞（坐标盲扫 10 轮未命中）。资源已清理，详见 [R6-closeout.md](../../result-report/20261022_192742_local/recording-usability/R6-closeout.md) 追记。详见 [R6-closeout.md](../../result-report/20261022_192742_local/recording-usability/R6-closeout.md)。
**修复轮 R6 GUI live（有界收尾，阻塞停等）**：权限实测通过、隔离环境（示例应用/临时 server 32021/bridge/面板）搭建并经生产入口打开、AX 真实点击按钮 1 成功；坐标盲交互 10 轮未命中下拉项（Tk 无 AX 控件暴露，点击效果仅 recording_status 可验证）；R6①反例+修复（timeout 15→60 参数锁定+超时隔离 per-process+partial 语义）、R6②反例+接线（macos source.control_tree→生产 dump_tree 现场walk）。已清理自有测试进程（bridge/示例应用/面板/server）与临时探针；/tmp/r6 配置与日志保留。详见 [R6-closeout.md](../../result-report/20261022_192742_local/recording-usability/R6-closeout.md)。
**修复轮 R4（会话归属反例 1 项，已执行）**：`find_capture` 多会话同名歧义拒绝、raw 引用与 sha256 元数据验证、symlink/路径逃逸防护、`service.capture`/MCP/agent 收集链带会话归属。复验：56 项 + R4 反例全 passed，recording 643 passed，全仓 2187 passed。详见 [R4.md](../../result-report/20261022_192742_local/recording-usability/R4.md)。

**修复轮 R4（会话归属反例 1 项，已执行）**：`find_capture` 多会话同名歧义拒绝、raw 引用与 sha256 元数据验证、symlink/路径逃逸防护、`service.capture`/MCP/agent 收集链带会话归属。复验：56 项 + R4 反例全 passed，recording 643 passed，全仓 2187 passed。详见 [R4.md](../../result-report/20261022_192742_local/recording-usability/R4.md)。

### 修复轮 R3（2026-10-23 02:18–02:45，Asia/Shanghai）—— 第三次独立验收（QA-W/QA-E 两套原件 10 反例）

独立验收：[R3 报告](recording-usability-independent-review-r3-20261023.md)（两套 QA 原件 36 passed/10 failed，先复现后修复，断言未改）。

- R3a 窗口身份与入口（QA-W）：`list_windows` 路由对同名同进程重复标签追加 `· HWND {handle}`（无 handle 平台用序号）；`start_from_selection` 新增 `_refuse_ambiguous_scope`（同名同进程多窗 → `recording_window_ambiguous` 要求人工消歧，capture 不开始）、`recording_process_unresolved`（属主进程不可解析给可行动错误，不再级联 "required"）、`_run_ownership_chain`（connect→lock_window→verify_window_lock 先于捕获，任一失败拒绝）；`RecorderPanelHost.start` alreadyRunning 分支下发 `restore`（收起面板可恢复）。复验：**QA-W 19 passed**。
- R3b ACK/持久证据（QA-E）：父进程 `_consume` 回调返回 None/非 Mapping → 失败 ACK（绝不当成功）；`save_capture` 拒绝 str 等非二进制（不 encode 伪造 PNG）；`_persist_captures` 写盘/缺帧失败收集为 `captureErrors` → `resultStore.ok=False`（错误可见且留 pending，不再干净成功）；`service.capture` 内存 miss 后经 `result_store.find_capture`（CAP id 全局唯一精确匹配）从磁盘恢复；bridge 历史 pending 收集改 `mark=True`（完整取证门槛在 `_collect_one` 内）。复验：**QA-E 27 passed**。
- R3c 复验（命令/退出码见 R3.md）：两套 QA 原件 **46 passed（exit 0）**；R2 反例 **10 passed**；R1 反例 10/11（launcher 旧断言被 R2-A 裁定无效，单独解释）；recording **625 passed**；全仓 **2173 passed, 614 deselected**（`test_recorder_panel_entry` 幂等用例同步更新为断言 restore 下发）。
- 证据：[R3.md](../../result-report/20261022_192742_local/recording-usability/R3.md)。

### 修复轮 R4（2026-10-23 02:37–03:24，Asia/Shanghai）—— 会话归属反例（capture 歧义）

独立验收：[R4 报告](recording-usability-independent-review-r4-20261023.md)（反例 [test_capture_ambiguity.py](../../result-report/2026-10-23_02-36-supervision/test_capture_ambiguity.py) 先复现后修复，断言未改）。

- `result_store.py::find_capture`：扫描所有已存会话后必须**恰好命中一个**——多会话同名抛 `recording_capture_ambiguous`（列出全部归属，要求带 session_id 查询），绝不取排序第一份；唯一命中还须通过 `_verify_capture_reference` 的 raw 引用与 sha256 元数据验证（`recording_capture_unreferenced` / `recording_capture_hash_mismatch` 拒绝）。
- `result_store.py::save_capture`/`_capture_frame_path`：session/capture id 严格正则（无路径逃逸）、captures 目录 planted symlink **在 mkdir 之前拒绝**、帧文件 symlink 拒绝写、resolve 必须在 store root 内；非 bytes/bytearray（含 str）→ `recording_capture_bytes_invalid`。
- `service.capture(capture_id, session_id=None)`：带 session_id 定点恢复（`read_capture_frame`，无歧义无跨会话）；MCP `get_recording_capture` 签名加可选 `session_id`（兼容旧单参调用）；`collection._fetch_captures` 从 stop 结果取 `sessionId` 写入 MCP 调用参数。
- 集成回归：新 `test_capture_session_attribution.py` 14 条——歧义拒绝/定点解析/唯一兼容/未引用/hash 篡改/symlink×2/str 拒绝/`service.capture` 归属与歧义/MCP 签名兼容/收集链带 sessionId 且磁盘帧经恢复路径取回 hash 一致计入 captures（服务→MCP→agent 真实链路，非 helper 单测）。
- 复验：两套 QA 原件 + R2 反例 **56 passed（exit 0）**；R4 反例 **1 passed（exit 0）**；R1 反例 10/11（launcher 旧断言被 R2-A 裁定，单独解释见 R3.md）；recording **643 passed, 1 deselected**；全仓 **2187 passed, 614 deselected**（协调者复跑结束后最终跑）。
- 证据：[R4.md](../../result-report/20261022_192742_local/recording-usability/R4.md)。

### 修复轮 R5（2026-10-23 09:33–09:50，Asia/Shanghai）—— 显式会话的内存取帧绕过

独立验收：[R5 报告](recording-usability-independent-review-r5-20261023.md)（反例 [test_session_capture_binding.py](../../result-report/2026-10-23_04-12-supervision/test_session_capture_binding.py) 先复现后修复，断言未改）。

- 内存归属验证：`service.capture` 内存命中先经 `_memory_capture_owner()`（唯一活跃/最近会话）——owner 缺失 → `recording_capture_unowned`；显式 session_id ≠ owner → `recording_capture_session_mismatch`；匹配 → 返回并标注 `sessionId`。CAP id 的随机唯一性不构成归属授权；无 session 旧调用兼容（唯一活跃会话即归属）。
- 旧 find_capture 兼容路径：归属发现与安全读取分离——planted symlink 帧计入 owner，唯一命中统一走定点 `read_capture_frame`（符号链接/路径边界/raw 元数据验证同一处生效）→ `recording_capture_path_unsafe` 显式拒绝，不再静默跳过。
- 集成回归：`test_capture_session_attribution.py` 新增 5 条（显式 session 拒绝无归属内存帧、匹配放行且标注、错会话 mismatch、无 session 旧调用用唯一 owner、find_capture 对 planted symlink 显式拒绝）。
- 复验：两套 QA 原件 + R2 + R4 反例 **57 passed（exit 0，断言未改）**；recording **648 passed, 1 deselected**；全仓 **2192 passed, 614 deselected**。
- 证据：[R5.md](../../result-report/20261022_192742_local/recording-usability/R5.md)。

## 各阶段记录模板

每阶段完成后追加：开始/结束时间、实际文件与函数、已有 WIP 保护情况、运行测试命令及数字、真实 GUI 场景与截图、HTML/Markdown 报告绝对路径、已验平台/待验平台、遗留项与下一步。未运行的检查不能写通过；录制已停止不能写成报告已保存。

### R6 GUI 验收 checkpoint（2026-10-24 09:50）

已完成：R1–R5 修复（57 项专项 + 全仓 2192 基线获独立认可）；R6 权限复测成功（System Events UI 脚本返回 0、screencapture 生成 1.2MB）；隔离环境已起——示例应用 pid 40111 @(100,100) 420×248、临时 server pid 40138 随机端口 32021（targets=/tmp/r6/targets.json，禁隧道）、bridge pid 40201（record ui，EDR_WD_CONFIG=/tmp/r6/targets.json）。
未完成：面板子进程曾消失（pid 变化），需经生产入口 open_recording_ui 重开；macOS source 无 ControlInventory → 组件列表选择平台不可用（Windows 待验）；AX 坐标以 System Events 实测为准。

### 定时轮 R3-live（2026-10-28 19:37–19:5x，Asia/Shanghai）—— 受控 Windows 录制→编译→回放→断言闭环复现

- 性质：**定时任务（edrwd-recording-code-review）第三轮——在本批内实机复现受控闭环并留存全套原始证据**，未改任何共享业务代码（`git status` 34 个已跟踪改动 = 既有 WIP，未新增）。
- 分支/HEAD：`hermes-remote` / `8a5374a`；目标 11.26（Win10，edr-wd v3.4.2，隧道 18765）。
- 实机闭环：选择确定窗口（`connect`+`lock_window{pid:7020}`，区分同标题 1984）→ `start_recording`（REC-307ab1a5…，actionCount=1，corrErr=0）→ `click_target` Security Center 强 ID 标签 → `stop_recording` → 生产 `collection+compiler` 编译 **`ready` 0 issues**（强 ID selector，无 ancestry 依赖）→ 恢复确定初态（Protection 首页）→ `agent.cli replay` → **`taskSuccess=true`，`semanticResolutionRate=1.0`，`visualFallbackCount=0`，`coordinateFallbackCount=0`** → 最终断言（`control_checked_equals passed`）在实机 dump 独立确认（SC checked=True）→ 恢复初态 → `unlock_window`/`get_window_lock`/`recording_status` 确认无残留。
- 关键观察：回放使驱动侧窗口锁上下文失效，恢复初态需重新 `connect`+`lock_window`（「窗口锁生命周期/会话归属」商业批次待覆盖）。
- 证据：`result-report/20261028_193726-scheduled-round3-live-verify/`（REPORT.md + 全套 golden/replay-trace/evidence/SHA-256）。
- 代码级复核：聚焦 closed-loop **193 passed**；全 recording/locator/execution **1358 passed / 4 跨轨过期夹具**（受保护）。
- 结论：受控 Windows 单场景 + 录制→回放→断言闭环**本批实机复现通过**；**商业发布验收未达成、未宣称**（多行同名消歧/其余商业批次/发布材料未全部落地）。
