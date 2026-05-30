"""Test IAccessible on HiSec Endpoint Qt window via MCP"""
import json
import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32
oleacc = ctypes.windll.oleacc

hwnd = 0x000504E4  # HiSec Endpoint Qt window

results = []

def walk_accessible(parent_hwnd, acc, depth=0, max_depth=15):
    """Walk IAccessible tree recursively"""
    if depth > max_depth:
        return

    try:
        # Get accessible name and role
        name_buf = ctypes.create_unicode_buffer(256)
        role_buf = ctypes.create_unicode_buffer(256)

        try:
            name_len = user32.AccessibleObjectFromWindow(
                parent_hwnd, 0, None, ctypes.byref(ctypes.c_int())
            )
        except:
            name_len = -1

        # Try AccessibleChildren
        try:
            import comtypes
            from comtypes.gen.Accessibility import IAccessible
        except ImportError:
            results.append({"depth": depth, "error": "comtypes not available"})
            return

        results.append({"depth": depth, "parent_hwnd": hex(parent_hwnd), "status": "walked"})

    except Exception as e:
        results.append({"depth": depth, "error": str(e)})

# Simple test: try to get IAccessible from window handle
try:
    import comtypes
    from comtypes.client import CoCreateInstance
    from comtypes.gen.Accessibility import IAccessible
    from comtypes import COMObject

    acc = CoCreateInstance(
        comtypes.GUID("{618736E0-3C3D-11CF-810C-00AA00389B71}"),  # IAccessible CLSID
        interface=IAccessible
    )
    results.append({"test": "comtypes IAccessible", "ok": True})
except Exception as e:
    results.append({"test": "comtypes IAccessible", "error": str(e)})

# Try pure ctypes approach
try:
    acc = ctypes.c_void_p()
    hr = oleacc.AccessibleObjectFromWindow(
        hwnd,
        0,  # OBJID_CLIENT
        None,
        ctypes.byref(acc)
    )
    results.append({"test": "AccessibleObjectFromWindow", "hr": hr, "acc": hex(acc.value) if acc.value else None})
except Exception as e:
    results.append({"test": "AccessibleObjectFromWindow", "error": str(e)})

# Check if comtypes is available
try:
    import comtypes
    results.append({"comtypes": "available", "version": getattr(comtypes, '__version__', 'unknown')})
except ImportError:
    results.append({"comtypes": "NOT available"})

# Check if uiautomation is available
try:
    import uiautomation as uia
    results.append({"uiautomation": "available"})
except ImportError:
    results.append({"uiautomation": "NOT available"})

# Check pywinauto version and backends
try:
    import pywinauto
    results.append({"pywinauto": "available", "version": pywinauto.__version__})
except ImportError:
    results.append({"pywinauto": "NOT available"})

print(json.dumps(results, ensure_ascii=False, indent=2))
