# Window Detection — Agent Workflow

## Core Principle

> **Never assume a window is open. Verify it.**

`activate_edr()` returning `"ok": true` means the process started,
not that the GUI window appeared. Real success = `wait_window` finds the
EDRClient top-level window **and** `dump_tree` returns non-empty controls.

---

## New Tools

| Tool | What it does | When to use |
|------|-------------|-------------|
| `list_windows()` | Enumerate all top-level windows on the desktop | Debug / discover window state |
| `is_window_open(title_re, process_name, class_name)` | Check if any window matches | Quick boolean check before acting |
| `wait_window(title_re, process_name, class_name, timeout, interval)` | Poll until match or timeout | Verify a window appeared after an action |
| `activate_edr(wait=True, timeout=15)` | Launch EDR + wait for window | Replace the old fire-and-forget activate_edr |

### Return Structure

All window-detection tools return the same shape:

```json
{
  "ok": true,
  "found": true,
  "count": 1,
  "windows": [
    {
      "title": "华为智能终端安全系统",
      "class_name": "SafraUIMainWindow",
      "control_id": null,
      "process_id": 1234,
      "handle": 987654,
      "visible": true,
      "enabled": true,
      "rectangle": {"x": 100, "y": 100, "w": 900, "h": 600}
    }
  ]
}
```

---

## Standard Agent Sequence

### Before clicking any EDR control

```
1. is_window_open(title_re=".*(HiSec|Hisec|Endpoint|EDR|华为|安全).*")
   → if found: proceed
   → if not found: activate_edr(wait=True, timeout=15)

2. connect(title_re=".*(HiSec|Hisec|Endpoint|EDR|华为|安全).*")
   → if connect fails: screenshot + list_windows to diagnose

3. dump_tree(max_depth=10)
   → if controls is empty: screenshot + list_windows to diagnose

4. click_target(...) or click_window_at(x, y)

5. (optional) wait_window(...) to verify a new window appeared
```

### After clicking something that should open a dialog / sub-window

```
1. wait_window(title_re=".*目标对话框.*", timeout=10)
   → if timeout: screenshot + list_windows to diagnose

2. connect(title_re=".*目标对话框.*")
   → if connect fails: screenshot + list_windows to diagnose

3. dump_tree()
```

### After clicking HisecEndpointAgent system-tray icon

```
1. is_window_open(process_name="EDRClient.exe")
   → if found: proceed
   → if not found within 5s: screenshot + list_windows

2. connect(process_name="EDRClient.exe")

3. dump_tree()
```

---

## Default EDR Patterns

These are baked into `activate_edr` / `_EDR_TITLE_RE` and are also
valid for manual `is_window_open` / `wait_window` calls.

| Pattern | Matches |
|---------|---------|
| `title_re` | `.*(HiSec\|Hisec\|Endpoint\|EDR\|华为\|安全).*` |
| `process_name` | `EDRClient.exe` (preferred), `HisecEndpointAgent.exe` |
| `class_name` | `SafraUIMainWindow` |

**Recommendation**: always prefer `process_name` over `title_re` where
possible — process names are stable, titles can change with locale.

---

## Debug Checklist

When a window doesn't appear:

```
1. list_windows()                               → see all top-level windows
2. is_window_open(process_name="EDRClient.exe") → check by process
3. is_window_open(process_name="HisecEndpointAgent.exe") → maybe it launched but EDRClient hasn't spawned yet
4. screenshot()                                 → visual confirmation
```

---

## `activate_edr` Return Values

| Field | Meaning |
|-------|---------|
| `ok: true, already_open: true` | EDR GUI was already visible — no action taken |
| `ok: true, already_open: false, found: true` | Launched and window appeared within timeout |
| `ok: false` | Launch failed (permissions, wrong path, etc.) |
| `ok: true, found: false, error: "timeout"` | Launched but window didn't appear within timeout |

---

## Coordinate System

| Tool | Coordinate Type |
|------|----------------|
| `click_at(x, y)` | Screen absolute |
| `click_window_at(x, y)` | Window-relative |
| `click_target(automation_id=...)` | Control center (rectangle-based) |
| `click(control_id=...)` | UIA invoke |

See `references/activate-edr.md` for full coordinate reference.
