#!/usr/bin/env swift
// click_security_center.swift
// Finds "前往安全防护中心" inside the HiSecEndpointAgent window,
// clicks it using the specified method, then exits.
// Usage: click_security_center.swift [--method ax_press|cgevent_center]
// Exits 0 on click success, 1 on not found / click failed.
// Prints "OK <method>" to stdout on success.

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

func findTextInChildren(_ el: AXUIElement, target: String) -> AXUIElement? {
    var childrenRef: CFTypeRef?
    let r = AXUIElementCopyAttributeValue(el, kAXChildrenAttribute as CFString, &childrenRef)
    guard r == .success, let children = childrenRef as? [AXUIElement] else { return nil }
    for child in children {
        var valueRef: CFTypeRef?
        let vr = AXUIElementCopyAttributeValue(child, kAXValueAttribute as CFString, &valueRef)
        if vr == .success, let value = valueRef as? String, value.contains(target) {
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
    guard r == .success else { return nil }
    return parentRef as! AXUIElement
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

switch preferredMethod {
case .ax_press:
    // Explicit: AXPress on parent ONLY — no CGEvent fallback
    if let parentEl = parentOf(el) {
        if tryAXPress(parentEl) {
            clicked = true
            clickMethod = "ax_press"
        }
    }

case .cgevent_center:
    // Explicit: CGEvent center click on the element ONLY — no AXPress
    if clickCenter(el) {
        clicked = true
        clickMethod = "cgevent_center"
    }

case .auto:
    // Auto: try AXPress on parent first; on failure, CGEvent on element
    if let parentEl = parentOf(el) {
        if tryAXPress(parentEl) {
            clicked = true
            clickMethod = "ax_press"
        }
    }
    if !clicked {
        if clickCenter(el) {
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
    print("OK \(clickMethod)")
    exit(0)
} else {
    fputs("ERROR: click failed\n", stderr)
    exit(1)
}
