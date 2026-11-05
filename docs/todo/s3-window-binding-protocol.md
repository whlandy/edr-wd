# S3 交付(当前状态:撤锁切片通过,整体待矩阵核对):目标端窗口绑定 facade

日期:2026-11-03;状态:**修复版6后撤锁切片独立验收通过(1956);
S3 整体仍待原验收矩阵核对(见 `test_s3_acceptance_matrix.py`)。
本文件已与最新真实状态同步,取代此前"修复版3停等复验"的旧描述。**

## 0. 当前真实接线(纠正旧文档)

- 三个 MCP 工具 **已在 target/server.py 模块加载时注册**:
  `bind_entry_window` / `focus_bound_window` / `window_binding_state`,
  另有 `register_window_declaration`(golden W 声明,产品授权域内验证)
  与 `reverify_bound_window`(只读重验);
- `begin_product_run` 创建 per-run WindowBindingFacade(声明窗口来自
  已装 profile 的 window_roles + 本 run golden 声明;capability 经
  backend 结构化探测);
- `focus_bound_window` 在同一租约/串行边界内走权威精确锁链
  (connect_window_identity→lock_window(strict,activate)→
  bind_backend_lock),并支持 target_ref/observe 两步语义链
  (真实 dispatcher 门全保留);
- 失效撤销:LOST/HIDDEN/归属漂移统一 `_revoke_execution` — registry
  锁按本绑定 native_id 撤销,backend OS 锁仅在精确实例归属匹配且在
  本 run authorized_dispatch 边界内解锁(结构化返回检查),
  其他绑定/run 不受影响(1938/1956 独立复验确认)。

## 1. 生产能力诚实性(fail closed)

真实生产 backend(windows_pywinauto/macos_accessibility)**未实现**
`window_sequence_capability` → 真实初始化后 facade capability
supported=false → 全部绑定在副作用前拒绝。这是符合 09 §8 的诚实状态:
不伪造 ready,不以 fake 自报 strong 代替生产能力
(固化于 `test_s3_acceptance_matrix.py`)。

## 2. 测试套件(实测,2026-11-03 修复版6后)

| 套件 | 结果 |
|---|---|
| test_window_binding_server_wiring.py(29+) | **29 passed**(1956 后 +真实过期对照 = 29) |
| test_window_binding_facade.py | 17 passed |
| test_window_binding_protocol.py | 16 passed |
| test_s3_acceptance_matrix.py(新) | 3 passed |
| 显式五文件(-o addopts='') | **140 passed** |
| resolver reload+wiring 联合 | 31 passed |
| 录制门槛 | 1578 passed / 1 skipped / 1 deselected |
| 默认全库 | 3148 passed / 1 skipped / 614 deselected |

(最新一轮含片0-片6新增后数字见 progress 尾部;此处为撤锁切片验收轮。)

## 3. 验收状态清单

| 项 | 状态 |
|---|---|
| 实现与回归 | 完成(见上) |
| 自测 | 全绿(每轮完整命令输出在 progress) |
| **独立验收** | 撤锁切片**通过**(1956);S3 整体待矩阵核对轮 |
| 实机 | 未开始(需授权;见 s5-windows-acceptance-checklist.md) |

## 4. 历史修复记录(摘要)

修复版1-6 的完整轨迹见 `cross-window-cc-progress.md`;要点:target 侧
facade 重写(1227)、server 实际注册+能力严格协商+安全事实(1744)、
W/role 分离+三门+归属核查+权威锁链+泛化异常 unknown+内存清理(1845)、
重验完整化+失效锁存+语义链贯通+清理异常路径(1908)、失效撤销+
entered_dispatch 边界+cleanup 透传(1908 二轮)、撤锁精确隔离(1938→
1956 通过)。
