"""
activate_edr_v2.py — 用 pywinauto 点击系统托盘图标唤出 EDR 窗口
避免直接发 TB_* 原始消息（会崩 explorer）
"""
import time
import win32gui
import win32con
from pywinauto import Application, Desktop

# 用 pywinauto 的 Application 连接到 system tray
# pywinauto 可以用铜鼓 tray 图标的窗口类名找到它

print("[INFO] 尝试通过 pywinauto 找到托盘图标...")

# 方法：通过 tray 区域找 NotifyIcon 窗口
# Windows 托盘里的图标实际上由 Shell_TrayWnd 管理
# 用 pywinauto 的 Desktop(backend='uia') 枚举

d = Desktop(backend="uia")

# 找所有窗口
print("[DBG] 枚举所有窗口...")
all_windows = d.windows()
print(f"[DBG] 共找到 {len(all_windows)} 个窗口")

for w in all_windows:
    title = w.window_text()
    cls = w.element_info.class_name
    pid = w.element_info.process_id
    try:
        visible = w.is_visible()
    except Exception:
        visible = "?"
    if visible or title:
        print(f"  HWND={w.handle} | class={cls} | title={repr(title)} | PID={pid} | visible={visible}")

# 尝试找系统托盘区域
tray_windows = []
for w in all_windows:
    cls = w.element_info.class_name
    if "Tray" in cls or "Shell" in cls or "Notify" in cls:
        tray_windows.append(w)
        print(f"[FOUND TRAY] HWND={w.handle} class={cls}")

# 尝试用 Application("explorer").connect() 连到 explorer 进程
print("\n[INFO] 尝试连接到 explorer.exe...")
try:
    app = Application(backend="uia").connect(process_name="explorer.exe", timeout=5)
    print(f"[OK] 连接到 explorer, process={app.process}")
except Exception as e:
    print(f"[WARN] 连接 explorer 失败: {e}")
