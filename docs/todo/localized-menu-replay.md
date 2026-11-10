# 无 automationId 的多语言菜单回放

2026-11-06：按用户要求新增本地实现，不再以“避开语言菜单”替代功能。

## 已实现

- `selector.control.structural` 显式契约 `menu-layout/v1`，限定无稳定ID的 `MenuItem` 点击。编译时需要事件绑定的完整菜单树、非空父层级、2–64个不重叠同级菜单项，以及命中项的真实矩形。
- 保存整个同级菜单布局的归一化几何和槽位，不保存旧屏幕点击位置。菜单整体平移、缩放、统一宽度随翻译变化仍可匹配；整个布局的归一化误差上限2%。
- 回放在本次绑定窗口的新观测中重新验证父层级、同级项数和完整布局，解析真实当前target。文本只作录制描述，不再要求与录制语言相等；强ID仍优先，普通弱控件策略不变。
- 实际点击使用本次控件的唯一当前文本/类型，必要时使用唯一当前原生control_id，并关闭parent_fallback；无可安全注入身份时拒绝，不盲点旧坐标。
- 已接普通GoldenStepMaterializer与受控v3真实server/binding/dispatcher链，不另建执行循环。selector模型及golden JSON schema保留新契约。

## 验证

新增22个自包含用例，包含English/中文/Spanish/未录过的French、移动/缩放/宽度变化、错误进程/窗口/父层级、缺rect/重叠/结构改变、非菜单/强ID不降级、普通回放真实传当前Idioma、v3链当前Idioma恰一次输入/结构改变零输入。

- 定向定位及弱身份回归：69 passed。
- 固定最终代码录制库：1815 passed / 1 skipped / 1 deselected。
- 固定最终代码全库：3385 passed / 1 skipped / 614 deselected。
- 显式需求+新菜单/HybridLocator：331 passed；原四legacy：26 passed。
- 完整输出与实现说明：`result-report/localized-menu/20261106-0315/`。

## 实机范围及已知差额

本轮没有改录制原件、追加假assertion、清除丢事件/覆盖诊断、修改Windows环境或开放公共v3门。既有采集与布局拖拽WIP原样保留。

对提供的旧语言切换原件只读编译：前三次菜单点击已获得结构selector，不再因翻译标签报volatile_label；末次菜单缺对应完整树，仍不能据空证据生成结构selector。原件还有窗口覆盖、同名弹出菜单实例归属等问题，整体仍incomplete，不称整份实机语言切换流程已通过。

后续需要在明确授权的11.26交互桌面验证包含语言切换的完整流程；不得再用不碰语言菜单的三步流程冒充该功能验收。必要的弹出菜单owner/实例归属和采集完整性问题应按真实证据修复，不删除质量门或复用同名兄弟绑定。部署时必须包含新增 `agent/locator/menu_layout.py` 及同步模型/编译器/回放器，不只拷贝单个locator文件。
