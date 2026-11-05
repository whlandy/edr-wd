# R4 后续检查：显式会话的内存取帧绕过

派发更新：用户要求继续定时监督后，桌面访问已恢复；核实原 ZCode 会话 R4 已结束、无排队，于原会话单次下发本轮 R5，确认消息出现、输入清空并显示“工作中”。以下此前“尚未发送”记录为锁屏期间历史状态，勿重复派发。

2026-10-23 04:12 Asia/Shanghai。桌面工具仍返回 Mac locked / automatic unlock paused；本轮未能确认 ZCode 空闲，不派发消息、不绕过权限。R4 后上轮独立定点57项通过。

本轮复读 `RecordingService.capture`：先调用 evidence_store.get(capture_id)，仅在内存 miss 时使用 session_id 校验磁盘；内存命中直接返回 fetched。即显式请求另一会话，仍可返回没有归属证明的内存帧。

新增独立反例：`result-report/2026-10-23_04-12-supervision/test_session_capture_binding.py`。真实 service.capture + 最小内存存储适配器，显式合法session_id、无归属证明的成功帧；安全行为应拒绝或验证后返回，实际直接成功。命令 `.venv/bin/python -m pytest result-report/2026-10-23_04-12-supervision/test_session_capture_binding.py -o addopts= -q --tb=short`：1 failed，exit 1，0.05s。

待恢复桌面后单次下发最小修复：显式session绑定必须约束所有取帧路径，包括内存命中；不能以CAP随机唯一假定代替归属。补内存命中错误session/无归属/正确session及磁盘恢复集成，保持既有57项通过。另复查旧find_capture路径是否与定点read_capture_frame共用符号链接及路径边界校验；后者不能只保护新协议而遗漏兼容协议。

当前未完成验收；不修改实现、不推送、不部署。默认全仓独立复跑结果已于04:42收取：2187 passed / 614 deselected，exit 0，55.99s，单个弃用警告；全仓未包含本轮新增独立反例，不能抵消该失败。04:42桌面仍锁定；R5反例尚未发送给ZCode，待能确认原会话空闲无排队后单次派发。
