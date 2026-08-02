"""Fake image helpers for P1.4 evidence tests."""

from __future__ import annotations

import struct
import zlib
from typing import Tuple


_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _chunk(t: bytes, data: bytes) -> bytes:
    import binascii
    crc = binascii.crc32(t + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + t + data + struct.pack(">I", crc)


def make_png(
    width: int = 4,
    height: int = 4,
    *,
    color_type: int = 6,  # RGBA
    pixel_value: Tuple[int, int, int, int] = (0, 0, 0, 255),
) -> bytes:
    """Build a deterministic PNG (no Pillow dependency).

    Returns a valid PNG whose every pixel has the given RGBA
    value. Tests verify the SHA-256 + width/height round-trip.
    """
    if color_type == 6:
        bpp = 4
        line = b"\x00" + bytes(pixel_value) * width
    elif color_type == 2:
        bpp = 3
        line = b"\x00" + bytes(pixel_value[:3]) * width
    else:
        raise ValueError(f"unsupported color_type {color_type}")
    raw = line * height
    ihdr = struct.pack(
        ">IIBBBBB", width, height, 8, color_type, 0, 0, 0,
    )
    return (
        _PNG_SIG
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )


__all__ = ["make_png"]