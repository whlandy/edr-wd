"""Test Qt window structure from inside MCP session"""
import json
import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32
oleacc = ctypes.windll.oleacc

hwnd = 0x000504E4

results = {}

# 1. Test: try AccessibleObjectFromWindow with different OBJID
for objid in [0, -4, -2, -3, -1, -5, -6]:
    try:
        acc = ctypes.c_void_p()
        hr = oleacc.AccessibleObjectFromWindow(
            hwnd,
            objid,
            None,
            ctypes.byref(acc)
        )
        results[f"objid_{objid}"] = {"hr": hr, "acc": hex(acc.value) if acc.value else None}
    except Exception as e:
        results[f"objid_{objid}"] = {"error": str(e)}

# 2. Test: try GetGUIThreadInfo
try:
    gui_info = wintypes.GUITHREADINFO()
    gui_info.cbSize = ctypes.sizeof(gui_info)
    # Get thread ID from window
    thread_id = user32.GetWindowThreadProcessId(hwnd, None)
    ok = user32.GetGUIThreadInfo(thread_id, ctypes.byref(gui_info))
    results["gui_thread_info"] = {
        "ok": bool(ok),
        "hwndFocus": hex(gui_info.hwndFocus) if gui_info.hwndFocus else None,
        "hwndActive": hex(gui_info.hwndActive) if gui_info.hwndActive else None,
        "hwndCapture": hex(gui_info.hwndCapture) if gui_info.hwndCapture else None,
        "hwndMenuOwner": hex(gui_info.hwndMenuOwner) if gui_info.hwndMenuOwner else None,
        "hwndMoveSize": hex(gui_info.hwndMoveSize) if gui_info.hwndMoveSize else None,
        "hwndCaret": hex(gui_info.hwndCaret) if gui_info.hwndCaret else None,
    }
except Exception as e:
    results["gui_thread_info"] = {"error": str(e)}

# 3. Test: WindowFromDC - can we get any child DC info
try:
    dc = user32.GetDC(hwnd)
    results["get_dc"] = {"hwnd": hex(hwnd), "dc": hex(dc) if dc else None}
    if dc:
        user32.ReleaseDC(hwnd, dc)
except Exception as e:
    results["get_dc"] = {"error": str(e)}

# 4. Test: try IDispatch and IAccessible COM via comtypes
try:
    import comtypes
    from comtypes import IUnknown, GUID
    from comtypes.gen.Accessibility import IAccessible

    # Try to get the accessible object
    acc = oleacc.AccessibleObjectFromWindow(
        hwnd,
        0,  # OBJID_WINDOW
        comtypes.byref(GUID()),  # IID
        0
    )
    results["comtypes_accessible"] = {"ok": True, "acc": hex(acc) if acc else None}
except Exception as e:
    results["comtypes_accessible"] = {"error": str(e)}

# 5. Check Qt version via QtAircraft - try sending Qt-specific messages
try:
    import win32gui
    WM_QT_GETVERSION = win32gui.RegisterWindowMessage("QtWinVersion")
    result = win32gui.SendMessage(hwnd, WM_QT_GETVERSION, 0, 0)
    results["qt_version_msg"] = {"msg": WM_QT_GETVERSION, "result": result}
except Exception as e:
    results["qt_version_msg"] = {"error": str(e)}

# 6. Check if spyxx tool is available (for Qt class names)
try:
    import subprocess
    r = subprocess.run(['where', 'spyxx'], capture_output=True, text=True)
    results["spyxx"] = r.stdout.strip() or "not found"
except:
    results["spyxx"] = "error"

# 7. Try EnumProps/PropEnumProc - Qt sometimes stores object names in window properties
try:
    props = []
    def prop_enum(hwnd, name, data):
        try:
            props.append({"hwnd": hex(hwnd), "name": str(name), "data": str(data)})
        except:
            pass
        return True
    PropProc = wintypes.PROPENUMPROCA(prop_enum)
    user32.EnumPropsW(hwnd, PropProc)
    results["window_props"] = props[:20]  # limit
except Exception as e:
    results["window_props"] = {"error": str(e)}

# 8. Try to use UI Automation directly
try:
    import comtypes
    from comtypes.gen.UIAutomationCore import IUIAutomation
    # Create IUIAutomation
    ui_automation_clsid = comtypes.GUID("{30c90757-4ef1-410d-b25f-f1d89240194c}")
    # CoCreateInstance
    ui = comtypes.CoCreateInstance(ui_automation_clsid, interface=IUIAutomation)
    results["uiautomation"] = {"ok": True, "ui": str(ui)}
except Exception as e:
    results["uiautomation"] = {"error": str(e)}

print(json.dumps(results, ensure_ascii=False, indent=2))
