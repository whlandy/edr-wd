"""Test Windows UIAutomation via ctypes for Qt app"""
import ctypes
from ctypes import wintypes, byref, POINTER, c_int
from ctypes.com import GUID
import time

# UIAutomation CLSIDs and IIDs
CLSID_CUIAutomation = GUID('{ff48dba4-60ef-4211-8169-a17a0d9792ce}')
IID_IUIAutomation = GUID('{30cbe57d-d9d0-4521-850b-f6c042e9ac0b}')

# IUIAutability interface
class IUIAutomation(ctypes.Interface):
    _iid_ = GUID('{30cbe57d-d9d0-4521-850b-f6c042e9ac0b}')
    _methods_ = [
        # ElementFromHandle
        ctypes.WINFUNCTYPE(c_int, wintypes.HANDLE, POINTER(c_int))(),
    ]

hwnd = 0x000504E4  # HiSec Endpoint

ole32 = ctypes.windll.ole32
user32 = ctypes.windll.user32

# CoInitialize
ole32.CoInitialize(None)

# Create UIAutomation
pUA = ctypes.c_void_p()
hr = ole32.CoCreateInstance(
    byref(CLSID_CUIAutomation),
    None,
    1,  # CLSCTX_INPROC_SERVER
    byref(IID_IUIAutomation),
    byref(pUA)
)
print(f"CoCreateInstance IUIAutomation: hr={hr}, pUA={pUA.value}")

if hr == 0 and pUA.value:
    # Try ElementFromHandle
    pElement = ctypes.c_void_p()
    # We'll call via vtable since we don't have full interface def
    vtable = ctypes.cast(pUA.value, POINTER(ctypes.POINTER(ctypes.c_void_p))).contents.value

    # ElementFromHandle is method 3 (index 3, 0-based)
    # Actually let's just try to call via ctypes properly
    # The IUIAutomation interface has ElementFromHandle as method #3

    print(f"UIAutomation created, vtable at {vtable:#x}")

    # Try raw vtable call for ElementFromHandle (method 3, 0-indexed = offset 3*8)
    # Actually methods start after IUnknown (3 methods), so ElementFromHandle = offset 3*8 = 24
    elem_from_hwnd = ctypes.cast(vtable + 24, ctypes.c_void_p.value.__class__)
    print(f"ElementFromHandle function ptr: {elem_from_hwnd:#x}")

    # Actually let's just try to QI for IUIAutomationElement
    IID_IUIAutomationElement = GUID('{d8f608c2-1582-4f7c-86ee-a1d6c9db1a40}')

    # Since we don't have full IID defs, let's try a simpler approach
    # Use UIAutomation via OLEACC if available
    try:
        import comtypes
        from comtypes.client import CreateObject, GetActiveObject

        # Try creating UIAutomation via comtypes
        uia = CreateObject('{ff48dba4-60ef-4211-8169-a17a0d9792ce}', interface=None)
        print(f"comtypes UIA object: {uia}")

        # Get IUIAutomation interface
        from comtypes import GUID
        uia_iu = uia.QueryInterface(GUID('{30cbe57d-d9d0-4521-850b-f6c042e9ac0b}'))
        print(f"Got IUIAutomation: {uia_iu}")

        # ElementFromHandle
        elem = uia_iu.ElementFromHandle(hwnd)
        print(f"ElementFromHandle({hwnd:#x}): {elem}")

        if elem:
            # Get properties
            name = elem.CurrentName
            print(f"Name: {name}")

            # Get children
            children = elem.GetCurrentPatternAs(10000)  # Noesis - actually just try raw
            print(f"Children: {children}")

    except Exception as e:
        print(f"comtypes approach error: {e}")
        import traceback
        traceback.print_exc()
else:
    print("Failed to create UIAutomation")

ole32.CoUninitialize()
