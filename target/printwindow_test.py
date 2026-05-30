"""Pure Win32 screenshot via PrintWindow - bypasses UIPI"""
import ctypes
from ctypes import wintypes
from PIL import Image
import io

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

hwnd = 0x000504E4  # HiSec Endpoint

# Get window rect
rect = wintypes.RECT()
user32.GetWindowRect(hwnd, ctypes.byref(rect))
width = rect.right - rect.left
height = rect.bottom - rect.top
print(f"Window rect: {rect.left},{rect.top} {width}x{height}")

# Create compatible DC and bitmap
hdc = user32.GetDC(0)
memdc = gdi32.CreateCompatibleDC(hdc)
bmp = gdi32.CreateCompatibleBitmap(hdc, width, height)
gdi32.SelectObject(memdc, bmp)

# PrintWindow - captures the window including non-client areas
PW_RENDERFULLCONTENT = 2
result = user32.PrintWindow(hwnd, memdc, PW_RENDERFULLCONTENT)
print(f"PrintWindow result (full): {result}")

if result == 0:
    # Try without full content flag
    result = user32.PrintWindow(hwnd, memdc, 0)
    print(f"PrintWindow result (standard): {result}")

# Also try with screen DC
memdc2 = gdi32.CreateCompatibleDC(hdc)
bmp2 = gdi32.CreateCompatibleBitmap(hdc, width, height)
gdi32.SelectObject(memdc2, bmp2)

# Try PrintWindow with screen DC
result2 = user32.PrintWindow(hwnd, memdc2, 0)
print(f"PrintWindow with screen DC: {result2}")

user32.ReleaseDC(0, hdc)

# Read pixels from bitmap
if result or result2:
    bmp_used = bmp if result else bmp2
    bm_info = gdi32.GetGdiObjectW(bmp_used)
    # GetDIBits - get bitmap data
    bmi = wintypes.BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(wintypes.BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = width
    bmi.bmiHeader.biHeight = -height  # top-down
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0  # BI_RGB

    pixels = (wintypes.DWORD * (width * height))()
    bits_ptr = ctypes.cast(pixels, ctypes.POINTER(wintypes.c_char))
    scan_result = gdi32.GetDIBits(
        memdc if result else memdc2,
        bmp_used,
        0,
        height,
        bits_ptr,
        ctypes.byref(bmi),
        0  # DIB_RGB_COLORS
    )
    print(f"GetDIBits result: {scan_result}")

    if scan_result:
        # Convert to PIL Image (BGRA -> RGBA)
        img_data = bytearray(width * height * 4)
        for i in range(width * height):
            offset = i * 4
            img_data[offset] = pixels[i] & 0xFF  # B
            img_data[offset+1] = (pixels[i] >> 8) & 0xFF  # G
            img_data[offset+2] = (pixels[i] >> 16) & 0xFF  # R
            img_data[offset+3] = (pixels[i] >> 24) & 0xFF  # A

        img = Image.frombytes("RGBA", (width, height), bytes(img_data), "raw", "RGBA")
        img.save("C:/Users/admin/Desktop/edr-wd-main/edr-wd-main/hisec_printwindow.png")
        print(f"Saved! Size: {width}x{height}")
    else:
        print("GetDIBits failed")
else:
    print("PrintWindow failed - cannot capture")

gdi32.DeleteObject(bmp)
gdi32.DeleteObject(memdc)
