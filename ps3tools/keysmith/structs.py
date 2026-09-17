"""The on-disk structures of an SCE file, read and written as big-endian.

Field names follow scetool's own, because the point of comparison for every
figure in here is what scetool prints. Where a field's meaning is not known it
is named for its offset and carried through unchanged rather than guessed at:
an unknown that is preserved costs nothing, and an unknown that is invented is
a file a console refuses.

Everything here is a plain reader or writer over bytes. Nothing in this module
decrypts, so a file can be described before any key is in hand.
"""

import struct

from .errors import NotAnSce, TruncatedFile, UnsupportedSce

SCE_MAGIC = 0x53434500                                  # "SCE\0"

# sce_header.header_type
SCE_HEADER_SELF = 1
SCE_HEADER_RVK = 2
SCE_HEADER_PKG = 3
SCE_HEADER_SPP = 4

SCE_HEADER_NAMES = {
    SCE_HEADER_SELF: "SELF",
    SCE_HEADER_RVK: "RVK",
    SCE_HEADER_PKG: "PKG",
    SCE_HEADER_SPP: "SPP",
}

# self_header.header_type. 1 is a signed SELF; 3 is what retail application
# SELFs carry. Both are read the same way.
SELF_TYPE_LV0 = 1
SELF_TYPE_APP = 3

# app_info.self_type
SELF_TYPES = {
    1: "LV0",
    2: "LV1",
    3: "LV2",
    4: "APP",
    5: "ISO",
    6: "LDR",
    7: "UNK_7",
    8: "NPDRM",
}

# control_info.type
CONTROL_FLAGS = 1
CONTROL_DIGEST = 2
CONTROL_NPDRM = 3

# npdrm_info.licence_type. scetool spells these -b FREE and -b LOCAL, which
# says nothing about which number it writes, so both are spelled out here.
LICENCE_NETWORK = 1
LICENCE_LOCAL = 2
LICENCE_FREE = 3

LICENCE_NAMES = {
    LICENCE_NETWORK: "NETWORK",
    LICENCE_LOCAL: "LOCAL",
    LICENCE_FREE: "FREE",
}

# npdrm_info.app_type. scetool spells these -c EXEC, -c UPDATE and so on.
APP_TYPES = {
    1: "SPRX",
    2: "EXEC",
    3: "USPRX",
    4: "UEXEC",
    5: "SC_EXEC",
    0x20: "USPRX",
}

NPDRM_MAGIC = 0x4E504400                                # "NPD\0"

# metadata_section_header.type
SECTION_TYPE_SHDR = 1
SECTION_TYPE_PHDR = 2
SECTION_TYPE_SCEV = 3

# section_info.encrypted / metadata_section_header.encrypted use different
# numbers for the same idea, which is a trap worth naming rather than tidying.
SECTION_INFO_ENCRYPTED = 1
SECTION_INFO_PLAIN = 2
METADATA_ENCRYPTED = 3
METADATA_PLAIN = 1

COMPRESSED = 2
UNCOMPRESSED = 1


def _unpack(fmt, data, offset, path, field):
    size = struct.calcsize(fmt)
    if offset < 0 or offset + size > len(data):
        raise TruncatedFile(
            f"needs {size} bytes at 0x{offset:X} and the file is "
            f"{len(data)} bytes", path=path, field=field)
    return struct.unpack_from(fmt, data, offset)


class SceHeader:
    """The 0x20 bytes every SCE file opens with."""

    SIZE = 0x20
    FORMAT = ">IIHHIQQ"

    def __init__(self, magic, version, key_revision, header_type,
                 metadata_offset, header_length, data_length):
        self.magic = magic
        self.version = version
        self.key_revision = key_revision
        self.header_type = header_type
        self.metadata_offset = metadata_offset
        self.header_length = header_length
        self.data_length = data_length

    @classmethod
    def read(cls, data, offset=0, path=""):
        values = _unpack(cls.FORMAT, data, offset, path, "SCE header")
        head = cls(*values)
        if head.magic != SCE_MAGIC:
            raise NotAnSce("not an SCE file", path=path, field="magic",
                           expected=SCE_MAGIC, found=head.magic)
        if head.version != 2:
            raise UnsupportedSce("unknown SCE version", path=path,
                                 field="version", expected=2,
                                 found=head.version)
        return head

    def pack(self):
        return struct.pack(self.FORMAT, self.magic, self.version,
                           self.key_revision, self.header_type,
                           self.metadata_offset, self.header_length,
                           self.data_length)

    @property
    def type_name(self):
        return SCE_HEADER_NAMES.get(self.header_type,
                                    f"UNKNOWN_{self.header_type}")


class SelfHeader:
    """The 0x50 bytes that say where everything else in a SELF lives."""

    SIZE = 0x50
    FORMAT = ">QQQQQQQQQQ"

    def __init__(self, header_type, app_info_offset, elf_offset, phdr_offset,
                 shdr_offset, section_info_offset, sce_version_offset,
                 control_info_offset, control_info_size, padding):
        self.header_type = header_type
        self.app_info_offset = app_info_offset
        self.elf_offset = elf_offset
        self.phdr_offset = phdr_offset
        self.shdr_offset = shdr_offset
        self.section_info_offset = section_info_offset
        self.sce_version_offset = sce_version_offset
        self.control_info_offset = control_info_offset
        self.control_info_size = control_info_size
        self.padding = padding

    @classmethod
    def read(cls, data, offset, path=""):
        return cls(*_unpack(cls.FORMAT, data, offset, path, "SELF header"))

    def pack(self):
        return struct.pack(
            self.FORMAT, self.header_type, self.app_info_offset,
            self.elf_offset, self.phdr_offset, self.shdr_offset,
            self.section_info_offset, self.sce_version_offset,
            self.control_info_offset, self.control_info_size, self.padding)


class AppInfo:
    """Authentication ID, vendor, SELF type and version. 0x20 bytes."""

    SIZE = 0x20
    FORMAT = ">QIIQQ"

    def __init__(self, auth_id, vendor_id, self_type, version, padding):
        self.auth_id = auth_id
        self.vendor_id = vendor_id
        self.self_type = self_type
        self.version = version
        self.padding = padding

    @classmethod
    def read(cls, data, offset, path=""):
        return cls(*_unpack(cls.FORMAT, data, offset, path, "app info"))

    def pack(self):
        return struct.pack(self.FORMAT, self.auth_id, self.vendor_id,
                           self.self_type, self.version, self.padding)

    @property
    def type_name(self):
        return SELF_TYPES.get(self.self_type, f"UNKNOWN_{self.self_type}")

    @property
    def version_text(self):
        """The version as the four dotted parts scetool prints."""
        raw = self.version
        return "%02X.%02X.%02X.%02X" % ((raw >> 56) & 0xFF, (raw >> 48) & 0xFF,
                                        (raw >> 40) & 0xFF, (raw >> 32) & 0xFF)


class SectionInfo:
    """One entry per ELF program header. 0x20 bytes each.

    unknown1 and unknown2 have no documented meaning. They are read, printed
    and written back exactly as found.
    """

    SIZE = 0x20
    FORMAT = ">QQIIII"

    def __init__(self, offset, size, compressed, unknown1, unknown2,
                 encrypted):
        self.offset = offset
        self.size = size
        self.compressed = compressed
        self.unknown1 = unknown1
        self.unknown2 = unknown2
        self.encrypted = encrypted

    @classmethod
    def read(cls, data, offset, path=""):
        return cls(*_unpack(cls.FORMAT, data, offset, path, "section info"))

    def pack(self):
        return struct.pack(self.FORMAT, self.offset, self.size,
                           self.compressed, self.unknown1, self.unknown2,
                           self.encrypted)


class SceVersionInfo:
    """The SCE version block. 0x10 bytes.

    unknown3 is carried through unchanged. It is zero in every file in the
    corpus, which is not the same as knowing it must be.
    """

    SIZE = 0x10
    FORMAT = ">IIII"

    def __init__(self, header_type, present, size, unknown3):
        self.header_type = header_type
        self.present = present
        self.size = size
        self.unknown3 = unknown3

    @classmethod
    def read(cls, data, offset, path=""):
        return cls(*_unpack(cls.FORMAT, data, offset, path, "SCE version"))

    def pack(self):
        return struct.pack(self.FORMAT, self.header_type, self.present,
                           self.size, self.unknown3)


class ControlInfo:
    """One control info block: a 0x10 byte head and a payload.

    The payload is kept as raw bytes as well as parsed, because a block this
    does not understand still has to come back out byte for byte.
    """

    HEAD_SIZE = 0x10
    HEAD_FORMAT = ">IIQ"

    def __init__(self, info_type, size, next_block, payload):
        self.info_type = info_type
        self.size = size
        self.next = next_block
        self.payload = payload

    @classmethod
    def read(cls, data, offset, path=""):
        info_type, size, next_block = _unpack(cls.HEAD_FORMAT, data, offset,
                                              path, "control info")
        if size < cls.HEAD_SIZE:
            raise TruncatedFile(
                "control info block is shorter than its own header",
                path=path, field="control info size",
                expected=f">= 0x{cls.HEAD_SIZE:X}", found=size)
        end = offset + size
        if end > len(data):
            raise TruncatedFile("control info runs past the end of the file",
                                path=path, field="control info",
                                expected=end, found=len(data))
        payload = bytes(data[offset + cls.HEAD_SIZE:end])
        return cls(info_type, size, next_block, payload)

    def pack(self):
        return struct.pack(self.HEAD_FORMAT, self.info_type, self.size,
                           self.next) + self.payload

    @property
    def type_name(self):
        return {CONTROL_FLAGS: "control flags",
                CONTROL_DIGEST: "file digest",
                CONTROL_NPDRM: "NPDRM info"}.get(
                    self.info_type, f"unknown type {self.info_type}")


class NpdrmInfo:
    """The NPDRM control block, parsed out of a control info payload.

    This is the block TrueAncestor drops and the BO2 Eboot-Self Builder fills
    in wrongly. Losing it, or writing app type 0 into it, gives 8001000F on a
    console that checks licences, so every field here is preserved exactly.
    """

    SIZE = 0x80
    FORMAT = ">IIII48s16s16s16sQQ"

    def __init__(self, magic, version, licence_type, app_type, content_id,
                 digest, cid_fn_hash, header_hash, limited_time_start,
                 limited_time_end):
        self.magic = magic
        self.version = version
        self.licence_type = licence_type
        self.app_type = app_type
        self.content_id = content_id
        self.digest = digest
        self.cid_fn_hash = cid_fn_hash
        self.header_hash = header_hash
        self.limited_time_start = limited_time_start
        self.limited_time_end = limited_time_end

    @classmethod
    def read(cls, payload, path=""):
        values = _unpack(cls.FORMAT, payload, 0, path, "NPDRM info")
        info = cls(*values)
        if info.magic != NPDRM_MAGIC:
            raise UnsupportedSce("NPDRM block has the wrong magic", path=path,
                                 field="NPDRM magic", expected=NPDRM_MAGIC,
                                 found=info.magic)
        return info

    def pack(self):
        return struct.pack(self.FORMAT, self.magic, self.version,
                           self.licence_type, self.app_type, self.content_id,
                           self.digest, self.cid_fn_hash, self.header_hash,
                           self.limited_time_start, self.limited_time_end)

    @property
    def content_id_text(self):
        return self.content_id.split(b"\x00")[0].decode("ascii", "replace")

    @property
    def licence_name(self):
        return LICENCE_NAMES.get(self.licence_type,
                                 f"UNKNOWN_{self.licence_type}")

    @property
    def app_type_name(self):
        return APP_TYPES.get(self.app_type, f"UNKNOWN_{self.app_type}")


class MetadataInfo:
    """The 0x40 bytes of key material that sit at metadata_offset + 0x20.

    Encrypted with the per-revision key for a retail SELF, or derived from the
    klicensee for an NPDRM one. The two padding fields are checked after
    decryption: they are zero when the key was right, which is the only
    self-check the format offers.
    """

    SIZE = 0x40

    def __init__(self, key, key_pad, iv, iv_pad):
        self.key = key
        self.key_pad = key_pad
        self.iv = iv
        self.iv_pad = iv_pad

    @classmethod
    def read(cls, data, offset=0, path=""):
        if offset + cls.SIZE > len(data):
            raise TruncatedFile("metadata info runs past the end of the file",
                                path=path, field="metadata info",
                                expected=offset + cls.SIZE, found=len(data))
        chunk = bytes(data[offset:offset + cls.SIZE])
        return cls(chunk[0x00:0x10], chunk[0x10:0x20],
                   chunk[0x20:0x30], chunk[0x30:0x40])

    def pack(self):
        return self.key + self.key_pad + self.iv + self.iv_pad

    @property
    def looks_decrypted(self):
        return self.key_pad == b"\x00" * 16 and self.iv_pad == b"\x00" * 16


class MetadataHeader:
    """The 0x20 bytes describing how many sections and keys follow."""

    SIZE = 0x20
    FORMAT = ">QIIIIII"

    def __init__(self, signature_input_length, unknown1, section_count,
                 key_count, opt_header_size, unknown2, unknown3):
        self.signature_input_length = signature_input_length
        self.unknown1 = unknown1
        self.section_count = section_count
        self.key_count = key_count
        self.opt_header_size = opt_header_size
        self.unknown2 = unknown2
        self.unknown3 = unknown3

    @classmethod
    def read(cls, data, offset=0, path=""):
        return cls(*_unpack(cls.FORMAT, data, offset, path, "metadata header"))

    def pack(self):
        return struct.pack(self.FORMAT, self.signature_input_length,
                           self.unknown1, self.section_count, self.key_count,
                           self.opt_header_size, self.unknown2, self.unknown3)


class MetadataSection:
    """One 0x30 byte entry saying where a section's bytes are and how."""

    SIZE = 0x30
    FORMAT = ">QQIIIIIIII"

    def __init__(self, data_offset, data_size, section_type, index, hashed,
                 sha1_index, encrypted, key_index, iv_index, compressed):
        self.data_offset = data_offset
        self.data_size = data_size
        self.section_type = section_type
        self.index = index
        self.hashed = hashed
        self.sha1_index = sha1_index
        self.encrypted = encrypted
        self.key_index = key_index
        self.iv_index = iv_index
        self.compressed = compressed

    @classmethod
    def read(cls, data, offset, path=""):
        return cls(*_unpack(cls.FORMAT, data, offset, path,
                            "metadata section header"))

    def pack(self):
        return struct.pack(self.FORMAT, self.data_offset, self.data_size,
                           self.section_type, self.index, self.hashed,
                           self.sha1_index, self.encrypted, self.key_index,
                           self.iv_index, self.compressed)


class ElfHeader:
    """A 64-bit big-endian ELF header. The only kind a PS3 SELF carries."""

    SIZE = 0x40
    FORMAT = ">16sHHIQQQIHHHHHH"

    def __init__(self, ident, e_type, machine, version, entry, phoff, shoff,
                 flags, ehsize, phentsize, phnum, shentsize, shnum, shstrndx):
        self.ident = ident
        self.type = e_type
        self.machine = machine
        self.version = version
        self.entry = entry
        self.phoff = phoff
        self.shoff = shoff
        self.flags = flags
        self.ehsize = ehsize
        self.phentsize = phentsize
        self.phnum = phnum
        self.shentsize = shentsize
        self.shnum = shnum
        self.shstrndx = shstrndx

    @classmethod
    def read(cls, data, offset=0, path=""):
        head = cls(*_unpack(cls.FORMAT, data, offset, path, "ELF header"))
        if head.ident[:4] != b"\x7fELF":
            raise UnsupportedSce("not an ELF", path=path, field="ELF magic",
                                 expected=b"\x7fELF", found=head.ident[:4])
        if head.ident[4] != 2:
            raise UnsupportedSce("only 64-bit ELF is supported", path=path,
                                 field="EI_CLASS", expected=2,
                                 found=head.ident[4])
        if head.ident[5] != 2:
            raise UnsupportedSce("only big-endian ELF is supported", path=path,
                                 field="EI_DATA", expected=2,
                                 found=head.ident[5])
        return head

    def pack(self):
        return struct.pack(self.FORMAT, self.ident, self.type, self.machine,
                           self.version, self.entry, self.phoff, self.shoff,
                           self.flags, self.ehsize, self.phentsize, self.phnum,
                           self.shentsize, self.shnum, self.shstrndx)


class ProgramHeader:
    """A 64-bit ELF program header. 0x38 bytes."""

    SIZE = 0x38
    FORMAT = ">IIQQQQQQ"

    def __init__(self, p_type, flags, offset, vaddr, paddr, filesz, memsz,
                 align):
        self.type = p_type
        self.flags = flags
        self.offset = offset
        self.vaddr = vaddr
        self.paddr = paddr
        self.filesz = filesz
        self.memsz = memsz
        self.align = align

    @classmethod
    def read(cls, data, offset, path=""):
        return cls(*_unpack(cls.FORMAT, data, offset, path, "program header"))

    def pack(self):
        return struct.pack(self.FORMAT, self.type, self.flags, self.offset,
                           self.vaddr, self.paddr, self.filesz, self.memsz,
                           self.align)


class SectionHeader:
    """A 64-bit ELF section header. 0x40 bytes."""

    SIZE = 0x40
    FORMAT = ">IIQQQQIIQQ"

    def __init__(self, name, sh_type, flags, addr, offset, size, link, info,
                 addralign, entsize):
        self.name = name
        self.type = sh_type
        self.flags = flags
        self.addr = addr
        self.offset = offset
        self.size = size
        self.link = link
        self.info = info
        self.addralign = addralign
        self.entsize = entsize

    @classmethod
    def read(cls, data, offset, path=""):
        return cls(*_unpack(cls.FORMAT, data, offset, path, "section header"))

    def pack(self):
        return struct.pack(self.FORMAT, self.name, self.type, self.flags,
                           self.addr, self.offset, self.size, self.link,
                           self.info, self.addralign, self.entsize)
