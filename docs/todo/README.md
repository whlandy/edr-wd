# EDR-WD 待办事项汇总

所有剩余工作的统一入口。整合自本目录下的三份专题文档，以及全仓库范围内的代码/文档标记。

- 生成时间：2026-08-09
- 基线提交：`f133bf6`（分支 `hermes-remote`，工作区干净）
- 阅读顺序：先读本文件，需要完整设计依据时再打开对应专题文档。

## 校对说明（Reconciliation Notice）

`docs/todo/` 下的三份专题文档最后一次更新停留在提交 `a77bfbc`（P0.3）。此后已有九项实现落地，但文档未同步更新，导致这些文件夸大了剩余工作量。

以下条目在最后一次文档更新之后已经落地——逐项对照代码验证过，不是只看提交标题：

| 条目 | 文档中仍写的状态 | 实际落地情况 |
|---|---|---|
| P0.1 窗口范围原始滚动 | 待完成 | `7bc5dbd` —— `scroll_window` MCP 工具已接受 `window` / `window_title_re` / `expected_process_name` / `expected_pid` |
| P0.2 Windows RDP 前台回退 | 待完成 | `8d19910` —— `target/automation/windows_pywinauto.py` 中的 Win32 前台 HWND 路径 |
| P0.3 统一指针结果封套 | 已完成 | `a77bfbc` —— `target/scroll/pointer_result.py` |
| P1.1 CLI 入口命令 | 待完成 | `8fb843d` —— `agent/cli.py` 的 `window` / `scroll` / `page-next` 子命令 |
| P1.2 统一窗口参数 schema | 待完成 | `b6cfa54` —— `target/scroll/window_args.py` |
| P1.3 before/action/after 报告 | 待完成 | `f133bf6` —— CLI `--verify` 证据记录，含 ownership/coordinate 决策块 |
| T2 `page_table` | 待完成 | `ae8e652` —— `target/scroll/page_table.py` + MCP 工具 |
| T3 `scroll_until_visible` | 待完成 | `d2e2dd6` —— `target/scroll/scroll_until_visible.py` + MCP 工具 |
| T4 `drag_target` | 待完成 | `017ee01` —— `target/scroll/drag.py` + MCP 工具 |

另外，根据 `llm-action-id-sequences.md`，P0.1–P3.2 整条链路（动作目录、观测、协议模型、分发器、执行器、trace/证据、恢复、报告、planner、离线评估）也已全部完成。

目前 MCP 暴露的工具已达 40 个，包括 `scroll_region`、`scroll_window`、`scroll_until_visible`、`page_table`、`drag_target`、`execute_action`、`get_action_catalog`。

行动项：更新或归档这三份专题文档，使其不再把已完成的工作标注为待完成——见 W7。

---

## W1. 实机验收（最高优先级）

下面列出的内容均已实现，并有离线/stub 测试覆盖。缺少的是针对真实 HiSec 目标的验证证据。这是当前剩余工作中最大的一块。

注意：在 `test_case/` 中 `grep` 找不到任何通过环境变量门控的实机 pytest。虽然存在手动运行脚本 `test_case/run_windows_hisec.py`、`run_macos_hisec.py`、`run_macos_generic.py`，但没有「无实机则跳过」的约定。写这些测试之前需要先定下门控机制（环境变量或 pytest marker），否则没有实机环境时 CI 会挂红。

### W1.1 Windows HiSec 分页日志

场景：`日志中心 -> 操作日志 -> 查看更早记录`。

- Planner 能识别出这是一张分页表格（而非盲目滚轮滚动）。
- 每次调用恰好触发一次语义化的下一页点击（A020）。
- 首行时间戳或分页标记在操作前后发生变化。
- 报告中展示操作前/操作/操作后三段证据及验证结果。
- 正常路径下不产生临时的 `/tmp/edr_wd_*.py` 脚本。

来源：`scroll-and-paged-table-actions.md`、`window-scoped-scroll-and-verification.md`

### W1.2 Windows RDP 遮挡

场景：另一个窗口遮住了目标滚动区域的坐标。

- EDR-WD 要么安全地恢复窗口归属，要么返回遮挡错误。
- 绝不会误将滚动操作作用到被遮挡的其他窗口上。
- 结果负载中显式标注验证结果为降级状态。

来源：`scroll-and-paged-table-actions.md`、`window-scoped-scroll-and-verification.md`

### W1.3 macOS 通用滚动区域

- 解析并聚焦一个 macOS 可滚动区域。
- 执行一次有界的 scroll-region 操作。
- 验证内容确实发生变化，否则返回 `NO_SCROLL_EFFECT`。

来源：两份滚动相关专题文档

### W1.4 实机 HiSec Planner 场景

对应 `test_case/test_planner_e2e/test_todo_scenarios.py` 中确定性 stub 测试套件的环境门控实机版本：

- 在真实 HiSec 页面上成功执行语义化计划。
- 窗口/控件刷新后树结构过期（stale）。
- 目标歧义，多个控件同时匹配。
- 窗口或进程归属错误。
- 操作序列中途弹出对话框，需要重新观察/重新规划。

验收标准：

- 过期（stale）和歧义目标绝不会退化为未经验证的坐标点击。
- 归属错误必须在触发 GUI 变更之前被拦截。
- 序列中途的对话框会被观察到并重新规划，而不是被直接点掉。
- Trace/证据输出能解释 planner 的每一步决策。
- 现有的 stub-LLM 测试保持通过。

来源：`llm-action-id-sequences.md`

---

## W2. 打包 `mcp.exe`

尚未开工。`packaging/` 目录目前只有 `README.md`、`DESIGN.md` 和 `pyinstaller/README.md`——没有任何构建产物。

按目录约定，缺失的文件包括：

```text
packaging/pyinstaller/mcp.spec
packaging/pyinstaller/build.ps1
packaging/pyinstaller/build.sh
packaging/pyinstaller/smoke.ps1
packaging/pyinstaller/smoke.sh
```

已经确定的约束：

- 输出可执行文件必须命名为 `mcp.exe`。
- 打包根目录下的 `agent/` 和 `target/`；不要打包重复的源码树。
- 使用可移植路径，不能有 Windows 专属的路径字面量。
- 排除测试、本地配置、截图、日志、缓存、生成的报告。
- 包含 FastMCP 运行时所需的元数据。
- 不要原样照搬桌面原型的根级 `mcp-edr-wd.spec`。

来源：`packaging/README.md`、`packaging/pyinstaller/README.md`

---

## W3. 待定设计决策

每一项都需要在 `docs/requirements/DECISIONS.md` 中记录决策，可以在实现前或实现同时完成。

### 滚动/验证语义

- `verify=False` 时：继续返回 `NO_SCROLL_EFFECT`，还是新增 `Reason.UNVERIFIED`？
- 目标已经可见的情况：是新增独立的 `Reason.TARGET_VISIBLE`，还是继续复用 `NOT_DISPATCHED`？
- 强制指定的 `strategy` 与检测到的结构冲突：可能需要新增 `Reason.COMPOSITION_MISMATCH`。
- 横向滚动支持。目前规范化的方向只有 `next` / `prev` / `first`，加上面向工具层的 `down` / `up`。
- 通过 `scroll_region` 向上滚动目前不支持——`agent/cli.py` 中 `--up` 参数标注为「(not supported by scroll_region)」。要么把反向滚动端到端实现出来，要么直接去掉这个死选项，不能留着一个不生效的开关。
- `AutomationBackend.drag(...)`：是否要接受 `expected_process_name`，还是让 `drag_target` 单纯依赖 `verify_window_lock`？

### 验证鲁棒性

- 抖动过滤：瞬时的加载动画/重排变化不应被误判为内容发生了移动。
- 虚拟化列表：行复用可能导致树摘要（digest）保持不变，从而让移动检测失效。

### 证据与报告

- 哪些 HiSec 页面已经测试过逆向操作，哪些只有逻辑层面的恢复（未经实测）。
- 哪些 UI 区域/文本模式需要默认在截图中做脱敏处理。
- 运行报告应突出展示最近一次尝试、第一次尝试，还是两者都展示。

来源：`scroll-and-paged-table-actions.md`、`llm-action-id-sequences.md`

---

## W4. 平台能力缺口

### macOS 无障碍（accessibility）后端

`target/action_catalog/enums.py:109-111` 将以下动作标记为未实现，`target/automation/macos_accessibility.py` 对它们直接返回明确的「not supported」错误：

- `gui.type_text`（A030）—— 没有 AX `setValue` 路径。
- `gui.select`（A031）—— 没有 AX 选择器动作。
- `observe.control_text`（A032）—— 没有 AX 值回读。
- 另外未实现的还有：`dump_tree`（A010）、按 `control_id` / `automation_id` 点击（A020）、`click_target`（A021）。

需要逐项决策：通过 AX 实现，还是明确锁定为后端的永久性限制，并记录在 `references/mcp-tools.md` 中。

### 滚动条滑块坐标

需要为 Windows UIA 和 macOS AX 分别定义平台专属的滚动条滑块坐标提取方式。`drag_target` 目前需要的是滑块（handle）的矩形，而不是滚动轨道（rail）的中心点，这部分提取逻辑是平台相关的。

来源：`scroll-and-paged-table-actions.md`

---

## W5. 恢复策略缺口

在 `agent/execution/checkpoints.py:115-116` 中已声明但未实现：

- `RESTORE_STRATEGY_PROCESS_RESTART` —— 标注为「P2.2 (not implemented yet)」。
- `RESTORE_STRATEGY_SYSTEM_SNAPSHOT` —— 标注为「P2.2 (not implemented yet)」。

`environment_snapshot` 检查点目前只是声明存在，V1 阶段明确排除在范围之外。要么实现它们，要么让「仅声明未实现」这一状态在 API 边界上可见——因为对于没有确定性实现和验证规则的策略，绝不能返回 `restorable=true`。

来源：`docs/requirements/P2-recovery-planner.md:57`、`docs/requirements/P2-recovery-production.md:231`、`llm-action-id-sequences.md`

---

## W6. 安全加固

凭据目前存放在本地 JSON 文件中（`config/targets.local.json`）。三处代码/文档中都留有相同的延后处理标记：

- `SECURITY_NOTE.md:27`
- `CONTRIBUTING.md:57`
- `agent/target_config.py:444`（以及 `:598` 处关于 `auth.password_env` 兼容性的说明）

触发条件：工作流离开可信内网环境时。在此之前属于「已接受的风险」而非缺陷——但应当持续跟踪，不能遗忘。

---

## W7. 文档卫生

- 更新或归档 `window-scoped-scroll-and-verification.md`、`scroll-and-paged-table-actions.md`、`llm-action-id-sequences.md`，使它们不再把已落地的工作列为待完成（参见上文的校对对照表）。
- `docs/README.md` 中将 `docs/todo/` 描述为「历史实现计划与剩余验收项」——应改为指向本文件作为入口。
- 考虑建立一个可重复执行的检查项：任何关闭 TODO 条目的工作，文档同步刷新应作为其完成定义（definition of done）的一部分。

---

## 不做事项（Non-Goals，保持不变）

- 不通过关闭无关窗口来绕过窗口安全机制。
- 不把截图单独作为内容发生移动的权威证明。
- 不硬编码固定分辨率或固定坐标脚本。
- 不硬编码 HiSec 的 `nextPageButton`；使用基于结构的分页检测。
- 默认不向自主 planner 暴露任意 PowerShell 执行能力（`A070-A079` 已保留但未启用）。
- 环境快照（environment-snapshot）恢复在 V1 阶段保持不在范围内。

---

## 建议执行顺序

1. W7 文档校对——成本低，能避免下一位读者重复做一遍这次的审计工作。
2. W1.1 + W1.2 —— 两个 Windows 实机场景，是已上线的滚动/分页能力的验收门槛。
3. W3 滚动语义相关决策 —— 其中几项是稳定实机断言的前提条件。
4. W1.3 + W1.4 —— 剩余的实机验收工作。
5. W2 打包工作 —— 与以上工作相互独立，可以并行推进。
6. W4 / W5 / W6 —— 能力补齐与安全加固工作，当前没有阻塞项。
