"""Builds the disc images the ISO identification tests run against.

Committing real ISOs is out of the question and committing forty kilobytes of
opaque blob is not much better, so the images are built here instead. Every
offset in this file comes from ECMA-119 (ISO 9660) and ECMA-167 plus the UDF
2.50 specification, and the point of writing them out longhand is that the
fixtures then say what the parser is being asked to believe.

The images are deliberately tiny. A real PS3 disc is 25 GB of which the first
two directory sectors and one PARAM.SFO matter; these keep those and drop the
rest, which is exactly the subset a ranged read pulls off a console anyway.

Run it to drop the files in this directory for a look:

    python3 tests/fixtures/iso/make_fixtures.py

The tests do not need them on disk; they call the builders for the bytes.
"""

import os
import struct

SECTOR = 2048

PS3_SFO_FIELDS = (
    ("APP_VER", "01.24"),
    ("ATTRIBUTE", 32),
    ("BOOTABLE", 1),
    ("CATEGORY", "DG"),
    ("LICENSE", "Library programs are subject to their licences."),
    ("PARENTAL_LEVEL", 5),
    ("PS3_SYSTEM_VER", "03.5500"),
    ("RESOLUTION", 63),
    ("SOUND_FORMAT", 279),
    ("TITLE", "Call of Duty: Modern Warfare 3"),
    ("TITLE_ID", "BLES01428"),
    ("VERSION", "01.00"),
)

PS2_SYSTEM_CNF = ("BOOT2 = cdrom0:\\SLUS_217.82;1\r\n"
                  "VER = 1.00\r\n"
                  "VMODE = NTSC\r\n").encode("ascii")


# --- little helpers --------------------------------------------------------

def _both32(value):
    return struct.pack("<I", value) + struct.pack(">I", value)


def _both16(value):
    return struct.pack("<H", value) + struct.pack(">H", value)


def _pad(data, length, fill=b"\x00"):
    return data[:length] + fill * max(0, length - len(data))


class Image:
    """A sparse image addressed by logical block."""

    def __init__(self, sectors):
        self.data = bytearray(sectors * SECTOR)

    def put(self, lba, data):
        self.data[lba * SECTOR:lba * SECTOR + len(data)] = data

    def bytes(self):
        return bytes(self.data)


# --- PARAM.SFO -------------------------------------------------------------

def param_sfo(fields=PS3_SFO_FIELDS):
    """A PSF the way the PS3 writes one: header, index, keys, then data."""
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
            # The console rounds the reserved size up; the used length is the
            # honest one and is what a reader must trust.
            maximum = (len(encoded) + 3) & ~3
            fmt, raw = 0x0204, encoded
        entries.append((key_offset, fmt, len(raw), maximum, len(values)))
        values += _pad(raw, maximum)
    while len(keys) % 4:
        keys += b"\x00"
    key_start = 20 + 16 * len(entries)
    data_start = key_start + len(keys)
    for entry in entries:
        index += struct.pack("<HHIII", *entry)
    header = struct.pack("<4sIIII", b"\x00PSF", 0x00000101, key_start,
                         data_start, len(entries))
    return bytes(header + index + keys + values)


# --- ISO 9660 --------------------------------------------------------------

def _dir_record(name, lba, length, is_dir):
    if name in (b"\x00", b"\x01"):
        raw = name
    else:
        raw = name.encode("ascii") + (b"" if is_dir else b";1")
    record_length = 33 + len(raw)
    if record_length % 2:
        record_length += 1
    record = bytearray(record_length)
    record[0] = record_length
    record[1] = 0
    record[2:10] = _both32(lba)
    record[10:18] = _both32(length)
    record[18:25] = bytes((126, 9, 14, 12, 0, 0, 0))     # 2026-09-14 12:00 UTC
    record[25] = 0x02 if is_dir else 0x00
    record[28:32] = _both16(1)
    record[32] = len(raw)
    record[33:33 + len(raw)] = raw
    return bytes(record)


def _directory(entries, self_lba, self_length, parent_lba, parent_length):
    out = bytearray()
    out += _dir_record(b"\x00", self_lba, self_length, True)
    out += _dir_record(b"\x01", parent_lba, parent_length, True)
    for name, lba, length, is_dir in entries:
        out += _dir_record(name, lba, length, is_dir)
    return _pad(bytes(out), SECTOR)


def _primary_volume_descriptor(sectors, root_record, volume_id="PS3VOLUME"):
    descriptor = bytearray(SECTOR)
    descriptor[0] = 1
    descriptor[1:6] = b"CD001"
    descriptor[6] = 1
    descriptor[8:40] = _pad(b"PLAYSTATION3", 32, b" ")
    descriptor[40:72] = _pad(volume_id.encode("ascii"), 32, b" ")
    descriptor[80:88] = _both32(sectors)
    descriptor[120:124] = _both16(1)
    descriptor[124:128] = _both16(1)
    descriptor[128:132] = _both16(SECTOR)
    descriptor[156:156 + len(root_record)] = root_record
    for start, end in ((190, 318), (318, 446), (446, 574), (574, 702),
                       (702, 739), (739, 776), (776, 814)):
        descriptor[start:end] = b" " * (end - start)
    descriptor[881] = 1
    return bytes(descriptor)


def _terminator():
    descriptor = bytearray(SECTOR)
    descriptor[0] = 255
    descriptor[1:6] = b"CD001"
    descriptor[6] = 1
    return bytes(descriptor)


def _recognition(identifier):
    """One BEA01/NSR02/TEA01 structure of the UDF recognition sequence."""
    descriptor = bytearray(SECTOR)
    descriptor[0] = 0
    descriptor[1:6] = identifier
    descriptor[6] = 1
    return bytes(descriptor)


def build_ps3_bridge(pad_to=None, sfo=None):
    """A PS3 disc the way a dumper hands it over: ISO 9660 plus the UDF
    recognition sequence, PARAM.SFO under PS3_GAME.

    Real PS3 discs are UDF Bridge, so both filesystems are present and the ISO
     9660 one is the cheap way in. Only the recognition sequence of the UDF
    side is reproduced here; build_ps3_udf covers the UDF walk itself.
    """
    sfo = sfo if sfo is not None else param_sfo()
    root_lba, game_lba, sfo_lba = 22, 23, 24
    sfo_sectors = max(1, (len(sfo) + SECTOR - 1) // SECTOR)
    total = sfo_lba + sfo_sectors
    image = Image(total)
    root = _directory([("PS3_GAME", game_lba, SECTOR, True)],
                      root_lba, SECTOR, root_lba, SECTOR)
    game = _directory([("PARAM.SFO", sfo_lba, len(sfo), False)],
                      game_lba, SECTOR, root_lba, SECTOR)
    image.put(16, _primary_volume_descriptor(
        total, _dir_record(b"\x00", root_lba, SECTOR, True)))
    image.put(17, _terminator())
    image.put(18, _recognition(b"BEA01"))
    image.put(19, _recognition(b"NSR02"))
    image.put(20, _recognition(b"TEA01"))
    image.put(root_lba, root)
    image.put(game_lba, game)
    image.put(sfo_lba, sfo)
    data = image.bytes()
    if pad_to and pad_to > len(data):
        data += b"\x00" * (pad_to - len(data))
    return data


def build_ps2(pad_to=None, cnf=PS2_SYSTEM_CNF):
    """A PS2 disc: plain ISO 9660, SYSTEM.CNF in the root."""
    root_lba, cnf_lba = 20, 21
    image = Image(cnf_lba + 1)
    root = _directory([("SYSTEM.CNF", cnf_lba, len(cnf), False)],
                      root_lba, SECTOR, root_lba, SECTOR)
    image.put(16, _primary_volume_descriptor(
        cnf_lba + 1, _dir_record(b"\x00", root_lba, SECTOR, True),
        volume_id="PS2DISC"))
    image.put(17, _terminator())
    image.put(root_lba, root)
    image.put(cnf_lba, cnf)
    data = image.bytes()
    if pad_to and pad_to > len(data):
        data += b"\x00" * (pad_to - len(data))
    return data


# --- UDF -------------------------------------------------------------------

def _crc16(data):
    """The CRC-ITU-T UDF puts in every descriptor tag."""
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 \
                else (crc << 1) & 0xFFFF
    return crc


def _tag(identifier, location, body):
    tag = bytearray(16)
    struct.pack_into("<HHBBHHHI", tag, 0, identifier, 3, 0, 0, 1,
                     _crc16(body), len(body), location)
    tag[4] = (sum(tag[:4]) + sum(tag[5:16])) % 256
    return bytes(tag) + body


def _regid(identifier, flags=0):
    return bytes((flags,)) + _pad(identifier, 23) + b"\x00" * 8


def _charspec():
    return b"\x00" + _pad(b"OSTA Compressed Unicode", 63)


def _dstring(value, length):
    encoded = b"\x08" + value.encode("ascii")
    out = bytearray(_pad(encoded, length))
    out[length - 1] = len(encoded)
    return bytes(out)


def _timestamp():
    return struct.pack("<HHBBBBBBBBBB", 0x1000, 2026, 9, 14, 12, 0, 0,
                       0, 0, 0, 0, 0)[:12]


def _long_ad(length, lbn, partition=0):
    return struct.pack("<IIH", length, lbn, partition) + b"\x00" * 6


def _short_ad(length, position):
    return struct.pack("<II", length, position)


def _partition_descriptor(start, length):
    body = bytearray(512 - 16)
    struct.pack_into("<I", body, 0, 1)                       # sequence number
    struct.pack_into("<H", body, 4, 1)                       # allocated
    struct.pack_into("<H", body, 6, 0)                       # partition 0
    body[8:40] = _regid(b"+NSR03", 2)
    struct.pack_into("<I", body, 168, 1)                     # read only
    struct.pack_into("<I", body, 172, start)
    struct.pack_into("<I", body, 176, length)
    body[180:212] = _regid(b"*ps3-diag fixture", 0)
    return _tag(5, 0, bytes(body))


def _logical_volume_descriptor(fileset_lbn):
    body = bytearray(440 + 6 - 16)
    struct.pack_into("<I", body, 0, 1)
    body[4:68] = _charspec()
    body[68:196] = _dstring("PS3VOLUME", 128)
    struct.pack_into("<I", body, 196, SECTOR)
    body[200:232] = _regid(b"*OSTA UDF Compliant", 0)
    body[232:248] = _long_ad(SECTOR, fileset_lbn)
    struct.pack_into("<I", body, 248, 6)                     # map table length
    struct.pack_into("<I", body, 252, 1)                     # one map
    body[256:288] = _regid(b"*ps3-diag fixture", 0)
    body[424:430] = struct.pack("<BBHH", 1, 6, 1, 0)         # type 1 map
    return _tag(6, 0, bytes(body))


def _terminating_descriptor():
    return _tag(8, 0, bytes(512 - 16))


def _file_set_descriptor(root_lbn):
    body = bytearray(512 - 16)
    body[0:12] = _timestamp()
    struct.pack_into("<HH", body, 12, 3, 3)
    struct.pack_into("<II", body, 16, 1, 1)
    struct.pack_into("<II", body, 24, 0, 0)
    body[32:96] = _charspec()
    body[96:224] = _dstring("PS3VOLUME", 128)
    body[224:288] = _charspec()
    body[288:320] = _dstring("PS3VOLUME", 32)
    body[384:400] = _long_ad(SECTOR, root_lbn)
    body[400:432] = _regid(b"*OSTA UDF Compliant", 0)
    return _tag(256, 0, bytes(body))


def _file_entry(file_type, length, data_lbn, unique_id):
    """A plain File Entry with one short allocation descriptor."""
    body = bytearray(176 - 16 + 8)
    struct.pack_into("<H", body, 4, 4)                       # strategy 4
    struct.pack_into("<H", body, 8, 1)
    body[11] = file_type                                     # 4 dir, 5 file
    struct.pack_into("<H", body, 18, 0)                      # short_ad
    struct.pack_into("<II", body, 20, 0xFFFFFFFF, 0xFFFFFFFF)
    struct.pack_into("<I", body, 28, 0o444)
    struct.pack_into("<H", body, 32, 1)
    struct.pack_into("<Q", body, 40, length)
    struct.pack_into("<Q", body, 48, (length + SECTOR - 1) // SECTOR)
    body[56:68] = _timestamp()
    body[68:80] = _timestamp()
    body[80:92] = _timestamp()
    struct.pack_into("<I", body, 92, 1)
    body[112:144] = _regid(b"*ps3-diag fixture", 0)
    struct.pack_into("<Q", body, 144, unique_id)
    struct.pack_into("<I", body, 152, 0)                     # no attributes
    struct.pack_into("<I", body, 156, 8)                     # one short_ad
    body[160:168] = _short_ad(length, data_lbn)
    return _tag(261, 0, bytes(body))


def _file_identifier(name, icb_lbn, is_dir, parent=False):
    raw = b"" if parent else b"\x08" + name.encode("ascii")
    body = bytearray(38 - 16)
    struct.pack_into("<H", body, 0, 1)                       # version 1
    body[2] = (0x08 if parent else 0) | (0x02 if is_dir else 0)
    body[3] = len(raw)
    body[4:20] = _long_ad(SECTOR, icb_lbn)
    struct.pack_into("<H", body, 20, 0)
    descriptor = _tag(257, 0, bytes(body) + raw)
    return descriptor + b"\x00" * (-len(descriptor) % 4)


def build_ps3_udf(pad_to=None, sfo=None):
    """A PS3 image with no ISO 9660 filesystem at all, only UDF.

    Not what a retail disc looks like, which is the point: it is what is left
    when a tool rebuilds an image UDF-only, and it is the only thing that
    exercises the UDF walker.
    """
    sfo = sfo if sfo is not None else param_sfo()
    vds_lba = 32
    partition_start = 270
    # Partition relative: 0 file set, 1 root ICB, 2 root data, 3 PS3_GAME ICB,
    # 4 PS3_GAME data, 5 PARAM.SFO ICB, 6 PARAM.SFO data.
    image = Image(partition_start + 8)
    image.put(16, _recognition(b"BEA01"))
    image.put(17, _recognition(b"NSR02"))
    image.put(18, _recognition(b"TEA01"))
    image.put(vds_lba, _partition_descriptor(partition_start, 8))
    image.put(vds_lba + 1, _logical_volume_descriptor(0))
    image.put(vds_lba + 2, _terminating_descriptor())

    anchor = bytearray(32 - 16)
    struct.pack_into("<II", anchor, 0, 3 * SECTOR, vds_lba)
    image.put(256, _tag(2, 256, bytes(anchor) + b"\x00" * 480))

    image.put(partition_start + 0, _file_set_descriptor(1))
    root = (_file_identifier("", 1, True, parent=True)
            + _file_identifier("PS3_GAME", 3, True))
    game = (_file_identifier("", 1, True, parent=True)
            + _file_identifier("PARAM.SFO", 5, False))
    image.put(partition_start + 1, _file_entry(4, len(root), 2, 1))
    image.put(partition_start + 2, root)
    image.put(partition_start + 3, _file_entry(4, len(game), 4, 2))
    image.put(partition_start + 4, game)
    image.put(partition_start + 5, _file_entry(5, len(sfo), 6, 3))
    image.put(partition_start + 6, sfo)
    data = image.bytes()
    if pad_to and pad_to > len(data):
        data += b"\x00" * (pad_to - len(data))
    return data


# --- the awkward ones ------------------------------------------------------

def build_truncated(keep=40 * 1024):
    """A PS3 image that stops after its volume descriptors.

    The commonest real shape of this: a copy that was interrupted, so the
    header parses, promises a directory at sector 22, and the file ends first.
    """
    return build_ps3_bridge()[:keep]


def build_not_an_iso(size=64 * 1024):
    """Something with .iso on the end that is not an image.

    Zero filled rather than random on purpose: a parser that finds structure in
    zeros will find it in anything.
    """
    return b"\x00" * size


BUILDERS = {
    "ps3-bridge.iso": build_ps3_bridge,
    "ps3-udf.iso": build_ps3_udf,
    "ps2-iso9660.iso": build_ps2,
    "truncated.iso": build_truncated,
    "not-an-iso.iso": build_not_an_iso,
}


def write_all(directory=None):
    directory = directory or os.path.dirname(os.path.abspath(__file__))
    written = []
    for name, builder in BUILDERS.items():
        path = os.path.join(directory, name)
        with open(path, "wb") as handle:
            handle.write(builder())
        written.append((name, os.path.getsize(path)))
    return written


if __name__ == "__main__":
    for name, size in write_all():
        print(f"{name:20s} {size:>9d} bytes")
