"""Builds the images the patcher tests run against, in memory.

Nothing here is committed as a blob. The real files are six to fourteen
megabytes of encrypted game binary that nobody can redistribute, and the parts
that matter are tiny: a handful of PowerPC instructions at one known offset and
enough of an ELF header to look like one.

The offsets are the ones in ps3tools.titles, and they are the point of the
whole exercise. The scan cross-checks what each fix's own site finding returns
against the offset recorded there, so an image that puts the site anywhere else
has to be built deliberately to test that the cross-check fires. That is what
mw3_image(offset=...) is for.

A fake SELF is a header block and the image glued together. It is not
encryption and is not meant to be: it exists so the test's stand-in scetool has
something to take apart, and so that a klicensee that is wrong for the file
fails the way the real one does.
"""

import json
import struct

FAKE_MAGIC = b"FAKESELF"

BO2_SITE = 0x004980F0
MW3_SITE = 0x00330D20

BO2_IMAGE_SIZE = BO2_SITE + 0x1000
MW3_IMAGE_SIZE = MW3_SITE + 0x1000

LOAD_BASE = 0x10000

# patch-mw3.py's own constants, repeated here rather than imported so that a
# change in either shows up as a failing test rather than as two things moving
# together silently.
MW3_SIG_HEAD = bytes.fromhex("480FE2F92C1B0002")
MW3_SIG_TAIL = bytes.fromhex("4082009C")
MW3_STOCK = bytes.fromhex("607C0000")
MW3_PATCHED = bytes.fromhex("3B800000")

BO2_FMT = b"crm %lld %s\x00"
BO2_FMT_OFFSET = 0x400
BO2_NOP = 0x60000000
# 4BCCCE4D, the instruction the Black Ops II readme records at this site on
# BLES01717 1.19. Any bl would do; this is the one that is actually there.
BO2_STOCK_BL = 0x4BCCCE4D


def elf_header(entry, size):
    """ELF64 big endian with one PT_LOAD, so a vaddr maps to a file offset."""
    data = bytearray(0x40 + 0x38)
    data[0:4] = b"\x7fELF"
    data[4] = 2
    data[5] = 2
    data[6] = 1
    struct.pack_into(">HH", data, 0x10, 2, 0x15)
    struct.pack_into(">I", data, 0x14, 1)
    struct.pack_into(">QQQ", data, 0x18, entry, 0x40, 0)
    struct.pack_into(">HH", data, 0x36, 0x38, 1)
    struct.pack_into(">I", data, 0x40, 1)
    struct.pack_into(">QQ", data, 0x48, 0, LOAD_BASE)
    struct.pack_into(">Q", data, 0x60, size)
    return bytes(data)


def mw3_image(state="stock", offset=MW3_SITE, size=MW3_IMAGE_SIZE):
    """The multiplayer binary, with the sixteen byte signature at one offset.

    state may be "stock", "patched", or "neither", the last putting a word
    there that is neither instruction so that the scan has to call it
    unrecognised rather than guessing which it is closer to.
    """
    data = bytearray(max(size, offset + 0x10))
    head = elf_header(0x340D20, len(data))
    data[0:len(head)] = head
    middle = {"stock": MW3_STOCK, "patched": MW3_PATCHED,
              "neither": bytes.fromhex("38000000")}[state]
    site = offset - 8
    data[site:site + 4] = MW3_SIG_HEAD[:4]
    data[site + 4:site + 8] = MW3_SIG_HEAD[4:]
    data[offset:offset + 4] = middle
    data[offset + 4:offset + 8] = MW3_SIG_TAIL
    return bytes(data)


def bo2_image(state="stock", offset=BO2_SITE, size=BO2_IMAGE_SIZE):
    """The campaign or multiplayer binary, with the CRM logging call in it.

    The instruction sequence is the one in the Black Ops II readme's
    disassembly: the format string address is built, the buffer size is loaded,
    and the call the fix removes sits seven words after the addi.
    """
    data = bytearray(max(size, offset + 0x1000))
    head = elf_header(0x10000 + 0x100, len(data))
    data[0:len(head)] = head
    target = BO2_FMT_OFFSET + LOAD_BASE
    call = {"stock": BO2_STOCK_BL, "patched": BO2_NOP,
            "neither": 0x38000000}[state]
    words = [
        0x3C800000 | ((target >> 16) & 0xFFFF),      # lis   r4, hi
        0x3BC40000 | (target & 0xFFFF),              # addi  r30, r4, lo
        0x48000001,                                  # bl    <ms timer>
        0x60670000,                                  # ori   r7, r3, 0
        0x607F0000,                                  # ori   r3, r31, 0
        0x388001B8,                                  # li    r4, 0x1b8
        0x60C50000,                                  # ori   r5, r30, 0
        0x60A60000,                                  # ori   r6, r29, 0
        call,                                        # bl    <snprintf>
    ]
    start = offset - 4 * (len(words) - 1)
    for index, word in enumerate(words):
        struct.pack_into(">I", data, start + 4 * index, word)
    data[BO2_FMT_OFFSET:BO2_FMT_OFFSET + len(BO2_FMT)] = BO2_FMT
    return bytes(data)


# --- the fake container ----------------------------------------------------

def header_text(info):
    """scetool -i output, laid out the way it prints for these titles."""
    return "\n".join([
        "[*] SCE Header:",
        "  Key Revision                  0x%s" % info["key_revision"],
        "[*] SELF Header:",
        "  SELF-Type                     [NPDRM Application]",
        "[*] Application Info:",
        "  Auth-ID                       %s" % info["auth_id"],
        "  Vendor-ID                     %s" % info["vendor_id"],
        "  Version                       [01.00]",
        "  SELF-FW-Version               %s" % info["fw_version"],
        "[*] Control Info:",
        "  Licence Type                  [%s]" % info["licence_type"].title(),
        "  App Type                      [%s]" % info["app_type"],
        "  ContentID                     %s" % info["content_id"],
        "  CID_FN Hash                   %s" % info["cid_fn_hash"],
        "",
    ])


def wrap(info, klicensee, image):
    body = json.dumps({"info": info, "klicensee": klicensee or ""}).encode()
    return FAKE_MAGIC + struct.pack(">I", len(body)) + body + image


def unwrap(data):
    if not data.startswith(FAKE_MAGIC):
        raise ValueError("not a fake SELF")
    length = struct.unpack_from(">I", data, len(FAKE_MAGIC))[0]
    start = len(FAKE_MAGIC) + 4
    body = json.loads(data[start:start + length].decode())
    return body["info"], body["klicensee"], data[start + length:]


def cid_fn_hash(name):
    """Stands in for the hash of the filename fed to -g. What matters is only
    that it changes when the name does."""
    import hashlib
    return hashlib.sha1(name.encode()).hexdigest()[:32].upper()


def info_for(content_id, app_type, name, key_revision="001C",
             fw_version="0004002000000000"):
    return {
        "key_revision": key_revision,
        "self_type": "NPDRM",
        "auth_id": "1010000001000003",
        "vendor_id": "01000002",
        "app_version": "0001000000000000",
        "fw_version": fw_version,
        "licence_type": "FREE",
        "app_type": app_type,
        "content_id": content_id,
        "cid_fn_hash": cid_fn_hash(name),
    }
