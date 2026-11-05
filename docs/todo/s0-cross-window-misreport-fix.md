# S0 交付:同 selector 误报修正(09 号设计 §6.3)

日期:2026-11-03;状态:**交付,停等独立复验**(Codex)。
范围:仅修正 collapsed-window 比较对象;不改归因语义、不新增 schema、不承诺样本 ready。

## 1. 交付内容

### 1.1 失败回归(先红后绿)
文件:`test_case/test_recording/test_phase0_recording.py`(追加 2 个)

| 测试 | 语义 | 修复前 | 修复后 |
|---|---|---|---|
| `test_return_to_first_window_is_not_window_drift` | A→B→A(三个动作,首尾同 handle 13239958 同根 EdrMainWindow,中 B 不同根)| **红**(ready ≠ incomplete,误报存在)| ✅ 绿:golden ready,无 `compile_window_identity_ambiguous` |
| `test_recurring_selector_on_a_different_instance_still_refuses` | A→B→A2(同 selector 在**不同** handle 101→303 再现)| (新测试,语义锁定)| ✅ 绿:incomplete + 两步均带 issue — 修复未放松真实漂移信号 |

### 1.2 实现(唯一业务改动)
`agent/recording/compiler.py`:collapsed-window 检查的比较锚点从**紧邻上一个动作**
(`previous_action_event`)改为**该 selector 首次出现时的事件**。
- `collapsed_window_steps` 值扩展为 `(first_index, first_sequence, first_event)`;
- 再次出现时 `_different_recorded_handles(event, first_event)`:
  同 handle(同实例返回)→ 不误报;不同 handle(同身份第二实例/无法证明的替换)
  → 整组标记(含首次步骤),拒绝语义不变;
- `_different_recorded_handles` 行为零改动(任一方无 handle → 不判不同,保持既有
  "handle 只是录制内证据"纪律)。

## 2. supervised_1102b 复编译(设计 §2.1 预期逐条核对)

```
status: incomplete
all issues: ['compile_weak_identity_unresolved']
step-0001 issues: ()          ← 原 compile_window_identity_ambiguous 误报消除
step-0008 issues: ()          ← 同上(raw seq 1/10 = compiled 1/8,同主窗口 handle)
step-0009 issues: ('compile_weak_identity_unresolved',)
```

- raw 1/10(即 compiled step-0001/step-0008)同主窗口 handle 13239958 → 误报按
  预期消除;
- step-0009(title-only EDRClient,快照 CS-0003/CS-0005 无 root、无 complete)
  **如实保持 incomplete** — 未造 root/complete 凑 ready(设计 §2.1 明确要求);
- 全库既有行为零回归(见 §3)。

## 3. 测试输出(实测)

| 套件 | S0 前(临时 stash 实测) | S0 后 | Δ |
|---|---|---|---|
| 录制目录 `test_case/test_recording/ -q` | — | **1434 passed / 1 skipped / 1 deselected / 0 failed** | +2 = 恰好两个新回归 |
| 默认全库 `pytest -q` | **3002 passed / 1 skipped / 614 deselected** | **3004 passed / 1 skipped / 614 deselected / 0 failed** | **Δ passed = +2(恰好新回归,0 意外)** |

基线测量方法:`git stash push`(仅 S0 两文件)→ 跑全库(3002)→ `git stash pop`
恢复 → 重跑(3004)。stash往返完整,工作区当前含 S0 全部改动。
phase0 文件 collect-only:83 tests(原 81 + 新 2)。

## 4. 边界与未做(如实)

- 未改 `openedBy`/归因语义(设计 §2:"正向最近动作关联不是可靠直接因果" —
  S0 范围仅比较对象);
- 未新增 windowSequence/v3/schema(S1/S2 切片);
- 未承诺 supervised_1102b ready(设计 §11.2:历史 fixture 仅用于诊断定位);
- 未动其他 WIP 文件;未 commit/push。

## 5. 停等声明

按用户指令:**S0 交付后停等独立复验(Codex),复验通过前不开始 S1**。
复验请核:两个回归的语义与断言、compiler 改动的比较锚点、supervised_1102b
三步 issues 与 §2.1 预期一致性、Δ 记账。
