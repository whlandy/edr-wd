"""Dump HiSec Endpoint window tree"""
import json
import win32gui
import win32con

hwnd = 0x000504E4

def dump_tree(hwnd, depth=0, max_depth=20):
    if depth > max_depth:
        return []
    results = []
    try:
        title = win32gui.GetWindowText(hwnd)
    except:
        title = ""
    try:
        cls = win32gui.GetClassName(hwnd)
    except:
        cls = ""
    try:
        rect = win32gui.GetWindowRect(hwnd)
        rect_dict = {"x": rect[0], "y": rect[1], "w": rect[2]-rect[0], "h": rect[3]-rect[1]}
    except:
        rect_dict = {}
    try:
        is_vis = win32gui.IsWindowVisible(hwnd)
    except:
        is_vis = False
    try:
        is_en = win32gui.IsWindowEnabled(hwnd)
    except:
        is_en = False
    try:
        cid = win32gui.GetDlgCtrlID(hwnd)
    except:
        cid = None
    try:
        pid = win32gui.GetWindowThreadProcessId(hwnd)[1]
    except:
        pid = None

    results.append({
        "hwnd": hex(hwnd),
        "class_name": cls,
        "text": title,
        "control_id": cid,
        "rectangle": rect_dict,
        "is_visible": is_vis,
        "is_enabled": is_en,
        "pid": pid,
        "depth": depth
    })

    # Enum child windows
    def enum_child(hwnd, lparam):
        if hwnd != 0:
            results.extend(dump_tree(hwnd, depth+1, max_depth))
        return True
    try:
        win32gui.EnumChildWindows(hwnd, enum_child, None)
    except:
        pass

    return results

tree = dump_tree(hwnd)
print(f"Total controls: {len(tree)}")
for ctrl in tree:
    indent = "  " * ctrl["depth"]
    print(f"{indent}[{ctrl['control_id']:>6}] {ctrl['class_name']:<40} | {ctrl['text'][:50]}")
