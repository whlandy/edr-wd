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

## 部署步骤

### Step 0: 环境要求

- **Python 3.10+**（fastmcp 要求，pywinauto 支持 3.9）
- **Windows 开启 SSH Server**（远程控制必需）
- **管理员权限 PowerShell**

### Step 1: 克隆项目

```powershell
git clone https://github.com/whlandy/edr-wd.git
cd edr-wd
```

### Step 2: Windows 开启 SSH Server

以**管理员**运行 PowerShell：

```powershell
# 添加 SSH Server 功能
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0

# 启动并设置开机自启
Start-Service sshd
Set-Service -Name sshd -StartupType Automatic

# 确认 22 端口监听
netstat -an | findstr 22
```

### Step 3: 安装依赖

```powershell
pip install fastmcp pywinauto psutil Pillow
```

> ⚠️ `fastmcp` 需要 Python 3.10+，旧版本 Python 需先升级。

### Step 4: 配置防火墙

以**管理员**运行 PowerShell：

```powershell
# 放行 SSH 端口 22
New-NetFirewallRule -Name "SSH" -DisplayName "SSH" -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 22

# 放行 EDR-WD MCP 端口（如果需要直接连接，不走 SSH tunnel）
New-NetFirewallRule -Name "EDR-WD-8765" -DisplayName "EDR-WD MCP" -Enabled True -Direction Inbound -Protocol TCP -Action Allow -LocalPort 8765
```

### Step 5: 启动 MCP Server

**方式 A：直接连接（测试用）**
```powershell
# 绑定 0.0.0.0，允许外部连接
python -m edr_wd.server --http --host 0.0.0.0 --port 8765
```

**方式 B：SSH Tunnel（生产推荐）**
```powershell
# 仅监听本地，SSH tunnel 转发
python -m edr_wd.server --http --port 8765
```

**方式 C：开机自启后台服务**
```powershell
# 后台运行，开机自启
Start-Process -FilePath python -ArgumentList "-m edr_wd.server --http --host 0.0.0.0 --port 8765" -WindowStyle Hidden
```

### Step 6: 验证服务

```powershell
# 检查端口监听
netstat -an | findstr 8765

# 本机测试
curl http://127.0.0.1:8765
# 返回 404 是正常的（MCP 协议不响应普通 GET）
```

### Step 2: Mac 上配置 SSH Tunnel

> ⚠️ **已知问题：** Windows SSH Server 默认不许可空密码登录。如果 `admin` 用户无密码，需先设置密码或修改 SSH 配置。

#### 2.1 配置 SSH config

```bash
# 添加到 ~/.ssh/config
Host edr-win
    HostName <WINDOWS_IP>        # 例如 170.170.11.26
    User <WINDOWS_USERNAME>      # 例如 admin
    LocalForward 18765 127.0.0.1:8765
    ServerAliveInterval 60
```

#### 2.2 启动隧道

```bash
# 启动（后台运行）
ssh -N -f edr-win

# 验证隧道连通
curl http://127.0.0.1:18765
# 返回 404 说明隧道正常
```

#### 2.3 Windows SSH 空密码处理

如果连接被拒（"Too many authentication failures" 或 "Permission denied"）：

```powershell
# 在 Windows 上为 admin 用户设置密码
net user admin <密码>

# 或修改 SSH 配置允许空密码
# 编辑 C:\ProgramData\ssh\sshd_config
# 找到 PasswordAuthentication yes
# 找到 PermitEmptyPasswords yes
# 重启 sshd: Restart-Service sshd
```

#### 2.4 常用隧道命令

```bash
# 查看活跃隧道
lsof -i :18765

# 关闭隧道
pkill -f "ssh -N -f edr-win"

# 重新连接
ssh -N -f edr-win
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
