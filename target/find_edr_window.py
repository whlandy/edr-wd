"""
find_edr_window.py
枚举所有窗口，找到 EDR 相关窗口的准确标题
"""
from pywinauto import Application, Desktop
import time

def find_all_windows():
    """用 UIA backend 枚举所有窗口"""
    app = Application(backend='uia')
    all_windows = []

    # 枚举桌面上的所有窗口
    for wnd in Desktop(backend='uia').windows():
        try:
            title = wnd.window_text()
            cls = wnd.class_name()
            visible = wnd.is_visible()
            enabled = wnd.is_enabled()
            all_windows.append({
                'title': title,
                'class': cls,
                'visible': visible,
                'enabled': enabled
            })
        except Exception as e:
            pass

    return all_windows

def main():
    print("=" * 60)
    print("查找 EDR 窗口")
    print("=" * 60)

    # 等一下让窗口完全显示
    time.sleep(2)

    windows = find_all_windows()
    print(f"\n找到 {len(windows)} 个窗口:\n")

    for w in windows:
        if w['visible'] and w['enabled'] and w['title']:
            print(f"  [{'V' if w['visible'] else 'H'}] title={w['title'][:60]}")
            print(f"       class={w['class'][:60]}")

    # 特别检查包含华为/Hisec/Endpoint/安全的
    print("\n--- EDR 相关窗口 ---")
    keywords = ['华为', 'Hisec', 'Endpoint', '安全', 'EDR', 'HiSec']
    for w in windows:
        for kw in keywords:
            if kw.lower() in w['title'].lower() or kw in w['title']:
                print(f"  ** MATCH ** title={w['title']} class={w['class']}")
                break

if __name__ == "__main__":
    main()
