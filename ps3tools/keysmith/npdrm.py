"""The two NPDRM hashes, both AES-OMAC1 over the control block.

Neither is a guess. Both were worked out against the corpus and each is
confirmed on all fifteen retail files, across four titles, three key revisions
and both regions, as well as on a file scetool built and a console has played
online.

    cid_fn_hash = omac1(NP_tid, content_id + filename)
    ci_hash     = omac1(NP_ci XOR klicensee, block[0:0x60])

Two things follow that are worth stating plainly, because the first was the
open question in this package for a while.

The CI hash covers the first 0x60 bytes of the NPDRM control block and nothing
else. It does not cover the SELF header, the section table, the ELF digest or
the metadata. So patching a file, which moves all of those, does not change it:
the value a rebuild computes is the value the original carried. That is why
scetool's rebuild has a different one, and it is not because it recomputed
something we could not. It replaced the random pad, at 0x40, with the ASCII
"watermarktrololo", and the pad is inside the range the hash covers.

The CID_FN hash binds the content ID to the name the file has on the console.
That is the field that makes a perfectly valid SELF refuse to load when it is
written under a different name, so the name is asked for rather than assumed.
"""

from . import aes

#: Where the hash sits inside the block, and therefore how much it covers.
CI_HASH_AT = 0x60

#: Offsets inside the NPDRM control block payload.
CONTENT_ID_AT = 0x10
CONTENT_ID_BYTES = 0x30
CID_FN_HASH_AT = 0x50


def cid_fn_hash(store, content_id, filename):
    """Binds the content ID to the file name the console will see.

    content_id is the raw 0x30 byte field, nul padding included, because that
    is what is hashed. Handing it the trimmed text gives a different answer.
    """
    if len(content_id) != CONTENT_ID_BYTES:
        raise ValueError(f"the content ID field is {CONTENT_ID_BYTES} bytes, "
                         f"got {len(content_id)}")
    name = filename.encode() if isinstance(filename, str) else bytes(filename)
    return aes.omac1(store.named_key("NP_tid"), bytes(content_id) + name)


def ci_hash(store, block, klicensee):
    """The control info hash, over the block's own first 0x60 bytes.

    The key is NP_ci exclusive-ored with the klicensee, which is what ties the
    block to the title. A file signed with the wrong klicensee therefore has a
    hash that does not match, even though every visible field is right.
    """
    klic = bytes(klicensee)
    if len(klic) != 16:
        raise ValueError(f"a klicensee is sixteen bytes, got {len(klic)}")
    key = aes.xor_bytes(store.named_key("NP_ci"), klic)
    return aes.omac1(key, bytes(block)[:CI_HASH_AT])
