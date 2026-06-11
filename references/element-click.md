# Element-Level Click on macOS Targets

## The Problem

The MCP `click` tool (by `control_id`/`automation_id`) and `dump_tree` are **Windows-only** in the current edr-wd backend. On macOS targets, the `macos_accessibility` backend only exposes `click_at(x, y)` — coordinate-based clicking.

This means the AI **cannot** discover UI elements through the MCP interface on macOS. The workaround is to write a **Swift script** that uses macOS Accessibility APIs directly, then execute it over SSH.

---

## The Solution: Swift + AX API

macOS exposes its entire UI as an **Accessibility tree** (AX tree) through the Application Services framework — equivalent to Windows UIAutomation. Any element can be found by title/role/value, and clicked via `AXPressAction` or CGEvent.

### When to Use This

- You need to click a button/link/control by its **label text** on a macOS target
- `click_at(x, y)` is unreliable (dynamic UI, unknown position)
- `dump_tree` / `click` MCP tools are Windows-only
- The standard `activate_edr` → `click_at` flow isn't sufficient

---

## Complete Pattern

### Step 1: Find the Target App's PID

```bash
PID=$(sshpass -p 'PASSWORD' pgrep -f "AppName" | head -1)
echo "PID: $PID"
```

Or via Swift (enumerate all running apps):

```swift
import Cocoa

let targetNames = ["AppName", "AnotherName"]
for app in NSWorkspace.shared.runningApplications {
    if targetNames.contains(where: { (app.localizedName ?? "").contains($0) }) {
        print("\(app.localizedName ?? "?") (pid=\(app.processIdentifier))")
    }
}
```

### Step 2: Traverse the AX Tree to Find an Element

```swift
import Cocoa
import ApplicationServices

let TARGET = "按钮文字"  // e.g. "日志中心"

func findInElement(_ el: AXUIElement) -> AXUIElement? {
    // Check all string attributes for match
    var titleRef: CFTypeRef?
    var valueRef: CFTypeRef?
    var descRef: CFTypeRef?
    
    let title = (AXUIElementCopyAttributeValue(el, kAXTitleAttribute as CFString, &titleRef) == .success) ? (titleRef as? String ?? "") : ""
    let value = (AXUIElementCopyAttributeValue(el, kAXValueAttribute as CFString, &valueRef) == .success) ? (valueRef as? String ?? "") : ""
    let desc = (AXUIElementCopyAttributeValue(el, kAXDescriptionAttribute as CFString, &descRef) == .success) ? (descRef as? String ?? "") : ""
    
    if title.contains(TARGET) || value.contains(TARGET) || desc.contains(TARGET) {
        return el
    }
    
    // Recurse into children
    var childrenRef: CFTypeRef?
    if AXUIElementCopyAttributeValue(el, kAXChildrenAttribute as CFString, &childrenRef) == .success,
       let children = childrenRef as? [AXUIElement] {
        for child in children {
            if let found = findInElement(child) { return found }
        }
    }
    return nil
}
```

### Step 3: Click the Element

```swift
// Method A: AXPress (preferred — uses system accessibility action)
func pressAX(_ el: AXUIElement) -> Bool {
    return AXUIElementPerformAction(el, kAXPressAction as CFString) == .success
}

// Method B: CGEvent click at element center (fallback)
func rectFromElement(_ el: AXUIElement) -> CGRect? {
    var posRef: CFTypeRef?
    var sizeRef: CFTypeRef?
    guard AXUIElementCopyAttributeValue(el, kAXPositionAttribute as CFString, &posRef) == .success,
          AXUIElementCopyAttributeValue(el, kAXSizeAttribute as CFString, &sizeRef) == .success,
          CFGetTypeID(posRef) == AXValueGetTypeID(),
          CFGetTypeID(sizeRef) == AXValueGetTypeID() else { return nil }
    var pos = CGPoint.zero
    var size = CGSize.zero
    guard AXValueGetValue(posRef as! AXValue, .cgPoint, &pos),
          AXValueGetValue(sizeRef as! AXValue, .cgSize, &size) else { return nil }
    return CGRect(origin: pos, size: size)
}

func clickCenter(_ el: AXUIElement) -> Bool {
    guard let rect = rectFromElement(el) else { return false }
    let cx = rect.midX
    let cy = rect.midY
    let down = CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown,
                        mouseCursorPosition: CGPoint(x: cx, y: cy), mouseButton: .left)!
    let up = CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp,
                     mouseCursorPosition: CGPoint(x: cx, y: cy), mouseButton: .left)!
    down.post(tap: .cghidEventTap)
    up.post(tap: .cghidEventTap)
    return true
}
```

### Step 4: Assemble and Execute

Full working script (`/tmp/click_element.swift`):

```swift
#!/usr/bin/env swift
import Cocoa
import ApplicationServices

let TARGET = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : ""
let APP_NAME = CommandLine.arguments.count > 2 ? CommandLine.arguments[2] : ""

// Find the app
guard let app = NSWorkspace.shared.runningApplications.first(where: {
    ($0.localizedName ?? "").contains(APP_NAME)
}) else {
    print("ERROR: \(APP_NAME) not found")
    exit(1)
}

let axApp = AXUIElementCreateApplication(app.processIdentifier)
var windowsRef: CFTypeRef?
guard AXUIElementCopyAttributeValue(axApp, kAXWindowsAttribute as CFString, &windowsRef) == .success,
      let windows = windowsRef as? [AXUIElement], !windows.isEmpty else {
    print("ERROR: No windows")
    exit(1)
}

// Recursive search
func findInElement(_ el: AXUIElement) -> AXUIElement? {
    var titleRef: CFTypeRef?
    var valueRef: CFTypeRef?
    var descRef: CFTypeRef?
    let title = (AXUIElementCopyAttributeValue(el, kAXTitleAttribute as CFString, &titleRef) == .success) ? (titleRef as? String ?? "") : ""
    let value = (AXUIElementCopyAttributeValue(el, kAXValueAttribute as CFString, &valueRef) == .success) ? (valueRef as? String ?? "") : ""
    let desc = (AXUIElementCopyAttributeValue(el, kAXDescriptionAttribute as CFString, &descRef) == .success) ? (descRef as? String ?? "") : ""
    if title.contains(TARGET) || value.contains(TARGET) || desc.contains(TARGET) { return el }
    var childrenRef: CFTypeRef?
    if AXUIElementCopyAttributeValue(el, kAXChildrenAttribute as CFString, &childrenRef) == .success,
       let children = childrenRef as? [AXUIElement] {
        for child in children { if let f = findInElement(child) { return f } }
    }
    return nil
}

guard let el = findInElement(windows[0]) else {
    print("ERROR: '\(TARGET)' not found")
    exit(1)
}

// Try AXPress, fallback to CGEvent
if AXUIElementPerformAction(el, kAXPressAction as CFString) == .success {
    print("AXPress succeeded")
    exit(0)
} else {
    // CGEvent fallback...
    var posRef: CFTypeRef?
    var sizeRef: CFTypeRef?
    if AXUIElementCopyAttributeValue(el, kAXPositionAttribute as CFString, &posRef) == .success,
       AXUIElementCopyAttributeValue(el, kAXSizeAttribute as CFString, &sizeRef) == .success {
        var pos = CGPoint.zero, size = CGSize.zero
        if AXValueGetValue(posRef as! AXValue, .cgPoint, &pos),
           AXValueGetValue(sizeRef as! AXValue, .cgSize, &size) {
            let cx = pos.x + size.width/2, cy = pos.y + size.height/2
            CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown,
                    mouseCursorPosition: CGPoint(x: cx, y: cy), mouseButton: .left)?
                .post(tap: .cghidEventTap)
            CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp,
                    mouseCursorPosition: CGPoint(x: cx, y: cy), mouseButton: .left)?
                .post(tap: .cghidEventTap)
            print("CGEvent click at (\(cx), \(cy))")
            exit(0)
        }
    }
    print("ERROR: click failed")
    exit(1)
}
```

Execute via SSH:

```bash
sshpass -p 'PASSWORD' scp -P 22 /tmp/click_element.swift user@host:/tmp/click_element.swift
sshpass -p 'PASSWORD' ssh -p 22 user@host 'EDR_WD_ALLOW_REAL_CLICKS=1 swift /tmp/click_element.swift "按钮文字" "AppName"'
```

---

## macOS AX Attributes Reference

| Attribute | CFString | Meaning |
|-----------|----------|---------|
| Title | `kAXTitleAttribute` | Button text, window title |
| Value | `kAXValueAttribute` | Input field content, checkbox state |
| Description | `kAXDescriptionAttribute` | Accessibility description |
| Role | `kAXRoleAttribute` | Element type: `AXButton`, `AXStaticText`, `AXWindow`, etc. |
| Position | `kAXPositionAttribute` | `CGPoint` — screen coordinates |
| Size | `kAXSizeAttribute` | `CGSize` — element dimensions |
| Children | `kAXChildrenAttribute` | Child elements in the AX tree |
| Parent | `kAXParentAttribute` | Parent element |
| RoleDescription | `kAXRoleDescriptionAttribute` | Human-readable role name |

Common roles: `AXButton`, `AXStaticText`, `AXTextField`, `AXCheckBox`, `AXRadioButton`, `AXMenuItem`, `AXLink`, `AXGroup`, `AXWindow`, `AXToolbar`, `AXMenuBar`

---

## Common AX Tree Patterns

### Pattern 1: Click by Button Title
```swift
let TARGET = "确定"
let el = findInElement(window, target: TARGET)
AXUIElementPerformAction(el, kAXPressAction as CFString)
```

### Pattern 2: Click by Role + Title
```swift
for child in children {
    let role = getStringAttr(child, kAXRoleAttribute)
    let title = getStringAttr(child, kAXTitleAttribute)
    if role == "AXButton" && title.contains("日志") {
        AXUIElementPerformAction(child, kAXPressAction as CFString)
    }
}
```

### Pattern 3: Enumerate All Interactive Elements
```swift
func dumpRoles(_ el: AXUIElement, depth: Int = 0) {
    let role = getStringAttr(el, kAXRoleAttribute)
    let title = getStringAttr(el, kAXTitleAttribute)
    let prefix = String(repeating: "  ", count: depth)
    if role.contains("AX") {
        print("\(prefix)\(role) | \(title)")
    }
    for child in getChildren(el) {
        dumpRoles(child, depth: depth + 1)
    }
}
```

### Pattern 4: Click in HiSecEndpoint / EDRClient Windows
The HiSecEndpointAgent and EDRClient windows have shallow AX trees (1-2 levels of children). Use the app name to locate:
```swift
let appName = "HiSecEndpoint"  // or "HiSecEndpointAgent"
guard let app = NSWorkspace.shared.runningApplications.first(where: {
    ($0.localizedName ?? "").contains(appName)
}) else { exit(1) }
```

---

## Complete End-to-End Workflow (from agent side)

```python
import subprocess

# 1. Identify app name from running apps
r = subprocess.run([
    'sshpass', '-p', 'PASSWORD', 'ssh', '-p', '22',
    'user@host', 'osascript -e \'tell application "System Events" to get name of every process\''
], capture_output=True, text=True)
print(r.stdout)

# 2. Write Swift script to /tmp/
swift_code = """
import Cocoa
import ApplicationServices
// ... full script ...
"""

# 3. Copy and execute
subprocess.run(['sshpass', '-p', 'PASSWORD', 'scp', '-P', '22',
               '/tmp/click.swift', 'user@host:/tmp/click.swift'])
r = subprocess.run([
    'sshpass', '-p', 'PASSWORD', 'ssh', '-p', '22', 'user@host',
    'EDR_WD_ALLOW_REAL_CLICKS=1 swift /tmp/click.swift'
], capture_output=True, text=True)
print(r.stdout, r.stderr)
```

---

## Windows Equivalent (for reference)

On Windows targets, use the MCP tools directly — no Swift needed:

```python
from agent.subagent import TargetSubAgent
agent = TargetSubAgent.from_name("win-dev")
agent.initialize_mcp()

# dump_tree + click by control_id — all via MCP
tree = agent.call_tool("dump_tree", {"max_depth": 5})
result = agent.call_tool("click", {"control_id": "12345"})
```

---

## Key Takeaways

1. **macOS has no pywinauto equivalent in MCP** — the backend only exposes `click_at(x, y)`
2. **Swift + AX API = native element-level automation** — write a script, SCP it to target, run it over SSH
3. **AXPress is preferred** — it's the semantic action (tells the system "press this button")
4. **CGEvent is the fallback** — calculate element center and synthesize mouse events
5. **Use `EDR_WD_ALLOW_REAL_CLICKS=1`** on the target when executing Swift scripts that produce real clicks
6. **The AX tree is shallow for Java/Electron apps** — usually 1-3 levels of children; recursion depth of 5-10 is sufficient
7. **The HiSecEndpointAgent window** has role=`AXWindow`, title=`华为智能终端安全系统` — it's the entry window with the "前往安全防护中心" button
8. **The EDRClient window** has title=`华为HiSec Endpoint` (via CGWindowList) but AX title=`HiSec Endpoint` — the actual UI buttons are inside this window
