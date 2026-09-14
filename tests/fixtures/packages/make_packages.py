"""Package files built in memory, the way the ISO fixtures are.

A real .pkg is hundreds of megabytes of encrypted data and none of it is any
use here: everything this program reads is in the first block. So these build
exactly that block, with whatever is being tested put into it, rather than
committing a binary nobody can read or check.

Nothing here is a real package and nothing here would install on a console.
"""

import struct

MAGIC = b"\x7fPKG"
HEADER = 0x80
CONTENT_ID_AT = 0x30
TOTAL_SIZE_AT = 0x18


def param_sfo(fields):
    """The same PSF layout the ISO fixtures build. Kept here so a package
    fixture does not have to reach into the ISO ones for it."""
    keys = bytearray()
    values = bytearray()
    index = bytearray()
    entries = []
    for key, value in fields:
        key_offset = len(keys)
        keys += key.encode("ascii") + b"\x00"
        if isinstance(value, int):
            fmt, raw, maximum = 0x0404, struct.pack("<I", value), 4
        else:
            encoded = value.encode("utf-8") + b"\x00"
            maximum = (len(encoded) + 3) & ~3
            fmt, raw = 0x0204, encoded
        entries.append((key_offset, fmt, len(raw), maximum, len(values)))
        values += raw + b"\x00" * (maximum - len(raw))
    while len(keys) % 4:
        keys += b"\x00"
    key_start = 20 + 16 * len(entries)
    data_start = key_start + len(keys)
    for entry in entries:
        index += struct.pack("<HHIII", *entry)
    header = struct.pack("<4sIIII", b"\x00PSF", 0x00000101, key_start,
                         data_start, len(entries))
    return bytes(header + index + keys + values)


def package(content_id="EP0002-BLES01717_00-CODBLOPS2PATCH19",
            title=None, body_size=4096, magic=MAGIC, claimed_size=None):
    """One package header, with an optional PARAM.SFO behind it.

    claimed_size overrides the size the file says it is, which is how a
    truncated download is exercised without building a truncated download.
    """
    sfo = param_sfo((("APP_VER", "01.19"), ("CATEGORY", "GD"),
                     ("TITLE", title),
                     ("TITLE_ID", content_id[7:16]))) if title else b""
    blob = bytearray(magic + b"\x80\x00" + b"\x00\x01")
    blob += b"\x00" * (HEADER - len(blob))
    blob[CONTENT_ID_AT:CONTENT_ID_AT + len(content_id)] = \
        content_id.encode("ascii")
    blob += sfo
    blob += b"\x00" * max(0, body_size - len(sfo))
    total = len(blob) if claimed_size is None else claimed_size
    blob[TOTAL_SIZE_AT:TOTAL_SIZE_AT + 8] = struct.pack(">Q", total)
    return bytes(blob)


def not_a_package(size=2048):
    """Something the user picked by mistake. A JPEG, as it happens."""
    return b"\xff\xd8\xff\xe0" + b"\x00" * (size - 4)
