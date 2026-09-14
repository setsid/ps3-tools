#!/usr/bin/env python3
"""PS3 Tools by setsid.

The diagnostic collector and the PSN patchers in one window. Start here; the
shell finds the tools by enumerating what has registered itself, so this file
never needs editing when another one is added.

Some of what this program does writes to a console and some of it cannot. Which
is which is a property of the screen you are on, and each one says so; the
layering that makes the read-only half genuinely read-only is described in
docs/screen-interface.md.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ps3tools import crashreport

# Before the shell is even imported. A fault while the screens are being loaded
# is exactly the sort that leaves a user with a window that never appeared and
# nothing to send, and the handler needs nothing from the application to work.
crashreport.install()

from ps3tools.shell.app import main

if __name__ == "__main__":
    crashreport.note("Application started")
    sys.exit(main())
