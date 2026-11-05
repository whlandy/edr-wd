# 录制易用性修复轮 R1：第二次独立验收

日期：2026-10-23，Asia/Shanghai。结论：**仍暂不通过**。ZCode 的修复有实际进展，但尚未满足原设计的完整用户路径。

## 独立验证结果

核对原指定会话最终回复、空输入、无运行/排队后开始验收。HEAD 仍为 `8a5374afff5180041fd9cfe41584191ec3790fc7`，修改未提交。本轮只写验收材料，不改业务代码、不操作或部署 11.26。

| 检查 | 独立结果 | 含义 |
|---|---|---|
| 上轮原 11 项反例脚本 | 11 passed / 3.86s | 缺失保存方法、列表渲染、布尔控件切换、草稿重试、stop/save 竞态等原场景已翻转 |
| `test_case/test_recording/` 默认集 | 606 passed, 1 deselected / 36.84s | 当前录制回归集保持通过 |
| 本轮新增下游契约反例 | **10 failed / 0.65s** | 启动失败退出码、真实窗口数据、跨进程保存回执、截图持久化与 unknown 比较仍有缺陷 |

ZCode 自报 2150 passed 的默认全仓结果本轮未重复运行，不作为独立验证数字。五个核心受审文件的 SHA-256 在复验前后保持一致，`git diff --check` 通过；后续修复产生的新快照需重新验收。

本轮反例：[test_r2_acceptance.py](../../result-report/2026-10-23_00-05-33/recording-usability-review-r2/test_r2_acceptance.py)；[反例 JUnit](../../result-report/2026-10-23_00-05-33/recording-usability-review-r2/counterexamples.xml)；[录制回归 JUnit](../../result-report/2026-10-23_00-05-33/recording-usability-review-r2/recording-suite.xml)。

```bash
cd /Users/edr-test/ai-projects/edr-wd
python3 -m pytest result-report/2026-10-23_00-05-33/recording-usability-review-r2/test_r2_acceptance.py -o addopts= -q --tb=short
```

这些是生产函数/真实协议形状的离线验证；widget adapter 不操作桌面，Client stub 只控制连接结果，不触发真实 MCP 服务或业务 UI。本轮未新增 GUI 截图，不声称已完成 live 交互验证。

## R2-A / P1：启动失败返回 0，使原入口测试出现假绿

位置：`scripts/open_recorder.py:51,78,86`。

异步 Client API 的修复是正确方向，但外层调用忽略 `_open()` 返回的 code；服务不通、服务端明确拒绝、连接异常都改为 return 0。三个独立反例均打印“未能打开/打开失败”却退出成功。上轮入口测试只断言 main 返回 0，现会因这种错误语义通过，而非证明面板已打开。

修复：保持成功为 0，失败为非 0；返回实际 code，异常分支明确失败。测试分别覆盖成功、不可达、服务端拒绝与异常，断言退出状态及面板打开事实。打印启动指引不能代替操作成功。不能通过把错误路径改成成功来满足旧测试。

## R2-B / P1：真实 backend 仍拿不到进程名；前置失败也继续 start

位置：`target/recording/ui_controller.py::_process_name_for_pid/focus_selected_window/start_from_selection`。

R1 去掉 `pid:N` 后，若条目没有 process_name/Name 则返回空。生产 `WindowsGUI._win_info()` 返回 title/process_id/handle/visible 等，**没有 process_name**；生产 backend 也没有 `process_name_for_pid`。macOS 旧 list_windows 同样没有该字段。使用生产条目形状的反例得到 processName 空串，无法满足 `RecordingService.start` 的必填进程名。上轮针对人为补入 process_name 的测试通过，不能代表生产契约接通。

同时 `start_from_selection` 只调用 focus 而不检查其返回结果。反例中 activate_app 已返回 ok=false，后续 service.start 仍被调用且外层成功。按进程启动已注册应用也不等于按指定窗口身份前置。CLI 的 connect/lock/verify 链路并未在新入口复用，不能仅改报告措辞视为修好。

修复：从真实平台进程/窗口接口解析进程名并明确失败；复用既有按 HWND/PID/根身份连接、锁定、验证规则，聚焦失败不得捕获录制；窗口候选有歧义时保持拒绝。补生产 adapter 返回结构的测试，不再让 fake 多出生产没有的方法/字段。

## R2-C / P1：跨进程保存尚未确认，子进程就发成功 ACK；ACK 不校验 requestId

位置：`target/recording/indicator.py:382–414`、`_indicator_child_main()`。

真实子进程的 add_assertion callback 只是 emit JSON，返回 None。新 `_submit_assertion` 把任何非 Mapping 返回值视为 `{"ok":true}`，在父进程提交之前就把成功 ACK 放入自己的队列并关闭表单。父进程若随后拒绝，该表单已销毁，错误无法反馈。独立反例只发送请求、不提供父 ACK，表单仍被 destroy。

另一反例向正在编辑的表单注入其他 requestId 的成功 ACK，也会关闭当前表单；`_apply_assert_ack` 没有请求身份校验。服务端仍直接调用 `session.add_assertion`，并未如报告所述走 `commit_assertion_draft` 的冻结草稿成员校验。

修复：子进程发送请求后只进入 pending，成功/失败都来自父进程实际提交回执；ACK 必须匹配表单 requestId；不把 None/未返回当成功；错误保留表单。GUI 保存通过冻结 draft 的受控提交路径，不绕过会话/窗口/动作归属校验。补延迟 ACK、父端拒绝、错 ID、重复及停止后迟到 ACK。

## R2-D / P1：PNG 持久化写法必然报错，恢复/收集确认仍未连通

位置：`target/recording/result_store.py:47,120`、`RecordingService._persist_captures/capture`、`agent/recording/ui_bridge.py::_collect_one`。

`save_capture` 将 PNG bytes 传给以文本模式打开的 `_atomic_write`；独立反例报 `TypeError: write() argument must be str, not bytes`。service 吞掉该错误后仍报告 resultStore.ok=true/captures=0，不能据此宣称截图随 raw 已持久化。

恢复链也仍有静态断点：`get_recording_capture` 只从当前内存 evidence_store 取，未接已保存文件；`get_result` 只返回 raw/meta；`load_captures` 没有接入这条 Agent 收集路径。重启或新录制清缓存之后，增加一个写文件函数不足以恢复取证。

独立的收集确认反例还表明：collector 已返回 recording_capture_fetch_failed，bridge 仍调用 mark_recording_collected。缺证据结果会从 pending 中消失。历史 pending 收集调用的 mark 缺省为 False，也与“成功后标记全部已收集”的报告不一致。

修复：实现二进制原子落盘与 hash 校验，持久化失败显式呈现；按 sessionId/捕获 ID 提供可恢复的取帧链；仅完整验证 raw/必要截图并落盘后才确认收集，失败保留 pending；补停止→重启/新会话→恢复下载→hash→标记的端到端验证。

## R2-E / P1：未知勾选状态仍能被预演判为通过

位置：`target/recording/assertion_draft.py::preview_assertion`、`indicator.py::_run_trial`。

布尔分支用 `bool(actual) == bool(expected)`。向导现在将 frozen observed.value 直接作为所有类型的实际值，并以“组件有身份”决定 readStatus=ok。checked/enabled/visible 的实际能力读取并未对应进去。

独立反例为 checked=false、actual=None，结果是 `pass`。同理非空字符串也会被当作 true。这违背“读取失败/不支持不得通过”的原设计，不是文字改为“本地预演”就能免除的比较语义问题。冻结观测试算也不能冒充对用户选中组件的实时验证。

修复：按类型读取并校验真实布尔值，缺失/未知/错误返回 undetermined；复用回放的观测/比较语义，避免第二套宽松比较器。若仅提供冻结值试算，UI 清楚区分、按观测时间标注，实时试验和重新读取能力仍列未实现，不能宣称需求完成。

## R2-F / 未交付需求：可视化选择与结果绑定链仍缺核心步骤

面板新按钮只调用 assertion()，即在指针已经移到录制面板后做当前命中测试，失败取 last_observed_target。指引“把指针指向业务组件后再点击此按钮”并不能在点击面板时保留那个组件；没有显式的选取、高亮、候选列表与确认过程。

报告诚实列出了 F8/Esc、overlay、组件搜索 UI 未实现，这比先前标“待验”准确，但它们属于原需求，而非可忽略的实机测试。设计允许原生键不可用时采用组件列表；目前两种可用路径都没有。结果绑定按钮仍只 begin_pick，没有生产 confirm/freeze/editor 闭环。

接续范围：在原 U2/U3 内至少交付一种真实可操作的“选择组件/文本→确认身份→编辑→服务端保存”路径，优先可访问组件列表方案；从指定动作进入时完整绑定 causalId 与结果窗口。不能用更改完成定义来收口。

## 下一步

本轮可以认可旧反例对应的局部修复，不能认可 U0–U3 整体通过。将 R2-A–E 已复现缺陷及 R2-F 原需求缺口交回同一 ZCode 会话，在原范围内最小修复；先消除假成功/缺证据，再完成可用的组件选择与绑定用户路径。禁止改独立反例消红、弱化安全门、提交推送或无人值守部署 11.26。完成后重新固定快照、复跑原 11 项及本轮 10 项，结合真实用户路径证据独立验收。

已确认原会话空闲无排队后单次发送 R2-A–F；界面确认新用户轮次、输入清空、“工作中”及“停止生成”。本轮已接续执行，未新增自动化或并发任务；监督未修改业务实现。修复完成后再验收，当前结论不代表返工中快照的最终状态。
