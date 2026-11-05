# 录制链路最小收尾修复

本轮由协调者接管，不再交回 ZCode。保留全部既有 WIP，未提交或推送。

## 已确认并修复

1. `RecorderPanelHost.start`：新子进程必须重新等待 ready，清除旧 ready/error；子进程 panelError 不再被当成功。新增启动超时/错误回执回归。
2. `agent/tunnel.py`：原 accept 循环同步 `_forward`，第一条 SSE/HTTP持久连接未关闭会阻塞后续连接接收。改为每连接独立线程转发，保留原SSH字节转发，不改MCP协议。真实socketpair双连接测试验证第一连接保持打开时第二请求仍可往返。

## 独立验证

- 隧道并发、隧道恢复及面板入口：20 passed。
- 录制回归：652 passed / 1 deselected；32.82s。
- 默认全仓：2197 passed / 614 deselected，exit 0，57.62s；单个依赖弃用警告非失败。`git diff --check`通过。

## 明确边界

黑色截图不足以证明AppKit backing store不绘制；没有本轮受控复现，不改Tk渲染架构。Tk缺AX控件语义不等于肉眼不能操作，不再盲扫坐标。

本轮未覆盖远端现有部署或重启现有隧道，避免打断用户RDP工作。运行中的隧道仍需在受控重启后加载新代码；面板修复需同步target并重启服务才在11.26生效。完整GUI录制→断言→停止收集→报告仍未取得验收证据，不能标整批完成。

2026-10-25 02:42：显式严格窗口锁修正后，原生示例两次物理点击已录制，stop actionCount=2，收集生成 ready golden。证据 edr-wd/result-report/20261025-024136-raw-probe/。完整输入、显式断言、恢复初态及真实回放仍待验证，不作整体通过结论。
