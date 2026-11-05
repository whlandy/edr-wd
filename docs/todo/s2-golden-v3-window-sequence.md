# S2 交付(修复版 9):golden v3 窗口序列与显式源事件映射

日期:2026-11-03;状态:**修复版9交付,停等独立复验**。
1127 边界反例已独立反转认可;1140 复验发现放行前最后的 model/schema 同步
缺口(单一 P2),本版收口。

## 0. 1140 反例修复对照(4 回归,先红后绿)

根因:[desktop-golden-trace.schema.json](../../test_case/schema/desktop-golden-trace.schema.json)
的共享 `$defs.window` 缺 `rootAutomationId`、`$defs.verifier` 缺 `source`
——真实公开 compile 产出的 ready root-only v3 与 user_assertion/native_transition
产物被 schema 拒绝(此前 canonical v1 全绿不能替代新产物验收)。

修复:schema 同步到**实际已实现模型契约** — window 加 rootAutomationId
(string),verifier 加 source(enum: user_assertion | native_transition)。
不删字段、不降级 v3、additionalProperties=false 保持(不放宽任意字段)。

| 回归 | 覆盖 |
|---|---|
| `test_1140_real_v3_artifacts_pass_schema_dual_validation` | 真实 compile 的 root-only + title-only 正例:model PASS + schema PASS |
| `test_1140_verifier_source_passes_schema` | user_assertion(真实断言)与 native_transition 产物过 schema |
| `test_1140_schema_rejects_unknown_and_invalid_source` | 负例:未知字段拒绝;非法 source(made_up)schema 与 model 双拒绝 |
| `test_1140_v1_v2_still_validate` | §9 兼容:1102b 原件 v1/v2 产物继续过 schema |

复现脚本对照:四正例 **model PASS + schema PASS**(此前 title PASS/schema FAIL、
user_assertion FAIL、native FAIL);title-only 对照双过保持。

## 1. 测试输出(实测)

| 套件 | 结果 |
|---|---|
| test_golden_v3_window_sequence.py | **40 passed**(36 + 4;修复前 4 红) |
| S1+S2 显式(-p no:cacheprovider) | **76 passed**(监督独立计数一致) |
| 录制目录 | **1510 passed / 1 skipped / 1 deselected / 0 failed** |
| 默认全库 | **3080 passed / 1 skipped / 614 deselected / 0 failed**(基线 3004+76,Δ 精确) |
| `git diff --check` | 通过 |
| reproduce_schema_parity.py | 四正例 model+schema 双 PASS |

## 2. 改动文件

test_golden_v3_window_sequence.py(40 测试,+4)/
desktop-golden-trace.schema.json(共享 defs 同步)。业务实现零改动。

## 3. 停等声明

**停等独立复验,通过前不开始 S3**。未 commit/push;未动远端/GUI/权限;
历史原件未改;WIP 全保护。
