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

`fake_sign` builds a fake-signed SELF from a retail one, which is the form
PS3HEN loads. A retail re-sign carries a signature that cannot be regenerated
for a file that has been changed; custom firmware has that check patched out,
and HEN appears to keep it. A fake-signed SELF turns out to be its header
followed by the ELF verbatim, and the header offsets are computed from the
program header count rather than written down, so a binary with a different
count does not get overlapping tables.

Every fake-signed file in the corpus is zero across the whole NPDRM block,
which is why those files answer 8001000F. `fake_sign` carries the retail block
through whole and refuses a template whose block has no magic.

All three take a path or bytes. All three raise `keysmith.SceError` on failure,
and the message names the file, the field, what was expected and what was
found. Nothing prints a warning and carries on.

`keysmith.sign` is the public function, so it takes the name of the module it
is implemented in. The module is reached through `sys.modules` where anything
needs it, which only the tests do.

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

- key revision, authentication ID, vendor ID, SELF type, application version
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
