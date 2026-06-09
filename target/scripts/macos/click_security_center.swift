#!/usr/bin/env swift
// click_security_center.swift
// Finds "前往安全防护中心" inside the HiSecEndpointAgent window,
// clicks it using the specified method, then exits.
// Usage: click_security_center.swift [--method ax_press|cgevent_center]
// Exits 0 on click success, 1 on not found / click failed.
// Prints structured JSON to stdout on success/failure.

import Cocoa
import ApplicationServices

// ── Arguments ────────────────────────────────────────────────────────────────

enum ClickMethod: String {
    case ax_press = "ax_press"
    case cgevent_center = "cgevent_center"
    case auto = "auto"        // try both, succeed on first that works
    case ax_query = "ax_query" // just check if EDRClient window exists
}

let TARGET = "前往安全防护中心"
var preferredMethod: ClickMethod = .auto

// ── Helpers ───────────────────────────────────────────────────────────────────

func findHiSecAgent() -> NSRunningApplication? {
    NSWorkspace.shared.runningApplications.first(where: { $0.localizedName == "HiSecEndpointAgent" })
}

func axStringAttribute(_ el: AXUIElement, _ attr: CFString) -> String? {
    var valueRef: CFTypeRef?
    let r = AXUIElementCopyAttributeValue(el, attr, &valueRef)
    guard r == .success, let value = valueRef as? String else { return nil }
    return value
}

func matchesTargetText(_ el: AXUIElement, target: String) -> Bool {
    let attrs: [CFString] = [
        kAXValueAttribute as CFString,
        kAXTitleAttribute as CFString,
        kAXDescriptionAttribute as CFString,
        kAXHelpAttribute as CFString,
        kAXRoleDescriptionAttribute as CFString,
    ]
    for attr in attrs {
        if let value = axStringAttribute(el, attr), value.contains(target) {
            return true
        }
    }
    return false
}

func findTextInChildren(_ el: AXUIElement, target: String) -> AXUIElement? {
    if matchesTargetText(el, target: target) {
        return el
    }
    var childrenRef: CFTypeRef?
    let r = AXUIElementCopyAttributeValue(el, kAXChildrenAttribute as CFString, &childrenRef)
    guard r == .success, let children = childrenRef as? [AXUIElement] else { return nil }
    for child in children {
        if matchesTargetText(child, target: target) {
            return child
        }
        if let found = findTextInChildren(child, target: target) {
            return found
        }
    }
    return nil
}

func elementOrigin(_ el: AXUIElement) -> CGPoint? {
    var posRef: CFTypeRef?
    let r = AXUIElementCopyAttributeValue(el, kAXPositionAttribute as CFString, &posRef)
    guard r == .success, let posRef = posRef else { return nil }
    var pos = CGPoint.zero
    guard AXValueGetValue(posRef as! AXValue, .cgPoint, &pos) else { return nil }
    return pos
}

func elementSize(_ el: AXUIElement) -> CGSize? {
    var sizeRef: CFTypeRef?
    let r = AXUIElementCopyAttributeValue(el, kAXSizeAttribute as CFString, &sizeRef)
    guard r == .success, let sizeRef = sizeRef else { return nil }
    var size = CGSize.zero
    guard AXValueGetValue(sizeRef as! AXValue, .cgSize, &size) else { return nil }
    return size
}

func clickCenter(_ el: AXUIElement) -> Bool {
    guard let pos = elementOrigin(el), let sz = elementSize(el) else { return false }
    let cx = pos.x + sz.width / 2
    let cy = pos.y + sz.height / 2
    let down = CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown,
                       mouseCursorPosition: CGPoint(x: cx, y: cy), mouseButton: .left)!
    let up   = CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp,
                       mouseCursorPosition: CGPoint(x: cx, y: cy), mouseButton: .left)!
    down.post(tap: .cghidEventTap)
    up.post(tap: .cghidEventTap)
    return true
}

func tryAXPress(_ el: AXUIElement) -> Bool {
    let r = AXUIElementPerformAction(el, kAXPressAction as CFString)
    return r == .success
}

func parentOf(_ el: AXUIElement) -> AXUIElement? {
    var parentRef: CFTypeRef?
    let r = AXUIElementCopyAttributeValue(el, kAXParentAttribute as CFString, &parentRef)
    guard r == .success, let p = parentRef else { return nil }
    // parentRef is always an AXUIElement when the call succeeds
    return p as! AXUIElement
}

func pressableAncestor(_ el: AXUIElement) -> AXUIElement? {
    var current: AXUIElement? = el
    for _ in 0..<6 {
        guard let node = current else { break }
        var actionsRef: CFArray?
        let actionResult = AXUIElementCopyActionNames(node, &actionsRef)
        if actionResult == .success,
           let actions = actionsRef as? [CFString],
           actions.contains(where: { $0 as String == kAXPressAction as String }) {
            return node
        }
        current = parentOf(node)
    }
    return nil
}

func rectFromElement(_ el: AXUIElement) -> (CGPoint, CGSize)? {
    var posRef: CFTypeRef?
    var sizeRef: CFTypeRef?
    guard AXUIElementCopyAttributeValue(el, kAXPositionAttribute as CFString, &posRef) == .success,
          AXUIElementCopyAttributeValue(el, kAXSizeAttribute as CFString, &sizeRef) == .success,
          CFGetTypeID(posRef) == AXValueGetTypeID(),
          CFGetTypeID(sizeRef) == AXValueGetTypeID() else { return nil }
    var pos = CGPoint.zero
    var size = CGSize.zero
    guard AXValueGetValue(posRef! as! AXValue, .cgPoint, &pos),
          AXValueGetValue(sizeRef! as! AXValue, .cgSize, &size) else { return nil }
    return (pos, size)
}

func clickElement(_ el: AXUIElement, method: ClickMethod) -> Bool {
    switch method {
    case .ax_press:
        if let pressable = pressableAncestor(el) {
            return AXUIElementPerformAction(pressable, kAXPressAction as CFString) == .success
        }
        return false
    case .cgevent_center:
        if let (pos, size) = rectFromElement(el) {
            let cx = pos.x + size.width / 2
            let cy = pos.y + size.height / 2
            let down = CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown,
                               mouseCursorPosition: CGPoint(x: cx, y: cy), mouseButton: .left)
            let up = CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp,
                             mouseCursorPosition: CGPoint(x: cx, y: cy), mouseButton: .left)
            down?.post(tap: .cghidEventTap)
            up?.post(tap: .cghidEventTap)
            return true
        }
        return false
    case .auto:
        if clickElement(el, method: .ax_press) {
            return true
        }
        return clickElement(el, method: .cgevent_center)
    case .ax_query:
        return false
    }
}

func jsonEscape(_ s: String) -> String {
    if let data = try? JSONSerialization.data(withJSONObject: [s], options: []),
       let json = String(data: data, encoding: .utf8) {
        return String(json.dropFirst().dropLast())
    }
    return s.replacingOccurrences(of: "\"", with: "\\\"")
}

func printJSON(_ fields: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: fields, options: [.sortedKeys]),
       let text = String(data: data, encoding: .utf8) {
        print(text)
    } else {
        let items = fields.map { key, value in
            let valueText: String
            if let str = value as? String {
                valueText = "\"\(jsonEscape(str))\""
            } else {
                valueText = "\(value)"
            }
            return "\"\(jsonEscape(key))\":\(valueText)"
        }.joined(separator: ",")
        print("{\(items)}")
    }
}

// ── Main ──────────────────────────────────────────────────────────────────────

// Parse --method argument
for i in 1..<Int(CommandLine.argc) {
    let arg = CommandLine.arguments[i]
    if arg == "--method", i + 1 < Int(CommandLine.argc) {
        let val = CommandLine.arguments[i + 1]
        if let m = ClickMethod(rawValue: val) {
            preferredMethod = m
        }
    }
}

guard let app = findHiSecAgent() else {
    fputs("ERROR: HiSecEndpointAgent not found\n", stderr)
    exit(1)
}

let axApp = AXUIElementCreateApplication(app.processIdentifier)

var windowsRef: CFTypeRef?
guard AXUIElementCopyAttributeValue(axApp, kAXWindowsAttribute as CFString, &windowsRef) == .success,
      let windows = windowsRef as? [AXUIElement],
      !windows.isEmpty else {
    fputs("ERROR: No windows found\n", stderr)
    exit(1)
}

var targetElement: AXUIElement?
for win in windows {
    if let found = findTextInChildren(win, target: TARGET) {
        targetElement = found
        break
    }
}

guard let el = targetElement else {
    fputs("ERROR: '\(TARGET)' text not found\n", stderr)
    exit(1)
}

var clicked = false
var clickMethod = "unknown"
var helperFound = false
var windowBounds: [String: Any] = [:]

switch preferredMethod {
case .ax_press:
    // Explicit: AXPress on parent ONLY — no CGEvent fallback
    if clickElement(el, method: .ax_press) {
        clicked = true
        clickMethod = "ax_press"
    }

case .cgevent_center:
    // Explicit: CGEvent center click on the element ONLY — no AXPress
    if clickElement(el, method: .cgevent_center) {
        clicked = true
        clickMethod = "cgevent_center"
    }

case .auto:
    // Auto: try AXPress on parent first; on failure, CGEvent on element
    if clickElement(el, method: .ax_press) {
        clicked = true
        clickMethod = "ax_press"
    }
    if !clicked {
        if clickElement(el, method: .cgevent_center) {
            clicked = true
            clickMethod = "cgevent_center"
        }
    }

case .ax_query:
    // Check if EDRClient window exists and has a relevant title.
    // 1. Iterate AX windows of EDRClient/HiSecEndpoint processes.
    // 2. Read kAXTitleAttribute; if it contains "华为HiSec Endpoint", "HiSec", or
    //    "Endpoint" → strong success → EDRCLIENT_FOUND.
    // 3. If windows exist but no title matched → EDRCLIENT_FOUND_WITHOUT_TITLE.
    // 4. If no windows at all → error.
    let targetAppNames = ["EDRClient", "HiSecEndpoint"]
    let titleKeywords = ["华为HiSec Endpoint", "HiSec", "Endpoint"]

    for runningApp in NSWorkspace.shared.runningApplications {
        let name = runningApp.localizedName ?? ""
        if targetAppNames.contains(where: { name.contains($0) }) {
            let axApp = AXUIElementCreateApplication(runningApp.processIdentifier)
            var windowsRef: CFTypeRef?
            if AXUIElementCopyAttributeValue(axApp, kAXWindowsAttribute as CFString, &windowsRef) == .success,
               let windows = windowsRef as? [AXUIElement] {
                if windows.isEmpty {
                    continue
                }
                // Check each window for a matching title
                for win in windows {
                    var titleRef: CFTypeRef?
                    if AXUIElementCopyAttributeValue(win, kAXTitleAttribute as CFString, &titleRef) == .success,
                       let title = titleRef as? String {
                        if titleKeywords.contains(where: { title.contains($0) }) {
                            print("EDRCLIENT_FOUND")
                            exit(0)
                        }
                    }
                }
                // Windows exist but none had a matching title — weak success
                print("EDRCLIENT_FOUND_WITHOUT_TITLE")
                exit(0)
            }
        }
    }
    fputs("ERROR: EDRClient window not found\n", stderr)
    exit(1)
}

if clicked {
    // Best-effort detect the client window after the click.  This is useful for
    // the Python caller, but the click itself is the primary success criterion.
    for runningApp in NSWorkspace.shared.runningApplications {
        let name = runningApp.localizedName ?? ""
        if name.contains("EDRClient") || name.contains("HiSecEndpoint") {
            let axApp = AXUIElementCreateApplication(runningApp.processIdentifier)
            var windowsRef: CFTypeRef?
            if AXUIElementCopyAttributeValue(axApp, kAXWindowsAttribute as CFString, &windowsRef) == .success,
               let windows = windowsRef as? [AXUIElement], !windows.isEmpty {
                helperFound = true
                if let win = windows.first, let rect = rectFromElement(win) {
                    windowBounds = [
                        "x": rect.0.x,
                        "y": rect.0.y,
                        "w": rect.1.width,
                        "h": rect.1.height,
                    ]
                }
                break
            }
        }
    }
    printJSON([
        "ok": true,
        "click_method": clickMethod,
        "client_window_found": helperFound,
        "window_bounds": windowBounds,
        "error": "",
    ])
    exit(0)
} else {
    printJSON([
        "ok": false,
        "click_method": clickMethod,
        "client_window_found": false,
        "window_bounds": windowBounds,
        "error": "click failed",
    ])
    exit(1)
}
