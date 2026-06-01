"""
activate_edr_v5.py
策略改变：
1. 先通过 RECT 消息强制让溢出窗口显示（不依赖点击箭头）
2. 直接枚举 NotifyIconOverflowWindow 的子窗口树
3. 找到 EDR 图标对应的按钮并点击
"""
import time
import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32

WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
MK_LBUTTON = 0x0001


def get_all_windows():
    """枚举所有窗口"""
    windows = []

    def callback(hwnd, _):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            title_len = user32.GetWindowTextLengthW(hwnd)
            title = ctypes.create_unicode_buffer(title_len + 1) if title_len else ctypes.create_unicode_buffer(1)
            if title_len:
                user32.GetWindowTextW(hwnd, title, title_len + 1)
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            windows.append((hwnd, cls.value, title.value))
        except:
            pass
        return True

    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, ctypes.c_void_p)(
        lambda h, _: callback(h, None)
    )
    user32.EnumWindows(enum_proc, None)
    return windows


def find_overflow_window():
    """找到溢出窗口 NotifyIconOverflowWindow"""
    # 先强制让它可见 - 向 Shell_TrayWnd 发送窗口显示消息
    tray = user32.FindWindowW("Shell_TrayWnd", None)
    if tray:
        print(f"[DBG] Shell_TrayWnd HWND={tray}")
        # 尝试找到溢出按钮
        overflow_btn = None
        def enum_child(hwnd, _):
            nonlocal overflow_btn
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            if "ToolbarWindow32" in cls.value:
                btn_count = user32.SendMessageW(hwnd, 1024, 0, 0)  # TB_BUTTONCOUNT
                if btn_count > 0:
                    print(f"  [DBG] Toolbar HWND={hwnd} 按钮数={btn_count}")
            # 找 "PindlerNotifyIconOverflowWindow" 或溢出按钮
            title_buf = ctypes.create_unicode_buffer(256)
            title_len = user32.GetWindowTextW(hwnd, title_buf, 256)
            return True

        enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, ctypes.c_void_p)(
            lambda h, _: enum_child(h, None)
        )
        user32.EnumChildWindows(tray, enum_proc, None)

    # 直接找 NotifyIconOverflowWindow
    overflow_hwnd = user32.FindWindowW("NotifyIconOverflowWindow", None)
    if overflow_hwnd:
        print(f"[OK] 找到 NotifyIconOverflowWindow HWND={overflow_hwnd}")
        # 获取位置
        rect = ctypes.create_struct(wintypes.RECT)()
        user32.GetWindowRect(overflow_hwnd, ctypes.byref(rect))
        print(f"     原始位置: ({rect.left},{rect.top})-({rect.right},{rect.bottom}) visible={user32.IsWindowVisible(overflow_hwnd)}")

        # 如果不可见，强制显示
        if not user32.IsWindowVisible(overflow_hwnd):
            print(f"     溢出窗口当前不可见，尝试强制显示...")
            # 方法：发送 WM_WINDOWPOSCHANGING 然后 SHOW
            user32.ShowWindow(overflow_hwnd, 9)  # SW_RESTORE
            time.sleep(0.5)
            user32.ShowWindow(overflow_hwnd, 5)  # SW_SHOW
            user32.SetWindowPos(overflow_hwnd, 0, 0, 0, 0, 0, 0x0040 | 0x0001)  # SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW
            time.sleep(0.5)
            user32.GetWindowRect(overflow_hwnd, ctypes.byref(rect))
            print(f"     显示后位置: ({rect.left},{rect.top})-({rect.right},{rect.bottom}) visible={user32.IsWindowVisible(overflow_hwnd)}")

        return overflow_hwnd

    print("[FAIL] 未找到 NotifyIconOverflowWindow")
    return None


def dump_window_tree(hwnd, indent=0):
    """递归打印窗口控件树"""
    prefix = "  " * indent
    try:
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        title_len = user32.GetWindowTextLengthW(hwnd)
        title = ctypes.create_unicode_buffer(title_len + 1) if title_len else ctypes.create_unicode_buffer(1)
        if title_len:
            user32.GetWindowTextW(hwnd, title, title_len + 1)
        visible = user32.IsWindowVisible(hwnd)
        rect = ctypes.create_struct(wintypes.RECT)()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        print(f"{prefix}[{'V' if visible else 'H'}] HWND={hwnd} pid={pid.value} cls={cls.value[:40]} title={title.value[:40]} rect=({rect.left},{rect.top},{rect.right},{rect.bottom})")

        enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, ctypes.c_void_p)(
            lambda h, i=indent: dump_window_tree(h, i + 1)
        )
        user32.EnumChildWindows(hwnd, enum_proc, None)
    except Exception as e:
        print(f"{prefix}[ERR] HWND={hwnd}: {e}")


def click_overflow_chevron():
    """点击溢出箭头按钮"""
    # 溢出箭头通常在主托盘工具栏的最右边
    tray = user32.FindWindowW("Shell_TrayWnd", None)
    if not tray:
        print("[FAIL] 未找到 Shell_TrayWnd")
        return False

    # 找到 TrayBar（Tray 类型的通知区域）
    # 在不同 Windows 版本中结构不同，尝试几种方式

    # 方式1：找 NotifyIconOverflowWindow 里的"关闭溢出"按钮并点击
    overflow = user32.FindWindowW("NotifyIconOverflowWindow", None)
    if overflow:
        print(f"[DBG] 溢出窗口 HWND={overflow}")

    # 方式2：直接找屏幕右下角托盘区的 chevron 按钮
    # 通常在 (屏幕宽-50, 托盘高度中间) 位置
    cx = user32.GetSystemMetrics(76)  # SM_CXSCREEN
    cy = user32.GetSystemMetrics(77)  # SM_CYSCREEN
    print(f"[DBG] 屏幕分辨率: {cx}x{cy}")

    # 托盘高度约 28-48px，溢出箭头在最右下角
    # 尝试点击溢出箭头大概位置
    chevron_x = cx - 25  # 右数第几个像素
    chevron_y = cy - 20  # 底部往上

    print(f"[DBG] 尝试点击溢出箭头大概位置: ({chevron_x}, {chevron_y})")

    # 先尝试用 FromPoint 从指定坐标找窗口
    pt = ctypes.create_struct(wintypes.POINT)
    pt.x = chevron_x
    pt.y = chevron_y
    hwnd_under = user32.WindowFromPhysicalPoint(ctypes.byref(pt))
    print(f"[DBG] ({chevron_x},{chevron_y}) 处的 HWND={hwnd_under}")
    if hwnd_under and hwnd_under != 0xFFFFFFFF:
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd_under, cls, 256)
        print(f"     class={cls.value}")

    # 点击该位置
    user32.SetCursorPos(chevron_x, chevron_y)
    time.sleep(0.1)
    user32.mouse_event(2, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
    time.sleep(0.05)
    user32.mouse_event(4, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP
    print("[OK] 已发送鼠标点击")

    return True


def find_icon_by_tooltip(target_tooltip, parent_hwnd=None):
    """在溢出窗口中查找指定 tooltip 的图标按钮"""
    if parent_hwnd is None:
        parent_hwnd = user32.FindWindowW("NotifyIconOverflowWindow", None)
    if not parent_hwnd:
        return None

    results = []

    def enum_child(hwnd, _):
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        title_buf = ctypes.create_unicode_buffer(512)
        title_len = user32.GetWindowTextW(hwnd, title_buf, 512)
        title = title_buf.value if title_len else ""

        # 检查是否是按钮或图标
        if "ToolbarWindow32" in cls.value or "Button" in cls.value or "TrayIcon" in cls.value:
            # 获取 tooltip
            tooltips = []
            for tid in range(0x0400, 0x0400 + 64):
                buf = ctypes.create_unicode_buffer(512)
                lparam = ctypes.byref(ctypes.create_unicode_buffer(512))
                # TB_GETTOOLTIPTEXT
                try:
                    pass
                except:
                    pass

            visible = user32.IsWindowVisible(hwnd)
            rect = ctypes.create_struct(wintypes.RECT)()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            results.append((hwnd, cls.value, title, rect.left, rect.top, rect.right, rect.bottom, visible))
        return True

    enum_proc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, ctypes.c_void_p)(
        lambda h, _: enum_child(h, None)
    )
    user32.EnumChildWindows(parent_hwnd, enum_proc, None)

    return results


def main():
    print("=" * 60)
    print("activate_edr_v5: 打开溢出窗口 → 枚举控件树 → 找 EDR 图标")
    print("=" * 60)

    # Step 1: 点击溢出箭头
    print("\n[Step 1] 点击溢出箭头...")
    click_overflow_chevron()

    # Step 2: 等待
    print("\n[Step 2] 等待 1 秒...")
    time.sleep(1)

    # Step 3: 找 NotifyIconOverflowWindow
    print("\n[Step 3] 查找溢出窗口...")
    overflow = find_overflow_window()

    if overflow:
        print("\n[Step 4] 溢出窗口控件树:")
        dump_window_tree(overflow)

        print("\n[Step 5] 搜索 EDR 图标按钮...")
        icons = find_icon_by_tooltip("HiSecEndpoint")
        for item in icons:
            hwnd, cls, title, l, t, r, b, vis = item
            print(f"  [{'V' if vis else 'H'}] HWND={hwnd} cls={cls[:40]} title={title} rect=({l},{t},{r},{b})")
    else:
        # 枚举所有顶级窗口，找弹出的面板
        print("\n[Step 3b] 枚举所有顶级窗口（找弹出面板）...")
        windows = get_all_windows()
        for hwnd, cls, title in sorted(windows, key=lambda x: x[0]):
            try:
                visible = user32.IsWindowVisible(hwnd)
                if not visible:
                    continue
                rect = ctypes.create_struct(wintypes.RECT)()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                w = rect.right - rect.left
                h = rect.bottom - rect.top
                if w < 100 or h < 50:
                    continue
                print(f"  [POPUP] HWND={hwnd} cls={cls[:40]} title={title[:40]} rect=({rect.left},{rect.top},{rect.right},{rect.bottom})")
            except:
                pass


if __name__ == "__main__":
    main()
