#!/usr/bin/env python3
"""
Draw the Top Stories home-screen icons into intelligence/static.

A newspaper page in black and white: a headline bar over four lines of
text, the last one shorter. Only the standard library is used (PNG via
zlib/struct), so the container needs no image library. The output is
committed; run this again only after changing the design:

    python3 scripts/make-icons.py
"""

import struct
import zlib
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / 'intelligence' / 'static'

# Design on a 512 x 512 grid: (x0, y0, x1, y1) of the white bars on black
DESIGN = 512
BARS = [
    (96, 104, 416, 184),   # headline
    (96, 232, 416, 264),
    (96, 296, 416, 328),
    (96, 360, 416, 392),
    (96, 424, 320, 456),   # last line, shorter
]
INK, PAPER = 0x00, 0xFF


def pixels(size: int) -> list:
    """Rows of 8-bit gray values for an icon of size x size pixels."""
    scale = size / DESIGN
    rows = [[INK] * size for _ in range(size)]
    for x0, y0, x1, y1 in BARS:
        for y in range(round(y0 * scale), round(y1 * scale)):
            for x in range(round(x0 * scale), round(x1 * scale)):
                rows[y][x] = PAPER
    return rows


def png(size: int, rgba: bool = False) -> bytes:
    """
    An 8-bit grayscale PNG of the icon, or 32-bit RGBA for the .ico
    (Windows only reads 32-bit PNGs inside an .ico).
    """
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (struct.pack('>I', len(data)) + kind + data
                + struct.pack('>I', zlib.crc32(kind + data) & 0xFFFFFFFF))

    def encode(row: list) -> bytes:
        return bytes(v for gray in row for v in (gray, gray, gray, 0xFF)) if rgba else bytes(row)

    raw = b''.join(b'\x00' + encode(row) for row in pixels(size))  # filter 0 per row
    header = struct.pack('>IIBBBBB', size, size, 8, 6 if rgba else 0, 0, 0, 0)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', header)
            + chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b''))


def ico(image: bytes, size: int) -> bytes:
    """A .ico file holding one PNG image (understood by all current browsers)."""
    header = struct.pack('<HHH', 0, 1, 1)
    entry = struct.pack('<BBBBHHII', size % 256, size % 256, 0, 0, 1, 32, len(image), 6 + 16)
    return header + entry + image


def svg() -> str:
    bars = ''.join(f'<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}"/>'
                   for x0, y0, x1, y1 in BARS)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {DESIGN} {DESIGN}">'
            f'<rect width="{DESIGN}" height="{DESIGN}" fill="#000"/>'
            f'<g fill="#fff">{bars}</g></svg>\n')


def main() -> None:
    STATIC.mkdir(exist_ok=True)
    for size in (192, 512):
        (STATIC / f'icon-{size}.png').write_bytes(png(size))
    (STATIC / 'favicon.ico').write_bytes(ico(png(32, rgba=True), 32))
    (STATIC / 'icon.svg').write_text(svg(), encoding='utf-8')


if __name__ == '__main__':
    main()
