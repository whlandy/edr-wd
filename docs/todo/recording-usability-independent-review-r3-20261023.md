# ZCode R2 产出独立复验（R3）

2026-10-23；仓库 `/Users/edr-test/ai-projects/edr-wd`；HEAD `8a5374afff5180041fd9cfe41584191ec3790fc7`。本轮仅检查，不修业务代码、不修改测试期望、不下发返工、不推送或部署。

## 结论

**仍不通过整体验收。** 原生 ZCode 主会话最新回复为 R2 完成、停等第三次独立复验，界面已空闲。两个 QA 报告已产出。本轮对当前工作区独立复跑，录制正式集 625 passed / 1 deselected；两套额外 QA 共 36 passed / 10 failed。

数字不可相加理解为独立测试覆盖量，两套包含不同层次和重叠检查；625 全绿不能抵消用户契约反例。

## 实际执行

```sh
.venv/bin/python -m pytest test_case/test_recording/ -q -p no:cacheprovider
# exit 0；625 passed, 1 deselected；41.14s

.venv/bin/python -m pytest result-report/2026-10-23_zcode-parallel/qa-window/test_qa_window.py result-report/2026-10-23_zcode-parallel/qa-evidence/test_qa_e_save_receipt_and_capture_recovery.py -o addopts= -q --tb=short
# exit 1；36 passed, 10 failed；4.75s
```

两组均有一个 opentelemetry DeprecationWarning，不是失败原因。`git diff --check` 通过。未重跑全仓，不将 ZCode 自报 2169 当作本轮独立验证。

## 当前仍可复现的问题

| 问题 | 影响 | 源码位置 |
| --- | --- | --- |
| 同 PID、同标题窗口候选标签相同 | 下拉不能准确表达用户选择 | `target/recording/ui_controller.py` 的 list_windows 路由 |
| 同名双窗直接开始录制 | 仅进程名和标题正则的 scope 可纳入另一窗口 | `target/recording/ui_controller.py:229` |
| 进程身份不可解析报参数 required | 错误提示不对应用户实际问题 | 同上 |
| 新入口未复用 connect/lock/verify | 捕获开始前的所有权验证链缺失 | 同上 |
| 已运行面板再次打开不发 restore | 面板 withdraw 后入口无法恢复它 | `target/recording/ui_controller.py:358` |
| 父进程回调返回 None 被包装 ok=true | 未取得提交结果也可能给表单成功 ACK | `target/recording/indicator.py:537` |
| save_capture 接受 str 并 encode | 非 PNG 字节可被当截图写入 | `target/recording/result_store.py:129` |
| 截图写入异常被 continue 吞掉 | stop 的 resultStore 仍显示 ok=true、captures=0 | `target/recording/service.py:159` |
| 新服务实例不能取回磁盘截图 | 重启后 raw 在、截图无法经当前取证入口下载 | `target/recording/service.py:284`；`target/server.py` get_recording_capture |
| 历史 pending 收集成功不标 collected | 下次启动继续重复收集 | `agent/recording/ui_bridge.py:43` |

以上不仅是旧报告转述：当前工作区测试失败后，复读了 start_from_selection、面板 alreadyRunning、父进程 ACK、capture、持久化异常及历史收集实现。尤其 R2 自报“磁盘恢复已接通”与当前生产 `capture()` 仅返回 evidence_store.get 的实现不符；现有 load_captures 工具函数存在不等于 MCP 调用链已使用。

## 已认可的局部进展与边界

启动失败非零退出、子进程 emit-None 保持 pending、错误 requestId 拦截、布尔 unknown 不假通过、PNG 二进制写入及组件列表离线路径已有通过项。详见两份 QA 报告与本轮重跑结果。

F8/Esc 原生拾取、高亮 overlay、组件树实时搜索仍明确未实现；实时重读不能由冻结观测预演替代。真实 Tk 完整人工路径、Windows/11.26、实际截图报告本轮未验证，因此不能宣称产品已可用或全量完成。

下一步建议先处理窗口身份与 ACK/证据链 P1，再修恢复入口和可行动错误；修后按原断言复跑以上两套 QA，最后做可控应用上的真实用户路径验收。当前请求只授权检查，本轮没有向 ZCode 追加任务。

原 QA 报告：[QA-W](../../result-report/2026-10-23_zcode-parallel/qa-window/report.md)、[QA-E](../../result-report/2026-10-23_zcode-parallel/qa-evidence/report.md)。
