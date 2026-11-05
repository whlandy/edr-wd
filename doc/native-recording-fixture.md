# 原生录制回放对照示例

`scripts/recording_native_fixture.ps1` 使用 Windows 自带 WinForms 控件，不依赖 Tk，不修改 EDR 配置或业务数据。所有状态仅在本进程内；关闭窗口即退出，不写磁盘或注册表、不联网。

在已授权的 Windows 交互桌面启动：

```powershell
powershell -NoProfile -STA -File .\scripts\recording_native_fixture.ps1
```

输出唯一窗口标题与 PID。每个业务控件具有固定 Name/AccessibleName：FixtureInput、FixtureCheck、FixtureSave、FixtureReset、FixtureStatus。预期分别为输入、勾选、保存、重置、结果文本；实际 UIA role/automation_id 必须先用生产 dump_tree 核实，不能假定此文件已证明暴露成功。

验收顺序：精确绑定输出标题/PID→读取语义→录制修改输入/勾选/保存→断言 saved:<输入>:true→停止收集并编译→通过 Reset sample 恢复 baseline/false/ready→真实回放→独立读结果和截图。重置应在录制停止后执行，不混入被测轨迹。不得将生命周期测试或内部函数调用当成真实输入回放。

当前仅本地交付设施，未上传/运行于11.26，不代表Windows实机验收通过。部署target不是使用此示例的必要条件；新建示例窗口仍需确认桌面无其他控制者和无活动录制。出现角色/文本缺失时保存树和截图，对照Tk示例后再定位后端问题。
