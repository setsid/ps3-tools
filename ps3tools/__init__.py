"""PS3 Tools by setsid.

One application holding the diagnostic collector and the two PSN patchers that
used to ship as bo2-psn-fix.exe and mw3-psn-fix.exe.

Layering, which is the thing to keep straight:

    ps3diag/   domain. Collectors, parsers, rules, analysis. Read-only, and
               enforced as such by ps3diag/transport.py. Knows nothing about
               Qt and nothing about patching.
    ps3tools/  this application. The Qt shell, the screens, and the patcher,
               which is the only part of the program that writes to a console.

ps3diag must never import ps3tools.patching. There is a test asserting it, and
the reason is that the diagnostic path's promise to the user is that no code
path in it can write. A stray import is how a promise like that stops being
true without anybody noticing.
"""

from ps3diag import VERSION  # noqa: F401  the one place it is defined

APP_NAME = "PS3 Tools"
VENDOR = "setsid"
FULL_NAME = f"{APP_NAME} by {VENDOR}"
PROJECT_URL = "https://github.com/setsid"
