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

The signature is made where a private key is to hand and carried where it is
not. The keys file holds a real private key for four NPDRM revisions, 0x0001,
0x0004, 0x0007 and 0x000A, and 0x000A is the one PS3HEN wants, so a file built
for HEN is signed here the way scetool signs one. Every other revision in the
file ships zeros for the private key, which is why a retail rebuild carries
the template's signature through. See ecdsa.py.

What is carried through unchanged, because it cannot be derived:

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

The evidence is a third party's paired binaries, which ship a CFW build and a
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

A HEN build differs in more than the revision, and the last of those
differences is that its section table cannot come from the template at all.
The custom firmware builds in the survey were made with scetool's
skip-sections option, so they describe only the program headers the loader
needs: five of the eight in default_mp.self and three of the eight in
default.self. The HEN builds of the same ELFs describe all eight. A template
built that way does not say what the file being built has to contain, so at
key revision 0x000A the table is built from the ELF's own program headers
instead, with fresh key material for the sections the template never had. See
REBUILDS_SECTION_TABLE below for the figures.
"""

import hashlib
import hmac
import os
import zlib

from . import aes
from . import ecdsa
from . import keys as keymod
from . import npdrm as npdrm_hash
from .errors import SigningFailed
from .self import effective_klicensee
from .structs import (COMPRESSED, CONTROL_DIGEST, CONTROL_FLAGS,
                      CONTROL_NPDRM, METADATA_ENCRYPTED, METADATA_PLAIN,
                      UNCOMPRESSED,
                      ControlInfo, MetadataHeader, MetadataInfo,
                      MetadataSection, SECTION_TYPE_PHDR, SECTION_TYPE_SHDR,
                      SceHeader, SelfHeader)

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
#: 0x001C carries 42000. the third party's PS3HEN builds of Modern Warfare 2
#: carry 35500, which is 3.55 and is the value that belongs with 0x000A, and
#: he set it deliberately: his own custom firmware builds of the same binary
#: leave it at zero. Confirmed on both the European and American releases.
#:
#: So re-signing to another key revision moves this with it. Leaving 3.60 in a
#: file signed against the 3.55 keyset says two different things about the
#: same file, and this is the field that says which firmware will load it.
#: The control flags a build for a given key revision carries.
#:
#: Surveyed rather than assumed. All fourteen of the PS3HEN
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


#: The key table, slot by slot.
#:
#: A slot is sixteen bytes and a section's slots run together from its own
#: sha1_index: the 20 byte hash first, which spans two slots and leaves the
#: rest of the second zero, then the 0x40 byte HMAC key in four slots, then
#: the AES key and the counter in one each. An unencrypted section stops
#: after the HMAC key, because it has neither.
#:
#: Counted off real files rather than taken from a document, and the counting
#: checks out on every one of them. The PS3HEN build of default_mp.self has
#: eight encrypted sections and one unencrypted, 8 * 8 + 6 = 70 = 0x46, which
#: is the key count it carries; the custom firmware build of the same ELF has
#: five and one, 5 * 8 + 6 = 46 = 0x2E, which is its. Every retail file in
#: the corpus has five encrypted sections, a type 3 section and a type 1
#: section, 5 * 8 + 6 + 6 = 52 = 0x34, which is what all fifteen carry.
SLOT_BYTES = 16
HASH_BYTES = 20
HMAC_KEY_BYTES = 0x40
HMAC_KEY_SLOT = 2
AES_KEY_SLOT = 6
AES_IV_SLOT = 7
ENCRYPTED_SLOTS = 8

#: What a metadata section carries where a plain section has no key or IV.
NO_KEY_INDEX = 0xFFFFFFFF

#: metadata_section_header.hashed. Every section of every file read here
#: carries 2, template, paired build and retail alike. What the field means
#: beyond that is not known, so it is written as found rather than derived.
SECTION_HASHED = 2


def _hmac_key(key_table, section):
    """The 0x40 byte key a section's HMAC uses.

    Taken from the table two slots after the hash's own, which is where the
    layout above puts it. Confirmed against every section of every file in
    the corpus rather than taken from a document.
    """
    at = (section.sha1_index + HMAC_KEY_SLOT) * SLOT_BYTES
    key = bytes(key_table[at:at + HMAC_KEY_BYTES])
    if len(key) != HMAC_KEY_BYTES:
        raise SigningFailed(
            "the key table is too short to hold this section's HMAC key",
            field="sha1 index", found=len(key_table),
            expected=f"{at + HMAC_KEY_BYTES} bytes")
    return key


def _key_at(key_table, index):
    """One slot out of a key table that is being built rather than read."""
    at = index * SLOT_BYTES
    key = bytes(key_table[at:at + SLOT_BYTES])
    if len(key) != SLOT_BYTES:
        raise SigningFailed(
            "the key table is too short to hold this section's key",
            field="key index", found=len(key_table),
            expected=f"{at + SLOT_BYTES} bytes")
    return key


def _fresh_key_material(encrypted):
    """The slots one new section needs, with the hash left at zero.

    The hash is written once the payload it covers is known. Everything else
    is random, because it is stored in the file next to the data it protects
    and nothing outside the file has to agree with it.
    """
    material = bytearray(SLOT_BYTES * HMAC_KEY_SLOT)
    material += os.urandom(HMAC_KEY_BYTES)
    if encrypted:
        material += os.urandom(SLOT_BYTES)              # the AES key
        material += os.urandom(SLOT_BYTES)              # the counter
    return material


#: Whether a build for this key revision compresses the sections it can.
#:
#: Surveyed the way the control flags were, across fourteen paired builds of
#: each kind. Every custom firmware build stores every section uncompressed,
#: one pattern with no exceptions. Every PS3HEN build stores a section
#: compressed wherever compressing it makes it smaller and stores it as it is
#: wherever it does not, which is what the four sections of default_mp.self
#: that stay uncompressed have in common: they are 0, 4, 0x20 and 0x740 bytes
#: and only the last of those is worth packing. The rule reproduces all nine
#: sections of the multiplayer build and all nine of the single player build
#: byte for byte, so it is the rule scetool used rather than a description of
#: one file.
#:
#: This was taken for a build choice at first and it is not. A patch with the
#: other three fields right went on to a HEN console cleanly and the console
#: black screened at the moment the patched binary loaded, so the three were
#: necessary and not sufficient.
COMPRESSES_SECTIONS = {
    0x000A: True,
}

#: Whether a build for this key revision has its section table built from the
#: ELF rather than copied from the template.
#:
#: The fifth difference, and the one that was still wrong when the other four
#: were right: that patch black screened on a real HEN console at the moment
#: the binary loaded. Comparing the paired BLUS30377 builds of
#: default_mp.self, both of them scetool's work from the same eight program
#: header ELF:
#:
#:     custom firmware  6 sections, key count 0x2E, header length 0x980
#:                      type 2 for program headers 0 to 4, then type 1
#:     PS3HEN           9 sections, key count 0x46, header length 0xB80
#:                      type 2 for program headers 0 to 7, then type 1
#:
#: The custom firmware build leaves out program headers 5, 6 and 7, which is
#: scetool's skip-sections option, and the single player pair goes further:
#: its custom firmware build describes three of the eight and its HEN build
#: again describes all eight. So on the HEN path the table has to be built
#: from the ELF. All fourteen HEN builds carry the same nine sections and the
#: same key count, seven regions across both trees.
REBUILDS_SECTION_TABLE = {
    0x000A: True,
}

#: scetool aligns the header to this. Worked out from where the signature
#: ends: the metadata run is followed by a 0x2A byte signature and then by
#: padding to the next multiple of 0x80. That holds on all fifteen retail
#: files and on all twenty-eight paired builds, and no smaller alignment
#: accounts for all three header lengths, 0x800, 0x980 and 0xB80.
HEADER_ALIGN = 0x80

#: The signature is two 21 byte halves. Both halves of the one scetool wrote
#: on the PS3HEN builds start with a zero byte, which is what a 160 bit value
#: in 21 bytes looks like, and the 0x2A bytes together account for exactly
#: the gap between where the metadata says the signature starts and where the
#: header ends once the alignment above is allowed for.
#:
#: Read off their files rather than assumed: signature_input_length is 0xB40
#: and header_length is 0xB80 on all fourteen PS3HEN builds, and the 0x40
#: bytes between them are 0x2A of signature followed by 0x16 of zero. Both
#: halves of all twenty-nine real signatures read here verify against the
#: public key when they are taken this way round, r first.
SIGNATURE_BYTES = ecdsa.SIGNATURE_BYTES


def copy_compression(metadata_value):
    """The section info's spelling of what a metadata section records.

    The two tables use the same two numbers for the same idea, which is the
    one mercy in this format.
    """
    return COMPRESSED if metadata_value == COMPRESSED else UNCOMPRESSED


def compresses_for(revision):
    """Whether a build at this revision compresses what it can.

    False for every revision the survey does not cover.
    """
    return bool(COMPRESSES_SECTIONS.get(revision))


def rebuilds_section_table(revision):
    """Whether to build the section table from the ELF at this revision.

    False for every revision the survey does not cover, which leaves the
    custom firmware path on the template's own table.
    """
    return bool(REBUILDS_SECTION_TABLE.get(revision))


def section_payloads(template, elf, metadata):
    """The plaintext bytes each metadata section should carry, in order.

    Plaintext here means compressed if the section is compressed, because the
    HMAC and the encryption are both over that, not over the raw segment.

    Compression follows the template section by section. The PS3HEN path does
    not come through here at all: it builds its own table, and what it
    compresses is decided there.
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
            if section.compressed == COMPRESSED:
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
    if rebuilds_section_table(key_revision):
        return _rebuild_from_elf(template, elf, metadata, klicensee, store,
                                 filename, key_revision, revision, keyset)
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
        digest = hmac.new(_hmac_key(metadata.keys, section), payload,
                          hashlib.sha1).digest()
        at = section.sha1_index * SLOT_BYTES
        key_table[at:at + HASH_BYTES] = digest
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

    control_infos = _control_infos(template, elf, store, klicensee, filename,
                                   key_revision, unchanged)
    blob = _metadata_blob(metadata.header, new_sections, key_table,
                          metadata.optional, metadata.signature,
                          metadata.raw_length)
    header = _header(template, control_infos, section_infos, metadata, blob,
                     klicensee, store, data_length=len(elf),
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


def _rebuild_from_elf(template, elf, metadata, klicensee, store, filename,
                      key_revision, revision, keyset):
    """A SELF whose section table comes from the ELF, which is the HEN form.

    The template still says everything that identifies the file, and the ELF
    says what is in it. Nothing of the template's own table survives: the
    section headers are built from the program headers, the key table is
    built to match and the sections are laid out from the end of a header
    whose length the new table decides.

    The custom firmware builds in the survey describe only some of their
    program headers, so a template built that way cannot be copied into a
    file that has to describe all of them. REBUILDS_SECTION_TABLE has the
    figures.

    The type 3 section a retail template carries is dropped here, the same as
    the rest of the table. It holds a build comment table that is nowhere in
    the ELF, so there is nothing to rebuild it from, and scetool drops it
    too.
    """
    sections, payloads, key_table = _elf_section_table(
        template, elf, compresses_for(key_revision))
    key_count = len(key_table) // SLOT_BYTES
    signature_at, header_length = _header_geometry(template, metadata,
                                                   len(sections), key_count)

    at = header_length
    offsets = []
    for payload in payloads:
        offsets.append(at)
        at += len(payload)
        at = (at + SECTION_ALIGN - 1) // SECTION_ALIGN * SECTION_ALIGN

    # Every section is hashed and every encrypted one is encrypted, because
    # none of this came out of the template and there is nothing to carry.
    stored = []
    for section, offset, payload in zip(sections, offsets, payloads):
        section.data_offset = offset
        digest = hmac.new(_hmac_key(key_table, section), payload,
                          hashlib.sha1).digest()
        hash_at = section.sha1_index * SLOT_BYTES
        key_table[hash_at:hash_at + HASH_BYTES] = digest
        if section.encrypted == METADATA_ENCRYPTED:
            payload = aes.ctr_crypt(_key_at(key_table, section.key_index),
                                    _key_at(key_table, section.iv_index),
                                    payload)
        stored.append(payload)

    control_infos = _control_infos(template, elf, store, klicensee, filename,
                                   key_revision, unchanged=False)
    section_infos = _elf_section_infos(template, sections)
    head = MetadataHeader(signature_at, metadata.header.unknown1,
                          len(sections), key_count,
                          metadata.header.opt_header_size,
                          metadata.header.unknown2, metadata.header.unknown3)
    metadata_at = _metadata_at(template)
    # The field starts out zero where this can sign, and the signature goes
    # in once the header it covers is finished. Where it cannot, the
    # template's own is carried and nothing is computed over the header at
    # all.
    room = header_length - signature_at
    signable = can_sign(keyset)
    field = (bytes(room) if signable
             else _signature_field(metadata, signature_at, header_length))
    blob = _metadata_blob(head, sections, key_table, metadata.optional,
                          field, header_length - metadata_at)

    # scetool writes the file size less the header where a retail file writes
    # the length of the ELF inside. Both conventions are in the corpus: all
    # twenty-eight paired builds have data_length exactly file size minus
    # header length, and all fifteen retail files have the length of their
    # ELF. The retail one is what makes the round trip byte-identical, so it
    # stays on that path, and scetool's is what a HEN console has been shown
    # to load, so it is what this path writes.
    end = max(offset + len(payload)
              for offset, payload in zip(offsets, payloads))
    header = _header(template, control_infos, section_infos, metadata, blob,
                     klicensee, store, data_length=end - header_length,
                     key_revision=revision, keyset=keyset,
                     header_length=header_length,
                     shdr_offset=_shdr_offset(sections),
                     sign_at=signature_at if signable else None)
    out = bytearray(header)
    for offset, blob_bytes in zip(offsets, stored):
        if offset > len(out):
            out.extend(b"\x00" * (offset - len(out)))
        out[offset:offset + len(blob_bytes)] = blob_bytes
    return bytes(out)


def _elf_section_table(template, elf, compress):
    """(the metadata sections, their payloads, a key table for them).

    One type 2 section per program header and then the type 1 section header
    table, which is the shape all fourteen PS3HEN builds have. The sections
    carry no offsets yet, because where they go depends on how long the
    header holding this table turns out to be.

    Compression is decided per section by whether it helps. See
    COMPRESSES_SECTIONS for what that reproduces.
    """
    sections = []
    payloads = []
    key_table = bytearray()
    slot = 0
    for index, phdr in enumerate(template.program_headers):
        body = elf[phdr.offset:phdr.offset + phdr.filesz]
        if len(body) != phdr.filesz:
            raise SigningFailed(
                "the ELF is too short to hold a segment its own program "
                "headers describe", path=template.path,
                field=f"segment {index}", expected=phdr.filesz,
                found=len(body))
        packed = zlib.compress(body, ZLIB_LEVEL) if compress else body
        smaller = len(packed) < len(body)
        sections.append(MetadataSection(
            0, len(packed) if smaller else len(body), SECTION_TYPE_PHDR,
            index, SECTION_HASHED, slot, METADATA_ENCRYPTED,
            slot + AES_KEY_SLOT, slot + AES_IV_SLOT,
            COMPRESSED if smaller else UNCOMPRESSED))
        payloads.append(packed if smaller else body)
        key_table += _fresh_key_material(encrypted=True)
        slot += ENCRYPTED_SLOTS

    # The section header table, read off the template rather than out of the
    # ELF: the decrypted ELF carries it, but taking it from there would gain
    # a second source of truth for the same bytes and lose nothing.
    #
    # A SELF without one is legal, and there is then no section to write.
    #
    # Its index is one past the sections written, which is what both of their
    # builds carry: 6 where five are written and 9 where eight are. Which
    # rule scetool used cannot be told from any file on hand, because the
    # sections it writes are always the first n program headers, so "one past
    # the count" and "two past the last index" give the same number every
    # time. Every program header is written here, so the two agree here as
    # well.
    table = b"".join(shdr.pack() for shdr in template.section_headers)
    if table:
        sections.append(MetadataSection(
            0, len(table), SECTION_TYPE_SHDR, len(sections) + 1,
            SECTION_HASHED, slot, METADATA_PLAIN, NO_KEY_INDEX, NO_KEY_INDEX,
            UNCOMPRESSED))
        payloads.append(table)
        key_table += _fresh_key_material(encrypted=False)
    return sections, payloads, key_table


def _metadata_at(template):
    """Where the encrypted metadata run starts in the file."""
    return (int(template.sce.metadata_offset) + SceHeader.SIZE
            + MetadataInfo.SIZE)


def _header_geometry(template, metadata, section_count, key_count):
    """(where the signature starts, how long the whole header is).

    signature_input_length is an offset from the start of the file to the
    first byte of the signature, which is the same as saying everything
    before it is signed. Checked against all forty-three files read here: the
    figure each one carries is exactly its metadata header, section table,
    key table and optional header added up.

    The header then runs to the end of the signature, aligned. See
    HEADER_ALIGN and SIGNATURE_BYTES.
    """
    signature_at = (_metadata_at(template) + MetadataHeader.SIZE
                    + section_count * MetadataSection.SIZE
                    + key_count * SLOT_BYTES
                    + metadata.header.opt_header_size)
    end = signature_at + SIGNATURE_BYTES
    length = (end + HEADER_ALIGN - 1) // HEADER_ALIGN * HEADER_ALIGN
    return signature_at, length


def can_sign(keyset):
    """Whether this keyset can sign rather than only check a signature.

    Four NPDRM revisions in the keys file carry a real private key, 0x0001,
    0x0004, 0x0007 and 0x000A, and PS3HEN wants 0x000A. Every other revision
    carries zeros there, so asking the keyset is the whole test and no table
    of revisions is needed: a keys file that gains a private key gains the
    ability to sign with it.

    Each of those four was checked the only way that settles it, which is
    that the private key times the base point is the public key the same
    keyset carries. ecdsa.py has the figures.
    """
    private = bytes(getattr(keyset, "private", b"") or b"")
    return any(private)


def _signature_field(metadata, signature_at, header_length):
    """The signature, carried through, padded out to fill the header.

    This is the path for a keyset with no private key, which is every
    revision a retail file is signed at. The template is then the only place
    the file's signature can come from, and a rebuild of a file nobody has
    touched carries the one it came with, which is what makes the round trip
    byte-identical.
    """
    carried = bytes(metadata.signature[:SIGNATURE_BYTES])
    return carried.ljust(header_length - signature_at, b"\x00")


def _signed_metadata(head, blob, run_at, sign_at, keyset, path):
    """The metadata run with a real signature written into it.

    head holds the whole header with the metadata run and the metadata info
    block still in the clear, which is the form the signature covers.
    scetool builds the header, signs it, writes the signature at the end of
    the metadata run and encrypts the run afterwards, so the bytes that were
    hashed are never the bytes on disk. Checked by verifying real
    signatures both ways round: the plaintext form verifies on all fifteen
    retail files here and on all fourteen PS3HEN builds in the survey, and
    the on-disk form verifies on none of them.

    The signature sits at sign_at, which is signature_input_length, so
    everything the hash covers is in front of it and writing it afterwards
    cannot disturb it.

    The result is verified against the keyset's own public key before it is
    returned. That costs one scalar multiplication and it catches the one
    failure that would otherwise leave here quietly: a keyset whose private
    key and ctype do not belong together would sign with one curve's key on
    another curve's arithmetic, and the file would be refused by a console
    with nothing on screen to say why.
    """
    curve = ecdsa.curve(keyset.curve_type)
    digest = ecdsa.digest_of(bytes(head[:sign_at]))
    signature = ecdsa.sign(curve, keyset.private, digest)
    if len(signature) != SIGNATURE_BYTES:
        raise SigningFailed(
            "the signature came out the wrong length", path=path,
            field="signature", expected=SIGNATURE_BYTES,
            found=len(signature))
    at = sign_at - run_at
    if at < 0 or at + SIGNATURE_BYTES > len(blob):
        raise SigningFailed(
            "the metadata run has no room for a signature where the header "
            "says one goes", path=path, field="signature input length",
            expected=f"{at + SIGNATURE_BYTES} bytes of metadata",
            found=len(blob))
    if not ecdsa.verify(curve, keyset.public, digest, signature):
        raise SigningFailed(
            "the signature this keyset produced does not verify against the "
            "public key the same keyset carries, so the private key and the "
            "curve its ctype names do not belong together",
            path=path, field="signature",
            found=f"ctype 0x{keyset.curve_type:02X}")
    out = bytearray(blob)
    out[at:at + SIGNATURE_BYTES] = signature
    return bytes(out)


def _shdr_offset(sections):
    """Where the section header table ended up, or None if there is none."""
    for section in sections:
        if section.section_type == SECTION_TYPE_SHDR:
            return section.data_offset
    return None


def _elf_section_infos(template, sections):
    """The SELF's own section info table, saying the same thing again.

    One entry per program header, which is the length the template's table
    already is. The encrypted field is carried: it is 1 for the loaded
    segments and 0 for the three that are not loaded in every one of the
    forty-three files read here, so there is nothing to derive and a value
    that is read off the file it came from cannot be wrong.

    The two tables disagreeing is a file that reads differently depending on
    which one a reader trusts, and a section compressed on the way in and
    recorded as stored is a section the console will not unpack.
    """
    infos = [_copy_info(info) for info in template.section_infos]
    for section in sections:
        if section.section_type != SECTION_TYPE_PHDR:
            continue
        if section.index >= len(infos):
            raise SigningFailed(
                "the template has fewer section infos than the ELF has "
                "program headers", path=template.path, field="section info",
                expected=f"> {section.index}", found=len(infos))
        info = infos[section.index]
        info.offset = section.data_offset
        info.size = section.data_size
        info.compressed = copy_compression(section.compressed)
    return infos


def _control_infos(template, elf, store, klicensee, filename, key_revision,
                   unchanged):
    """The control blocks, with the digest, the flags and both NPDRM hashes.

    The file digest covers the ELF that was signed. That this is a plain SHA1
    of the ELF was verified against a known-good scetool build, whose value
    is exactly the SHA1 of the ELF it was handed, and the paired builds agree
    with each other on it as well.

    When nothing has changed it is carried through instead. A retail SELF is
    signed over the complete ELF Sony held, and the ELF that comes back out
    of one is not quite that file: the type 3 metadata section is not stored
    anywhere in it, so its bytes decrypt to a hole. Recomputing from the ELF
    this package can produce would therefore replace a correct digest with a
    different one for a file nobody has touched.
    """
    moved = (key_revision is not None
             and key_revision != template.sce.key_revision)
    control_infos = []
    for block in template.control_infos:
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
    return control_infos


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


def _metadata_blob(header, sections, key_table, optional, signature,
                   expected):
    """The metadata run, in the clear, exactly as long as it has to be.

    expected is how much room the header leaves for it, so a table that came
    out the wrong size is caught here rather than as a file the console
    quietly refuses.
    """
    parts = [header.pack()]
    parts += [section.pack() for section in sections]
    parts.append(bytes(key_table))
    parts.append(bytes(optional))
    parts.append(bytes(signature))
    blob = b"".join(parts)
    if len(blob) != expected:
        raise SigningFailed(
            "the rebuilt metadata does not fill the room the header leaves "
            "for it", field="metadata length", expected=expected,
            found=len(blob))
    return blob


def _header(template, control_infos, section_infos, metadata, blob, klicensee,
            store, data_length=None, key_revision=None, keyset=None,
            header_length=None, shdr_offset=None, sign_at=None):
    """Everything up to the first section's bytes.

    header_length and shdr_offset are for the path that builds its own
    section table, where the metadata is a different size from the
    template's and the section header table lands somewhere else. Left out,
    both stay as the template had them, which is what a rebuild that keeps
    the layout wants.

    sign_at is where the signature goes, and giving it is what asks for one
    to be made over the header this builds. Left out, whatever the blob
    already carries there is written as it stands, which is the template's
    own signature.
    """
    sce = template.sce
    keyset = keyset or template.keyset
    revision = sce.key_revision if key_revision is None else int(key_revision)
    head = bytearray(template.raw[:int(sce.header_length)])
    length = sce.data_length if data_length is None else data_length
    room = int(sce.header_length if header_length is None else header_length)
    if room > len(head):
        head.extend(b"\x00" * (room - len(head)))
    del head[room:]
    if (length != sce.data_length or revision != sce.key_revision
            or room != sce.header_length):
        # data_length is the length of the ELF inside on this path, and it
        # changes when a patch changes a segment. The key revision is what
        # the console reads to pick the keyset that opens the metadata info
        # below, so the two are written together or the file does not
        # decrypt at all.
        updated = SceHeader(sce.magic, sce.version, revision,
                            sce.header_type, sce.metadata_offset,
                            room, length)
        head[0:SceHeader.SIZE] = updated.pack()

    if shdr_offset is not None:
        # The section header table has moved and the SELF header is the only
        # place that says where it is. A stale offset here gives a file whose
        # section headers read as whatever happens to sit at the old address.
        own = template.self_header
        moved = SelfHeader(own.header_type, own.app_info_offset,
                           own.elf_offset, own.phdr_offset, shdr_offset,
                           own.section_info_offset, own.sce_version_offset,
                           own.control_info_offset, own.control_info_size,
                           own.padding)
        head[SceHeader.SIZE:SceHeader.SIZE + SelfHeader.SIZE] = moved.pack()

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
    info_at = sce.metadata_offset + SceHeader.SIZE
    run_at = info_at + MetadataInfo.SIZE

    # Both go in unencrypted first, because that is the form the signature
    # covers: the whole header with the metadata info block and the metadata
    # run in the clear. Signing then encrypting is scetool's order, and the
    # bytes that were hashed are never the bytes on disk.
    head[info_at:info_at + MetadataInfo.SIZE] = info.pack()
    head[run_at:run_at + len(blob)] = blob
    if sign_at is not None:
        blob = _signed_metadata(head, blob, run_at, sign_at, keyset,
                                template.path)

    wrapped = aes.cbc_encrypt(keyset.erk, keyset.riv, info.pack())
    if template.is_npdrm:
        klic = bytes(klicensee) or store.named_key("NP_klic_free")
        per_title = aes.ecb_decrypt(store.named_key("NP_klic_key"), klic)
        wrapped = aes.cbc_encrypt(per_title, b"\x00" * 16, wrapped)
    head[info_at:info_at + MetadataInfo.SIZE] = wrapped
    encrypted = aes.ctr_crypt(metadata.info.key, metadata.start_iv, blob)
    head[run_at:run_at + len(encrypted)] = encrypted
    return bytes(head)
