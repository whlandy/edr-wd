# TODO：窗口作用域滚动与结果验证

## 状态

待实现。

本文基于一次“在日志中心的操作日志页面向下滚动”的真实执行记录，整理 EDR-WD 的通用修改建议。问题不局限于 EDRClient：任何存在同进程多窗口、窗口遮挡、RDP 前台检测异常、虚拟化列表或分页表格的桌面应用，都可能触发同类误操作或结果误判。

## 现象摘要

执行过程出现了以下问题：

1. 代理先后创建多个 `/tmp/edr_wd_*.py` 临时脚本，重复封装初始化、MCP 调用和结果解析。
2. `dump_tree`、`lock_window` 等工具的窗口参数名不一致，代理多次试错后才找到正确参数。
3. “日志中心”和“华为HiSec Endpoint”属于同一个 `EDRClient.exe` 进程，仅校验进程无法区分具体窗口。
4. `Task Manager` 等窗口可能遮挡目标区域；屏幕坐标正确不代表滚轮事件到达目标窗口。
5. Windows RDP 会话中，`Desktop(...).get_active()` 失败，导致窗口锁校验返回 `Window lock mismatch`。
6. `strict=False` 虽被写入锁状态，但当前 `verify_window_lock()` 未根据该值改变行为，因此不能实现预期的降级。
7. `scroll` 返回 `ok: true` 只说明滚轮事件成功发出，不能证明页面内容发生了变化。
8. 操作日志是分页表格。滚轮最多改变当前页的可见行，要查看更早记录应使用 `nextPageButton`，而不是持续发送滚轮事件。
9. 截图如果没有显式绑定窗口，可能记录的不是本次操作目标，也无法单独证明滚动前后的差异。

## 根因

当前接口把“输入事件已发送”和“用户意图已完成”混为一个成功状态：

```text
目标窗口解析 -> 前台/遮挡校验 -> 输入事件 -> 状态变化验证
       ^              ^              ^             ^
     不统一        RDP 下不可靠     scroll 过于底层   当前缺失
```

`target/automation/windows_pywinauto.py` 中的 `scroll()` 最终调用 `pyautogui.scroll()`。只要调用没有抛异常，就返回 `ok: true`。它没有验证命中窗口、控件归属或滚动后的 UI 状态。因此该返回值只能表示 `event_dispatched`，不能表示 `effect_verified`。

## 修改目标

- 所有指针操作都能明确绑定到窗口和进程，不能依赖“当前鼠标下面碰巧是目标窗口”。
- RDP 前台检测失败时使用可解释、可审计的降级策略。
- 区分事件发送成功、界面变化成功和业务意图完成。
- 对分页表格优先使用语义控件操作，而不是盲目滚轮。
- 常规操作通过 CLI/MCP 直接完成，不再生成临时 Python 脚本。
- 每个动作生命周期自动记录 before、action、after 和验证结果。

## P0：必须修复

### P0.1 为滚动增加窗口作用域和归属校验

扩展 `scroll` 接口，至少支持：

```json
{
  "clicks": -5,
  "x": 416,
  "y": 174,
  "coordinate_space": "window",
  "window_title_re": "^日志中心$",
  "expected_process_name": "EDRClient.exe",
  "expected_pid": 6752
}
```

建议行为：

1. 通过标题、进程和 PID 解析唯一窗口。
2. 将窗口相对坐标转换为屏幕坐标，避免 RDP 分辨率变化导致旧坐标失效。
3. 操作前使用 `WindowFromPoint` 或等价平台能力确认坐标实际命中的顶层窗口。
4. 目标被遮挡时先激活并重新校验；仍不匹配则返回 `target_occluded`，不得发送滚轮事件。
5. HiSec 同进程多窗口场景必须同时校验精确窗口标题，不能只校验 `EDRClient.exe`。

兼容性：保留现有 `scroll(clicks, x, y)`，但将其标记为无窗口保障的低层接口，并在结果中返回 `safety: "unscoped"`。

### P0.2 修复 Windows 窗口锁和 `strict` 语义

当前 `_active_window_state()` 依赖 `Desktop(...).get_active()`，在部分 pywinauto/RDP 环境中不可用。建议增加多级策略：

1. Win32 `GetForegroundWindow` 获取前台 HWND。
2. 通过 HWND 获取 PID、标题和矩形。
3. pywinauto 仅作为补充信息来源。
4. 如果无法获得前台窗口：
   - `strict=True`：返回 `foreground_unavailable` 并阻止操作；
   - `strict=False`：仍须校验连接窗口存在、坐标命中 HWND 和进程归属，满足后才允许操作，并在结果中标记 `verification_degraded: true`。

不要让 `strict=False` 等同于无条件绕过安全检查。

### P0.3 将动作成功拆成三个层次

统一动作结果：

```json
{
  "ok": true,
  "status": "effect_verified",
  "event_dispatched": true,
  "target_verified": true,
  "effect_verified": true,
  "target_window": {
    "title": "日志中心",
    "process_name": "EDRClient.exe",
    "pid": 6752,
    "handle": 66336
  },
  "evidence": {
    "before_snapshot_id": "OBS-...",
    "after_snapshot_id": "OBS-...",
    "changed": true
  }
}
```

状态至少区分：

- `event_dispatched`：系统接受输入事件，但结果未知。
- `effect_verified`：目标控件或画面发生预期变化。
- `no_effect`：事件已发送但观察无变化。
- `target_occluded`：坐标被其他窗口遮挡。
- `target_ambiguous`：窗口或控件不唯一。
- `verification_unavailable`：环境无法验证结果。

代理不得把 `event_dispatched` 直接表述为“滚动成功”。

## P1：语义化操作和证据闭环

### P1.1 新增控件级滚动

优先支持基于 `target_ref` 的 `gui.scroll`：

```json
{
  "target_ref": {
    "snapshot_id": "OBS-...",
    "target_id": "T0042",
    "expected_process_name": "EDRClient.exe"
  },
  "direction": "down",
  "amount": "page"
}
```

执行顺序建议为：UIA ScrollPattern/控件原生滚动 -> 窗口相对滚轮 -> 受保护的屏幕坐标兜底。每次降级都应写入 trace。

### P1.2 识别分页容器并映射用户意图

观察结果中增加容器能力，例如：

```json
{
  "container_kind": "paginated_table",
  "can_scroll": true,
  "can_page_next": true,
  "next_page_target_id": "T0051"
}
```

当用户意图是“继续往下看更多/更早记录”，且当前控件是分页表格时，规划器应优先调用下一页控件；只有“查看当前页下方被遮住的行”才使用滚轮。

建议新增语义动作：

- `gui.scroll`
- `gui.page_next`
- `gui.page_previous`
- `gui.scroll_until`

### P1.3 自动执行 before/action/after 生命周期

每个 GUI 动作默认生成一个步骤生命周期：

1. 第一个步骤执行 `init/before` 截图和结构化观察。
2. 执行动作并记录精确工具参数、目标窗口、坐标空间和降级路径。
3. 自动采集 `after` 截图和结构化观察。
4. 当前步骤的 `after` 复用为下一步骤的 `before`。
5. 对截图差异、滚动位置、首行文本或页码进行验证。

报告中应按步骤把 trace 和截图放在一起，而不是将 Operation Trace 与截图分成两个独立区域。

### P1.4 统一工具窗口选择参数

建议所有涉及窗口的工具统一接受 `window` 对象：

```json
{
  "window": {
    "title_re": "^日志中心$",
    "process_name": "EDRClient.exe",
    "pid": 6752,
    "handle": 66336
  }
}
```

逐步废弃 `title_re`、`window_title_re`、`expected_process_name` 分散在不同层级的用法。工具元数据应给出完整 JSON Schema 和示例，使代理无需读取 `server.py` 猜参数。

## P2：减少代理试错和临时脚本

### P2.1 增加常用 CLI 工作流

已有 `edr-wd --target <target> call <tool> --args ...` 应成为默认入口。进一步建议增加：

```bash
edr-wd --target win-dev window list
edr-wd --target win-dev window inspect --title '^日志中心$'
edr-wd --target win-dev scroll --window-title '^日志中心$' --down 5 --verify
edr-wd --target win-dev page-next --window-title '^日志中心$' --verify
```

CLI 应自动完成结果解包、超时、截图持久化和 trace 写入。正常连接和 GUI 操作禁止写 `/tmp/edr_wd_*.py`；临时脚本只允许人工调试，并必须在结果中标记 `debug_only`。

### P2.2 为工具提供可发现的错误修复建议

错误响应增加机器可读字段：

```json
{
  "ok": false,
  "error_code": "missing_process_guard",
  "message": "HiSec pointer actions require expected_process_name",
  "retry_with": {
    "expected_process_name": "EDRClient.exe"
  }
}
```

这样代理可以修正一次调用，而不是生成新脚本反复试验。

## 验收标准

### 单元/契约测试

- `strict=True` 在无法取得前台 HWND 时阻止动作并返回稳定错误码。
- `strict=False` 走降级验证，且不会跳过坐标命中和进程/窗口归属检查。
- `scroll` 在遮挡窗口下返回 `target_occluded`，不发送滚轮事件。
- 同一进程的两个窗口重叠时，精确标题能选中“日志中心”。
- 无窗口作用域的旧接口明确返回 `safety: "unscoped"`。
- `event_dispatched=true` 且观察无变化时，结果为 `no_effect`，不能是 `effect_verified`。
- 工具 schema 对窗口选择字段保持一致，并包含可直接执行的示例。

### Windows RDP 集成测试

准备两个重叠窗口，其中前台窗口遮挡目标窗口：

1. 连接并锁定“日志中心”。
2. 在表格目标上执行窗口作用域滚动。
3. 验证事件只发送给“日志中心”。
4. 验证 before/after 截图均裁剪到同一窗口。
5. 验证可见首行、滚动百分比或页码发生变化。
6. 打开 Task Manager 遮挡相同坐标，再次执行时应自动恢复前台或返回 `target_occluded`，不得误滚 Task Manager。

### HiSec E2E

场景：“打开日志中心 -> 进入操作日志 -> 查看更早记录”。

期望：

- 规划器识别 `paginated_table` 并选择 `gui.page_next`。
- 下一页按钮通过 `auto_id_suffix=nextPageButton` 或稳定 `target_ref` 唯一解析。
- 操作前后第一行时间戳或页码发生预期变化。
- HTML/Markdown 报告按步骤展示 before、action trace、after 和验证结论。
- 整个流程不创建临时 Python 文件。

## 推荐实施顺序

1. P0.2：修复 Windows 前台窗口检测和 `strict` 语义。
2. P0.1：为 `scroll` 增加窗口、进程、PID、坐标空间和遮挡校验。
3. P0.3：拆分事件发送与效果验证状态。
4. P1.3：接入统一动作生命周期与报告证据。
5. P1.1/P1.2：实现控件级滚动和分页语义动作。
6. P1.4/P2：统一 schema、CLI 与错误修复提示，清除正常路径中的临时脚本依赖。

## 非目标

- 不通过关闭 Task Manager 等无关窗口规避遮挡问题。
- 不默认取消窗口安全校验。
- 不把截图像素变化作为唯一成功条件；动画、光标和时间变化会产生假阳性。
- 不为某个固定分辨率或固定窗口坐标编写专用脚本。
- 不把“下一页”硬编码为 EDRClient 专用逻辑，应以通用分页容器能力实现。
