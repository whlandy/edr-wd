"""枚举所有窗口，包括其他 Window Station"""
import ctypes
from ctypes import wintypes
import sys

user32 = ctypes.windll.user32

# 先尝试获取桌面列表
def get_desktops():
    """获取所有桌面"""
    desktops = []
    try:
        # DesktopObjectId doesn't exist in older ctypes, try alternative
        pass
    except:
        pass
    return desktops

# 枚举所有进程的所有窗口
def get_all_process_windows():
    """尝试获取所有可见进程的主窗口"""
    import subprocess
    result = subprocess.run(['tasklist', '/v', '/fo', 'csv', '/nh'], 
                          capture_output=True, text=True, encoding='utf-8', errors='ignore')
    return result.stdout

print("=== All processes with windows ===")
print(get_all_process_windows())

print("\n=== All desktops (Win32) ===")
try:
    import win32job, win32process, win32api, pywinauto
    print("pywinauto available")
except:
    print("no pywinauto")

# 尝试用 PyGetWindow 或直接 win32gui
print("\n=== Win32gui EnumWindows ===")
try:
    import win32gui
    windows = []
    def cb(hwnd, extra):
        if win32gui.IsWindowVisible(hwnd):
            title = win32gui.GetWindowText(hwnd)
            cls = win32gui.GetClassName(hwnd)
            windows.append((hwnd, cls, title))
    win32gui.EnumWindows(cb, None)
    for hwnd, cls, title in windows:
        if title:
            print(f"0x{hwnd:08X} | VIS | {cls[:40]:<40} | {title[:60]}")
except Exception as e:
    print(f"win32gui error: {e}")

print("\n=== Win32gui EnumWindows (all) ===")
try:
    import win32gui
    windows = []
    def cb(hwnd, extra):
        try:
            title = win32gui.GetWindowText(hwnd)
            cls = win32gui.GetClassName(hwnd)
        except:
            title = ""
            cls = ""
        windows.append((hwnd, cls, title, win32gui.IsWindowVisible(hwnd)))
    win32gui.EnumWindows(cb, None)
    for hwnd, cls, title, vis in windows[:200]:
        print(f"0x{hwnd:08X} | {'VIS' if vis else '   '} | {cls[:40]:<40} | {title[:60]}")
except Exception as e:
    print(f"win32gui error: {e}")
