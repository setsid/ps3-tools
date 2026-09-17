"""Reading a SELF: headers, metadata, and the keys that open it.

Nothing here writes a file. Decryption of the section data lives in elf.py and
rebuilding lives in sign.py, so this module can be used to describe a file that
no key in hand will open.

The two-layer scheme, since it is the part that costs people an evening:

  * The metadata info is 0x40 bytes at metadata_offset + 0x20. For an NPDRM
    SELF it is wrapped twice. The klicensee is first decrypted in ECB with
    NP_klic_key to give the per-title key, that key decrypts the block in CBC
    with a zero IV, and the per-revision erk and riv decrypt what comes out.
    A non-NPDRM SELF has only the second of those layers.
  * Everything from metadata_offset + 0x60 up to the signature is one AES-CTR
    stream under the key and IV that block carried.

The check that the key was right is that the metadata info decrypts with two
blocks of padding that are zero. That is the only self-check the format gives,
so a wrong klicensee is reported as a wrong klicensee rather than as a parse
failure forty fields later.
"""

import struct
import zlib

from . import aes
from . import keys as keymod
from .errors import (DecryptionFailed, KeyNotFound, NotAnSce, TruncatedFile,
                     UnsupportedSce)
from .structs import (AppInfo, CONTROL_NPDRM, ControlInfo, ElfHeader,
                      MetadataHeader, MetadataInfo, MetadataSection, NpdrmInfo,
                      ProgramHeader, SCE_HEADER_SELF, SECTION_TYPE_PHDR,
                      SECTION_TYPE_SCEV, SECTION_TYPE_SHDR, SceHeader,
                      SceVersionInfo, SectionHeader, SectionInfo, SelfHeader)

# app_info.self_type values that mean the klicensee layer is present.
NPDRM_SELF_TYPE = 8

# A fake-signed SELF carries this key revision and no usable signature. scetool
# calls it DEBUG and refuses the file; stage four reads them instead.
DEBUG_KEY_REVISION = 0x8000

SELF_TYPE_NAMES_FOR_KEYS = {
    1: "LV0", 2: "LV1", 3: "LV2", 4: "APP", 5: "ISO", 6: "LDR",
    8: "NPDRM",
}


class Metadata:
    """The decrypted metadata: header, section headers, keys, optional data."""

    def __init__(self, info, header, sections, key_material, optional,
                 signature, raw_length, start_iv):
        self.info = info
        self.header = header
        self.sections = sections
        self.keys = key_material
        self.optional = optional
        self.signature = signature
        self.raw_length = raw_length
        self.start_iv = start_iv

    def key_at(self, index):
        at = index * 16
        if at + 16 > len(self.keys):
            raise DecryptionFailed(
                f"key index {index} is past the {len(self.keys) // 16} keys "
                f"this file carries", field="key index", found=index)
        return self.keys[at:at + 16]


class SelfFile:
    """One SELF, parsed. Decryption of the metadata is done on request."""

    def __init__(self, data, path=""):
        self.data = memoryview(bytes(data))
        self.raw = bytes(data)
        self.path = str(path)
        self.metadata = None
        self.keyset = None
        self._parse()

    # -- construction

    @classmethod
    def from_file(cls, path):
        with open(path, "rb") as handle:
            return cls(handle.read(), path)

    def _parse(self):
        data = self.raw
        path = self.path
        self.sce = SceHeader.read(data, 0, path)
        if self.sce.header_type != SCE_HEADER_SELF:
            raise UnsupportedSce(
                "this is not a SELF", path=path, field="SCE header type",
                expected="SELF", found=self.sce.type_name)
        self.self_header = SelfHeader.read(data, SceHeader.SIZE, path)
        self.app_info = AppInfo.read(data, self.self_header.app_info_offset,
                                     path)
        self.elf_header = ElfHeader.read(data, self.self_header.elf_offset,
                                         path)

        at = self.self_header.phdr_offset
        self.program_headers = []
        for index in range(self.elf_header.phnum):
            self.program_headers.append(ProgramHeader.read(
                data, at + index * ProgramHeader.SIZE, path))

        at = self.self_header.section_info_offset
        self.section_infos = []
        for index in range(self.elf_header.phnum):
            self.section_infos.append(
                SectionInfo.read(data, at + index * SectionInfo.SIZE, path))

        self.sce_version = SceVersionInfo.read(
            data, self.self_header.sce_version_offset, path)

        self.control_infos = []
        at = self.self_header.control_info_offset
        end = at + self.self_header.control_info_size
        while at < end:
            block = ControlInfo.read(data, at, path)
            self.control_infos.append(block)
            at += block.size

        # Section headers sit in the clear at the end of a retail SELF. A file
        # built without them is legal, so their absence is not an error.
        self.section_headers = []
        at = self.self_header.shdr_offset
        if at and self.elf_header.shnum:
            want = at + self.elf_header.shnum * SectionHeader.SIZE
            if want <= len(data):
                for index in range(self.elf_header.shnum):
                    self.section_headers.append(SectionHeader.read(
                        data, at + index * SectionHeader.SIZE, path))

    # -- what the file says about itself

    @property
    def is_npdrm(self):
        return self.app_info.self_type == NPDRM_SELF_TYPE

    @property
    def is_fake_signed(self):
        return self.sce.key_revision == DEBUG_KEY_REVISION

    @property
    def npdrm(self):
        """The NPDRM control block, or None if the file has none.

        None is the answer that matters: a SELF rebuilt without this block
        gives 8001000F on a console that checks licences, so callers compare
        against None rather than assuming it is there.
        """
        for block in self.control_infos:
            if block.info_type == CONTROL_NPDRM:
                return NpdrmInfo.read(block.payload, self.path)
        return None

    @property
    def keyset_self_type(self):
        return SELF_TYPE_NAMES_FOR_KEYS.get(self.app_info.self_type, "")

    # -- decryption

    def metadata_key_material(self, klicensee=b"", store=None):
        """The 0x40 bytes of metadata info, decrypted, and the keyset used.

        Every keyset for this revision is tried and the one whose padding comes
        out zero is the answer. That is deliberate: two keysets can carry the
        same revision, and picking the first and reporting a failure elsewhere
        is how an hour goes missing.
        """
        store = store or keymod.load()
        offset = self.sce.metadata_offset + SceHeader.SIZE
        if offset + MetadataInfo.SIZE > len(self.raw):
            raise TruncatedFile(
                "the metadata info runs past the end of the file",
                path=self.path, field="metadata offset",
                expected=offset + MetadataInfo.SIZE, found=len(self.raw))
        raw = self.raw[offset:offset + MetadataInfo.SIZE]

        if self.is_npdrm:
            klic = bytes(klicensee) if klicensee else b""
            if not klic:
                klic = store.named_key("NP_klic_free")
            if len(klic) != 16:
                raise KeyNotFound(
                    "a klicensee is sixteen bytes", path=self.path,
                    field="klicensee", expected=16, found=len(klic))
            per_title = aes.ecb_decrypt(store.named_key("NP_klic_key"), klic)
            raw = aes.cbc_decrypt(per_title, b"\x00" * 16, raw)

        self_type = self.keyset_self_type
        if not self_type:
            raise KeyNotFound(
                "this SELF type has no keysets", path=self.path,
                field="SELF type", found=self.app_info.self_type)
        candidates = store.require_candidates(self_type, self.sce.key_revision,
                                              self.path)
        for keyset in candidates:
            info = MetadataInfo.read(
                aes.cbc_decrypt(keyset.erk, keyset.riv, raw), 0, self.path)
            if info.looks_decrypted:
                return info, keyset
        raise DecryptionFailed(
            "the metadata info did not decrypt: its padding is not zero with "
            "any of the " + str(len(candidates)) + " keysets for key "
            "revision 0x%04X. " % self.sce.key_revision
            + ("The klicensee is the usual cause." if self.is_npdrm
               else "The key revision is the usual cause."),
            path=self.path, field="metadata info")

    def decrypt_metadata(self, klicensee=b"", store=None):
        """Reads the metadata header, section headers, keys and signature."""
        if self.metadata is not None:
            return self.metadata
        info, keyset = self.metadata_key_material(klicensee, store)
        self.keyset = keyset

        start = self.sce.metadata_offset + SceHeader.SIZE + MetadataInfo.SIZE
        # header_length covers everything up to the start of the section data,
        # so the encrypted metadata run is what is left of it after the point
        # the run begins.
        available = self.sce.header_length - start
        if available <= 0:
            raise TruncatedFile(
                "the header is too short to hold any metadata",
                path=self.path, field="header length",
                expected=f"> 0x{start:X}", found=self.sce.header_length)
        blob = aes.ctr_crypt(info.key, info.iv,
                             self.raw[start:start + available])

        header = MetadataHeader.read(blob, 0, self.path)
        at = MetadataHeader.SIZE
        sections = []
        for index in range(header.section_count):
            sections.append(MetadataSection.read(blob, at, self.path))
            at += MetadataSection.SIZE
        key_material = blob[at:at + header.key_count * 16]
        at += header.key_count * 16
        optional = blob[at:at + header.opt_header_size]
        at += header.opt_header_size
        signature = blob[at:]

        self.metadata = Metadata(info, header, sections, key_material,
                                 optional, signature, available, info.iv)
        return self.metadata

    # -- the figure scetool prints, for comparison only

    def printed_iv(self, metadata):
        """scetool prints the metadata IV after it has been used as a counter.

        Its CBC and CTR helpers write the counter back over the IV, and the
        print happens afterwards, so what it shows is the starting IV plus one
        per sixteen bytes of metadata. Reproduced here only so the two can be
        compared field for field without the difference looking like a bug.
        """
        blocks = (metadata.raw_length + 15) // 16
        value = int.from_bytes(metadata.start_iv, "big")
        return ((value + blocks) & ((1 << 128) - 1)).to_bytes(16, "big")

    # -- section data

    def section_bytes(self, section, metadata):
        """One metadata section's bytes, decrypted and decompressed.

        Compression is zlib with a header, so the length that comes out is the
        program header's file size rather than anything the section itself
        records. A section that decompresses to a different length is reported
        as that, because the alternative is an ELF that is quietly short.
        """
        start = section.data_offset
        end = start + section.data_size
        if end > len(self.raw):
            raise TruncatedFile(
                "a section runs past the end of the file", path=self.path,
                field=f"section {section.index} data",
                expected=end, found=len(self.raw))
        blob = self.raw[start:end]
        if section.encrypted == 3:
            key = metadata.key_at(section.key_index)
            counter = metadata.key_at(section.iv_index)
            blob = aes.ctr_crypt(key, counter, blob)
        if section.compressed == 2:
            try:
                blob = zlib.decompress(blob)
            except zlib.error as exc:
                raise DecryptionFailed(
                    f"a compressed section would not decompress ({exc}). The "
                    f"usual cause is the wrong key, because a section that "
                    f"decrypted wrongly is not valid zlib",
                    path=self.path,
                    field=f"section {section.index} data") from None
        return blob

    def to_elf(self, klicensee=b"", store=None):
        """The original ELF, rebuilt.

        The file is assembled at the offsets the ELF headers give rather than
        by concatenating sections in order, because the two are not the same:
        a PS3 SELF routinely carries segments whose file offsets leave gaps,
        and writing them end to end gives a file that is the right length and
        wrong everywhere after the first gap.
        """
        metadata = self.decrypt_metadata(klicensee, store)
        out = bytearray()

        def put(at, blob):
            end = at + len(blob)
            if end > len(out):
                out.extend(b"\x00" * (end - len(out)))
            out[at:end] = blob

        put(0, self.elf_header.pack())
        headers = b"".join(phdr.pack() for phdr in self.program_headers)
        put(self.elf_header.phoff, headers)

        for section in metadata.sections:
            blob = self.section_bytes(section, metadata)
            if section.section_type == SECTION_TYPE_PHDR:
                if section.index >= len(self.program_headers):
                    raise DecryptionFailed(
                        "a metadata section names a program header that is "
                        "not there", path=self.path,
                        field="metadata section index",
                        expected=f"< {len(self.program_headers)}",
                        found=section.index)
                phdr = self.program_headers[section.index]
                if section.compressed == 2 and len(blob) != phdr.filesz:
                    raise DecryptionFailed(
                        "a compressed section decompressed to the wrong "
                        "length", path=self.path,
                        field=f"segment {section.index}",
                        expected=phdr.filesz, found=len(blob))
                put(phdr.offset, blob)
            elif section.section_type == SECTION_TYPE_SHDR:
                put(self.elf_header.shoff, blob)
            elif section.section_type == SECTION_TYPE_SCEV:
                # The SCE version block. It has no home in the ELF and scetool
                # does not write it out either, so it is read and dropped.
                continue

        # Section headers sit in the clear in a retail SELF, and when they do
        # there is no metadata section carrying them.
        if self.section_headers and self.elf_header.shoff:
            blob = b"".join(shdr.pack() for shdr in self.section_headers)
            put(self.elf_header.shoff, blob)
        return bytes(out)
