# 跨轨待办登记：test_compile_vs_replay_field_parity.py 4 条红测 = 过期夹具（2026-10-28 18:43 上海）

- 性质：**跨轨 WIP 契约不一致登记**（非生产缺陷；生产 parity 已实证正确）。本登记不修改该测试、不修改共享业务代码，交由该测试的拥有方裁决「修复夹具或退役」。
- 关联：`result-report/20261028_184039-field-parity-reconcile/`（含完整诊断证据、变异证明）。
- 分支/HEAD：`hermes-remote` / `8a5374a`。

## 状态

`test_case/test_recording/test_compile_vs_replay_field_parity.py`（未跟踪 WIP）当前 **4 failed / 2 passed**：

| 用例 | 根因 |
|---|---|
| `[123-DataItem-numeric-text]` | `ObservedTarget(text=123)` → 模型拒绝非字符串 text（生产层面数字 text 到不了编译器） |
| `[Details-7-numeric-controlType]` | `ObservedTarget(control_type=7)` → 模型拒绝 |
| `[-DataItem-empty-text]` | `synthesize_selector` 对空名返回 `None`，`_control_from_event` 对 `None.control` 解引用崩溃（测试代码 bug，不可运行的夹具） |
| `test_compiler_does_not_mark_numeric_text_ready...` | 同上（构造 `text=123` 事件先失败） |

## 关键结论

- **生产 parity 正确**：喂真实事件 + int 节点文本 → compiler=0 & locator=0（绝不对 locator 拒绝的节点承诺 ready）；str 节点→ 两端 1（匹配未被误关）。实证见 result-report §2 / `evidence/20-production-parity-verify.out`。
- **模型字符串严格化是比编译器级 parity 更强的上游守卫**，数字 text 在生产录制路径上不可达。该测试的非字符串前提已随模型硬化而过时。
- 已新增非冲突回归 `test_field_parity_numeric_contract.py`（5 条，纯增量，变异证明承重）锁定当前正确契约，见 result-report §4。

## 建议拥有方动作（其一）

1. **修复夹具**：把非字符串 parity 用例改为直接喂节点 dict（绕开 `ObservedTarget`），空名用例改为断言 `synthesize_selector is None` 且编译 `incomplete`（与新增回归重叠，可合入）；或
2. **退役**：删除「不可达」的非字符串用例，保留 canonical-string 用例，并引用新增回归覆盖数字/空名语义。

不改安全行为：生产代码本已正确，无需修改任何共享业务文件。
