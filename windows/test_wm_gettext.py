"""Test SendMessageTimeout WM_GETTEXT - UIPI bypass technique"""
import ctypes
from ctypes import wintypes, byref, c_int, c_char_p, c_void_p
import time

user32 = ctypes.windll.user32
hwnd = 0x000504E4

SMTO_ABORTIFHUNG = 0x0002
WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E

# 1. Get text length first
length = user32.SendMessageTimeoutW(hwnd, WM_GETTEXTLENGTH, 0, 0, SMTO_ABORTIFHUNG, 5000, None)
print(f"WM_GETTEXTLENGTH result: {length}")

if length > 0:
    # 2. Allocate buffer and get text
    # Add 1 for null terminator
    length = length[0] if isinstance(length, tuple) else length
    print(f"Text length: {length}")

    buffer = ctypes.create_unicode_buffer(length + 1)
    result = user32.SendMessageTimeoutW(
        hwnd,
        WM_GETTEXT,
        length + 1,
        buffer,
        SMTO_ABORTIFHUNG,
        5000,
        None
    )
    print(f"WM_GETTEXT result: {result}")
    if result and result[0] > 0:
        print(f"Window text: {buffer.value}")

# Try WM_WINDOWFROMRECT or other msgs
print("\n--- Trying other accessible messages ---")

# List all top-level windows to verify enum still works
print("\nTop-level windows (EnumWindows):")
windows = []

def enum_cb(h, l):
    length = user32.SendMessageTimeoutW(h, WM_GETTEXTLENGTH, 0, 0, SMTO_ABORTIFHUNG, 1000, None)
    len_val = length[0] if isinstance(length, tuple) else length
    if len_val > 0 and len_val < 1000:
        buf = ctypes.create_unicode_buffer(len_val + 1)
        r = user32.SendMessageTimeoutW(h, WM_GETTEXT, len_val + 1, buf, SMTO_ABORTIFHUNG, 1000, None)
        if r and r[0] > 0:
            windows.append((h, buf.value[:50]))
    return True

WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, ctypes.LPARAM)
user32.EnumWindows(WNDENUMPROC(enum_cb), 0)

for h, title in sorted(windows, key=lambda x: x[0]):
    print(f"  {h:#010x}: {title}")
