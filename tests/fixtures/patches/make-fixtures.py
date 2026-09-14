#!/usr/bin/env python3
"""Builds the patch-state fixtures. Run it from anywhere; it writes beside
itself.

Everything here is synthetic. The real files are 6 to 7 MB of encrypted game
binary that nobody can redistribute, and none of that bulk is read: the header
samples carry only the plaintext head of a SELF, laid out exactly as scetool
prints it for these titles, and the ELF samples carry only enough instructions
for each patcher's own site finding to have something to find.

The field values are the ones in the two repository READMEs and in the scetool
dumps in the Black Ops II repository's test-fixtures folder, so a change in
either shows up here as a failing test rather than as a quiet wrong answer.
"""

import os
import struct

HERE = os.path.dirname(os.path.abspath(__file__))

SCE_MAGIC = 0x53434500
NPDRM_MAGIC = 0x4E504400

# Offsets as they appear on every retail file of these two titles.
APP_INFO = 0x70
CONTROL_INFO = 0x3C0
CONTROL_SIZE = 0x100
TOTAL = CONTROL_INFO + CONTROL_SIZE


def self_header(key_revision, app_type, content_id, data_length,
                licence_type=3):
    data = bytearray(TOTAL)
    struct.pack_into(">IIHHIQQ", data, 0, SCE_MAGIC, 2, key_revision, 1,
                     0x4A0, 0xA80, data_length)
    struct.pack_into(">QQQQQQQQQQ", data, 0x20,
                     3, APP_INFO, 0x90, 0xD0, 0x5D2DFC, 0x290, 0x390,
                     CONTROL_INFO, CONTROL_SIZE, 0)
    # Auth ID, vendor ID and SELF type 8, which scetool prints as NPDRM
    # Application.
    struct.pack_into(">QIIQQ", data, APP_INFO,
                     0x1010000001000003, 0x01000002, 8,
                     0x0001000000000000, 0)
    position = CONTROL_INFO
    struct.pack_into(">IIQ", data, position, 1, 0x30, 1)      # flags
    position += 0x30
    struct.pack_into(">IIQ", data, position, 2, 0x40, 1)      # digests
    position += 0x40
    struct.pack_into(">IIQ", data, position, 3, 0x90, 0)      # NPDRM
    body = position + 0x10
    struct.pack_into(">IIII", data, body, NPDRM_MAGIC, 1, licence_type,
                     app_type)
    encoded = content_id.encode("ascii")
    data[body + 0x10:body + 0x10 + len(encoded)] = encoded
    data[body + 0x50:body + 0x60] = bytes(range(0x10))        # CID_FN hash
    return bytes(data)


def elf_header(entry=0):
    """Just enough ELF64 big endian header for load_segments to read it."""
    data = bytearray(0x40)
    data[0:4] = b"\x7fELF"
    data[4] = 2          # 64 bit
    data[5] = 2          # big endian
    data[6] = 1
    struct.pack_into(">HH", data, 0x10, 2, 0x15)              # EXEC, PPC64
    struct.pack_into(">I", data, 0x14, 1)
    struct.pack_into(">QQQ", data, 0x18, entry, 0x40, 0)
    struct.pack_into(">HH", data, 0x36, 0x38, 0)             # no PT_LOADs
    return bytes(data)


def mw3_elf(patched):
    """The 16 byte signature patch-mw3.py matches on, and nothing else."""
    data = bytearray(0x200)
    data[0:0x40] = elf_header(0x340D20)
    site = 0x100
    words = [0x480FE2F9, 0x2C1B0002,
             0x3B800000 if patched else 0x607C0000, 0x4082009C]
    for index, word in enumerate(words):
        struct.pack_into(">I", data, site + 4 * index, word)
    return bytes(data)


FMT = b"crm %lld %s\x00"
FMT_OFFSET = 0x200
LOAD_BASE = 0x10000


def bo2_elf(patched):
    """The CRM logging call patch-bo2.py looks for, in its stock shape.

    lis/addi build the address of the format string, the size and the argument
    set up the snprintf call, and the call itself is what the fix replaces with
    a nop.
    """
    data = bytearray(0x300)
    data[0:0x40] = elf_header(0x10100)
    target = FMT_OFFSET + LOAD_BASE
    words = [
        0x3C800000 | ((target >> 16) & 0xFFFF),      # lis   r4, hi
        0x3BC40000 | (target & 0xFFFF),              # addi  r30, r4, lo
        0x48000001,                                  # bl    <ms timer>
        0x60670000,                                  # ori   r7, r3, 0
        0x607F0000,                                  # ori   r3, r31, 0
        0x388001B8,                                  # li    r4, 0x1b8
        0x60C50000,                                  # ori   r5, r30, 0
        0x60A60000,                                  # ori   r6, r29, 0
        0x60000000 if patched else 0x48000001,       # bl    <snprintf>
    ]
    for index, word in enumerate(words):
        struct.pack_into(">I", data, 0x100 + 4 * index, word)
    data[FMT_OFFSET:FMT_OFFSET + len(FMT)] = FMT
    return bytes(data)


FILES = {
    # Black Ops II, BLES01717 title update 1.19. Values from the scetool dumps
    # in the fix repository.
    "bo2/eboot-bles01717-stock.selfhdr":
        self_header(0x1C, 0x21, "EP0002-BLES01717_00-CODBLOPS2PATCH09",
                    11706540),
    "bo2/t6mp-bles01717-stock.selfhdr":
        self_header(0x1C, 0x20, "EP0002-BLES01717_00-CODBLOPS2PATCH09",
                    14215768),
    "bo2/eboot-blus31140-stock.selfhdr":
        self_header(0x1C, 0x21, "UP0002-BLUS31140_00-CODBLOPS2PATCH09",
                    11706540),
    # Modern Warfare 3, BLES01428 title update 1.24.
    "mw3/default_mp-bles01428-stock.selfhdr":
        self_header(0x19, 0x20, "EP0002-BLES01428_00-MW3P000000000124",
                    7578328),
    # The same file re-signed with -c UEXEC, which is the mistake that gives a
    # valid file the console will not load.
    "mw3/default_mp-bles01428-wrong-app-type.selfhdr":
        self_header(0x19, 0x21, "EP0002-BLES01428_00-MW3P000000000124",
                    7578328),
    # A signed file from another title altogether, put here by hand.
    "bad/wrong-title.selfhdr":
        self_header(0x19, 0x20, "EP0002-BLES01702_00-TEKKENTAGTOURN2",
                    4194304),
    # Head of a file where the collector stopped before the control info.
    "bad/truncated.selfhdr":
        self_header(0x19, 0x20, "EP0002-BLES01428_00-MW3P000000000124",
                    7578328)[:0x100],
    "bad/short.selfhdr": b"SCE\x00\x00\x00\x00\x02",
    "bad/garbage.bin": bytes(range(256)) * 2,
    "bad/empty.bin": b"",
    "mw3/default_mp-stock.elf": mw3_elf(False),
    "mw3/default_mp-patched.elf": mw3_elf(True),
    "bo2/spzm-stock.elf": bo2_elf(False),
    "bo2/spzm-patched.elf": bo2_elf(True),
    "bad/not-a-game.elf": elf_header() + bytes(0x1C0),
}


def main():
    for name, content in FILES.items():
        path = os.path.join(HERE, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(content)
        print("%8d  %s" % (len(content), name))


if __name__ == "__main__":
    main()
