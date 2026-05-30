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

### Step 0: 环境要求

- **Python 3.10+**（fastmcp 要求，pywinauto 支持 3.9）
- **Windows 开启 SSH Server**（远程控制必需）
- **管理员权限 PowerShell**

### Step 1: 克隆项目

```powershell
git clone https://github.com/whlandy/edr-wd.git
cd edr-wd
git pull  # 更新到最新版本
```

### Step 2: 一键部署（推荐）

以**管理员**运行 PowerShell：

```powershell
# 完整部署（SSH Server + 防火墙 + 依赖 + 启动服务）
.\deploy.ps1 -Port 8765 -AutoStart

# 跳过 SSH Server 配置（如果已配置）
.\deploy.ps1 -Port 8765 -NoSsh -AutoStart
```

**或分步执行：**

```powershell
# 2.1 配置 SSH Server（可选，已有可跳过）
.\setup-ssh.ps1 -AutoStart

# 2.2 配置防火墙（可选，已有可跳过）
.\setup-fw.ps1

# 2.3 安装依赖
pip install fastmcp pywinauto psutil Pillow

# 2.4 启动服务
python -m edr_wd.server --http --host 0.0.0.0 --port 8765
```

### Step 3: 验证服务

```powershell
# 检查端口监听
netstat -an | findstr 8765

# 本机测试
curl http://127.0.0.1:8765
# 返回 404 是正常的（MCP 协议不响应普通 GET）
```

### Step 2: Mac 配置

#### 方式 A：一键配置（推荐）

```bash
# 传入 Windows IP 和用户名
bash setup-mac.sh 170.170.11.26 admin
```

#### 方式 B：分步手动配置

```bash
# 1. 配置 SSH tunnel
bash tunnel.sh start

# 2. 测试连接
bash tunnel.sh test
```

#### 常用 tunnel 命令

```bash
bash tunnel.sh start   # 启动隧道
bash tunnel.sh status  # 查看状态
bash tunnel.sh stop    # 停止隧道
bash tunnel.sh test    # 测试连接
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
dump_tree(window_title_re=".*华为.*", max_depth=10)
```

返回控件列表：

```
[
  {"class_name": "Dialog", "text": "华为智能终端安全系统", "control_id": null, "rectangle": {"x": 2, "y": 1, "w": 920, "b": 600}, "is_visible": true, "is_enabled": true, "depth": 0, "automation_id": "SafraUIMainWindow", "control_type": "Window"},
  {"class_name": "Button", "text": "快速扫描", "control_id": null, "rectangle": {"x": 185, "y": 337, "w": 212, "h": 204}, "is_visible": true, "is_enabled": true, "depth": 4, "automation_id": "...quickScanBtn", "control_type": "Button"},
  {"class_name": "Button", "text": "全盘扫描", "control_id": null, "rectangle": {"x": 417, "y": 337, "w": 212, "h": 204}, "is_visible": true, "is_enabled": true, "depth": 4, "automation_id": "...fullScanBtn", "control_type": "Button"},
  ...
]
```

> **注意**：`automation_id` 和 `control_type` 字段只有在 `backend='uia'` 时才存在（默认）。`control_id` 对 Qt 控件通常为 null，应使用 `automation_id` 或 `text` 作为控件标识。

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
| `dump_tree` | 导出控件树 | `window_title_re`（可选）、`max_depth`（默认15） |
| `click` | 点击控件 | `control_id`（首选）、`text`（文字）、`class_name` |
| `type_text` | 向输入框写入文本 | `control_id`、`string` |
| `select` | 下拉框选择 | `control_id`、`item`（文字）或 `index`（序号） |
| `get_text` | 读取控件文本 | `control_id`、`text`、`class_name` |
| `screenshot` | 截图 | `path`（可选，保存路径） |

## 控件标识优先级

1. **`automation_id`** — 最可靠，Qt 控件的 `AutomationId` 全局唯一（`backend='uia'` 才有）
2. **`text`** — 控件显示的文字（支持正则），最常用
3. **`control_id`** — Windows 原生控件有效，Qt 控件通常为 null
4. **`class_name`** — Windows 窗口类名

**Qt 窗口推荐用 `text` 或 `automation_id`**，最稳定。

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
├── SKILL.md              ← 部署文档
├── pyproject.toml         ← Python 包配置
├── deploy.ps1             ← Windows 一键部署脚本（推荐）
├── setup-ssh.ps1         ← Windows SSH Server 独立配置脚本
├── setup-fw.ps1          ← Windows 防火墙独立配置脚本
├── setup-mac.sh          ← Mac 一键配置脚本（SSH tunnel + Hermes）
├── tunnel.sh             ← Mac SSH tunnel 管理脚本
├── client.py             ← Mac 端 tunnel 测试工具
└── edr_wd/
    ├── __init__.py
    ├── server.py          ← fastmcp HTTP/stdio Server
    └── pywinauto_client.py ← pywinauto 封装
```

## 快速上手

**Windows（管理员 PowerShell）：**
```powershell
git clone https://github.com/whlandy/edr-wd.git
cd edr-wd
.\deploy.ps1 -Port 8765 -AutoStart
```

**Mac：**
```bash
git clone https://github.com/whlandy/edr-wd.git
cd edr-wd
bash setup-mac.sh 170.170.11.26 admin
bash tunnel.sh start
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
