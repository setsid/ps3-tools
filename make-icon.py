#!/usr/bin/env python3
"""
Draws icon.ico and icon.png for PS3 Tools by setsid.

This is a placeholder. It exists so a fresh checkout builds an exe with an icon
rather than the default Python one, and so the window and the taskbar button
have something to show. REPLACE IT with a real icon before releasing anything:
a two letter tile is what a program looks like when nobody has drawn it one yet.
It matters more now than it did, because this is the icon on the one exe a
nervous stranger has been told to download and run.

Stdlib only, so the icon can be rebuilt on any machine that can run the program
itself.

    python3 make-icon.py

icon.ico carries 16, 32 and 48, which is what Windows asks for between the title
bar and the desktop. icon.png is the 64 that Tk uses everywhere else.
"""

import os
import struct
import sys
import zlib

BACKGROUND = (0x15, 0x1B, 0x26)
INK = (0x5B, 0xC0, 0xEB)

# Both glyphs are 5 wide and 7 tall, the shapes from the classic 5x7 terminal
# font. Anything smarter falls apart at 16 pixels, where one cell is one pixel
# and these 35 cells are the whole letter.
GLYPHS = {
    "P": ("####.",
          "#...#",
          "#...#",
          "####.",
          "#....",
          "#....",
          "#...."),
    "3": ("#####",
          "...#.",
          "..#..",
          "...#.",
          "....#",
          "#...#",
          ".###."),
}

TEXT = "P3"
GLYPH_WIDTH = 5
GLYPH_HEIGHT = 7
# One blank cell between the two letters.
GAP = 1
BLOCK_WIDTH = len(TEXT) * GLYPH_WIDTH + (len(TEXT) - 1) * GAP

ICO_SIZES = (16, 32, 48)
PNG_SIZE = 64


def cells():
    """The whole of P3 as one grid of True and False, 11 by 7."""
    grid = [[False] * BLOCK_WIDTH for _ in range(GLYPH_HEIGHT)]
    for index, letter in enumerate(TEXT):
        left = index * (GLYPH_WIDTH + GAP)
        for y, row in enumerate(GLYPHS[letter]):
            for x, mark in enumerate(row):
                if mark != ".":
                    grid[y][left + x] = True
    return grid


def render(size):
    """The tile at one size, as rows of (r, g, b).

    A cell is a twelfth of the tile, which leaves the 11 cell block with half a
    cell of air each side, and the 7 rows centred in what is left. At 16 that
    works out at one pixel per cell, which is the size the shapes were chosen
    for.
    """
    grid = cells()
    cell = max(1, size // 12)
    left = (size - BLOCK_WIDTH * cell) // 2
    top = (size - GLYPH_HEIGHT * cell) // 2
    rows = [[BACKGROUND] * size for _ in range(size)]
    for y, line in enumerate(grid):
        for x, on in enumerate(line):
            if not on:
                continue
            for dy in range(cell):
                row = rows[top + y * cell + dy]
                for dx in range(cell):
                    row[left + x * cell + dx] = INK
    return rows


def png_bytes(rows):
    """A plain 8 bit RGB PNG. No filtering: the image is two colours in large
    blocks, which deflate handles perfectly well on its own."""
    size = len(rows)
    raw = bytearray()
    for row in rows:
        raw.append(0)
        for pixel in row:
            raw.extend(pixel)

    def chunk(kind, body):
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\x0a"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def dib_bytes(rows):
    """One icon image: a 32 bit BGRA bitmap, bottom row first, with the AND mask
    Windows still expects after it even though every pixel is opaque."""
    size = len(rows)
    pixels = bytearray()
    for row in reversed(rows):
        for r, g, b in row:
            pixels.extend((b, g, r, 0xFF))
    # The mask is 1 bit per pixel, each row padded out to 4 bytes, and all zero
    # means show the lot.
    mask_stride = ((size + 31) // 32) * 4
    mask = bytes(mask_stride * size)
    # biHeight is the image and the mask stacked, so twice the real height.
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                         len(pixels) + len(mask), 0, 0, 0, 0)
    return header + bytes(pixels) + mask


def ico_bytes(sizes):
    images = [dib_bytes(render(size)) for size in sizes]
    offset = 6 + 16 * len(images)
    directory = b""
    for size, image in zip(sizes, images):
        directory += struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32,
                                 len(image), offset)
        offset += len(image)
    return struct.pack("<HHH", 0, 1, len(images)) + directory + b"".join(images)


def preview(size):
    return "\n".join("".join("#" if pixel == INK else "." for pixel in row)
                     for row in render(size))


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    if "--preview" in sys.argv:
        print(preview(PNG_SIZE))
        return
    for name, blob in (("icon.ico", ico_bytes(ICO_SIZES)),
                       ("icon.png", png_bytes(render(PNG_SIZE)))):
        path = os.path.join(here, name)
        with open(path, "wb") as handle:
            handle.write(blob)
        print(f"wrote {path}  {len(blob):,} bytes")


if __name__ == "__main__":
    main()
