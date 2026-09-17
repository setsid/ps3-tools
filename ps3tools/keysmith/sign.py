"""Rebuilding a SELF from a template and an ELF.

The template is the user's own original file. Everything that identifies the
file and cannot be worked out from an ELF is taken from it: key revision,
authentication and vendor IDs, SELF type, application version, the control
flags, and the whole NPDRM block including the licence type, the application
type, the content ID and the CID_FN hash. Those are the fields that cost
people days. TrueAncestor drops the NPDRM block and the Black Ops 2
Eboot-Self Builder writes application type 0, and both give 8001000F on a
console that checks licences.

What is recomputed is what genuinely depends on the payload: section sizes and
offsets, the per-section HMACs, the ELF digest, and the lengths in the headers.

What is carried through unchanged, because it cannot be derived:

  * The signature. These files are signed with a private key nobody outside
    Sony has, and the keys file ships zeros for it. scetool cannot sign either.
  * The NPDRM CI hash. Its construction is not known here. HMAC-SHA1 and
    AES-CMAC were both tried over every contiguous range of the header with
    every key in the keys file and none reproduces the value a known-good
    scetool build wrote, so it is copied rather than guessed at.
  * The type 3 metadata section. It holds a build comment table that is not in
    the decrypted ELF at all, so a rebuild that worked from the ELF alone would
    silently drop it. scetool does drop it.
  * Every field named unknown in structs.py.

Compression follows the template section by section. Turning it off is what
makes an output roughly twice the size of stock, and turning it on for a title
whose sections were never compressed would be just as wrong: the Modern
Warfare binaries in the corpus carry no compressed sections at all.
"""

import hashlib
import hmac
import zlib

from . import aes
from . import keys as keymod
from .errors import SigningFailed
from .structs import (CONTROL_DIGEST, ControlInfo, MetadataInfo,
                      SECTION_TYPE_PHDR, SECTION_TYPE_SHDR, SceHeader)

#: What a fake-signed SELF carries where a key revision goes. Spelled out here
#: rather than imported from fself, which imports nothing from this module and
#: should stay that way.
FAKE_KEY_REVISION = 0x8000

# Sony's compressor used zlib at its default level. That is not a guess: every
# compressed section in the corpus is reproduced byte for byte by level 6 and
# by no other level, checked across eighteen sections in nine files.
ZLIB_LEVEL = 6

# scetool packs sections to this alignment after the header. Read off a
# known-good scetool build rather than assumed.
SECTION_ALIGN = 0x10

# The first of the two digests in the file digest control block is the same
# constant in every SELF anybody has seen, including all eighteen here.
DIGEST_CONSTANT = bytes.fromhex("627CB1808AB938E32C8C091708726A579E2586E4")


def _hmac_key(metadata, section):
    """The 0x40 byte key a section's HMAC uses.

    The layout is a run of slots per section: the hash itself at sha1_index,
    then the key two slots later. Confirmed against every section of every
    file in the corpus rather than taken from a document.
    """
    at = (section.sha1_index + 2) * 16
    key = metadata.keys[at:at + 0x40]
    if len(key) != 0x40:
        raise SigningFailed(
            "the key table is too short to hold this section's HMAC key",
            field="sha1 index", found=len(metadata.keys),
            expected=f"{(section.sha1_index + 2) * 16 + 0x40} bytes")
    return key


def section_payloads(template, elf, metadata):
    """The plaintext bytes each metadata section should carry, in order.

    Plaintext here means compressed if the section is compressed, because the
    HMAC and the encryption are both over that, not over the raw segment.
    """
    out = []
    for section in metadata.sections:
        if section.section_type == SECTION_TYPE_PHDR:
            if section.index >= len(template.program_headers):
                raise SigningFailed(
                    "a metadata section names a program header that is not "
                    "there", path=template.path, field="section index",
                    expected=f"< {len(template.program_headers)}",
                    found=section.index)
            phdr = template.program_headers[section.index]
            body = elf[phdr.offset:phdr.offset + phdr.filesz]
            if len(body) != phdr.filesz:
                raise SigningFailed(
                    "the ELF is too short to hold a segment the template "
                    "describes", path=template.path,
                    field=f"segment {section.index}", expected=phdr.filesz,
                    found=len(body))
            if section.compressed == 2:
                body = zlib.compress(body, ZLIB_LEVEL)
        elif section.section_type == SECTION_TYPE_SHDR:
            # The section header table, taken from the template rather than
            # from the ELF: a decrypted ELF carries it, but reading it back
            # from there would lose nothing and gain a second source of truth.
            body = b"".join(shdr.pack() for shdr in template.section_headers)
            if not body:
                body = template.raw[section.data_offset:
                                    section.data_offset + section.data_size]
        else:
            # Carried through. Not in the ELF, so there is nothing to rebuild
            # it from.
            body = template.raw[section.data_offset:
                                section.data_offset + section.data_size]
        out.append(body)
    return out


def rebuild(template, elf, klicensee=b"", store=None, keep_layout=True):
    """A SELF built from this template and this ELF.

    keep_layout reuses the template's own section offsets when the payload
    sizes have not changed, so rebuilding a file nobody touched gives back the
    bytes it started with. When a size does change the sections are packed from
    the end of the header at the same alignment scetool uses.
    """
    # Checked before anything else: a fake-signed template has no keys and no
    # metadata, so every message from further down would be about the wrong
    # thing.
    if template.sce.key_revision == FAKE_KEY_REVISION:
        raise SigningFailed(
            "this template is a fake-signed SELF, which is rebuilt by the "
            "fself path rather than this one", path=template.path,
            field="key revision", expected="a retail key revision",
            found=template.sce.key_revision)
    store = store or keymod.load()
    _check_geometry(template, elf)
    metadata = template.decrypt_metadata(klicensee, store)
    payloads = section_payloads(template, elf, metadata)

    unchanged = all(
        len(payload) == section.data_size
        for payload, section in zip(payloads, metadata.sections))

    # Work out where each section's bytes go.
    offsets = []
    if keep_layout and unchanged:
        offsets = [section.data_offset for section in metadata.sections]
    else:
        at = int(template.sce.header_length)
        for payload in payloads:
            offsets.append(at)
            at += len(payload)
            at = (at + SECTION_ALIGN - 1) // SECTION_ALIGN * SECTION_ALIGN

    # Encrypt each section and hash its plaintext.
    #
    # A section whose bytes have not changed keeps the hash it came with. That
    # is not laziness: it is the difference between recomputing a value and
    # asserting that a value can be recomputed. The type 3 section's hash is
    # one this package cannot reproduce, and a file nobody has touched should
    # come back out as it went in rather than with one field quietly rewritten.
    key_table = bytearray(metadata.keys)
    stored = []
    for section, payload in zip(metadata.sections, payloads):
        original = template.raw[section.data_offset:
                                section.data_offset + section.data_size]
        plain = original
        if section.encrypted == 3 and original:
            plain = aes.ctr_crypt(metadata.key_at(section.key_index),
                                  metadata.key_at(section.iv_index), original)
        if payload == plain:
            # Unchanged, so its hash still holds and its ciphertext is already
            # in the template. Skipping both is not only quicker: encrypting a
            # section again to arrive at bytes that are already to hand is a
            # chance to arrive at different ones.
            stored.append(original)
            continue
        digest = hmac.new(_hmac_key(metadata, section), payload,
                          hashlib.sha1).digest()
        at = section.sha1_index * 16
        key_table[at:at + 20] = digest
        if section.encrypted == 3:
            blob = aes.ctr_crypt(metadata.key_at(section.key_index),
                                 metadata.key_at(section.iv_index), payload)
        else:
            blob = payload
        stored.append(blob)

    # Rewrite the metadata section headers and the SELF's own section infos.
    new_sections = []
    for section, offset, payload in zip(metadata.sections, offsets, payloads):
        copy = _copy_section(section)
        copy.data_offset = offset
        copy.data_size = len(payload)
        new_sections.append(copy)

    section_infos = [_copy_info(info) for info in template.section_infos]
    for section, offset, payload in zip(new_sections, offsets, payloads):
        if section.section_type == SECTION_TYPE_PHDR:
            info = section_infos[section.index]
            info.offset = offset
            info.size = len(payload)

    # The file digest covers the ELF that was signed. That this is a plain
    # SHA1 of the ELF was verified against a known-good scetool build, whose
    # value is exactly the SHA1 of the ELF it was handed.
    #
    # When nothing has changed it is carried through instead. A retail SELF is
    # signed over the complete ELF Sony held, and the ELF that comes back out
    # of one is not quite that file: the type 3 metadata section is not stored
    # anywhere in it, so its bytes decrypt to a hole. Recomputing from the ELF
    # this package can produce would therefore replace a correct digest with a
    # different one for a file nobody has touched.
    control_infos = []
    for block in template.control_infos:
        if (block.info_type == CONTROL_DIGEST and len(block.payload) >= 0x30
                and not unchanged):
            payload = bytearray(block.payload)
            payload[0:20] = DIGEST_CONSTANT
            payload[20:40] = hashlib.sha1(elf).digest()
            control_infos.append(ControlInfo(block.info_type, block.size,
                                             block.next, bytes(payload)))
        else:
            control_infos.append(block)

    blob = _metadata_blob(metadata, new_sections, key_table)
    header = _header(template, control_infos, section_infos, metadata, blob,
                     klicensee, store, elf_length=len(elf))

    # Where the layout is kept, the rebuild starts from the original file so
    # that whatever sits in the gaps between sections survives. A retail SELF
    # has real bytes there, and writing zeros over them is a file that is the
    # right length and wrong in twenty-two thousand places.
    if keep_layout and unchanged:
        out = bytearray(template.raw)
        out[:len(header)] = header
    else:
        out = bytearray(header)
    for offset, blob_bytes in zip(offsets, stored):
        if offset > len(out):
            out.extend(b"\x00" * (offset - len(out)))
        out[offset:offset + len(blob_bytes)] = blob_bytes

    # Retail files carry padding past the last section. Keeping the original
    # length when the layout was kept is the difference between a round trip
    # that matches and one that is forty-eight bytes short.
    if keep_layout and unchanged and len(out) < len(template.raw):
        out.extend(template.raw[len(out):])
    return bytes(out)


def _check_geometry(template, elf):
    """The ELF must have the shape the template describes.

    A patch changes bytes inside segments and leaves the program headers
    alone, which is what every patcher in this program does and what was
    checked against a known-good rebuild. An ELF whose headers have moved
    needs a different section and key layout, and quietly signing it against
    the template's layout would give a file that looks right and is not. So
    this refuses instead, naming the field that moved.
    """
    from .structs import ElfHeader, ProgramHeader
    head = ElfHeader.read(elf, 0, template.path)
    for field in ("entry", "phoff", "shoff", "phnum", "shnum", "type",
                  "machine"):
        mine = getattr(head, field)
        theirs = getattr(template.elf_header, field)
        if mine != theirs:
            raise SigningFailed(
                "the ELF does not have the shape the original had, so the "
                "section layout read off the original does not describe it",
                path=template.path, field=f"ELF {field}", expected=theirs,
                found=mine)
    for index, phdr in enumerate(template.program_headers):
        at = head.phoff + index * ProgramHeader.SIZE
        other = ProgramHeader.read(elf, at, template.path)
        for field in ("type", "offset", "filesz", "memsz", "vaddr", "flags"):
            if getattr(other, field) != getattr(phdr, field):
                raise SigningFailed(
                    "a program header has moved, so the section layout read "
                    "off the original no longer describes this ELF",
                    path=template.path,
                    field=f"program header {index} {field}",
                    expected=getattr(phdr, field), found=getattr(other, field))


def _copy_section(section):
    from .structs import MetadataSection
    return MetadataSection(section.data_offset, section.data_size,
                           section.section_type, section.index,
                           section.hashed, section.sha1_index,
                           section.encrypted, section.key_index,
                           section.iv_index, section.compressed)


def _copy_info(info):
    from .structs import SectionInfo
    return SectionInfo(info.offset, info.size, info.compressed, info.unknown1,
                       info.unknown2, info.encrypted)


def _metadata_blob(metadata, sections, key_table):
    """The metadata run, in the clear, exactly as long as it was."""
    head = metadata.header
    parts = [head.pack()]
    parts += [section.pack() for section in sections]
    parts.append(bytes(key_table))
    parts.append(metadata.optional)
    parts.append(metadata.signature)
    blob = b"".join(parts)
    if len(blob) != metadata.raw_length:
        raise SigningFailed(
            "the rebuilt metadata is a different length from the original",
            field="metadata length", expected=metadata.raw_length,
            found=len(blob))
    return blob


def _header(template, control_infos, section_infos, metadata, blob, klicensee,
            store, elf_length=None):
    """Everything up to the first section's bytes."""
    sce = template.sce
    head = bytearray(template.raw[:int(sce.header_length)])
    if elf_length is not None and elf_length != sce.data_length:
        # data_length is the length of the ELF inside, which changes when a
        # patch changes a segment.
        updated = SceHeader(sce.magic, sce.version, sce.key_revision,
                            sce.header_type, sce.metadata_offset,
                            sce.header_length, elf_length)
        head[0:SceHeader.SIZE] = updated.pack()

    at = template.self_header.control_info_offset
    for block in control_infos:
        packed = block.pack()
        head[at:at + len(packed)] = packed
        at += block.size

    at = template.self_header.section_info_offset
    for info in section_infos:
        packed = info.pack()
        head[at:at + len(packed)] = packed
        at += len(packed)

    # The metadata info block, wrapped back up the way it was unwrapped.
    info = MetadataInfo(metadata.info.key, b"\x00" * 16, metadata.start_iv,
                        b"\x00" * 16)
    wrapped = aes.cbc_encrypt(template.keyset.erk, template.keyset.riv,
                              info.pack())
    if template.is_npdrm:
        klic = bytes(klicensee) or store.named_key("NP_klic_free")
        per_title = aes.ecb_decrypt(store.named_key("NP_klic_key"), klic)
        wrapped = aes.cbc_encrypt(per_title, b"\x00" * 16, wrapped)
    at = sce.metadata_offset + SceHeader.SIZE
    head[at:at + MetadataInfo.SIZE] = wrapped

    at += MetadataInfo.SIZE
    encrypted = aes.ctr_crypt(metadata.info.key, metadata.start_iv, blob)
    head[at:at + len(encrypted)] = encrypted
    return bytes(head)
