import ctypes
import time
import win32gui
import win32con
import win32api

user32 = ctypes.windll.user32

SW_RESTORE = 9
TB_BUTTONCOUNT = 0x0411
TB_GETBUTTONTEXTW = 0x042D
TB_PRESSBUTTON = 0x0407

# 1. 枚举所有窗口
windows = []
def enum_cb(hwnd, _):
    if True:  # 包括隐藏窗口
        title = win32gui.GetWindowText(hwnd)
        cls = win32gui.GetClassName(hwnd)
        windows.append((hwnd, title, cls))
    return True

win32gui.EnumWindows(enum_cb, None)

# 2. 找 EDR 主窗口
edr_hwnd = None
for hwnd, title, cls in windows:
    if any(kw in title.lower() for kw in ["hisec", "edr", "华为", "endpoint"]):
        edr_hwnd = hwnd
        print(f"[OK] 找到 EDR 窗口: HWND={hwnd}, title={repr(title)}, class={cls}")
        break

# 3. 如果找到了，直接激活
if edr_hwnd:
    user32.ShowWindow(edr_hwnd, SW_RESTORE)
    time.sleep(0.3)
    user32.SetForegroundWindow(edr_hwnd)
    print(f"[OK] 已激活 HWND={edr_hwnd}")
else:
    print("[INFO] 未找到标准 EDR 窗口，尝试点击系统托盘...")

    # 4. 找系统托盘 ToolbarWindow32
    tray_hwnd = None
    for hwnd, title, cls in windows:
        if "Shell_TrayWnd" in cls or "NotifyIconOverflowWindow" in cls:
            tray_hwnd = hwnd
            break

    if tray_hwnd:
        print(f"[DBG] 托盘 HWND={tray_hwnd}")

        childs = []
        def tray_enum(h, _):
            childs.append(h)
            return True
        win32gui.EnumChildWindows(tray_hwnd, tray_enum, None)

        for child in childs:
            cls = win32gui.GetClassName(child)
            if "ToolbarWindow32" in cls:
                count = user32.SendMessage(child, TB_BUTTONCOUNT, 0, 0)
                print(f"[DBG] Toolbar HWND={child}, 按钮数={count}")
                for i in range(count):
                    btn_buf = ctypes.create_unicode_buffer(256)
                    ret = user32.SendMessageW(child, TB_GETBUTTONTEXTW, i, ctypes.byref(btn_buf))
                    text = btn_buf.value
                    if text and any(kw in text.lower() for kw in ["hisec", "edr", "华为", "endpoint", "安全"]):
                        user32.SendMessage(child, TB_PRESSBUTTON, i, 1)
                        time.sleep(0.8)
                        print(f"[OK] 点击了托盘按钮: {repr(text)}")

                        # 点击后重新枚举，找刚弹出的 popup
                        new_windows = []
                        def new_enum(h, _):
                            title = win32gui.GetWindowText(h)
                            cls = win32gui.GetClassName(h)
                            new_windows.append((h, title, cls))
                            return True
                        win32gui.EnumWindows(new_enum, None)

                        for pop_hwnd, pop_title, pop_cls in new_windows:
                            if any(kw in pop_title for kw in ["hisec", "华为", "安全", "endpoint"]):
                                print(f"[OK] 找到弹出窗口: HWND={pop_hwnd}, title={repr(pop_title)}")
                                user32.ShowWindow(pop_hwnd, SW_RESTORE)
                                time.sleep(0.2)
                                user32.SetForegroundWindow(pop_hwnd)
                                print(f"[OK] 已激活弹出窗口")
                                break
                        break
    else:
        print("[FAIL] 未找到系统托盘窗口")
