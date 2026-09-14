"""The only code in this program that writes to a console.

Everything that can change a file on a PS3 lives under this package and nowhere
else. ps3diag is read-only and enforced as such by its own transport; the
diagnostic screen's promise to the user rests on that being checkable rather
than merely believed, so the writing commands are kept here, behind a different
package name, and tests/test_layering.py asserts the diagnostic side cannot
reach them by any route.

The parts:

    ftpwrite  RETR, STOR, SIZE, LIST, MKD, DELE against webMANftpd
    scetool   decrypting and re-signing, wrapped so it can be replaced
    backup    pull the originals to the Desktop and prove the copy is good
    flow      scan and patch, driven by the table in ps3tools.titles

Nothing here decides where a patch site is. That stays in the two repositories
that own the two fixes, and is imported from them.
"""
