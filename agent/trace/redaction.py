"""
redaction.py — V1 minimum text + image redaction (architecture §14.4,
FR-P1.4-07).

[commit A — initial implementation, pre-self-fix]
"""

from __future__ import annotations

import dataclasses
import re
import zlib
from dataclasses import dataclass, field
from typing import Sequence


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TextRule:
    rule_id: str
    pattern: str  # regex source
    flags: int = 0

    def compile(self) -> re.Pattern[str]:
        return re.compile(self.pattern, self.flags)


@dataclass(frozen=True)
class Rectangle:
    """An opaque rectangle in pixel coordinates of the original PNG."""
    x: int
    y: int
    width: int
    height: int


@dataclass(frozen=True)
class ImageRule:
    rule_id: str
    rectangles: tuple[Rectangle, ...] = ()


RedactionRule = TextRule | ImageRule


# ---------------------------------------------------------------------------
# Text redaction
# ---------------------------------------------------------------------------


def apply_text_redaction(
    text: str, rules: Sequence[TextRule]
) -> tuple[str, list[str]]:
    """Return (redacted_text, fired_rule_ids)."""
    fired: list[str] = []
    out = text
    for rule in rules:
        compiled = rule.compile()
        if compiled.search(out):
            fired.append(rule.rule_id)
            out = compiled.sub("[REDACTED]", out)
    return out, fired


# ---------------------------------------------------------------------------
# Image redaction (pre-fix: SPECIALS includes backslash
# causing double-escape issues are NOT in this module — those
# were in markdown.py. The bug here in pre-fix state is that
# empty-rectangles rules re-emit the PNG (they should be a no-op).
# ---------------------------------------------------------------------------


_PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _read_png_chunks(png_bytes: bytes) -> list[tuple[int, int, int, bytes]]:
    if not png_bytes.startswith(_PNG_SIG):
        raise ValueError("not a PNG")
    chunks: list[tuple[int, int, int, bytes]] = []
    pos = len(_PNG_SIG)
    while pos < len(png_bytes):
        if pos + 8 > len(png_bytes):
            raise ValueError("truncated chunk header")
        length = int.from_bytes(png_bytes[pos:pos + 4], "big")
        type_code = int.from_bytes(
            png_bytes[pos + 4:pos + 8], "big",
        )
        data = png_bytes[pos + 8:pos + 8 + length]
        next_offset = pos + 8 + length + 4
        if next_offset > len(png_bytes):
            raise ValueError("truncated chunk body")
        chunks.append((pos, length, type_code, data))
        pos = next_offset
    return chunks


def _png_dimensions(png_bytes: bytes) -> tuple[int, int]:
    for _, _, type_code, data in _read_png_chunks(png_bytes):
        if type_code == int.from_bytes(b"IHDR", "big"):
            width = int.from_bytes(data[0:4], "big")
            height = int.from_bytes(data[4:8], "big")
            return width, height
    raise ValueError("PNG has no IHDR chunk")


def apply_image_redaction(
    png_bytes: bytes, rules: Sequence[ImageRule]
) -> tuple[bytes, list[str]]:
    """Apply every ImageRule's rectangles in-place on a PNG.

    Pre-fix state: an empty-rectangles rule still re-emits the PNG
    (because the rule is in `rules`, not because of empty rects).
    Bug: returns a fresh PNG even when no rectangle was actually
    applied. Fixed in review #1.
    """
    if not rules:
        return png_bytes, []
    width, height = _png_dimensions(png_bytes)
    for _, _, type_code, data in _read_png_chunks(png_bytes):
        if type_code == int.from_bytes(b"IHDR", "big"):
            color_type = data[9]
            break
    bpp = {2: 3, 6: 4}.get(color_type)
    if bpp is None:
        return png_bytes, []

    chunks = _read_png_chunks(png_bytes)
    fired: list[str] = []
    rects: list[tuple[int, int, int, int]] = []
    for rule in rules:
        # Pre-fix: still appends rule_id even if rectangles is empty.
        fired.append(rule.rule_id)
        for r in rule.rectangles:
            rects.append((r.x, r.y, r.width, r.height))

    if not rects:
        # Nothing to apply; pre-fix returns original bytes but
        # fired is still populated (bug: rule_id reported without
        # any pixels masked).
        return png_bytes, fired

    idat_data = b"".join(
        d for _, _, tc, d in chunks if tc == int.from_bytes(b"IDAT", "big")
    )
    raw = zlib.decompress(idat_data)
    sl = width * bpp + 1
    if len(raw) != sl * height:
        return png_bytes, fired

    decoded = bytearray()
    prev = bytes(width * bpp)
    for y in range(height):
        line = raw[y * sl:(y + 1) * sl]
        if not line:
            return png_bytes, fired
        filter_type = line[0]
        cur = bytearray(line[1:])
        if filter_type == 0:
            pass
        elif filter_type == 1:
            for i in range(bpp, len(cur)):
                cur[i] = (cur[i] + cur[i - bpp]) & 0xFF
        elif filter_type == 2:
            for i in range(len(cur)):
                cur[i] = (cur[i] + prev[i]) & 0xFF
        elif filter_type == 3:
            for i in range(len(cur)):
                left = cur[i - bpp] if i >= bpp else 0
                up = prev[i]
                cur[i] = (cur[i] + ((left + up) // 2)) & 0xFF
        elif filter_type == 4:
            for i in range(len(cur)):
                left = cur[i - bpp] if i >= bpp else 0
                up = prev[i]
                up_left = prev[i - bpp] if i >= bpp else 0
                p = left + up - up_left
                pa, pb, pc = abs(p - left), abs(p - up), abs(p - up_left)
                if pa <= pb and pa <= pc:
                    pred = left
                elif pb <= pc:
                    pred = up
                else:
                    pred = up_left
                cur[i] = (cur[i] + pred) & 0xFF
        else:
            return png_bytes, fired
        decoded.extend(cur)
        prev = bytes(cur)

    for x, y, w, h in rects:
        for row in range(y, min(y + h, height)):
            for col in range(x, min(x + w, width)):
                base = (row * width + col) * bpp
                if base + bpp > len(decoded):
                    continue
                for k in range(bpp):
                    decoded[base + k] = 0

    raw = bytearray()
    pos = 0
    for _ in range(height):
        raw.append(0)
        raw.extend(decoded[pos:pos + width * bpp])
        pos += width * bpp
    new_idat = zlib.compress(bytes(raw), level=6)

    out = bytearray(_PNG_SIG)
    for _, length, type_code, data in chunks:
        if type_code == int.from_bytes(b"IDAT", "big"):
            continue
        import struct
        out.extend(struct.pack(">I", length))
        out.extend(struct.pack(">I", type_code))
        out.extend(data)
        import binascii
        crc = binascii.crc32(struct.pack(">I", type_code) + data) & 0xFFFFFFFF
        out.extend(struct.pack(">I", crc))
    out.extend(struct.pack(">I", len(new_idat)))
    out.extend(struct.pack(">I", int.from_bytes(b"IDAT", "big")))
    out.extend(new_idat)
    crc = binascii.crc32(struct.pack(">I", int.from_bytes(b"IDAT", "big")) + new_idat) & 0xFFFFFFFF
    out.extend(struct.pack(">I", crc))
    return bytes(out), fired


__all__ = [
    "TextRule",
    "ImageRule",
    "Rectangle",
    "RedactionRule",
    "apply_text_redaction",
    "apply_image_redaction",
]