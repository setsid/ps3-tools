"""The real files this package is checked against, and where they live.

These are the user's own dumps and they are not in the repository: they are
several hundred megabytes of retail game binaries. Every test that wants one
skips when it is not there, so a checkout without them still runs green, and
the suite says plainly which files it did not have.

Klicensees are per title and come from the user. A wrong one does not fail
loudly; it produces a header that will not decrypt, which is why each is tied
to its files here rather than typed in at each call site.
"""

import os

HOME = os.path.expanduser("~")

BO1_KLIC = "AF0A8F0A8909F09234091AFADF909AF0"
BO2_MP_KLIC = "8C10AC1473DF38ADD7A4F2EE8C838DAB"
# ASCII "InfinityWardKey\0". Both Modern Warfare titles use it.
IW_KLIC = "496E66696E697479576172644B657900"
# NP_klic_free out of scetool's own keys file. The disc EBOOT.BIN of every
# title here is FREE licensed and carries no klicensee of its own, so this is
# what opens it. Handing it a title klicensee gives "Could not decrypt header",
# which says nothing about which of the two is wrong.
FREE_KLIC = "72F990788F9CFF745725F08E4C128387"


class Sample:
    def __init__(self, key, path, klicensee="", note="", kind="self"):
        self.key = key
        self.path = os.path.join(HOME, path)
        self.klicensee = klicensee
        self.note = note
        self.kind = kind

    @property
    def there(self):
        return os.path.isfile(self.path)

    @property
    def size(self):
        return os.path.getsize(self.path) if self.there else 0

    def __repr__(self):
        return f"<Sample {self.key}>"


SAMPLES = [
    # Black Ops 1, both disc regions. Both decrypt to the same ELF.
    Sample("bo1-bles01031", "bo1-BLES01031/stock/t5mp_ps3f.self", BO1_KLIC,
           "BO1 EU multiplayer, key revision 0x0010"),
    Sample("bo1-blus30591", "bo1-regions/BLUS30591_t5mp_ps3f.self", BO1_KLIC,
           "BO1 US multiplayer"),
    Sample("bo1-bles01031-sp", "bo1-BLES01031/t5_ps3f.self", BO1_KLIC,
           "BO1 EU single player"),

    # Black Ops 2, both regions.
    Sample("bo2-bles01717-eboot", "bo2-BLES01717-stock/EBOOT.BIN",
           FREE_KLIC, "BO2 EU EBOOT, key revision 0x001C"),
    Sample("bo2-bles01717-sp", "bo2-BLES01717-stock/t6_ps3f.self",
           BO2_MP_KLIC, "BO2 EU single player"),
    Sample("bo2-bles01717-mp", "bo2-BLES01717-stock/t6mp_ps3f.self",
           BO2_MP_KLIC, "BO2 EU multiplayer"),
    Sample("bo2-blus31140-eboot", "bo2/blus31140/BLUS31140 ORIGINAL/EBOOT.BIN",
           FREE_KLIC, "BO2 US EBOOT"),
    Sample("bo2-blus31140-sp", "bo2/blus31140/BLUS31140 ORIGINAL/t6_ps3f.self",
           BO2_MP_KLIC, "BO2 US single player"),
    Sample("bo2-blus31140-mp",
           "bo2/blus31140/BLUS31140 ORIGINAL/t6mp_ps3f.self",
           BO2_MP_KLIC, "BO2 US multiplayer"),

    # Modern Warfare 3, title update 1.24.
    Sample("mw3-eboot", "mw3/tu124/USRDIR/EBOOT.BIN", FREE_KLIC,
           "MW3 EBOOT, key revision 0x0019"),
    Sample("mw3-sp", "mw3/tu124/USRDIR/default.self", IW_KLIC,
           "MW3 single player"),
    Sample("mw3-mp", "mw3/tu124/USRDIR/default_mp.self", IW_KLIC,
           "MW3 multiplayer"),
    # Listed as a retail SELF, but its key revision reads DEBUG and scetool
    # finds no keyset for it: it is fake signed, like the two below.
    Sample("mw3-bles01428", "mw3-regions/BLES01428_default_mp.self", IW_KLIC,
           "MW3 BLES01428 multiplayer, fake signed", kind="fself"),

    # Modern Warfare 2.
    Sample("mw2-eboot", "mw2-BLES00683/stock/EBOOT.BIN", FREE_KLIC,
           "MW2 EBOOT"),
    Sample("mw2-sp", "mw2-BLES00683/stock/default.self", IW_KLIC,
           "MW2 single player"),
    Sample("mw2-mp", "mw2-BLES00683/stock/default_mp.self", IW_KLIC,
           "MW2 multiplayer"),

    # Fake-signed. scetool cannot read these at all.
    Sample("npeb00756-fself", "bo1-regions/NPEB00756_t5mp_ps3f.self",
           BO1_KLIC, "BO1 digital EU, fake signed", kind="fself"),
    Sample("npub30787-fself", "mw3-regions/NPUB30787_default_mp.self",
           IW_KLIC, "MW3 digital US, fake signed", kind="fself"),
]

BY_KEY = {sample.key: sample for sample in SAMPLES}

# Stock beside the file scetool produced from it, for comparing a real patch.
PAIRS = [
    ("bo1-patched", "bo1-BLES01031/t5mp_ps3f.self",
     "bo1-BLES01031/t5mp_ps3f.xuid.self", BO1_KLIC),
    ("bo2-eboot-rebuilt", "bo2/backup/EBOOT.BIN",
     "bo2/blus31140/rebuilt/EBOOT.BIN", BO2_MP_KLIC),
    ("bo2-mp-rebuilt", "bo2/backup/t6mp_ps3f.self",
     "bo2/blus31140/rebuilt/t6mp_ps3f.self", BO2_MP_KLIC),
]

# The figure the user gave to check stage 2 against, independently of scetool.
BO1_ELF_SHA1 = "acccd87f23f84d72f7e9a5d5f174e4759f12f84a"
BO1_ELF_SIZE = 11906144


def present():
    return [sample for sample in SAMPLES if sample.there]


def selfs():
    return [s for s in present() if s.kind == "self"]


def fselfs():
    return [s for s in present() if s.kind == "fself"]
