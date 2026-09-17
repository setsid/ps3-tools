"""Rebuilding a SELF from a template and an ELF.

The template is the user's own original file. Everything that identifies the
file and cannot be worked out from an ELF is taken from it: the key revision
unless the caller asks for another one, the authentication and vendor IDs, the
SELF type, the application version, the control flags, and the whole NPDRM
block including the licence type, the application type, the content ID and the
CID_FN hash. Those are the fields that cost people days. TrueAncestor drops
the NPDRM block and the Black Ops 2 Eboot-Self Builder writes application type
0, and both give 8001000F on a console that checks licences.

What is recomputed is what genuinely depends on the payload: section sizes and
offsets, the per-section HMACs, the ELF digest, the lengths in the headers, and
both NPDRM hashes. See npdrm.py for the two of those.

What is carried through unchanged, because it cannot be derived:

  * The signature. These files are signed with a private key nobody outside
    Sony has, and the keys file ships zeros for it. scetool cannot sign either.
  * The type 3 metadata section. It holds a build comment table that is not in
    the decrypted ELF at all, so a rebuild that worked from the ELF alone would
    silently drop it. scetool does drop it.
  * Every field named unknown in structs.py.

Compression follows the template section by section. Turning it off is what
makes an output roughly twice the size of stock, and turning it on for a title
whose sections were never compressed would be just as wrong: the Modern
Warfare binaries in the corpus carry no compressed sections at all.

The one identifying field a caller may override is the key revision, and the
reason is PS3HEN. HEN runs on 4.8x firmware but loads a SELF through the
3.55-era appldr keyset, so a file built at the key revision a 4.8x retail copy
carries is not one it can open. That is what the community advice to "resign
to 3.55" means in this format: key revision 0x000A.

The evidence is Jacob Schroeder's IW4 binaries, which ship a CFW build and a
HEN build for all seven Modern Warfare 2 regions. Reading the BLUS30377 pair
against each other: neither is fake signed, both are ordinary retail re-signs
scetool built, and the whole NPDRM control block is byte-identical between
them, content ID, CID_FN hash, header hash, licence type 3, application type
0x20 and the pad at 0x40 included. The only header field that differs is the
SCE key revision, 0x0010 on the CFW build and 0x000A on the HEN one. So the
HEN form is the same re-sign against a different keyset rather than a
different kind of file, and fake signing is not part of it.

Writing a revision therefore has to do two things at once: put the number in
the SCE header, and wrap the metadata info under that revision's erk and riv.
The console reads the first to choose the second, so setting either without
the other gives a file whose header will not decrypt at all.
"""

import hashlib
import hmac
import zlib

from . import aes
from . import keys as keymod
from . import npdrm as npdrm_hash
from .errors import SigningFailed
from .self import effective_klicensee
from .structs import (CONTROL_DIGEST, CONTROL_FLAGS, CONTROL_NPDRM,
                      ControlInfo, MetadataInfo,
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

#: Where the minimum firmware version sits in the file digest block, and what
#: belongs with each key revision.
#:
#: The field tracks the keyset across every retail file in the corpus: key
#: revision 0x0010 carries 36000, which is 3.60, 0x0019 carries 40000 and
#: 0x001C carries 42000. Jacob Schroeder's PS3HEN builds of Modern Warfare 2
#: carry 35500, which is 3.55 and is the value that belongs with 0x000A, and
#: he set it deliberately: his own custom firmware builds of the same binary
#: leave it at zero. Confirmed on both the European and American releases.
#:
#: So re-signing to another key revision moves this with it. Leaving 3.60 in a
#: file signed against the 3.55 keyset says two different things about the
#: same file, and this is the field that says which firmware will load it.
#: The control flags a build for a given key revision carries.
#:
#: Surveyed rather than assumed. All fourteen of Jacob Schroeder's PS3HEN
#: binaries carry exactly this, seven regions across both the multiplayer and
#: the campaign trees, and it does not vary between any of them. All fourteen
#: of his custom firmware builds are zero, all eighteen stock retail files
#: here are zero, and so are the scetool rebuilds on hand. Every HEN build
#: sets it, nothing else does, and it never varies, so it is part of what
#: makes a build a HEN build and is written rather than carried.
#:
#: What the two bytes mean is not known here. They are reproduced because the
#: survey says they belong, which is a different thing from understanding
#: them, and no third value has ever been seen to choose between.
CONTROL_FLAGS_FOR_REVISION = {
    0x000A: bytes.fromhex("40" + "00" * 30 + "02"),
}

DIGEST_FIRMWARE_AT = 40
FIRMWARE_FOR_REVISION = {
    0x000A: 35500,
    0x0010: 36000,
    0x0019: 40000,
    0x001C: 42000,
}


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


def rebuild(template, elf, klicensee=b"", store=None, keep_layout=True,
            filename="", key_revision=None):
    """A SELF built from this template and this ELF.

    keep_layout reuses the template's own section offsets when the payload
    sizes have not changed, so rebuilding a file nobody touched gives back the
    bytes it started with. When a size does change the sections are packed from
    the end of the header at the same alignment scetool uses.

    filename is the name the file will have on the console. It feeds the
    CID_FN hash, so a file written under a different name needs it given here
    or it will be valid and refuse to load. Left empty, the template's hash is
    kept, which is right whenever the name has not changed.

    key_revision rebuilds the file against a different keyset: the number goes
    into the SCE header and the metadata info is wrapped under that revision's
    erk and riv. None keeps the template's own, which is what a custom
    firmware console wants. PS3HEN wants 0x000A; the module docstring has the
    evidence for that.
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
    # The template is always opened with its own keyset. Only the keyset it is
    # written back out under is a choice, and decrypt_metadata has to have run
    # before that choice can be made, because it is what records which of the
    # candidate keysets actually opened this file.
    metadata = template.decrypt_metadata(klicensee, store)
    revision, keyset = _signing_keyset(template, store, key_revision)
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
        moved = (key_revision is not None
                 and key_revision != template.sce.key_revision)
        if (block.info_type == CONTROL_DIGEST and len(block.payload) >= 0x30
                and (not unchanged or moved)):
            payload = bytearray(block.payload)
            if not unchanged:
                payload[0:20] = DIGEST_CONSTANT
                payload[20:40] = hashlib.sha1(elf).digest()
            _set_firmware(payload, template.sce.key_revision, key_revision)
            control_infos.append(ControlInfo(block.info_type, block.size,
                                             block.next, bytes(payload)))
        elif block.info_type == CONTROL_FLAGS and moved:
            control_infos.append(_control_flags(block, key_revision))
        elif block.info_type == CONTROL_NPDRM:
            control_infos.append(_npdrm_block(block, store, klicensee,
                                              filename))
        else:
            control_infos.append(block)

    blob = _metadata_blob(metadata, new_sections, key_table)
    header = _header(template, control_infos, section_infos, metadata, blob,
                     klicensee, store, elf_length=len(elf),
                     key_revision=revision, keyset=keyset)

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


def _signing_keyset(template, store, key_revision):
    """(the revision to write, the keyset to write it under).

    key_revision None, or the revision the template already carries, keeps the
    keyset that opened the file, so nothing about a plain rebuild changes.

    Where more than one keyset in the file carries the wanted revision the
    first in file order is used. Reading a file can do better than that, since
    a wrong keyset shows up as padding that is not zero, but writing one has
    no such check available: only a console can say. Revision 0x000A, the one
    PS3HEN wants, has exactly one NPDRM keyset in naehrwert's file, so this
    does not arise for the case it was written for.
    """
    if key_revision is None:
        return template.sce.key_revision, template.keyset
    revision = int(key_revision)
    if revision == template.sce.key_revision:
        return revision, template.keyset
    if revision == FAKE_KEY_REVISION:
        raise SigningFailed(
            "0x8000 is what a fake-signed SELF carries rather than a keyset "
            "revision, and a fake-signed file is built by the fself path "
            "rather than this one", path=template.path, field="key revision",
            expected="a retail key revision", found=revision)
    self_type = template.keyset_self_type
    if not self_type:
        raise SigningFailed(
            "this SELF type has no keysets, so it cannot be rebuilt against "
            "another key revision", path=template.path, field="SELF type",
            found=template.app_info.self_type)
    return revision, store.require_candidates(self_type, revision,
                                              template.path)[0]


def _control_flags(block, revision):
    """The control flags for the revision being signed at.

    A revision the survey does not cover keeps the template's flags, for the
    same reason the minimum firmware does: a field that decides what a console
    will load is never invented.
    """
    want = CONTROL_FLAGS_FOR_REVISION.get(revision)
    if want is None or len(want) != len(block.payload):
        return block
    return ControlInfo(block.info_type, block.size, block.next, want)


def _set_firmware(payload, was, now):
    """Move the minimum firmware version with the key revision.

    Only when the revision has actually changed, and only to a value read off
    real files. A revision this has not seen leaves the field alone rather
    than guessing at it: a wrong minimum firmware is a file the console
    refuses for a reason nothing on screen would explain.
    """
    if now is None or now == was:
        return
    version = FIRMWARE_FOR_REVISION.get(now)
    if version is None:
        return
    payload[DIGEST_FIRMWARE_AT:DIGEST_FIRMWARE_AT + 8] = (
        version.to_bytes(8, "big"))


def _npdrm_block(block, store, klicensee, filename):
    """The NPDRM control block with both of its hashes computed.

    Computed rather than copied. The CID_FN hash only changes when the file
    name does, and the CI hash covers this block's own first 0x60 bytes and
    nothing outside it, so for a rebuild under the same name both come out as
    the values the original carried. That is the point: a hash that is
    recomputed and agrees is a hash that has been checked, and one that is
    copied is only an assumption.
    """
    payload = bytearray(block.payload)
    if filename:
        content_id = bytes(payload[npdrm_hash.CONTENT_ID_AT:
                                   npdrm_hash.CONTENT_ID_AT
                                   + npdrm_hash.CONTENT_ID_BYTES])
        digest = npdrm_hash.cid_fn_hash(store, content_id, filename)
        payload[npdrm_hash.CID_FN_HASH_AT:
                npdrm_hash.CID_FN_HASH_AT + 16] = digest
    klic = effective_klicensee(store, klicensee)
    payload[npdrm_hash.CI_HASH_AT:npdrm_hash.CI_HASH_AT + 16] = (
        npdrm_hash.ci_hash(store, bytes(payload), klic))
    return ControlInfo(block.info_type, block.size, block.next, bytes(payload))


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
            store, elf_length=None, key_revision=None, keyset=None):
    """Everything up to the first section's bytes."""
    sce = template.sce
    keyset = keyset or template.keyset
    revision = sce.key_revision if key_revision is None else int(key_revision)
    head = bytearray(template.raw[:int(sce.header_length)])
    length = sce.data_length if elf_length is None else elf_length
    if length != sce.data_length or revision != sce.key_revision:
        # data_length is the length of the ELF inside, which changes when a
        # patch changes a segment. The key revision is what the console reads
        # to pick the keyset that opens the metadata info below, so the two
        # are written together or the file does not decrypt at all.
        updated = SceHeader(sce.magic, sce.version, revision,
                            sce.header_type, sce.metadata_offset,
                            sce.header_length, length)
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
    #
    # The keyset here is the one being written for, which is the template's
    # own unless a revision was asked for. The per-file metadata key and IV
    # inside the block are the template's either way: they are what the
    # section data is already encrypted under, and only the wrapping around
    # them changes.
    info = MetadataInfo(metadata.info.key, b"\x00" * 16, metadata.start_iv,
                        b"\x00" * 16)
    wrapped = aes.cbc_encrypt(keyset.erk, keyset.riv, info.pack())
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
