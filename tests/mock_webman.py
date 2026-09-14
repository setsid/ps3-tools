"""A webMAN that is not a PS3, bound to 127.0.0.1 and nothing else.

Exists so the whole tool, transport included, can be exercised without a console
and without a packet leaving the machine. Both servers bind to 127.0.0.1
explicitly rather than to 0.0.0.0, so even a misconfigured test cannot reach the
network.

The FTP side is a deliberately small implementation of the handful of commands
ps3-diag uses: USER, PASS, TYPE, PASV, LIST, REST, RETR, SIZE, QUIT. REST is
here because ranged reads are how ISO identification avoids downloading 40 GB
to learn a title ID.
"""

import os
import socket
import threading
import http.server

HOST = "127.0.0.1"
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(*parts):
    with open(os.path.join(FIXTURES, *parts), "rb") as handle:
        return handle.read()


DEFAULT_ROUTES = {
    "/": ("http", "root_147.html"),
    "/index.ps3": ("http", "root_147.html"),
    "/cpursx.ps3": ("http", "cpursx_147.html"),
    "/setup.ps3": ("http", "setup.html"),
    # Confirmed present on webMAN 1.47.48q. It is a path rather than a query
    # string, which is why it fits the transport rule without weakening it.
    # Nobody has fired it at a real console yet: the reply body here is a
    # plausible shape, not an observed one.
    "/install.ps3/dev_hdd0/packages": ("http", "install.html"),
}

DEFAULT_LISTINGS = {
    "/": ("ftp", "list_root.txt"),
    "/dev_hdd0/": ("ftp", "list_dev_hdd0.txt"),
    "/dev_hdd0/PS3ISO/": ("ftp", "list_ps3iso.txt"),
    "/dev_hdd0/PS2ISO/": ("ftp", "list_ps2iso.txt"),
    "/dev_hdd0/GAMES/": ("ftp", "list_games.txt"),
    "/dev_hdd0/crash_report/": ("ftp", "list_crash_report.txt"),
    "/dev_hdd0/plugins/": ("ftp", "list_plugins.txt"),
    "/dev_hdd0/game/": ("ftp", "list_dev_hdd0_game.txt"),
    "/dev_hdd0/game/BLES01717/USRDIR/": ("ftp", "list_usrdir_bles01717.txt"),
    "/dev_hdd0/game/BLES01428/USRDIR/": ("ftp", "list_usrdir_bles01428.txt"),
    "/dev_hdd0/game/NPEB02143/USRDIR/": ("ftp", "list_usrdir_npeb02143.txt"),
    # Empty by default. The install endpoint installs whatever is in this
    # folder rather than a named file, so a test for "somebody else's package
    # is already sitting there" overrides this with list_packages_stranger.txt.
    "/dev_hdd0/packages/": ("ftp", "list_packages_empty.txt"),
    "/dev_hdd0/home/": ("ftp", "list_home.txt"),
    "/dev_hdd0/home/00000001/savedata/": ("ftp", "list_savedata_user1.txt"),
    "/dev_hdd0/home/00000002/savedata/": ("ftp", "list_packages_empty.txt"),
    "/dev_hdd0/home/00000001/savedata/BLES01717-GAMEDATA/":
        ("ftp", "list_save_folder.txt"),
    "/dev_hdd0/home/00000001/savedata/BLES01428USRDIR/":
        ("ftp", "list_save_folder.txt"),
    "/dev_hdd0/home/00000001/savedata/NPEB02143-AUTOSAVE/":
        ("ftp", "list_save_folder.txt"),
    "/dev_hdd0/home/00000001/savedata/FREEFORM-NOTITLEID/":
        ("ftp", "list_save_folder.txt"),
    "/dev_usb000/": ("ftp", "list_dev_hdd0.txt"),
    "/dev_usb000/GAMES/": ("ftp", "list_usb_games.txt"),
}

DEFAULT_FILES = {
    "/dev_hdd0/boot_plugins.txt": ("text", "boot_plugins.txt"),
    "/dev_flash/vsh/etc/version.txt": ("text", "version.txt"),
    "/dev_hdd0/crash_report/core.20260901-184012.txt": ("text",
                                                        "crash_report.txt"),
    "/dev_hdd0/crash_report/core.20260828-210311.txt": ("text",
                                                        "crash_report.txt"),
}


def _resolve(table):
    out = {}
    for key, value in table.items():
        out[key] = fixture(*value) if isinstance(value, tuple) else value
    return out


# --- HTTP ------------------------------------------------------------------

class MockWebmanHttp:
    """GET only. Everything not in routes is a 404, which is the case that
    matters most: the endpoint list is unverified and most of it will 404."""

    def __init__(self, routes=None, delay=0.0):
        self.routes = _resolve(routes if routes is not None
                               else DEFAULT_ROUTES)
        self.delay = delay
        self.requests = []
        self._server = None
        self._thread = None

    @property
    def address(self):
        return f"{HOST}:{self._server.server_port}"

    @property
    def port(self):
        return self._server.server_port

    def start(self):
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *args):
                pass

            def _record(self):
                server.requests.append((self.command, self.path))

            def do_GET(self):
                self._record()
                if server.delay:
                    import time
                    time.sleep(server.delay)
                body = server.routes.get(self.path)
                if body is None:
                    body = fixture("http", "notfound.html")
                    self.send_response(404)
                else:
                    self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                # Recorded so a test can assert nothing ever posts.
                self._record()
                self.send_response(405)
                self.end_headers()

        self._server = http.server.HTTPServer((HOST, 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)
        self._thread.start()
        return self

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False


# --- FTP -------------------------------------------------------------------

# What a real webMANftpd 1.47.48q MOD answers with. The version and the NTFS
# mount count are both in it, which is why the client parses it.
DEFAULT_BANNER = "220 webMANftpd 1.47.48q MOD [NTFS:0]"

# webMANftpd puts a raw 0xb0 (a degree sign in latin-1) into the status reply it
# sends before a data transfer, immediately after the echoed path. ftplib has
# decoded the control connection as strict UTF-8 since Python 3.9, so this one
# byte made every LIST against a real console fail with
# "'utf-8' codec can't decode byte 0xb0". The mock emits it by default: a mock
# that is politer than the hardware is a mock that passes 490 tests against a
# client which cannot list a single real directory.
#
# The prefix length is chosen so the byte lands at the offsets the real console
# produced: 41 for /dev_hdd0/, 48 for /dev_hdd0/PS3ISO/, 54 for
# /dev_hdd0/crash_report/.
QUIRK_BYTE = "\xb0"
TRANSFER_REPLY = "150 Sending directory listing: {path}" + QUIRK_BYTE


class Disconnect(Exception):
    """Raised by a fault to drop the control connection mid-command."""


class MockWebmanFtp:
    """Enough of RFC 959 to answer ps3-tools, and read-only unless asked.

    A command this does not implement gets 502, which is what the real thing
    does for the commands webMAN's FTP server leaves out.

    writable=True turns on STOR, DELE and MKD, which only the patcher's own
    client ever uses. It is off by default so that a diagnostic test which
    somehow tried to write would fail rather than quietly succeed.

    fault(verb, argument) lets a test make the console misbehave: return a
    reply string to send instead, "DROP" to lose the connection, or
    "TRUNCATE:<n>" to accept only the first n bytes of an upload. That is how
    the patcher's backup, upload and verification failures are exercised
    without a console that can actually fail.
    """

    def __init__(self, listings=None, files=None, writable=False,
                 banner=DEFAULT_BANNER, fault=None, quirks=True):
        self.listings = _resolve(listings if listings is not None
                                 else DEFAULT_LISTINGS)
        self.files = _resolve(files if files is not None else DEFAULT_FILES)
        self.writable = writable
        self.banner = banner
        self.fault = fault
        # Off only for a test that deliberately wants a well behaved server.
        self.quirks = quirks
        self.commands = []
        self.written = {}
        self._server = None
        self._thread = None
        self._stop = threading.Event()

    @property
    def port(self):
        return self._server.getsockname()[1]

    def start(self):
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind((HOST, 0))
        self._server.listen(8)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def _serve(self):
        self._server.settimeout(0.3)
        while not self._stop.is_set():
            try:
                client, _address = self._server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._session, args=(client,),
                             daemon=True).start()

    def _normalise(self, path):
        if not path.startswith("/"):
            path = "/" + path
        return path

    def _listing_for(self, path):
        path = self._normalise(path)
        for candidate in (path, path + "/", path.rstrip("/") or "/"):
            if candidate in self.listings:
                return self.listings[candidate]
        return None

    def _session(self, client):
        client.settimeout(10)
        stream = client.makefile("rwb")
        data_socket = None
        rest = 0

        def reply(text):
            # latin-1, so a 0xb0 in a status message goes out as the single
            # byte the console sends rather than as two UTF-8 bytes.
            stream.write((text + "\r\n").encode("latin-1", errors="replace"))
            stream.flush()

        def transfer_reply(path):
            if not self.quirks:
                return "150 opening data connection"
            return TRANSFER_REPLY.format(path=path)

        reply(self.banner)
        try:
            while True:
                line = stream.readline()
                if not line:
                    break
                message = line.decode("utf-8", errors="replace").strip()
                self.commands.append(message)
                verb, _, argument = message.partition(" ")
                verb = verb.upper()
                injected = self.fault(verb, argument) if self.fault else None
                if injected == "DROP":
                    raise Disconnect(message)
                if injected and not str(injected).startswith("TRUNCATE:"):
                    data_socket = self._discard(data_socket)
                    reply(str(injected))
                    continue
                if verb == "USER":
                    reply("331 password please")
                elif verb == "PASS":
                    reply("230 logged in")
                elif verb in ("TYPE", "NOOP", "OPTS"):
                    reply("200 ok")
                elif verb == "SYST":
                    reply("215 UNIX Type: L8")
                elif verb == "PWD":
                    reply('257 "/"')
                elif verb == "CWD":
                    reply("250 ok")
                elif verb == "PASV":
                    data_socket = socket.socket(socket.AF_INET,
                                                socket.SOCK_STREAM)
                    data_socket.bind((HOST, 0))
                    data_socket.listen(1)
                    port = data_socket.getsockname()[1]
                    reply(f"227 Entering Passive Mode "
                          f"(127,0,0,1,{port >> 8},{port & 0xFF})")
                elif verb == "REST":
                    try:
                        rest = int(argument)
                        reply(f"350 restarting at {rest}")
                    except ValueError:
                        reply("501 bad offset")
                elif verb == "SIZE":
                    body = self.files.get(self._normalise(argument))
                    reply(f"213 {len(body)}" if body is not None
                          else "550 no such file")
                elif verb in ("LIST", "NLST"):
                    body = self._listing_for(argument or "/")
                    if body is None:
                        # The listener opened by PASV is dropped here rather
                        # than left dangling, because a real client reissues
                        # PASV for the next attempt and the old one would never
                        # be accepted.
                        data_socket = self._discard(data_socket)
                        reply("550 no such directory")
                        continue
                    reply(transfer_reply(argument or "/"))
                    self._send(data_socket, body)
                    data_socket = None
                    reply("226 transfer complete")
                elif verb == "RETR":
                    body = self.files.get(self._normalise(argument))
                    if body is None:
                        data_socket = self._discard(data_socket)
                        reply("550 no such file")
                        rest = 0
                        continue
                    reply(transfer_reply(self._normalise(argument)))
                    self._send(data_socket, body[rest:])
                    data_socket = None
                    rest = 0
                    reply("226 transfer complete")
                elif verb == "STOR" and self.writable:
                    limit = None
                    if injected and str(injected).startswith("TRUNCATE:"):
                        limit = int(str(injected).split(":", 1)[1])
                    reply(transfer_reply(self._normalise(argument)))
                    received = self._receive(data_socket, limit)
                    data_socket = None
                    path = self._normalise(argument)
                    # REST before STOR is how a dropped upload resumes: the
                    # client says how far it got and sends the rest, so the
                    # bytes already there must be kept rather than replaced.
                    # A 36 GB transfer that failed at 90% should cost seconds
                    # to finish, not an hour to redo.
                    if rest:
                        head = self.files.get(path, b"")[:rest]
                        # A client resuming past the end of what is there would
                        # otherwise leave a hole. Pad so the result is at least
                        # explicit rather than silently short.
                        head = head + b"\0" * max(0, rest - len(head))
                        received = head + received
                    self.files[path] = received
                    self.written[path] = received
                    rest = 0
                    reply("226 transfer complete")
                elif verb == "DELE" and self.writable:
                    reply("250 deleted" if
                          self.files.pop(self._normalise(argument), None)
                          is not None else "550 no such file")
                elif verb == "MKD" and self.writable:
                    reply(f'257 "{argument}" created')
                elif verb == "QUIT":
                    reply("221 bye")
                    break
                else:
                    # Anything that writes lands here and is refused, which is
                    # also an assertion: a test can check nothing tried.
                    reply("502 not implemented")
        except (OSError, socket.timeout, Disconnect):
            pass
        finally:
            self._discard(data_socket)
            try:
                stream.close()
            except OSError:
                pass
            client.close()

    @staticmethod
    def _discard(data_socket):
        if data_socket is not None:
            try:
                data_socket.close()
            except OSError:
                pass
        return None

    @staticmethod
    def _receive(data_socket, limit=None):
        """Reads an upload. limit stops short, which is a transfer that died."""
        if data_socket is None:
            return b""
        chunks = []
        total = 0
        connection = None
        try:
            data_socket.settimeout(5)
            connection, _address = data_socket.accept()
            while True:
                block = connection.recv(8192)
                if not block:
                    break
                if limit is not None and total + len(block) >= limit:
                    chunks.append(block[:max(0, limit - total)])
                    break
                chunks.append(block)
                total += len(block)
        except (OSError, socket.timeout):
            pass
        finally:
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass
            data_socket.close()
        return b"".join(chunks)

    @staticmethod
    def _send(data_socket, body):
        if data_socket is None:
            return
        connection = None
        try:
            data_socket.settimeout(5)
            connection, _address = data_socket.accept()
            connection.sendall(body)
        except (OSError, socket.timeout):
            # A client that has read enough and aborted is normal here: it is
            # exactly what a ranged read does, and the half sent transfer must
            # not leave a socket behind.
            pass
        finally:
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass
            data_socket.close()

    def stop(self):
        self._stop.set()
        if self._server:
            try:
                self._server.close()
            except OSError:
                pass
            self._server = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False


class MockConsole:
    """Both servers together, which is what a collection run needs."""

    def __init__(self, routes=None, listings=None, files=None,
                 writable=False, fault=None):
        self.http = MockWebmanHttp(routes)
        self.ftp = MockWebmanFtp(listings, files, writable=writable,
                                 fault=fault)

    def start(self):
        self.http.start()
        self.ftp.start()
        return self

    def stop(self):
        self.http.stop()
        self.ftp.stop()

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()
        return False
