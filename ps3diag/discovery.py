"""Finding the console on the local network.

Two deliberate constraints shape this. It must not touch anything outside the
private subnets this PC is itself attached to, and it must be drivable by
tests without a network, so the two things that touch the network, the TCP
connect and the HTTP GET, are injected rather than imported. The tests pass
fakes; the GUI passes the real ones.

Working out this PC's own address is done by reading the operating system's
interface table, not by sending anything. ipconfig and ip addr both print what
the kernel already knows, and so does the routing table, which is how the
adapter carrying the default route is found. Nothing here opens a socket
except the injected connect the caller passes to scan().
"""

import concurrent.futures
import ipaddress
import socket
import subprocess
import sys

from .parsers import looks_like_webman, parse_ip_addr_devices, \
    parse_ip_route_default, parse_ipconfig_adapters, webman_score
from .transport import tcp_open

# Kept small because the whole point is that the scan finishes while the user is
# still looking at it. 256 addresses at 0.4s with 64 threads is a couple of
# seconds on a quiet network and about ten on a busy one.
DEFAULT_WORKERS = 64
DEFAULT_CONNECT_TIMEOUT = 0.4

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


class Interface:
    """One IPv4 address this PC holds, and whether it leads anywhere.

    `leads_out` is the signal that matters. An adapter carrying the default
    route is on the wire the router is on, which is the wire the console is
    on; a Hyper-V, WSL, VMware or VirtualBox adapter has an address and no
    route off it. No packet is sent to learn this: the routing table already
    says so and both ipconfig and ip(8) print it.
    """

    def __init__(self, address, device="", gateway="", default_route=False):
        self.address = address
        self.device = device
        self.gateway = gateway
        self.default_route = default_route

    @property
    def leads_out(self):
        return bool(self.default_route
                    or (self.gateway and self.gateway != "0.0.0.0"))

    def __repr__(self):
        return (f"<Interface {self.address} on {self.device!r} "
                f"gateway={self.gateway!r}>")


def local_interfaces(runner=None):
    """Every IPv4 address this PC holds, with the adapter and gateway it came
    from. Reads the OS's own tables; sends nothing, resolves nothing."""
    runner = runner or _run
    if sys.platform == "win32":
        return _windows_interfaces(runner)
    found = _posix_interfaces(runner)
    if found:
        return found
    # ip(8) missing, or a runner handing back ipconfig output, which is what
    # the tests do so that the Windows path can be exercised anywhere.
    return _windows_interfaces(runner)


def _windows_interfaces(runner):
    interfaces = []
    for adapter in parse_ipconfig_adapters(runner(["ipconfig"])):
        for address in adapter["addresses"]:
            interfaces.append(Interface(address, adapter["name"],
                                        adapter["gateway"]))
    return interfaces


def _posix_interfaces(runner):
    devices = parse_ip_addr_devices(runner(["ip", "-4", "addr", "show"]))
    if not devices:
        return []
    routes = {}
    for route in parse_ip_route_default(
            runner(["ip", "route", "show", "default"])):
        routes.setdefault(route["device"], route["gateway"])
    interfaces = []
    for device in devices:
        for address in device["addresses"]:
            name = device["name"]
            interfaces.append(Interface(address, name, routes.get(name, ""),
                                        name in routes))
    return interfaces


def local_addresses(runner=None):
    """Every IPv4 address this PC holds, loopback excluded.

    Reads the interface table through the OS's own tool. No packet is sent, no
    name is resolved, and a machine with several interfaces returns all of them
    so the caller can decide rather than this guessing.
    """
    found = [interface.address for interface in local_interfaces(runner)]
    if found:
        return found
    return _fallback_address()


def _run(command):
    try:
        proc = subprocess.run(command, capture_output=True, text=True,
                              errors="replace", timeout=15,
                              creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return ""
    return f"{proc.stdout}\n{proc.stderr}"


def _fallback_address():
    """Used only when the OS tool is missing or its output changed shape.

    A connected UDP socket sends nothing. connect() on SOCK_DGRAM only asks the
    routing table which local address would be used to reach the given one and
    binds to it, which is exactly the question being asked here.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("10.255.255.255", 9))
        return [probe.getsockname()[0]]
    except OSError:
        return []
    finally:
        probe.close()


def subnet_hosts(address, skip_self=True):
    """The /24 containing address, minus the network, broadcast and this PC."""
    try:
        network = ipaddress.ip_network(f"{address}/24", strict=False)
    except ValueError:
        return []
    return [str(host) for host in network.hosts()
            if not (skip_self and str(host) == address)]


# Ranges a virtual adapter is likely to be sitting on. Deprioritised and
# never excluded: a real LAN on 192.168.56.0/24 is unusual, not impossible,
# and a machine whose only address is one of these still gets searched.
VIRTUAL_SUBNETS = ("192.168.56.", "192.168.99.", "192.168.137.",
                   "192.168.140.", "192.168.174.", "192.168.186.",
                   "192.168.229.", "192.168.248.")

HYPERV_RANGE = ipaddress.ip_network("172.16.0.0/12")

# Enough to cover a laptop with a LAN, wifi, WSL and a VPN without the search
# taking four times as long as it should.
MAX_SUBNETS = 4


def _as_interface(item):
    if isinstance(item, Interface):
        return item
    return Interface(str(item))


def address_rank(address, leads_out=False):
    """How likely this address shares a wire with the console. Lower is
    better; None means it cannot be searched at all.

    The default route beats every heuristic and is checked first. Below that
    the tells are all weak on their own and are added up rather than used to
    exclude: a host-only adapter is nearly always `.1` on its own subnet, WSL
    and Docker live in 172.16/12, and VMware and VirtualBox favour a handful
    of 192.168 ranges.
    """
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return None
    if (parsed.version != 4 or not parsed.is_private or parsed.is_loopback
            or parsed.is_link_local):
        return None
    if leads_out:
        return 0
    score = 1
    if address.endswith(".1"):
        score += 2
    if address.startswith(VIRTUAL_SUBNETS):
        score += 2
    if not address.startswith("192.168."):
        score += 1
    if parsed in HYPERV_RANGE:
        score += 1
    return score


def choose_subnets(interfaces, limit=MAX_SUBNETS):
    """The addresses whose /24s are worth searching, best first, one per /24.

    If anything carries the default route, only those are searched: that is
    the router's wire and guessing past it would be wrong. Otherwise every
    plausible subnet is searched rather than one being picked and blamed,
    because picking wrong is exactly the bug this replaces.
    """
    ranked = []
    for order, item in enumerate(interfaces or []):
        interface = _as_interface(item)
        rank = address_rank(interface.address, interface.leads_out)
        if rank is None:
            continue
        ranked.append((rank, order, interface.address))
    ranked.sort()
    if ranked and ranked[0][0] == 0:
        ranked = [entry for entry in ranked if entry[0] == 0]
    chosen = []
    seen = set()
    for _rank, _order, address in ranked:
        network = subnet_label(address)
        if network in seen:
            continue
        seen.add(network)
        chosen.append(address)
        if len(chosen) >= max(1, limit):
            break
    return chosen


def preferred_address(interfaces):
    """The single best address, for callers that still want just one."""
    chosen = choose_subnets(interfaces, limit=1)
    return chosen[0] if chosen else ""


def subnet_label(address):
    """The /24 an address sits in, written the way the search reports it."""
    try:
        return str(ipaddress.ip_network(f"{address}/24", strict=False))
    except ValueError:
        return ""


def scan_hosts(addresses, exclude=()):
    """Every host across the chosen /24s, deduplicated, this PC left out.

    Two adapters on the same /24 must not double the work, and none of this
    PC's own addresses is ever a candidate.
    """
    skip = set(exclude) | set(addresses)
    hosts = []
    seen = set()
    for address in addresses:
        for host in subnet_hosts(address, skip_self=False):
            if host in skip or host in seen:
                continue
            seen.add(host)
            hosts.append(host)
    return hosts


class Candidate:
    def __init__(self, address, score, markers, title=""):
        self.address = address
        self.score = score
        self.markers = markers
        self.title = title

    def __repr__(self):
        return f"<Candidate {self.address} score={self.score} {self.title!r}>"


def _title_of(body):
    import re
    match = re.search(r"(?is)<title[^>]*>(.*?)</title>", body or "")
    return re.sub(r"\s+", " ", match.group(1)).strip() if match else ""


def scan(hosts, fetch, connect=None, workers=DEFAULT_WORKERS,
         connect_timeout=DEFAULT_CONNECT_TIMEOUT, on_progress=None,
         should_stop=None):
    """Returns the hosts that answered on 80 and looked like webMAN.

    connect(host) says whether port 80 accepts, fetch(host) returns the body of
    GET / or None. Both are parameters so the test suite can run the whole of
    this against a table of canned answers, and so the only code that can put a
    packet on the wire is the pair the GUI hands in.
    """
    connect = connect or (lambda host: tcp_open(host, 80, connect_timeout))
    found = []
    done = 0
    total = len(hosts)

    def check(host):
        if should_stop and should_stop():
            return None
        if not connect(host):
            return None
        body = fetch(host)
        if not body:
            return None
        score, markers = webman_score(body)
        if score < 5:
            return None
        return Candidate(host, score, markers, _title_of(body))

    with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, min(workers, total or 1))) as pool:
        futures = {pool.submit(check, host): host for host in hosts}
        for future in concurrent.futures.as_completed(futures):
            done += 1
            try:
                candidate = future.result()
            except Exception:
                candidate = None
            if candidate:
                found.append(candidate)
            if on_progress:
                on_progress(done, total, len(found))
    found.sort(key=lambda item: (-item.score, item.address))
    return found


def describe_outcome(candidates):
    """The sentence the window shows. Plain English, no jargon, no next steps
    the user cannot act on."""
    if len(candidates) == 1:
        return "one", f"Found a PS3 at {candidates[0].address}."
    if candidates:
        return "several", (f"Found {len(candidates)} devices that look like a "
                           f"PS3. Pick the right one from the list.")
    return "none", (
        "No PS3 found on this network. Check it is switched on, that it is at "
        "the XMB rather than in a game, that webMAN is running, and that it is "
        "plugged into the same router as this PC. If you know the console's IP "
        "address you can type it in instead.")
