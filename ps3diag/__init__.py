"""Read-only diagnostic collection from a PS3 running webMAN MOD.

Every module here is import-safe and side effect free. Nothing in this package
opens a socket at import time, and nothing in it ever writes to the console:
see transport.py for how that is enforced rather than merely intended.
"""

APP_NAME = "ps3-diag"

#: THE VERSION OF THE WHOLE APPLICATION. This is the only place it is written
#: down. ps3tools/__init__.py imports it, and the About screen, the update
#: check, the window footer, the diagnostic manifest and the HTTP User-Agent
#: all read it from there. Change it here and rebuild; nothing else needs
#: touching.
#:
#: It lives in ps3diag rather than ps3tools because the arrow between the two
#: only points one way: ps3tools may import ps3diag, and ps3diag may never
#: import ps3tools (tests/test_layering.py enforces it). A constant the lower
#: layer needs therefore has to live in the lower layer.
#:
#: Tags are compared against this, so keep it as plain dotted numbers -- "0.9",
#: "1.0", "1.0.1". A suffix like "1.0-rc1" is deliberately unreadable to the
#: update check and would silently stop it offering anything.
VERSION = "1.3.3"

# Shown on the front page of the window and repeated at the top of summary.txt,
# because the whole reason a nervous owner runs a stranger's tool is that they
# have been told it cannot break anything. Saying it once is not enough.
READ_ONLY_NOTICE = (
    "This tool only reads. It never writes, deletes, mounts, unmounts or "
    "powers the console on or off."
)
