# S1 交付(修复版 7):raw v2 录制实例与 windowTimeline(09 号设计 §4)

日期:2026-11-03;状态:**第七次交付,停等独立复验**。
0647 复验认可 0627 input owner 修复;本版仅收口 **P2 ingress 的四处显式
raw 序号覆盖**(同一原范围不变量,复验 repro_ingress.py 可直接验证)。

## 0. 0647 反例修复对照(先红后绿,2 个真实 source worker 回归)

反例:真 QueuedCaptureSource worker 驱动 show/hide/destroy 三包、0 raw 输出 →
timeline ingress=[1,2,1](destroy 被 raw 序号覆盖回 1)。根因:`auto` 默认被
四处**显式实参**覆盖(1733 destroy / 1779 identity_changed / 1933 input
identity_changed / 1982 input 首见,传 sequence / event.sequence)。
修复:四处全部改 `auto`(= correlator 独立接收计数);grep 确认零残留;
`_timeline_append`/`_declare_instance_observation` 默认 auto 链路打通
(上轮 _declare 的 auto 默认未透传给内层 append — 本轮补齐)。

| 回归 | 覆盖(复验指定) | 断言 |
|---|---|---|
| `test_0647_worker_ingress_is_receive_order_no_raw` | 真 source.start worker;show/hide/destroy 三包 **0 raw 输出** | ingress=[1,2,3] 递增互异;raw 序号契约保持(restored.events==()) |
| `test_0647_worker_input_first_observation_and_owner_change_ingress` | **无 raw 产物后 input 首见**(weak resolver)+ **owner 换代第二输入** | 同包多事实同 ingress;相邻不同包递增(first_obs < identity_changed);manifest↔timeline 引用一致 |

同包多事实同 ingress 的正确断言(复验警告的"全局唯一"错误断言已避开)。
真实 worker 时序:包间用 dequeued 计数同步(harness 竞态非生产行为)。

## 1. 交付口径更正(0647 要求)

上轮报告称"全部 auto/event.sequence 不再传入"与代码不符(四处显式覆盖仍在)。
本版按**真实 diff** 陈述:windows.py 四处 `ingress_sequence=sequence/
event.sequence` → `auto`;无其他 ingress 相关改动。复现脚本:
show/hide/destroy → `[('created',1),('hidden',2),('destroyed',3)]`。

## 2. 测试输出(实测)

| 套件 | 结果 |
|---|---|
| test_raw_v2_window_timeline.py | **36 passed**(34 + 2;修复前 2 红) |
| 录制目录 | **1470 passed / 1 skipped / 1 deselected / 0 failed** |
| 默认全库 | **3040 passed / 1 skipped / 614 deselected / 0 failed**(基线 3004+36,Δ 精确) |
| `git diff --check` | 通过 |
| reproduce_ingress.py | **[1,2,1] → [1,2,3]** 反转 |

## 3. 停等声明

**停等独立复验,通过前不开始 S2**。未 commit/push;未动远端/GUI/权限;
历史原件未改;WIP 全保护。
