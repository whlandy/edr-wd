---
name: edr-wd
description: |
  Windows EDR (HiSecEndpoint) GUI 自动化 MCP Server。通过 pywinauto 枚举控件树，
  用 automation_id 或 text 作为唯一标识，完成点击、输入、下拉框、截图等操作。

  触发场景：
  (1) 需要自动化 Windows 桌面应用（HiSecEndpoint 等）
  (2) 需要读取窗口控件树并选择控件操作
  (3) 通过 MCP over SSH tunnel 远程控制 Windows GUI
  (4) 任意 MCP Client（OpenClaw / Hermes / Codex / Claude Desktop / 自己写的）跨平台 GUI 自动化

  适用平台：
  - Windows (MCP Server 部署端)
  - Mac / Linux (任意 MCP Client 通过 SSH tunnel 调用)
---

# EDR-WD — Windows EDR GUI 自动化

通用 MCP Server，任何支持 MCP streamable HTTP 的 agent 都能连接。

## 架构

```
┌──────────────────────────────────────────────────────────┐
│  Any MCP Client / Agent                                  │
│  (OpenClaw / Hermes / Codex / Claude Desktop / 其他)    │
└─────────────────────┬────────────────────────────────────┘
                      │  MCP streamable-http
                      ▼
            http://127.0.0.1:18765/mcp
                      │
                      │  SSH LocalForward
                      │  (Mac :18765 → Windows :8765)
                      ▼
┌──────────────────────────────────────────────────────────┐
│  Windows EDR MCP Server (fastmcp + pywinauto)            │
│  127.0.0.1:8765                                          │
│                                                          │
│  GUI: connect / dump_tree / click / type_text /          │
│       select / get_text / screenshot                     │
│  PS:  run_powershell / start_powershell /                │
│       get_job / cancel_job                              │
└─────────────────────┬────────────────────────────────────┘
                      │  pywinauto UIA
                      ▼
        HiSecEndpoint GUI (华为智能终端安全系统)
```

**edr-wd 不属于任何特定 Agent**。所有 MCP Client 都是等价的连接方式。

## Client Setup

MCP endpoint:

```
http://127.0.0.1:18765/mcp
Transport: streamable-http
```

配置你的 MCP client 指向这个 endpoint。以下是任选示例：

**Generic MCP client:**
```yaml
url: http://127.0.0.1:18765/mcp
transport: streamable-http
```

**OpenClaw:**
```bash
openclaw mcp set edr-wd '{"url":"http://127.0.0.1:18765/mcp","transport":"streamable-http"}'
```

**Claude Desktop (macOS):**
Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "edr-wd": {
      "url": "http://127.0.0.1:18765/mcp",
      "transport": "streamable-http"
    }
  }
}
```

**自己写的 MCP client:**
```python
# standard MCP HTTP client connecting to the same endpoint
```

Restart your MCP client if it does not hot-reload server configs.

## 部署步骤

### Step 1: Windows 部署

```powershell
git clone https://github.com/whlandy/edr-wd.git
cd edr-wd

# 一键 bootstrap + 启动服务；从 SSH 等非交互会话运行时会自动投递到已登录的交互桌面
.\deploy.ps1 -Action start -BindHost 127.0.0.1 -Port 8765
```

### 推荐工作流

1. 让 Windows 目标机保持一个已登录的本地或 RDP 桌面会话。
2. 在 Mac/Linux agent 侧运行 `bash agent/edr-wd.sh up`；它会通过 SSH 远程执行 `deploy.ps1 -Action start`，并自动拉起 tunnel。
3. `deploy.ps1 -Action start` 会先做 bootstrap，再启动 MCP server 并记录 PID。
   - 如果命令已经在 Windows 交互桌面里运行，server 直接作为后台进程启动。
   - 如果命令来自 SSH / 非交互会话，server 会通过 Windows 计划任务投递到当前已登录用户的交互桌面中启动。
4. 用 `deploy.ps1 -Action status` 查看当前状态，用 `deploy.ps1 -Action stop` 关闭服务。
5. 如果后续连接 EDR 时窗口没有激活，优先用 `connect(..., auto_activate=True)`。
6. 如果只是单独把 EDR 窗口拉起来，再显式调用 `activate_edr()`。

Agent 侧正常 workflow:

```bash
bash agent/edr-wd.sh up
```

Windows 侧也仍支持直接运行:

```powershell
cd target
.\deploy.ps1 -Action start -BindHost 127.0.0.1 -Port 8765
```

**单独启动 server：**
```powershell
$env:EDR_WD_ENABLE_POWERSHELL = "1"
python -m edr_wd.server --http --host 127.0.0.1 --port 8765
```

**重要：GUI 自动化必须运行在 Windows 已登录的交互桌面会话中。**
`deploy.ps1 -Action start -StartMode auto` 会自动处理 SSH / 非交互会话：它不会把 pywinauto server 留在 SSH 后台，而是用交互式计划任务投递到当前已登录用户的桌面。不要改用 `Start-Job`、Windows service、纯 SSH 后台进程来启动 pywinauto server；这些会话通常没有 GUI desktop context，`dump_tree` 会返回空树或找不到窗口。

如果没有任何用户登录 Windows 桌面，计划任务无法创建 GUI desktop context；先让目标机保持一次登录会话，然后 agent 侧就能反复远程拉起/停止，无需在 RDP 里手动执行命令。

也可以显式指定启动模式：

```powershell
.\deploy.ps1 -Action start -StartMode auto
.\deploy.ps1 -Action start -StartMode scheduled-task
.\deploy.ps1 -Action start -StartMode process
```

然后在 MCP 客户端侧使用 `connect(title_re=".*HiSec.*", auto_activate=True)` 作为默认入口。这样当 EDR 没有激活时，server 会自动尝试 `activate_edr()` 再重试一次。

### 关闭和检查

```powershell
.\deploy.ps1 -Action status
.\deploy.ps1 -Action stop
```

### 文件传输

Windows 侧启用 SSH Server 后，agent 可以直接 `scp` 文件到固定目录，例如：

```bash
scp ./payload.zip admin@WINDOWS_HOST:C:\path\to\edr-wd\target\incoming\
```

推荐用统一入口脚本来做这些操作：

```bash
bash agent/edr-wd.sh up
bash agent/edr-wd.sh status
bash agent/edr-wd.sh push ./payload.zip
bash agent/edr-wd.sh smoke
bash agent/edr-wd.sh smoke --gui
bash agent/edr-wd.sh down
```

`agent/edr-wd.sh` 会负责：
- 启动 Windows 侧 `deploy.ps1`
- 拉起或关闭本地 SSH tunnel
- 复制文件到 target
- 跑 MCP smoke test

### Smoke test

Windows 目标机上的 live server 可以用这个脚本做回归：

```powershell
python target\tests\smoke_mcp_client.py --base-url http://127.0.0.1:8765/mcp
python target\tests\smoke_mcp_client.py --base-url http://127.0.0.1:8765/mcp --gui
```

`--gui` 模式会额外验证 `connect(..., auto_activate=True)` 和 `dump_tree`，更适合做最终回归。

如需开放直连 MCP 端口，而不是 SSH tunnel，再显式运行：
```powershell
.\target\setup-fw.ps1 -ExposeMcp
python -m edr_wd.server --http --host 0.0.0.0 --port 8765
```

### Step 2: Mac/Linux 配置 SSH tunnel

```bash
git clone https://github.com/whlandy/edr-wd.git
cd edr-wd
bash agent/tunnel.sh start
```

**tunnel 命令：**
```bash
bash agent/tunnel.sh start   # 启动
bash agent/tunnel.sh status   # 查看状态
bash agent/tunnel.sh stop     # 停止
```

**参数化（环境变量）：**
```bash
EDR_WD_HOST=170.170.11.26 \
EDR_WD_USER=admin \
EDR_WD_LOCAL_PORT=18765 \
EDR_WD_REMOTE_PORT=8765 \
bash agent/tunnel.sh start
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
    "clientInfo": {"name": "test", "version": "1.0"}
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
| `click` | 点击控件 | `automation_id`、`control_id`、`text`、`class_name` |
| `click_target` | 点击控件矩形中心 | `automation_id`、`text`、`class_name`、`x_offset`、`y_offset` |
| `click_at` | 点击绝对屏幕坐标 | `x`、`y` |
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
    ├── tunnel.sh              ← SSH tunnel 管理（参数化，唯一必需）
    └── setup-mac.sh           ← SSH config + tunnel setup
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
