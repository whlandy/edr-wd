"""
pywinauto_client.py — pywinauto 封装，提供控件级 GUI 操作

EDR (HiSecEndpoint) Windows 自动化核心客户端。
通过 pywinauto 连接 Windows 窗口，枚举控件树，按 control_id 执行操作。
"""

from __future__ import annotations

import base64
import io
import logging
import time
from typing import Optional

from pywinauto import Application, timings
from pywinauto import mouse

logger = logging.getLogger("edr_wd.pywinauto_client")


class WindowsGUI:
    """Windows GUI 自动化客户端（pywinauto 封装）"""

    def __init__(self, backend: str = "uia"):
        self.backend = backend
        self.app: Optional[Application] = None
        self.main_window = None

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect_by_title(self, title_re: str, timeout: float = 10.0) -> dict:
        """通过窗口标题模糊匹配连接应用"""
        try:
            self.app = Application(backend=self.backend).connect(
                title_re=title_re, timeout=timeout
            )
            self.main_window = self.app.window(title_re=title_re)
            self.main_window.wait("visible", timeout=timeout)
            return {"ok": True, "title": self.main_window.window_text()}
        except Exception as e:
            logger.exception("connect_by_title failed")
            return {"ok": False, "error": str(e)}

    def connect_by_process(self, process_name: str, timeout: float = 10.0) -> dict:
        """通过进程名连接应用"""
        try:
            self.app = Application(backend=self.backend).connect(
                process_name=process_name, timeout=timeout
            )
            self.main_window = self.app.windows()[0]
            return {"ok": True, "process": process_name}
        except Exception as e:
            logger.exception("connect_by_process failed")
            return {"ok": False, "error": str(e)}

    def connect_by_pid(self, pid: int) -> dict:
        """通过 PID 连接"""
        try:
            self.app = Application(backend=self.backend).connect(process=pid)
            self.main_window = self.app.window()
            return {"ok": True, "pid": pid}
        except Exception as e:
            logger.exception("connect_by_pid failed")
            return {"ok": False, "error": str(e)}

    # ------------------------------------------------------------------
    # Dump tree
    # ------------------------------------------------------------------

    def dump_tree(self, window_title_re: str = None, max_depth: int = 15) -> dict:
        """
        导出控件树。

        返回结构:
        {
          "ok": True,
          "title": "窗口标题",
          "controls": [
            {
              "class_name": "Button",
              "text": "确定",
              "control_id": 12345,
              "rectangle": {"x": 0, "y": 0, "w": 100, "h": 30},
              "is_visible": True,
              "is_enabled": True,
              "depth": 0
            },
            ...
          ]
        }
        """
        try:
            if window_title_re and self.app:
                win = self.app.window(title_re=window_title_re)
            elif self.main_window:
                win = self.main_window
            else:
                return {"ok": False, "error": "No window connected"}

            tree = self._build_tree(win, depth=0, max_depth=max_depth)
            return {
                "ok": True,
                "title": win.window_text(),
                "controls": tree
            }
        except Exception as e:
            logger.exception("dump_tree failed")
            return {"ok": False, "error": str(e)}

    def _build_tree(self, ctrl, depth: int = 0, max_depth: int = 15) -> list:
        """递归构建控件树（带深度限制防止卡死）"""
        if depth > max_depth:
            return []

        results = []
        try:
            try:
                text = ctrl.window_text()
            except Exception:
                text = ""
            try:
                cid = ctrl.control_id()
            except Exception:
                cid = None
            try:
                cls = ctrl.friendly_class_name()
            except Exception:
                cls = ""
            try:
                rect = ctrl.rectangle()
                rect_dict = {"x": rect.left, "y": rect.top, "w": rect.width(), "h": rect.height()}
            except Exception:
                rect_dict = {}
            try:
                is_visible = ctrl.is_visible()
            except Exception:
                is_visible = False
            try:
                is_enabled = ctrl.is_enabled()
            except Exception:
                is_enabled = False
            # UIA-specific fields (may not exist on win32 backend)
            automation_id = ""
            control_type = ""
            try:
                automation_id = ctrl.automation_id()
            except Exception:
                pass
            try:
                control_type = str(ctrl.control_type())
            except Exception:
                pass

            results.append({
                "class_name": cls,
                "text": text,
                "control_id": cid,
                "rectangle": rect_dict,
                "is_visible": is_visible,
                "is_enabled": is_enabled,
                "depth": depth,
                "automation_id": automation_id,
                "control_type": control_type,
            })
        except Exception:
            pass

        # 递归子控件（限制子控件数量避免卡死）
        try:
            children = ctrl.children()
            for child in children[:200]:
                results.extend(self._build_tree(child, depth + 1, max_depth))
        except Exception:
            pass

        return results

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def click(self, control_id: int = None, text: str = None,
              class_name: str = None, parent_text: str = None,
              automation_id: str = None) -> dict:
        """点击控件（按 control_id、automation_id、文本或 class_name）"""
        try:
            ctrl = self._find_control(control_id, text, class_name, parent_text, automation_id)
            if not ctrl:
                return {"ok": False, "error": "Control not found"}

            ctrl.click_input()
            time.sleep(0.1)
            return {"ok": True, "method": "click_input", "control_id": control_id}
        except Exception as e:
            logger.exception("click failed")
            return {"ok": False, "error": str(e)}

    def click_target(self, control_id: int = None, text: str = None,
                     class_name: str = None, parent_text: str = None,
                     automation_id: str = None, x_offset: int = 0,
                     y_offset: int = 0) -> dict:
        """
        Click the center of a matched control's screen rectangle.

        This is useful for label-like Qt/UIA controls such as QLabel/Static
        where invoke/click-by-title can report success without triggering the
        UI's mouse handler.
        """
        try:
            ctrl = self._find_control(control_id, text, class_name, parent_text, automation_id)
            if not ctrl:
                return {"ok": False, "error": "Control not found"}

            rect = ctrl.rectangle()
            x = int(rect.left + rect.width() / 2 + x_offset)
            y = int(rect.top + rect.height() / 2 + y_offset)
            mouse.click(button="left", coords=(x, y))
            time.sleep(0.1)
            return {
                "ok": True,
                "method": "mouse.click",
                "x": x,
                "y": y,
                "rectangle": {"x": rect.left, "y": rect.top, "w": rect.width(), "h": rect.height()},
            }
        except Exception as e:
            logger.exception("click_target failed")
            return {"ok": False, "error": str(e)}

    def click_at(self, x: int, y: int) -> dict:
        """Click absolute screen coordinates."""
        try:
            mouse.click(button="left", coords=(int(x), int(y)))
            time.sleep(0.1)
            return {"ok": True, "method": "mouse.click", "x": int(x), "y": int(y)}
        except Exception as e:
            logger.exception("click_at failed")
            return {"ok": False, "error": str(e)}

    def double_click(self, control_id: int = None, text: str = None,
                     class_name: str = None) -> dict:
        """双击控件"""
        try:
            ctrl = self._find_control(control_id, text, class_name)
            if not ctrl:
                return {"ok": False, "error": "Control not found"}
            ctrl.double_click_input()
            return {"ok": True, "method": "double_click_input"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def type_text(self, control_id: int = None, text: str = None,
                   class_name: str = None, string: str = "") -> dict:
        """向控件输入文本"""
        try:
            ctrl = self._find_control(control_id, text, class_name)
            if not ctrl:
                return {"ok": False, "error": "Control not found"}
            ctrl.set_edit_text(string)
            return {"ok": True, "method": "set_edit_text", "text": string}
        except Exception as e:
            # Fallback: click first then type_keys
            try:
                ctrl.click_input()
                ctrl.type_keys(string, with_spaces=True)
                return {"ok": True, "method": "type_keys", "text": string}
            except Exception as e2:
                return {"ok": False, "error": f"set_edit_text: {e}, type_keys: {e2}"}

    def select(self, control_id: int = None, text: str = None,
                class_name: str = None, item: str = None, index: int = None) -> dict:
        """下拉框选择"""
        try:
            ctrl = self._find_control(control_id, text, class_name)
            if not ctrl:
                return {"ok": False, "error": "Control not found"}

            if item:
                ctrl.select(item)
            elif index is not None:
                ctrl.select(index)
            else:
                return {"ok": False, "error": "Must specify item or index"}

            return {"ok": True, "method": "select", "item": item or index}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_text(self, control_id: int = None, text: str = None,
                  class_name: str = None) -> dict:
        """读取控件文本"""
        try:
            ctrl = self._find_control(control_id, text, class_name)
            if not ctrl:
                return {"ok": False, "error": "Control not found"}
            content = ctrl.window_text()
            return {"ok": True, "text": content}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def screenshot(self, path: str = None) -> dict:
        """
        截图整个窗口。
        返回 base64 PNG 或保存到文件。
        """
        try:
            if self.main_window:
                win = self.main_window
            else:
                return {"ok": False, "error": "No window connected"}

            img = win.capture_as_image()
            if img is None:
                return {"ok": False, "error": "capture_as_image returned None"}

            if path:
                img.save(path)
                return {"ok": True, "saved_to": path}

            buf = io.BytesIO()
            img.save(buf, format="PNG")
            b64 = base64.b64encode(buf.getvalue()).decode()
            return {"ok": True, "image_b64": b64, "width": img.width, "height": img.height}
        except Exception as e:
            logger.exception("screenshot failed")
            return {"ok": False, "error": str(e)}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _find_control(self, control_id=None, text=None, class_name=None, parent_text=None,
                      automation_id=None):
        """通过 control_id / automation_id / text / class_name 查找控件"""
        try:
            if self.app is None:
                return None

            if parent_text:
                parent = self.app.window(title_re=parent_text)
            elif self.main_window:
                parent = self.main_window
            else:
                return None

            if control_id is not None:
                return parent.child_window(control_id=control_id)

            if automation_id:
                try:
                    return parent.child_window(auto_id=automation_id)
                except Exception:
                    pass

                for ctrl in parent.descendants():
                    try:
                        if ctrl.automation_id() == automation_id:
                            return ctrl
                    except Exception:
                        pass

            if text:
                try:
                    return parent.child_window(title_re=text, class_name=class_name)
                except Exception:
                    pass

                for ctrl in parent.descendants():
                    try:
                        if text in ctrl.window_text():
                            if class_name and ctrl.friendly_class_name() != class_name:
                                continue
                            return ctrl
                    except Exception:
                        pass
                return None

            if class_name:
                return parent.child_window(class_name=class_name)

            return None
        except Exception:
            return None
