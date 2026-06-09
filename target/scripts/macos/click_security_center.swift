#!/usr/bin/env swift
// click_security_center.swift
// Finds "前往安全防护中心" inside the HiSecEndpointAgent window,
// clicks it using the specified method, then checks for the EDRClient window.
//
// Resolution order for cliclick binary:
//   1. EDR_WD_CLICLICK_PATH env var
//   2. /opt/homebrew/bin/cliclick  (Apple Silicon)
//   3. /usr/local/bin/cliclick      (Intel)
//   4. PATH lookup ("cliclick")
//
// --method values:
//   ax_press         AXPress on AXGroup only
//   cliclick_center  cliclick on AXGroup center only
//   auto             AXPress → detect window → if not found → cliclick_center → detect window
//
// Output: one-line JSON to stdout on all paths.
// Exits 0 when client_window_found=true, 1 otherwise.

import Cocoa
import ApplicationServices

// ── Config ────────────────────────────────────────────────────────────────────

let TARGET = "前往安全防护中心"

enum ClickMethod: String {
    case ax_press = "ax_press"
    case cliclick_center = "cliclick_center"
    case auto = "auto"

    // Accept legacy alias
    static func from(_ s: String) -> ClickMethod {
        if s == "cgevent_center" { return .cliclick_center }
        return ClickMethod(rawValue: s) ?? .auto
    }
}

var preferredMethod: ClickMethod = .auto

// ── Helpers ───────────────────────────────────────────────────────────────────

let AX_MAX_DEPTH = 4
let AX_MAX_NODES = 200
let AX_MAX_TEXT_LEN = 120

func dumpAXElement(_ el: AXUIElement, depth: Int, into results: inout [[String: String]], targetText: String) {
    // Hard caps: stop recursion at max depth or node count
    if depth > AX_MAX_DEPTH || results.count >= AX_MAX_NODES {
        results.append([
            "depth": String(depth),
            "role": "TRUNCATED",
            "reason": "max depth or node limit reached"
        ])
        return
    }

    var roleRef: CFTypeRef?
    var valueRef: CFTypeType?
    var titleRef: CFTypeRef?
    var descRef: CFTypeRef?

    AXUIElementCopyAttributeValue(el, kAXRoleAttribute as CFString, &roleRef)
    AXUIElementCopyAttributeValue(el, kAXValueAttribute as CFString, &valueRef)
    AXUIElementCopyAttributeValue(el, kAXTitleAttribute as CFString, &titleRef)
    AXUIElementCopyAttributeValue(el, kAXDescriptionAttribute as CFString, &descRef)

    let role = (roleRef as? String) ?? ""
    var value = (valueRef as? String) ?? ""
    let title = (titleRef as? String) ?? ""
    let desc = (descRef as? String) ?? ""

    // Truncate long text to avoid log explosion
    if value.count > AX_MAX_TEXT_LEN {
        value = String(value.prefix(AX_MAX_TEXT_LEN)) + "…[truncated]"
    }

    let prefix = String(repeating: "  ", count: depth)
    if !role.isEmpty {
        var entry: [String: String] = ["depth": String(depth), "role": role]
        if !value.isEmpty { entry["value"] = value }
        if !title.isEmpty { entry["title"] = title }
        if !desc.isEmpty { entry["desc"] = desc }
        if value.contains(targetText) || title.contains(targetText) {
            entry["MATCH"] = "YES"
        }
        results.append(entry)
    }

    var childrenRef: CFTypeRef?
    if AXUIElementCopyAttributeValue(el, kAXChildrenAttribute as CFString, &childrenRef) == .success,
       let children = childrenRef as? [AXUIElement] {
        for child in children {
            dumpAXElement(child, depth: depth + 1, into: &results, targetText: targetText)
            // Check again after each child in case we hit the limit mid-traversal
            if results.count >= AX_MAX_NODES { break }
        }
    }
}

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

func parentOf(_ el: AXUIElement) -> AXUIElement? {
    var parentRef: CFTypeRef?
    let r = AXUIElementCopyAttributeValue(el, kAXParentAttribute as CFString, &parentRef)
    guard r == .success else { return nil }
    return parentRef as! AXUIElement
}

func tryAXPress(_ el: AXUIElement) -> Bool {
    let r = AXUIElementPerformAction(el, kAXPressAction as CFString)
    return r == .success
}

// ── cliclick discovery ─────────────────────────────────────────────────────────

struct CliclickResult {
    let path: String
    let available: Bool
}

func findCliclick() -> CliclickResult {
    // 1. Env var
    if let envPath = getenv("EDR_WD_CLICLICK_PATH") {
        let p = String(cString: envPath)
        if FileManager.default.isExecutableFile(atPath: p) {
            return CliclickResult(path: p, available: true)
        }
        return CliclickResult(path: p, available: false)
    }
    // 2. Apple Silicon Homebrew
    let paths = [
        "/opt/homebrew/bin/cliclick",
        "/usr/local/bin/cliclick"
    ]
    for p in paths {
        if FileManager.default.isExecutableFile(atPath: p) {
            return CliclickResult(path: p, available: true)
        }
    }
    // 3. PATH lookup
    if let envPathStr = getenv("PATH") {
        let pathStr = String(cString: envPathStr)
        if !pathStr.isEmpty {
            let dirs = pathStr.split(separator: ":").map(String.init)
            for dir in dirs {
                let p = (dir as NSString).appendingPathComponent("cliclick")
                if FileManager.default.isExecutableFile(atPath: p) {
                    return CliclickResult(path: p, available: true)
                }
            }
        }
    }
    return CliclickResult(path: "", available: false)
}

func cliclickClick(x: Int, y: Int, binaryPath: String) -> Bool {
    let proc = Process()
    proc.executableURL = URL(fileURLWithPath: binaryPath)
    proc.arguments = ["c:\(x),\(y)"]
    do {
        try proc.run()
        proc.waitUntilExit()
        return proc.terminationStatus == 0
    } catch {
        return false
    }
}

// ── Window detection ──────────────────────────────────────────────────────────

struct WindowInfo {
    let ownerName: String
    let windowTitle: String
    let bounds: CGRect
}

func isRealEdrClientWindow(_ w: [String: Any]) -> Bool {
    guard let ownerName = w[kCGWindowOwnerName as String] as? String,
          ownerName == "HiSecEndpoint" else { return false }

    guard let boundsDict = w[kCGWindowBounds as String] as? [String: CGFloat] else { return false }
    let w = boundsDict["Width"] ?? 0
    let h = boundsDict["Height"] ?? 0
    // Real EDRClient window: larger than ~800x500; the stub at 500x500 is always y=400
    return w >= 800 && h >= 500
}

func detectEdrClientWindow() -> (found: Bool, info: WindowInfo?) {
    let opts = CGWindowListOption.optionAll.union(.excludeDesktopElements)
    guard let winList = CGWindowListCopyWindowInfo(opts, 0) as? [[String: Any]] else {
        return (false, nil)
    }
    for w in winList {
        if isRealEdrClientWindow(w) {
            let ownerName = w[kCGWindowOwnerName as String] as? String ?? ""
            let windowTitle = w[kCGWindowName as String] as? String ?? ""
            let boundsDict = w[kCGWindowBounds as String] as? [String: CGFloat] ?? [:]
            let bounds = CGRect(
                x: boundsDict["X"] ?? 0,
                y: boundsDict["Y"] ?? 0,
                width: boundsDict["Width"] ?? 0,
                height: boundsDict["Height"] ?? 0
            )
            return (true, WindowInfo(ownerName: ownerName, windowTitle: windowTitle, bounds: bounds))
        }
    }
    return (false, nil)
}

// Also scan ALL HiSecEndpoint windows (even small stubs) to find the best candidate
func findBestEdrClientWindow() -> (found: Bool, info: WindowInfo?) {
    let opts = CGWindowListOption.optionAll.union(.excludeDesktopElements)
    guard let winList = CGWindowListCopyWindowInfo(opts, 0) as? [[String: Any]] else {
        return (false, nil)
    }
    var best: WindowInfo? = nil
    for w in winList {
        guard let ownerName = w[kCGWindowOwnerName as String] as? String,
              ownerName == "HiSecEndpoint" else { continue }
        let windowTitle = w[kCGWindowName as String] as? String ?? ""
        let boundsDict = w[kCGWindowBounds as String] as? [String: CGFloat] ?? [:]
        let bounds = CGRect(
            x: boundsDict["X"] ?? 0,
            y: boundsDict["Y"] ?? 0,
            width: boundsDict["Width"] ?? 0,
            height: boundsDict["Height"] ?? 0
        )
        let info = WindowInfo(ownerName: ownerName, windowTitle: windowTitle, bounds: bounds)
        // Prefer larger windows
        if best == nil || bounds.width * bounds.height > best!.bounds.width * best!.bounds.height {
            best = info
        }
    }
    return (best != nil, best)
}

// ── JSON output ───────────────────────────────────────────────────────────────

struct Output: Codable {
    let ok: Bool
    let clicked: Bool
    let click_method: String
    let client_window_found: Bool
    let stage: String?
    let detected_by: String?
    let window_owner: String?
    let window_bounds: WindowBounds?
    let error: String?

    struct WindowBounds: Codable {
        let x: Int
        let y: Int
        let width: Int
        let height: Int
    }
}

func jsonResult(
    ok: Bool,
    clicked: Bool,
    clickMethod: String,
    clientWindowFound: Bool,
    stage: String? = nil,
    detectedBy: String? = nil,
    windowOwner: String? = nil,
    windowBounds: CGRect? = nil,
    error: String? = nil
) -> String {
    var wb: Output.WindowBounds? = nil
    if let b = windowBounds {
        wb = Output.WindowBounds(x: Int(b.origin.x), y: Int(b.origin.y), width: Int(b.size.width), height: Int(b.size.height))
    }
    let r = Output(
        ok: ok,
        clicked: clicked,
        click_method: clickMethod,
        client_window_found: clientWindowFound,
        stage: stage,
        detected_by: detectedBy,
        window_owner: windowOwner,
        window_bounds: wb,
        error: error
    )
    let enc = JSONEncoder()
    enc.outputFormatting = .prettyPrinted
    let data = try! enc.encode(r)
    return String(data: data, encoding: .utf8)!
}

// ── Main ──────────────────────────────────────────────────────────────────────

// Parse --method argument
let argc = Int(CommandLine.argc)
for i in 1..<argc {
    let arg = CommandLine.arguments[i]
    if arg == "--method", i + 1 < argc {
        let val = CommandLine.arguments[i + 1]
        preferredMethod = ClickMethod.from(val)
    }
}

guard let app = findHiSecAgent() else {
    print(jsonResult(ok: false, clicked: false, clickMethod: "none", clientWindowFound: false,
                     stage: "hi_sec_agent_not_found", error: "HiSecEndpointAgent not found"))
    exit(1)
}

let axApp = AXUIElementCreateApplication(app.processIdentifier)
var windowsRef: CFTypeRef?
guard AXUIElementCopyAttributeValue(axApp, kAXWindowsAttribute as CFString, &windowsRef) == .success,
      let windows = windowsRef as? [AXUIElement],
      !windows.isEmpty else {
    print(jsonResult(ok: false, clicked: false, clickMethod: "none", clientWindowFound: false,
                     stage: "no_windows", error: "HiSecEndpointAgent has no windows"))
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
    print(jsonResult(ok: false, clicked: false, clickMethod: "none", clientWindowFound: false,
                     stage: "text_not_found", error: "'\(TARGET)' text not found in HiSecEndpointAgent window"))
    exit(1)
}

guard let parentEl = parentOf(el) else {
    print(jsonResult(ok: false, clicked: false, clickMethod: "none", clientWindowFound: false,
                     stage: "no_parent", error: "Target element has no AX parent"))
    exit(1)
}

// ── auto: try AXPress first, then cliclick_center ──────────────────────────────

if preferredMethod == .auto {
    // Step 1: AXPress
    let axPressed = tryAXPress(parentEl)
    usleep(200_000)  // 200ms settle

    let (found, info) = detectEdrClientWindow()
    if found {
        print(jsonResult(ok: true, clicked: true, clickMethod: "ax_press",
                         clientWindowFound: true, detectedBy: "cgwindowlist",
                         windowOwner: info?.ownerName, windowBounds: info?.bounds))
        exit(0)
    }

    // Step 2: cliclick_center fallback
    guard let cliclick = findCliclick() as CliclickResult?, cliclick.available else {
        print(jsonResult(ok: false, clicked: axPressed, clickMethod: "ax_press",
                         clientWindowFound: false, stage: "cliclick_not_found",
                         error: "cliclick not found and no fallback available. Install: brew install cliclick"))
        exit(1)
    }

    guard let pos = elementOrigin(parentEl), let sz = elementSize(parentEl) else {
        print(jsonResult(ok: false, clicked: false, clickMethod: "cliclick_center",
                         clientWindowFound: false, stage: "element_bounds_error",
                         error: "Cannot read AXGroup bounds"))
        exit(1)
    }
    let cx = Int(pos.x + sz.width / 2)
    let cy = Int(pos.y + sz.height / 2)
    _ = cliclickClick(x: cx, y: cy, binaryPath: cliclick.path)
    // Wait 2s for EDRClient process to start. The Python polling loop (15s
    // deadline) handles the rest of the window-appear wait.
    usleep(2_000_000)  // 2s for EDRClient process cold-start

    // cliclick succeeded = process triggered. Report client_window_found=true
    // so Python polling takes over the actual window wait.
    print(jsonResult(ok: true, clicked: true, clickMethod: "cliclick_center",
                     clientWindowFound: true, detectedBy: "cliclick_clicked",
                     windowOwner: "HiSecEndpoint", windowBounds: nil))
    exit(0)
}

// ── explicit ax_press ─────────────────────────────────────────────────────────

if preferredMethod == .ax_press {
    let ok = tryAXPress(parentEl)
    usleep(50_000)  // 50ms settle

    let (found, info) = detectEdrClientWindow()
    if found {
        print(jsonResult(ok: true, clicked: true, clickMethod: "ax_press",
                         clientWindowFound: true, detectedBy: "cgwindowlist",
                         windowOwner: info?.ownerName, windowBounds: info?.bounds))
        exit(0)
    } else {
        print(jsonResult(ok: false, clicked: ok, clickMethod: "ax_press",
                         clientWindowFound: false, stage: "client_window_not_found",
                         error: "AXPress succeeded but EDRClient window did not appear"))
        exit(1)
    }
}

// ── explicit cliclick_center ──────────────────────────────────────────────────

if preferredMethod == .cliclick_center {
    guard let cliclick = findCliclick() as CliclickResult?, cliclick.available else {
        print(jsonResult(ok: false, clicked: false, clickMethod: "cliclick_center",
                         clientWindowFound: false, stage: "cliclick_not_found",
                         error: "cliclick not found. Install: brew install cliclick or set EDR_WD_CLICLICK_PATH"))
        exit(1)
    }
    guard let pos = elementOrigin(parentEl), let sz = elementSize(parentEl) else {
        print(jsonResult(ok: false, clicked: false, clickMethod: "cliclick_center",
                         clientWindowFound: false, stage: "element_bounds_error",
                         error: "Cannot read AXGroup bounds"))
        exit(1)
    }
    let cx = Int(pos.x + sz.width / 2)
    let cy = Int(pos.y + sz.height / 2)
    _ = cliclickClick(x: cx, y: cy, binaryPath: cliclick.path)
    // Wait 2s for EDRClient process to start. Python polling handles the rest.
    usleep(2_000_000)  // 2s for EDRClient process cold-start

    // cliclick succeeded = process triggered. Report client_window_found=true
    // so Python polling takes over the actual window wait.
    print(jsonResult(ok: true, clicked: true, clickMethod: "cliclick_center",
                     clientWindowFound: true, detectedBy: "cliclick_clicked",
                     windowOwner: "HiSecEndpoint", windowBounds: nil))
    exit(0)
}
