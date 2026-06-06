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
import sys


_REGISTRY: dict[str, str] = {
    # name -> module path (relative to target/ runtime root)
    # server.py is deployed inside target/ and runs with target/ as CWD,
    # so 'automation' is on the path but 'target.automation' is not.
    "windows_pywinauto":   "automation.windows_pywinauto",
    "macos_accessibility": "automation.macos_accessibility",
}

# class name inside the module
_CLASS_NAME: dict[str, str] = {
    "windows_pywinauto":   "WindowsPywinautoBackend",
    "macos_accessibility": "MacOSAccessibilityBackend",
}


class UnsupportedBackendError(ValueError):
    """Raised when the requested backend name is not registered."""
    pass


_PLATFORM_DEFAULT: dict[str, str] = {
    "darwin":              "macos_accessibility",
    "win32":               "windows_pywinauto",
}


def create_backend(name: str | None = None) -> "object":
    """
    Construct a backend instance.

    Resolution order:
      1. `name` argument (explicit)
      2. EDR_WD_AUTOMATION_BACKEND env var
      3. platform-specific default (darwin → macos_accessibility,
         win32 → windows_pywinauto)
    """
    import importlib

    if name is None:
        name = os.environ.get("EDR_WD_AUTOMATION_BACKEND")
        _source = "env"
        if name is None:
            name = _PLATFORM_DEFAULT.get(sys.platform)
            _source = "platform"
            if name is None:
                raise UnsupportedBackendError(
                    f"No automation backend for platform '{sys.platform}' and "
                    f"EDR_WD_AUTOMATION_BACKEND is not set. "
                    f"Set EDR_WD_AUTOMATION_BACKEND=macos_accessibility (macOS) "
                    f"or EDR_WD_AUTOMATION_BACKEND=windows_pywinauto (Windows)."
                )
    else:
        _source = "explicit"

    if name not in _REGISTRY:
        raise UnsupportedBackendError(
            f"automation backend '{name}' is not registered. "
            f"Known: {sorted(_REGISTRY)}"
        )
    module = importlib.import_module(_REGISTRY[name])
    cls = getattr(module, _CLASS_NAME[name])
    return cls()


__all__ = ["create_backend", "UnsupportedBackendError"]
