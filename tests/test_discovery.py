"""Discovery tests.

The scan is never run against a real subnet, here or anywhere else. Both of the
things that would touch the network are parameters, and these tests pass fakes
for both, so the whole of the search logic is exercised with nothing leaving the
machine.
"""

from support import FixtureCase, fixture
from ps3diag import discovery


class Subnet(FixtureCase):
    def test_a_full_slash_24_without_this_pc(self):
        hosts = discovery.subnet_hosts("192.168.1.23")
        self.assertEqual(len(hosts), 253)
        self.assertIn("192.168.1.1", hosts)
        self.assertIn("192.168.1.254", hosts)
        self.assertNotIn("192.168.1.23", hosts)
        self.assertNotIn("192.168.1.0", hosts)
        self.assertNotIn("192.168.1.255", hosts)

    def test_rubbish_gives_nothing_rather_than_raising(self):
        self.assertEqual(discovery.subnet_hosts("not an address"), [])
        self.assertEqual(discovery.subnet_hosts(""), [])


class LocalAddresses(FixtureCase):
    def test_reads_the_interface_table_it_is_given(self):
        text = fixture("net", "ip_addr_linux.txt")
        found = discovery.local_addresses(runner=lambda command: text)
        self.assertIn("192.168.1.23", found)
        self.assertNotIn("127.0.0.1", found)

    def test_windows_output(self):
        text = fixture("net", "ipconfig_windows.txt")
        found = discovery.local_addresses(runner=lambda command: text)
        self.assertIn("192.168.1.23", found)


class Interfaces(FixtureCase):
    """Which wire the search is pointed at.

    Every runner here is a canned string. Nothing shells out, nothing is sent,
    and no subnet is ever scanned by these tests.
    """

    def _windows(self, name):
        text = fixture("net", name)
        return discovery.local_interfaces(runner=lambda command: text)

    def _posix(self, addr_name, route_name):
        addresses = fixture("net", addr_name)
        routes = fixture("net", route_name)
        return discovery.local_interfaces(
            runner=lambda command: routes if "route" in command else addresses)

    def test_the_real_lan_wins_because_it_has_the_default_gateway(self):
        # The reported bug: this PC held 192.168.140.1 on a VMware adapter and
        # the console was on 192.168.50.0/24 the whole time.
        interfaces = self._windows("ipconfig_failing_machine.txt")
        self.assertEqual(discovery.choose_subnets(interfaces),
                         ["192.168.50.10"])

    def test_the_gateway_beats_the_order_the_adapters_are_printed_in(self):
        # The virtual adapters come first in ipconfig on that machine, so
        # anything relying on order would still pick one of them.
        interfaces = self._windows("ipconfig_failing_machine.txt")
        self.assertEqual(interfaces[0].address, "172.29.64.1")
        self.assertFalse(interfaces[0].leads_out)
        self.assertTrue(interfaces[-1].leads_out)

    def test_the_posix_default_route_is_matched_back_to_its_address(self):
        interfaces = self._posix("ip_addr_multi.txt", "ip_route_default.txt")
        leading = [item.address for item in interfaces if item.leads_out]
        self.assertEqual(leading, ["192.168.50.10"])
        self.assertEqual(discovery.choose_subnets(interfaces),
                         ["192.168.50.10"])

    def test_with_no_default_route_every_plausible_subnet_is_searched(self):
        interfaces = self._windows("ipconfig_no_default_gateway.txt")
        chosen = discovery.choose_subnets(interfaces)
        # Several, not one: picking one and reporting failure is the bug.
        self.assertGreater(len(chosen), 1)
        self.assertEqual(chosen[0], "192.168.50.10")
        self.assertIn("192.168.140.1", chosen)

    def test_the_number_of_subnets_searched_is_capped(self):
        interfaces = self._windows("ipconfig_no_default_gateway.txt")
        self.assertEqual(len(interfaces), 5)
        self.assertLessEqual(len(discovery.choose_subnets(interfaces)),
                             discovery.MAX_SUBNETS)
        self.assertEqual(len(discovery.choose_subnets(interfaces, limit=2)), 2)

    def test_a_posix_box_with_no_default_route_searches_more_than_one(self):
        interfaces = self._posix("ip_addr_multi.txt",
                                 "ip_route_no_default.txt")
        self.assertFalse(any(item.leads_out for item in interfaces))
        chosen = discovery.choose_subnets(interfaces)
        self.assertEqual(chosen[0], "192.168.50.10")
        self.assertIn("172.17.0.1", chosen)

    def test_a_host_only_adapter_is_deprioritised_and_never_excluded(self):
        # .1 on its own subnet is the strongest tell there is, but a real LAN
        # on 192.168.56.0/24 is unusual rather than impossible.
        for virtual in ("192.168.56.1", "192.168.140.1", "172.19.16.1"):
            self.assertEqual(
                discovery.choose_subnets([virtual, "192.168.178.22"])[0],
                "192.168.178.22", virtual)
            self.assertEqual(discovery.choose_subnets([virtual]), [virtual])

    def test_a_gateway_beats_every_heuristic_against_it(self):
        # A genuine LAN on VirtualBox's usual range, with the PC at .1 on it.
        real = discovery.Interface("192.168.56.1", "Ethernet",
                                   "192.168.56.254")
        virtual = discovery.Interface("192.168.1.20", "vEthernet")
        self.assertEqual(discovery.choose_subnets([virtual, real]),
                         ["192.168.56.1"])

    def test_two_adapters_on_one_subnet_are_searched_once(self):
        chosen = discovery.choose_subnets(["192.168.50.10", "192.168.50.11"])
        self.assertEqual(chosen, ["192.168.50.10"])

    def test_addresses_that_cannot_hold_a_console_are_dropped(self):
        self.assertEqual(
            discovery.choose_subnets(["8.8.8.8", "127.0.0.1", "169.254.7.7",
                                      "nonsense", ""]), [])
        self.assertEqual(discovery.preferred_address([]), "")

    def test_hosts_across_the_chosen_subnets_leave_this_pc_out(self):
        hosts = discovery.scan_hosts(["192.168.50.10", "192.168.140.1"],
                                     exclude=["172.19.16.1"])
        self.assertEqual(len(hosts), 253 + 253)
        self.assertIn("192.168.50.95", hosts)
        self.assertIn("192.168.140.20", hosts)
        self.assertNotIn("192.168.50.10", hosts)
        self.assertNotIn("192.168.140.1", hosts)
        self.assertEqual(len(set(hosts)), len(hosts))

    def test_local_addresses_still_answers_the_older_question(self):
        found = [item.address
                 for item in self._windows("ipconfig_failing_machine.txt")]
        self.assertEqual(discovery.local_addresses(
            runner=lambda command: fixture(
                "net", "ipconfig_failing_machine.txt")), found)


class Scan(FixtureCase):
    """Both network calls are injected, so nothing here opens a socket."""

    def setUp(self):
        self.webman = fixture("http", "root_147.html")
        self.router = fixture("http", "not_webman.html")
        self.connected = []

    def scan(self, answers, hosts=None):
        def connect(host):
            self.connected.append(host)
            return host in answers

        def fetch(host):
            return answers.get(host)

        return discovery.scan(hosts or sorted(answers) or ["10.0.0.1"],
                              fetch, connect=connect, workers=4)

    def test_one_console_among_silence(self):
        found = self.scan({"192.168.1.42": self.webman})
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].address, "192.168.1.42")
        self.assertIn("webMAN", found[0].title)

    def test_a_router_answering_on_80_is_not_a_ps3(self):
        found = self.scan({"192.168.1.1": self.router})
        self.assertEqual(found, [])

    def test_two_consoles_are_both_returned(self):
        found = self.scan({"192.168.1.42": self.webman,
                           "192.168.1.43": self.webman,
                           "192.168.1.1": self.router})
        self.assertEqual(len(found), 2)

    def test_a_host_that_does_not_accept_is_never_fetched(self):
        fetched = []

        def fetch(host):
            fetched.append(host)
            return None

        discovery.scan(["10.0.0.1", "10.0.0.2"], fetch,
                       connect=lambda host: False, workers=2)
        self.assertEqual(fetched, [])

    def test_a_fetch_that_throws_does_not_stop_the_scan(self):
        def fetch(host):
            if host == "10.0.0.1":
                raise OSError("refused")
            return self.webman

        found = discovery.scan(["10.0.0.1", "10.0.0.2"], fetch,
                               connect=lambda host: True, workers=2)
        self.assertEqual([item.address for item in found], ["10.0.0.2"])

    def test_stopping_early(self):
        found = discovery.scan(["10.0.0.1"], lambda host: self.webman,
                               connect=lambda host: True,
                               should_stop=lambda: True)
        self.assertEqual(found, [])

    def test_progress_is_reported_for_every_host(self):
        seen = []
        discovery.scan(["10.0.0.1", "10.0.0.2", "10.0.0.3"],
                       lambda host: None, connect=lambda host: False,
                       workers=2, on_progress=lambda *args: seen.append(args),
                       passes=1)
        self.assertEqual(len(seen), 3)

    def test_a_silent_sweep_is_tried_once_more_and_reports_both(self):
        # The retry exists because the first packet to an address nobody has
        # spoken to waits on ARP, and a console can miss a short timeout
        # because of it. Progress covers both sweeps: the search really is
        # doing the work twice, and saying otherwise would be a lie about how
        # long it will take.
        seen = []
        discovery.scan(["10.0.0.1", "10.0.0.2", "10.0.0.3"],
                       lambda host: None, connect=lambda host: False,
                       workers=2, on_progress=lambda *args: seen.append(args))
        self.assertEqual(len(seen), 6)


class Outcome(FixtureCase):
    def test_the_three_messages(self):
        one = [discovery.Candidate("192.168.1.42", 9, [])]
        self.assertEqual(discovery.describe_outcome(one)[0], "one")
        self.assertIn("192.168.1.42", discovery.describe_outcome(one)[1])
        several = one + [discovery.Candidate("192.168.1.43", 9, [])]
        self.assertEqual(discovery.describe_outcome(several)[0], "several")
        kind, message = discovery.describe_outcome([])
        self.assertEqual(kind, "none")
        # The failure message has to tell a non-technical user what to check.
        for hint in ("switched on", "webMAN", "same router"):
            self.assertIn(hint, message)
