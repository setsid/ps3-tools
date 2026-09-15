"""Pulling just enough of an ISO off the console to know what it is.

The console this runs against is in use. It may be part way through a transfer
somebody started an hour ago, and it is serving FTP from a games console, not
from a server. So the rules here are about restraint rather than parsing:

  - never a whole file. Every read is REST plus RETR for one aligned block,
    and the transfer is aborted as soon as the block is in hand. A 40 GB image
    is identified from a few tens of kilobytes.
  - a hard budget per file and across the run, counted in bytes actually
    fetched. When the budget is gone the walk stops and the entry falls back to
    its filename, because an answer of "probably, from the name" is worth more
    than a console that has been hammered for an hour.
  - the control connection is held open no longer than one file needs, and an
    aborted transfer either leaves it usable or is torn down. A wedged control
    connection is worse than a slow one: the collector behind us still has
    listings to do.

The identity itself is worked out by isoid, which never touches a socket. This
module only decides which bytes to fetch and what it costs.

One thing this module does not do is reach round transport's download
allowlist. transport.may_download() is about whole-file downloads and rightly
refuses ISOs. A ranged read is a different thing and needs its own permission;
until transport grows one, the gate below is it, and it is written so that a
may_range_read() added to transport takes over automatically.
"""

import ftplib
import re
import socket

from . import isoid, regioncodes, transport

SCHEMA_VERSION = 1

# Aligned blocks, so the three or four sectors a walk asks for land inside one
# or two fetches rather than one fetch each.
DEFAULT_BLOCK = 32 * 1024
DEFAULT_FILE_BUDGET = 256 * 1024
DEFAULT_TOTAL_BUDGET = 8 * 1024 * 1024

DEFAULT_TIMEOUT = 30.0
# How long to wait for the server to acknowledge a transfer we cut short. Short
# on purpose: past this the connection is dropped rather than waited on.
SETTLE_TIMEOUT = 5.0

METHOD_FILENAME = "filename"
METHOD_NONE = isoid.METHOD_NONE

ISO_SUFFIXES = (".iso",)

# The ranged-read gate. Only disc images, only under a device root. Kept here
# rather than in transport because transport must not be edited from this side;
# see the module docstring.
RANGED_READABLE = (
    re.compile(r"(?i)^/dev_(?:hdd\d|usb\d{1,3}|sd|ms|cf|ntfs\d*)"
               r"(?:/[^/]+)*/[^/]+\.iso$"),
)


def may_range_read(path):
    """True when path may be read a block at a time.

    Defers to transport if the integrator has added a ranged-read exemption
    there, so this copy of the policy stops being the authority the moment
    there is a better one.
    """
    gate = getattr(transport, "may_range_read", None)
    if gate is not None:
        return bool(gate(path))
    return any(pattern.match(path) for pattern in RANGED_READABLE)


class RangeReadRefused(transport.UnsafeRequest):
    """Raised before anything leaves this machine."""


# --- fetching --------------------------------------------------------------

class FtpRangeReader:
    """REST + RETR for one block, then abort.

    Takes a callable returning a live ftplib connection rather than opening one
    itself, so the caller keeps ownership of the socket: an FtpLister passes
    its own open/close, and a test passes a connection to 127.0.0.1.
    """

    def __init__(self, connect, reset=None, timeout=DEFAULT_TIMEOUT, log=None):
        self._connect = connect
        self._reset = reset
        self.timeout = timeout
        self.log = log
        self.bytes_read = 0
        self.aborted = 0
        self.dropped = 0
        self._ftp = None

    # -- connection handling

    def _open(self):
        if self._ftp is None:
            self._ftp = self._connect()
        return self._ftp

    def _drop(self):
        """Tear the control connection down. Wedged is worse than absent."""
        ftp = self._ftp
        self._ftp = None
        if ftp is None:
            return
        self.dropped += 1
        if self._reset is not None:
            try:
                self._reset()
                return
            except Exception:                          # noqa: BLE001
                pass
        try:
            ftp.close()
        except Exception:                              # noqa: BLE001
            pass

    def release(self):
        """Let go of the connection between files, keeping the reader usable.

        Called after each image. The next read reconnects, which costs a login
        and buys not sitting on a socket of a console somebody else is using.
        """
        ftp, self._ftp = self._ftp, None
        if ftp is None:
            return
        if self._reset is not None:
            try:
                self._reset()
                return
            except Exception:                          # noqa: BLE001
                return
        try:
            ftp.quit()
        except Exception:                              # noqa: BLE001
            try:
                ftp.close()
            except Exception:                          # noqa: BLE001
                pass

    close = release

    def _settle(self, ftp, cut_short):
        """Collect the reply to a transfer we stopped listening to.

        Servers differ: some answer 226 as though nothing happened, some send
        426 and then 226, some say nothing until they notice the dead socket.
        Anything that is not one of the first two, and anything slow, costs the
        connection rather than the run.
        """
        sock = getattr(ftp, "sock", None)
        previous = sock.gettimeout() if sock is not None else None
        try:
            if sock is not None:
                sock.settimeout(SETTLE_TIMEOUT)
            try:
                ftp.voidresp()
            except ftplib.error_temp:
                # 426, transfer aborted. The 226 normally follows it.
                try:
                    ftp.voidresp()
                except ftplib.all_errors:
                    self._drop()
        except (OSError, EOFError, ftplib.Error, socket.timeout):
            self._drop()
        else:
            if sock is not None and self._ftp is not None:
                try:
                    sock.settimeout(previous)
                except OSError:
                    self._drop()
        if cut_short:
            self.aborted += 1

    # -- reads

    def size(self, path):
        """SIZE, or None. Cheap, and it saves reading past the end."""
        try:
            ftp = self._open()
            ftp.voidcmd("TYPE I")
            return ftp.size(path)
        except (ftplib.error_perm, ValueError):
            return None
        except ftplib.all_errors:
            self._drop()
            return None

    def read(self, path, offset, length):
        """Up to length bytes at offset. Short or empty means no more."""
        if not may_range_read(path):
            raise RangeReadRefused(
                f"not on the ranged-read allowlist: {path!r}")
        if length <= 0:
            return b""
        try:
            ftp = self._open()
            ftp.voidcmd("TYPE I")
            connection = ftp.transfercmd(f"RETR {path}",
                                         rest=offset or None)
        except ftplib.all_errors:
            self._drop()
            return b""
        data = bytearray()
        try:
            connection.settimeout(self.timeout)
            while len(data) < length:
                block = connection.recv(min(8192, length - len(data)))
                if not block:
                    break
                data += block
        except (OSError, socket.timeout):
            pass
        finally:
            try:
                connection.close()
            except OSError:
                pass
        # Getting everything asked for means the server still had the rest of
        # the image to send and was cut off. A short read is the file ending,
        # which is the only way a transfer here finishes on its own.
        cut_short = len(data) >= length
        if self._ftp is not None:
            self._settle(ftp, cut_short)
        self.bytes_read += len(data)
        if self.log:
            self.log.event("ftp_range", path=path, offset=offset,
                           wanted=length, bytes=len(data), aborted=cut_short)
        return bytes(data)


def reader_for(lister, **kwargs):
    """An FtpRangeReader driving an FtpLister's own connection.

    The lister keeps ownership: it opened the socket, it closes it, and its
    read-only surface is untouched. This only borrows the connection for the
    ranged reads that FtpLister has no method for.
    """
    return FtpRangeReader(connect=lister.open, reset=lister.close, **kwargs)


class BytesReader:
    """A reader over buffers already in memory. For tests and saved dumps."""

    def __init__(self, files):
        self.files = dict(files)
        self.bytes_read = 0

    def size(self, path):
        data = self.files.get(path)
        return len(data) if data is not None else None

    def read(self, path, offset, length):
        data = self.files.get(path)
        if data is None:
            return b""
        chunk = data[offset:offset + length]
        self.bytes_read += len(chunk)
        return chunk

    def release(self):
        pass


# --- budgeting -------------------------------------------------------------

class _BudgetedRanges:
    """The read_range isoid is handed: aligned, cached and capped.

    Caching matters more than it looks. The walk reads the volume descriptors,
    then the root directory, then a subdirectory, then the file, and on a real
    disc several of those are in the same 32 KB block. Without the cache that
    is four round trips to a console; with it, often two.
    """

    def __init__(self, reader, path, block, budget, spend, size=None):
        self.reader = reader
        self.path = path
        self.block = block
        self.budget = budget
        self.spend = spend
        self.size = size
        self.bytes_read = 0
        self.exhausted = False
        self.refused = None
        self._blocks = {}

    def _block(self, index):
        if index in self._blocks:
            return self._blocks[index]
        offset = index * self.block
        if self.size is not None and offset >= self.size:
            self._blocks[index] = b""
            return b""
        if self.bytes_read + self.block > self.budget or not self.spend(
                self.block):
            self.exhausted = True
            return b""
        try:
            data = self.reader.read(self.path, offset, self.block)
        except transport.UnsafeRequest as exc:
            self.refused = str(exc)
            data = b""
        except Exception as exc:                       # noqa: BLE001
            self.refused = str(exc) or exc.__class__.__name__
            data = b""
        self._blocks[index] = data
        self.bytes_read += len(data)
        return data

    def __call__(self, offset, length):
        out = bytearray()
        position = offset
        end = offset + length
        while position < end:
            index = position // self.block
            block = self._block(index)
            start = position - index * self.block
            chunk = block[start:start + (end - position)]
            if not chunk:
                break
            out += chunk
            position += len(chunk)
        return bytes(out)


# --- the artefact ----------------------------------------------------------

def _is_iso(entry):
    if entry.get("kind") not in (None, "file"):
        return False
    name = entry.get("name") or ""
    return name.lower().endswith(ISO_SUFFIXES)


def _path_for(entry):
    device = (entry.get("device") or "").strip("/")
    folder = (entry.get("folder") or "").strip("/")
    name = entry.get("name") or ""
    parts = [part for part in (device, folder, name) if part]
    return "/" + "/".join(parts)


def _row(entry, path, identity, bytes_read, reason, opened=True):
    """One isos[] record, the shape docs/artefact-schema.md reserves."""
    name = entry.get("name") or ""
    from_name = regioncodes.find_title_id(name)
    title_id = identity.title_id
    method = identity.method
    if title_id is None and from_name:
        # The name is a claim rather than evidence, and it is said to be one.
        method = METHOD_FILENAME
        title_id = from_name
    elif title_id is None:
        method = METHOD_NONE
    mismatch = bool(from_name and identity.title_id
                    and from_name.upper() != identity.title_id.upper())
    return {
        "name": name,
        #: True when this image was actually read. False means nothing was
        #: learned about it: a refused read, a spent budget, a worker that
        #: died. A caller must not treat a False here as "looked at and found
        #: nothing", or the image disappears from its list having never been
        #: opened.
        "opened": bool(opened),
        "device": entry.get("device"),
        "folder": entry.get("folder"),
        "path": path,
        "size": entry.get("size"),
        "method": method,
        "title_id": title_id,
        "title": identity.title,
        "app_version": identity.app_version,
        "category": identity.category,
        "region": regioncodes.describe_title_id(title_id)["region"],
        "bytes_read": bytes_read,
        "name_mismatch": mismatch,
        "reason": reason,
    }


def identify_isos(entries, reader, block=DEFAULT_BLOCK,
                  file_budget=DEFAULT_FILE_BUDGET,
                  total_budget=DEFAULT_TOTAL_BUDGET, log=None,
                  on_progress=None):
    """The games/iso-identity.json payload for every ISO in entries.

    entries are game rows as ArtefactSet.game_entries() produces them: name,
    kind, size, device and folder. Anything that is not a file ending .iso is
    skipped, because a folder game's PARAM.SFO is a plain small file and needs
    none of this.

    Never raises. A console that stops answering half way through leaves the
    images already done intact and the rest identified by name.

    on_progress(done, total, name) is called before each image is opened. This
    runs for minutes on a shelf of games and, without it, the screen that asked
    for it sits there looking frozen.
    """
    isos = []
    spent = [0]
    todo = [entry for entry in (entries or []) if _is_iso(entry)]
    total = len(todo)
    done = 0

    def spend(count):
        if spent[0] + count > total_budget:
            return False
        spent[0] += count
        return True

    for entry in todo:
        if on_progress:
            on_progress(done, total, entry.get("name") or "")
        done += 1
        path = _path_for(entry)
        if not may_range_read(path):
            identity = isoid.IsoIdentity(
                reason="not on the ranged-read allowlist, so the image was "
                       "not opened")
            isos.append(_row(entry, path, identity, 0, identity.reason,
                             opened=False))
            continue
        size = entry.get("size") or None
        ranges = _BudgetedRanges(reader, path, block, file_budget, spend,
                                 size=size)
        identity = isoid.identify(ranges)
        reason = identity.reason
        # Refused and exhausted both mean this image has not had its chance.
        # Only a read that ran to a conclusion counts as having been opened.
        opened = True
        if not identity.identified:
            if ranges.refused:
                reason = f"the image could not be read: {ranges.refused}"
                opened = False
            elif ranges.exhausted:
                reason = (f"stopped after {ranges.bytes_read} bytes without "
                          f"finding the identity")
                opened = False
        isos.append(_row(entry, path, identity, ranges.bytes_read, reason,
                         opened=opened))
        if log:
            log.event("iso_identity", path=path, method=isos[-1]["method"],
                      title_id=isos[-1]["title_id"],
                      bytes=ranges.bytes_read,
                      mismatch=isos[-1]["name_mismatch"])
        # Nothing else is wanted from this connection for now, and the console
        # may have somebody else's transfer to get on with.
        release = getattr(reader, "release", None)
        if callable(release):
            release()
    return {"schema_version": SCHEMA_VERSION, "isos": isos}


def mismatches(payload):
    """The rows worth putting in front of a helper: the name is wrong."""
    return [row for row in (payload or {}).get("isos", [])
            if row.get("name_mismatch")]
