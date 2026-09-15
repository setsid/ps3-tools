"""FTP that can write, kept deliberately apart from the read-only transport.

ps3diag.transport exposes exactly two methods and neither of them changes
anything, which is what lets the diagnostic screen promise that no code path in
it can touch a console. That promise stops being worth anything the moment a
STOR appears anywhere it can reach, so the writing commands live here instead,
in a package the domain layer is forbidden to import.

webMANftpd wants anonymous and nothing else. The greeting from the console this
was written against is

    220 webMANftpd 1.47.48q MOD [NTFS:0]

and USER anonymous with any password at all answers 230. The previous
generation of these tools tried three credential pairs in turn, which was worse
than useless, because a server that refuses a login usually drops the control
connection with it and the second and third attempts then failed against a dead
socket. One attempt, and whatever the server said comes back to the caller.
"""

import ftplib
import hashlib
import os
import time

from ps3diag.parsers import recode_ftp_line
from ps3diag.transport import force_byte_safe

#: How long before a pause is pointless. A connection that last worked more
#: than this ago was dropped by the server's idle timeout, and the console is
#: not busy: reconnecting immediately is right. Below it, the drop is the
#: console having had too many connections too quickly, and a moment's wait is
#: what lets the next one succeed.
IDLE_DROP_SECONDS = 20.0

#: The wait in the hurried case. The read client uses 0.5 then 1.5 across two
#: attempts; this client retries once, so it takes the longer of the two.
RECONNECT_PAUSE = 1.5

DEFAULT_TIMEOUT = 60.0

# Large enough that a 7 MB SELF is not ten thousand callbacks, small enough
# that a progress bar still moves on a console that is being slow.
BLOCK = 32768

# For disc images rather than SELFs. A 36 GB file at 32 KB a block is a million
# callbacks and a million checks of a stop flag; a megabyte still updates a
# progress bar four times a second at the speed a PS3 manages.
UPLOAD_BLOCK = 1 << 20


class TransferFailed(Exception):
    """A transfer that did not finish. The message is fit to show a user."""


def sha1_of_file(path):
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


class FtpWriter:
    """LIST, SIZE, RETR, STOR, MKD and DELE. Anonymous, one host.

    The connection is opened lazily and reused, because a console asked to open
    a fresh control connection for every one of a dozen transfers spends longer
    on the handshakes than on the files.
    """

    def __init__(self, host, timeout=DEFAULT_TIMEOUT, factory=None, log=None):
        self.host = host
        self.timeout = timeout
        self.log = log
        self.banner = ""
        self._factory = factory or self._connect
        self._ftp = None
        #: When a command last succeeded. A drop long after that one is the
        #: server's idle timeout rather than the console being overwhelmed.
        self._last_ok = None

    # -- connection

    def _connect(self):
        # See ps3diag/transport.py: strict UTF-8 on the control connection
        # makes every listing fail against a real webMANftpd.
        ftp = ftplib.FTP(encoding="latin-1")
        ftp.connect(self.host, 21, timeout=self.timeout)
        self.banner = (ftp.getwelcome() or "").strip()
        ftp.login("anonymous", "anonymous@")
        if self.log:
            self.log.event("ftp_write_login", banner=self.banner)
        return ftp

    def open(self):
        if self._ftp is None:
            # See ps3diag/transport.py: ftplib's strict UTF-8 default makes a
            # single 0xb0 in a webMANftpd status message break every listing,
            # so it is forced here whoever built the connection.
            self._ftp = force_byte_safe(self._factory())
            # Captured here as well as in _connect so the greeting survives a
            # caller that supplied a factory of its own.
            if not self.banner:
                self.banner = recode_ftp_line(
                    (self._ftp.getwelcome() or "").strip())
        return self._ftp

    def close(self):
        if self._ftp is not None:
            # all_errors is itself the tuple (Error, OSError, EOFError), and a
            # tuple nested inside an except clause is a TypeError at the moment
            # it fires rather than at import. So it is always used bare.
            try:
                self._ftp.quit()
            except ftplib.all_errors:
                pass
            try:
                self._ftp.close()
            except ftplib.all_errors:
                pass
            self._ftp = None

    def _command(self, run):
        """One command, with a single reconnect if the link has died.

        A console that has been sat idle, or that is busy spinning a disc, drops
        the control connection partway through a run of transfers. Without this
        the first file to hit that takes every later one down with it. One retry
        on a fresh connection; a second failure is real.
        """
        try:
            answer = run(self.open())
        except ftplib.error_perm:
            # A refusal is an answer: the path is not there, or the server will
            # not do it. Asking again gets the same reply.
            raise
        except ftplib.all_errors as exc:
            if self.log:
                self.log.event("ftp_write_reconnect",
                               reason=exc.__class__.__name__)
            self.close()
            self._pause_if_hurried()
            answer = run(self.open())
        self._last_ok = time.monotonic()
        return answer

    def _pause_if_hurried(self):
        """Wait before reconnecting, but only when haste is what broke it.

        webMANftpd hangs up when it has had several data connections in quick
        succession, and a fresh connection opened microseconds later meets it
        in the same state. That is what the pause is for.

        A connection that has been sitting unused for a long time died of the
        server's idle timeout instead, and there is nothing to wait for: the
        console is not busy, it simply let go. Pausing there added seconds to
        the start of every upload that followed a long download, which is a
        cost paid on the normal path for a case that is not happening.
        """
        if self._last_ok is None:
            return
        if time.monotonic() - self._last_ok < IDLE_DROP_SECONDS:
            time.sleep(RECONNECT_PAUSE)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # -- reading

    def list_dir(self, path):
        """Raw LIST output for one directory, as text."""

        def run(ftp):
            lines = []
            ftp.retrlines(f"LIST {path}", lines.append)
            return lines

        return "\n".join(recode_ftp_line(line) for line in self._command(run))

    def size(self, path):
        """SIZE in bytes, or None if the server will not say."""

        def run(ftp):
            ftp.voidcmd("TYPE I")
            reply = ftp.sendcmd(f"SIZE {path}")
            return int(reply[3:].strip()) if reply[:3] == "213" else None

        try:
            return self._command(run)
        except (ftplib.error_perm, ValueError):
            return None

    def retrieve(self, path, destination, on_block=None):
        """Whole file to a local path. Returns {"bytes", "sha1"}.

        The hash is taken from the stream rather than from the file afterwards,
        so that a short write to the local disk shows up as a mismatch against
        the re-read rather than being hashed twice from the same bad copy.
        """
        expected = self.size(path)

        def run(ftp):
            digest = hashlib.sha1()
            total = 0
            with open(destination, "wb") as handle:
                def take(block):
                    nonlocal total
                    handle.write(block)
                    digest.update(block)
                    total += len(block)
                    if on_block:
                        on_block(total, expected)

                ftp.retrbinary(f"RETR {path}", take, blocksize=BLOCK)
            return {"bytes": total, "sha1": digest.hexdigest()}

        result = self._command(run)
        if expected is not None and result["bytes"] != expected:
            raise TransferFailed(
                f"{path} stopped after {result['bytes']} bytes of the "
                f"{expected} the console said it had. The console may have "
                f"been switched off or the game may have been started.")
        result["expected"] = expected
        return result

    def retrieve_bytes(self, path):
        """Whole file into memory. Used to check what actually landed."""

        def run(ftp):
            chunks = []
            ftp.retrbinary(f"RETR {path}", chunks.append, blocksize=BLOCK)
            return b"".join(chunks)

        return self._command(run)

    # -- writing

    def store(self, source, path, on_block=None):
        """Upload one local file. Returns the number of bytes sent.

        A successful STOR is not proof of anything: a console that ran out of
        space, or a transfer that died, can still answer 226. Every caller here
        reads the file back afterwards, and that is the check that counts.
        """
        total_size = os.path.getsize(source)

        def run(ftp):
            sent = 0
            # Opened inside run so that a reconnect starts from the beginning
            # of the file rather than from wherever the dead attempt stopped.
            with open(source, "rb") as handle:
                def watch(block):
                    nonlocal sent
                    sent += len(block)
                    if on_block:
                        on_block(sent, total_size)

                ftp.storbinary(f"STOR {path}", handle, blocksize=BLOCK,
                               callback=watch)
            return sent

        sent = self._command(run)
        if self.log:
            self.log.event("ftp_store", path=path, bytes=sent)
        return sent

    def store_resumable(self, source, path, on_block=None,
                        should_continue=None, resume_from=None,
                        blocksize=UPLOAD_BLOCK):
        """Upload that can carry on from where a previous one stopped.

        Added for the transfer card and additive on purpose: store() above is
        another agent's path and is not touched. The two differ in what they
        are for. store() sends a seven megabyte SELF, where restarting costs a
        second and the simplest code wins. This sends a thirty-six gigabyte
        disc image, where restarting costs nine hours, and so the two things
        store() does not do both matter:

        REST before STOR. The bytes already on the console are kept and the
        transfer picks up at that offset, which turns a failure at 90% of a
        long file into seconds of work rather than an evening of it.

        A stop between blocks. storbinary() loops inside ftplib and the only
        way out of it is an exception, so the loop is written out here instead
        and the caller is asked between every block whether to keep going. A
        stop button that waits for the current file to finish is not a stop
        button on a file this size.

        Returns {"bytes", "sent", "offset", "complete"}: what should now be on
        the console, what this call put there, where it started, and whether
        the local file was sent to its end. A run that stopped early leaves a
        short file, which is exactly what the next run needs to see to resume.
        """
        total_size = os.path.getsize(source)
        offset = resume_from
        if offset is None:
            remote = self.size(path)
            offset = remote if isinstance(remote, int) and remote > 0 else 0
        # Longer than the local file means what is there is not an unfinished
        # copy of it, whatever it is. Appending to it would produce a file that
        # is neither. Start again from nothing.
        if offset < 0 or offset > total_size:
            offset = 0
        if offset == total_size and total_size:
            return {"bytes": total_size, "sent": 0, "offset": offset,
                    "complete": True}

        def run(ftp):
            sent = 0
            stopped = False
            # Opened inside run so that a reconnect starts from the recorded
            # offset rather than from wherever the dead attempt happened to be.
            with open(source, "rb") as handle:
                handle.seek(offset)
                ftp.voidcmd("TYPE I")
                # rest=None rather than rest=0 for a fresh upload: REST 0 is
                # legal and pointless, and there is no reason to find out how
                # webMANftpd feels about it on a file this size.
                connection = ftp.transfercmd(f"STOR {path}",
                                             rest=offset or None)
                try:
                    while True:
                        if should_continue is not None and \
                                not should_continue():
                            stopped = True
                            break
                        block = handle.read(blocksize)
                        if not block:
                            break
                        connection.sendall(block)
                        sent += len(block)
                        if on_block:
                            on_block(offset + sent, total_size)
                finally:
                    connection.close()
                # Closing the data connection is how a transfer ends either
                # way, so the server answers 226 to a stop as readily as to a
                # finished file. The short file left behind is the point.
                ftp.voidresp()
            return {"bytes": offset + sent, "sent": sent, "offset": offset,
                    "complete": not stopped}

        result = self._command(run)
        if self.log:
            self.log.event("ftp_store_resumable", path=path,
                           bytes=result["sent"], offset=result["offset"],
                           complete=result["complete"])
        return result

    def make_dir(self, path):
        """MKD, treating "it is already there" as success."""

        def run(ftp):
            return ftp.mkd(path)

        try:
            return self._command(run)
        except ftplib.error_perm:
            return path

    def delete(self, path):
        """DELE. False if the server refused, which usually means it is gone."""

        def run(ftp):
            ftp.delete(path)
            return True

        try:
            return self._command(run)
        except ftplib.error_perm:
            return False
