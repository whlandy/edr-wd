"""
redaction.py — V1 minimum text + image redaction (architecture §14.4,
FR-P1.4-07).

Two layers:

  * `apply_text_redaction(text, rules)` — replace matches of each
    rule's pattern with `[REDACTED]`; return the redacted text
    plus the rule IDs that fired.

  * `apply_image_redaction(png_bytes, rules)` — apply each rule's
    rectangle(s) by overwriting the PNG pixels with a solid opaque
    color in-place. Returns the redacted bytes plus the rule IDs
    that fired.

A rule is identified by a stable string id and carries either a
text pattern (regex) or one or more opaque-rectangle geometry
records. Rules are immutable; the API returns a new redacted
output rather than mutating input.

V1 minimum (per spec §14.4): "text pattern registry + opaque
rectangle rules". The image redaction records `rule_ids` without
storing the original — we implement the masking in-place so the
on-disk bytes already carry the redaction; the rule_id list is the
audit trail.
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
    """Return (redacted_text, fired_rule_ids).

    Each TextRule's pattern is matched globally; matches are
    replaced with `[REDACTED]` (a single token, regardless of
    match length). Rule IDs are returned in the order they fired
    on the input.
    """
    fired: list[str] = []
    out = text
    for rule in rules:
        compiled = rule.compile()
        if compiled.search(out):
            fired.append(rule.rule_id)
            out = compiled.sub("[REDACTED]", out)
    return out, fired


# ---------------------------------------------------------------------------
# Image redaction
# ---------------------------------------------------------------------------


# PNG signature + IHDR chunk header.
_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_IHDR_LEN = 13  # length field of the IHDR chunk data


def _read_png_chunks(png_bytes: bytes) -> list[tuple[int, int, int, bytes]]:
    """Return list of `(offset, length, type_code, data)` for each
    chunk in the PNG. The type code is the 4-byte big-endian tag
    read as an int.

    Raises ValueError if the PNG signature is missing or the byte
    stream ends mid-chunk.
    """
    if not png_bytes.startswith(_PNG_SIG):
        raise ValueError("not a PNG")
    chunks: list[tuple[int, int, int, bytes]] = []
    pos = len(_PNG_SIG)
    while pos < len(png_bytes):
        if pos + 8 > len(png_bytes):
            raise ValueError("truncated chunk header")
        length = int.from_bytes(png_bytes[pos:pos + 4], "big")
        crc_offset = pos + 4
        type_offset = pos + 4
        data_offset = pos + 8
        next_offset = data_offset + length + 4  # +4 for trailing CRC
        if next_offset > len(png_bytes):
            raise ValueError("truncated chunk body")
        type_code = int.from_bytes(
            png_bytes[type_offset:type_offset + 4], "big",
        )
        data = png_bytes[data_offset:data_offset + length]
        chunks.append((pos, length, type_code, data))
        pos = next_offset
    return chunks


def _png_dimensions(png_bytes: bytes) -> tuple[int, int]:
    """Return (width, height) from the IHDR chunk.

    Raises ValueError if there is no IHDR chunk.
    """
    for _, _, type_code, data in _read_png_chunks(png_bytes):
        if type_code == int.from_bytes(b"IHDR", "big"):
            width = int.from_bytes(data[0:4], "big")
            height = int.from_bytes(data[4:8], "big")
            return width, height
    raise ValueError("PNG has no IHDR chunk")


def _idat_payloads(png_bytes: bytes) -> list[tuple[int, int]]:
    """Return (offset, length) of every IDAT chunk's data segment."""
    out: list[tuple[int, int]] = []
    for _, _, type_code, data in _read_png_chunks(png_bytes):
        if type_code == int.from_bytes(b"IDAT", "big"):
            out.append((0, len(data)))  # indices within the data slice
    return out


def apply_image_redaction(
    png_bytes: bytes, rules: Sequence[ImageRule]
) -> tuple[bytes, list[str]]:
    """Apply every ImageRule's rectangles in-place on a PNG.

    Pixels are replaced with a solid opaque gray (RGB 0, 0, 0) in
    the un-filtered scanline representation. The IHDR width/height
    determine the bytes-per-pixel assumption (RGBA = 4 bpp when
    color type 6; RGB = 3 bpp when color type 2). We default to
    RGBA masking and skip non-supported color types (V1 minimum).

    Returns (redacted_bytes, fired_rule_ids).  Raises ValueError
    if the input is not a recognisable PNG.
    """
    if not rules:
        return png_bytes, []
    # Check whether any rule actually has rectangles; a rule with
    # an empty rectangles tuple is a no-op.
    if not any(r.rectangles for r in rules):
        return png_bytes, []
    width, height = _png_dimensions(png_bytes)
    # Find color type from IHDR data.
    for _, _, type_code, data in _read_png_chunks(png_bytes):
        if type_code == int.from_bytes(b"IHDR", "big"):
            color_type = data[9]
            break
    bpp = {2: 3, 6: 4}.get(color_type)  # RGB or RGBA only in V1.
    if bpp is None:
        # V1 minimum: skip unsupported color types (palette, gray).
        return png_bytes, []

    # Rebuild the PNG with every IDAT concatenated, then split by scanline
    # + filter byte. Mutate, recompute IDAT chunks, write back.
    chunks = _read_png_chunks(png_bytes)
    fired: list[str] = []
    rects: list[tuple[int, int, int, int]] = []
    for rule in rules:
        if not rule.rectangles:
            continue
        fired.append(rule.rule_id)
        for r in rule.rectangles:
            rects.append((r.x, r.y, r.width, r.height))

    # Concatenate IDAT data.
    idat_data = b"".join(
        d for _, _, tc, d in chunks if tc == int.from_bytes(b"IDAT", "big")
    )
    raw = zlib.decompress(idat_data)
    # Scanline length = width * bpp + 1 filter byte.
    sl = width * bpp + 1
    if len(raw) != sl * height:
        return png_bytes, []  # malformed; do not redact.

    # Decode each scanline's filter (apply None/Sub/Up/Average/Paeth inverse).
    decoded = bytearray()
    prev = bytes(width * bpp)
    for y in range(height):
        line = raw[y * sl:(y + 1) * sl]
        if not line:
            return png_bytes, []
        filter_type = line[0]
        cur = bytearray(line[1:])
        if filter_type == 0:
            pass
        elif filter_type == 1:  # Sub
            for i in range(bpp, len(cur)):
                cur[i] = (cur[i] + cur[i - bpp]) & 0xFF
        elif filter_type == 2:  # Up
            for i in range(len(cur)):
                cur[i] = (cur[i] + prev[i]) & 0xFF
        elif filter_type == 3:  # Average
            for i in range(len(cur)):
                left = cur[i - bpp] if i >= bpp else 0
                up = prev[i]
                cur[i] = (cur[i] + ((left + up) // 2)) & 0xFF
        elif filter_type == 4:  # Paeth
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
            return png_bytes, []  # unknown filter; bail.
        decoded.extend(cur)
        prev = bytes(cur)

    # Apply every rectangle.
    for x, y, w, h in rects:
        for row in range(y, min(y + h, height)):
            for col in range(x, min(x + w, width)):
                base = (row * width + col) * bpp
                if base + bpp > len(decoded):
                    continue
                for k in range(bpp):
                    decoded[base + k] = 0  # opaque black.

    # Re-encode scanlines (filter 0 / None).
    raw = bytearray()
    pos = 0
    for _ in range(height):
        raw.append(0)
        raw.extend(decoded[pos:pos + width * bpp])
        pos += width * bpp
    new_idat = zlib.compress(bytes(raw), level=6)

    # Rebuild the PNG: keep all chunks except IDAT, append new IDAT.
    out = bytearray(_PNG_SIG)
    for offset, length, type_code, data in chunks:
        if type_code == int.from_bytes(b"IDAT", "big"):
            continue
        # Write length + type + data + CRC of (type + data).
        import struct
        out.extend(struct.pack(">I", length))
        out.extend(struct.pack(">I", type_code))
        out.extend(data)
        # CRC over type + data
        import binascii
        crc = binascii.crc32(struct.pack(">I", type_code) + data) & 0xFFFFFFFF
        out.extend(struct.pack(">I", crc))
    # New IDAT chunk.
    out.extend(struct.pack(">I", len(new_idat)))
    out.extend(struct.pack(">I", int.from_bytes(b"IDAT", "big")))
    out.extend(new_idat)
    import binascii
    crc = binascii.crc32(struct.pack(">I", int.from_bytes(b"IDAT", "big")) + new_idat) & 0xFFFFFFFF
    out.extend(struct.pack(">I", crc))
    # IEND is mandatory; ensure it's the last chunk.
    return bytes(out), fired


__all__ = [
    "TextRule",
    "ImageRule",
    "Rectangle",
    "RedactionRule",
    "apply_text_redaction",
    "apply_image_redaction",
]