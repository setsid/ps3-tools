#!/usr/bin/env python3
"""
Draws icon.ico and icon.png for PS3 Tools by setsid.

Both are cut out of logo.png rather than drawn here: the two chevrons on the
left of the wordmark are square on their own, which is exactly what a taskbar
button and a title bar want. One source of truth means the icon cannot drift
away from the logo the program shows in its own top left.

Stdlib only, so the icon can be rebuilt on any machine that can run the program
itself. That means the PNG reading, the resampling and the ICO writing are all
here. logo.png is 8 bit RGBA and not interlaced; anything else is refused
rather than guessed at.

    python3 make-icon.py

icon.ico carries 16, 32, 48 and 256, which is what Windows asks for between the
title bar and the desktop. icon.png is the 256 the shell uses everywhere else.
"""

import os
import struct
import sys
import zlib

SOURCE = "logo.png"

#: The chevrons, in logo.png's own pixels. Read off the image by looking for
#: the first run of columns that has anything in it: the wordmark starts at 352
#: and there is nothing between.
#: left, top, right, bottom -- right and bottom exclusive.
CHEVRON_BOX = (40, 64, 260, 260)

#: A little air around the glyph. Windows draws icons edge to edge and a shape
#: that touches all four sides looks bigger than everything beside it.
MARGIN = 0.08

ICO_SIZES = (16, 32, 48, 256)
PNG_SIZE = 256


# --- reading ---------------------------------------------------------------

def read_png(path):
    """(width, height, RGBA bytes) for an 8 bit non-interlaced RGBA PNG."""
    with open(path, "rb") as handle:
        data = handle.read()
    if data[:8] != b"\x89PNG\r\n\x1a\x0a":
        raise SystemExit(f"{path} is not a PNG")
    width, height, depth, colour, _comp, _filt, interlace = struct.unpack(
        ">IIBBBBB", data[16:29])
    if (depth, colour, interlace) != (8, 6, 0):
        raise SystemExit(
            f"{path} is not 8 bit RGBA without interlacing "
            f"(depth={depth} colour={colour} interlace={interlace}). "
            f"Save it that way and run this again.")
    idat = b""
    offset = 8
    while offset < len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        if kind == b"IDAT":
            idat += data[offset + 8:offset + 8 + length]
        offset += 12 + length
    return width, height, _unfilter(zlib.decompress(idat), width, height)


def _unfilter(raw, width, height):
    """Undo the five PNG row filters. Four bytes a pixel throughout."""
    bpp = 4
    stride = width * bpp
    out = bytearray()
    previous = bytearray(stride)
    position = 0
    for _y in range(height):
        kind = raw[position]
        position += 1
        line = bytearray(raw[position:position + stride])
        position += stride
        if kind == 1:
            for x in range(bpp, stride):
                line[x] = (line[x] + line[x - bpp]) & 0xFF
        elif kind == 2:
            for x in range(stride):
                line[x] = (line[x] + previous[x]) & 0xFF
        elif kind == 3:
            for x in range(stride):
                left = line[x - bpp] if x >= bpp else 0
                line[x] = (line[x] + ((left + previous[x]) >> 1)) & 0xFF
        elif kind == 4:
            for x in range(stride):
                left = line[x - bpp] if x >= bpp else 0
                up = previous[x]
                corner = previous[x - bpp] if x >= bpp else 0
                guess = left + up - corner
                da, db, dc = (abs(guess - left), abs(guess - up),
                              abs(guess - corner))
                if da <= db and da <= dc:
                    nearest = left
                elif db <= dc:
                    nearest = up
                else:
                    nearest = corner
                line[x] = (line[x] + nearest) & 0xFF
        elif kind != 0:
            raise SystemExit(f"unknown PNG row filter {kind}")
        out += line
        previous = line
    return bytes(out)


# --- cutting out and scaling ------------------------------------------------

def square_source(width, height, pixels):
    """The chevrons on a transparent square, with a margin. (size, rows)."""
    left, top, right, bottom = CHEVRON_BOX
    if right > width or bottom > height:
        raise SystemExit(f"{SOURCE} is smaller than the chevrons are supposed "
                         f"to be; CHEVRON_BOX needs looking at again.")
    glyph_width = right - left
    glyph_height = bottom - top
    side = int(max(glyph_width, glyph_height) * (1 + MARGIN * 2))
    x_offset = (side - glyph_width) // 2
    y_offset = (side - glyph_height) // 2
    rows = [[(0, 0, 0, 0)] * side for _ in range(side)]
    for y in range(glyph_height):
        base = ((top + y) * width + left) * 4
        row = rows[y_offset + y]
        for x in range(glyph_width):
            here = base + x * 4
            row[x_offset + x] = tuple(pixels[here:here + 4])
    return side, rows


def resample(rows, size):
    """Box filter down to size, averaging colour weighted by alpha.

    Weighting matters: averaging the colour of a transparent pixel in with an
    opaque one drags the edge towards whatever happens to be stored under the
    transparency, which shows up as a dark fringe at 16 pixels.
    """
    source = len(rows)
    out = []
    for y in range(size):
        y0 = y * source // size
        y1 = max(y0 + 1, (y + 1) * source // size)
        line = []
        for x in range(size):
            x0 = x * source // size
            x1 = max(x0 + 1, (x + 1) * source // size)
            count = (y1 - y0) * (x1 - x0)
            alpha_total = 0
            red = green = blue = 0
            for sy in range(y0, y1):
                row = rows[sy]
                for sx in range(x0, x1):
                    r, g, b, a = row[sx]
                    alpha_total += a
                    red += r * a
                    green += g * a
                    blue += b * a
            if alpha_total:
                line.append((red // alpha_total, green // alpha_total,
                             blue // alpha_total, alpha_total // count))
            else:
                line.append((0, 0, 0, 0))
        out.append(line)
    return out


# --- writing ----------------------------------------------------------------

def png_bytes(rows):
    """An 8 bit RGBA PNG, one filter byte of zero a row."""
    height = len(rows)
    width = len(rows[0])
    raw = bytearray()
    for row in rows:
        raw.append(0)
        for pixel in row:
            raw.extend(pixel)

    def chunk(kind, body):
        return (struct.pack(">I", len(body)) + kind + body
                + struct.pack(">I", zlib.crc32(kind + body) & 0xFFFFFFFF))

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\x0a"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + chunk(b"IEND", b""))


def dib_bytes(rows):
    """One icon image: 32 bit BGRA, bottom row first, then the AND mask.

    The mask is still written even though the alpha channel already says what
    is transparent, because the shell falls back to it when it draws the icon
    somewhere that predates 32 bit icons.
    """
    size = len(rows)
    pixels = bytearray()
    for row in reversed(rows):
        for r, g, b, a in row:
            pixels.extend((b, g, r, a))
    mask_stride = ((size + 31) // 32) * 4
    mask = bytearray()
    for row in reversed(rows):
        bits = bytearray(mask_stride)
        for x, pixel in enumerate(row):
            if pixel[3] < 128:
                bits[x // 8] |= 0x80 >> (x % 8)
        mask += bits
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0,
                         len(pixels) + len(mask), 0, 0, 0, 0)
    return header + bytes(pixels) + bytes(mask)


def ico_bytes(images):
    """images is {size: rows}. 256 goes in as a PNG, which is what it is
    for: a quarter megabyte of bitmap is not what an ICO wants."""
    blobs = []
    for size in sorted(images):
        rows = images[size]
        blobs.append((size, png_bytes(rows) if size >= 256
                      else dib_bytes(rows)))
    offset = 6 + 16 * len(blobs)
    directory = b""
    for size, blob in blobs:
        # 0 in the width and height byte means 256; the field is one byte.
        stored = 0 if size >= 256 else size
        directory += struct.pack("<BBBBHHII", stored, stored, 0, 0, 1, 32,
                                 len(blob), offset)
        offset += len(blob)
    return (struct.pack("<HHH", 0, 1, len(blobs)) + directory
            + b"".join(blob for _size, blob in blobs))


def preview(rows):
    shades = " .:-=+*#%@"
    return "\n".join(
        "".join(shades[min(len(shades) - 1, pixel[3] * len(shades) // 256)]
                for pixel in row)
        for row in rows)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    source = os.path.join(here, SOURCE)
    if not os.path.exists(source):
        raise SystemExit(
            f"{SOURCE} is missing, and the icon is cut out of it.")
    width, height, pixels = read_png(source)
    side, rows = square_source(width, height, pixels)
    sizes = sorted(set(ICO_SIZES) | {PNG_SIZE})
    scaled = {size: resample(rows, size) for size in sizes}
    if "--preview" in sys.argv:
        print(preview(scaled[32]))
        return
    written = [("icon.ico", ico_bytes({size: scaled[size]
                                       for size in ICO_SIZES})),
               ("icon.png", png_bytes(scaled[PNG_SIZE]))]
    for name, blob in written:
        path = os.path.join(here, name)
        with open(path, "wb") as handle:
            handle.write(blob)
        print(f"wrote {path}  {len(blob):,} bytes  from {SOURCE} "
              f"({side}x{side} source square)")


if __name__ == "__main__":
    main()
