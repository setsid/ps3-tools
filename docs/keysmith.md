# keysmith

PS3 SELF and fake-signed SELF handling, written in Python against the standard
library alone. It replaces scetool, which this program used to bundle.

## Why it exists

scetool is naehrwert's, it is very good, and it is a 2012 Windows binary. Every
one of these cost time:

- It writes its whole report to stderr, so a caller that captured stdout saw
  nothing and concluded it had failed.
- It resolves `data/keys` relative to the working directory, so the same
  command worked or failed depending on which folder it was typed in.
- A keys file with the wrong line endings comes back as "Could not decrypt
  header", which says nothing about lines.
- Its flags do not describe what they do. `-c USPRX` sets application type
  0x20; `-b FREE` sets licence type 0x03 and `-b LOCAL` sets 0x02.
- It is an unsigned executable of that age, so antivirus flags it and users
  report the download as malware.
- It cannot read or write a fake-signed SELF at all, which is why the digital
  releases needed TrueAncestor's `unfself.exe` and could not be patched end to
  end.

None of the cryptography is novel. What is unforgiving is byte-exactness, and
that is what the corpus is for.

## The public API

```python
from ps3tools import keysmith

described = keysmith.inspect(path, klicensee)   # a report and parsed fields
elf       = keysmith.decrypt(path, klicensee)   # SELF or fself to ELF
rebuilt   = keysmith.sign(elf, path, klicensee) # ELF back into its container
faked     = keysmith.fake_sign(elf, path, klicensee)   # the fake-signed form
```

`sign` also takes `key_revision`, which rebuilds the file against another
keyset instead of the template's own. That is the PS3HEN case and it is
covered below.

`fake_sign` builds a fake-signed SELF. A fake-signed SELF turns out to be its
header followed by the ELF verbatim, and the header offsets are computed from
the program header count rather than written down, so a binary with a
different count does not get overlapping tables. The case it is right for is a
template that arrives already fake signed, which is how the digital releases
ship: such a file has no keyset and no metadata, so it goes back out in the
form it came in.

Every fake-signed file in the corpus is zero across the whole NPDRM block,
which is why those files answer 8001000F. `fake_sign` carries the retail block
through whole and refuses a template whose block has no magic.

All three take a path or bytes. All three raise `keysmith.SceError` on failure,
and the message names the file, the field, what was expected and what was
found. Nothing prints a warning and carries on.

`keysmith.sign` is the public function, so it takes the name of the module it
is implemented in. The module is reached through `sys.modules` where anything
needs it, which only the tests do.

## What PS3HEN wants, and it is not a fake-signed file

HEN runs on 4.8x firmware and loads a SELF through the 3.55-era appldr keyset.
So a HEN console wants the same retail re-sign a CFW console wants, built
against key revision 0x000A. That is what the community advice to "resign to
3.55" means in this format, and it is `keysmith.sign(..., key_revision=0x000A)`
rather than `fake_sign`.

The evidence is a third party's paired binaries, which ship a CFW build and a
HEN build of Modern Warfare 2 for all seven regions. Reading a matched pair
against each other:

- Neither is fake signed. Both are ordinary retail re-signs, both scetool
  built.
- The whole NPDRM block is byte-identical between them: content ID, CID_FN
  hash, header hash, licence type 3, application type 0x20, and the pad at
  0x40 as well.
- The only header field that differs is the SCE key revision, 0x0010 on the
  CFW build and 0x000A on the HEN one.

Writing a revision does two things at once: the number goes into the SCE
header, and the metadata info is wrapped under that revision's `erk` and
`riv`. The console reads the first to choose the second, so setting either
without the other gives a file whose header will not decrypt at all. Where
more than one keyset in the file carries the wanted revision the first in file
order is used; reading a file can tell which keyset was right, because a wrong
one leaves padding that is not zero, and writing one has no such check.
Revision 0x000A has exactly one NPDRM keyset, so that does not arise here.

An earlier version of this program fake-signed for HEN, on the reasoning that
a retail re-sign carries a signature that cannot be regenerated and that HEN
checks it. The paired binaries say otherwise, and that reasoning is not in the
tree any more.

## Layout

    ps3tools/keysmith/
        __init__.py   the three public calls
        self.py       parsing and decrypting a signed SELF
        fself.py      fake-signed SELFs, which need no keys at all
        keys.py       the keyset file, and where it is found
        sign.py       rebuilding a SELF from a template and an ELF
        structs.py    the on-disk structures
        aes.py        AES-128/192/256, ECB, CBC, CTR and OMAC1
        npdrm.py      the two NPDRM control block hashes
        report.py     the description, laid out the way scetool laid it out
        errors.py     failures that name the file and the field
        data/keys     the keyset, inside the package but not in the repo

Nothing in the package imports the rest of the application, so it can be lifted
into a repository of its own.

`data/keys` is naehrwert's and is not redistributed with this source, the same
as scetool never was. Put a copy of scetool's `data` folder there. Everything
that needs it says where it goes when it is missing, and the tests that need it
skip rather than fail.

## How it is checked

Every gate is external. A test asserting this package's output against this
package's own parser would prove nothing.

| Stage | What is checked | Against |
| --- | --- | --- |
| Parse | every field of the report | `scetool -i` on the same file |
| Decrypt | the ELF, byte for byte | `scetool -d`, and a known-good SHA1 |
| Re-sign | the whole file, byte for byte | the user's own unmodified retail files |
| fself | the whole file, byte for byte | the file itself, and `unfself.exe` |

The retail files are not in the repository. Tests that need one skip with the
path in the message. `KEYSMITH_CORPUS=1` runs the large samples as well as the
two small ones, which is about three minutes.

## What is recomputed and what is carried

Recomputed, because it depends on the payload:

- section offsets and sizes, in both the metadata and the SELF's section infos
- the per-section HMAC-SHA1, keyed from the slot two after the hash's own
- the file digest, which is the SHA1 of the ELF that was signed
- both NPDRM hashes, the CID_FN hash and the CI hash
- `data_length` in the SCE header

Carried through unchanged, because it identifies the file:

- key revision unless the caller asks for another, authentication ID, vendor
  ID, SELF type, application version
- the whole NPDRM block: licence type, application type, content ID, CID_FN
  hash

That list is not decoration. TrueAncestor drops the NPDRM block, and the Black
Ops 2 Eboot-Self Builder writes application type 0x00 and key revision 0x0010
over a file whose own values are 0x20 and 0x001C. Both produce a file that is
structurally valid and answers 8001000F on a console that checks licences.

Carried through because it cannot be derived:

- **The signature.** These files are signed with a private key nobody outside
  Sony has, and the public keyset ships zeros for it. scetool cannot sign
  either.
- **The type 3 metadata section.** It holds a build comment table that is not
  in the decrypted ELF at all. A rebuild working from the ELF alone drops it,
  which is what scetool does.
- Every field named `unknown` in `structs.py`.

## The two NPDRM hashes

Both are AES-OMAC1 over the NPDRM control block, and both are confirmed on all
fifteen retail files, across four titles, three key revisions and both regions,
as well as on a file scetool built that a console has played online with.

    cid_fn_hash = omac1(NP_tid, content_id + filename)
    ci_hash     = omac1(NP_ci XOR klicensee, block[0:0x60])

The exclusive-or with the klicensee is what took the longest to find, and it is
what ties the block to the title: a file signed with the wrong klicensee has a
CI hash that does not match even though every visible field is right.

**The CI hash covers the first 0x60 bytes of the control block and nothing
else.** Not the SELF header, not the section table, not the ELF digest, not the
metadata. Patching a file moves all of those and leaves the hash where it was,
which is why a rebuild computes the same value the original carried. That is
also why scetool's rebuild has a different one: it replaces the random pad at
0x40 with the ASCII "watermarktrololo", and the pad is inside the covered
range.

The CID_FN hash binds the content ID to the name the file has on the console.
That is the field that makes a perfectly valid SELF refuse to load when it is
written under a different name, so `sign()` takes the name rather than assuming
it.

Both are computed rather than copied. For a rebuild under the same name they
come out as the values the original carried, and the round trip tests rely on
exactly that: a hash that is recomputed and agrees has been checked, and one
that is copied is only an assumption.

## What a PS3HEN build differs in

Five things, surveyed across a third party's paired builds of Modern
Warfare 2, seven regions in both the multiplayer and the campaign trees, and
checked against every stock file here:

| Field | Custom firmware | PS3HEN |
| --- | --- | --- |
| key revision | the file's own | 0x000A |
| minimum firmware | the file's own | 35500, which is 3.55 |
| control flags | zero | `40 00 ... 00 02` |
| compression | none | wherever it makes a section smaller |
| section table | the template's own | built from the ELF |

All fourteen HEN builds carry the same control flags and all fourteen custom
firmware builds are zero, as is every stock retail file, so the value is part
of what makes a build a HEN build. What the two bytes mean is not known here;
they are reproduced because the survey says they belong, which is a different
thing from understanding them. A key revision the survey does not cover keeps
the template's flags and its minimum firmware, because a field that decides
what a console will load is never invented.

The last two were each found the same way: a patch with the three fields
above it right went on to a real HEN console and black screened at the moment
the patched binary loaded, which said the list was not finished. The section
table is the one that takes real work. The paired builds of BLUS30377
`default_mp.self` are scetool's work from one eight-header ELF, and they do
not describe the same file:

|  | Custom firmware | PS3HEN |
| --- | --- | --- |
| sections | 6 | 9 |
| program headers described | 0 to 4 | 0 to 7 |
| key count | 0x2E | 0x46 |
| header length | 0x980 | 0xB80 |

That is scetool's skip-sections option. The campaign pair goes further: its
custom firmware build describes three of the eight. So on the HEN path the
table cannot be copied, and it is built from the ELF's own program headers
instead: one type 2 section each, then the type 1 section header table, with
a random AES key, counter and HMAC key for every section the template did not
have. The key count follows from the sections, eight slots for an encrypted
one and six for an unencrypted one, and the header length follows from the
key count.

`data_length` goes with it. Retail files put the length of the ELF inside
there and the byte-identical round trip depends on that, so the custom
firmware path keeps writing it. All twenty-eight paired builds put the file
size less the header length, which is scetool's convention, so that is what
the HEN path writes.

Re-signing the custom firmware build of a pair at 0x000A here and comparing
it against the HEN build shipped beside it gives the same file field for
field, on three pairs across two regions and both trees: the same size, the
same header and data lengths, the same nine sections with the same types,
indices, offsets, sizes and compression flags, the same section plaintexts,
and the same control blocks down to scetool's own `watermarktrololo` pad.

**One field still differs, and it is the signature.** The private key for
revision 0x000A is in naehrwert's keys file, which is the one revision it is
in, so scetool signed their HEN builds for real and left their custom
firmware builds at zero. Signing is not implemented here, so a file built
from a custom firmware template carries that template's zeros. Whether a HEN
console checks the signature is not known here, and it is the obvious thing
to look at if a build that matches in every other field is still refused.

## Two findings worth writing down

**Compression is zlib at level 6.** Every compressed section in the corpus is
reproduced byte for byte at level 6 and at no other level. That is what makes a
byte-identical rebuild possible at all. Modern Warfare 2 and 3 carry no
compressed sections, so "compression on" means following the original section by
section rather than setting a flag.

**The decrypted ELF is not quite the file Sony signed.** The type 3 section's
bytes are stored only in the SELF, so the corresponding region of the ELF
decrypts to a hole. This is why the file digest of a stock retail SELF is not
the SHA1 of the ELF that comes out of it, and it is why a rebuild of an
unchanged file carries that digest rather than recomputing it.
