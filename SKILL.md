---
name: edr-wd
description: |
  Windows EDR (HiSecEndpoint) GUI 自动化 MCP Server。通过 pywinauto 枚举控件树，
  用 automation_id 或 text 作为唯一标识，完成点击、输入、下拉框、截图等操作。

  触发场景：
  (1) 需要自动化 Windows 桌面应用（HiSecEndpoint 等）
  (2) 需要读取窗口控件树并选择控件操作
  (3) 通过 MCP over SSH tunnel 远程控制 Windows GUI
  (4) OpenClaw / OpenCode / Hermes / 任意 MCP Client 跨平台 GUI 自动化

  适用平台：
  - Windows (MCP Server 部署端)
  - Mac / Linux (任意 MCP Client 通过 SSH tunnel 调用)
---

# EDR-WD — Windows EDR GUI 自动化

通过 pywinauto + fastmcp 实现控件级 Windows GUI 自动化，作为通用 MCP Server 供任意 MCP Client 调用。

## 架构

```
┌─────────────────────────────────────────────────────────────┐
│  Agent / OpenClaw / Hermes / Codex / 任意 MCP Client        │
└──────────────────┬──────────────────────────────────────────┘
                   │  MCP over SSH tunnel (LocalForward 18765→Windows:8765)
                   ▼
┌─────────────────────────────────────────────────────────────┐
│  Windows EDR MCP Server (fastmcp + pywinauto, 0.0.0.0:8765)│
│  └── run_powershell / start_powershell / get_job / cancel_job│
│  └── connect / dump_tree / click / type_text / screenshot     │
└──────────────────┬────────────────────────────────────────────┘
                   │  pywinauto UIA / Win32 API
                   ▼
        HiSecEndpoint GUI 窗口 (华为智能终端安全系统)
```

**edr-wd 不属于任何特定 Agent**。Hermes、OpenClaw、Codex 只是不同的 MCP Client。

## 部署步骤

### Step 0: 环境要求

- **Python 3.10+**（fastmcp 要求）
- **Windows 开启 SSH Server**（远程控制必需）
- **管理员权限 PowerShell**

### Step 1: Windows 部署

```powershell
git clone https://github.com/whlandy/edr-wd.git
cd edr-wd

# 完整部署（SSH Server + 防火墙 + 依赖 + 启动服务）
.\deploy.ps1 -Port 8765 -AutoStart
```

### Step 2: Mac/Linux 配置 tunnel

```bash
git clone https://github.com/whlandy/edr-wd.git
cd edr-wd
bash agent/setup-mac.sh 170.170.11.26 admin
```

**tunnel 常用命令**：

```bash
bash agent/tunnel.sh start   # 启动隧道（参数可环境变量覆盖）
bash agent/tunnel.sh status  # 查看状态
bash agent/tunnel.sh stop    # 停止隧道
bash agent/tunnel.sh test     # 测试连接
```

**参数化**（环境变量或命令行）：

```bash
EDR_WD_HOST=170.170.11.26 \
EDR_WD_USER=admin \
EDR_WD_LOCAL_PORT=18765 \
EDR_WD_REMOTE_PORT=8765 \
bash agent/tunnel.sh start
```

### Step 3: 配置 MCP Client

#### OpenClaw（推荐主路径）

```bash
openclaw mcp set edr-wd '{
  "url": "http://127.0.0.1:18765/mcp",
  "transport": "streamable-http",
  "connectionTimeoutMs": 10000
}'
openclaw mcp show edr-wd --json
```

#### Hermes（可选，需要显式 opt-in）

```bash
# 启动 setup 时指定 hermes client
bash agent/setup-mac.sh 170.170.11.26 admin --client hermes
```

或在 `~/.hermes/config.yaml` 中添加：

```yaml
mcp_servers:
  edr-wd:
    url: "http://127.0.0.1:18765/mcp"
```

重启 Hermes Agent。

#### 其他 MCP Client

通用 HTTP MCP Client 均可连接：

```
URL: http://127.0.0.1:18765/mcp
Transport: streamable-http
```

### Step 4: 验证服务

```powershell
# Windows 上检查端口
netstat -an | findstr 8765
```

```bash
# Mac 上测试
bash agent/tunnel.sh test
```

## MCP 协议细节（FastMCP 3.3.1）

**Endpoint**: `POST /mcp`（根路径 `/` 返回 404，这是设计）

**Session 建立流程**：
1. `GET /mcp` → 返回 400 但 header 中含 `Mcp-Session-Id`
2. `POST initialize`（带 session id）→ 成功后 session 激活
3. `POST tools/list` / `tools/call`

**必须 header**：
```
Accept: application/json, text/event-stream
Content-Type: application/json
Mcp-Session-Id: <从GET响应header获取的值>   # 注意大写 M
```

**protocolVersion 必须是 `2025-11-25`**

```python
import urllib.request, urllib.error, json

base_url = "http://127.0.0.1:18765/mcp"
session_id = None

def do_get():
    global session_id
    req = urllib.request.Request(base_url, method="GET",
        headers={"Accept": "text/event-stream"})
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        session_id = resp.headers.get("Mcp-Session-Id")
    except urllib.error.HTTPError as e:
        session_id = e.headers.get("Mcp-Session-Id")

def do_rpc(method, params):
    global session_id
    payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params
    }).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Mcp-Session-Id": session_id
    }
    req = urllib.request.Request(base_url, data=payload, headers=headers, method="POST")
    resp = urllib.request.urlopen(req, timeout=15)
    return resp.read().decode()

do_get()          # 建立 session
do_rpc("initialize", {
    "protocolVersion": "2025-11-25",
    "capabilities": {},
    "clientInfo": {"name": "openclaw", "version": "1.0"}
})
result = do_rpc("tools/call", {
    "name": "run_powershell",
    "arguments": {"command": "Get-Date", "timeout": 10}
})
```

## 工具列表

| 工具 | 说明 | 关键参数 |
|------|------|---------|
| `connect` | 连接 Windows 应用 | `title_re`、`process_name`、`pid` |
| `dump_tree` | 导出控件树 | `window_title_re`（可选）、`max_depth`（默认15） |
| `click` | 点击控件 | `control_id`（首选）、`text`（文字）、`class_name` |
| `type_text` | 向输入框写入文本 | `control_id`、`string` |
| `select` | 下拉框选择 | `control_id`、`item`（文字）或 `index`（序号） |
| `get_text` | 读取控件文本 | `control_id`、`text`、`class_name` |
| `screenshot` | 截图 | `path`（可选） |
| `run_powershell` | 同步执行 PowerShell | `command`、`timeout`（≤30s） |
| `start_powershell` | 启动异步 PowerShell job | `command`、`timeout`（≤300s） |
| `get_job` | 轮询 job 状态 | `job_id` |
| `cancel_job` | 取消 job | `job_id` |

## 控件标识优先级

1. **`automation_id`** — 最可靠，Qt 控件的 `AutomationId` 全局唯一（`backend='uia'` 才有）
2. **`text`** — 控件显示的文字（支持正则），最常用
3. **`control_id`** — Windows 原生控件有效，Qt 控件通常为 null
4. **`class_name`** — Windows 窗口类名

**Qt 窗口推荐用 `text` 或 `automation_id`**，最稳定。

## HiSecEndpoint 典型操作

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
├── SKILL.md                  ← 本文档
├── pyproject.toml
├── target/                   ← Windows 目标机器（MCP Server + EDR 软件）
│   ├── deploy.ps1            ← Windows 一键部署脚本
│   ├── edr_wd/
│   │   ├── server.py          ← fastmcp HTTP Server
│   │   └── pywinauto_client.py
│   └── tests/
└── agent/                    ← Mac/Linux 控制端脚本
    ├── tunnel.sh              ← SSH tunnel 管理（参数化）
    └── setup-mac.sh           ← 配置脚本（可选 --client openclaw/hermes/none）
```

## 已知限制

- 需要 Windows 管理员权限（某些控件操作）
- 部分自定义控件（非标准 Win32 控件）可能无法枚举
- SSH tunnel 依赖 Windows 开启 SSH Server 服务

## 调试

```powershell
# Windows 上查看 MCP server 日志
cd target
python -m edr_wd.server --http --port 8765

# 查看哪些窗口可以连接
python -c "from pywinauto import Application; print([w.window_text() for w in Application().windows()])"
```
