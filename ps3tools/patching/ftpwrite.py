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

from ps3diag.parsers import recode_ftp_line
from ps3diag.transport import force_byte_safe

DEFAULT_TIMEOUT = 60.0

# Large enough that a 7 MB SELF is not ten thousand callbacks, small enough
# that a progress bar still moves on a console that is being slow.
BLOCK = 32768


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
            return run(self.open())
        except ftplib.error_perm:
            # A refusal is an answer: the path is not there, or the server will
            # not do it. Asking again gets the same reply.
            raise
        except ftplib.all_errors as exc:
            if self.log:
                self.log.event("ftp_write_reconnect",
                               reason=exc.__class__.__name__)
            self.close()
            return run(self.open())

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
