"""通过 IAccessible 接口枚举 Qt 窗口的子控件"""
import ctypes
from ctypes import wintypes
import json

user32 = ctypes.windll.user32
oleacc = ctypes.windll.oleacc

hwnd = 0x000504E4  # HiSec Endpoint

# 获取窗口的 IAccessible 对象
try:
    import comtypes
    from comtypes.gen.UIAutomationCore import IUIAutomation
    from comtypes.gen.Accessibility import IAccessible
    print("comtypes available")
except ImportError:
    print("comtypes not available, trying pure ctypes")

# 用纯 ctypes + oleacc
try:
    acc = ctypes.c_void_p()
    child_id = ctypes.c_long()
    result = oleacc.AccessibleObjectFromWindow(
        hwnd,
        0,  # OBJID_CLIENT
        ctypes.byref(ctypes.c_int()),  # IID
        ctypes.byref(acc)
    )
    print(f"AccessibleObjectFromWindow result: {result}")
    print(f"acc: {acc}")
except Exception as e:
    print(f"Error: {e}")

# 尝试用 AccessibleChildren
try:
    from ctypes import POINTER
    from ctypes.wintypes import HRESULT, LONG, LPOLESTR, OLECHAR

    # IAccessible VTable
    class IDispatch(ctypes.Structure):
        _fields_ = [
            ("lpVtbl", ctypes.c_void_p),
        ]

    class IAccessible(ctypes.Structure):
        _fields_ = [
            ("lpVtbl", ctypes.c_void_p),
        ]

    # AccessibleChildren
    AccessibleChildren = oleacc.AccessibleChildren
    AccessibleChildren.argtypes = [wintypes.LPVOID, LONG, LONG, ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(LONG)]
    AccessibleChildren.restype = wintypes.HRESULT

    acc_obj = ctypes.c_void_p()
    acquired = LONG()
    buf = (wintypes.LPVOID * 100)()

    hr = AccessibleChildren(acc_obj, 0, 100, buf, ctypes.byref(acquired))
    print(f"AccessibleChildren hr: {hr}, acquired: {acquired.value}")
except Exception as e:
    print(f"AccessibleChildren error: {e}")

# 直接用 win32gui + Qt property 尝试
print("\n=== Trying direct Qt approach ===")
try:
    import win32gui
    import win32con

    # Qt 窗口特征：Qt51512QWindowIcon
    # Qt 子窗口可能用特殊的 class name

    # 获取所有子窗口
    children = []
    def enum_cb(hwnd, lparam):
        if hwnd != 0:
            try:
                cls = win32gui.GetClassName(hwnd)
                title = win32gui.GetWindowText(hwnd)
                rect = win32gui.GetWindowRect(hwnd)
                vis = win32gui.IsWindowVisible(hwnd)
                children.append({
                    "hwnd": hex(hwnd),
                    "class": cls,
                    "title": title,
                    "rect": {"x": rect[0], "y": rect[1], "w": rect[2]-rect[0], "h": rect[3]-rect[1]},
                    "visible": vis
                })
            except:
                pass
        return True

    win32gui.EnumChildWindows(hwnd, enum_cb, None)
    print(f"Direct child windows: {len(children)}")
    for c in children:
        print(f"  {c['hwnd']} | {c['class']:<40} | {c['title'][:40]} | vis={c['visible']}")

except Exception as e:
    print(f"win32gui error: {e}")

# 尝试 Spy++ 方式：使用 WM_GETDLGCODE 和特殊消息
print("\n=== Trying Qt-specific messages ===")
try:
    import win32gui
    import win32con
    import win32message

    # Qt 使用 RegisterWindowMessage("QtWinVersion") 等
    WM_QT_GETVERSION = win32gui.RegisterWindowMessage("QtWinVersion")
    print(f"QtWinVersion msg: {WM_QT_GETVERSION}")

    # 尝试发送消息获取 Qt 控件信息
    result = win32gui.SendMessage(hwnd, WM_QT_GETVERSION, 0, 0)
    print(f"QtVersion result: {result}")

except Exception as e:
    print(f"Qt msg error: {e}")
