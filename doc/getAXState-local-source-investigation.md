# getAXState 本机实现取证

日期：2026-10-23。只读检查本机安装包；未绕过应用权限，未修改安装包或连接内部 IPC。本文不是完整原生源码恢复，也不把 ZCode 的实现归因于 OpenAI。

## 1. 找到了什么

找到了 ChatGPT 安装包内 `@oai/cua` 的实际 JavaScript 实现（压缩后的发布代码），不只是类型声明。也找到了 ZCode 独立实现的明文 JavaScript。此前“内部封装实现不可见”的描述过于笼统：JS 层可见，原生服务实现尚未取得源码。

本机 OpenAI 包的根路径：

`/Applications/ChatGPT.app/Contents/Resources/cua_node/lib/node_modules/@oai/cua/`

下列文件均已实际读取：

| 相对根路径 | 实际职责 |
| --- | --- |
| `dist/lib/js/oai_js_cua/src/tinysky_alt/bind_mac_app.js` | macOS `getAXState`、click、setValue 等方法包装 |
| `dist/project/cua/sky_js/src/targets/mac/get_app_state.js` | 经过 Computer Use policy 调用客户端并转换结果 |
| `dist/project/cua/sky_js/src/targets/mac/client.js` | `MacComputerUseClient`，请求类型、动作参数转换 |
| `dist/project/cua/sky_js/src/targets/mac/native-pipe.js` | 原生 pipe 连接、协议版本、JSON-RPC、消息分帧、期限和错误处理 |
| `dist/project/cua/sky_js/src/targets/mac/window_result.js` | 从原生 `skyshot` 取 text、截图 URL，按应用加入说明 |

原生服务文件实际存在于：

`/Applications/ChatGPT.app/Contents/Resources/cua_node/lib/node_modules/@oai/sky/Codex Computer Use.app/Contents/MacOS/SkyComputerUseService`

这是发布二进制，不是 Swift/Objective-C 原始源码；不能把下面的客户端分析冒充服务端的树遍历源码。

## 2. 已确认的 macOS 调用链

```text
app.getAXState(options)
  bind_mac_app.js
    → get_app_state({ app, disableDiff? })
  targets/mac/get_app_state.js
    → withComputerUsePolicy(...)
    → getClient().getAppState({ app, disableDiff })
  targets/mac/client.js
    → requestType = ComputerUseIPCAppGetSkyshotRequest
    → apiVersion = CodexComputerUseIPC-5（本安装版本默认值）
  targets/mac/native-pipe.js
    → 原生连接：JSON-RPC + 长度分帧 + deadline
  SkyComputerUseService
    → 返回 skyshot（内部如何构建尚未取得源码）
  window_result.js
    → skyshot.text / skyshot.screenshot.url
  bind_mac_app.js
    → 显示 text 并返回字符串
```

关键发现：JS 的 `getAXState` 本身并不遍历 AX 树，也不执行 OCR。它拿到服务端已经生成的文字结果。是否抓取图像、如何过滤树、如何编号与做 diff，需要继续研究原生服务，不能从函数名直接推出。

`disableDiffing` 在包装层转换为 `disableDiff` 后传给客户端；因此确有全量/差量控制参数，但原生 diff 算法尚未获得。

`window_result` 会读取 `skyshot.text`；部分情况下还会前置应用说明。因此最终返回字符串不必然是纯树文本，edr-wd 不宜把整段返回值当作可信执行配置。

## 3. 控件索引如何执行

macOS 包装层 click 对数字和坐标分别处理：数字转 `element_index`；数组转 x/y。客户端再把整数索引转成字符串 elementID，并通过 `ComputerUseIPCAppPerformActionRequest` 发送。`setValue` 同样把 elementIndex 转成 elementID。

这确认“索引操作”最终交给原生服务处理，不是 JS 在屏幕上搜同名文本。但原生服务保存元素引用、重定位和防失效的具体算法尚未确认，不能把 ZCode 的算法补在这里当作 OpenAI 事实。

原生 pipe 层已可读，包含协议握手、请求 ID、顺序调度、超时与连接关闭错误。本文不提供绕过可信 runtime 或审批的直接 IPC 调用脚本；edr-wd 应用自己的服务协议与系统授权实现同等能力。

## 4. ZCode 的独立实现提供了哪些可读细节

实际文件：

`/Applications/ZCode.app/Contents/Resources/glm/packages/zcode-cua-plugin/scripts/computer-use-client.mjs`

`/Applications/ZCode.app/Contents/Resources/glm/packages/node-repl-host/dist/mcp/server.js`

已读取的代码点：

- client 约 584 行：`createTarget` 内部 `observe` 调用 `get_app_state`；维护 stateId、frameId、treeSeen。
- client 约 641 行：`getAXState` 返回 state.text，并附加告知性文字。
- 首次给模型展示树时强制全量；只截图但不展示树时，不把该观察当成已展示的树基线。
- server 约 127900 行：`getAppStateTool` 将 disable_diffing 转 force_full，再经 settleObservation 调 broker 的 capture_app。
- server 约 126894 行：`resolveElement` 检查当前状态、索引范围、模型见过的身份与当前身份。
- `resolveShiftedIndex` 检查历史唯一性、指纹匹配与当前歧义；不满足则明确失败，而不是取同名第一个。

这是 ZCode 自己的代码证据，不证明 OpenAI 原生服务采用相同机制。注释中的历史事故也只能作为该实现的背景，不能当成当前测试结论。

## 5. 对 edr-wd 下一轮设计的修正

1. “统一观察→引用动作”的架构有本机发布代码证据；不是只能参考概念。
2. 原生采集服务、客户端协议、模型展示层应分开；getAXState 应是薄包装，而不是同时承担所有逻辑。
3. 全量/差量基线必须关联消费者实际见过的内容，后台采集不能悄悄移动用户或模型的展示基线。
4. 数字索引只是会话观察中的引用；真正动作应由持有原生控件的 target 解析并校验。
5. 不复制这些发布文件进项目生产代码；可读不等于开源或已获再分发许可。应按 OS 公开 API 独立实现相同机制。

主设计见 [无障碍优先迭代设计](accessibility-first-computer-use-design.md)。

## 6. 还没有拿到的部分

OpenAI 原生服务的 AX 属性读取顺序、树裁剪规则、元素缓存、diff 算法、失效重绑定和截图一致性实现仍未取得源码。需要进一步做有界的二进制符号/反汇编分析或在授权测试应用上进行黑盒验证。反编译得到的是恢复结果，不会自动还原成原始工程。

本次没有将 CuaDriver.app、ZCode helper 与 OpenAI SkyComputerUseService 混成一个实现，也没有修改产品、关闭授权或提取凭证。
