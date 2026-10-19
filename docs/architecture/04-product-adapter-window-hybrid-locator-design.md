# 客户端意图驱动、窗口管理与混合定位设计

## 状态与范围

- 状态：设计已进入实现；下文的量化验收数字仍是目标值，不能当作实测结果。当前代码与未通过项见本节末尾的实施记录。
- 首个完整适配对象：Windows/macOS 上的 HiSec/EDRClient，优先验证 Windows EDRClient 的多窗口流程。
- 扩展边界：其他 PC 客户端可以增加产品适配器；Windows UIA、macOS Accessibility、窗口守卫和定位器不得依赖 EDR 的进程名、标题或启动命令。
- 本文承接 [架构总览](00-overview.md)、[桌面录制与回放设计](03-desktop-record-script-golden-replay-design.md)，继续复用 action catalog、`AtomicExecutor`、确认机制、trace 和报告。
- 本轮在设计基线上实施代码与自动化测试；Windows EDRClient 实机、macOS 有输入桌面的指针路径与 OCR 安装情况必须分别验收。

### 应用目录收口（2026-10-16）

应用信息统一存放在 `target/product_specs/apps.json`，`default_app=edrclient`。
每个 `apps[]` 条目含 `app_id/name` 与平台 `profiles[]`；每个 profile 必须记录
绝对安装路径 `application.path` 和完整已知 `windows[]`（窗口名称、可选精确标题、
角色路径、进程归属、标题规则及父窗口）。目标配置的 `app_profile` 只负责覆盖默认
选择，不复制应用信息；省略时 Windows/macOS 分别选择 `windows_hisec` / `macos_hisec`。
动态标题用角色和规则表示，不将当前标题当成永久配置。

目标端只读取本机部署的目录：`register_app_catalog()` 校验所有条目并以
`register_product_spec()` 注册能力；`activate_product_app(profile_id)` 从本地目录
取路径，拒绝调用方提供任意路径，Windows 还复核可执行文件名与窗口进程归属。
EDR 的入口与客户端路径也由同一目录提供，保留特殊的 HiSec 启动编排。

原子验收：缺失路径/窗口名/角色路径的目录 3/3 拒绝且不部分注册；默认产品
Windows/macOS 2/2 指向 EDR；显式选择非 EDR 2/2 保持；目标端未知产品、
跨 backend、活动 run 冲突 3/3 拒绝；实机仍需核对每台机器的安装路径和动态窗口。

### 产品可选化增量（2026-10-15）

EDRClient 的 Windows/macOS 角色与动作清单现已移到 `target/product_specs/`，与
Notepad、Calculator 示例使用同一套目标端 JSON 加载、能力摘要和窗口所有权校验；
`EDRClientAdapter` 只保留 HiSec 特殊启动与日志中心业务意图。`app_profile`
或命令行 `--profile` 选产品，`edr-wd products` 列出本地已安装项，
`open-app` 用通用的激活→角色聚焦→截图流程。`open-edr` 为兼容旧用法保留。

新增产品的原子实施与量化验收：

| 步骤 | 函数/文件 | 可执行目标 |
|---|---|---|
| 1 描述能力 | `target/product_specs/apps.json`、`register_app_catalog()` | 每个应用有安装路径及完整已知窗口表；角色进程非空、角色唯一、动作 ID 均在 backend catalog，非法输入拒绝且不部分注册 |
| 2 本地装载 | `load_installed_product_specs()`、`get_capability()` | Agent/target 读取同一描述，`profile_id/backend/version/roles/actions` 摘要一致；目标未安装时 `begin_product_run()` 拒绝 |
| 3 选择产品 | `supported_profiles()`、`get_product_adapter()`、`edr-wd products/open-app` | 正确平台可选，错误平台与未知 ID 明确失败；非 EDR 不调用 `activate_edr` |
| 4 意图映射 | `GenericProductAdapter.resolve_intent()`、`execute_intent()` | 一次语义点击、独立结果断言；同窗意图不误判为“已打开”，跨窗角色必须唯一 |
| 5 实机验收 | `focus_window_role()`、`HybridLocator.resolve()`、目标动作守卫 | 指定进程、窗口、锁和遮挡检查全部通过后才注入；连续两次成功且输出 trace，异常窗口 0 次误点 |

本地新增 Notepad/Calculator 选择与虚构编辑器意图单测；两款示例软件的实机
界面/菜单未在本轮验证，故不能据此宣称其业务意图已端到端通过。描述文件须进入
被部署的 tracked target payload；仅本地新增但未部署时，远端会拒绝摘要。

### 当前实施记录（2026-10-15）

| 阶段 | 本地落地与可核查证据 | 尚未达到的验收 |
|---|---|---|
| P1 产品适配 | `agent/products/`、`target/product_profiles.py`、`target/product_policy.py`；假产品注册、Profile 摘要、独占租约、动作过滤及 run 隔离已用单测覆盖 | 自定义产品在远端须预装同一 target 能力描述；尚未做目标机部署验证 |
| P2 窗口身份 | `target/windowing/` 建立 native ID、角色、父子关系、generation、墓碑与锁绑定；重建/同名歧义测试覆盖 | Windows 主→日志→弹窗→主的真实窗口事件序列未跑通 |
| P3 混合定位 | 同一 `HybridLocator` 供 Planner 候选、实时动作与黄金轨迹回放使用；观察包校验同窗身份、摘要与脱敏截图 | 历史录制在 EDRClient 上的连续两轮实机回放未验收 |
| P4 视觉/OCR | 模板作为融合证据参与语义消歧；目标端可选 OCR 只读取已遮蔽窗口图像，缺依赖明确返回 `unsupported` | 目标机 OCR 引擎、语言包、真实受保护控件负例尚未验证 |
| P5 指针守卫 | Windows 点击/拖拽/滚动、macOS 真实鼠标动作接入锁定窗口、前台、遮挡校验；dispatcher 在产品 run 内派发前重验 generation、窗口矩形与语义控件指纹；公开 MCP 指针入口的静态覆盖审计通过 | 公开入口的实机负例注入计数及 Windows/macOS 遮挡试验未完成 |
| P6 业务意图 | `edrclient.log_center.open` 经适配器角色、统一执行器与独立结果断言执行；通用 `intent` 入口按注册表解析 | 日志中心内容与页签断言的真实 2/2 连续执行未验收 |

本地自动化实测：默认 `python3 -m pytest -q` 为 1735 passed、612 deselected；`python3 -m pytest -q -m 'unit or regression'` 为 2320 passed、27 deselected；`git diff --check` 与目标配置 JSON 语法检查通过。这些只证明模拟环境与既有兼容性；目标 Windows 终端两次连接探测均超时，代码未部署，任何实机目标均不得标记为通过。旧的 `activate_edr` 和 HiSec 兼容启发式暂留在平台实现中；新产品须使用通用注册与窗口/定位路径，不应复制这些兼容分支。运行租约只负责独占与过期隔离，不代替 MCP/SSH 传输认证。

### 后续增量（2026-10-16）

- 实时 `execute_live_action()` 接受经摘要验证的脱敏模板，由当前窗口观察动态产生视觉候选，并与控件/OCR 候选一起交给 `HybridLocator`；适配器意图可声明可选视觉模板，但 EDRClient 日志中心尚无审定模板资产。
- Windows 录制窗口事件订阅以同一安装根目录识别跨进程子窗口，保留真实进程归属；仍需 11.26 目标机验证。
- `agent/consistency/` 新增端云快照关联、字段对比与限时轮询；Browser Use 取数绑定、EDR-WD 实机抽取及端云联测仍待接入验收。
- 录制断言新增 `text_time_range` / `window_text_time_range`，支持时间前缀与 `durationSeconds`，并修正 ID 回放被录制时动态文本卡住的问题；离线双模式两轮回放测试已增加。目标机 SSH 状态探测仍超时，不能宣称实机完成。
- target 新文件已用 Git intent-to-add 纳入 tracked-only 部署清单；未提交也未部署，正式发布必须提交并在目标机完成两轮录制/回放验收。

## 1. 现状与问题

现有 `agent/planner/` 以 action catalog 约束动作计划，`target/automation/` 提供平台驱动，`agent/recording/` 支持录制、稳定选择器、语义回放以及受保护的图像模板回退。`connect → lock_window → verify_window_lock → observe → action → verify` 是当前安全链路。Windows 的 `scroll_window` 另有唯一窗口、坐标转换与遮挡检查。

需要补齐的边界如下：

| 当前实现 | 影响 | 目标 |
|---|---|---|
| `app_profile` 是固定枚举；Planner 的 `profile` 参数尚无产品级动作过滤 | 通用计划可能看到 HiSec 专用动作 | 产品适配器统一提供能力与业务语义，执行侧也校验产品能力 |
| Windows `activate_app` 不承担通用启动；`activate_edr`、`open-edr` 包含产品常量 | 新产品需改通用代码 | 平台提供启动/激活原语，EDRClient 适配器编排其特殊启动流程 |
| backend 保留单个连接窗口及 `_window_lock` | 同进程的主窗、日志中心、弹窗切换依赖标题和临时重连 | 会话内窗口注册表记录身份、角色、关系和生命周期 |
| 回放的 `semantic_first` 仅在语义零匹配时使用图像 | 语义歧义、图像与控件冲突无法联合判断 | 统一候选与证据模型，按策略融合、拒绝歧义和冲突 |
| 模板匹配仅允许受保护的点击回退；尚无 OCR 通道 | 无稳定控件 ID 的文字按钮难以定位 | OCR 作为定位证据，不能直接成为业务断言或敏感数据持久化来源 |
| 部分窗口相对点击、拖拽和滚动有不同的守卫路径 | 遮挡、窗口移动、焦点变化时行为不一致 | 所有指针动作复用同一目标端窗口守卫 |

本文不改动已有黄金轨迹的含义，不把旧录制的屏幕坐标升级成稳定定位，也不让自然语言直接生成未经 catalog/confirmation 校验的目标端动作。

## 2. 设计目标与不变量

1. EDRClient 保持主要产品适配对象；产品差异由 `ProductProfile` 与 `ProductAdapter` 表达，平台 backend 只处理 OS 能力。
2. 意图、录制回放与手动实时执行共用窗口选择和混合定位策略；三条入口仍通过现有 action catalog 与 `AtomicExecutor` 执行动作。
3. 每一步使用新观察生成 observation-local `TargetRef`。跨运行可保存产品角色、语义选择器和脱敏视觉模板，不持久化可执行的旧 PID、HWND、`target_id` 或绝对坐标。
4. 指针注入前，在目标端检查锁定窗口身份、点位归属与遮挡；无法证明时失败，不用另一个进程/窗口“凑一个命中”。
5. OCR、截图和模板遵守目标端源头脱敏、窗口作用域和摘要校验；密码、令牌及作用域外内容不进入 Agent 产物。
6. 动作成功与业务成功分开：每个业务意图声明可检查的结果断言；未确认的写操作继续受现有风险与确认机制约束。

## 3. 分层与调用链

```text
用户测试目标 / 手工操作 / 黄金轨迹
  → ProductAdapter：业务意图、页面角色、允许动作、断言
  → Planner / Recording compiler / Replay materializer
  → WindowRegistry：解析角色与当前窗口身份
  → Observation：窗口状态 + 控件树 + 已脱敏窗口图像 + 可选 OCR
  → HybridLocator：生成候选及定位证据，选出唯一目标
  → AtomicExecutor → action catalog → target MCP dispatcher
  → target WindowGuard：即时复核身份、焦点、点位与遮挡
  → UIA / Accessibility / 受控指针动作
  → 新观察 + 显式断言 + append-only trace / 报告
```

`ProductAdapter`、意图编排、定位决策属于 Agent；窗口枚举、截图脱敏、平台 OCR 采集和动作前守卫属于 target。Agent 不直接用图像坐标注入 OS 输入，target 不解释“打开日志中心”这类业务意图。

## 4. ProductProfile 与 ProductAdapter

### 4.1 契约

`ProductProfile` 是版本化、只读的产品描述，最少包括：

```text
profile_id, profile_version, supported_backends
application_id / process aliases / bundle id
window_roles: role → ownership criteria + root control hints + relation rules
allowed_action_ids: backend 与产品能力的交集
pages: page_id → window role + stable landmarks
intents: intent_id → preconditions + action sequence template + result verifiers
```

这些字段不保存目标机凭据或运行时会话数据；受信任的目标本地应用目录单独保存安装路径。`config/targets.local.json` 继续负责目标连接与可选的 `app_profile` 覆盖；新增产品描述由目标本地安装并由动态配置校验识别，无需修改 schema 枚举或将业务选择器塞进连接配置。未知 Profile 必须显式失败；仅缺省选择才回到目录声明的 EDRClient。

`ProductAdapter` 对上提供 `resolve_intent`、`resolve_window_role`、`page_landmarks`、`allowed_actions`、`expected_result`；对下只调用通用窗口与动作接口。实现中的意图模板是可审阅的结构化步骤，不是任意脚本。Planner 构建提示、计划校验、回放预检和 target dispatcher 的策略应使用同一个产品能力集合；仅在 Planner 隐藏动作不足以构成执行约束。

当前 `target/server.py` 使用共享 backend，`execute_action` 参数里也没有可信的 MCP 会话身份，因此不能直接把 `app_profile` 视为“已绑定会话”。实施时增加 target 本地的 `ProductRunContext`：部署时只同步不含凭据的 Profile ID/摘要允许清单；`begin_product_run(profile_id, digest)` 只接受本机允许值，返回有租约的随机 `run_token`；`execute_action`、窗口锁与新的观察接口使用该 token 读取 target 保存的上下文，不从单次请求的 `profile_id` 决定权限。同一 target 同时只允许一个可变更 GUI 的 run；活跃 run 期间，不带 token 的旧 GUI 变更工具拒绝执行，不能抢占窗口锁。无活跃 run 时，旧工具按本机允许 Profile 和同一锁策略执行。run 结束或租约到期清理锁与缓存。部署配置、run token 与 SSH/MCP 身份认证的关系须在阶段 1 用并发测试验证，不能仅凭客户端传入的 token 认为调用方可信。

首个 `EDRClientAdapter` 描述 HiSec 入口窗口、EDRClient 主窗口、日志中心及其弹窗的角色、进程归属、启动/恢复路径和结果断言。原有 `activate_edr` 可在迁移期保留兼容入口，但通用 `activate_app` 应提供按已注册应用标识激活已安装程序及聚焦窗口的原语；EDR 特殊命令仍只存在于适配器或明确的 HiSec 扩展中。

### 4.2 与现有模型的关系

- `ActionStep` 继续表示一个 catalog 动作；`IntentPlan` 是适配器生成的上层步骤集合，不增加第二套执行器。
- 产品页面和窗口角色是稳定选择依据，运行时再解析为 `WindowIdentity`；不能把进程名或标题单独视为身份。
- `expected_result` 产生现有 `Expectation` 或经版本化扩展的等价断言。适配器不能从“此刻看到的值”自动推断测试预期。
- 产品能力覆盖现有 backend 缺口表：实际可用动作取 `catalog ∩ backend ∩ profile ∩ runtime policy`。

## 5. WindowIdentity 与 WindowRegistry

### 5.1 身份模型

`WindowIdentity` 为运行时观察值，建议字段：

| 字段 | 用途 |
|---|---|
| `session_id`、`backend`、`process_id`、`process_name`/`bundle_id` | 会话及进程归属 |
| `native_id` | Windows HWND 或 macOS CGWindowID/AX window reference；只在当前会话有效 |
| `role`、`title`、`root_control_id`、`class_name` | 产品角色及可解释的匹配证据 |
| `parent_native_id`、`opened_by_action_id` | 父子/因果关系；无法证明时标记未知，不猜测 |
| `rect`、`visible`、`enabled`、`foreground`、`generation`、`observed_at` | 生命周期与新鲜度 |

`native_id` 不足以单独作为身份：句柄可复用，窗口也可能重建。`generation` 在同一原生 ID 的进程、根控件或创建事件变化时递增；动作绑定 `(session_id, native_id, generation, process_id)`，在输入前复核。macOS 原生 ID 取不到时允许可验证的 PID + AX 引用 + 根控件组合；若同类窗口仍不唯一，拒绝定位。

`WindowRegistry` 由 target 会话拥有，维护 `discovered → visible → focused/locked → hidden → closed` 生命周期，接收枚举快照与平台窗口事件。枚举是事实校准源，事件仅用于缩短发现延迟；关闭后保留有限期墓碑，防止窗口重建时误用旧身份。父子关系优先使用 OS owner/parent，随后使用同进程且有因果动作的打开事件；只有时间接近不能建立父子关系。

对外提供 `list`, `resolve_unique(role, profile)`, `focus_and_lock(identity)`, `verify(identity)`, `wait_transition(before, expected, timeout)`。`resolve_unique` 返回零个或多个候选及原因；角色命中多个窗口时必须由根控件、父窗口或明确的测试上下文消歧。切换窗口时先重新解析并锁定目标，再刷新控件树与截图；旧窗口锁不会自动覆盖新窗口。录制中的临时弹窗可通过 `opened_by_action_id` 扩展作用域，关闭后撤销。

## 6. HybridLocator 与定位证据

### 6.1 统一输入输出

`HybridLocator.resolve(request, observation, policy) → Resolution | ResolutionError`。`request` 包括产品角色、窗口角色、动作类型、语义选择器、可选 OCR 文本/区域、可选图像模板与相对落点；`observation` 是一个目标端采集的 `WindowObservationBundle`，携带同一锁定窗口、限定采集区间内的控件树、窗口图像、OCR 框和元数据。输出包含唯一窗口身份、目标矩形/新鲜 `TargetRef`、定位方式、各证据、有效期和拒绝原因。Planner 可查询候选以规划，实时执行与回放用同一个 `resolve` 作最终决策。

现有 `MCPObservationProvider.refresh()` 顺序调用 `verify_window_lock`、`dump_tree`、`replay_capture`，两次观察之间窗口可能变化。新增 target 原子观察接口 `observe_window_bundle(run_token, capture_image, include_ocr)`：在一个窗口锁下依次核对 identity → 控件树 → 脱敏截图/OCR → identity；前后 `native_id/generation/rect` 不一致时丢弃整包并返回 `observation_window_changed`。bundle 有唯一 `observation_id`、采集起止时间、截图摘要与坐标变换；超过策略规定的采集时长则标为过期。相同窗口身份仍不能证明动态内容完全不变，因此定位完成到注入前还要复核目标矩形/可见性；内容持续变化时返回 `observation_stale`。Agent 只在校验整包后建立 `TargetRef`；旧 `refresh()` 在迁移期仅走语义兼容路径，不把不同调用所得树和图像强行合并。

语义候选来自 automation ID、role/type、名称、稳定祖先与状态；视觉候选来自模板匹配；OCR 产生文字框及识别置信度。候选按窗口、页面区域和矩形相交关系对齐，证据必须属于同一窗口与同一快照。坐标转换显式记录窗口原点、DPI/缩放和截图尺寸，不以录制时绝对坐标回放。

### 6.2 决策规则

1. 先验证窗口身份、截图作用域、脱敏状态、模板摘要、观察时效和动作允许的定位方式；缺少必要证据即拒绝。
2. 控件候选唯一且身份证据稳定时可使用语义定位；若同时存在图像/OCR 证据且与其明显冲突，返回 `locator_evidence_conflict`，不把冲突当作普通回退。
3. 语义候选多个时，使用同窗口图像/OCR/区域交集消歧；只有一个候选满足硬约束并与第二名有足够区分度才选中，否则 `locator_ambiguous`。
4. 语义零候选时，仅对策略允许的动作采用视觉/OCR 定位。V1 先覆盖受保护的点击；输入、选择、拖拽须有可验证的焦点/起止目标与结果断言后再逐项开放。
5. `score` 只作为同类证据排序，阈值按设备缩放和模板质量校准；进程/窗口归属、保护区域、动作许可和候选唯一性是硬门槛，不能靠加权分数抵消。

OCR 引擎作为可选平台能力，返回文本、矩形、置信度与语言；图像/文字匹配均在目标端完成必要遮蔽后才能传给 Agent。不得落盘未脱敏整屏截图或原始 OCR 全文。OCR 仅帮助找控件，不证明业务状态正确；结果仍由明确的 UI/日志/状态断言判断。

现有 `semantic_only`、`semantic_first`、`visual_only` 回放参数在迁移期保留：前两者映射到 HybridLocator 的证据策略；`visual_only` 继续作为视觉能力评估模式。旧 golden selector 按原 schema 读取并在运行时转换，新增字段须版本化，旧轨迹不能静默获得新能力。trace 记录候选数、使用的证据类型、置信度区间、拒绝原因及最终定位方式，避免记录敏感文字或图像内容。

## 7. 统一窗口守卫

目标端提供 `WindowGuard.authorize_pointer(identity, points, coordinate_space, action)`，由窗口相对点击、双击、右键、滚动和拖拽共用；拖拽校验起点、终点和可接受路径。单次动作应在注入输入的 backend 方法内完成检查，覆盖 `execute_action` 和旧的 `click_window_at`、`scroll_window`、`drag` 等 MCP 工具，避免绕过 dispatcher：

1. 当前锁与目标 `WindowIdentity` 一致，原生 ID、generation、进程归属、窗口矩形和前台状态可验证。
2. 将窗口相对点转换为屏幕点，确认每个点处于窗口可交互区域；窗口移动后重新取矩形，不复用观察期坐标。
3. 使用 OS hit-test 与顶层窗口枚举确认点位归属、遮挡和覆盖弹窗。目标产品自己的弹窗也不能冒充被锁定父窗口。
4. 复核过程中窗口消失、身份变化、被遮挡或焦点转移时返回稳定错误码，不执行输入，也不自动改选相似窗口。
5. 执行后重新观察并运行结果断言；`ok=true` 仅证明动作已派发，不能证明 UI 或业务状态变化。

Windows 现有 `window_scope.py` 的纯函数可作为统一守卫的起点；原 `scroll_window` 逻辑迁移到共用入口。语义动作若最终以屏幕指针注入，也必须经过同一守卫。守卫检查后若 OS 注入 API 与检查之间仍发生窗口变化，回执只能标记结果未知并停止后续自动重试；对写操作不得盲目重发。无输入桌面的 RDP 状态维持 fail closed；降级观察不能授权坐标输入。macOS 使用 AX 窗口归属、CGWindowList 与可用的前台/遮挡信息实现同等契约；平台无法提供等价证据时返回能力缺口，不假定安全。

## 8. 意图层：EDRClient 示例

意图记录格式建议为 `intent_id + profile_id + arguments + expected_result`，由适配器编译为已有动作步骤和断言。例如 `edrclient.log_center.open`：

```text
前置：EDRClient 主窗口角色可唯一解析；当前用户具备访问日志中心能力
1. focus_and_lock(main_window)
2. HybridLocator 找到“日志中心”入口，执行 catalog 中的点击动作
3. WindowRegistry 等待 log_center 角色窗口出现，关联 opened_by_action_id
4. focus_and_lock(log_center)，刷新观察
结果：日志中心窗口身份正确，关键页面控件/标题可见
```

窗口已经打开时，适配器先判断是否满足结果状态，避免重复点击；若打开但内容错误，返回明确失败并保存证据。`edrclient.log_center.select_tab` 等意图依照相同模式声明页签、页面可见性或日志行变化断言。业务文本、别名和期望状态属于适配器，不能硬编码进 `HybridLocator` 或 OS backend。

云端控制台与端侧一致性场景可在更高层编排：浏览器侧 Skill 获取云端状态或接口响应，EDR-WD 提供终端观察与断言，按设备/策略/时间窗口关联并等待同步；该编排不改变本设计中的客户端定位与窗口安全边界。

## 9. 实施顺序与验收

| 阶段 | 主要改动 | 最低验收 |
|---|---|---|
| 1. 产品适配 | Profile/Adapter 注册、EDRClient 适配、能力过滤与配置校验 | 旧 EDR 流程保持可用；通用 Profile 看不到也执行不了 `hisec.*`；未知 Profile 拒绝 |
| 2. 窗口身份 | target WindowIdentity/Registry、角色解析、生命周期与父子关系 | 同进程同标题窗口不能误选；窗口重建/句柄复用使旧锁失效；弹窗开闭可追踪 |
| 3. 共用定位器 | Agent HybridLocator 接入实时执行、录制回放与 Planner 候选视图 | 三入口对同一观察给出相同候选/拒绝原因；回放不复用旧 `target_id` |
| 4. 视觉与 OCR | 同快照证据融合、歧义/冲突拒绝、OCR 脱敏与阈值校准 | 控件歧义可由交叉证据唯一消解；冲突、低置信和敏感区域均拒绝 |
| 5. 窗口守卫 | 点击、滚动、拖拽统一目标端校验 | 窗口移动、遮挡、弹窗覆盖、焦点变化、无输入桌面均不会误注入 |
| 6. 业务意图 | EDRClient 日志中心等意图映射、前置条件和结果断言 | 从意图到执行/证据/报告闭环；重复执行与失败恢复可解释 |

每阶段先完成模型及接口契约，再做 Windows EDRClient 实机验证；macOS 对等行为须标记明确的能力矩阵和单独实机结果。必须覆盖主窗→日志中心→弹窗→主窗的跨窗口流程，以及控件缺失、重复控件、图像误匹配、OCR 误识别、窗口重建、RDP 断连等失败路径。回放至少连续成功两次，执行 trace 完整、所有 required step 和显式断言通过，不能以截图相似或动作回执代替业务结果。

## 10. 预期代码归属与兼容策略

```text
agent/products/                 Profile/Adapter、EDRClient 意图与页面规则
agent/locator/                  HybridLocator、候选与证据策略
agent/planner/                  消费适配器能力和定位候选
agent/recording/                选择器编译、旧格式转换、共用定位器接入
agent/execution/                保持唯一执行状态机和断言/确认入口
target/windowing/               WindowIdentity、Registry、WindowGuard
target/automation/              UIA/AX/图像/OCR 平台原语
target/action_dispatcher/       动作前目标端授权及 Profile 能力复核
```

新增模型通过版本化 schema 和转换器接入现有 `ReplaySelector`、`TargetRef`、`ObservationSnapshot`；不直接更改旧产物。先以兼容适配调用旧 `connect`、`lock_window`、`activate_edr`，完成阶段验收后再逐步替换入口。`open-edr` 作为兼容命令继续路由 EDRClientAdapter，通用入口使用产品角色而非固定标题。每次迁移在 trace 中标明所用适配器版本、窗口身份 generation、定位方式与守卫结果，以便比较新旧路径。

设计评审重点：Profile 能力是否在执行端真正生效；窗口关系是否能在不同进程/平台上证明；视觉/OCR 数据是否始终窗口限定且脱敏；所有指针动作是否共用同一个即时守卫；旧黄金轨迹与现有 EDRClient SOP 是否保持可回放。

## 11. 自省自查与已修订决策

| 自查发现 | 原设计的隐含假设 | 修订后的可验证约束 |
|---|---|---|
| Profile 隔离 | `app_profile` 能自然绑定 MCP 会话 | 当前 target 共享 backend；新增 target 本地允许清单、租约 run context 和并发拒绝，单次 action 参数不能声明自身权限 |
| 幂等回执 | 新策略检查一定先于缓存返回 | 当前 dispatcher 先查 `request_id` 缓存；缓存键必须包含 run ID，且先校验 run token，避免旧回执跨 Profile 复用 |
| 混合证据一致性 | 分别调用 `dump_tree` 与截图就是同一画面 | target 生成 `WindowObservationBundle`，前后复核身份/矩形；不一致整包作废 |
| 守卫覆盖面 | 在 `execute_action` 检查即可覆盖全部输入 | 旧 MCP 工具直接调用 backend；守卫放到实际注入点并逐一统计覆盖 |
| 平台等价性 | macOS 能提供 Windows HWND 同等级别身份证据 | macOS 使用可验证的 CGWindowID/AX 引用组合；无法区分同类窗口即拒绝，并记录能力缺口 |
| 视觉安全 | 模板匹配分数足够高就能点击 | 归属、唯一性、截图脱敏、动作权限、遮挡均是硬门槛；分数只能排列有效候选 |
| 旧产物兼容 | 新模型可以直接替换 ReplaySelector | 旧 schema 保持原义，运行时显式转换；新字段、错误码、trace 记录均版本化 |

本设计尚未证明 OCR 在所有 UI 缩放和语言下可用，也尚未证明 macOS 的遮挡信息等价于 Windows hit-test；这两项由阶段 4、5 的实机门槛决定是否开启对应能力。实时操作当前存在直接 MCP 工具路径，阶段 3 必须提供经过 `AtomicExecutor` 的统一入口，不能声称现状已共用执行器。

## 12. 原子实施清单

以下每一行是一次独立、可评审的代码变更。路径和函数名是拟定的正式接口；实施时若已有同义函数，保留现有函数并在评审中说明映射，不另造并行实现。`0/20` 表示在 20 次重复负例中零次误派发，`N/N` 表示固定样例全部通过。每项均需在完成前给出对应自动化测试或实机回执，不能只验证返回值格式。

### 12.1 阶段 1：产品适配

| ID | 代码落点与拟定函数 | 单一交付物 | 可量化完成条件 |
|---|---|---|---|
| P1.1 | `agent/products/models.py::ProductProfile.validate()` | 版本化 Profile 模型与字段约束 | 4 个合法/非法模型样例 4/4；缺少角色或未知 backend 100% 拒绝 |
| P1.2 | `agent/products/registry.py::get_product_profile(profile_id, backend)` | Profile 注册与 target 配置解析 | `windows_hisec`、`macos_hisec`、`macos_generic` 三种已知组合 3/3；未知 Profile 2/2 拒绝 |
| P1.3 | `agent/products/edrclient.py::EDRClientAdapter.resolve_window_role(role)` | EDR 入口、主窗、日志中心、弹窗的角色规则 | 4 个角色 4/4 可解析；同标题跨进程样例 2/2 不混淆 |
| P1.4 | `target/product_policy.py::load_allowed_profiles(manifest)` | target 本机 Profile 允许清单 | 允许/未知/摘要被篡改三例 3/3；未在本机清单的 Profile 不能绑定 |
| P1.5 | `target/product_policy.py::begin_product_run(profile_id, digest)` | 独占、限时 run context | 允许 Profile 1/1 可绑定；未知/摘要不符/并发抢占 3/3 拒绝 |
| P1.6 | `target/product_policy.py::end_product_run(run_token)` | run 结束或租约到期清理 | 主动结束/超时两例 2/2 清理窗口锁与缓存；过期 token 1/1 拒绝 |
| P1.7 | `target/product_policy.py::authorize_legacy_gui_call(active_run, tool_name)` | 活跃 run 阻止旧工具抢锁/变更 GUI | 活跃 run 下旧点击、拖拽、窗口锁三例 3/3 拒绝；run 结束后兼容调用 1/1 可用 |
| P1.8 | `target/action_dispatcher/conditions.py::check_product_action(context, action_id)` | dispatcher 产品能力闸门 | HiSec Profile 合法动作 2/2 放行；generic 对 `hisec.*` 2/2 拒绝；无上下文写动作 1/1 拒绝 |
| P1.9 | `agent/planner/catalog_view.py::enabled_actions_for(backend, profile)` | Planner 使用同一能力交集 | 三种现有 Profile 各与执行闸门集合相等 3/3；generic 暴露 `hisec.*` 数为 0 |
| P1.10 | `target/action_dispatcher/cache.py::scoped_request_key(run_id, request_id)` | 幂等缓存按 run 隔离，且查缓存前校验 run | 同 run 重复请求仅执行 1 次；跨 run 同 request ID 2/2 不复用旧回执；过期 token 1/1 拒绝 |
| P1.11 | `agent/products/edrclient.py::EDRClientAdapter.activate()` | EDR 入口与主窗口的产品启动编排 | 已启动/未启动/启动失败三例 3/3；失败不能虚报主窗可用 |
| P1.12 | `target/automation/windows_pywinauto.py::activate_registered_app(application_id)` | Windows 已注册应用的通用启动/聚焦原语 | 已运行/未运行两例 2/2；未注册应用和任意路径启动 2/2 拒绝 |
| P1.13 | `agent/products/registry.py::register_product_adapter(adapter)` | 新产品接入边界验证 | 一个最小假产品仅新增 Profile/Adapter 和配置 schema 条目即可通过 1/1；通用定位/窗口模块 EDR 字面量数为 0 |

### 12.2 阶段 2：窗口身份与生命周期

| ID | 代码落点与拟定函数 | 单一交付物 | 可量化完成条件 |
|---|---|---|---|
| P2.1 | `target/windowing/models.py::WindowIdentity.from_native(snapshot)` | 平台窗口字段标准化和 generation 契约 | Windows/macOS/缺失 native ID 三类样例 3/3；跨会话旧 ID 1/1 拒绝 |
| P2.2 | `target/windowing/registry.py::WindowRegistry.reconcile(windows)` | 枚举快照增删与墓碑 | 打开、隐藏、关闭、重建 4 条状态序列 4/4；句柄复用旧锁 1/1 失效 |
| P2.3 | `target/windowing/registry.py::WindowRegistry.apply_event(event)` | 平台事件更新及枚举校准 | 事件乱序/重复/漏报 3/3 经枚举收敛；不产生重复活动窗口 |
| P2.4 | `target/windowing/relations.py::link_parent(child, candidates, cause)` | OS 父子与因果关联 | OS owner、动作因果、仅时间接近 3/3 正确；仅时间接近时父关系为空 |
| P2.5 | `target/windowing/registry.py::WindowRegistry.resolve_unique(role, profile)` | 角色到唯一窗口的解析 | 主窗/日志窗 2/2 命中；同标题双窗 1/1 报歧义，错误码稳定 |
| P2.6 | `target/windowing/registry.py::WindowRegistry.focus_and_lock(identity)` | 身份绑定的窗口切换 | 主→日志→弹窗→主 4/4 切换后锁与窗口一致；重建旧身份 1/1 拒绝 |

### 12.3 阶段 3：统一定位入口

| ID | 代码落点与拟定函数 | 单一交付物 | 可量化完成条件 |
|---|---|---|---|
| P3.1 | `target/windowing/observation.py::observe_window_bundle(run_token, capture_image, include_ocr)` | 同一窗口身份下的观察包 | 稳定窗口 1/1 有完整摘要；采集中移动/替换/超时 3/3 整包作废，均不返回部分图像 |
| P3.2 | `agent/recording/mcp_runtime.py::MCPObservationProvider.refresh_bundle()` | Agent 校验观察包并建立快照 | 摘要正确 1/1 接收；窗口 ID、generation、截图摘要错误 3/3 拒绝 |
| P3.3 | `agent/locator/hybrid.py::HybridLocator.collect_candidates(request, bundle)` | UIA/AX 控件候选的统一表示 | 唯一、重复、缺失控件 3/3 输出正确候选数；候选均带同一 observation ID |
| P3.4 | `agent/locator/hybrid.py::HybridLocator.resolve(request, bundle, policy)` | 唯一定位或结构化拒绝 | 唯一/零个/多个候选 3/3 按策略处理；零候选无视觉时 1/1 不派发 |
| P3.5 | `agent/recording/replay.py::GoldenStepMaterializer.__call__()` | 回放改走 `HybridLocator.resolve` | 旧黄金轨迹固定样例 2/2 行为一致；每步旧 `target_id` 复用次数为 0 |
| P3.6 | `agent/interaction/runtime.py::execute_live_action(request)` | 实时操作统一进入 AtomicExecutor | 语义点击、视觉点击、失败断言 3/3 留下 action/expectation trace；无直接旁路 |
| P3.7 | `agent/planner/candidates.py::candidate_view(bundle, request)` | Planner 消费同一候选集的脱敏视图 | 与实时定位候选 ID 集合 3/3 一致；提示词中原始截图/OCR 全文出现次数为 0 |

### 12.4 阶段 4：图像、OCR 与交叉证据

| ID | 代码落点与拟定函数 | 单一交付物 | 可量化完成条件 |
|---|---|---|---|
| P4.1 | `target/automation/ocr.py::extract_redacted_text(window_png, masks)` | 从已脱敏窗口图像产生 OCR 框 | 普通/受保护/窗口外三类样例 3/3；受保护原文落盘与上传次数为 0 |
| P4.2 | `agent/locator/evidence.py::align_evidence(candidates, templates, ocr_boxes)` | 控件、模板与 OCR 的同窗几何对齐 | 同窗/跨窗/不同快照 3/3 判定；跨窗证据合并次数为 0 |
| P4.3 | `agent/locator/policy.py::choose_unique_candidate(evidence, policy)` | 歧义消解和冲突拒绝 | 双控件由图像消歧 1/1；语义与视觉冲突、低置信、同分 3/3 拒绝；误派发 0/20 |
| P4.4 | `agent/locator/policy.py::mode_to_locator_policy(replay_mode)` | 旧 `semantic_first`/`visual_only` 策略映射 | 三种旧 replay mode 固定样例 3/3 语义一致；旧 schema 加载 2/2 |
| P4.5 | `agent/locator/trace.py::record_resolution_decision(trace_store, result)` | 仅记录可审计的定位元数据 | 成功/拒绝 2/2 可追溯；敏感文字、原始图片、完整 OCR 内容字段数为 0 |

### 12.5 阶段 5：统一窗口守卫

| ID | 代码落点与拟定函数 | 单一交付物 | 可量化完成条件 |
|---|---|---|---|
| P5.1 | `target/windowing/guard.py::WindowGuard.authorize_pointer(identity, points, coordinate_space, action)` | 统一归属、遮挡、坐标校验 | 窗口内、越界、遮挡、身份变化、无输入桌面 5/5；负例误派发 0/20 |
| P5.2 | `target/windowing/guard.py::WindowGuard.verify_target_before_injection(resolution)` | 注入前重验目标矩形、可见性与观察有效期 | 目标未变 1/1 放行；移动、隐藏、内容刷新三例 3/3 返回 `observation_stale` |
| P5.3 | `target/automation/windows_pywinauto.py::_guarded_pointer(action, points, identity)` | Windows 指针注入点接入守卫 | click/double/right/scroll/drag 五类 5/5 走守卫；旧 MCP 与 dispatcher 各抽样 5/5 |
| P5.4 | `target/automation/macos_accessibility.py::_guarded_pointer(action, points, identity)` | macOS 指针注入点接入同等契约 | 可证明归属 1/1 放行；无法证明窗口/遮挡 2/2 拒绝；负例误派发 0/20 |
| P5.5 | `target/windowing/guard.py::WindowGuard.authorize_drag_path(identity, start, end, samples)` | 拖拽路径与弹窗覆盖检查 | 起点、终点、中点被遮挡三例 3/3 拒绝；元素拖拽正常例 1/1 放行 |
| P5.6 | `test_case/test_window_guard_coverage.py::test_all_pointer_tools_are_guarded()` | 旧 MCP 与 dispatcher 路由覆盖审计 | `target/server.py` 中所有公开指针 MCP 工具逐项清点，覆盖率 100%；任一入口负例注入次数为 0 |

### 12.6 阶段 6：EDRClient 业务意图

| ID | 代码落点与拟定函数 | 单一交付物 | 可量化完成条件 |
|---|---|---|---|
| P6.1 | `agent/products/intent.py::validate_intent_request(request, profile)` | 意图 ID、参数及预期结果模型 | 已注册/未知/缺少 expected result 三例 3/3；未知意图 100% 拒绝 |
| P6.2 | `agent/products/edrclient.py::EDRClientAdapter.resolve_intent(intent_id, args)` | “打开日志中心”的结构化步骤和断言 | 窗口关闭/已打开/同名伪窗三例 3/3；已打开时重复点击数为 0 |
| P6.3 | `agent/products/edrclient.py::EDRClientAdapter.expected_result(intent_id, args)` | 日志中心/页签可见性业务断言 | 打开成功、页面错误、日志未变化三例 3/3；仅动作 `ok=true` 不计通过 |
| P6.4 | `agent/interaction/runtime.py::execute_intent(request)` | 意图编排接现有 executor/trace/report | Windows EDRClient 主→日志→弹窗→主 2 次连续回放 2/2；required step/断言通过率 100% |
| P6.5 | `agent/cli.py::main()` 的兼容路由与新意图入口 | `open-edr` 继续委托 EDR 适配器 | 旧命令、录制、回放三条冒烟流 3/3；通用入口 EDR 进程/标题字面量数为 0 |

## 13. 统一度量与阶段门槛

验收数据按运行记录保存：Profile/Adapter 版本、backend、窗口 role 与 generation、观察包 ID、候选数、定位证据类型、动作守卫结果、断言结果、trace 完整性。报告汇总下列计数；安全负例的分母按重复次数实算，不把跳过算作通过。

| 指标 | 计算方式 | 阶段目标 |
|---|---|---|
| 错误窗口注入 | 输入实际落在非目标窗口的次数 | 所有自动化负例与 Windows/macOS 已启用能力的实机验证均为 0 |
| 未授权动作执行 | Profile 不允许或无有效 run context 时成功派发数 | 0；P1.7—P1.10 所列正负例 100% 通过 |
| 定位歧义拒绝 | 多候选/证据冲突时拒绝次数 ÷ 应拒绝次数 | 100%；相应场景至少重复 20 次，误派发 0 |
| 观察包一致性 | identity、rect、采集时长与摘要全部合格的已接受包 ÷ 已接受包 | 100%；不一致或过期包接受数为 0 |
| EDRClient 意图通过 | required step、显式断言、cleanup、trace 完整性全部通过的运行数 ÷ 运行数 | 阶段 6 Windows 实机连续 2/2；失败运行不计成功 |
| 兼容回放 | 阶段前冻结的旧 golden 样例可加载且语义/结果不漂移 | 固定回归样例 2/2；若旧轨迹本身 incomplete，仍须按旧规则拒绝 |

前一阶段的模型、自动化正负例与对应运行回执齐全后才进入下一阶段。实机不具备 OCR/AX/遮挡证明时，该平台能力标记 `unsupported` 并禁止相应指针动作；不得用其他平台的通过数据代替。固定样例在实施前冻结，新增故障必须补入样例集；量化指标以可审计的测试输出和 execution trace 为准，不以文档中的目标数冒充已完成结果。

## 14. 关键接口签名与错误契约

以下是实施时需冻结的公共接口草案。`Result[T]` 表示成功数据或带稳定 `code` 的失败，不允许靠空字典表示“没有找到”。新增 MCP 字段先走可选扩展及版本协商，再升级为必填；已有直接 Python 调用和 legacy MCP 入口保持可解析。

```python
def get_product_profile(profile_id: str, backend: str) -> ProductProfile: ...
def begin_product_run(profile_id: str, digest: str) -> Result[ProductRunContext]: ...
def end_product_run(run_token: str) -> Result[None]: ...
def check_product_action(context: ProductRunContext, action_id: str) -> Result[None]: ...
def execute_action(
    action_id: str, args: dict, target_ref: dict | None,
    request_id: str, run_token: str | None = None,
) -> ActionReceipt: ...

def reconcile(self, windows: list[NativeWindow]) -> RegistryDelta: ...
def resolve_unique(self, role: str, profile: ProductProfile) -> Result[WindowIdentity]: ...
def focus_and_lock(self, identity: WindowIdentity) -> Result[WindowIdentity]: ...
def observe_window_bundle(
    run_token: str, capture_image: bool, include_ocr: bool,
) -> Result[WindowObservationBundle]: ...

def collect_candidates(
    self, request: LocatorRequest, bundle: WindowObservationBundle,
) -> tuple[LocatorCandidate, ...]: ...
def resolve(
    self, request: LocatorRequest, bundle: WindowObservationBundle,
    policy: LocatorPolicy,
) -> Result[Resolution]: ...
def authorize_pointer(
    self, identity: WindowIdentity, points: tuple[Point, ...],
    coordinate_space: str, action: str,
) -> Result[GuardDecision]: ...
def execute_intent(request: IntentRequest) -> CaseRunResult: ...
```

`Resolution` 仅在 `semantic` 模式提供新鲜 `TargetRef`；`visual`/`ocr_assisted` 模式提供窗口相对落点与目标身份，由目标端守卫转换坐标。任何模式都必须携带 `observation_id` 和 `WindowIdentity.generation`。执行前若观察包失效，则重新观察并最多重新定位一次；已经发出可能改变产品状态的动作后不自动重发。

| 错误码 | 触发条件 | 执行结果 |
|---|---|---|
| `product_profile_unknown` / `product_action_forbidden` | Profile 未注册或动作不在能力集合 | 阻止计划/派发 |
| `product_run_conflict` / `product_run_expired` | 同 target 并发争用或租约失效 | 阻止新 GUI 动作，要求重新建立 run |
| `window_role_ambiguous` / `window_identity_stale` | 同角色多窗或身份 generation 变化 | 清除旧锁，重新观察；不自动选择另一窗口 |
| `observation_window_changed` / `observation_stale` | 采集前后窗口身份/矩形变化、采集超时或动作前目标变化 | 丢弃整包，允许尚未派发动作的有界重观测 |
| `locator_ambiguous` / `locator_evidence_conflict` / `locator_low_confidence` | 定位证据不足、冲突或无法唯一选择 | 不派发，保存脱敏诊断 |
| `pointer_target_occluded` / `pointer_target_mismatch` / `input_desktop_unavailable` | 点位被挡、归属变化或无输入桌面 | 不注入；已注入但回执未知时停止自动重试 |

接口评审必须检查新错误码与现有 `ActionReceipt`/`StepMaterializationError` 的映射，使 CLI、pytest 和 Agent 报告对同一失败给出同一 `code`，不把守卫拒绝折叠成笼统的 backend error。
