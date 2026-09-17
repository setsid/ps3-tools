"""Fake-signed SELFs: the format scetool cannot touch at all.

A fake-signed SELF is a SELF with nothing encrypted. Its key revision is
0x8000, which scetool prints as DEBUG before refusing the file for want of a
keyset. The section data sits in the clear at the offsets the SELF's own
section info table gives, the metadata region is filled with junk, and there is
no signature worth the name.

That last point is what the digital releases run into. Every fake-signed file
in the corpus carries an NPDRM control block that is entirely zero, magic
included. A console that checks licences answers 8001000F to that, which is
the failure the user has already lost days to with TrueAncestor's resigner.
So building one here fills that block in from whatever the caller read off the
retail file, rather than zeroing it.

Reading one needs no keys at all, which is why this module takes none.
"""

import struct

from .errors import SigningFailed, UnsupportedSce
from .structs import (CONTROL_NPDRM, NPDRM_MAGIC, NpdrmInfo, SceHeader,
                      SectionInfo)

#: What the SCE header carries instead of a key revision. scetool calls it
#: DEBUG; everybody else calls the file fake signed.
FAKE_KEY_REVISION = 0x8000

#: A fake-signed SELF has no encrypted metadata, so the region between the
#: metadata offset and the end of the header is filled with junk. It is kept
#: verbatim when rebuilding: it means nothing, and inventing new junk would
#: change a file for no reason.
JUNK_NOTE = "carried through unchanged; it is padding, not data"


def is_fake_signed(self_file):
    return self_file.sce.key_revision == FAKE_KEY_REVISION


def npdrm_block(self_file):
    """The NPDRM block, or None when it is there but zeroed.

    Zeroed is the interesting answer and the reason this is not just a call to
    SelfFile.npdrm: a block of zeros is structurally present and semantically
    absent, and reporting it as "no block" would hide the thing that makes
    these files fail on a console.
    """
    for block in self_file.control_infos:
        if block.info_type != CONTROL_NPDRM:
            continue
        magic = int.from_bytes(block.payload[:4], "big")
        if magic != NPDRM_MAGIC:
            return None
        return NpdrmInfo.read(block.payload, self_file.path)
    return None


def has_zeroed_npdrm(self_file):
    for block in self_file.control_infos:
        if block.info_type == CONTROL_NPDRM:
            return not any(block.payload)
    return False


def to_elf(self_file):
    """The ELF inside a fake-signed SELF.

    Everything is in the clear, so this is a matter of copying each segment
    from where the section info table says it is to where the program header
    says it goes.
    """
    if not is_fake_signed(self_file):
        raise UnsupportedSce(
            "this is not a fake-signed SELF, so it needs keys and the SELF "
            "path rather than this one", path=self_file.path,
            field="key revision", expected=FAKE_KEY_REVISION,
            found=self_file.sce.key_revision)
    out = bytearray()

    def put(at, blob):
        end = at + len(blob)
        if end > len(out):
            out.extend(b"\x00" * (end - len(out)))
        out[at:end] = blob

    put(0, self_file.elf_header.pack())
    put(self_file.elf_header.phoff,
        b"".join(phdr.pack() for phdr in self_file.program_headers))

    for index, info in enumerate(self_file.section_infos):
        if index >= len(self_file.program_headers):
            break
        if info.compressed == 2:
            raise UnsupportedSce(
                "a fake-signed SELF with a compressed section",
                path=self_file.path, field=f"section {index} compressed",
                expected=1, found=info.compressed)
        phdr = self_file.program_headers[index]
        if not info.size:
            continue
        end = info.offset + info.size
        if end > len(self_file.raw):
            raise UnsupportedSce(
                "a section runs past the end of the file",
                path=self_file.path, field=f"section {index}",
                expected=end, found=len(self_file.raw))
        put(phdr.offset, self_file.raw[info.offset:end])

    if self_file.section_headers and self_file.elf_header.shoff:
        put(self_file.elf_header.shoff,
            b"".join(shdr.pack() for shdr in self_file.section_headers))
    return bytes(out)


def rebuild(template, elf):
    """A fake-signed SELF built from this template and this ELF.

    The template's header is kept whole, including the junk in the metadata
    region, so a file that has not been changed comes back out as it went in.
    Section sizes are taken from the template's program headers, which a patch
    does not move.
    """
    if not is_fake_signed(template):
        raise SigningFailed(
            "the template is not a fake-signed SELF", path=template.path,
            field="key revision", expected=FAKE_KEY_REVISION,
            found=template.sce.key_revision)
    out = bytearray(template.raw)
    for index, info in enumerate(template.section_infos):
        if index >= len(template.program_headers) or not info.size:
            continue
        phdr = template.program_headers[index]
        body = elf[phdr.offset:phdr.offset + phdr.filesz]
        if len(body) != phdr.filesz:
            raise SigningFailed(
                "the ELF is too short to hold a segment the template "
                "describes", path=template.path,
                field=f"segment {index}", expected=phdr.filesz,
                found=len(body))
        if len(body) != info.size:
            raise SigningFailed(
                "a segment is not the size the section info records, so the "
                "layout would have to move", path=template.path,
                field=f"section {index} size", expected=info.size,
                found=len(body))
        out[info.offset:info.offset + len(body)] = body
    return bytes(out)


def fill_npdrm(template_bytes, source_npdrm):
    """Puts a real NPDRM block into a fake-signed SELF that has none.

    This is the fix for 8001000F on a digital release. The block is copied from
    the retail file the parameters were read off, so the licence type, the
    application type, the content ID and the CID_FN hash are the ones the
    console expects rather than zeros.
    """
    from .self import SelfFile
    parsed = SelfFile(template_bytes, "rebuilt")
    at = parsed.self_header.control_info_offset
    for block in parsed.control_infos:
        if block.info_type == CONTROL_NPDRM:
            payload = source_npdrm.pack()
            if len(payload) != block.size - block.HEAD_SIZE:
                raise SigningFailed(
                    "the NPDRM block is not the size this file has room for",
                    field="NPDRM block size",
                    expected=block.size - block.HEAD_SIZE,
                    found=len(payload))
            out = bytearray(template_bytes)
            out[at + block.HEAD_SIZE:at + block.size] = payload
            return bytes(out)
        at += block.size
    raise SigningFailed("this file has no NPDRM control block to fill in",
                        field="control info")
