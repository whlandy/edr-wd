# edr-doc-loop 最终报告

生成日期:2026-08-07

本报告总结了 edr-doc-loop 本轮任务(共 13 项)的打磨结果,全部基于
`docs/todo/scroll-and-paged-table-actions.md` 的最终设计文档内容。loop 状态
`finished`,`done` 列表包含全部 13 项(P1..P6, PR1..PR3, T1..T4),无遗漏。

---

## Completed Items

本次 loop 将「滚动 / 拖拽 / 分页表格操作」从一段实践笔记打磨成一份可落地的
设计文档,核心是把 scroll/drag 从「裸指针原语」升级为「带验证意图的复合层」。
13 项逐一下结论:

### 可执行性约束 (P1..P6)

- **P1 — Verify 不是可选项 (ScrollResult 契约)**:引入 `ScrollResult(dispatched,
  moved, reason)` 冻结 dataclass,把「原语已派发」与「内容真正移动」分离;
  `success == dispatched and moved`,`moved=True 要求 dispatched=True`,
  `NO_SCROLL_EFFECT 要求 moved=False`,`NOT_DISPATCHED 要求 dispatched=False`。
  `Reason` 枚举(含 NEXT_PAGE / WHEEL_MOVED / SCROLLBAR_DRAGGED /
  FOCUS_THEN_SCROLL / NO_SCROLL_EFFECT / NOT_DISPATCHED),**不设 `SUCCESS`
  成员**(成功是派生值,从不存储)。低层 MCP 工具保持向后兼容,只报告派发。

- **P2 — 有界重试 (ScrollPolicy)**:重试/换策略循环必须按 UI 有界,耗尽时以
  `reason=NO_SCROLL_EFFECT` 终止。引入 `ScrollPolicy(max_attempts=3,
  strategy_order=(WHEEL, SCROLLBAR_DRAG, FOCUS_THEN_SCROLL, PAGINATION))`;
  定义「一次尝试 = 一次策略派发 + 一次 observer verify」。两个终止类必须区分:
  exhausted-but-armed → `NO_SCROLL_EFFECT`;nothing-dispatchable → `NOT_DISPATCHED`。
  `MAX_SCROLL_ATTEMPTS` 固定为 3。

- **P3 — 结构化内容泛化 (ContentNavigator / Detector-Controller-Verifier)**:
  分类器不得绑定单一页面名或控件(`nextPageButton` / `pagedTable.tableView`
  / `操作日志` 只是 HiSec 的一个实例)。用 `ContentNavigator`(比 TableNavigator
  更泛)作 facade,拆成 `PageDetector` / `PageController` / `PageVerifier` 三个
  纯角色,`PageStructure` 枚举(PAGINATED / INFINITE / TREE_LAZY / LOAD_MORE /
  FLAT)经 `structure_to_strategy` 桥接到 P2 的 Strategy。

- **P4 — 决策顺序作为状态机**:整个生命周期
  `DISCOVER → CLASSIFY → OWNERSHIP → EXECUTE → VERIFY`(FALLBACK 回到 EXECUTE)
  建模为单一确定性状态机,三个终止态 `SUCCESS | NO_SCROLL_EFFECT |
  NOT_DISPATCHED`。`transition(s, ctx)` 纯函数只做决策,副作用限定在 EXECUTE/
  VERIFY handler,使每个 fallback 路径可因转移可测。EXECUTE 不是新循环,而是
  P2 的 `scroll_with_policy` 有界体;FALLBACK 是唯一推进策略的下一条边。

- **P5 — 回归不变量**:复合层是原语的超集,绝非行为变更。四个回归不变量:
  R0 无分页时派发与原 `pointer.scroll` 逐字节一致;R1 可拖控件(滑块/缩放柄/
  滚动条拇指)必须解析为 drag,永不归为分页;R2 RDP/window-lock 回退路径与现
  有「Focus Then Scroll」配方一致;R3 `window_lock` 前置条件永不减弱。

- **P6 — Verify 归属:Observer/diff-engine 层**:`verify()` 由独立 Observer
  实现,基于现有 `observation` 机制(复用 `ObservationSnapshot.tree_digest` /
  `Target.fingerprint`,非新快照格式)。`DiffStrategy`(TREE_DIGEST /
  FINGERPRINT / SCREENSHOT)按「如何比较」而非「新 schema」选。`moved` 只能
  由 Observer 产出,任何复合工具不得用坐标差或裸 `ok` 计算 `moved`。

### 实现排序 (PR1..PR3)

- **PR1 — ScrollResult + Observer + scroll_and_verify**:结果契约、Observer
  基线(仅 TREE_DIGEST)、单策略 `scroll_and_verify` + 测试。显式不含
  ScrollPolicy / 状态机 / PageDetector。先落地契约,PR2/PR3 建立在稳定语义上。
  未决:success `reason` 归属用 `strategy_used` 字段还是机制命名 member,必须
  在 PR2 前定。

- **PR2 — 状态机 + 有界策略**:`ScrollPolicy` + `transition` 循环 +
  `scroll_with_policy` 单次尝试执行器 + 终止。CLASSIFY/OWNERSHIP 用硬编码
  `FLAT → WHEEL` 桩;无分页逻辑。`EXECUTE` 恰好一次派发+一次 verify,`moved=True`
  仅当 receipt armed 且 verify True。总派发上界 `len(strategy_order) * max_attempts`。

- **PR3 — PageDetector/PageController/PageVerifier 抽象**:三个纯角色 +
  `ContentNavigator.step(direction, snapshot, policy)`,泛化分页检测(用户/告警/
  资产/操作列表)。替换 PR2 的 `FLAT → WHEEL` 硬编码为真实
  `structure_to_strategy`。`step` 读取缓存的 DetectionResult,不重跑 detect。

### 工具扩展 (T1..T4)

- **T1 — `scroll_region`**:通用复合层,滚动任意区域(分页或普通),挑选最丰富
  策略并验证移动。对 `FLAT` 区域与原 A029 逐字节一致(R0),仅在检测到结构分页
  时委托 ContentNavigator。参数 `title_re / process_name / target_ref /
  direction / amount / strategy / verify / max_attempts`。

- **T2 — `page_table`**:分页专用复合层,**限定 `Strategy.PAGINATION`**,
  绝不发 A029/A028,只做语义 next/prev/load-more 点击并证明页面确实变化。页门:
  `FLAT / INFINITE / TREE_LAZY` → `NOT_DISPATCHED`(失败快速,不静默滚动)。「翻
  遍整表」是调用方循环,不是一次调用。

- **T3 — `scroll_until_visible`**:目标驱动循环,持续推进视图直到观察目标或预算
  耗尽。每次迭代 = 一个有界策略步 + 一次强制目标探测;返回 `ScrollResult` 而非
  bool。预算语义:NOT_DISPATCHED/NO_SCROLL_EFFECT 立即终止,只有 moved-but-
  unmatched 才计预算。start 已可见 → 零派发,`NOT_DISPATCHED`。`down→next` /
  `up→prev` 方向翻译必须显式。

- **T4 — `drag_target`**:基于手柄的复合层,针对被拖动的控件(滑块、列表排序行、
  分隔条、缩放柄、虚拟列表微调)。唯一直接派发 `pointer.drag` (A028) 的工具,
  门控与 A028 一致(`window_lock`)。不静默回退到 wheel/点击。`grab` 策略(center
  vs handle)、端点校验、`expected_process_name` 后端签名扩展等均标为未决风险。

---

## Changed Files

本次任务实际改动的文件清单:

- `SKILL.md`(操作文档:新增 `MAX_SCROLL_ATTEMPTS` 代理端默认上限等)
- `docs/README.md`
- `references/element-click.md`
- `references/mcp-tools.md`
- `docs/todo/scroll-and-paged-table-actions.md`(本设计文档,约 1834 行)
- `docs/todo/edr-doc-loop-final-report.md`(本报告,新增)

> 隐私说明:未 add 任何 `targets.local.json` 或敏感/临时/缓存文件;仅提交上述
> markdown 工作产物。

---

## Unresolved Design Decisions

仍待拍板的开放问题(摘自设计文档):

1. **`moved` 语义**:严格(摘要任何变化即 moved)还是容差式(per-surface epsilon,
   如半行可见)?推荐显式 `tolerance` 参数,默认「任意变化」,首个 HiSec 回归后再议。

2. **success `reason` 归属(阻塞 PR2)**:用独立的 `strategy_used` 字段,还是机制
   命名的 `Reason` 成员?PR1 必须定,PR2 状态机会 match 它。`LOAD_MORE` vs 编号
   `PAGINATED` 是否需两个成员也未决。

3. **`verify=False` 的 reason 歧义(item-1 不变量冲突)**:`dispatched=True,
   moved=False` 被不变量钉在 `NO_SCROLL_EFFECT`,但「跳过验证」与「已验证无效果」
   被混淆。需接受 `NO_SCROLL_EFFECT` 并文档化,或新增 `Reason.UNVERIFIED` 并更新
   不变量表。

4. **present-at-start 是否需要独立信号**:是否新增 `Reason.TARGET_VISIBLE`
   (须配测试),否则调用方把 `NOT_DISPATCHED` 当「目标已存在或无事可做」。

5. **page_table 是否先 HiSec-specific 还是跨后端泛化**:文档倾向泛化(结构检测),
   但首个用例是 `日志中心 -> 操作日志`。

6. **Observer 的 churn vs content 判定未定**:`tree_digest` 变化可能来自 transient
   重排(spinner/reflow)而非内容移动;需选 (a) 过滤身份子集、(b) `tolerance`、
   (c) 交给 PR3 PageVerifier。这是全设计最关键的防误报旋钮。

7. **虚拟列表 UNCERTAIN 消歧**:row 就地回收导致 `tree_digest` 不变,需定用哪个
   可观察量(scroll-position / first-visible-row)判定,并确认 INFINITE 继续步进但
   绝不标 `moved=True`。

8. **DetectionResult 缓存 vs 每步重测**:per-`step()` detect 是 O(可见目标),可能
   慢;缓存须在 verified page change 时失效(lazy-load sibling 亦然)。

9. **`scroll_with_policy` / 相关最终签名**:PR1 `scroll_and_verify` 是否内部解析
   区域、`verify=False` 交互;PR2 回调签名;`NavigateContext` 字段(含保留的第五个
   DetectionResult 槽)。

10. **`drag_target` 的后端签名**:现有 `AutomationBackend.drag` 无
    `expected_process_name` 参数(click 族才有),需决定扩展后端签名还是删除参数。

11. **R1 最尖锐边缘**:可拖手柄与滚动条拇指视觉接近,`PageDetector` 结构启发式
    必须被证明停在 `FLAT`,否则成为静默误导航源。需显式定义 「handle 优先于
    pagination」的优先级。

12. **水平 `direction`**:核心契约 `ContentNavigator.step` 只收
    `["next","prev","first"]`,无 left/right 映射;需定义怎么映射到 `Strategy.WHEEL`,
    否则以 `NO_SCROLL_EFFECT/NOT_DISPATCHED` 拒绝而非猜。

13. **滚动条拖拽坐标来源**:`detect_scroll_region` 平台相关(UIA on Windows / AX on
    macOS),最不成熟;无可靠 handle 坐标须回退 wheel 而非报错。

14. **`strategy` 强制约束 vs 自动检测冲突**:强制 Strategy 与 PageDetector 结果矛盾
    时谁赢?推荐为约束而非覆盖,矛盾时返回新 `Reason`(如 `COMPOSITION_MISMATCH`)。

---

## Implementation Backlog (PR Checklist)

按设计文档「Implementation Sequencing」,每个 PR 一份可勾选清单,供后续代码实现
直接使用。

### PR1 — ScrollResult + Observer + scroll_and_verify

目标:先落地「派发 vs 移动」契约与 verify 归属,供 PR2/PR3 建立。**不含**
ScrollPolicy、策略链/状态机、PageDetector、分页逻辑。

- [ ] 新增 `ScrollResult` frozen dataclass(`dispatched: bool`, `moved: bool`,
      `reason: Reason`),复用 `dataclass(frozen=True)` 风格(参照 `ActionReceipt` /
      `AssignmentResult`)。
- [ ] 实现 `Reason` 类型化枚举,**无 `SUCCESS` 成员**(派生,从不存储)。
- [ ] 实现 `__post_init__` 不变量:`NO_SCROLL_EFFECT ⇒ moved is False`;
      `moved ⇒ dispatched`;`NOT_DISPATCHED ⇒ dispatched is False`;
      `success == dispatched and moved`。
- [ ] `Observer` 基线:薄适配现有 `build_snapshot()`
      (`target/observations/snapshot.py`),`snapshot()` 返回现有 `ObservationSnapshot`
      (`tree_digest`, `targets`)。
- [ ] `Observer.content_changed(before, after, *, strategy=DiffStrategy.TREE_DIGEST,
      tolerance=None) -> bool`,PR1 仅 `TREE_DIGEST`:
      `before.tree_digest != after.tree_digest`。钉住签名供 PR3 的 churn/tolerance 用。
- [ ] 可选(可桩/可延后):`FINGERPRINT` / `SCREENSHOT` / `Diff` / `DigestRef` /
      `churn` 分支,勿发死分支。
- [ ] `scroll_and_verify(...)` 复合层:snapshot before → 恰好一次 primitive 派发 →
      snapshot after → 计算 moved → 返回 `ScrollResult`。
- [ ] `dispatched` 由 `dispatch.py` 返回的 `ActionReceipt.ok` 派生;PR1 **不得**
      发明多原语回退(那是 PR2 的事)。
- [ ] reason 映射:`receipt.ok False ⇒ NOT_DISPATCHED`;`ok True 且 moved False ⇒
      NO_SCROLL_EFFECT`;`moved True ⇒` 所用原语的标记(见 Blocking 未决项)。
- [ ] 单测(白盒):每个 item-1 不变量一个构造测试(非法组合抛 ValueError)。
- [ ] 单测 Observer 确定性:无 UI 变化两次 snapshot digest 相等;
      `content_changed` 相等为 False、不等为 True。
- [ ] 单测 `scroll_and_verify` 对 mock dispatcher:ok=False / ok=True+无变化 /
      ok=True+有变化 三路径。
- [ ] **Blocking(PR2 前定)**:success `reason` 归属 —— `strategy_used` 字段 vs
      机制命名 `Reason` member;定后 PR2 状态机 match 它。
- [ ] **Blocking**:`verify=False` 的 reason 歧义(接受 `NO_SCROLL_EFFECT` + 文档化,
      或加 `Reason.UNVERIFIED` 并更新不变量表)。
- [ ] **Blocking**:present-at-start 是否加 `Reason.TARGET_VISIBLE`(须配测试)。
- [ ] 文档化 PR1 限制:仅 TREE_DIGEST,无 churn 过滤,无 per-target locality。
- [ ] 固定 `scroll_and_verify` 调用形状(预解析区域 or 内部解析、`verify=False`
      强制 moved=False),供 PR2 循环与 T1..T4 共享。

### PR2 — 状态机 + 有界策略 (ScrollPolicy + transition)

目标:在有界策略执行器上搭状态机循环,建立 `NO_SCROLL_EFFECT` /
`NOT_DISPATCHED` 终止。**不含** PageDetector(CLASSIFY/OWNERSHIP 用
`FLAT → WHEEL` 桩)。

- [ ] `Strategy` 枚举:`WHEEL`(A029)、`SCROLLBAR_DRAG`(A028)、
      `FOCUS_THEN_SCROLL`、`PAGINATION`(语义点击 A020)。
- [ ] `ScrollPolicy` frozen dataclass:`max_attempts: int = 3`(镜像
      `MAX_SCROLL_ATTEMPTS`)、`strategy_order: tuple[Strategy, ...]`。无模块级整数,
      边界随调用方走。默认 max_attempts=3 < 4 成员 order 是有意为之。
- [ ] `scroll_with_policy(policy, verify, dispatch) -> ScrollResult` —— **单次尝试**
      执行器,非循环:一次 = 一次策略派发 + 一次 verify。签名顺序钉为
      `(policy, verify, dispatch)`(调和 item2/item4 冲突;调用点用 keyword args)。
- [ ] `ScrollResult` 分支:`moved=True` 仅当 receipt armed 且 verify True;
      armed-but-unmoved → `NO_SCROLL_EFFECT`;never-armed → `NOT_DISPATCHED`。
      `NO_SCROLL_EFFECT` 不是 `scroll_with_policy` 的终止返回,终止归状态机。
- [ ] `Step` 枚举:`DISCOVER / CLASSIFY / OWNERSHIP / EXECUTE / VERIFY /
      FALLBACK / SUCCESS / NO_SCROLL_EFFECT / NOT_DISPATCHED`。
- [ ] 纯 `transition(s: Step, ctx: NavigateContext) -> Step` 状态机 —— **PR2 唯一
      的循环**:单 `FALLBACK → EXECUTE` 边驱动策略推进;EXECUTE 内部不二重循环。
      `VERIFY` 对任何 armed 的 EXECUTE 都强制(不可跳过)。
- [ ] 终止态恰好 `SUCCESS | NO_SCROLL_EFFECT | NOT_DISPATCHED`;总派发上界
      `len(strategy_order) * max_attempts`。
- [ ] `NavigateContext`:携带运行中 `ScrollResult` 累加器、剩余 strategy_order、
      ScrollPolicy、snapshot pair;**保留第五个 `DetectionResult` 槽**供 PR3 用。
- [ ] CLASSIFY/OWNERSHIP 硬编码 `FLAT → WHEEL`;PAGINATION / FOCUS_THEN_SCROLL
      可桩为「解析到 nothing」以测 fallback/终止路径。
- [ ] 单测 `ScrollPolicy` 构造:默认 max_attempts==3;max_attempts==0 → 零派发
      NOT_DISPATCHED;max_attempts >= len(order) 走满全链。
- [ ] 单测 `scroll_with_policy` 对 mock:armed+change / armed+no change / never-armed;
      ok=False 永不贡献 moved=True。
- [ ] 转移表测试:枚举每个 `(state, ctx-class)` 边断言后继(确定性 + 全 fallback
      覆盖,无真实 UI)。
- [ ] 单 owner 不变量:每个 EXECUTE 至多一个策略派发;每个 FALLBACK 恰好推进一个
      策略位。
- [ ] 终止测试:每次运行在 `len(strategy_order)*max_attempts` 内到达三个终止态之一。
- [ ] **Blocking**:`scroll_with_policy` 调用形状(verify 回调签名、per-iteration
      max_attempts 与 per-surface strategy_order 覆盖交互、success reason 来源)。
- [ ] **Blocking**:NOT_DISPATCHED vs NO_SCROLL_EFFECT 纪律(防「从未 armed 却报
      NO_SCROLL_EFFECT」)。
- [ ] 决定 strategy_order 保持静态默认,reorder 延到 PR3 的 OWNERSHIP 映射。

### PR3 — PageDetector / PageController / PageVerifier 抽象

目标:结构化内容泛化 + 真实 `structure_to_strategy`。**不含**新原语、策略链/状态机
重写、新 snapshot/fingerprint 代码。复用 A020/A028/A029 与 PR1 Observer。

- [ ] `PageStructure` 枚举:`PAGINATED / INFINITE / TREE_LAZY / LOAD_MORE / FLAT`。
- [ ] `PageDetector.detect(snapshot) -> DetectionResult`(唯一真实入口;
      has_pagination 等是薄 helper,非独立 heuristics)。
- [ ] `DetectionResult`:structure、next_controls / prev_controls
      (`DetectedControl(target, enabled)`)、confidence(破 PAGINATED vs LOAD_MORE 平局)。
- [ ] `structure_to_strategy(s) -> Strategy` 桥接 P2(PAGINATED→PAGINATION,
      INFINITE→WHEEL, etc)。
- [ ] `PageController`:纯规划器,`next/advance/reset_to_first(result) ->
      list[ActionReceipt]`,target 单一 enabled owner 控件;`[]` ⇒ NOT_DISPATCHED;
      永不自行调 resolver。不重循环 A029。
- [ ] `PageChange` 三态枚举:`CHANGED / UNCHANGED / UNCERTAIN`(非 bool)。
- [ ] `PageVerifier.page_changed(before, after, threshold) -> PageChange`,委托 PR1
      Observer,不重复实现 diff 策略;虚拟化就地回收行 → UNCERTAIN。
- [ ] `ContentNavigator.step(direction, snapshot, policy) -> ScrollResult`:facade,
      读取缓存的 DetectionResult(CLASSIFY/OWNERSHIP 在状态机里跑,step **不**重跑
      detect);组合 Controller plan → dispatch → Verifier。direction 映射
      `next→next, prev→advance, first→reset_to_first`。
- [ ] step 尊重 item-1(armed 必验证)与 item-2(max_attempts 由调用方状态机应用)。
- [ ] 替换 PR2 `FLAT → WHEEL` 桩为真实 `structure_to_strategy`。
- [ ] 单测 引用透明性:`detect(同 snapshot)` 返回等值 DetectionResult;Controller/
      Verifier 无 UI 状态,可 stubbed observer + 捕获快照测。
- [ ] 单测 `PageController.next` on PAGINATED fixture 恰一个 receipt;owner
      enabled=False → `[]`(→NOT_DISPATCHED);LOAD_MORE→A020;INFINITE→A029;
      FLAT→WHEEL/SCROLLBAR_DRAG;永不 PAGINATED/TREE_LAZY。
- [ ] 泛化 fixture:用户/告警/资产/操作各一个 PAGINATED / INFINITE / LOAD_MORE /
      FLAT,分类一致(无 table-id 键控分类器)。
- [ ] 单测 `ContentNavigator.step` 组合恰好一次且不重跑 detect(动 spy)。
- [ ] 方向映射 spy:step("next")→next,("prev")→advance,("first")→reset_to_first。
- [ ] stubbed Observer 无变化 ⇒ moved=False;变化 ⇒ moved=True;UNCERTAIN 绝不
      moved=True。
- [ ] **Blocking**:虚拟列表 UNCERTAIN 消歧可观察量 + 确认 INFINITE 继续步进但绝不
      moved=True。
- [ ] **Blocking**:strategy_order reorder 通过 NavigateContext 第五槽暴露,不在
      transition 内重定。
- [ ] 决策 DetectionResult 缓存 vs 每步重测(缓存须在 verified page change 失效)。
- [ ] 钉死 LOAD_MORE vs PAGINATED 重叠的 precedence(confidence tie-break,PAGINATED
      赢),与 R1 防误分类一致。

---

*Report generated automatically by the 2026-08-07 08:00 cron job.*
