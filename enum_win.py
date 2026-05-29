"""枚举所有桌面窗口（不依赖 session）"""
import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32

windows = []

def enum_callback(hwnd, lparam):
    windows.append(hwnd)
    return True

EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)(enum_callback)
user32.EnumWindows(EnumWindowsProc, 0)

for hwnd in windows[:100]:
    length = user32.GetWindowTextLengthW(hwnd)
    if length > 0:
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        cls_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls_buf, 256)
        cls = cls_buf.value
        is_visible = user32.IsWindowVisible(hwnd)
        print(f"0x{hwnd:08X} | {'VIS' if is_visible else '   '} | {cls[:40]:<40} | {title[:60]}")
