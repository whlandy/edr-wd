"""
activate_edr_v3.py — 专门处理 Windows 11/10 托盘溢出区（隐藏图标）
"""
import time
import ctypes
from ctypes import wintypes
import win32gui
import win32con

user32 = ctypes.windll.user32

# 先定义 SendMessageW 签名
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageW.restype = wintypes.LPARAM

TB_BUTTONCOUNT = 0x0411
TB_GETBUTTONTEXTW = 0x042D
TB_PRESSBUTTON = 0x0407
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202

# 1. 枚举所有顶级窗口
windows = []
def enum_top(hwnd, _):
    title = win32gui.GetWindowText(hwnd)
    cls = win32gui.GetClassName(hwnd)
    visible = user32.IsWindowVisible(hwnd)
    windows.append((hwnd, title, cls, visible))
    return True

win32gui.EnumWindows(enum_top, None)
print(f"[DBG] 顶级窗口数: {len(windows)}")
for hwnd, title, cls, visible in windows:
    print(f"  HWND={hwnd} | visible={visible} | class={cls} | title={repr(title)}")

# 2. 找 "隐藏图标" 溢出窗口
overflow_hwnd = None
for hwnd, title, cls, visible in windows:
    if "NotifyIconOverflowWindow" in cls:
        overflow_hwnd = hwnd
        print(f"\n[OK] 找到溢出窗口: HWND={hwnd}, title={repr(title)}, visible={visible}")
        break

# 3. 找通知区域（主托盘里的通知图标工具栏）
tray_hwnd = None
for hwnd, title, cls, visible in windows:
    if cls == "Shell_TrayWnd":
        tray_hwnd = hwnd
        print(f"\n[OK] 找到主托盘: HWND={hwnd}")
        break

# 4. 遍历两个地方的 toolbar，找 EDR 图标按钮
found_icon = False

def find_edr_button(parent_hwnd, parent_name):
    global found_icon
    children = []
    def enum_child(h, _):
        children.append(h)
        return True
    win32gui.EnumChildWindows(parent_hwnd, enum_child, None)

    for child in children:
        try:
            cls = win32gui.GetClassName(child)
        except Exception:
            continue
        if "ToolbarWindow32" not in cls:
            continue
        try:
            count = user32.SendMessageW(child, TB_BUTTONCOUNT, 0, 0)
        except Exception:
            continue
        print(f"  [{parent_name}] Toolbar HWND={child}, 按钮数={count}")
        for i in range(count):
            btn_buf = ctypes.create_unicode_buffer(256)
            try:
                user32.SendMessageW(child, TB_GETBUTTONTEXTW, i, ctypes.addressof(btn_buf))
            except Exception:
                continue
            text = btn_buf.value
            if not text:
                continue
            print(f"    按钮 {i}: {repr(text)}")
            if any(kw in text.lower() for kw in ["hisecendpointagent", "hisec", "华为"]):
                print(f"  [OK!] 找到 EDR 图标按钮: {repr(text)}, 索引={i}")
                # 用 TB_PRESSBUTTON 点击
                user32.SendMessageW(child, TB_PRESSBUTTON, i, 1)
                time.sleep(1)
                found_icon = True
                return

if overflow_hwnd:
    find_edr_button(overflow_hwnd, "Overflow")

if not found_icon and tray_hwnd:
    find_edr_button(tray_hwnd, "Tray")

if not found_icon:
    print("[FAIL] 未找到 EDR 托盘图标")

# 5. 点击后等待新窗口出现
if found_icon:
    time.sleep(1)
    print("\n[INFO] 重新枚举窗口，检查 EDR 窗口...")
    new_windows = []
    def enum_new(hwnd, _):
        title = win32gui.GetWindowText(hwnd)
        cls = win32gui.GetClassName(hwnd)
        visible = user32.IsWindowVisible(hwnd)
        new_windows.append((hwnd, title, cls, visible))
        return True
    win32gui.EnumWindows(enum_new, None)
    for hwnd, title, cls, visible in new_windows:
        if any(kw in title.lower() for kw in ["hisec", "华为", "安全"]):
            print(f"  [OK] 找到 EDR 窗口: HWND={hwnd}, title={repr(title)}")
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            user32.InvalidateRect(hwnd, None, True)
            user32.UpdateWindow(hwnd)
            print(f"  [OK] 已激活")
