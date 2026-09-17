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
Writing one needs none either, except in the one case where the file is going
on to the console under a new name and the CID_FN hash has to move with it.

There are two ways to write one here. rebuild() keeps an existing fake-signed
file's header whole and drops a patched ELF into it, which needs a fake-signed
release of that same binary to work from. from_retail() builds the container
from scratch around a retail file's identity, which is what the Black Ops
titles need: neither has a fake-signed release on disk, and a retail re-sign
is not loadable on PS3HEN.
"""

import hashlib

from .errors import SigningFailed, UnsupportedSce
from .structs import (AppInfo, CONTROL_DIGEST, CONTROL_NPDRM, ControlInfo,
                      ElfHeader, NPDRM_MAGIC, NpdrmInfo, ProgramHeader,
                      SCE_HEADER_SELF, SCE_MAGIC, SECTION_INFO_ENCRYPTED,
                      SECTION_INFO_PLAIN, SceHeader, SceVersionInfo,
                      SectionInfo, SelfHeader, UNCOMPRESSED)

#: What the SCE header carries instead of a key revision. scetool calls it
#: DEBUG; everybody else calls the file fake signed.
FAKE_KEY_REVISION = 0x8000

#: How much room the header leaves for the metadata region, measured from the
#: SCE header's metadata offset. All three fake-signed files in the corpus
#: carry metadata offset 0x480 and header length 0x980, and both fall out of
#: this figure together with the layout in from_retail: 0x4A0 of headers and
#: control info, less the 0x20 the metadata offset sits ahead of the metadata
#: info, and then this much filler.
FAKE_METADATA_ROOM = 0x500

#: Inside the file digest control block's payload: a constant every SELF
#: carries, then the SHA1 of the ELF, then the minimum firmware version the
#: title asks for. That last one reads 36000 for Black Ops 1, 40000 for
#: Modern Warfare 3 and 42000 for Black Ops 2, which are those titles' own
#: stated requirements, so the name is read off the corpus rather than
#: guessed. Only the SHA1 depends on the payload, so only the SHA1 is
#: rewritten and the other two are carried.
DIGEST_ELF_SHA1_AT = 0x14
DIGEST_FIRMWARE_AT = 0x28
DIGEST_MIN_SIZE = 0x30

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


def from_retail(template, elf, filename="", klicensee=b"", store=None):
    """A fake-signed SELF built around a retail file's identity.

    rebuild() needs a fake-signed release of the same binary to work from, and
    only Modern Warfare 3 has one on disk. This builds the container instead,
    so the Black Ops titles can be fake signed from their own retail files.

    Why fake signed rather than re-signed. A retail re-sign carries Sony's
    signature, which cannot be regenerated once the file's bytes have moved,
    and PS3HEN appears to check it: three consoles black screened at the
    moment the patched multiplayer binary loaded. A fake-signed binary is the
    form HEN loads.

    The NPDRM control block is carried over from the retail template whole, so
    the licence type, the application type, the content ID and both hashes are
    the genuine ones. That is the one deliberate difference from every
    fake-signed release in the corpus: all three carry a block that is
    entirely zero, magic included, which is exactly why they answer 8001000F
    on a console that checks licences.

    filename is the name the file will carry on the console, and it is only
    wanted when that name changes. It feeds the CID_FN hash, and moving that
    hash moves the CI hash with it because the CI hash covers the block's own
    first 0x60 bytes. Left empty, the block is copied byte for byte and both
    hashes stay the ones the retail file was issued with, which is right
    whenever the name is unchanged.
    """
    if is_fake_signed(template):
        raise SigningFailed(
            "the template is already fake signed, so rebuild() keeps its "
            "header rather than building a new one", path=template.path,
            field="key revision", expected="a retail key revision",
            found=template.sce.key_revision)
    elf = bytes(elf)
    elf_header = ElfHeader.read(elf, 0, template.path)
    program_headers = _check_shape(template, elf, elf_header)

    # Everything up to the control info is fixed by the number of program
    # headers, and every file in the corpus lands on the same offsets because
    # every one of them has eight. It is computed rather than written down so
    # that a binary with a different count does not quietly produce a header
    # whose tables overlap.
    phnum = elf_header.phnum
    app_info_offset = SceHeader.SIZE + SelfHeader.SIZE
    elf_offset = app_info_offset + AppInfo.SIZE
    phdr_offset = elf_offset + ElfHeader.SIZE
    section_info_offset = phdr_offset + phnum * ProgramHeader.SIZE
    sce_version_offset = section_info_offset + phnum * SectionInfo.SIZE
    control_info_offset = sce_version_offset + SceVersionInfo.SIZE

    control_infos = _fake_control_infos(template, elf, filename, klicensee,
                                        store)
    control_info_size = sum(block.size for block in control_infos)

    # The metadata info sits 0x20 past the metadata offset and directly after
    # the control info. That holds for every file in the corpus, retail and
    # fake signed alike, which is where this comes from.
    metadata_offset = control_info_offset + control_info_size - SceHeader.SIZE
    header_length = metadata_offset + FAKE_METADATA_ROOM

    # A fake-signed SELF is its header followed by the ELF verbatim, so every
    # offset into the body is the ELF's own offset shifted by the header
    # length. Confirmed on all three corpus fselfs, whose bodies are byte for
    # byte the ELF that comes out of them.
    shdr_offset = 0
    if elf_header.shoff and elf_header.shnum:
        shdr_offset = header_length + elf_header.shoff

    section_infos = []
    for index, phdr in enumerate(program_headers):
        original = (template.section_infos[index]
                    if index < len(template.section_infos) else None)
        # encrypted is 1 for an encrypted section and 2 for a plain one, so
        # every section a retail file encrypts becomes plain here. The corpus
        # fselfs carry 0 for the three segments their retail counterparts also
        # leave at 0. What 0 means is not documented, so it is carried through
        # rather than turned into a 2.
        encrypted = SECTION_INFO_PLAIN
        unknown1 = unknown2 = 0
        if original is not None:
            unknown1, unknown2 = original.unknown1, original.unknown2
            if original.encrypted != SECTION_INFO_ENCRYPTED:
                encrypted = original.encrypted
        section_infos.append(SectionInfo(
            header_length + phdr.offset, phdr.filesz, UNCOMPRESSED,
            unknown1, unknown2, encrypted))

    # The SCE version block is written as absent. Its 0x20 bytes of payload
    # sit in the clear in a retail header and all three corpus fselfs drop
    # them, which is the whole of why a fake-signed header is 0x20 shorter
    # before the control info than the retail one it came from. What the
    # payload holds is a build stamp of some kind; it is not needed to load
    # the file, on the evidence of those three.
    version = SceVersionInfo(template.sce_version.header_type, 0,
                             SceVersionInfo.SIZE,
                             template.sce_version.unknown3)

    sce = SceHeader(SCE_MAGIC, template.sce.version, FAKE_KEY_REVISION,
                    SCE_HEADER_SELF, metadata_offset, header_length, len(elf))
    self_header = SelfHeader(
        template.self_header.header_type, app_info_offset, elf_offset,
        phdr_offset, shdr_offset, section_info_offset, sce_version_offset,
        control_info_offset, control_info_size, template.self_header.padding)

    head = bytearray(header_length)
    head[0:SceHeader.SIZE] = sce.pack()
    head[SceHeader.SIZE:app_info_offset] = self_header.pack()
    # Authentication ID, vendor ID, SELF type and application version, all
    # carried. They identify the title and none of them can be derived.
    head[app_info_offset:elf_offset] = template.app_info.pack()
    head[elf_offset:phdr_offset] = elf_header.pack()
    at = phdr_offset
    for phdr in program_headers:
        head[at:at + ProgramHeader.SIZE] = phdr.pack()
        at += ProgramHeader.SIZE
    at = section_info_offset
    for info in section_infos:
        head[at:at + SectionInfo.SIZE] = info.pack()
        at += SectionInfo.SIZE
    head[sce_version_offset:control_info_offset] = version.pack()
    at = control_info_offset
    for block in control_infos:
        head[at:at + block.size] = block.pack()
        at += block.size
    head[at:header_length] = _metadata_junk(header_length - at, elf)
    return bytes(head) + elf


def _check_shape(template, elf, elf_header):
    """The ELF must be the one this template describes, and it must fit.

    A patch changes bytes inside segments and leaves the headers where they
    are, which is what every patcher in this program does. An ELF whose
    headers have moved is a different binary, and building it into this
    template's identity would hand it another file's content ID and another
    file's CID_FN hash: valid in every visible field, and refused by the
    console.
    """
    for field in ("type", "machine", "entry", "phoff", "phnum", "shoff",
                  "shnum"):
        mine = getattr(elf_header, field)
        theirs = getattr(template.elf_header, field)
        if mine != theirs:
            raise SigningFailed(
                "the ELF does not have the shape the retail file had, so it "
                "is not the binary this template identifies",
                path=template.path, field=f"ELF {field}", expected=theirs,
                found=mine)
    program_headers = []
    for index in range(elf_header.phnum):
        at = elf_header.phoff + index * ProgramHeader.SIZE
        phdr = ProgramHeader.read(elf, at, template.path)
        theirs = template.program_headers[index]
        for field in ("type", "offset", "filesz", "memsz", "vaddr", "flags"):
            if getattr(phdr, field) != getattr(theirs, field):
                raise SigningFailed(
                    "a program header has moved, so the section layout read "
                    "off the retail file no longer describes this ELF",
                    path=template.path,
                    field=f"program header {index} {field}",
                    expected=getattr(theirs, field),
                    found=getattr(phdr, field))
        end = phdr.offset + phdr.filesz
        if end > len(elf):
            raise SigningFailed(
                "the ELF is too short to hold a segment its own program "
                "headers describe", path=template.path,
                field=f"segment {index}", expected=end, found=len(elf))
        program_headers.append(phdr)
    end = elf_header.shoff + elf_header.shnum * 0x40
    if elf_header.shoff and end > len(elf):
        raise SigningFailed(
            "the ELF is too short to hold its own section header table",
            path=template.path, field="section headers", expected=end,
            found=len(elf))
    return program_headers


def _fake_control_infos(template, elf, filename, klicensee, store):
    """The control info blocks for a fake-signed file, from the template.

    The control flags block is copied as it stands. The file digest's second
    SHA1 is recomputed over the ELF being written, which is the one figure in
    the header a patch moves: the real Modern Warfare 3 fself carries exactly
    the SHA1 of the ELF inside it, so this is checked rather than assumed. The
    eight bytes after it are the minimum firmware version and are carried,
    where all three corpus fselfs zero them.
    """
    blocks = []
    carried_npdrm = False
    for block in template.control_infos:
        if (block.info_type == CONTROL_DIGEST
                and len(block.payload) >= DIGEST_MIN_SIZE):
            payload = bytearray(block.payload)
            payload[DIGEST_ELF_SHA1_AT:DIGEST_ELF_SHA1_AT + 20] = (
                hashlib.sha1(elf).digest())
            blocks.append(ControlInfo(block.info_type, block.size, block.next,
                                      bytes(payload)))
        elif block.info_type == CONTROL_NPDRM:
            carried_npdrm = True
            blocks.append(_carry_npdrm(block, template, filename, klicensee,
                                       store))
        else:
            blocks.append(block)
    if not carried_npdrm:
        raise SigningFailed(
            "the template has no NPDRM control block to carry over, and a "
            "fake-signed file without one answers 8001000F on a console that "
            "checks licences", path=template.path, field="control info",
            expected="an NPDRM block", found="none")
    return blocks


def _carry_npdrm(block, template, filename, klicensee, store):
    """The retail NPDRM block, copied over rather than zeroed.

    This is the whole reason from_retail exists in this module rather than
    being TrueAncestor's job. Every fake-signed release in the corpus carries
    a block of zeros here, and that is what a console answers 8001000F to.
    """
    magic = int.from_bytes(block.payload[:4], "big")
    if magic != NPDRM_MAGIC:
        raise SigningFailed(
            "the template's NPDRM block has no magic, so carrying it over "
            "would write the zeroed block that answers 8001000F",
            path=template.path, field="NPDRM magic", expected=NPDRM_MAGIC,
            found=magic)
    if not filename:
        # Copied byte for byte. Both hashes were computed by Sony over fields
        # that have not changed, so recomputing them would need the klicensee
        # and arrive at the values already here.
        return block

    # Only this branch needs a keyset, which is why it is imported here: the
    # module's promise is that reading and writing a fake-signed file needs no
    # keys, and that still holds for every caller that is not renaming a file.
    from . import keys as keymod
    from . import npdrm as npdrm_hash
    store = store or keymod.load()
    payload = bytearray(block.payload)
    content_id = bytes(payload[npdrm_hash.CONTENT_ID_AT:
                               npdrm_hash.CONTENT_ID_AT
                               + npdrm_hash.CONTENT_ID_BYTES])
    payload[npdrm_hash.CID_FN_HASH_AT:npdrm_hash.CID_FN_HASH_AT + 16] = (
        npdrm_hash.cid_fn_hash(store, content_id, filename))
    # A FREE licensed file carries no klicensee of its own and is keyed by
    # NP_klic_free. Getting this wrong gives a CI hash that does not match
    # while every visible field is right.
    klic = bytes(klicensee) or store.named_key("NP_klic_free")
    payload[npdrm_hash.CI_HASH_AT:npdrm_hash.CI_HASH_AT + 16] = (
        npdrm_hash.ci_hash(store, bytes(payload), klic))
    return ControlInfo(block.info_type, block.size, block.next, bytes(payload))


def _metadata_junk(length, elf):
    """Filler for the metadata region of a fake-signed file.

    Nothing reads it. The three fselfs in the corpus fill it with 15-bit
    Windows rand() values written as little-endian 32-bit words, which says
    which tool built them and nothing more.

    It is derived from the ELF here rather than drawn from the system random
    source, so that fake signing the same binary twice gives the same file. A
    user comparing two builds should see no difference, and a difference that
    means nothing is the worst kind to have to account for.
    """
    out = bytearray()
    block = hashlib.sha1(elf).digest()
    while len(out) < length:
        block = hashlib.sha1(block).digest()
        out += block
    return bytes(out[:length])
