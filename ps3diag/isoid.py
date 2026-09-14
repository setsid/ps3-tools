"""What a disc image says it is, read out of the image itself.

A filename is a claim. It is typed by whoever dumped the disc, it survives
every rename, and on a drive holding forty ISOs it is wrong often enough that
"the console will not start BLES01428" and "the file called BLES01428 is not
BLES01428" are routinely the same fault. The identity that settles it is the
one inside the image: PS3_GAME/PARAM.SFO on a PS3 disc, SYSTEM.CNF on a PS2
one.

Nothing here opens a socket or a file. The walker is handed a read_range
callback and asks for the ranges it wants, so whoever supplies that callback
decides where the bytes come from and what fetching them costs. That is what
makes a 40 GB image identifiable from a few tens of kilobytes: only the sectors
about to be parsed are ever asked for, and a caller that cannot supply a range
just returns short bytes and gets a reason back instead of an exception.

Nothing here raises. A truncated image, a file that is not an image at all, and
a layout this does not recognise all come back as an identity with title_id
None and a reason saying what stopped it, because a helper can work with "the
image ends before its directory" and cannot work with a traceback.

Two filesystems are walked. ISO 9660 is the one that answers in practice: a PS3
game disc is UDF Bridge, so the ISO 9660 filesystem is present alongside the
UDF one and is far cheaper to walk. The UDF walker exists for images that carry
only UDF, and is deliberately minimal.
"""

import re
import struct
from dataclasses import dataclass, field

SECTOR = 2048

# ISO 9660 puts its volume descriptors at 32 KB, one per sector, terminated by
# a type 255 descriptor. UDF's anchor sits at sector 256 by definition.
VOLUME_DESCRIPTOR_LBA = 16
MAX_VOLUME_DESCRIPTORS = 16
UDF_ANCHOR_LBA = 256
MAX_VDS_SECTORS = 32
MAX_UDF_EXTENTS = 16

# Caps, not budgets. The budget belongs to whoever supplies read_range; these
# only stop a corrupt length field turning into a request for four gigabytes.
MAX_READ = 1 << 20
MAX_DIRECTORY_BYTES = 64 * 1024
MAX_SFO_BYTES = 32 * 1024
MAX_CNF_BYTES = 8 * 1024
MAX_SFO_ENTRIES = 1024

SFO_MAGIC = b"\x00PSF"

PS3_SFO_PATH = ("PS3_GAME", "PARAM.SFO")
PS2_CNF_PATH = ("SYSTEM.CNF",)

# Kept out of fields and promoted to their own attribute. Everything else the
# SFO carries stays in fields so a helper can see it without another read.
PROMOTED_KEYS = ("TITLE_ID", "TITLE", "APP_VER", "CATEGORY", "PS3_SYSTEM_VER")

METHOD_NONE = "none"
METHOD_SFO = "param_sfo"
METHOD_CNF = "system_cnf"


@dataclass
class IsoIdentity:
    """What the image claims. Every field bar method may be None."""

    method: str = METHOD_NONE
    title_id: str = None
    title: str = None
    app_version: str = None
    category: str = None
    ps3_system_ver: str = None
    filesystem: str = None
    reason: str = None
    fields: dict = field(default_factory=dict)

    @property
    def identified(self):
        return self.title_id is not None

    def to_dict(self):
        return {
            "method": self.method,
            "title_id": self.title_id,
            "title": self.title,
            "app_version": self.app_version,
            "category": self.category,
            "ps3_system_ver": self.ps3_system_ver,
            "filesystem": self.filesystem,
            "reason": self.reason,
        }


# --- the read_range contract ----------------------------------------------

class _Reader:
    """Wraps the caller's read_range so the walkers can be written naively.

    Short reads and read failures are recorded rather than propagated, which is
    what lets "the image stops here" be told apart from "this is not an image".
    """

    def __init__(self, read_range):
        self._read_range = read_range
        self.short = False
        self.error = None

    def __call__(self, offset, length):
        if offset < 0 or length <= 0:
            return b""
        length = min(length, MAX_READ)
        try:
            data = self._read_range(offset, length)
        except Exception as exc:                      # noqa: BLE001
            self.error = str(exc) or exc.__class__.__name__
            self.short = True
            return b""
        if not isinstance(data, (bytes, bytearray, memoryview)):
            self.error = "read_range did not return bytes"
            self.short = True
            return b""
        data = bytes(data)[:length]
        if len(data) < length:
            self.short = True
        return data


def _u16(data, offset):
    return struct.unpack_from("<H", data, offset)[0]


def _u32(data, offset):
    return struct.unpack_from("<I", data, offset)[0]


# --- ISO 9660 --------------------------------------------------------------

_VERSION_SUFFIX = re.compile(r";\d+$")


def _normalise_name(name):
    """ISO 9660 stores FILE.EXT;1 and pads names; compare on the bare name."""
    name = _VERSION_SUFFIX.sub("", name.strip())
    return name.rstrip(".").upper()


def _directory_records(data):
    position = 0
    limit = len(data)
    while position < limit:
        length = data[position]
        if length == 0:
            # Records never straddle a sector, so a zero length byte means the
            # rest of this sector is padding rather than the end of the
            # directory.
            position = (position // SECTOR + 1) * SECTOR
            continue
        if length < 33 or position + length > limit:
            return
        yield data[position:position + length]
        position += length


class _Iso9660:
    def __init__(self, read, root_record, joliet=False):
        self.read = read
        self.name = "joliet" if joliet else "iso9660"
        self._joliet = joliet
        self.root = (_u32(root_record, 2), _u32(root_record, 10), True)

    def _decode(self, raw):
        if self._joliet:
            return raw.decode("utf-16-be", errors="replace")
        return raw.decode("ascii", errors="replace")

    def _extent(self, lba, length):
        if lba <= 0 or length <= 0:
            return b""
        return self.read(lba * SECTOR, length)

    def _child(self, data, wanted):
        for record in _directory_records(data):
            name_length = record[32]
            if name_length == 0 or 33 + name_length > len(record):
                continue
            raw = record[33:33 + name_length]
            if raw in (b"\x00", b"\x01"):
                continue                      # "." and ".."
            if _normalise_name(self._decode(raw)) != wanted:
                continue
            return (_u32(record, 2), _u32(record, 10),
                    bool(record[25] & 0x02))
        return None

    def open(self, parts, cap):
        """Bytes of one file by path, or None when it is not there."""
        lba, length, _is_dir = self.root
        for index, part in enumerate(parts):
            want_directory = index < len(parts) - 1
            data = self._extent(lba, min(length, MAX_DIRECTORY_BYTES))
            if not data:
                return None
            found = self._child(data, part.upper())
            if found is None:
                return None
            lba, length, is_directory = found
            if is_directory != want_directory:
                return None
        return self._extent(lba, min(length, cap)) or None


def _iso9660_filesystems(read, notes):
    """Every CD001 filesystem in the image, primary first, then any Joliet."""
    sectors = read(VOLUME_DESCRIPTOR_LBA * SECTOR,
                   SECTOR * MAX_VOLUME_DESCRIPTORS)
    if len(sectors) < SECTOR:
        notes.append("the image ends before its volume descriptors")
        return
    if sectors[1:6] != b"CD001":
        notes.append("no ISO 9660 volume descriptor at sector 16")
        return
    found = 0
    for index in range(len(sectors) // SECTOR):
        descriptor = sectors[index * SECTOR:(index + 1) * SECTOR]
        if descriptor[1:6] != b"CD001":
            continue
        kind = descriptor[0]
        if kind == 255:
            break
        if kind not in (1, 2):
            continue
        # A supplementary descriptor is Joliet when its escape sequence is one
        # of the three UCS-2 levels. Anything else is left alone.
        joliet = kind == 2 and descriptor[88:91] in (b"%/@", b"%/C", b"%/E")
        if kind == 2 and not joliet:
            continue
        found += 1
        yield _Iso9660(read, descriptor[156:190], joliet=joliet)
    if not found:
        notes.append("the ISO 9660 descriptors carry no usable filesystem")


# --- UDF -------------------------------------------------------------------

def _tag_ok(data, identifier):
    """Descriptor tag check: the identifier and the one-byte checksum.

    The CRC over the descriptor body is not checked. It would catch corruption
    this does not, but it is not what tells a UDF descriptor from a stretch of
    a game's texture data, and the checksum plus the identifier already do
    that.
    """
    if len(data) < 16 or _u16(data, 0) != identifier:
        return False
    total = sum(data[:4]) + sum(data[5:16])
    return total % 256 == data[4]


class _Udf:
    name = "udf"

    def __init__(self, read, partition_start, root_lbn):
        self.read = read
        self.partition_start = partition_start
        self._root_lbn = root_lbn

    def _sector(self, lbn):
        return self.read((self.partition_start + lbn) * SECTOR, SECTOR)

    def _entry(self, lbn, cap):
        """(file_type, contents) for one ICB, or (None, b"")."""
        data = self._sector(lbn)
        if _tag_ok(data, 261):
            extended = False
        elif _tag_ok(data, 266):
            extended = True
        else:
            return None, b""
        file_type = data[16 + 11]
        flags = _u16(data, 16 + 18)
        base = 216 if extended else 176
        attribute_length = _u32(data, base - 8)
        descriptor_length = _u32(data, base - 4)
        start = base + attribute_length
        if start + descriptor_length > len(data):
            return file_type, b""
        descriptors = data[start:start + descriptor_length]
        if flags & 0x07 == 3:
            # The file is small enough to live inside its own ICB.
            return file_type, descriptors[:cap]
        stride = 16 if flags & 0x07 == 1 else 8
        out = bytearray()
        for index in range(min(len(descriptors) // stride, MAX_UDF_EXTENTS)):
            chunk = descriptors[index * stride:(index + 1) * stride]
            raw_length = _u32(chunk, 0)
            if raw_length >> 30:
                break                      # not recorded: a hole, so stop
            length = raw_length & 0x3FFFFFFF
            if length == 0:
                break
            position = _u32(chunk, 4)
            wanted = min(length, cap - len(out))
            if wanted <= 0:
                break
            out += self.read((self.partition_start + position) * SECTOR,
                             wanted)
            if len(out) >= cap:
                break
        return file_type, bytes(out)

    @staticmethod
    def _child(data, wanted):
        position = 0
        while position + 38 <= len(data):
            if not _tag_ok(data[position:position + 16], 257):
                return None
            characteristics = data[position + 18]
            name_length = data[position + 19]
            implementation_length = _u16(data, position + 36)
            start = position + 38 + implementation_length
            raw = data[start:start + name_length]
            length = (38 + implementation_length + name_length + 3) & ~3
            if raw and not characteristics & 0x08:
                if raw[0] == 16:
                    name = raw[1:].decode("utf-16-be", errors="replace")
                else:
                    name = raw[1:].decode("latin-1", errors="replace")
                if _normalise_name(name) == wanted:
                    return _u32(data, position + 24)
            position += length
        return None

    def open(self, parts, cap):
        lbn = self._root_lbn
        for index, part in enumerate(parts):
            want_directory = index < len(parts) - 1
            file_type, data = self._entry(lbn, MAX_DIRECTORY_BYTES)
            if file_type != 4 or not data:
                return None
            found = self._child(data, part.upper())
            if found is None:
                return None
            lbn = found
            if not want_directory:
                file_type, data = self._entry(lbn, cap)
                return data or None
        return None


def _udf_filesystem(read, notes):
    anchor = read(UDF_ANCHOR_LBA * SECTOR, SECTOR)
    if not _tag_ok(anchor, 2):
        notes.append("no UDF anchor descriptor at sector 256")
        return None
    location = _u32(anchor, 20)
    count = min(max(_u32(anchor, 16) // SECTOR, 1), MAX_VDS_SECTORS)
    partition_start = None
    fileset_lbn = None
    for index in range(count):
        descriptor = read((location + index) * SECTOR, SECTOR)
        if len(descriptor) < SECTOR:
            break
        identifier = _u16(descriptor, 0)
        if identifier == 8:
            break                                  # terminating descriptor
        if identifier == 5 and _tag_ok(descriptor, 5):
            partition_start = _u32(descriptor, 188)
        elif identifier == 6 and _tag_ok(descriptor, 6):
            fileset_lbn = _u32(descriptor, 252)
    if partition_start is None or fileset_lbn is None:
        notes.append("the UDF volume descriptor sequence is incomplete")
        return None
    fileset = read((partition_start + fileset_lbn) * SECTOR, SECTOR)
    if not _tag_ok(fileset, 256):
        notes.append("the UDF file set descriptor is missing")
        return None
    return _Udf(read, partition_start, _u32(fileset, 404))


# --- PARAM.SFO -------------------------------------------------------------

def parse_param_sfo(data):
    """(fields, reason). Fields is whatever parsed, even on a partial read."""
    if data is None:
        return {}, "PARAM.SFO could not be read"
    if len(data) < 20:
        return {}, "PARAM.SFO is shorter than its own header"
    if data[:4] != SFO_MAGIC:
        return {}, "PARAM.SFO does not start with the PSF magic"
    _version, key_start, data_start, count = struct.unpack_from("<IIII",
                                                                data, 4)
    if count > MAX_SFO_ENTRIES:
        return {}, f"PARAM.SFO claims {count} entries, which is not credible"
    out = {}
    reason = None
    for index in range(count):
        entry = 20 + index * 16
        if entry + 16 > len(data):
            reason = "PARAM.SFO is truncated part way through its index table"
            break
        key_offset, fmt, used, _maximum, data_offset = struct.unpack_from(
            "<HHIII", data, entry)
        key_at = key_start + key_offset
        end = data.find(b"\x00", key_at)
        if key_at >= len(data) or end < 0:
            reason = "PARAM.SFO is truncated part way through its key table"
            break
        key = data[key_at:end].decode("ascii", errors="replace")
        value_at = data_start + data_offset
        raw = data[value_at:value_at + used]
        if len(raw) < used:
            reason = "PARAM.SFO is truncated part way through its data table"
            if not raw:
                break
        if fmt == 0x0404:
            out[key] = (struct.unpack_from("<I", raw)[0]
                        if len(raw) >= 4 else None)
        elif fmt in (0x0004, 0x0204):
            out[key] = raw.split(b"\x00", 1)[0].decode("utf-8",
                                                       errors="replace")
        else:
            out[key] = raw.hex()
    if not out and reason is None:
        reason = "PARAM.SFO carries no entries"
    return out, reason


def _normalise_version(value):
    """01.24 as the SFO stores it, 1.24 as a person writes it."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    return re.sub(r"^0+(?=\d)", "", value)


# --- SYSTEM.CNF ------------------------------------------------------------

def parse_system_cnf(raw):
    """The handful of KEY = VALUE lines a PS2 disc boots from."""
    if raw is None:
        return {}
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8", errors="replace")
    out = {}
    for line in raw.splitlines():
        key, separator, value = line.partition("=")
        if not separator:
            continue
        key = key.strip().upper()
        if key:
            out[key] = value.strip().strip("\x00")
    return out


_BOOT_ID = re.compile(r"^([A-Z]{4})(\d{5})$")


def title_id_from_boot(value):
    """SLES_123.45 out of cdrom0:\\SLES_123.45;1, as SLES12345."""
    if not value:
        return None
    tail = re.split(r"[\\/:]", value.strip())[-1]
    tail = tail.split(";")[0]
    core = tail.replace("_", "").replace(".", "").replace("-", "").upper()
    match = _BOOT_ID.match(core)
    return match.group(1) + match.group(2) if match else None


# --- the walk itself -------------------------------------------------------

def identify(read_range):
    """Identity of the image read_range reads, never raising.

    read_range(offset, length) returns up to length bytes at offset. It may
    return fewer, or nothing at all, and that is treated as the end of what is
    readable rather than as an error.
    """
    read = _Reader(read_range)
    try:
        return _identify(read)
    except Exception as exc:                          # noqa: BLE001
        return IsoIdentity(reason=f"the image could not be walked: {exc}")


def identify_bytes(data):
    """identify() over a buffer already in memory. Used by the tests."""
    data = bytes(data or b"")

    def read_range(offset, length):
        return data[offset:offset + length]

    return identify(read_range)


def _filesystems(read, notes):
    """Every filesystem in the image, cheapest first and lazily.

    Laziness is the whole point on a bridge disc: the UDF anchor lives half a
    megabyte in, and asking for it after ISO 9660 has already answered doubles
    what the console is made to serve for nothing.
    """
    for filesystem in _iso9660_filesystems(read, notes):
        yield filesystem
    udf = _udf_filesystem(read, notes)
    if udf is not None:
        yield udf


def _identify(read):
    notes = []
    tried = []
    for filesystem in _filesystems(read, notes):
        tried.append(filesystem)
        sfo = filesystem.open(PS3_SFO_PATH, MAX_SFO_BYTES)
        if sfo:
            return _from_sfo(sfo, filesystem.name)
        cnf = filesystem.open(PS2_CNF_PATH, MAX_CNF_BYTES)
        if cnf:
            return _from_cnf(cnf, filesystem.name)
    return IsoIdentity(filesystem=tried[0].name if tried else None,
                       reason=_no_identity_reason(read, tried, notes))


def _no_identity_reason(read, filesystems, notes):
    if read.error:
        return f"the image could not be read: {read.error}"
    if filesystems:
        if read.short:
            return ("the image ends before PS3_GAME/PARAM.SFO or SYSTEM.CNF "
                    "could be reached")
        return "the image holds neither PS3_GAME/PARAM.SFO nor SYSTEM.CNF"
    # The walkers note why they gave up in the order they were tried, and the
    # first note is the one about sector 16, which is the useful one: whether
    # this is an image at all is decided there.
    return notes[0] if notes else "the image is not ISO 9660 or UDF"


def _from_sfo(data, filesystem):
    fields, reason = parse_param_sfo(data)
    identity = IsoIdentity(filesystem=filesystem, fields=fields,
                           reason=reason)
    title_id = fields.get("TITLE_ID")
    if isinstance(title_id, str) and title_id.strip():
        identity.method = METHOD_SFO
        identity.title_id = title_id.strip().upper()
    elif reason is None:
        identity.reason = "PARAM.SFO carries no TITLE_ID"
    identity.title = (fields.get("TITLE") or None)
    identity.app_version = _normalise_version(fields.get("APP_VER"))
    identity.category = (fields.get("CATEGORY") or None)
    identity.ps3_system_ver = _normalise_version(
        fields.get("PS3_SYSTEM_VER")) or None
    return identity


def _from_cnf(data, filesystem):
    fields = parse_system_cnf(data)
    boot = fields.get("BOOT2") or fields.get("BOOT")
    title_id = title_id_from_boot(boot)
    identity = IsoIdentity(filesystem=filesystem, fields=fields)
    if title_id:
        identity.method = METHOD_CNF
        identity.title_id = title_id
        identity.app_version = _normalise_version(fields.get("VER"))
    elif boot:
        identity.reason = (f"SYSTEM.CNF boots {boot!r}, which carries no "
                           f"title ID")
    else:
        identity.reason = "SYSTEM.CNF has no BOOT2 line"
    return identity
