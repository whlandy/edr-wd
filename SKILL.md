---
name: edr-wd
description: |
  Windows EDR (HiSecEndpoint) GUI 自动化 skill。通过 pywinauto 枚举 Windows 应用控件树，
  用 control_id 作为唯一标识，完成点击、输入、下拉框选择、截图等操作。

  **注意：此 skill 与 maa-fw 完全独立，无任何共享代码。**

  触发场景：
  (1) 需要自动化 Windows 桌面应用（HiSecEndpoint 等）
  (2) 需要读取窗口控件树并选择控件操作
  (3) 通过 SSH tunnel 远程控制 Windows 上的 GUI 应用
  (4) OpenClaw / OpenCode 跨平台 GUI 自动化

  适用平台：
  - Windows (MCP Server 部署端)
  - Mac / Windows (Hermes Agent 通过 SSH tunnel 调用)
---

# EDR-WD — Windows EDR GUI 自动化

通过 pywinauto + fastmcp 实现控件级 Windows GUI 自动化，独立于 maa-fw 项目。

## 架构

```
Mac/Windows (Hermes Agent)
    │
    │  MCP over SSH tunnel (LocalForward 18765 → Windows:8765)
    ▼
Windows EDR MCP Server (fastmcp + pywinauto)
    │
    │  pywinauto / Win32 API
    ▼
HiSecEndpoint (Windows EDR 客户端)
```

## 部署步骤

### Step 1: Windows 上安装 MCP Server

```powershell
# 克隆仓库（如果还没有）
git clone https://github.com/whlandy/edr-wd.git
cd edr-wd

# 运行部署脚本（需要管理员权限）
.\deploy.ps1 -Port 8765
```

**或者手动安装：**

```powershell
pip install fastmcp pywinauto psutil Pillow
python -m edr_wd.server --http --port 8765
```

验证服务运行：

```powershell
# 应该看到 HTTP 服务在 8765 端口监听
netstat -an | findstr 8765
```

### Step 2: Mac 上配置 SSH Tunnel

```bash
# 添加到 ~/.ssh/config
Host edr-win
    HostName <WINDOWS_IP>
    User <WINDOWS_USERNAME>
    LocalForward 18765 127.0.0.1:8765
    ServerAliveInterval 60
```

```bash
# 启动隧道
ssh -N -f edr-win

# 验证隧道连通
python edr-wd/client.py --test --tunnel-port 18765
```

### Step 3: 配置 Hermes MCP Client

在 `~/.hermes/config.yaml` 中添加：

```yaml
mcp:
  servers:
    edr-wd:
      command: "ssh"
      args: ["-N", "-f", "edr-win"]
      url: "http://127.0.0.1:18765"
```

重启 Hermes Agent。

## 使用流程

### 1. 连接应用

```json
connect(title_re=".*HiSecEndpoint.*")
```

返回：

```json
{"ok": true, "title": "HiSec Endpoint"}
```

### 2. 查看控件树

```json
dump_tree()
```

返回控件列表：

```
[
  {"class_name": "TabControl", "text": "", "control_id": 1001, "rectangle": {"x": 0, "y": 0, "w": 800, "h": 600}, "is_visible": true, "is_enabled": true, "depth": 0},
  {"class_name": "Button", "text": "确定", "control_id": 1002, "rectangle": {"x": 700, "y": 560, "w": 80, "h": 30}, "is_visible": true, "is_enabled": true, "depth": 1},
  {"class_name": "ComboBox", "text": "", "control_id": 1003, "rectangle": {"x": 100, "y": 100, "w": 200, "h": 25}, "is_visible": true, "is_enabled": true, "depth": 1},
  ...
]
```

### 3. 操作控件

```json
// 点击按钮（用 control_id，最可靠）
click(control_id=1002)

// 向输入框写入文本
type_text(control_id=1004, string="Hello World")

// 下拉框选择
select(control_id=1003, item="选项文字")
// 或按索引
select(control_id=1003, index=2)

// 读取控件文本
get_text(control_id=1002)

// 截图
screenshot()
// 或保存到文件
screenshot(path="C:\\temp\\capture.png")
```

## 工具列表

| 工具 | 说明 | 关键参数 |
|------|------|---------|
| `connect` | 连接 Windows 应用 | `title_re`（窗口标题正则）、`process_name`、`pid` |
| `dump_tree` | 导出控件树 | `window_title_re`（可选，模糊匹配） |
| `click` | 点击控件 | `control_id`（首选）、`text`、`class_name` |
| `type_text` | 向输入框写入文本 | `control_id`、`string` |
| `select` | 下拉框选择 | `control_id`、`item`（文字）或 `index`（序号） |
| `get_text` | 读取控件文本 | `control_id`、`text`、`class_name` |
| `screenshot` | 截图 | `path`（可选，保存路径） |

## 控件标识优先级

1. **`control_id`** — 最可靠，同一窗口内唯一
2. **`text`** — 控件显示的文字（支持正则）
3. **`class_name`** — Windows 窗口类名

**推荐始终使用 `control_id`**，最稳定。

## HiSecEndpoint 典型操作

### 打开日志中心并查看升级日志

```
1. connect(title_re=".*HiSecEndpoint.*")
2. dump_tree()  → 找到"日志中心" tab 的 control_id
3. click(control_id=<日志中心tab>)  → 切换到日志 tab
4. dump_tree()  → 找到"升级日志" radio button
5. click(control_id=<升级日志radio>)  → 选中级日志
6. dump_tree()  → 找到"导出"/"刷新"按钮
7. click(control_id=<导出按钮>)
```

## 文件结构

```
edr-wd/
├── SKILL.md              ← 本文档
├── pyproject.toml         ← Python 包配置
├── deploy.ps1             ← Windows 部署脚本
├── client.py              ← Mac 端 SSH tunnel 工具
└── edr_wd/
    ├── __init__.py
    ├── server.py          ← fastmcp HTTP/stdio Server
    └── pywinauto_client.py ← pywinauto 封装
```

## 已知限制

- 需要 Windows 管理员权限（某些控件操作）
- 部分自定义控件（非标准 Win32 控件）可能无法枚举
- HiSecEndpoint 如果使用了非标准 UI 框架，control_id 可能不稳定
- SSH tunnel 依赖 Windows 开启 SSH Server 服务

## 调试

```powershell
# Windows 上查看 MCP server 日志
python -m edr_wd.server --http --port 8765
# 查看实时日志输出

# 查看哪些窗口可以连接
python -c "from pywinauto import Application; print([w.window_text() for w in Application().windows()])"
```
