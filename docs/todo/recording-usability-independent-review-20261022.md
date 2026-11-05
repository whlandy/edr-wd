# ZCode 录制易用性 U0–U3 独立验收

2026-10-22（Asia/Shanghai）。结论：**暂不通过**。本轮没有修改业务实现，也没有部署或操作 11.26。

## 验收范围与证据

已核对 ZCode 指定会话的最终回复、输入框空闲且无运行/排队，及实际工作树。基线 HEAD 为 `8a5374afff5180041fd9cfe41584191ec3790fc7`，本批次代码仍为未提交工作树；原有 CLI/租约 WIP 保留。

独立运行结果：

- `python3 -m pytest test_case/test_recording/ -q -p no:cacheprovider`：**595 passed, 1 deselected / 32.76s**。
- `python3 -m pytest test_case/ -q -p no:cacheprovider --junitxml=result-report/2026-10-22_23-20-15/recording-usability-review/existing-suite.xml`：**2139 passed, 614 deselected / 57.68s**。
- 新增独立验收反例 **11 failed / 0.75s**。这些测试断言正确的用户行为，失败用于揭示缺陷；未通过修改断言、放宽安全门或替换业务函数来制造成功。

默认 pytest 使用 `unit and not regression`，614 个排除项不能全部称为 live。现有默认集通过证明回归覆盖仍绿，不证明新增 GUI 用户路径完整。

复现入口：[test_independent_acceptance.py](../../result-report/2026-10-22_23-20-15/recording-usability-review/test_independent_acceptance.py)。[反例 JUnit](../../result-report/2026-10-22_23-20-15/recording-usability-review/counterexamples.xml)、[既有集 JUnit](../../result-report/2026-10-22_23-20-15/recording-usability-review/existing-suite.xml)。

```bash
cd /Users/edr-test/ai-projects/edr-wd
python3 -m pytest result-report/2026-10-22_23-20-15/recording-usability-review/test_independent_acceptance.py -o addopts= -q --tb=short
```

验证方法：使用真实生产函数和 JSON 消息结构；Tk 控件用无桌面副作用的 widget adapter 驱动真实表单回调。FastMCP 使用本机实际安装的 Client，仅把端口探测设为可达，错误发生在连接建立之前。stop/save 使用两个真实线程和事件栅栏控制时序。没有把这些离线验证写成真实 GUI 或 Windows 实机验收。

## 已确认的问题

### UX1 / P1：目标端启动入口无法使用当前 FastMCP

位置：`scripts/open_recorder.py:59`。服务可达后调用 `with Client(...)`，但已安装的 FastMCP Client 只支持 `async with`；`call_tool` 也需要 await。

独立复现返回退出码 1，错误为 `Client object does not support the context manager protocol ... supports the asynchronous context manager protocol`。因此“目标机双击入口可用”不成立，不是待实机才能判断的问题。

修复：使用项目已有连接封装或异步 Client，通过同步 main 的明确入口运行；补真实 Client API 的连接契约测试，不只测服务端口关闭分支。

### UX2 / P1：面板刷新回执没有渲染到窗口选择器

位置：`target/recording/ui_controller.py::RecorderPanelHost._consume`、`target/recording/recorder_panel.py:175`。

真实 host 返回 `{"ack": ..., "result": {"ok": true, "windows": [...]}}`，面板仅在顶层含 `windows` 时更新候选，ACK 分支只看错误/目录。复现中后端已返回 1 个候选，面板 `_windows=[]`。即使手工发送顶层 windows，代码也没有给 `_window_box` 设置 `values`；第二个反例的下拉候选仍为 None。

另有同一入口的未接通项：面板“暂停”命令始终发送 pause，PAUSED 时没有改为 resume；收起使用 withdraw，但 host 再次 open 只返回 alreadyRunning，没有命令让子窗口 deiconify。不能标“收起可恢复”。

修复：统一 ACK/snapshot/windows 协议，初始化主动加载候选，真实配置下拉列表且保留当前选择；为暂停/恢复与收起/恢复补状态到控件的集成验证。

### UX3 / P1：窗口列表/聚焦没有对接真实 backend 契约

位置：`target/recording/ui_controller.py:128`、`:165`、`:215`。

`_process_name_for_pid` 依赖 `process_name_for_pid`，但两个生产 backend 均无此方法，测试 FakeBackend 却补了此方法。实际回退 `pid:123` 被当作真实 process_name 用于聚焦/录制。独立反例即使提供已有 `process_name=Fixture.exe`，输出仍为 `pid:123`。

定位调用 `activate_app(app_name=process_name)`，真实 Windows 方法用于注册应用启动，不是按指定 HWND 聚焦；激活返回 `ok=false` 时，controller 仍向面板返回 `ok=true`。反例明确出现 `activated={ok:false,code:registered_app_invalid}` 与外层 `ok=true` 同时存在。macOS list_windows 当前不提供 handle，现有按 handle 的身份匹配也无法区分多个窗口。

此外 `start_from_selection()` 直接 service.start，并没有文案中声称的 connect/lock/verify 链；既有 CLI start 的该链没有被新入口复用。

修复：接入现有真实平台窗口身份/连接实现，规范化窗口契约；processName/PID/HWND/根身份按平台真实来源取；精确前置并复验，失败保持失败，不用 FakeBackend 的专用方法掩盖接口缺失。

### UX4 / P1：断言向导“保存”调用不存在的方法

位置：`target/recording/indicator.py:343`。

真实表单保存回调执行 `self._async(self._submit_assertion, payload, dialog)`，但 `TkRecordingIndicator` 没有 `_submit_assertion`。独立表单回调直接抛 `AttributeError`；不是 ACK 延迟，而是根本无法发送保存。

修复：恢复完整提交/ACK 路径，并让 GUI 保存走会话草稿提交。必须用真实表单按钮→IPC→管理校验→会话→对应 requestId ACK 的测试覆盖。普通 Add assertion 按钮仍直接 `_show_assertion_editor`，未请求 session 准备新目标，也需与 CLI/拾取统一入口。

### UX5 / P1：组件拾取仍是未连接的逻辑层，不能按“仅待 live”标完成

位置：`target/recording/recorder_panel.py`、`ui_controller.py::apply_ui_request`、`source.py`、`picker.py`、`indicator.py::_indicator_child_main`。

代码核对：面板没有普通“选择组件加断言”入口；绑定按钮只启动 begin_pick，生产 UI/native 路径没有后续 update_pick_pointer/freeze_pick 调用，也没有打开拾取结果编辑器的路由。Windows/macOS 原生层没有 F8/Esc 拾取管理接线；没有 overlay 或组件树搜索 UI。`PICK_READ_TIMEOUT=1.0` 只是常量，update_pointer 同步调用 inspect，未实施超时。

子进程 indicator callbacks 没有 read_target，向导“立即试验”始终禁用；即使在同进程注入当前 read_target，它返回 ObservedTarget，而 preview reader 期待 `(status, actual)`，也不是同一契约。当前取指针组件并不等同于对冻结组件重读。

修复：先交付真实可达的“选择→确认/取消→冻结→编辑→保存”链路，不能只测试手工调用 session 方法。若平台原生拾取暂时不支持，提供设计允许的可用组件列表方案，明确禁用未实现模式；缺失代码不能仅写“待人工验收”。

### UX6 / P1：浮窗 Stop 绕过结果持久化，恢复收集不完整

位置：`target/recording/service.py:82`、`::stop`、`result_store.py::save_stopped_recording`、`agent/recording/ui_bridge.py`。

indicator 的 Stop 回调直接 `session.stop("indicator")`，只有 service.stop 才写 result_store。独立反例通过真实 indicator callback 停止后 session 已 stopped，但 store.list_pending 为空；bridge 此后 get_recording_result 会失败。

结果存储只写 raw JSON/meta，没有持久化截图字节；capture 仍从当前内存 evidence_store 读，新录制 start 会 clear。服务重启或新录制后，旧 raw 即使可加载，也不意味着其截图还能下载。bridge 也没有枚举/收集历史 pending 会话或完成后 mark_collected 的路径。

修复：统一停止原因到同一可幂等保存链，保存脱敏 raw 与截图并按 sessionId 校验恢复；收集成功才标记 collected。补 UI Stop→断连/重启→恢复→截图 hash→收集确认用例。

### UX7 / P1：stop/save 竞态可在停止之后追加并谎报保存成功

位置：`target/recording/session.py:625`。

add_assertion 的活动状态校验与 `_append_assertion_event` 不在同一个原子区域；后者只检查 sequence，不再次检查状态或 requestId。受控双线程反例：保存通过初始校验后等待，主线程 stop 得到 0 事件的最终 raw，再放行追加，返回 `ok=true/state=stopped`，会话变成 1 事件。已交出的停止结果与成功 ACK 不一致。

修复：状态、requestId 去重、序号生成、证据附加与追加在同一事务边界内完成；stop 先完成则拒绝迟到保存。同 requestId 并发也必须在提交点复核，不能只在校验入口查缓存。

### UX8 / P2：草稿第一次校验失败后无法更正重试

位置：`target/recording/session.py:749`。

commit 在校验和保存成功之前 pop pending。复现先提交布尔类型错误，再用相同 draftId 更正为合法文本，第二次报 recording_draft_unknown。与“失败保留表单、可修正重试”冲突。

修复：校验失败保留草稿，成功原子追加后再移除；冻结组件/窗口不可由任意客户端字段覆盖，允许修改的类型/期望/超时等字段应明确。

### UX9 / P2：布尔编辑与绑定复选框没有落实用户选择

位置：`target/recording/indicator.py` 的类型切换回调和 `bind_previous`（323 行）。

独立表单反例：从 text_equals 切换到 checked 后只刷新摘要，布尔控件仍隐藏，用户无法选择“否”；勾选“作为上一个动作的结果验证”后该变量没有被读取，生成 payload 的 bindPrevious 仍为 false。绑定反例为隔离后续问题，仅给缺失的提交方法注入捕获 stub，未替换真实表单/校验函数。

修复：类型变更同时切换实际输入控件、预填与说明；保存显式读取用户绑定选择。由指定动作进入的绑定不得被普通 last-action 选项覆盖。

## 阶段结论与后续

| 阶段 | 独立验收 | 主要原因 |
|---|---|---|
| U0 | 部分逻辑有效，整体不通过 | 暂停保存及顺序去重有覆盖；GUI 保存缺方法、stop 持久化旁路、stop/save 竞态 |
| U1 | 不通过 | 本地入口报错、候选不渲染、真实 backend 身份/聚焦契约不接通 |
| U2 | 不通过 | 拾取缺 UI/native 接线；保存失败；类型切换控件、草稿重试与试验缺口 |
| U3 | 不通过 | 缺少完成组件选取/冻结/编辑的用户路径，复选框绑定选择丢失 |

Windows/11.26 本轮未部署，真实 GUI 操作截图没有新增，仍为待验；这不影响以上离线可复现缺陷成立。进度表中各阶段报告的 `../../../result-report` 相对链接多退一级，当前指向 ai-projects/result-report，应改为 `../../result-report`，避免用户点不开成果。

下一轮按 UX1–UX9 的已确认问题交回 ZCode 原会话，要求先复现上述脚本，修复后重新提供用户入口到保存结果的集成证据、阶段状态和准确已验/待验范围。不得仅以 2139 项旧集通过认定本需求通过，不改测试期望或门槛来消红。

已在原“30分钟后：edr-wd 录制易用性 U0→U3 分阶段实施”会话单次送达此轮修复。发送前确认空闲无排队；发送后界面出现新用户轮次、输入清空、“工作中”与“停止生成”。要求先接通 UX1/UX2/UX4 的最小用户闭环，再修剩余已确认问题并报告。未创建新调度，也未自行改业务代码。受审七个核心文件 SHA-256 在独立测试前后保持不变；后续修复中的工作树不沿用本轮测试结果，完成后需重新验收。
