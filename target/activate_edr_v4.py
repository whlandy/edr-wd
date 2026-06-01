"""
activate_edr_v4.py
点击托盘溢出箭头 → 等待弹出面板 → 枚举面板内容
"""
import time
import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32

# 定义常见常量
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
MK_LBUTTON = 0x0001

def get_chevron_button_hwnd():
    """找到托盘工具栏中的溢出箭头按钮"""
    # 主托盘窗口
    tray_hwnd = user32.FindWindowW("Shell_TrayWnd", None)
    if not tray_hwnd:
        print("[FAIL] 未找到 Shell_TrayWnd")
        return None, None

    # 枚举托盘的子 toolbar
    toolbar_hwnd = None

    def enum_child(hwnd, _):
        nonlocal toolbar_hwnd
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if "ToolbarWindow32" in cls.value:
            # 检查按钮数量
            btn_count = user32.SendMessageW(hwnd, 1024, 0, 0)  # TB_BUTTONCOUNT
            print(f"  [DBG] Toolbar HWND={hwnd}, 按钮数={btn_count}")
            toolbar_hwnd = hwnd
        return True

    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, ctypes.c_void_p)(
        lambda h, _: enum_child(h, None)
    )
    user32.EnumChildWindows(tray_hwnd, enum_proc, None)

    if not toolbar_hwnd:
        print("[FAIL] 未找到 ToolbarWindow32")
        return None, None

    # 获取按钮数量
    btn_count = user32.SendMessageW(toolbar_hwnd, 1024, 0, 0)
    print(f"[DBG] 托盘 toolbar HWND={toolbar_hwnd}, 总按钮数={btn_count}")

    # 找最后一个按钮（溢出箭头）
    for i in range(max(0, btn_count - 1), btn_count):
        rect = ctypes.create_struct(wintypes.RECT)()
        user32.SendMessageW(toolbar_hwnd, 1025, i, ctypes.byref(rect))  # TB_GETITEMRECT
        print(f"  [DBG] 按钮 {i}: rect=({rect.left},{rect.top},{rect.right},{rect.bottom})")

    # 点击最右边那个按钮（通常是"溢出"箭头）
    if btn_count > 0:
        rect = ctypes.create_struct(wintypes.RECT)()
        user32.SendMessageW(toolbar_hwnd, 1025, btn_count - 1, ctypes.byref(rect))  # 最后一个按钮
        x = (rect.left + rect.right) // 2
        y = (rect.top + rect.bottom) // 2
        hwnd_target = toolbar_hwnd
        print(f"[OK] 点击溢出箭头按钮 HWND={hwnd_target} 位置 ({x},{y})")
        return hwnd_target, (x, y)

    return None, None


def click_at(hwnd, x, y):
    """发送鼠标点击消息"""
    user32.SendMessageW(hwnd, WM_LBUTTONDOWN, MK_LBUTTON, (y << 16) | x)
    time.sleep(0.05)
    user32.SendMessageW(hwnd, WM_LBUTTONUP, 0, (y << 16) | x)


def enum_top_windows():
    """枚举顶级窗口"""
    windows = []

    def callback(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        title_len = user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(title_len + 1) if title_len else ctypes.create_unicode_buffer(1)
        if title_len:
            user32.GetWindowTextW(hwnd, title, title_len + 1)
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        windows.append((hwnd, cls.value, title.value))
        return True

    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, ctypes.c_void_p)(
        lambda h, _: callback(h, None)
    )
    user32.EnumWindows(enum_proc, None)
    return windows


def wait_for_overflow_popup(timeout=3):
    """等待溢出窗口弹出，返回其 HWND"""
    start = time.time()
    while time.time() - start < timeout:
        windows = enum_top_windows()
        for hwnd, cls, title in windows:
            if "NotifyIconOverflowWindow" in cls or "Shell_TrayWnd" in cls:
                if user32.IsWindowVisible(hwnd):
                    rect = ctypes.create_struct(wintypes.RECT)()
                    user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    if rect.right - rect.left > 100 and rect.bottom - rect.top > 50:
                        print(f"[OK] 找到可见溢出/托盘面板 HWND={hwnd} cls={cls}")
                        print(f"     位置: ({rect.left},{rect.top})-({rect.right},{rect.bottom})")
                        return hwnd
        time.sleep(0.2)
    return None


def get_all_popup_windows():
    """获取所有弹出窗口（包括溢出面板）"""
    popups = []

    # 方法1：通过 GUI 线程信息获取前景窗口链
    gui_info = ctypes.create_struct(ctypes.Structure)  # placeholder
    class GUITHREADINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("hwndActive", wintypes.HWND),
            ("hwndFocus", wintypes.HWND),
            ("hwndCapture", wintypes.HWND),
            ("hwndMenuOwner", wintypes.HWND),
            ("hwndMoveSize", wintypes.HWND),
            ("hwndCaret", wintypes.HWND),
            ("rcCaret", wintypes.RECT),
        ]

    info = GUITHREADINFO()
    info.cbSize = ctypes.sizeof(GUITHREADINFO)
    if user32.GetGUIThreadInfo(0, ctypes.byref(info)):
        print(f"[DBG] Active={info.hwndActive} Focus={info.hwndFocus}")

    # 方法2：枚举所有可见窗口，找非普通类的
    windows = enum_top_windows()
    for hwnd, cls, title in windows:
        if not user32.IsWindowVisible(hwnd):
            continue
        rect = ctypes.create_struct(wintypes.RECT)()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        # 过滤小窗口和普通窗口
        if w < 200 or h < 100:
            continue
        if cls in ("WorkerW", "Shell_SecondaryTrayWnd", "NotifyIconOverflowWindow", "Windows.UI.Core.CoreWindow"):
            popups.append((hwnd, cls, title, rect.left, rect.top, w, h))
            print(f"  [POPUP] HWND={hwnd} cls={cls} title={title} rect=({rect.left},{rect.top},{rect.right},{rect.bottom})")

    return popups


def dump_overflow_window(hwnd):
    """获取溢出窗口的完整子控件树"""
    print(f"\n[DBG] 抓取 HWND={hwnd} 的控件树:")

    def enum_child(hwnd, indent=0):
        prefix = "  " * indent
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        title_len = user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(title_len + 1) if title_len else ctypes.create_unicode_buffer(1)
        if title_len:
            user32.GetWindowTextW(hwnd, title, title_len + 1)

        visible = user32.IsWindowVisible(hwnd)
        rect = ctypes.create_struct(wintypes.RECT)()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))

        print(f"{prefix}[{'V' if visible else 'H'}] HWND={hwnd} cls={cls.value[:40]} title={title.value[:30]} rect=({rect.left},{rect.top},{rect.right},{rect.bottom})")

        enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, ctypes.c_void_p)(
            lambda h, i=indent: enum_child(h, i + 1)
        )
        user32.EnumChildWindows(hwnd, enum_proc, None)

    enum_child(hwnd)


def main():
    print("=" * 60)
    print("activate_edr_v4: 点击溢出箭头 → 等待弹出 → 枚举控件树")
    print("=" * 60)

    # Step 1: 找到溢出箭头按钮
    chevron_hwnd, (cx, cy) = get_chevron_button_hwnd()
    if not chevron_hwnd:
        print("[FAIL] 找不到溢出箭头按钮")
        return

    # Step 2: 点击溢出箭头
    print(f"\n[Step] 点击溢出箭头...")
    click_at(chevron_hwnd, cx, cy)

    # Step 3: 等一下让窗口展开
    print("[Step] 等待弹出面板出现...")
    time.sleep(0.8)

    # Step 4: 找所有弹出窗口
    print("\n[DBG] 枚举所有弹出窗口:")
    popups = get_all_popup_windows()

    if not popups:
        print("[FAIL] 没有找到任何弹出面板")
        return

    # Step 5: 抓控件树
    for hwnd, cls, title, l, t, w, h in popups:
        dump_overflow_window(hwnd)


if __name__ == "__main__":
    main()
