# EDR Activation & Operation Guide

## Activate EDR GUI

If the EDR window is not visible, activate it first:

```powershell
$exe = "C:\Program Files\HiSec-Endpoint\core\safra\HisecEndpointAgent.exe"
Start-Process -FilePath $exe -ArgumentList "cmd ui"
```

Or via MCP tool:

```
activate_edr()
```

## Standard Operation Sequence

1. **activate_edr()** — ensure EDR window is visible
2. **connect(title_re=".*HiSec.*", auto_activate=False)** — connect to window (auto_activate=True also activates EDR on failure)
3. **dump_tree(max_depth=10)** — inspect controls
4. **click_target(...)** or **click_window_at(x, y)** — click a control
5. **screenshot(path="C:\\Users\\<TARGET_USER>\\verify.png")** — verify result

## Coordinate System Reference

| Tool | Coordinate Type | When to Use |
|------|---------------|-------------|
| `click_at(x, y)` | Screen absolute | When you have raw screen coordinates |
| `click_window_at(x, y)` | Window-relative | When coordinates are relative to window top-left |
| `click_target(automation_id=...)` | Control center | When targeting a specific control by its rectangle |
| `click(control_id=...)` | UIA invoke | For standard Button/CheckBox invoke |

## dump_tree Response

```json
{
  "ok": true,
  "title": "华为智能终端安全系统",
  "window_rectangle": {"x": 101, "y": 32, "w": 760, "h": 559},
  "rectangle_mode": "screen",
  "controls": [...]
}
```

- `window_rectangle` — main window position and size
- `rectangle_mode` — "screen" means all control rectangles are in absolute screen coordinates
- Each control has its own `rectangle` in the same coordinate space
