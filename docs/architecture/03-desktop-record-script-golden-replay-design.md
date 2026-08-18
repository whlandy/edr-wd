# Desktop Recording, Script Generation, And Golden Replay Design

## Status

- State: active implementation design (progress is tracked separately)
- Intended implementer: EDR-WD maintainers or a coding agent
- Review owner: EDR-WD maintainers
- Initial recording schema: `edr.desktop-recording/v1`
- Initial compiled-case schema: `edr.desktop-recorded-case/v1`
- Initial golden schema: `edr.desktop-golden-trace/v1`
- Applies to: Windows UIA and macOS Accessibility targets

> Implementation progress and live-acceptance gaps are authoritative in
> `docs/todo/desktop-recording-golden-replay.md`. “当前缺口” below describes
> the baseline when this design was approved, not the latest code state.

本文定义 EDR-WD 补齐以下三个能力的实现契约：

1. 捕获用户在桌面应用中的人工操作；
2. 将录制结果生成可审阅、可执行的测试脚本；
3. 通过语义定位或受保护的视觉定位回放黄金轨迹。

设计复用现有 action catalog、observation、executor、expectation、confirmation、trace、
evidence 和 report，不创建第二套 GUI 执行框架。

## 1. 当前缺口

EDR-WD 已经能够：

- 通过 Windows UIA 或 macOS Accessibility 观察窗口和控件；
- 执行 `gui.click`、`gui.type_text`、`gui.select`、scroll、drag 等动作；
- 执行预先定义的 `TestCase`；
- 保存追加式事件链、截图、`trace.md` 和运行报告；
- 在执行前检查窗口所有权、目标新鲜度、风险和确认令牌。

但它还不能：

- 在用户手工操作时被动捕获输入并关联到实际 UI 控件；
- 将原始输入事件编译为稳定、可维护的 action sequence；
- 从录制产物自动生成 pytest 测试；
- 把录制时的成功路径作为黄金输入重新定位和执行；
- 对回放路径、定位回退、额外动作和断言结果进行评分。

现有执行 trace 不能直接充当回放脚本。trace 记录的是执行结果，其中的
`snapshot_id`、`target_id`、PID、窗口句柄和坐标可能只在当次会话有效；部分参数还会因
脱敏而不可逆。

## 2. 目标

1. 用户在目标桌面中正常操作，recorder 不替用户执行动作。
2. 每个输入事件都绑定到明确的进程、窗口和控件候选。
3. 将一次人工流程编译成 action catalog 中已有的稳定 `action_id`。
4. 不把 observation-local `target_id` 当作跨运行选择器。
5. 生成一份规范化 `RecordedTestCase` 和一份可直接运行的 pytest 脚本；回放时再将每个
   recorded step 物化为现有 `AtomicTestStep`。
6. 生成一份版本化、可校验、可回放的黄金轨迹。
7. 回放默认使用语义定位；视觉定位是受保护的回退或评估模式。
8. 所有回放动作继续经过 `AtomicExecutor`、confirmation gate 和 expectation registry。
9. 回放产生新的 append-only execution trace，不修改黄金轨迹或原始录制。
10. 从捕获源头阻止密码、令牌和不在目标窗口内的输入落盘。

## 3. 非目标

- 录制整个操作系统或监控不属于指定目标应用的输入。
- 生成任意 Python、PowerShell 或 shell 代码。
- 将固定坐标作为默认或唯一定位手段。
- 从当前 UI 值自动猜测用户想要的业务断言。
- 自动回放未确认的高风险或不可逆动作。
- 用截图相似度替代结构化断言。
- 保证录制结果不经审阅即可进入生产环境执行。
- 将 EDR-RAG 的知识检索、流程推荐或反馈存储并入本模块。

## 4. 术语和数据所有权

| 名称 | 含义 | 是否可执行 |
|---|---|---:|
| Raw recording | OS 输入事件、命中的窗口/控件、前后观察和截图 | 否 |
| Compiled case | 从 raw recording 规范化得到的 `RecordedTestCase` | 是 |
| Generated script | 加载 compiled case 或 golden trace 的 pytest 入口 | 是 |
| Golden trace | 经用户审阅的动作、稳定选择器、显式断言和成功条件 | 是 |
| Execution trace | 某一次回放实际产生的 `events.jsonl`、证据和报告 | 否 |

执行 trace 的事实来源仍是 `agent/trace/`。黄金轨迹是输入，不属于 trace event chain，
也不能通过删除 `step-results.json` 后重新投影得到。

## 5. 总体架构

```text
目标机器上的用户
  -> Windows low-level hooks / macOS CGEventTap
       -> target-local capture session
            -> active window ownership check
            -> UIA/AX hit-test and focused-control lookup
            -> source redaction
            -> raw capture events
                 -> agent recording compiler
                      -> event coalescing
                      -> action catalog mapping
                      -> stable replay selector synthesis
                      -> explicit assertion binding
                      -> compiled RecordedTestCase
                      -> golden-trace.json
                      -> generated pytest

golden-trace.json
  -> replay adapter
       -> fresh ObservationSnapshot per step
       -> stable selector resolution
       -> live TargetRef
       -> existing AtomicExecutor
       -> existing confirmation / recovery / trace / report
```

### 5.1 Target 端职责

- 开始、暂停、恢复和停止一个有明确作用域的 capture session；
- 监听用户输入，但不注入输入；
- 只接受锁定进程/窗口范围内的事件；
- 使用 UIA `ElementFromPoint` / focused element 或 AX hit-test 找到目标控件；
- 捕获事件发生前后的最小观察信息和可选截图；
- 在数据离开目标机器前识别密码控件并脱敏；
- 显示用户可见的“正在录制”状态；
- 以单调递增序号上报 raw capture events。

### 5.2 Agent 端职责

- 建立 target session 和 capture session；
- 持久化 raw recording、截图和观察快照；
- 合并键盘、鼠标和控件变化事件；
- 生成跨运行稳定的 replay selector；
- 将事件映射到 action catalog；
- 标记不完整、歧义或风险步骤，禁止静默降级；
- 生成 compiled case、pytest 和 golden trace；
- 加载黄金轨迹并通过现有 executor 回放；
- 比较黄金路径与实际路径并生成评分。

## 6. Capture Session

### 6.1 生命周期

新增独立的 `CaptureSessionState`，避免和现有执行状态机中的 `RECORDING` 混淆：

```text
idle -> starting -> recording <-> paused -> stopping -> stopped
                         |                       |
                         +-------> failed <------+
```

每个状态转换都必须有 target-local receipt。Agent 失联时，target 在租约超时后自动停止
hooks，不能无限后台记录。V1 默认 activity lease 为 300 秒；成功捕获的业务事件、
Pause/Resume、显式断言入口和 agent heartbeat 都续租，纯噪声或作用域外事件不能续租。

### 6.2 CLI

```bash
edr-wd --target TARGET record start \
  --name policy-flow \
  --process-name EDRClient.exe \
  --window-title '^EDRClient$'

edr-wd --target TARGET record status
edr-wd --target TARGET record pause
edr-wd --target TARGET record resume
edr-wd --target TARGET record assert
edr-wd --target TARGET record stop

edr-wd record compile recordings/policy-flow/recording.json
edr-wd --target TARGET replay recordings/policy-flow/golden-trace.json
```

`record start` 必须先完成 connect、window lock 和 ownership verification。作用域缺失、
窗口不唯一或目标后端没有录制权限时直接失败。
`record stop` 默认把目标配置中的 `app_profile` 绑定到 golden environment；离线
`record compile` 可通过 `--profile` 显式绑定。存在 profile 绑定时 replay 必须精确匹配，
不能把不同产品流程仅因 backend 相同就视为兼容。

### 6.3 MCP 管理接口

新增管理面工具：

```text
start_recording
recording_status
pause_recording
resume_recording
add_recording_assertion
stop_recording
get_recording_capture
```

这些接口控制 recorder 自身，不是被测产品动作，因此不加入 action catalog，也不允许出现在
LLM 生成的业务 ActionSequence 中。

### 6.4 平台捕获方式

#### Windows

- 鼠标和键盘：`SetWindowsHookEx(WH_MOUSE_LL/WH_KEYBOARD_LL)`；
- 目标控件：UI Automation `ElementFromPoint` 和 focused element；
- 控件变化：ValuePattern、TogglePattern、SelectionPattern 和窗口事件；
- 所有 hook callback 只入队，不在 callback 内做网络或慢速 UIA 查询。

#### macOS

- 鼠标和键盘：`CGEventTap`；
- 目标控件：Accessibility hit-test 和 focused UI element；
- 控件变化：AXValue、AXSelected、AXFocusedUIElement 和窗口通知；
- 缺少 Accessibility 或 Input Monitoring 权限时阻止开始录制；
- Screen Recording 权限只影响截图/视觉模板，不影响纯 AX 语义录制。

### 6.5 用户可见性

录制期间必须在目标桌面显示状态指示器，至少包含：

- 录制名称和作用域应用；
- recording/paused 状态；
- 当前步骤数；
- 停止按钮和断言入口。

指示器必须带 `recorder_ui=true` 身份，从 hit-test、截图模板和生成步骤中过滤。不能只在
Agent 终端打印“正在录制”，因为操作用户可能只看到远程桌面。

## 7. Raw Capture Event

Raw recording 顶层信封必须携带捕获健康状态：

```json
{
  "schema": "edr.desktop-recording/v1",
  "sessionId": "REC-...",
  "name": "policy-flow",
  "captureDiagnostics": {
    "droppedPackets": 0,
    "correlationErrorCount": 0
  },
  "events": []
}
```

### 7.1 事件信封

```json
{
  "sequence": 17,
  "wallTime": "2026-08-17T10:20:31.225Z",
  "monotonicMs": 43125,
  "type": "pointer_click",
  "scope": {
    "target": "2.26-edr-win26-win11",
    "backend": "windows_pywinauto",
    "processName": "EDRClient.exe",
    "windowTitle": "EDRClient"
  },
  "input": {
    "button": "left",
    "clickCount": 1,
    "screenPoint": [842, 516]
  },
  "observedTarget": {
    "snapshotId": "OBS-01J...",
    "targetId": "T0042",
    "fingerprint": "sha256:...",
    "controlType": "Button",
    "automationId": "btnApply",
    "text": "应用",
    "rect": [790, 490, 890, 542]
  },
  "evidence": {
    "beforeCapture": {
      "id": "CAP-<32 lowercase hex>",
      "sha256": "sha256:<64 lowercase hex>",
      "width": 1440,
      "height": 900,
      "origin": [0, 0],
      "redacted": true,
      "redactions": []
    },
    "capture": {
      "id": "CAP-<32 lowercase hex>",
      "sha256": "sha256:<64 lowercase hex>",
      "width": 1440,
      "height": 900,
      "origin": [0, 0],
      "redacted": true,
      "redactions": []
    }
  }
}
```

`snapshotId` 和 `targetId` 只用于证明录制时点到了什么，不进入跨运行 replay selector。

### 7.2 捕获事件类型

V1 支持：

| Raw event | 编译结果 |
|---|---|
| `pointer_click` | 左键为 `gui.click`；右/中键为 `pointer.right_click` / `pointer.middle_click`，均从 fresh semantic target 计算实时中心点，不复用录制坐标 |
| `pointer_double_click` | catalog 中的 `pointer.double_click`；仍受窗口锁和坐标回退策略保护 |
| `text_commit` | `gui.type_text` |
| `selection_change` | `gui.select` |
| `toggle_change` | click + 显式目标状态 expectation |
| `key_command` | catalog 支持的按键动作；不支持则 incomplete |
| `scroll_commit` | `pointer.scroll`；缺少因果绑定的内容/位置 verifier 时标为 incomplete |
| `drag_commit` | `pointer.drag`；起点/终点各自解析为稳定 selector + 控件内相对 anchor，缺少结果 verifier 时标为 incomplete |
| `window_transition` | transition metadata，不单独生成无意义动作 |
| `assertion` | 只生成 expectation/verifier |

鼠标移动、修饰键单独按下、重复 focus、recorder UI 操作和作用域外事件不落盘。

recorder 不会仅凭截图变化推断业务成功。`scroll_commit` 和 `drag_commit` 必须绑定一条显式
结果 verifier 才能 ready：用户在 assertion editor 勾选 `bindPrevious`，session 就把该断言的
`causalId` 设为上一条动作事件的 `causalId`，compiler 再把它折叠成那一步的 verifier。未绑定的
断言仍保留自己的 observe-only step，不会伪装成动作证明。

`drag_commit` 由 press/release 对合成：位移超过系统拖拽阈值（`SM_CXDRAG`/`SM_CYDRAG`，
取不到时回退 4px）才算拖拽，否则仍是 `pointer_click`。起点与终点各自解析为语义 selector，
并把录制时的屏幕坐标投影成控件矩形内的相对 anchor；replay 用当前观察到的矩形重新计算绝对
坐标，因此窗口移动或缩放后仍拖同一个把手。终点无法绑定稳定控件身份时以
`compile_selector_ambiguous` 保持 incomplete，不退化成录制坐标。

`window_transition.input` 使用 `kind=opened|closed`，并可包含 `processName`、`title`、
`titleRegex` 和 `timeoutSeconds`；它必须与触发动作共享 `causalId`。无法绑定时保留一个
`compile_transition_unbound` incomplete step，不能静默删除。
Windows adapter 通过 `SetWinEventHook` 订阅顶层窗口 show/destroy，macOS adapter 轮询
`CGWindowListCopyWindowInfo`；两者都只按进程过滤（点击通常打开标题不同的对话框），并由
correlator 绑定最近一次动作的 `causalId`。

作用域随因果打开的窗口增长：录制开始时作用域是用户给的 `processName` + `windowTitle`
正则，每当一次动作导致新窗口打开，该窗口标题加入作用域；窗口关闭时移出。没有这一条，
"点击打开对话框"会被录下来，而用户随后在对话框里做的一切都被静默丢弃 —— 这正是
`日志中心` 这类子窗口流程的常态。作用域只对**动作因果打开**的窗口增长，自行弹出的后台
窗口不会放宽作用域。

作用域外的输入是正常的（用户可能切到别的窗口看一眼），因此不构成 compile issue，但必须
可计数：`captureDiagnostics.outOfScopeEvents` 记录被拒绝的输入数量，并出现在
`compile-report.json` 的 `diagnostics` 中，避免把一次"大部分被丢弃"的录制误当作完整录制。`timeoutSeconds` 由实测延迟推导（3 倍，钳制在
[5, 30] 秒），replay 因此不使用固定 sleep。没有近期动作可解释的窗口变化属于后台噪声，
不进入录制。

### 7.3 事件合并

Compiler 必须执行以下确定性归一化：

1. mouse down/up/click 合并成一次 click；
2. target 读取系统双击间隔（平台 API 不可用时回退 500ms），将阈值写入
   `evidence.doubleClickIntervalMs`；compiler 仅在该阈值内且 causal ID/目标相同的两次
   click 合并为 double click；
3. 连续字符输入按 focused control 合并为一次 `text_commit`；
4. 用户先输入错误再改正时，以 focus 离开、Enter、提交或 debounce 后的最终控件值为准；
5. checkbox/radio 的 pointer event 与 toggle event 合并，避免 click 后再 click；
6. 只有真正产生新值的 selection/toggle 事件进入 compiled case；
7. 同一动作产生的窗口变化和观察变化关联到该动作，不另建空步骤。

归一化算法必须使用 monotonic time 和 causal IDs，不能只按 wall-clock 时间窗口猜测。
`captureDiagnostics` 是 raw recording 的必填健康边界。只要 bounded hook queue 丢包或
correlator 报错，compiler 就分别产生 `recording_packets_dropped` 或
`recording_correlation_errors`，并把整个产物标为 incomplete；不得用剩余事件静默生成
ready 测试。事件中显式 `captureError` 同样使对应步骤 incomplete。

## 8. 稳定 Replay Selector

### 8.1 选择器模型

```json
{
  "window": {
    "processName": "EDRClient.exe",
    "titleRegex": "^EDRClient$",
    "bundleId": null
  },
  "control": {
    "automationId": "btnApply",
    "identifier": null,
    "controlType": "Button",
    "name": "应用",
    "ancestry": [
      {"controlType": "Dialog", "name": "策略设置"}
    ],
    "anchor": null
  },
  "visual": {
    "template": "assets/step-0004-element.png",
    "contextTemplate": "assets/step-0004-context.png",
    "elementSha256": "sha256:<64 lowercase hex>",
    "contextSha256": "sha256:<64 lowercase hex>",
    "redacted": true,
    "relativePoint": [0.52, 0.48]
  }
}
```

### 8.2 语义定位优先级

1. window ownership + stable automation ID/AX identifier；
2. window ownership + role/control type + accessible name；
3. 限定祖先容器后的稳定属性；
4. 唯一 sibling/anchor 文本关系（设计保留，V1 尚未合成 anchor）；
5. 录制 fingerprint 仅作诊断 evidence；当前录制与 replay observation 的 fingerprint
   算法尚未统一，不能作为跨运行硬匹配条件；
6. 高置信且唯一的视觉模板；
7. 窗口内归一化坐标，只能作为显式开启的最后回退。

任何层级匹配多个控件都返回 `target_ambiguous`，不能使用第一个匹配项。所有 GUI mutation
仍要求当前窗口锁和进程所有权通过验证。

### 8.3 和现有 TargetRef 的关系

`ReplaySelector` 是跨运行描述；现有 `TargetRef` 是单次 observation 内的执行引用。每一步
回放时必须：

1. 获取新的 `ObservationSnapshot`；
2. 用 `ReplaySelector` 找到唯一 live target；
3. 构造包含新 `snapshot_id` 和新 `target_id` 的 `TargetRef`；
4. 把 `TargetRef` 交给现有 dispatcher/executor。

禁止把黄金轨迹里录制时的 `target_id` 直接送入 dispatcher。

## 9. 人工断言和 Expected Value

录制器不能从控件当前值静默推断业务意图。用户必须明确选择断言类型并确认
`expected`。所有断言，包括布尔断言，都必须持久化显式 expected value。

桌面应用不能统一劫持右键，因为右键往往是产品真实操作。V1 使用以下入口：

- recorder 状态指示器中的“添加断言”；
- 全局快捷键，例如 `Ctrl+Shift+A` / `Cmd+Shift+A`；
- `edr-wd --target TARGET record assert` 管理命令。

入口打开 target-local assertion editor，默认选择鼠标悬停或当前 focused control，但只有
用户点击“确认”后才保存。

```json
{
  "type": "assertion",
  "selector": {
    "window": {"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"},
    "control": {"automationId": "statusText", "controlType": "Text"}
  },
  "assertion": "text_equals",
  "expected": "已生效",
  "timeoutSeconds": 30
}
```

V1 支持：

| Assertion | Expected 类型 | Existing expectation mapping |
|---|---|---|
| `text_equals` | string | `control_text_equals` |
| `text_contains` | string | `control_text_contains` |
| `value_equals` | string | 新增 evaluator |
| `visible` | boolean | `control_exists` / `control_absent` |
| `checked` | boolean | 新增 evaluator |
| `enabled` | boolean | 新增 evaluator |
| `window_open` | boolean 或 `{exists, processName/process_name, title, titleRegex}` | `window_open` / `window_closed` |

空字符串是有效 expected，但 assertion editor 必须二次确认；`false` 不能因为 falsy 被省略。
`timeoutSeconds` 必须由 editor 显式保存，范围为 `(0, 300]`，默认 10 秒；不同 assertion
类型必须在 target 端验证 expected 类型，不能等到 replay 时才把类型错误解释成断言失败。

## 10. 凭据和隐私

### 10.1 最小捕获

- capture scope 必须指定 target、process 和 window；
- 作用域外 keyboard/mouse event 在 target 内存中立即丢弃；
- raw keyboard characters 默认不写入事件流；
- 普通字符 key-up 只产生不含 key/value 的内存 `text_activity` 信号；normal text 在
  focus 离开、Enter/Tab 或 stop flush 时从 scoped focused control 获取最终值；
- recorder 不采集剪贴板内容。

### 10.2 Secret 输入

当 UIA/AX 报告 password/protected field 时：

```json
{
  "actionId": "gui.type_text",
  "args": {
    "textSource": {"kind": "env", "name": "EDR_WD_SECRET_1"}
  }
}
```

密码明文不得出现在 raw recording、截图 OCR、golden trace、pytest、trace event 或报告中。
无法确定控件是否敏感时，compiler 将步骤标为 `needs_secret_review`，不能自动生成 ready
golden trace。

### 10.3 截图

截图继续使用现有 evidence persistence、digest、ownership 和 redaction policy。视觉模板必须
从脱敏后的截图裁剪；如果 redaction 覆盖目标区域，该步骤不能进入 `visual_only` 模式。

实际流水线由 target 端 `RecordingEvidenceStore.attach` 完成：hook callback 只入队，correlator
worker 在事件通过 scope/secret 边界后取窗口截图、枚举 protected/secure-text rectangles、先在
内存中遮蔽，再保存有限数量的脱敏 PNG。`recording.json` 只包含 capture ID、SHA-256、尺寸、
截图原点和 redaction rectangles，不包含 base64 或 target 文件路径。停止录制后 agent 使用
`get_recording_capture` 拉取脱敏图片并同时核对 recording metadata 与 target response 中的
摘要；任一摘要不一致都不生成模板。显式传入 screenshot path 仍可落盘，录制内部调用必须
使用无 path 的 in-memory 模式。
protected-control 或 recorder-window 枚举失败时截图流水线必须 fail closed；“无法枚举”不能
被解释成“页面没有敏感区域”。INIT 阶段失败时 hooks 不启动，步骤阶段失败时写入
`captureError` 并使 compiled case/golden trace incomplete。
证据层必须校验 backend 返回 `capture_scope=window`；全屏帧即使能够在内存中二次裁剪也不
进入录制产物。macOS CoreGraphics 调用使用已连接窗口矩形作为 capture region，不能把其他
应用窗口或桌面内容带入 recording evidence。

截图采用生命周期链而不是每个步骤重复截 before/after：`start_recording` 在 source 启动前
建立唯一的 INIT 基线；每个可执行事件完成后只截一张 after，并将上一生命周期的 after capture
ID 作为当前事件的 `beforeCapture`。因此第一个步骤的 before 指向 INIT，后续步骤的 before
都指向前一步 after。agent 拉取和落盘时以 capture ID 去重，统一保存为
`assets/observations/<capture-id>.png`；相邻步骤共享同一文件，报告仍能恢复完整的前后状态链。

## 11. 编译和脚本生成

### 11.1 产物目录

```text
~/Desktop/edr-wd-record/recordings/<flow>/
  recording.json
  case.json
  golden-trace.json
  test_<flow>.py
  compile-report.json
  assets/
    step-0001-element.png
    step-0001-context.png
    observations/
      CAP-<uuid>.png
```

- `recording.json`：不可变的原始捕获；
- `case.json`：符合 `edr.desktop-recorded-case/v1` 的可维护定义；其 action、expectation、
  transition 和 evidence 语义复用现有协议模型，但 target 使用跨运行 `ReplaySelector`；
- `golden-trace.json`：带 replay selectors、verifiers 和路径元数据；
- `test_<flow>.py`：可执行、可审阅的 pytest 入口；
- `compile-report.json`：歧义、未支持动作、secret review 和风险清单。

capture 下载、base64 或摘要校验失败不会伪造 visual selector；该事实同时写入 CLI 输出与
`compile-report.json.artifactIssues`。语义 selector 仍可独立审阅，但报告必须明确视觉资产
未落盘。

### 11.2 编译流水线

```text
validate raw schema
  -> verify sequence and evidence digests
  -> coalesce input events
  -> map to action catalog
  -> synthesize ReplaySelector
  -> attach explicit assertions
  -> classify transitions and risk
  -> validate against live catalog version/digest
  -> emit RecordedTestCase
  -> emit golden trace
  -> emit pytest projection
```

Compiler 不允许：

- 生成 catalog 外的任意工具调用；
- 将 unsupported event 静默删掉；
- 把坐标回退伪装成语义选择器；
- 自动添加当前值等于当前值的业务断言；
- 为通过编译而放宽用户确认过的 expected value。

存在 unsupported、ambiguous、missing-secret 或 missing-assertion-review 时，golden trace
状态为 `incomplete`。只有所有 required steps 都 ready 时才允许默认 replay。
没有任何 accepted lifecycle event 的空录制同样产生 `compile_recording_empty`，必须为
`incomplete`；空步骤集合不能通过“所有步骤均成功”的真空逻辑得到成功评价。

### 11.3 生成的 pytest

生成脚本保持轻薄，以 canonical JSON 为输入，不复制 executor 逻辑：

```python
from pathlib import Path

from agent.recording.replay import load_golden_trace, replay_golden_trace


def test_policy_flow(edr_wd_target):
    case_dir = Path(__file__).parent
    golden = load_golden_trace(case_dir / "golden-trace.json")
    result = replay_golden_trace(edr_wd_target, golden)
    assert result.task_success, result.summary
```

脚本文件可以由 `golden-trace.json` 重建。用户对业务步骤的维护应优先修改 `case.json` 或
重新录制；生成器不得解析 pytest 源码来恢复 canonical model。

## 12. Golden Trace Schema

```json
{
  "schema": "edr.desktop-golden-trace/v1",
  "status": "ready",
  "name": "policy-flow",
  "sourceRecording": {
    "schema": "edr.desktop-recording/v1",
    "sha256": "sha256:..."
  },
  "catalog": {
    "version": "1.0.0",
    "digest": "sha256:..."
  },
  "environment": {
    "profile": "windows_hisec",
    "backend": "windows_pywinauto",
    "application": "EDRClient.exe"
  },
  "entry": "step-0001",
  "steps": {
    "step-0001": {
      "stepId": "step-0001",
      "actionId": "gui.click",
      "args": {},
      "selector": {
        "window": {"processName": "EDRClient.exe", "titleRegex": "^EDRClient$"},
        "control": {"automationId": "btnApply", "controlType": "Button"},
        "visual": null
      },
      "verifiers": [
        {"type": "window_open", "expected": true, "timeoutSeconds": 10}
      ],
      "required": true,
      "status": "ready",
      "issues": [],
      "next": null
    }
  },
  "cleanup": []
}
```

每一步必须有一个 action 或一个 pure verifier。Verifier 不通过 action targeting 执行，避免
视觉点击成功与业务断言共用同一判断机制。

黄金轨迹必须保存 catalog version/digest。Digest 不匹配时默认拒绝；显式兼容模式也必须
重新校验每个 action 和 selector，并在新的 execution trace 中记录接受原因。

## 13. Replay

### 13.1 模式

| Mode | 行为 | 用途 |
|---|---|---|
| `semantic_only` | 只允许 UIA/AX 稳定选择器 | 默认安全回放 |
| `semantic_first` | 语义失败后允许高置信视觉回退 | UI 结构轻微漂移 |
| `visual_only` | 所有可定位动作都使用模板 | 评估视觉 Agent |

`semantic_first` 的视觉回退必须由 case 或命令显式允许。高风险动作默认仍是
`semantic_only`。

### 13.2 单步回放算法

1. 验证 schema、golden status、catalog digest 和目标 profile；
2. 确认 capture/replay 不在同一目标上同时运行；
3. connect、lock window、verify ownership；
4. 获取新的 observation；
5. 按 replay selector 找到唯一 target；
6. 如果语义定位失败且策略允许，执行视觉匹配；
7. 将 live target 转成新的 observation-local `TargetRef`；
8. 通过现有 confirmation gate 检查风险和 side effect；
9. 将 recorded step 物化成 `AtomicTestStep` 并交给 `AtomicExecutor`；
10. 按 expectation timeout 轮询新的 observation，不使用固定 sleep；
11. 保存 receipt 摘要、expectation result 和 fallback strategy；视觉模式的 runtime
    screenshot 必须标记为锁定窗口捕获；`replay_capture` 工具在 target 端复用与录制相同的
    source-redaction 边界，`--persist-screenshots` 才允许把 runtime frame 写进 execution
    trace 的 `screenshots/`，未脱敏或非窗口捕获一律拒绝落盘；
12. 进入下一步，或按现有 on-error/recovery policy 终止或恢复。

回放不会把“点击 API 返回 ok”当作测试成功。required action、显式 verifiers、cleanup 和
trace integrity 必须全部满足。

### 13.3 视觉定位安全条件

视觉目标只有同时满足以下条件时才能执行：

- 截图属于当前已验证、已锁定的窗口；
- 模板没有包含未脱敏的敏感区域；
- 最佳匹配达到最低置信度；
- 最佳匹配和第二名之间达到最小 margin；
- 目标矩形完全位于锁定窗口内；
- 录制分辨率、当前缩放和坐标转换可解释；
- 相对点击点位于模板目标区域内；
- action risk policy 允许 pointer fallback。

任何条件失败都返回 typed failure，不进行试探性点击。

## 14. Replay Evaluation

每次回放输出 execution trace 和独立 evaluation projection：

```json
{
  "schema": "edr.desktop-replay-evaluation/v1",
  "taskSuccess": true,
  "requiredSteps": 8,
  "passedSteps": 8,
  "assertionPassRate": 1.0,
  "semanticResolutionRate": 0.875,
  "visualFallbackCount": 1,
  "coordinateFallbackCount": 0,
  "extraActionCount": 0,
  "retryCount": 0,
  "pathFidelity": 1.0,
  "cleanupPassed": true,
  "traceIntegrity": true
}
```

`taskSuccess` 至少要求：

- 所有 required steps passed；
- 所有显式断言 passed；
- 没有未声明的高风险动作；
- cleanup 按 case contract 成功；
- execution trace integrity 通过；
- 没有 skipped required step。

定位方式属于评分事实。视觉或坐标回退可以仍然通过，但不能在报告中伪装成语义命中。

## 15. 模块布局

```text
target/recording/
  models.py                 # capture session 和 raw event wire models
  session.py                # target-local lifecycle/lease/scope
  service.py                # MCP management facade and platform source routing
  source.py                 # bounded callback queue and correlator protocol
  windows.py                # Windows low-level hooks + UIA correlation
  macos.py                  # permission preflight + CGEventTap + AX correlation
  indicator.py              # target-local visible recorder UI
  evidence.py               # in-memory capture, source redaction, digest retrieval

agent/recording/
  models.py                 # recording/RecordedTestCase/golden/replay selector models
  compiler.py               # raw -> TestCase/golden
  selectors.py              # stable selector synthesis and validation
  artifacts.py              # canonical atomic artifact persistence
  generate_pytest.py        # canonical model -> pytest projection
  replay.py                 # golden -> live TargetRef -> AtomicExecutor
  mcp_runtime.py            # MCP observation/execute_action adapters
  visual.py                 # visual fallback policy and safety gates
  templates.py              # redact-before-crop element/context templates
  pillow_matcher.py         # scale-aware bitmap candidate matcher

test_case/schema/
  desktop-recording.schema.json
  desktop-golden-trace.schema.json
  desktop-replay-evaluation.schema.json
```

`agent/recording/replay.py` 是 adapter，不实现第二个 state machine。动作执行、断言、重试、
确认、恢复和运行时 trace 仍由 `agent/execution/` 与 `agent/trace/` 拥有。

### 15.1 已实现函数依赖

```text
target.server.start_recording
  -> RecordingService.start
  -> RecordingSession.attach_source / attach_indicator / start
  -> QueuedCaptureSource.start
  -> WindowsLowLevelHookDriver | MacOSEventTapDriver
  -> WindowsUIACorrelator | MacOSAXCorrelator
  -> RecordingSession.ingest
  -> RecordingEvidenceStore.initialize (one INIT baseline)
  -> RecordingEvidenceStore.attach (one after capture per accepted lifecycle event)

agent.cli record stop
  -> get_recording_capture MCP (capture ID only)
  -> verify target + recording SHA-256
  -> write_compilation_artifacts
  -> generate_redacted_templates
  -> attach visual selector + template digests

agent.cli record stop (semantic compiler path)
  -> stop_recording MCP
  -> RawRecording.from_dict
  -> compile_recording
  -> write_compilation_artifacts

agent.cli replay
  -> load_golden_trace
  -> MCPObservationProvider + MCPActionDispatch
  -> replay_golden_trace
  -> GoldenStepMaterializer
  -> resolve_replay_selector (每步 fresh observation)
  -> AtomicExecutor.run_case(step_materializer=...)
  -> ReplayEvaluation + TraceStore.verify
```

`TkRecordingIndicator` 的 Pause/Resume/Stop 回调直接进入同一个
`RecordingSession`；`Ctrl+Shift+A` / `Cmd+Shift+A` 在 `RecordingSession.ingest`
被识别为 recorder 管理动作，不写入业务事件。`SafeVisualResolver` 只消费
`PillowTemplateMatcher` 的候选，低置信、低 margin、越界、未声明 redacted 或非 click 动作
均在 dispatcher 之前失败。

## 16. 对现有模型的修改

### 16.1 保持不变

- action catalog 的稳定 action ID 和 digest 规则；
- target-local window ownership enforcement；
- observation-local `TargetRef` 语义；
- `AtomicExecutor` 的 action + expectation pass contract；
- append-only execution trace 和 evidence digest；
- recovery branch 不重写失败历史。

### 16.2 需要扩展

- observation target 增加可选 `identifier`、accessible state、ancestry 和 protected-field
  元数据；
- expectation registry 增量式增加 `control_value_equals`、`control_checked_equals` 和
  `control_enabled_equals`；
- action catalog 为键盘快捷键、双击或现有后端实际支持但 catalog 未表达的动作补齐稳定 ID；
- `AtomicExecutor.run_case` 增加可选的 step materializer/resolver hook；静态 `TestCase`
  保持原行为，golden replay 通过该 hook 在每一步执行前生成 fresh `TargetRef`，case-level
  abort、retry 和 cleanup 仍由同一个 executor 管理；
- trace event enum 增加 replay resolution/fallback 事件时必须进行协议版本评审；也可以先将
  resolution detail 放进现有 `ACTION_REQUESTED` payload，避免不必要的 breaking change；
- CLI 和 target MCP server 增加 recording management 子命令/工具。

## 17. 失败模型

新增稳定错误码：

| Code | 含义 |
|---|---|
| `recording_permission_missing` | OS 权限不足 |
| `recording_scope_not_unique` | 进程/窗口作用域不唯一 |
| `recording_lease_expired` | Agent 失联后自动停止 |
| `recording_event_out_of_scope` | 输入不属于锁定窗口；事件被丢弃 |
| `recording_target_unresolved` | 无法将输入关联到控件 |
| `recording_capture_evidence_unavailable` | INIT 或步骤截图无法在 target 内存中生成 |
| `recording_packets_dropped` | bounded hook queue 丢包，录制不完整 |
| `recording_correlation_errors` | platform event correlation 出错，录制不完整 |
| `recording_secret_review_required` | 敏感输入分类不确定 |
| `compile_action_unsupported` | raw event 无 catalog 映射 |
| `compile_recording_empty` | raw recording 没有任何已接受的生命周期事件 |
| `compile_selector_ambiguous` | 不能生成唯一稳定 selector |
| `compile_scroll_verifier_required` | 录制滚动缺少显式内容/位置 verifier |
| `compile_drag_verifier_required` | 录制拖拽缺少显式结果 verifier |
| `recording_assertion_unbound` | 断言要求绑定上一条动作，但没有可绑定的动作 |
| `replay_drag_end_missing` | 黄金轨迹的拖拽缺少终点 selector |
| `replay_anchor_invalid` | anchor 相对坐标不在 [0, 1] |
| `replay_capture_not_redacted` | runtime 截图未经 target 端脱敏，拒绝落盘 |
| `replay_capture_not_window_scoped` | runtime 截图不是锁定窗口捕获，拒绝落盘 |
| `golden_trace_incomplete` | required step 尚未 ready |
| `golden_catalog_mismatch` | catalog version/digest 不匹配 |
| `replay_target_not_found` | 当前 observation 无目标 |
| `replay_target_ambiguous` | 当前 observation 多目标匹配 |
| `visual_match_low_confidence` | 模板置信度不足 |
| `visual_match_ambiguous` | 最佳和次佳匹配 margin 不足 |
| `visual_template_integrity_failed` | 模板路径或 SHA-256 校验失败 |

所有错误都必须保留 target、step、selector strategy 和 candidate count 等可诊断信息，但先
经过现有 redaction boundary。

## 18. 实施阶段

### Phase 0 — Schema 和离线编译器

- 定义 strict dataclasses 和 JSON Schema；
- 用固定 UIA/AX fixtures 实现事件合并、action mapping 和 selector synthesis；
- 生成 `case.json`、golden trace 和 pytest；
- 先不接真实 OS hooks。

### Phase 1 — Windows 语义录制

- 实现 scoped mouse/keyboard hooks；
- UIA hit-test、focused control 和 value/toggle/select correlation；
- target-local indicator 和 assertion editor；
- 支持 click、double click、type、select、toggle；
- 实机录制后连续回放两次。

### Phase 2 — macOS 语义录制

- 实现 CGEventTap 和 AX correlation；
- 权限 preflight、indicator 和 assertion editor；
- 与 Windows 共享 raw schema/compiler/golden schema；
- 用相同产品级 case 做跨平台 acceptance。

### Phase 3 — Golden replay 和评估

- ReplaySelector -> fresh TargetRef step materializer；
- 接入 AtomicExecutor、confirmation、cleanup、trace 和 report；
- evaluation projection；
- catalog mismatch 和 incomplete trace gate。

### Phase 4 — 视觉回退

- 元素/上下文模板捕获；
- scale-aware matching、unique margin 和 window-bound checks；
- `semantic_first` 和 `visual_only`；
- 视觉回退专门的安全与评估测试。

复杂快捷键在各平台基础 click/type/select 稳定后再进入 acceptance，不能延迟核心语义录制和
安全回放。scroll、drag 和跨窗口 transition 的编译/回放契约已实现，其 live acceptance 与
click/type/select 同批进行。

## 19. 测试策略

### 19.1 单元测试

- raw schema unknown field 拒绝；
- sequence、causal ID 和 digest 校验；
- click/double-click/text/toggle 合并；
- 作用域外事件不落盘；
- password control 永不出现明文；
- `expected=false` 和 `expected=""` 不丢失；
- selector priority、ambiguity 和 ownership；
- unsupported action 产生 incomplete，而不是被删除；
- golden catalog mismatch 默认拒绝；
- generated pytest 可导入且只调用公共 replay API。

### 19.2 离线集成测试

- Windows UIA 和 macOS AX observation fixtures 编译为同一语义 action；
- golden adapter 每步生成新的 live `TargetRef`；
- fake target 跑完整 AtomicExecutor/expectation/trace 路径；
- trace integrity 和 evaluation projection 可重复；
- 相同输入生成 byte-identical canonical JSON。

### 19.3 Live acceptance

每个平台至少完成：

1. 人工录制一个含 click、type、select、toggle 和显式断言的 8 步流程；
2. 关闭应用并重新打开后回放成功；
3. 改变窗口位置后语义回放成功；
4. 控件 observation-local ID 变化后仍成功；
5. 连续回放两次，无 skipped required step；
6. intentionally duplicate control 时以 ambiguous 失败，绝不点第一个；
7. 密码输入产物目录全文搜索无明文；
8. 高风险动作没有 confirmation token 时被 blocked；
9. assertion expected 被修改后，回放按用户确认值判断而不是录制时当前值；
10. 所有 execution trace integrity checks 通过。

视觉 acceptance 另加窗口缩放、DPI、近似模板冲突、遮挡和 redacted target 等场景。

## 20. 验收标准

以下全部满足后，才能宣称 EDR-WD 具备这三个能力：

### 捕获人工操作

- Windows 和 macOS 各至少一个正式支持 profile 通过 live acceptance；
- recorder 只捕获明确锁定窗口；
- 用户可见 start/stop 状态；
- secret 和作用域外输入不落盘；
- raw recording 可通过 schema 和 digest 校验。

### 生成可执行脚本

- 每次录制生成 `case.json`、golden trace 和 pytest；
- 脚本通过公共 EDR-WD runner 执行，不含私有临时代码；
- 不含 observation-local IDs、固定 PID 或未声明绝对坐标；
- unsupported/ambiguous/secret review 使产物 incomplete；
- 同一 canonical input 的生成结果确定且可重建。

### 黄金轨迹回放

- 默认语义定位，每步重观察并生成 fresh TargetRef；
- 显式断言使用用户确认的 expected；
- 回放完整经过 confirmation、expectation、cleanup 和 trace；
- 视觉回退有置信度、唯一性、窗口边界和风险策略保护；
- 产生 execution trace 和 evaluation，且 `taskSuccess` 不依赖单纯 action ok；
- 同一流程在全新应用会话中连续通过两次。

## 21. 已锁定设计决策

1. 录制 hooks 在 target 端运行，compiler 和 artifacts 在 agent 端运行。
2. Raw recording、golden trace 和 execution trace 是三种不同 schema。
3. 黄金轨迹不保存可直接复用的旧 `TargetRef`。
4. 回放必须适配到现有 `AtomicExecutor`，不创建第二执行器。
5. 桌面断言不全局劫持右键，使用 recorder UI、快捷键或管理命令。
6. 所有 assertion 都保存显式 expected，包括 `false` 和空字符串。
7. Secret 在 target 端 source-redact，不能依赖写盘后的二次清理。
8. 视觉定位是受保护的 fallback/evaluation，不是默认安全路径。
9. 录制管理工具不进入业务 action catalog。
10. 任何 ambiguity、unsupported event 或 policy mismatch 都显式失败或标记 incomplete。
