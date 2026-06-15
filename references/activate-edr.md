# HiSec EDR Activation Reference

Use this reference when changing `activate_edr` or debugging HiSec window-pair
E2E failures.

## Success Contract

`activate_edr(wait=True)` succeeds only when both windows are visible:

- Entry/main window:
  - Windows: `HisecEndpointAgent.exe`
  - macOS: `HiSecEndpointAgent`
- Target/client window:
  - Windows: `EDRClient.exe`
  - macOS: `EDRClient`

The returned payload should include `main.window_found == true` and
`client.window_found == true`.

## Windows Flow

1. Ensure `HisecEndpointAgent.exe cmd ui` is visible.
2. Prefer direct EDRClient launch:

```powershell
cd "C:\Program Files\HiSec-Endpoint\core"
.\EDRClient.exe 17 --show
```

3. If EDRClient does not appear, connect to `HisecEndpointAgent.exe` and click
   the `edrWidget` entry card.
4. When connecting by process, prefer a visible top-level desktop window handle.
   Some Qt/UIA windows are enumerable through `Desktop(backend="uia").windows()`
   but fail with `Application.connect(process=PID)`.

Use the MCP tool:

```
activate_edr(wait=True, timeout=15)
```

## macOS Flow

1. Ensure `HiSecEndpointAgent` is visible.
   - Try `open /Applications/HiSecEndpoint.app`.
   - Fall back to
     `/Applications/HiSecEndpoint.app/Contents/MacOS/safra/HiSecEndpointAgent cmd ui`.
   - Do not redirect stdout/stderr; the Qt window can appear offscreen.
2. Prefer direct EDRClient launch:

```bash
sudo -n /Applications/HiSecEndpoint.app/Contents/script/root_start_client.sh
```

3. If EDRClient does not appear, foreground the HiSec entry window and use the
   Swift Accessibility helper to click “前往安全防护中心”.

## Standard Operation Sequence

1. **activate_edr(wait=True)** — ensure both HiSec windows are visible.
2. **connect(process_name=...)** — connect to the exact window that owns the
   requested control. Use `HisecEndpointAgent.exe` / `HiSecEndpointAgent` for
   entry-window controls and `EDRClient.exe` / `EDRClient` for client-window
   controls.
3. **dump_tree(max_depth=...)** — inspect controls and select a unique node.
4. **click(...)** — prefer semantic component activation. On Windows, a precise
   UIA click should return `uia_invoke` or `uia_toggle` when the control exposes
   those patterns.
5. **dump_tree(...)** or **screenshot(...)** — verify the resulting page.

## Action Reference

| Tool | Coordinate Type | When to Use |
|------|---------------|-------------|
| `click(...)` | Component semantic action when available | Default for tree-derived selectors |
| `click_target(automation_id=...)` | Control center | Fallback when semantic activation is unavailable |
| `click_window_at(x, y)` | Window-relative | Fallback for known window-relative coordinates |
| `click_at(x, y)` | Screen absolute | Final fallback for raw screen coordinates |

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
