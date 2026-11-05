# 区域多组件断言 · 独立审查准备 R1（2026-10-26）

- 编制：C 轨·回归审查（独立审查方，未写任何产品代码）
- 依据：`docs/architecture/06-region-multicomponent-assertion-design.md`（用户已确认 §1–4）、`docs/architecture/05-recording-usability-and-assertion-picker-design.md`、`/Users/edr-test/ai-projects/AGENTS.md`
- 快照：2026-10-25T23:09:54+0800，分支 `hermes-remote`，23 个已改跟踪文件 + 42 个未跟踪新增；A/B 两轨并行改码中，本文所有 file:line **仅供后续对照**，验收时须按彼时快照复核。
- 配套基线：`result-report/20261026-region-assertion-regression/baseline.md`（全量 2263 passed / 614 deselected / 0 failed，recording 分套 718 passed / 1 deselected）。

## 1. 06 §6 验收项分类与建议验证方法

三类标记：**【离线】**离线可测（pytest + fixture/注入时钟）；**【11.26】**需 11.26 实机（真实桌面、真实 UIA）；**【RDP】**需 Windows RDP 专项（快捷键投递、多屏、DPI）。原则：真实操作步骤完成才算通过，离线绿**不冒充**实机通过；同一条内拆分子项分别标记。

### 验收项 1：Ctrl+Alt+Q 实机弹出遮罩；Esc 恢复；不触发业务快捷键、不录入伪动作
- 分类：**【RDP】为主 +【离线】子项**。
- RDP 项（不可离线替代）：在 11.26 RDP 交互桌面内，目标应用（如 EDR 客户端）拥有焦点时按 Ctrl+Alt+Q，观察遮罩弹出；再用有业务快捷键冲突的输入（如在记事本中按 Ctrl+S 场景）确认遮罩期间业务快捷键未被触发；结束后检查 raw trace 无伪动作事件。
- 离线子项：Esc 取消后恢复进入前录制状态、遮罩清理、快捷键释放逻辑，用 ui_controller/recorder_panel 单测覆盖（管理输入不落 raw 的既有断言口径，参照 `test_recording_visibility.py`、`test_key_command_navigation_merge.py`）。

### 验收项 2：多选组件可增删且高亮与身份一致；选区/窗口失效明确失败
- 分类：**【离线】逻辑 +【11.26】真实命中 +【RDP】坐标**。
- 离线：给定 fixture 控制树，验证框选几何→组件集合的映射、增选/取消后的高亮集合与组件身份（含 runtime ID 不固化）一致；窗口销毁/会话失效时返回明确错误而非静默通过。
- 11.26：真实拖拽框选列表容器与多行，核对高亮框与 UIA 命中目标一一对应；主动销毁窗口验证明确失败提示。
- RDP：多屏负坐标与 DPI 缩放下，框选矩形换算后的命中区域不漂移（对照 05 §7-2 既有 RDP 坐标/DPI 测试口径）。

### 验收项 3：第一行不匹配而第二/三行满足时通过；跨行拼接反例失败
- 分类：**【离线】**（纯查询/比较语义）。
- 方法：构造 fixture 列表（3 行，名称/状态/时间分散在不同行），单测断言：同帧行级合取在第 2 行满足时通过；"第一行名称+第二行状态+第三行时间"式跨行拼接必须失败。反例与正例都要断言到"未满足行"的证据输出。对应 B 轨 `test_region_query_replay.py` / `test_region_assertion_model.py`。

### 验收项 4：同名行、排序变化、控件重建、空列表、虚拟列表范围均有可解释结果
- 分类：**【离线】为主 +【11.26】抽验**。
- 离线：同名行去重/歧义、刷新后行序变化重新解析容器、控件重建（新 runtime ID）后目标重解析、空列表的明确"不存在/不可判定"结论、虚拟列表只认当前可访问数据（不得把未加载行当作已搜索、不得擅自滚动刷新）——逐项 fixture 单测。
- 11.26：抽验一个真实长列表（可滚动）确认"只查可见数据、不滚动"的真实行为与离线结论一致。

### 验收项 5：默认 5s/1min 正确显示，分别修改后真实执行生效；边界、超时、解析失败有证据
- 分类：**【离线】显示/语义 +【11.26】真实执行**。
- 离线：摘要文本默认"最近5秒/最多1分钟"、两值独立编辑互不覆盖、单位显示明确；边界值（0s、负值、超长）、超时到点、日期解析失败返回不可判定并附原文——注入时钟单测。
- 11.26：把观察时长改为 10s、内容阈值改为 2s 后真实录制+回放，用回放证据中的时间戳核实两阈值分别生效（而非只改了显示）。

### 验收项 6：首次匹配模式反例覆盖——首次满足、晚于期限满足、短暂满足后消失、完整观察期不存在
- 分类：**【离线】为主 +【11.26】真实期限**。
- 离线：注入时钟驱动四类时序：期限内首匹配通过；期限后才出现→失败；出现后消失→按首匹配模式已通过（须留首匹配证据）；整期不存在→到期才失败（不得首帧无匹配即通过，存在性否定规则见 06 §4）。
- 11.26：真实回放一次"晚于期限满足"用例，核实单调时钟观察期真实生效、向导/预览耗时未扣入观察期。

### 验收项 7：预览、真实录制、真实回放结果一致；证据/绑定/截图/hash 可追溯
- 分类：**【离线】一致性 +【11.26】产物实查**。
- 离线：预览与回放共用同一查询/比较器（不得第二套语义），同一 fixture 下预览结果与 verifier 回放结果一致；动作绑定 causalId/sequence 正确。
- 11.26：真实录制一条列表断言并回放，核对原始采样/匹配证据、动作绑定记录、截图与 hash 链路可追溯且未被脱敏破坏。

### 验收项 8：旧录制兼容、旧入口移除、保存失败保留表单且重试不重复写入
- 分类：**【离线】为主 +【11.26】目检**。
- 离线：旧格式录制编译/回放不回归（以 baseline.md 数字对照）；requestId 去重（同 id 重试只落一条）；保存失败保留表单与用户输入。
- 11.26：实机目检四个旧入口确已消失（见 §4 已裁决口径），Ctrl+Alt+Q 是唯一断言入口。

## 2. 受保护成果核验（只读，file:line 为 23:09:54 快照）

### 2.1 recorder_panel.py `_request` 签名（任务书指定）
- `target/recording/recorder_panel.py:169`：`def _request(self, request_name: str, **args: Any) -> None:` —— **存在** ✓。
- 无 name 形参冲突：首参名为 `request_name` 而非 `name`，业务参数经 `**args` 透传；实查 `recorder_panel.py:110-112` 存在 `self._request("start", selectionId=..., name=self._name_var.get())` 调用——`name` 作为 kwarg 进入 `args`，若首参误命名 `name` 此处将 TypeError。**结论：无冲突回归** ✓（A 轨改造此文件时须保持 `request_name` 命名）。

### 2.2 windows_pywinauto.py 聚焦与 App Paths（任务书指定 ~L659 / ~L688-721）
- `target/automation/windows_pywinauto.py:659-681` `focus_existing_window`：按 HWND 取窗→三重复核身份（pid、标题、进程名 casefold）→set_focus→**聚焦后再次复核身份**，两段均返回 `recording_window_stale`；异常兜底 `recording_window_focus_failed`。docstring 明确"never resolve or launch an application"。**逻辑完整** ✓。
- `windows_pywinauto.py:683-692` `activate_app`：仅接受 App Paths 注册的 exe 名，拒绝 bundle_id/空名（`registered_app_invalid`）；`:694-738` `activate_registered_app`：exe basename 正则校验→HKLM/HKCU 双 hive 查 `App Paths`→路径必须是绝对路径且以该 exe 结尾，否则 `registered_app_not_found`（:720-721）→单窗口聚焦/多窗口 `window_role_ambiguous`/无窗口经 Popen 启动并区分 `registered_app_focus_failed`/`registered_app_launch_failed`。**逻辑完整，未发现快捷改键或路径放宽** ✓。

### 2.3 ui_controller.py 面板宿主加固（任务书指定）
- `target/recording/ui_controller.py:462-463`：启动前 `self._ready.clear()` + `self._last_error = None`（注释明确"previous child's readiness/error must not authorize this launch"）——**存在** ✓。
- `ui_controller.py:479-490`：就绪超时→stop 并抛 `RecordingModelError("recording_panel_unavailable", "recorder panel startup timed out", path="panel")`（:481-484）；子进程上报 `panelError` → `:527-529` 记入 `_last_error` 并置 ready → `:485-490` stop 并抛 `recording_panel_unavailable`。**清理与抛错链完整** ✓。
- 追加：幂等恢复（:448-454，重复打开不启第二个面板进程、withdraw 面板被 restore）仍在。**结论：三项任务书指定成果均未被 A/B 并行改动破坏**。

### 2.4 追加核验（父 session 审计增量第 5 条）
- `target/recording/session.py:268-306` `ingest`：**scope 过滤** `:273-274`（`event.scope != self.scope` 直接拒收）与 **key_down/key_up/pointer_move 丢弃** `:281-282` 均在位；保护值明文防线 `:283-284` 与 sequence 连续性 `:285-287` 未动。新入口尚未破坏该语义 ✓。注意 `:275-280` Shift+Ctrl/Meta+A 旧快捷键仍在（处置见 §4）。
- `target/recording/result_store.py`：`_atomic_write`（:47-64，mkstemp+os.replace）；`_capture_frame_path`（:151-180）symlink/逃逸三重防护——captures 目录 `is_symlink()` 先于 mkdir（:161-166）、帧路径 `is_symlink()` 拒写（:169-174）、resolve 后必须仍位于 root 之下（:175-180）；读路径同样拒 symlink（:186-187）。**R4/R5 成果未被触碰** ✓。

## 3. 文件所有权对照表（以 A、B 任务书为准）

标记说明：✅=快照时刻已存在且归属明确；🆕=任务书拟新增，快照时尚未出现（A/B 轨待产出）。**并发中，仅供后续对照**；验收时以彼时 git 快照与 owner 声明重新核对。

### A 轨（快捷键框选 UI）
| 文件 | 快照状态 |
|---|---|
| target/recording/recorder_panel.py | ✅（未跟踪新文件，已存在） |
| target/recording/ui_controller.py | ✅ |
| target/recording/indicator.py | ✅（:123 "Add assertion" 按钮仍在，待移除） |
| target/recording/region_overlay.py | 🆕 |
| target/recording/region_wizard.py | 🆕 |
| test_case/test_recording/test_region_ui.py | 🆕 |
| test_case/test_recording/test_recorder_panel_entry.py | 🆕 |
| test_case/test_recording/test_key_command_navigation_merge.py | 🆕 |
| test_case/test_recording/test_assertion_wizard.py | ✅（已存在） |
| test_case/test_recording/test_recording_visibility.py | ✅（既有 WIP） |

### B 轨（条件模型/编译回放）
| 文件 | 快照状态 |
|---|---|
| target/recording/region_assertion.py | 🆕 |
| target/recording/assertion_draft.py | ✅（未跟踪新文件，已存在） |
| target/recording/session.py | ✅（已改，见 2.4） |
| target/recording/service.py | ✅ |
| target/recording/models.py | ✅（:192 `_validate_assertion`） |
| agent/recording/compiler.py | ✅（已改） |
| agent/recording/replay.py | ✅（已改） |
| agent/execution/expectations.py | ✅ |
| agent/execution/executor.py | ✅（已改） |
| target/automation/base.py | ✅（已改） |
| target/automation/windows_pywinauto.py | ✅（已改，见 2.2） |
| target/pywinauto_client.py | ✅（已改） |
| target/recording/time_assertions.py | 待核对（快照未单列，验收时定位） |
| test_case/test_recording/test_region_assertion_model.py | 🆕 |
| test_case/test_recording/test_region_query_replay.py | 🆕 |
| test_case/test_recording/test_management.py | ✅（已改） |
| test_case/test_recording/test_session.py | ✅ |
| test_case/test_recording/test_replay.py | ✅ |
| test_case/test_recording/test_u3_binding_integration.py | ✅ |
| test_case/test_recording/test_time_assertion.py | ✅ |
| test_case/test_recording/test_time_range_assertion.py | ✅ |

### 主 session（11.26 操作与汇总）
- 11.26 部署/重启/实机验收（C 轨与 A/B 轨均禁止 SSH/部署/连 11.26）。
- `docs/architecture/06` §7 进展更新。
- `result-report` 汇总。

## 4. 旧入口处置——用户已裁决口径（原"待定项"已收口，2026-10-25 深夜裁决）

后续批次独立核验按此口径执行：

1. **确认移除**四处旧断言入口：① recorder_panel 面板「选择组件加断言」按钮；② 面板「从组件列表选择」按钮（一并下线）；③ 浮窗 indicator「Add assertion」（indicator.py:123）；④ session.py:275-280 Shift+Ctrl/Cmd+A 内嵌快捷键。Ctrl+Alt+Q 为唯一断言入口。
2. **确认保留**：MCP `add_recording_assertion`（server.py:1170，CLI `record assert`）。
3. 核验方法：实机目检四处 UI 入口消失（验收项 8）+ 离线断言旧入口代码路径已删、相关旧测试同步更新而非删除掩盖。

## 5. 断言类型枚举 6 处同步检查表（父 session 审计增量第 3 条）

新增/修改断言类型时，以下 6 处必须逐一同步，验收时逐处核对 file:line 与语义一致（行号为快照参考）：

| # | 位置 | 快照锚点 |
|---|---|---|
| 1 | target/recording/models.py `_validate_assertion` | :192 起（目标端类型表） |
| 2 | target/recording/session.py `add_assertion` 类型表 | 验收时定位（会话侧白名单） |
| 3 | agent/recording/models.py `_validate_verifier` 与 `_strict` allowed 集 | :38 / :21（Agent 端 verifier 校验） |
| 4 | agent/recording/compiler.py | 编译期类型分发 |
| 5 | agent/recording/replay.py `_verifier_expectation` | 回放期期望解析 |
| 6 | agent/execution/expectations.py | 求值器注册表 |

任何一处缺失即判"编译/回放断链"，不接受"UI 能保存但回放不消费"的字段。

## 6. QA 原件位置（父 session 审计增量第 2 条更正）

- 两套 pytest 原件：`result-report/2026-10-23_zcode-parallel/qa-window/`（QA-W）与 `result-report/2026-10-23_zcode-parallel/qa-evidence/`（QA-E）。
- R1–R5 反例已沉淀为 `test_case/test_recording/test_user_path_acceptance.py` 与 `test_case/test_recording/test_r2_acceptance_regression.py`（快照确认存在）。后续批次独立复验时纳入对照。

## 7. 反"离线绿冒充实机"审查要点清单

1. **不得手改 golden**：任何批次的回归数字必须与 `result-report/20261026-region-assertion-regression/baseline.md` 同口径对比；发现 golden 文件 diff 即拦下（05 §9、06 §3 明令）。重冻契约基线、宽松化 matcher/verifier 同罪。
2. **求值器不可判定不得记通过**：读取失败、日期缺失、时区不明、时钟偏差无法确定时必须输出"不可判定"并保留原文/证据（06 §4）；读取异常不得进入比较器，false/空串/0 是合法值不得 truthy 丢弃（05 §3.3）。审查时专查 try/except 是否吞错转"通过"。
3. **离线 stub 与真机差异点（下述三项只认实机证据，离线绿不算数）**：
   - **RegisterHotKey 真实投递**：离线只能验证注册/取消回调逻辑；RDP 会话中快捷键可能被系统占用、其他进程抢占、需交互桌面焦点，注册失败路径（必须明确告知、不得悄悄改键）只有实机可复现。
   - **pywinauto 真实行查询**：离线 fixture 是静态控制树；真实 UIA 有缓存/刷新语义、虚拟列表只暴露当前可见行、控件重建后 runtime ID 不稳定——行级同帧合取必须对真实快照验证。
   - **RDP 焦点**：`set_focus` 在 RDP 断开/锁屏/Session 0 下行为不同；多屏负坐标与 DPI 缩放影响框选几何；`focus_existing_window` 的聚焦后身份复核只有在真实窗口重建时才能被触发验证。
4. **通过口径**：每条验收项按 §1 标记分别出具"实现 / 离线验证 / 实机验证"三种状态；【11.26】【RDP】项在未上机前一律标"待验"，不得以离线单测截图或日志冒充实机通过（05 §6、§8 同口径）。
5. **并发改码纪律**：A/B 并行期间本清单所有 file:line 仅供对照；验收每批次前重新采集 git 快照并重跑全量回归与 baseline.md 数字比对。
