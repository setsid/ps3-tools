"""A description of a SELF, laid out the way scetool lays it out.

Matching scetool's formatting exactly is not vanity. It is what lets the two be
compared by running both and diffing, which is the only way to be sure this
reads a field the same way rather than merely plausibly. The tests do exactly
that against the real binary.

Everything goes to stdout. scetool writes its whole report to stderr, so a
caller that captured stdout got nothing and concluded it had failed.
"""

from .structs import (CONTROL_DIGEST, CONTROL_FLAGS, CONTROL_NPDRM,
                      NpdrmInfo)

# Known authentication IDs. Anything else is printed as its raw value, because
# inventing a name for an ID this has not seen would be a guess in a report
# somebody is using to decide what to sign.
AUTH_IDS = {
    0x1010000001000003: "retail game/update",
    0x1070000452000001: "retail npdrm",
    0x1070000552000001: "retail npdrm",
}

VENDOR_IDS = {
    0x01000002: "normal",
    0x01000001: "normal",
}

SELF_TYPE_LABELS = {
    1: "LV0", 2: "LV1", 3: "LV2", 4: "Application", 5: "Isolated SPU Module",
    6: "Secure Loader", 8: "NPDRM Application",
}

ELF_TYPES = {0: "NONE", 1: "REL", 2: "EXEC", 3: "DYN", 4: "CORE"}
ELF_MACHINES = {0x15: "PPC64", 0x17: "SPU"}

PHDR_TYPES = {
    1: "LOAD", 2: "DYNAMIC", 3: "INTERP", 4: "NOTE", 5: "SHLIB", 6: "PHDR",
    7: "TLS", 0x60000001: "PARAMS", 0x60000002: "PRX",
}

SHDR_TYPES = {
    0: "NULL", 1: "PROGBITS", 2: "SYMTAB", 3: "STRTAB", 4: "RELA", 5: "HASH",
    6: "DYNAMIC", 7: "NOTE", 8: "NOBITS", 9: "REL", 10: "SHLIB",
    11: "DYNSYM", 14: "INIT_ARRAY", 15: "FINI_ARRAY", 16: "PREINIT_ARRAY",
    17: "GROUP", 18: "SYMTAB_SHNDX",
}

# Capability flag bits, in the order scetool prints them.
CAPABILITY_FLAGS = [
    (0x01, "0x01"), (0x02, "0x02"), (0x04, "0x04"), (0x08, "REFTOOL"),
    (0x10, "DEBUG"), (0x20, "RETAIL"), (0x40, "SYSDBG"),
]


def _bytes_line(raw, per_line=16, indent=""):
    """Bytes as scetool prints them: two hex digits, a space after each."""
    lines = []
    for at in range(0, len(raw), per_line):
        chunk = raw[at:at + per_line]
        lines.append(("" if at == 0 else indent)
                     + "".join("%02X " % byte for byte in chunk))
    return lines


def _yes(flag):
    return "[YES]" if flag else "[NO ]"


def _true(flag):
    return "[TRUE]" if flag else "[FALSE]"


def _phdr_flags(flags):
    """PPU, SPU and RSX permission triples, exactly as scetool renders them.

    The three sets of bits are not adjacent. PPU sits at the bottom, SPU at
    bit 20 and RSX at bit 24, which was read off the real headers rather than
    assumed: segment 4 of the Black Ops 1 multiplayer binary carries
    0x06600006 and scetool renders it -WR -WR -WR, which pins all three.
    """
    out = []
    for shift in (0, 20, 24):
        bits = (flags >> shift) & 0x7
        out.append(("X" if bits & 0x1 else "-")
                   + ("W" if bits & 0x2 else "-")
                   + ("R" if bits & 0x4 else "-"))
    return out


def _shdr_flags(flags):
    return (("W" if flags & 0x1 else "-")
            + ("A" if flags & 0x2 else "-")
            + ("E" if flags & 0x4 else "-"))


def _capability_names(flags):
    names = [name for bit, name in CAPABILITY_FLAGS if flags & bit]
    leftover = flags & ~sum(bit for bit, _n in CAPABILITY_FLAGS)
    if leftover:
        names.append("0x%X" % leftover)
    return names


def sce_header_lines(self_file):
    head = self_file.sce
    return [
        "[*] SCE Header:",
        " Magic           0x%08X [%s]" % (head.magic,
                                          "OK" if head.magic == 0x53434500
                                          else "ERROR"),
        " Version         0x%08X" % head.version,
        " Key Revision    " + ("[DEBUG]" if head.key_revision == 0x8000
                               else "0x%04X" % head.key_revision),
        " Header Type     [%s]" % head.type_name,
        " Metadata Offset 0x%08X" % head.metadata_offset,
        " Header Length   0x%016X" % head.header_length,
        " Data Length     0x%016X" % head.data_length,
    ]


def metadata_lines(self_file, metadata):
    lines = ["[*] Metadata Info:"]
    lines.append(" Key " + _bytes_line(metadata.info.key)[0])
    lines.append(" IV  " + _bytes_line(self_file.printed_iv(metadata))[0])
    head = metadata.header
    lines += [
        "[*] Metadata Header:",
        " Signature Input Length 0x%016X" % head.signature_input_length,
        " unknown_0              0x%08X" % head.unknown1,
        " Section Count          0x%08X" % head.section_count,
        " Key Count              0x%08X" % head.key_count,
        " Optional Header Size   0x%08X" % head.opt_header_size,
        " unknown_1              0x%08X" % head.unknown2,
        " unknown_2              0x%08X" % head.unknown3,
        "[*] Metadata Section Headers:",
        " Idx Offset   Size     Type Index Hashed SHA1 Encrypted Key IV "
        "Compressed",
    ]
    for index, section in enumerate(metadata.sections):
        encrypted = section.encrypted == 3
        lines.append(
            " %03d %08X %08X %02X   %02X    %s  %02X   %s     %s  %s %s"
            % (index, section.data_offset, section.data_size,
               section.section_type, section.index,
               _yes(section.hashed == 2), section.sha1_index,
               _yes(encrypted),
               ("%02X" % section.key_index) if encrypted else "--",
               ("%02X" % section.iv_index) if encrypted else "--",
               _yes(section.compressed == 2)))
    lines.append("[*] SCE File Keys:")
    for index in range(len(metadata.keys) // 16):
        chunk = metadata.keys[index * 16:index * 16 + 16]
        lines.append(" %02X: " % index + _bytes_line(chunk)[0])
    return lines


def self_header_lines(self_file):
    head = self_file.self_header
    return [
        "[*] SELF Header:",
        " Header Type         0x%016X" % head.header_type,
        " App Info Offset     0x%016X" % head.app_info_offset,
        " ELF Offset          0x%016X" % head.elf_offset,
        " PH Offset           0x%016X" % head.phdr_offset,
        " SH Offset           0x%016X" % head.shdr_offset,
        " Section Info Offset 0x%016X" % head.section_info_offset,
        " SCE Version Offset  0x%016X" % head.sce_version_offset,
        " Control Info Offset 0x%016X" % head.control_info_offset,
        " Control Info Size   0x%016X" % head.control_info_size,
    ]


def app_info_lines(self_file):
    info = self_file.app_info
    auth = AUTH_IDS.get(info.auth_id)
    vendor = VENDOR_IDS.get(info.vendor_id)
    kind = SELF_TYPE_LABELS.get(info.self_type)
    return [
        "[*] Application Info:",
        " Auth-ID   " + ("[%s]" % auth if auth else "0x%016X" % info.auth_id),
        " Vendor-ID " + ("[%s]" % vendor if vendor
                         else "0x%08X" % info.vendor_id),
        " SELF-Type " + ("[%s]" % kind if kind else "0x%08X" % info.self_type),
        # The version is a 64-bit field whose printed parts are the third and
        # fourth bytes: 0x0001000000000000 shows as 01.00.
        " Version   %02X.%02X" % ((info.version >> 48) & 0xFF,
                                  (info.version >> 40) & 0xFF),
    ]


def sce_version_lines(self_file):
    version = self_file.sce_version
    return [
        "[*] SCE Version:",
        " Header Type 0x%08X" % version.header_type,
        " Present     %s" % _true(version.present == 1),
        " Size        0x%08X" % version.size,
        " unknown_3   0x%08X" % version.unknown3,
    ]


def control_info_lines(self_file):
    lines = []
    for block in self_file.control_infos:
        lines.append("[*] Control Info")
        if block.info_type == CONTROL_FLAGS:
            lines.append(" Type      Flags")
        elif block.info_type == CONTROL_DIGEST:
            lines.append(" Type      Digest")
        elif block.info_type == CONTROL_NPDRM:
            lines.append(" Type      NPDRM")
        else:
            lines.append(" Type      0x%08X" % block.info_type)
        lines.append(" Size      0x%08X" % block.size)
        lines.append(" Next      %s" % _true(block.next == 1))
        if block.info_type == CONTROL_FLAGS:
            for number, text in enumerate(_bytes_line(block.payload,
                                                      indent="       ")):
                lines.append((" Flags " if number == 0 else "") + text
                             if number == 0 else text)
        elif block.info_type == CONTROL_DIGEST and len(block.payload) >= 0x30:
            first = block.payload[0:20]
            second = block.payload[20:40]
            firmware = int.from_bytes(block.payload[40:48], "big")
            for number, text in enumerate(_bytes_line(first,
                                                      indent="            ")):
                lines.append((" Digest 1   " + text) if number == 0 else text)
            for number, text in enumerate(_bytes_line(second,
                                                      indent="            ")):
                lines.append((" Digest 2   " + text) if number == 0 else text)
            lines.append(" FW Version %d [%02d.%02d]"
                         % (firmware, firmware // 10000,
                            (firmware % 10000) // 100))
        elif block.info_type == CONTROL_NPDRM and not any(block.payload):
            # A block of zeros is what a fake-signed release built by
            # TrueAncestor carries, and it is the reason such a file answers
            # 8001000F on a console that checks licences. Worth saying plainly
            # in the report rather than raising over the missing magic.
            lines.append(" Magic        0x00000000 [MISSING]")
            lines.append(" The NPDRM block is present but entirely zero. A "
                         "console that checks")
            lines.append(" licences answers 8001000F to this. The licence "
                         "type, application")
            lines.append(" type, content ID and CID_FN hash all have to be "
                         "filled in from the")
            lines.append(" retail file this was built from.")
        elif block.info_type == CONTROL_NPDRM:
            npdrm = NpdrmInfo.read(block.payload, self_file.path)
            lines += [
                " Magic        0x%08X [%s]" % (
                    npdrm.magic, "OK" if npdrm.magic == 0x4E504400
                    else "ERROR"),
                " unknown_0    0x%08X" % npdrm.version,
                " Licence Type 0x%08X" % npdrm.licence_type,
                " App Type     0x%08X" % npdrm.app_type,
                " ContentID    " + npdrm.content_id_text,
                " Random Pad   " + _bytes_line(npdrm.digest)[0],
                " CID_FN Hash  " + _bytes_line(npdrm.cid_fn_hash)[0],
                " CI Hash      " + _bytes_line(npdrm.header_hash)[0],
                " unknown_1    0x%016X" % npdrm.limited_time_start,
                " unknown_2    0x%016X" % npdrm.limited_time_end,
            ]
    return lines


def optional_header_lines(metadata):
    """The optional headers that follow the SCE file keys.

    Only the capability flags header appears in anything this has been shown.
    Anything else is reported by type and size and its payload left alone.
    """
    lines = []
    blob = metadata.optional
    at = 0
    while at + 16 <= len(blob):
        kind, size, next_block = (int.from_bytes(blob[at:at + 4], "big"),
                                  int.from_bytes(blob[at + 4:at + 8], "big"),
                                  int.from_bytes(blob[at + 8:at + 16], "big"))
        if size < 16 or at + size > len(blob):
            break
        lines.append("[*] Optional Header")
        lines.append(" Type      " + ("Capability Flags" if kind == 1
                                      else "0x%08X" % kind))
        lines.append(" Size      0x%08X" % size)
        lines.append(" Next      %s" % _true(next_block == 1))
        payload = blob[at + 16:at + size]
        if kind == 1 and len(payload) >= 0x20:
            unknown3 = int.from_bytes(payload[0:8], "big")
            unknown4 = int.from_bytes(payload[8:16], "big")
            flags = int.from_bytes(payload[16:24], "big")
            unknown6 = int.from_bytes(payload[24:28], "big")
            unknown7 = int.from_bytes(payload[28:32], "big")
            lines += [
                " unknown_3 0x%016X" % unknown3,
                " unknown_4 0x%016X" % unknown4,
                " Flags     0x%016X [ %s ]" % (
                    flags, " ".join(_capability_names(flags))),
                " unknown_6 0x%08X" % unknown6,
                " unknown_7 0x%08X" % unknown7,
            ]
        at += size
        if next_block != 1:
            break
    return lines


def section_info_lines(self_file):
    lines = ["[*] Section Infos:",
             " Idx Offset   Size     Compressed unk0     unk1     Encrypted"]
    for index, info in enumerate(self_file.section_infos):
        lines.append(" %03d %08X %08X %s      %08X %08X %s"
                     % (index, info.offset, info.size,
                        _yes(info.compressed == 2), info.unknown1,
                        info.unknown2, _yes(info.encrypted == 1)))
    return lines


def elf_lines(self_file):
    head = self_file.elf_header
    kind = ELF_TYPES.get(head.type)
    machine = ELF_MACHINES.get(head.machine)
    lines = [
        "[*] ELF64 Header:",
        " Type                   " + ("[%s]" % kind if kind
                                      else "0x%04X" % head.type),
        " Machine                " + ("[%s]" % machine if machine
                                      else "0x%04X" % head.machine),
        " Version                0x%08X" % head.version,
        " Entry                  0x%016X" % head.entry,
        " Program Headers Offset 0x%016X" % head.phoff,
        " Section Headers Offset 0x%016X" % head.shoff,
        " Flags                  0x%08X" % head.flags,
        " Program Headers Count  %04d" % head.phnum,
        " Section Headers Count  %04d" % head.shnum,
        " SH String Index        %04d" % head.shstrndx,
        "[*] ELF64 Program Headers:",
        " Idx Type     Offset   VAddr    PAddr    FileSize MemSize  "
        "PPU SPU RSX Align",
    ]
    for index, phdr in enumerate(self_file.program_headers):
        ppu, spu, rsx = _phdr_flags(phdr.flags)
        name = PHDR_TYPES.get(phdr.type, "0x%08X" % phdr.type)
        lines.append(" %03d %-8s %08X %08X %08X %08X %08X %s %s %s %08X"
                     % (index, name, phdr.offset, phdr.vaddr, phdr.paddr,
                        phdr.filesz, phdr.memsz, ppu, spu, rsx, phdr.align))
    if self_file.section_headers:
        lines.append("[*] ELF64 Section Headers:")
        lines.append(" Idx Name Type          Flags Address    Offset   "
                     "Size     ES   Align    LK")
        for index, shdr in enumerate(self_file.section_headers):
            name = SHDR_TYPES.get(shdr.type, "0x%08X" % shdr.type)
            lines.append(
                " %03d %04X %-13s %s   %08X   %08X %08X %04X %08X %03d"
                % (index, shdr.name, name, _shdr_flags(shdr.flags), shdr.addr,
                   shdr.offset, shdr.size, shdr.entsize, shdr.addralign,
                   shdr.link))
    return lines


def describe(self_file, metadata=None):
    """Every line of the report, in scetool's order."""
    lines = sce_header_lines(self_file)
    if metadata is not None:
        lines += metadata_lines(self_file, metadata)
    lines += self_header_lines(self_file)
    lines += app_info_lines(self_file)
    lines += sce_version_lines(self_file)
    lines += control_info_lines(self_file)
    if metadata is not None:
        lines += optional_header_lines(metadata)
    lines += section_info_lines(self_file)
    lines += elf_lines(self_file)
    return lines
