"""Running the Black Ops 1 code cave rather than reading it.

The cave is the half of the fix that nothing else tests. Everything in
test_bo1.py asks whether the right bytes were written in the right place,
which is a question about the patcher; whether those bytes, once the console
runs them, actually reach getUserID with the account ID is a question about
the code, and reading a listing is not an answer to it. A real console
reported the fix applying cleanly and the rank still reading 1, which is
exactly what a cave that stops after its first failed open would look like
from the outside.

So this file executes the cave. There is a small big endian PowerPC
interpreter below that understands the two dozen instructions the cave is
built from and nothing else, a fake operating system that answers
cellFsOpen, cellFsRead, cellFsClose, va() and getUserID, and a fake console
with whatever files a test wants on it. The tests then assert on what the
cave asked for and what it handed to getUserID, which is the behaviour the
player sees.

Everything here goes through the patcher's public API, because the shape of
the cave is the patcher's business and not this file's. Nothing below knows
whether the two paths are tried by a loop or by two copies of the same code.
"""

import os
import struct
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

from ps3diag import patchstate
from test_bo1 import BASE, CAVE, IMAGE, build_image

bo1 = patchstate.patcher_module("bo1")


# --- the machine -----------------------------------------------------------

MASK = (1 << 64) - 1

#: Room for the image at its virtual addresses and a stack well clear of it.
MEMORY_SIZE = 0x40000

#: Where the cave's frame is opened. Far enough from the image that a store
#: through a bad pointer lands somewhere a test would notice.
STACK_TOP = 0x30000

#: The original argument: the online ID string the game passes in r3.
ONLINE_ID_AT = 0x20000

#: Where the fake va() leaves what it formatted.
SCRATCH_AT = 0x20100

#: The return address handed to the cave. Reaching it is how a run ends, and
#: it is deliberately somewhere that is not mapped, so a run that carries on
#: past it faults rather than wandering.
SENTINEL = 0x00F00000

#: What the fake getUserID hands back. Nothing checks it; it is there so that
#: a run which ignores the return value can be told from one that does not.
XUID = 0x1122334455667788


class Unsupported(Exception):
    """An instruction this interpreter does not know.

    Raised rather than passed over. An instruction quietly skipped would turn
    a test of the cave's control flow into a test of nothing at all, and the
    cave is expected to be built from a small fixed set of instructions.
    """


def _signed(value, bits):
    top = 1 << (bits - 1)
    return value - (1 << bits) if value & top else value


def _signed64(value):
    return _signed(value & MASK, 64)


def _signed32(value):
    return _signed(value & 0xFFFFFFFF, 32)


class Machine:
    """Thirty two registers, a link register, CR0 and some memory.

    Big endian throughout, because the image is and the account ID is. Every
    register holds an unsigned 64 bit value and the signedness is applied
    where an instruction says it is, which is the whole of why the account ID
    survives the trip from the file to the format string.
    """

    def __init__(self):
        self.memory = bytearray(MEMORY_SIZE)
        self.gpr = [0] * 32
        self.lr = 0
        self.cr = [[False] * 4 for _ in range(8)]
        self.pc = 0

    # -- memory
    def write(self, address, raw):
        self.memory[address:address + len(raw)] = raw

    def read(self, address, length):
        return bytes(self.memory[address:address + length])

    def string(self, address):
        end = self.memory.index(0, address)
        return self.memory[address:end].decode("ascii")

    def map_image(self, image, data):
        """The executable segment, at the addresses it is loaded at."""
        position = image.locate(image.base, image.size)
        if position is None:
            raise AssertionError("the executable segment is not in the file")
        self.write(image.base, data[position:position + image.size])

    # -- registers
    def put(self, register, value):
        self.gpr[register] = value & MASK

    def base_of(self, register):
        return 0 if register == 0 else self.gpr[register]

    def compare(self, field, left, right):
        self.cr[field] = [left < right, left > right, left == right, False]

    # -- one instruction
    def step(self):
        if self.pc + 4 > len(self.memory):
            raise Unsupported("the machine ran off the end of memory at "
                              "%08X" % self.pc)
        word = struct.unpack_from(">I", self.memory, self.pc)[0]
        op = word >> 26
        rt = (word >> 21) & 31
        ra = (word >> 16) & 31
        rb = (word >> 11) & 31
        simm = _signed(word & 0xFFFF, 16)
        uimm = word & 0xFFFF
        ds = _signed(word & 0xFFFC, 16)
        self.pc += 4

        if op == 14:                                    # addi, li
            self.put(rt, self.base_of(ra) + simm)
        elif op == 15:                                  # addis, lis
            self.put(rt, self.base_of(ra) + (simm << 16))
        elif op == 24:                                  # ori
            self.put(ra, self.gpr[rt] | uimm)
        elif op == 32:                                  # lwz
            at = (self.base_of(ra) + simm) & MASK
            self.put(rt, struct.unpack_from(">I", self.memory, at)[0])
        elif op == 58 and (word & 3) == 0:              # ld
            at = (self.base_of(ra) + ds) & MASK
            self.put(rt, struct.unpack_from(">Q", self.memory, at)[0])
        elif op == 62 and (word & 3) in (0, 1):         # std, stdu
            at = (self.base_of(ra) + ds) & MASK
            struct.pack_into(">Q", self.memory, at, self.gpr[rt])
            if word & 1:
                self.put(ra, at)
        elif op == 11:                                  # cmpwi, cmpdi
            left = (_signed64(self.gpr[ra]) if (word >> 21) & 1
                    else _signed32(self.gpr[ra]))
            self.compare((word >> 23) & 7, left, simm)
        elif op == 31:
            self._extended(word, rt, ra, rb)
        elif op == 16:                                  # bc
            self._conditional(word)
        elif op == 18:                                  # b, bl
            target = self.pc - 4 + _signed(word & 0x03FFFFFC, 26)
            if word & 1:
                self.lr = self.pc
            self.pc = target & 0xFFFFFFFF
        elif op == 19 and (word >> 1) & 0x3FF == 16:    # blr
            self.pc = self.lr
        else:
            raise Unsupported("opcode %d at %08X (%08X) is not one this "
                              "interpreter knows" % (op, self.pc - 4, word))

    def _extended(self, word, rt, ra, rb):
        extended = (word >> 1) & 0x3FF
        if extended == 339:                             # mflr
            self.put(rt, self.lr)
        elif extended == 467:                           # mtlr
            self.lr = self.gpr[rt]
        elif extended == 444:                           # or, and so mr
            self.put(ra, self.gpr[rt] | self.gpr[rb])
        elif extended == 266:                           # add
            self.put(rt, self.gpr[ra] + self.gpr[rb])
        elif extended == 986:                           # extsw
            self.put(ra, _signed32(self.gpr[rt]))
        elif extended == 0:                             # cmpd, cmpw
            if (word >> 21) & 1:
                left, right = (_signed64(self.gpr[ra]),
                               _signed64(self.gpr[rb]))
            else:
                left, right = (_signed32(self.gpr[ra]),
                               _signed32(self.gpr[rb]))
            self.compare((word >> 23) & 7, left, right)
        else:
            raise Unsupported("extended opcode %d at %08X is not one this "
                              "interpreter knows" % (extended, self.pc - 4))

    def _conditional(self, word):
        bo = (word >> 21) & 31
        bi = (word >> 16) & 31
        bit = self.cr[bi // 4][bi % 4]
        if bo == 12:
            taken = bit
        elif bo == 4:
            taken = not bit
        else:
            raise Unsupported("BO=%d at %08X is a form of conditional branch "
                              "this interpreter does not know"
                              % (bo, self.pc - 4))
        if taken:
            self.pc = (self.pc - 4 + _signed(word & 0xFFFC, 16)) & 0xFFFFFFFF


# --- the operating system the cave calls -----------------------------------

#: cellFsOpen's answer for a file that is not there, or is not readable by
#: the account the game runs as. Negative in its low 32 bits, which is what
#: the cave tests.
ENOENT = 0x8001000A

#: cellFsRead's answer when the handle is good but the read is not.
EIO = 0x80010005


class Console:
    """A console with whatever files a test says it has.

    Only the five functions the cave calls are here, and each of them is
    answered the way the real one would be: a failed open writes no file
    descriptor, a failed read writes no count, and every one of them returns
    to its caller.
    """

    def __init__(self, files=None, read_errors=()):
        self.files = dict(files or {})
        self.read_errors = set(read_errors)
        #: Every path cellFsOpen was asked for, in the order it was asked.
        self.opened = []
        #: Every string getUserID was given, in order. The one that matters.
        self.user_ids = []
        #: Every string va() was asked to format, as (format, value).
        self.formatted = []
        self.closed = 0
        self.handles = {}
        self.next_fd = 11

    # -- the calls
    def fs_open(self, machine):
        path = machine.string(machine.gpr[3])
        self.opened.append(path)
        if path not in self.files:
            machine.put(3, ENOENT)
            return
        fd = self.next_fd
        self.next_fd += 1
        self.handles[fd] = path
        struct.pack_into(">I", machine.memory, machine.gpr[5], fd)
        machine.put(3, 0)

    def fs_read(self, machine):
        path = self.handles[machine.gpr[3] & 0xFFFFFFFF]
        if path in self.read_errors:
            machine.put(3, EIO)
            return
        wanted = machine.gpr[5]
        raw = self.files[path][:wanted]
        machine.write(machine.gpr[4], raw)
        struct.pack_into(">Q", machine.memory, machine.gpr[6], len(raw))
        machine.put(3, 0)

    def fs_close(self, machine):
        self.closed += 1
        machine.put(3, 0)

    def va(self, machine):
        fmt = machine.string(machine.gpr[3])
        assert fmt == "%llu", "the cave asked va() for %r" % fmt
        self.formatted.append((fmt, machine.gpr[4]))
        text = ("%d" % machine.gpr[4]).encode("ascii") + b"\x00"
        machine.write(SCRATCH_AT, text)
        machine.put(3, SCRATCH_AT)

    def get_user_id(self, machine):
        self.user_ids.append(machine.string(machine.gpr[3]))
        machine.put(3, XUID)


# --- one run ---------------------------------------------------------------

ACCOUNT_ID = 3034630675101139701
ACCOUNT_TEXT = "3034630675101139701"
ONLINE_ID = "shtum_pill34"

#: Values left in the non volatile registers so that the epilogue putting
#: them back can be told from it leaving whatever the cave happened to use.
MARKERS = {29: 0xAAAA0000AAAA0029,
           30: 0xBBBB0000BBBB0030,
           31: 0xCCCC0000CCCC0031}

#: A run longer than this is a cave that has started going round in circles.
STEP_LIMIT = 4000


class Result:
    """What one execution of the cave did."""

    def __init__(self, console, machine, what, data, steps):
        self.console = console
        self.machine = machine
        self.what = what
        self.data = data
        self.steps = steps

    @property
    def paths(self):
        return self.what["paths"]

    @property
    def finished(self):
        return self.machine.pc == SENTINEL


def content_id_for(title_id):
    """A content ID carrying a given title, the way a real SELF spells one."""
    return f"EP0002-{title_id}_00-CODBLOPSPATCH012"


def run_cave(files=None, read_errors=(), title_id="BLES01031",
             online_id=ONLINE_ID, image=None):
    """Patch an image, then run the cave it wrote against a fake console.

    files is keyed by the paths the patcher chose, which a test reads back
    out of what["paths"] rather than spelling out, so that changing where the
    fix looks changes the tests' fixtures with it.
    """
    data, what = bo1.apply(IMAGE if image is None else image,
                           content_id_for(title_id))
    machine = Machine()
    machine.map_image(bo1.Image(data), data)
    machine.write(ONLINE_ID_AT, online_id.encode("ascii") + b"\x00")

    console = Console(files, read_errors)
    marks = what["landmarks"]
    intercepts = {marks["cellFsOpen"]: console.fs_open,
                  marks["cellFsRead"]: console.fs_read,
                  marks["cellFsClose"]: console.fs_close,
                  marks["va"]: console.va,
                  marks["getUserID"]: console.get_user_id}

    machine.gpr[1] = STACK_TOP
    machine.gpr[3] = ONLINE_ID_AT
    for register, value in MARKERS.items():
        machine.gpr[register] = value
    machine.lr = SENTINEL
    machine.pc = what["entry"]

    steps = 0
    while machine.pc != SENTINEL:
        steps += 1
        if steps > STEP_LIMIT:
            raise AssertionError(
                "the cave was still running after %d instructions, having "
                "opened %r" % (STEP_LIMIT, console.opened))
        handler = intercepts.get(machine.pc)
        if handler is not None:
            handler(machine)
            machine.pc = machine.lr
            continue
        machine.step()
    return Result(console, machine, what, data, steps)


def account_file(value=ACCOUNT_ID, extra=b"shtum_pill34\x00\x00\x00\x00"):
    """np_cache.dat as the console writes it: the account ID, then the rest."""
    return struct.pack(">Q", value) + extra


def the_path(title_id="BLES01031"):
    """The one path the cave asks for, taken from the patcher itself."""
    _out, what = bo1.apply(IMAGE, content_id_for(title_id))
    return what["paths"][0]


#: Where the real file would be, which the cave must never ask for. Spelled
#: out here rather than taken from the patcher, because the point of these
#: tests is that the patcher no longer knows this path at all.
REAL_FILE = "/dev_hdd0/home/00000001/np_cache.dat"


# --- what the cave does ----------------------------------------------------

class TheCaveReachesTheAccountId(unittest.TestCase):
    """The thing the whole fix exists to do, executed rather than read."""

    def test_the_copy_being_readable_reaches_the_account(self):
        """The case every real console is in.

        np_cache.dat under /dev_hdd0/home is mode rw------- and belongs to the
        signed in user, so the game cannot open it. The copy the tool places
        in the game's own folder is the one that is read. A console reported
        the fix applying and the rank still reading 1, which is what a cave
        that never reaches the account ID looks like from the player's side,
        so this is the assertion that matters most here.
        """
        result = run_cave(files={the_path(): account_file()})
        self.assertEqual(result.console.user_ids, [ACCOUNT_TEXT])

    def test_the_account_id_is_read_big_endian_out_of_the_first_eight(self):
        # The same eight bytes npcache.account_id reads on the tool's side,
        # read the same way by the code that runs on the console.
        result = run_cave(files={the_path(): account_file()})
        self.assertEqual(result.console.formatted, [("%llu", ACCOUNT_ID)])

    def test_the_copy_is_the_only_file_it_asks_for(self):
        """One path attempted, and it is the copy.

        An earlier version asked for the signed in user's own np_cache.dat
        first and fell back to the copy, on the reasoning that the game cannot
        open the real one anyway so a failed open costs nothing. It was the
        one thing this tool did that the build confirmed on hardware did not.
        """
        result = run_cave(files={the_path(): account_file()})
        self.assertEqual(result.console.opened, result.paths)
        self.assertEqual(len(result.console.opened), 1)
        self.assertIn("/USRDIR/", result.console.opened[0])

    def test_the_real_file_is_never_asked_for_even_when_it_is_there(self):
        # Put it on the fake console with a different account in it. If the
        # cave ever reached for it, this is where that would show.
        result = run_cave(files={REAL_FILE: account_file(42),
                                 the_path(): account_file()})
        self.assertNotIn(REAL_FILE, result.console.opened)
        self.assertEqual(result.console.user_ids, [ACCOUNT_TEXT])

    def test_the_path_follows_the_title_into_the_run(self):
        # Nothing is compiled in, so a console holding another release of the
        # game is asked for that release's own folder.
        result = run_cave(title_id="NPEB00756", files={})
        self.assertEqual(result.console.opened,
                         ["/dev_hdd0/game/NPEB00756/USRDIR/np_cache.dat"])


class EveryFailureEndsUpWhereItStarted(unittest.TestCase):
    """An account the fix cannot help is left exactly as the game had it.

    The fallback is not a nicety. Accounts made before Sony's 2018 change
    already hash correctly from the online ID, and so does every account on a
    console where np_cache.dat cannot be read for whatever reason. Handing
    getUserID anything other than the pointer it was called with would break
    those.
    """

    def test_the_path_not_opening_falls_back_to_the_original_pointer(self):
        result = run_cave(files={})
        self.assertEqual(result.console.user_ids, [ONLINE_ID])
        self.assertEqual(result.console.opened, result.paths)

    def test_the_fallback_is_the_pointer_and_not_a_copy_of_the_string(self):
        # r3 on the way in is what goes to getUserID on the way out. Checked
        # through a different string because a cave that rebuilt the text
        # somewhere else would pass on the default alone.
        result = run_cave(files={}, online_id="somebody_else")
        self.assertEqual(result.console.user_ids, ["somebody_else"])

    def test_a_read_that_fails_falls_back_rather_than_using_rubbish(self):
        # The buffer is on the stack and a failed read leaves it as it was,
        # so a cave that did not check would hash whatever was in the frame.
        path = the_path()
        result = run_cave(files={path: account_file()}, read_errors=[path])
        self.assertEqual(result.console.opened, [path])
        self.assertEqual(result.console.user_ids, [ONLINE_ID])

    def test_a_short_read_falls_back_rather_than_using_what_arrived(self):
        # A truncated np_cache.dat, which is what a file copied while the
        # console was signing in looks like. Four bytes of account ID is not
        # an account ID, and npcache refuses the same thing on the tool side.
        result = run_cave(files={the_path(): b"\x2A\x1F\x88\x99"})
        self.assertEqual(result.console.user_ids, [ONLINE_ID])

    def test_getUserID_is_called_exactly_once_however_the_run_goes(self):
        """Once, always. Twice would hash the same player under two names.

        The call the cave replaced was a single call, and whatever the cave
        does in between, the game gets one answer back from one call.
        """
        path = the_path()
        runs = [run_cave(files={}),
                run_cave(files={path: account_file()}),
                run_cave(files={path: b"\x01\x02"}),
                run_cave(files={path: account_file()}, read_errors=[path])]
        for result in runs:
            self.assertEqual(len(result.console.user_ids), 1,
                             result.console.opened)

    def test_every_file_it_opened_is_closed_again(self):
        # A handle leaked per call is a handle leaked per stats lookup, and
        # the game makes a great many of those.
        path = the_path()
        result = run_cave(files={path: account_file()}, read_errors=[path])
        self.assertEqual(result.console.closed, 1)

    def test_a_file_that_never_opened_is_not_closed(self):
        result = run_cave(files={})
        self.assertEqual(result.console.closed, 0)


class ItGivesTheGameBackWhatItBorrowed(unittest.TestCase):
    """The cave is called from the middle of a function and has to behave.

    The hook replaced one `bl`, so as far as the game is concerned the cave is
    getUserID. It gets a frame, it uses non volatile registers and it has to
    hand all of them back, or the caller carries on with a stack pointer and
    registers that are not its own and the fault turns up somewhere else
    entirely.
    """

    def setUp(self):
        _out, self.what = bo1.apply(IMAGE, content_id_for("BLES01031"))
        self.path = self.what["paths"][0]

    def test_it_returns_to_its_caller_rather_than_running_on(self):
        result = run_cave(files={self.path: account_file()})
        self.assertTrue(result.finished)

    def test_the_frame_is_unwound_on_every_way_out(self):
        for files, errors in (({}, ()),
                              ({self.path: account_file()}, ()),
                              ({self.path: b"\x01\x02"}, ()),
                              ({self.path: account_file()}, [self.path])):
            result = run_cave(files=files, read_errors=errors)
            self.assertEqual(result.machine.gpr[1], STACK_TOP,
                             result.console.opened)

    def test_the_non_volatile_registers_come_back_as_they_went_in(self):
        for files in ({}, {self.path: account_file()},
                      {self.path: b"\x01\x02"}):
            result = run_cave(files=files)
            for register, value in MARKERS.items():
                self.assertEqual(result.machine.gpr[register], value,
                                 "r%d after %r" % (register, files))

    def test_the_link_register_is_the_one_it_was_given(self):
        result = run_cave(files={self.path: account_file()})
        self.assertEqual(result.machine.lr, SENTINEL)

    def test_the_cave_is_where_the_patcher_said_it_put_it(self):
        # The entry is past the head, and the head is what marks the cave as
        # this fix's work, so executing from anywhere else would be executing
        # the mark.
        self.assertEqual(self.what["cave"], BASE + CAVE)
        self.assertEqual(self.what["entry"], BASE + CAVE + bo1.HEAD)

    def test_it_runs_the_same_in_an_image_laid_out_differently(self):
        # The addresses the cave calls are found in the image rather than
        # written down, so a cave built for another layout has to run too.
        other = build_image(cave_size=0x3000)
        _out, what = bo1.apply(other, content_id_for("BLUS30591"))
        result = run_cave(files={what["paths"][0]: account_file()},
                          title_id="BLUS30591", image=other)
        self.assertEqual(result.console.user_ids, [ACCOUNT_TEXT])
        self.assertTrue(result.finished)


if __name__ == "__main__":
    unittest.main()
