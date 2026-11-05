# R3 产出独立复验：R4 待修

2026-10-23 02:36 Asia/Shanghai。HEAD 8a5374afff5180041fd9cfe41584191ec3790fc7，未提交工作区。ZCode 原会话已结束、无排队。

独立运行两份 QA 原件及原 R2 反例：56 passed，exit 0，2.94s。认可本轮原反例修复。默认全仓 `.venv/bin/python -m pytest test_case/ -q -p no:cacheprovider`：2173 passed / 614 deselected，exit 0，59.44s。单个弃用警告非失败。结果对应R3快照，不代表后续R4修改已验。

新增反例 `result-report/2026-10-23_02-36-supervision/test_capture_ambiguity.py`：1 failed，exit 1。通过真实 RecordingResultStore.save_capture 在两个合法 session 保存同一 capture_id，find_capture 不拒绝歧义，返回排序第一份。当前函数逐目录扫描命中即返回，并未实现报告所称“无跨会话同名遍历”；save_capture 也不强制全局唯一。该反例不操作生产服务或 GUI。

要求最小修复：生产 MCP/agent 收集协议增加明确 session 归属并验证 raw 引用及摘要；旧无 session 调用必须在验证唯一且合法引用后兼容，重复归属明确拒绝，不能取第一份。验证有效 session 目录、禁止符号链接/路径逃逸、不能将未经元数据验证的文件当可信脱敏截图。补实际服务与收集链回归，保留现有56项与本反例断言。

本轮只写复验报告与反例，不改业务代码。GUI完整用户路径与Windows仍未验；F8/Esc/overlay/实时搜索仍未实现，不能宣称整体验收完成。

派发记录：已向空闲且无排队的原ZCode会话单次下发R4，确认用户消息出现、输入清空并显示“工作中”；不重复派发。本轮自动监督继续。
