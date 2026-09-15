"""The update check, with every network call injected.

There is a live PS3 on this network and this module is the first part of the
program that can talk to the internet at all, so the rule here is stricter than
"do not hit the network by accident": no test in this file may be capable of
hitting it. Every one of them passes its own fetcher, and setUp replaces
urllib's opener with something that raises, so a fetcher forgotten in a future
test fails the test rather than making a request.
"""

import json
import os
import tempfile
import shutil
import unittest
import urllib.error
import urllib.request

from support import ROOT  # noqa: F401  (puts the project on sys.path)

from ps3tools import update


def release_payload(tag="v2.1", notes="", assets=None, **extra):
    payload = {
        "tag_name": tag,
        "name": f"PS3 Tools {tag}",
        "body": notes,
        "html_url": f"https://github.com/{update.REPOSITORY}/releases/{tag}",
        "assets": [] if assets is None else assets,
    }
    payload.update(extra)
    return payload


def exe_asset(name="PS3-Tools.exe", url=None, size=1234):
    return {"name": name, "size": size,
            "browser_download_url":
                url or f"https://github.com/{update.REPOSITORY}/{name}"}


def json_fetcher(payload, record=None):
    """A fetcher that answers with one JSON document and notes it was asked."""
    def fetch(url, timeout=None):
        if record is not None:
            record.append((url, timeout))
        return json.dumps(payload).encode("utf-8")
    return fetch


def failing_fetcher(exception, record=None):
    def fetch(url, timeout=None):
        if record is not None:
            record.append((url, timeout))
        raise exception
    return fetch


class NoNetworkCase(unittest.TestCase):
    """Makes the default code path fail loudly instead of quietly working."""

    def setUp(self):
        self.calls = []

        def refuse(*args, **kwargs):
            raise AssertionError(
                "a test reached the real urllib opener; inject a fetcher")

        self._saved = urllib.request.urlopen
        urllib.request.urlopen = refuse
        self.addCleanup(self._restore)

    def _restore(self):
        urllib.request.urlopen = self._saved


# --- versions ---------------------------------------------------------------

class VersionTests(NoNetworkCase):
    def test_parses_a_plain_version(self):
        self.assertEqual(update.parse_version("2.1"), (2, 1))

    def test_strips_the_v_prefix(self):
        self.assertEqual(update.parse_version("v2.1.3"), (2, 1, 3))
        self.assertEqual(update.parse_version("V2"), (2,))

    def test_tolerates_surrounding_space(self):
        self.assertEqual(update.parse_version("  v3.0 "), (3, 0))

    def test_refuses_what_it_cannot_read(self):
        for text in ("", "latest", "2.1-rc1", "v", "2..1", "two.one",
                     None, 2.1, "2.1a"):
            self.assertIsNone(update.parse_version(text), text)

    def test_newer_version(self):
        self.assertTrue(update.is_newer("2.1", "2.0"))
        self.assertTrue(update.is_newer("v2.0.1", "2.0"))
        self.assertTrue(update.is_newer("10.0", "9.9"))

    def test_older_version(self):
        self.assertFalse(update.is_newer("1.9", "2.0"))
        self.assertFalse(update.is_newer("2.0", "2.0.1"))

    def test_equal_version(self):
        self.assertFalse(update.is_newer("2.0", "2.0"))
        self.assertFalse(update.is_newer("v2.0", "2.0"))

    def test_differing_segment_counts_pad_with_zeros(self):
        self.assertEqual(update.compare_versions("2.0", "2.0.0"), 0)
        self.assertEqual(update.compare_versions("2.0.0.1", "2.0"), 1)

    def test_malformed_version_is_never_newer(self):
        for tag in ("", "latest", "v", "nightly", "2.1-rc1", None):
            self.assertFalse(update.is_newer(tag, "2.0"), tag)

    def test_comparing_the_unreadable_raises_rather_than_guessing(self):
        with self.assertRaises(ValueError):
            update.compare_versions("latest", "2.0")


# --- fetching ---------------------------------------------------------------

class FetchTests(NoNetworkCase):
    def test_fetches_the_one_url_built_from_the_constant(self):
        seen = []
        payload = update.fetch_release(
            fetcher=json_fetcher(release_payload(), seen))
        self.assertEqual(payload["tag_name"], "v2.1")
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], update.RELEASES_URL)
        self.assertIn(update.REPOSITORY, update.RELEASES_URL)
        self.assertTrue(update.RELEASES_URL.startswith(
            "https://api.github.com/repos/"))

    def test_a_timeout_is_always_passed(self):
        seen = []
        update.fetch_release(fetcher=json_fetcher(release_payload(), seen))
        self.assertEqual(seen[0][1], update.TIMEOUT)

    def test_no_network_is_silent(self):
        self.assertIsNone(update.fetch_release(
            fetcher=failing_fetcher(OSError("Network is unreachable"))))

    def test_http_error_is_silent(self):
        error = urllib.error.HTTPError(
            update.RELEASES_URL, 404, "Not Found", {}, None)
        self.assertIsNone(update.fetch_release(fetcher=failing_fetcher(error)))

    def test_rate_limited_is_silent(self):
        error = urllib.error.HTTPError(
            update.RELEASES_URL, 403, "rate limit exceeded", {}, None)
        self.assertIsNone(update.fetch_release(fetcher=failing_fetcher(error)))

    def test_rate_limit_body_without_a_release_is_silent(self):
        # GitHub answers a rate limit with a perfectly valid JSON document.
        body = {"message": "API rate limit exceeded", "documentation_url": "x"}
        self.assertIsNone(update.fetch_release(fetcher=json_fetcher(body)))

    def test_malformed_json_is_silent(self):
        def fetch(url, timeout=None):
            return b"<html>captive portal</html>"
        self.assertIsNone(update.fetch_release(fetcher=fetch))

    def test_a_json_list_is_silent(self):
        self.assertIsNone(update.fetch_release(fetcher=json_fetcher([1, 2])))

    def test_missing_tag_name_is_silent(self):
        payload = release_payload()
        del payload["tag_name"]
        self.assertIsNone(update.fetch_release(fetcher=json_fetcher(payload)))

    def test_empty_tag_name_is_silent(self):
        self.assertIsNone(update.fetch_release(
            fetcher=json_fetcher(release_payload(tag="   "))))

    def test_the_user_agent_names_the_program(self):
        self.assertIn("PS3Tools", update.USER_AGENT)
        self.assertIn(update.VERSION, update.USER_AGENT)

    def test_the_release_version_is_the_one_being_shipped(self):
        """Pins VERSION so a stray edit cannot ship as the wrong release.

        A build that reports the wrong version makes the update check
        meaningless: the reported version is the whole of what a release is
        compared against.

        Changing the release version is meant to be deliberate. When you bump
        it, bump it here too -- that is the point of this test, not an
        obstacle to it. It exists so a version change is always a decision
        somebody made rather than something noticed later on a screenshot.
        """
        self.assertEqual(update.VERSION, "1.2.1")
        # Plain dotted numbers, or the tag comparison silently stops working.
        self.assertIsNotNone(update.parse_version(update.VERSION))

    def test_the_repository_is_confirmed_and_hard_coded(self):
        # The repository is settled. What this guards now is that it stays a
        # constant: if the name is ever made configurable, or read out of a
        # server's answer, this test is the thing that should stop it. A URL
        # taken from a reply is how a checker starts fetching somebody else's
        # releases.
        self.assertEqual(update.REPOSITORY, "setsid/ps3-tools")
        self.assertEqual(
            update.RELEASES_URL,
            "https://api.github.com/repos/setsid/ps3-tools/releases/latest")
        path = os.path.join(os.path.dirname(update.__file__), "update.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("UNCONFIRMED", source)
        self.assertEqual(source.count('REPOSITORY = "'), 1)

    def test_the_about_screen_link_comes_from_the_same_constant(self):
        # Two places naming the repository is two places to get it wrong.
        from ps3tools.screens import about
        links = dict((name, url) for name, url in about.REPOSITORIES)
        self.assertIn(f"https://github.com/{update.REPOSITORY}",
                      links.values())


# --- release parsing ---------------------------------------------------------

class ReleaseTests(NoNetworkCase):
    def test_reads_the_fields_the_banner_needs(self):
        release = update.Release(release_payload(
            tag="v2.1", notes="Fixes the thing.",
            assets=[exe_asset()]))
        self.assertEqual(release.tag, "v2.1")
        self.assertEqual(release.version, (2, 1))
        self.assertEqual(release.notes, "Fixes the thing.")
        self.assertEqual(release.asset_name, "PS3-Tools.exe")
        self.assertIn("Version 2.1", release.summary())

    def test_ignores_assets_that_are_not_the_program(self):
        release = update.Release(release_payload(
            assets=[{"name": "source.zip",
                     "browser_download_url": "https://example.invalid/s.zip"},
                    exe_asset()]))
        self.assertEqual(release.asset_name, "PS3-Tools.exe")

    def test_survives_rubbish_in_the_assets_list(self):
        release = update.Release(
            release_payload(assets=["nonsense", None, {}]))
        self.assertEqual(release.asset_name, "")
        self.assertEqual(release.asset_url, "")


class HashInNotesTests(NoNetworkCase):
    DIGEST = "a" * 64
    OTHER = "b" * 64

    def test_finds_a_single_hash(self):
        notes = f"Fixes things.\n\nsha256: {self.DIGEST}\n"
        self.assertEqual(update.sha256_from_notes(notes), self.DIGEST)

    def test_is_case_insensitive_and_normalises(self):
        notes = "sha256 " + ("A" * 64)
        self.assertEqual(update.sha256_from_notes(notes), "a" * 64)

    def test_no_hash_at_all(self):
        self.assertEqual(update.sha256_from_notes("Nothing here."), "")
        self.assertEqual(update.sha256_from_notes(""), "")
        self.assertEqual(update.sha256_from_notes(None), "")

    def test_a_short_hex_string_is_not_a_hash(self):
        self.assertEqual(update.sha256_from_notes("deadbeef"), "")

    def test_several_hashes_and_none_named_is_refused(self):
        # Refusing is the point: picking one at random turns "not verified"
        # into "failed verification", which reads as an attack.
        notes = f"{self.DIGEST}\n{self.OTHER}\n"
        self.assertEqual(update.sha256_from_notes(notes), "")

    def test_several_hashes_with_the_asset_named_picks_that_one(self):
        notes = (f"source.zip {self.OTHER}\n"
                 f"PS3-Tools.exe {self.DIGEST}\n")
        self.assertEqual(
            update.sha256_from_notes(notes, "PS3-Tools.exe"), self.DIGEST)

    def test_release_picks_up_the_hash_for_its_own_asset(self):
        release = update.Release(release_payload(
            notes=f"PS3-Tools.exe {self.DIGEST}\nsource.zip {self.OTHER}",
            assets=[exe_asset()]))
        self.assertEqual(release.sha256, self.DIGEST)
        self.assertTrue(release.verifiable)

    def test_release_with_no_hash_is_not_verifiable(self):
        release = update.Release(release_payload(notes="Fixed a thing."))
        self.assertFalse(release.verifiable)


# --- the check --------------------------------------------------------------

class CheckTests(NoNetworkCase):
    def setUp(self):
        super().setUp()
        self.settings = {}
        self.seen = []

    def fetcher(self, payload):
        return json_fetcher(payload, self.seen)

    def test_newer_release_is_reported(self):
        release = update.check(
            self.settings, fetcher=self.fetcher(release_payload("v2.1")),
            now=1000.0)
        self.assertIsNotNone(release)
        self.assertEqual(release.tag, "v2.1")

    def test_same_version_says_nothing(self):
        self.assertIsNone(update.check(
            self.settings,
            fetcher=self.fetcher(release_payload(update.VERSION)),
            now=1000.0))

    def test_older_version_says_nothing(self):
        # Derived from VERSION rather than written out. A literal here was
        # "older" only while the program happened to be 2.0, and silently
        # became a newer version the moment the release number changed.
        parts = list(update.parse_version(update.VERSION))
        # Decrement the last segment that is not already zero, and zero
        # whatever follows it. Decrementing the first segment breaks on a 0.x
        # release, which is exactly what this program is at the moment.
        index = max(i for i, part in enumerate(parts) if part)
        parts[index] -= 1
        parts[index + 1:] = [0] * (len(parts) - index - 1)
        older = "v" + ".".join(str(part) for part in parts)
        self.assertTrue(update.is_newer(update.VERSION, older),
                        f"{older} should be older than {update.VERSION}")
        self.assertIsNone(update.check(
            self.settings, fetcher=self.fetcher(release_payload(older)),
            now=1000.0))

    def test_malformed_tag_says_nothing(self):
        self.assertIsNone(update.check(
            self.settings, fetcher=self.fetcher(release_payload("nightly")),
            now=1000.0))

    def test_no_network_says_nothing_and_does_not_raise(self):
        self.assertIsNone(update.check(
            self.settings,
            fetcher=failing_fetcher(OSError("unreachable"), self.seen),
            now=1000.0))

    def test_rate_limited_says_nothing(self):
        error = urllib.error.HTTPError(
            update.RELEASES_URL, 403, "rate limit exceeded", {}, None)
        self.assertIsNone(update.check(
            self.settings, fetcher=failing_fetcher(error), now=1000.0))

    def test_malformed_json_says_nothing(self):
        def fetch(url, timeout=None):
            self.seen.append(url)
            return b"{not json"
        self.assertIsNone(update.check(self.settings, fetcher=fetch,
                                       now=1000.0))

    def test_missing_tag_name_says_nothing(self):
        payload = release_payload()
        del payload["tag_name"]
        self.assertIsNone(update.check(self.settings,
                                       fetcher=self.fetcher(payload),
                                       now=1000.0))

    # -- the cache

    def test_a_second_check_inside_a_day_does_not_ask_again(self):
        first = update.check(self.settings,
                             fetcher=self.fetcher(release_payload("v2.1")),
                             now=1000.0)
        self.assertIsNotNone(first)
        self.assertEqual(len(self.seen), 1)
        second = update.check(self.settings,
                              fetcher=self.fetcher(release_payload("v2.1")),
                              now=1000.0 + update.CACHE_SECONDS - 1)
        self.assertEqual(len(self.seen), 1, "asked GitHub twice in one day")
        self.assertIsNotNone(second)
        self.assertEqual(second.tag, "v2.1")

    def test_a_check_after_a_day_asks_again(self):
        update.check(self.settings,
                     fetcher=self.fetcher(release_payload("v2.1")),
                     now=1000.0)
        update.check(self.settings,
                     fetcher=self.fetcher(release_payload("v2.2")),
                     now=1000.0 + update.CACHE_SECONDS + 1)
        self.assertEqual(len(self.seen), 2)

    def test_a_failed_check_still_starts_the_clock(self):
        # Otherwise a machine with no internet asks at every launch, and an
        # address that is rate limited stays that way by being asked.
        update.check(self.settings,
                     fetcher=failing_fetcher(OSError("down"), self.seen),
                     now=1000.0)
        self.assertEqual(len(self.seen), 1)
        update.check(self.settings,
                     fetcher=failing_fetcher(OSError("down"), self.seen),
                     now=1000.0 + 60)
        self.assertEqual(len(self.seen), 1)

    def test_a_clock_that_went_backwards_does_not_pin_the_cache(self):
        update.check(self.settings,
                     fetcher=self.fetcher(release_payload("v2.1")),
                     now=10_000.0)
        update.check(self.settings,
                     fetcher=self.fetcher(release_payload("v2.1")),
                     now=5_000.0)
        self.assertEqual(len(self.seen), 2)

    def test_a_corrupt_cache_is_ignored_rather_than_fatal(self):
        self.settings[update.SETTING_CACHE] = "not a dict"
        release = update.check(self.settings,
                               fetcher=self.fetcher(release_payload("v2.1")),
                               now=1000.0)
        self.assertIsNotNone(release)

    def test_force_ignores_the_cache(self):
        update.check(self.settings,
                     fetcher=self.fetcher(release_payload("v2.1")),
                     now=1000.0)
        update.check(self.settings,
                     fetcher=self.fetcher(release_payload("v2.1")),
                     now=1001.0, force=True)
        self.assertEqual(len(self.seen), 2)

    def test_clearing_the_cache_lets_the_next_check_ask(self):
        update.check(self.settings,
                     fetcher=self.fetcher(release_payload("v2.1")),
                     now=1000.0)
        update.clear_cache(self.settings)
        update.check(self.settings,
                     fetcher=self.fetcher(release_payload("v2.1")),
                     now=1001.0)
        self.assertEqual(len(self.seen), 2)

    def test_the_cache_is_json_serialisable(self):
        # The shell writes the settings dict out as JSON, and a value that
        # will not serialise is dropped silently at the write.
        update.check(self.settings,
                     fetcher=self.fetcher(release_payload("v2.1")),
                     now=1000.0)
        json.dumps(self.settings)

    # -- the setting

    def test_on_by_default(self):
        self.assertTrue(update.enabled({}))
        self.assertTrue(update.ENABLED_BY_DEFAULT)

    def test_disabled_never_asks(self):
        update.set_enabled(self.settings, False)
        self.assertIsNone(update.check(
            self.settings, fetcher=self.fetcher(release_payload("v2.1")),
            now=1000.0))
        self.assertEqual(self.seen, [])

    def test_disabled_is_not_overridden_by_force(self):
        update.set_enabled(self.settings, False)
        self.assertIsNone(update.check(
            self.settings, fetcher=self.fetcher(release_payload("v2.1")),
            now=1000.0, force=True))
        self.assertEqual(self.seen, [])

    def test_a_nonsense_setting_falls_back_to_the_default(self):
        self.settings[update.SETTING_ENABLED] = "yes please"
        self.assertTrue(update.enabled(self.settings))

    def test_no_settings_at_all_still_works(self):
        self.assertIsNone(update.check(
            None, fetcher=failing_fetcher(OSError("down"))))

    def test_the_default_fetcher_is_never_reached_from_a_test(self):
        # Proof that the whole thing is injectable: with no fetcher passed,
        # the default path reaches urllib, which setUp has made raise. The
        # check swallows it, which is also the "no network" behaviour.
        self.assertIsNone(update.check(self.settings, now=1000.0))


# --- downloading ------------------------------------------------------------

class DownloadTests(NoNetworkCase):
    PAYLOAD = b"MZ this is not really an exe"

    def setUp(self):
        super().setUp()
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.folder = holder.name
        self.digest = update.digest_bytes(self.PAYLOAD)

    def bytes_fetcher(self, data=None, record=None):
        def fetch(url, timeout=None):
            if record is not None:
                record.append((url, timeout))
            return self.PAYLOAD if data is None else data
        return fetch

    def release(self, notes=""):
        return update.Release(release_payload(
            tag="v2.1", notes=notes, assets=[exe_asset()]))

    def test_a_matching_hash_is_saved_and_called_verified(self):
        result = update.download(self.release(f"sha256 {self.digest}"),
                                 fetcher=self.bytes_fetcher(),
                                 folder=self.folder)
        self.assertTrue(result.ok)
        self.assertIs(result.verified, True)
        self.assertTrue(os.path.isfile(result.path))
        self.assertEqual(os.path.dirname(result.path), self.folder)
        self.assertIn("matches the checksum", result.detail)

    def test_a_mismatched_hash_is_not_kept(self):
        result = update.download(self.release("sha256 " + "c" * 64),
                                 fetcher=self.bytes_fetcher(),
                                 folder=self.folder)
        self.assertFalse(result.ok)
        self.assertIs(result.verified, False)
        self.assertEqual(result.path, "")
        self.assertEqual(os.listdir(self.folder), [])
        self.assertIn("does not match", result.detail)
        self.assertIn("Do not run", result.detail)

    def test_notes_with_no_hash_say_so_and_do_not_claim_verified(self):
        result = update.download(self.release("Fixed a thing."),
                                 fetcher=self.bytes_fetcher(),
                                 folder=self.folder)
        self.assertTrue(result.ok)
        self.assertIsNone(result.verified)
        self.assertIn("not been verified", result.detail)
        self.assertNotIn("matches the checksum", result.detail)
        self.assertIn(self.digest, result.detail)

    def test_two_hashes_and_no_asset_named_is_treated_as_no_hash(self):
        notes = ("a" * 64) + "\n" + ("b" * 64)
        result = update.download(self.release(notes),
                                 fetcher=self.bytes_fetcher(),
                                 folder=self.folder)
        self.assertIsNone(result.verified)
        self.assertIn("not been verified", result.detail)

    def test_a_failed_download_is_reported_not_raised(self):
        result = update.download(self.release(),
                                 fetcher=failing_fetcher(OSError("gone")),
                                 folder=self.folder)
        self.assertFalse(result.ok)
        self.assertEqual(os.listdir(self.folder), [])
        self.assertIn("did not finish", result.detail)

    def test_a_release_with_no_download_says_so(self):
        release = update.Release(release_payload(tag="v2.1"))
        result = update.download(release, fetcher=self.bytes_fetcher(),
                                 folder=self.folder)
        self.assertFalse(result.ok)
        self.assertIn("no download", result.detail)

    def test_nothing_is_written_outside_the_chosen_folder(self):
        # The asset name comes from GitHub, so a name with a path in it must
        # not be able to choose where the file lands.
        release = update.Release(release_payload(
            tag="v2.1", assets=[exe_asset(name="../../evil.exe")]))
        result = update.download(release, fetcher=self.bytes_fetcher(),
                                 folder=self.folder)
        self.assertEqual(os.path.dirname(result.path), self.folder)

    def test_the_download_is_asked_for_the_asset_url_with_a_timeout(self):
        seen = []
        update.download(self.release(), fetcher=self.bytes_fetcher(None, seen),
                        folder=self.folder)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][1], update.TIMEOUT)

    def test_the_default_target_is_the_desktop(self):
        from ps3diag import config
        self.assertEqual(update.target_folder(), config.desktop_dir())

    def test_nothing_downloads_without_being_asked(self):
        # check() returns a Release and never a file. The only way bytes are
        # fetched is a call to download(), which only a button makes.
        seen = []
        release = update.check({}, fetcher=json_fetcher(
            release_payload("v2.1", assets=[exe_asset()]), seen), now=1.0)
        self.assertIsNotNone(release)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], update.RELEASES_URL)



class WhereTheDownloadLands(unittest.TestCase):
    """Nothing on the Desktop is ever written over.

    Reported from a real Desktop: every release ships the same file name, the
    copy already there was the program doing the downloading, Windows had it
    locked, and the save failed as "write protected" with nothing the user
    could do about it. Closing the program first cannot help, because the
    program is the thing doing the writing.
    """

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="ps3-update-target-")
        self.addCleanup(shutil.rmtree, self.folder, True)

    def name_for(self, asset="ps3-tools.exe", tag="v1.2.0"):
        return os.path.basename(update.free_path(self.folder, asset, tag))

    def test_the_version_goes_in_the_name(self):
        self.assertEqual(self.name_for(), "ps3-tools-1.2.0.exe")

    def test_a_file_that_is_already_there_is_left_alone(self):
        first = update.free_path(self.folder, "ps3-tools.exe", "v1.2.0")
        with open(first, "wb") as handle:
            handle.write(b"the copy that is running")
        second = update.free_path(self.folder, "ps3-tools.exe", "v1.2.0")
        self.assertNotEqual(first, second)
        self.assertEqual(os.path.basename(second), "ps3-tools-1.2.0 (2).exe")
        with open(first, "rb") as handle:
            self.assertEqual(handle.read(), b"the copy that is running")

    def test_it_keeps_counting_rather_than_giving_up(self):
        for _ in range(4):
            path = update.free_path(self.folder, "ps3-tools.exe", "v1.2.0")
            open(path, "wb").close()
        self.assertEqual(self.name_for(), "ps3-tools-1.2.0 (5).exe")

    def test_a_name_that_already_carries_the_version_is_not_doubled(self):
        self.assertEqual(self.name_for(asset="ps3-tools-1.2.0.exe"),
                         "ps3-tools-1.2.0.exe")

    def test_a_release_with_no_tag_still_gets_a_name(self):
        self.assertEqual(self.name_for(tag=""), "ps3-tools.exe")

    def test_a_download_says_the_name_it_actually_used(self):
        release = update.Release({
            "tag_name": "v1.2.0",
            "assets": [{"name": "ps3-tools.exe", "size": 4,
                        "browser_download_url":
                            "https://github.com/setsid/ps3-tools/x.exe"}]})
        taken = update.free_path(self.folder, "ps3-tools.exe", "v1.2.0")
        open(taken, "wb").close()
        result = update.download(release, fetcher=lambda url, timeout=None:
                                 b"data", folder=self.folder)
        self.assertTrue(result.ok)
        self.assertEqual(os.path.basename(result.path),
                         "ps3-tools-1.2.0 (2).exe")
        self.assertIn("ps3-tools-1.2.0 (2).exe", result.detail)


if __name__ == "__main__":
    unittest.main()
