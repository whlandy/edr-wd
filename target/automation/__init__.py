"""
automation — Platform-agnostic GUI automation backend registry.

Each backend implements the AutomationBackend protocol defined in base.py.
The default backend is selected by EDR_WD_AUTOMATION_BACKEND env var
(e.g. 'windows_pywinauto', 'macos_accessibility'); fallback for
unrecognized values is 'windows_pywinauto' since the original server was
Windows-only.

Public surface:
    create_backend(name: str | None = None) -> AutomationBackend

Adding a new backend:
  1. Create target/automation/<platform>_<tech>.py with a class
     implementing the AutomationBackend methods.
  2. Register it in _REGISTRY below.
"""

from __future__ import annotations

import os


_REGISTRY: dict[str, str] = {
    # name -> module path
    "windows_pywinauto":   "target.automation.windows_pywinauto",
    "macos_accessibility": "target.automation.macos_accessibility",
}

# class name inside the module
_CLASS_NAME: dict[str, str] = {
    "windows_pywinauto":   "WindowsPywinautoBackend",
    "macos_accessibility": "MacOSAccessibilityBackend",
}


class UnsupportedBackendError(ValueError):
    """Raised when the requested backend name is not registered."""
    pass


def create_backend(name: str | None = None) -> "object":
    """
    Construct a backend instance.

    Resolution order:
      1. `name` argument (explicit)
      2. EDR_WD_AUTOMATION_BACKEND env var
      3. 'windows_pywinauto' (legacy default)
    """
    import importlib
    selected = name or os.environ.get("EDR_WD_AUTOMATION_BACKEND", "windows_pywinauto")
    if selected not in _REGISTRY:
        raise UnsupportedBackendError(
            f"automation backend '{selected}' is not registered. "
            f"Known: {sorted(_REGISTRY)}"
        )
    module = importlib.import_module(_REGISTRY[selected])
    cls = getattr(module, _CLASS_NAME[selected])
    return cls()


__all__ = ["create_backend", "UnsupportedBackendError"]
