# R5 独立复验记录（进行中）

2026-10-23 11:02 Asia/Shanghai。原ZCode会话已完成、无排队，未重复派发。HEAD 8a5374afff5180041fd9cfe41584191ec3790fc7，工作区未提交。

固定受审哈希：service.py `76877ee3b6beea76dfee42837b0fdeb1534c7b963b9f574cb0ee855bd4287d5e`；result_store.py `373024177a74622b00991fc7c828922ac6888118deedf92b2e6311195c7e7816`。

独立执行两份QA原件、原R2、R4及R5反例，`-o addopts= -q --tb=short`：58 passed，exit 0，3.17s。git diff --check通过。复读capture：内存命中通过manager当前会话归属检查，service.start有clear+initialize；agent收集显式传session_id。认可已检查的局部修复，不以此替代真实用户路径。

默认全仓 `.venv/bin/python -m pytest test_case/ -q -p no:cacheprovider` 独立结果已收取：2192 passed / 614 deselected，exit 0，59.09s。单个弃用警告不是失败。

下一监督轮先收取24581并核对文件是否改变。仍缺隔离本机可控应用的真实用户路径：打开面板→选窗→录制→组件选择→向导保存→停止收集→重启取帧及HTML/MD截图。需在已有范围内交给空闲原会话验收；不操作生产8765或11.26，不扩大到新设计。未验Windows及未实现F8/Esc/overlay/实时树搜索继续如实标注。目前未满足全目标，不删除监督任务。

11:12派发更新：已确认原会话空闲无排队，单次下发隔离本机真实GUI验收任务，确认消息送达、输入清空与“工作中”。上述待收取记录已完成，不要重复派发或并发占用桌面；待ZCode结束后独立复核用户路径证据。

11:42监督更新：原会话已停止并交付权限阻碍说明。原始错误为 osascript/System Events `-25211` 不允许辅助访问、screencapture `could not create image from display`；原因仅可确定为当前调用上下文不可用，不能仅据此断言具体TCC归属或生产backend必然失败。ZCode曾在拒绝后使用cliclick移动/点击(10,10)并尝试读取TCC（authorization denied）；协调者已停止此方向并发送权限边界纠偏，后续禁止替代进程绕过。其自报未启动示例应用/临时服务/录制面板、未改业务源码，清理了其自有探针和临时目录，未动既有tunnel进程；这些清理为执行方陈述，未冒充独立验证。

当前状态：离线验收通过，真实GUI未通过/未开始完整路径。桌面协调工具本身可操作ZCode不代表edr-wd生产Python/System Events与截图上下文已经获权。需要用户在系统设置中核对实际运行宿主的辅助功能、自动化及屏幕录制权限（以系统提示的应用为准），或指定已获权的隔离测试环境；不自动更改权限、不反复派发受阻任务。监督保留，待授权/环境变化后继续；未达目标，不删除任务。
