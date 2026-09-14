"""Builds the diagnostics screen and drives it, against loopback and nothing.

Most of what goes wrong in a Qt screen goes wrong at construction or in the
wiring between a worker and the widgets, and none of that shows up in a unit
test of the layers underneath. So the screen is built for real, with a stub
theme so it does not depend on the shell's own, and driven.

One rule this module keeps, absolutely: every server it talks to is started by
the test and bound to 127.0.0.1. Nothing here scans, probes or names an address
that is not loopback. The screen itself no longer has any search of its own to
exercise.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import subprocess
import sys
import tempfile
import unittest
import ftplib

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QLabel, QPushButton

from support import FixtureCase, ROOT, fixture_path
from mock_webman import MockConsole
from ps3diag import runner
from ps3diag.analysis import Analysis
from ps3diag.collectors import CATEGORIES, CollectorResult
from ps3diag.findings import Finding
from ps3diag.transport import FtpLister, HttpProbe

from ps3tools.shell.screen import ConnectionState, Services, Theme, \
    THEME_TOKENS
from ps3tools.screens import diagnostics

# One QApplication for the module. A second one aborts the process, and one
# per test leaks the first.
APP = QApplication.instance() or QApplication([])

# A different colour per token, so a test can tell which token a widget was
# painted from. The real values are the shell's business.
COLOURS = {token: "#%06x" % (0x010203 + index * 0x111111)
           for index, token in enumerate(THEME_TOKENS)}


class StubTheme(Theme):
    """Any colour per token, and no dependency on the shell's theme landing."""

    def colour(self, token):
        return COLOURS[token]

    @property
    def dark(self):
        return False


class ScreenCase(FixtureCase):
    def setUp(self):
        self.connection = ConnectionState()
        self.theme = StubTheme()
        self.settings = {}
        self.services = Services(self.connection, self.theme, self.settings)
        self.screen = diagnostics.DiagnosticsScreen(self.services)
        self.addCleanup(self._teardown)

    def _teardown(self):
        if self.screen._run_task is not None:
            self.screen._run_task.cancel()
        self.services.wait(30000)
        self.screen.deleteLater()
        self.screen = None
        APP.processEvents()

    def pump(self, milliseconds=60000):
        """Lets the workers finish and their signals arrive on this thread."""
        self.services.wait(milliseconds)
        for _ in range(5):
            APP.processEvents()

    def findings_widgets(self):
        layout = self.screen.findings_layout
        return [layout.itemAt(index).widget()
                for index in range(layout.count())]

    def cards(self):
        return [widget for widget in self.findings_widgets()
                if isinstance(widget, diagnostics.FindingCard)]

    def findings_text(self):
        out = []
        for widget in self.findings_widgets():
            if isinstance(widget, QLabel):
                out.append(widget.text())
            else:
                out.extend(child.text()
                           for child in widget.findChildren(QLabel))
        return out


class Construction(ScreenCase):
    def test_it_registers_itself_with_the_published_attributes(self):
        self.assertEqual(diagnostics.DiagnosticsScreen.key, "diagnostics")
        self.assertEqual(diagnostics.DiagnosticsScreen.title, "Diagnostics")
        self.assertEqual(diagnostics.DiagnosticsScreen.tile, "DX")
        self.assertEqual(diagnostics.DiagnosticsScreen.order, 10)
        self.assertTrue(diagnostics.DiagnosticsScreen.blurb)

    def test_every_category_has_a_tick_box_and_they_all_start_ticked(self):
        self.assertEqual(sorted(self.screen.category_boxes),
                         sorted(key for key, _t, _c in CATEGORIES))
        for key, tick in self.screen.category_boxes.items():
            self.assertTrue(tick.isChecked(), key)

    def test_identifiers_are_off_by_default_and_the_warning_is_shown(self):
        self.assertFalse(self.screen.identifiers_box.isChecked())
        warnings = [label.text() for label in self.screen.findChildren(QLabel)
                    if "uniquely identify" in label.text()]
        self.assertTrue(warnings)
        self.assertIn("not be posted publicly", warnings[0])

    def test_every_category_has_its_own_progress_line(self):
        self.assertEqual(sorted(self.screen.progress_rows),
                         sorted(key for key, _t, _c in CATEGORIES))
        for label in self.screen.progress_rows.values():
            self.assertEqual(label.text(), "waiting")
        # Per category, and counted: not one indeterminate bar.
        self.assertEqual(self.screen.overall_progress.maximum(),
                         len(CATEGORIES))

    def test_the_buttons_start_in_the_right_state(self):
        self.assertTrue(self.screen.run_button.isEnabled())
        self.assertFalse(self.screen.stop_button.isEnabled())
        self.assertFalse(self.screen.show_button.isEnabled())

    def test_the_read_only_promise_is_on_the_screen(self):
        joined = " ".join(label.text()
                          for label in self.screen.findChildren(QLabel))
        self.assertIn("only reads", joined)
        self.assertIn("never writes", joined)

    def test_a_theme_change_repaints_in_the_colour_a_widget_is_now(self):
        # Not the one it was built with. A label that has turned red because a
        # category failed must not go quietly grey when the theme switches.
        label = self.screen.progress_rows["system"]
        self.screen._set_status(label, "not collected")
        self.assertIn(COLOURS["error"], label.styleSheet())
        self.theme.changed.emit()
        APP.processEvents()
        self.assertIn(COLOURS["error"], label.styleSheet())

    def test_no_colour_is_named_anywhere_but_through_a_token(self):
        # Every colour the screen paints has to have come from theme.colour,
        # so both themes stay legible without this screen being checked twice.
        for label in self.screen.findChildren(QLabel):
            sheet = label.styleSheet()
            for chunk in sheet.split():
                if chunk.startswith("#"):
                    self.assertIn(chunk.strip(";"), COLOURS.values(), sheet)


class RefusingToStart(ScreenCase):
    def test_a_run_with_no_address_is_refused_rather_than_started(self):
        self.connection.set_host("")
        self.screen._start()
        self.assertIsNone(self.screen._run_task)
        self.assertIn("address", self.screen.summary_label.text())

    def test_unticking_everything_is_refused(self):
        self.connection.set_host("127.0.0.1")
        for tick in self.screen.category_boxes.values():
            tick.setChecked(False)
        self.screen._start()
        self.assertIsNone(self.screen._run_task)
        self.assertIn("Tick at least one", self.screen.summary_label.text())


class ScanStateIsNotOnThisScreen(ScreenCase):
    """The bug the old window had, stated so it cannot come back.

    The old screen carried a Find button with the scan result beside it, and a
    scan that came back empty painted "No PS3 found on this network" in red
    under a populated address while a collection from that address succeeded.
    The search now lives in the top bar, so the fix asserted here is the strong
    one: nothing on this screen renders scan state, in any colour, ever.
    """

    def setUp(self):
        super().setUp()
        self.connection.set_host("192.168.1.50")
        self.connection.set_connection("connected", "Reached it.")

    def widget_text(self):
        return [label.text()
                for label in self.screen.findChildren(QLabel)]

    def test_the_screen_owns_no_search_control_at_all(self):
        self.assertFalse(hasattr(self.screen, "find_button"))
        self.assertFalse(hasattr(self.screen, "scan_label"))
        self.assertFalse(hasattr(self.screen, "_scan_task"))
        for name in ("_find", "_refresh_scan", "_scan_finished",
                     "_scan_progress", "_scan_failed", "_scan_done",
                     "_choose"):
            self.assertFalse(hasattr(self.screen, name), name)
        self.assertFalse(hasattr(diagnostics, "CandidateDialog"))
        buttons = [button.text()
                   for button in self.screen.findChildren(QPushButton)]
        self.assertNotIn("Find my PS3", buttons)
        for text in self.widget_text():
            self.assertNotIn("Find my PS3", text)

    def test_no_widget_renders_scan_state_however_the_scan_ended(self):
        for state, detail in (("scanning", "Looking for the PS3 on ..."),
                              ("none", "No PS3 found on this network."),
                              ("failed", "The search could not run."),
                              ("found", "Found one at 192.168.1.77.")):
            with self.subTest(state=state):
                self.connection.set_scan(state, detail)
                APP.processEvents()
                for text in self.widget_text():
                    self.assertNotIn(detail, text)
                    self.assertNotIn("No PS3 found", text)
                # And the connection verdict the top bar shows is untouched.
                self.assertEqual(self.connection.connection, "connected")
                self.assertEqual(self.connection.connection_detail,
                                 "Reached it.")

    def test_a_scan_that_found_nothing_does_not_reach_the_summary(self):
        before = self.screen.summary_label.text()
        self.connection.set_scan("none", "No PS3 found on this network.")
        APP.processEvents()
        self.assertEqual(self.screen.summary_label.text(), before)

    def test_the_screen_never_reads_the_scan_side_of_the_connection(self):
        # A property that raises if anything on this screen so much as looks
        # at it: the separation is a rule about reads, not only about paint.
        def forbidden(_self):
            raise AssertionError("the diagnostics screen read connection.scan")

        original_scan = type(self.connection).scan
        original_detail = type(self.connection).scan_detail
        type(self.connection).scan = property(forbidden)
        type(self.connection).scan_detail = property(forbidden)
        try:
            self.connection.set_host("192.168.1.51")
            self.screen.on_enter()
            self.theme.changed.emit()
            APP.processEvents()
        finally:
            type(self.connection).scan = original_scan
            type(self.connection).scan_detail = original_detail

    def test_a_run_that_reached_nothing_says_so_through_the_connection(self):
        result = runner.RunResult("192.168.1.50", ["system"])
        outcome = CollectorResult("system", "System and firmware")
        outcome.status = "failed"
        outcome.error = "Nothing answered."
        result.results.append(outcome)
        self.screen._finished("/nowhere/saved.zip", None, Analysis(), result)
        self.assertEqual(self.connection.connection, "unreachable")
        self.assertIn("Could not reach", self.connection.connection_detail)
        self.assertIn("Could not reach", self.screen.summary_label.text())
        # The verdict is the collection's, and the scan state is left alone.
        self.assertEqual(self.connection.scan, "idle")


class Roominess(ScreenCase):
    """Nothing overlaps or clips at the smallest window the shell allows.

    Checked as geometry rather than by eye, because the way this went wrong on
    a real console was a label one pixel narrower than its own text.
    """

    def lay_out(self, width, height):
        self.screen.resize(width, height)
        self.screen.show()
        APP.processEvents()
        self.screen.layout().activate()
        APP.processEvents()

    def test_no_category_tick_box_is_narrower_than_its_own_label(self):
        # 760 is roughly what the screen gets inside the shell's 940 minimum
        # once the sidebar has taken its share.
        self.lay_out(760, 600)
        for key, tick in self.screen.category_boxes.items():
            self.assertGreaterEqual(tick.width(), tick.sizeHint().width(), key)
            self.assertGreaterEqual(tick.height(), tick.sizeHint().height(),
                                    key)

    def test_category_tick_boxes_do_not_overlap_each_other(self):
        self.lay_out(760, 600)
        boxes = list(self.screen.category_boxes.values())
        rects = [(tick, tick.mapTo(self.screen, tick.rect().topLeft()))
                 for tick in boxes]
        for index, (tick, origin) in enumerate(rects):
            for other, other_origin in rects[index + 1:]:
                a = (origin.x(), origin.y(), tick.width(), tick.height())
                b = (other_origin.x(), other_origin.y(),
                     other.width(), other.height())
                overlaps = (a[0] < b[0] + b[2] and b[0] < a[0] + a[2]
                            and a[1] < b[1] + b[3] and b[1] < a[1] + a[3])
                self.assertFalse(overlaps,
                                 f"{tick.text()} overlaps {other.text()}")

    def test_a_screen_too_tall_for_the_window_scrolls_instead_of_clipping(self):
        # The shell places screens directly, so overflow has to be handled
        # here or the bottom of the results is simply not drawn.
        self.lay_out(760, 500)
        body = self.screen.scroller.widget()
        # The content keeps its own full height, and the viewport is shorter:
        # that difference is exactly what used to be clipped away.
        self.assertGreaterEqual(body.height(),
                                body.minimumSizeHint().height())
        self.assertGreater(body.height(),
                           self.screen.scroller.viewport().height())
        self.assertNotEqual(self.screen.scroller.verticalScrollBarPolicy(),
                            Qt.ScrollBarAlwaysOff)

    def test_progress_rows_have_room_for_their_descenders(self):
        self.lay_out(760, 600)
        for key, label in self.screen.progress_rows.items():
            self.assertGreaterEqual(label.height(),
                                    label.fontMetrics().height(), key)


class FindingsRendering(ScreenCase):
    def render(self, findings, broken=()):
        outcome = Analysis()
        outcome.findings = list(findings)
        outcome.broken_rules = list(broken)
        self.screen._render_findings(outcome)
        APP.processEvents()
        return outcome

    def test_explanation_and_fix_are_both_visible_without_expanding(self):
        self.render([Finding("a", "error", "The title", "The explanation.",
                             "The fix.", ["evidence line"], "storage")])
        card = self.cards()[0]
        self.assertEqual(card.title_label.text(), "The title")
        self.assertEqual(card.explanation_label.text(), "The explanation.")
        self.assertFalse(card.explanation_label.isHidden())
        self.assertEqual(card.fix_label.text(), "What to do: The fix.")
        self.assertFalse(card.fix_label.isHidden())

    def test_evidence_is_available_but_subordinate(self):
        self.render([Finding("a", "error", "T", "E", "F", ["a line"])])
        card = self.cards()[0]
        self.assertTrue(card.evidence_label.isHidden())
        card.evidence_button.setChecked(True)
        self.assertFalse(card.evidence_label.isHidden())
        self.assertEqual(card.evidence_label.text(), "a line")

    def test_findings_are_grouped_under_a_coloured_severity_heading(self):
        self.render([Finding("a", "error", "Broken", "x"),
                     Finding("b", "warn", "Odd", "y"),
                     Finding("c", "info", "Noted", "z")])
        headings = {widget.text(): widget.styleSheet()
                    for widget in self.findings_widgets()
                    if isinstance(widget, QLabel)}
        self.assertIn("PROBLEM (1)", headings)
        self.assertIn("WORTH CHECKING (1)", headings)
        self.assertIn("INFORMATION (1)", headings)
        self.assertIn(COLOURS["error"], headings["PROBLEM (1)"])
        self.assertIn(COLOURS["warn"], headings["WORTH CHECKING (1)"])
        self.assertIn(COLOURS["info"], headings["INFORMATION (1)"])

    def test_severity_can_be_filtered(self):
        self.render([Finding("a", "error", "Broken", "x"),
                     Finding("b", "info", "Noted", "z")])
        self.assertEqual(len(self.cards()), 2)
        self.screen.severity_filters["info"].setChecked(False)
        APP.processEvents()
        titles = [card.finding.title for card in self.cards()]
        self.assertEqual(titles, ["Broken"])

    def test_the_filters_carry_the_counts(self):
        self.render([Finding("a", "error", "Broken", "x"),
                     Finding("b", "error", "Also broken", "x")])
        self.assertEqual(self.screen.severity_filters["error"].text(),
                         "Problem (2)")
        self.assertEqual(self.screen.severity_filters["info"].text(),
                         "Information (0)")

    def test_no_findings_says_so_carefully(self):
        self.render([])
        joined = " ".join(self.findings_text())
        self.assertIn("Nothing stood out", joined)
        self.assertIn("not the same as proving", joined)

    def test_a_broken_check_is_admitted_to(self):
        self.render([], [{"rule_id": "x", "error": "boom"}])
        joined = " ".join(self.findings_text())
        self.assertIn("could not run", joined)


class OpeningASavedDiagnostic(ScreenCase):
    def test_a_saved_zip_is_loaded_and_analysed_with_no_console(self):
        path = fixture_path("sample-diagnostic.zip")
        self.screen._choose_file = lambda: path
        self.screen._open_saved()
        self.pump()
        self.assertEqual(self.screen.last_zip, path)
        self.assertIsNotNone(self.screen.artefacts)
        self.assertIsNotNone(self.screen.analysis)
        self.assertTrue(self.screen.show_button.isEnabled())
        self.assertIn("sample-diagnostic.zip",
                      self.screen.summary_label.text())
        # The progress column is repurposed to say what is in the file.
        self.assertEqual(self.screen.progress_rows["system"].text(),
                         "collected")
        self.assertTrue(self.screen.analysis.findings,
                        "the sample should produce findings to render")
        self.assertEqual(len(self.cards()),
                         len(self.screen.analysis.findings))
        # Nothing was asked of a console: there is no address at all.
        self.assertEqual(self.connection.host, "")
        self.assertEqual(self.connection.connection, "unknown")

    def test_cancelling_the_dialog_changes_nothing(self):
        self.screen._choose_file = lambda: ""
        self.screen._open_saved()
        self.pump()
        self.assertIsNone(self.screen.artefacts)

    def test_a_file_that_is_not_a_zip_is_refused_politely(self):
        self.screen._choose_file = lambda: fixture_path("text",
                                                        "boot_plugins.txt")
        self.screen._open_saved()
        self.pump()
        self.assertIn("could not be opened",
                      self.screen.summary_label.text())
        self.assertTrue(self.screen.open_button.isEnabled())


class AFullRun(ScreenCase):
    """The whole screen, against the mock console on loopback."""

    def setUp(self):
        super().setUp()
        self.console = MockConsole().start()
        self.addCleanup(self.console.stop)
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.settings["output_dir"] = self.folder.name
        self.settings["run_timeout"] = 120
        self.screen.transport_factory = self._transport
        self.connection.set_host("127.0.0.1")

    def _transport(self, _host):
        port = self.console.ftp.port

        def factory():
            ftp = ftplib.FTP()
            ftp.connect("127.0.0.1", port, timeout=5)
            ftp.login("anonymous", "ps3-diag@localhost")
            return ftp

        return (HttpProbe(f"127.0.0.1:{self.console.http.port}", timeout=5),
                FtpLister("127.0.0.1", 5, factory=factory))

    def test_a_run_produces_a_zip_and_renders_what_it_found(self):
        busy = []
        self.screen.busy_changed.connect(busy.append)
        self.screen._start()
        self.assertIsNotNone(self.screen._run_task)
        self.pump()

        self.assertTrue(self.screen.last_zip)
        self.assertTrue(os.path.exists(self.screen.last_zip))
        self.assertTrue(self.screen.last_zip.endswith(".zip"))
        self.assertIn("Saved to", self.screen.summary_label.text())
        self.assertEqual(busy, [True, False])

        # Per category, and every one of them got there.
        self.assertEqual(self.screen.progress_rows["system"].text(),
                         "collected")
        self.assertEqual(self.screen.overall_progress.value(),
                         len(CATEGORIES))

        self.assertIsNotNone(self.screen.analysis)
        self.assertTrue(self.screen.analysis.findings)
        self.assertEqual(len(self.cards()),
                         len(self.screen.analysis.findings))

        # The buttons are back, and the run said something about reachability
        # through the connection rather than through the scan.
        self.assertTrue(self.screen.run_button.isEnabled())
        self.assertFalse(self.screen.stop_button.isEnabled())
        self.assertTrue(self.screen.show_button.isEnabled())
        self.assertEqual(self.connection.connection, "connected")
        self.assertEqual(self.connection.scan, "idle")

    def test_the_run_button_is_disabled_while_the_run_is_in_flight(self):
        self.screen._start()
        self.assertFalse(self.screen.run_button.isEnabled())
        self.assertFalse(self.screen.open_button.isEnabled())
        self.assertTrue(self.screen.stop_button.isEnabled())
        self.assertFalse(self.screen.can_leave())
        self.pump()
        self.assertTrue(self.screen.can_leave())

    def test_the_log_fills_with_what_is_being_read(self):
        self.screen._start()
        self.pump()
        text = self.screen.detail_log.toPlainText()
        self.assertIn("Listing /", text)
        self.assertIn("Game inventory", text)
        self.assertGreater(len(text.splitlines()), len(CATEGORIES))
        self.assertIn("Finished", self.screen.activity_label.text())

    def test_the_slow_pass_is_not_run_unless_the_box_is_ticked(self):
        seen = self.capture_options()
        self.screen._start()
        self.pump()
        self.assertEqual(seen, [{"identify_isos": False}])
        opened = [command for command in self.console.ftp.commands
                  if command.upper().startswith("RETR")
                  and command.lower().endswith(".iso")]
        self.assertEqual(opened, [])

    def test_ticking_it_asks_the_runner_for_it(self):
        seen = self.capture_options()
        self.screen.identify_box.setChecked(True)
        self.screen._start()
        self.pump()
        self.assertEqual(seen, [{"identify_isos": True}])

    def capture_options(self):
        """What the screen hands runner.run as its options, per call."""
        seen = []
        real = diagnostics.runner.run

        def capture(*args, **kwargs):
            seen.append(dict(kwargs.get("options") or {}))
            return real(*args, **kwargs)

        diagnostics.runner.run = capture
        self.addCleanup(setattr, diagnostics.runner, "run", real)
        return seen

    def test_stopping_still_saves_what_was_collected(self):
        self.screen._start()
        self.screen._stop()
        self.pump()
        # A cancelled Task never emits finished, so a screen that waited on it
        # would throw away the zip the run had already written.
        self.assertTrue(self.screen.last_zip)
        self.assertTrue(os.path.exists(self.screen.last_zip))
        self.assertIsNone(self.screen._run_task)
        self.assertTrue(self.screen.run_button.isEnabled())


class TheSlowPassIsASubOption(ScreenCase):
    """Reading inside every disc image is opt-in, and reads as subordinate.

    On a real console with 24 images this pass was the whole of the two minutes
    the inventory took, while the names and sizes were already in hand in
    seconds. It is a thing the game inventory can be asked to additionally do,
    not an eighth category, and it is off until somebody asks.
    """

    def test_it_is_not_one_of_the_categories(self):
        self.assertNotIn("identify_isos", self.screen.category_boxes)
        self.assertNotIn(self.screen.identify_box,
                         self.screen.category_boxes.values())

    def test_it_starts_switched_off(self):
        self.assertFalse(self.screen.identify_box.isChecked())

    def test_it_says_plainly_what_it_does_and_what_it_costs(self):
        joined = (self.screen.identify_box.text() + " "
                  + self.screen.identify_hint.text()).lower()
        self.assertIn("inside every disc image", joined)
        self.assertIn("slow", joined)
        for expected in ("minute", "off by default", "names, sizes"):
            self.assertIn(expected, joined)

    def test_it_sits_under_the_game_inventory_rather_than_beside_it(self):
        self.screen.resize(760, 700)
        self.screen.show()
        APP.processEvents()
        self.screen.layout().activate()
        APP.processEvents()
        games = self.screen.category_boxes["games"]
        games_at = games.mapTo(self.screen, games.rect().topLeft())
        mine = self.screen.identify_box.mapTo(
            self.screen, self.screen.identify_box.rect().topLeft())
        self.assertGreater(mine.y(), games_at.y())
        self.assertGreater(mine.x(), games_at.x())

    def test_it_is_greyed_out_when_the_inventory_is_not_being_collected(self):
        self.screen.category_boxes["games"].setChecked(False)
        APP.processEvents()
        self.assertFalse(self.screen.identify_box.isEnabled())
        self.screen.category_boxes["games"].setChecked(True)
        APP.processEvents()
        self.assertTrue(self.screen.identify_box.isEnabled())

    def test_it_is_remembered_like_the_other_tick_boxes(self):
        self.screen.identify_box.setChecked(True)
        self.assertTrue(self.settings["identify_isos"])
        self.screen.identify_box.setChecked(False)
        self.assertFalse(self.settings["identify_isos"])

    def test_it_comes_back_from_the_settings(self):
        self.settings["identify_isos"] = True
        screen = diagnostics.DiagnosticsScreen(self.services)
        self.addCleanup(screen.deleteLater)
        self.assertTrue(screen.identify_box.isChecked())


class TheRunningCommentary(ScreenCase):
    """What is happening now, big enough to read, and a list of what happened.

    The complaint this answers is exact: one row reading "waiting" for two
    minutes. The per-category column stays, because it is the summary, but it
    cannot be the only thing that moves.
    """

    def test_it_starts_idle_and_empty(self):
        self.assertIn("Not collecting", self.screen.activity_label.text())
        self.assertEqual(self.screen.detail_log.toPlainText(), "")

    def test_a_detail_goes_to_the_big_line_and_to_the_log(self):
        self.screen._run_progress(
            ("detail", "games", "Listing /dev_hdd0/PS3ISO/"))
        APP.processEvents()
        self.assertIn("Listing /dev_hdd0/PS3ISO/",
                      self.screen.activity_label.text())
        self.assertIn("Game inventory", self.screen.activity_label.text())
        self.assertIn("Listing /dev_hdd0/PS3ISO/",
                      self.screen.detail_log.toPlainText())

    def test_the_log_keeps_the_ones_before_it(self):
        for index in range(1, 6):
            self.screen._run_progress(
                ("detail", "games", f"Reading inside image {index} of 24"))
        APP.processEvents()
        text = self.screen.detail_log.toPlainText()
        for index in range(1, 6):
            self.assertIn(f"image {index} of 24", text)
        self.assertEqual(len(text.strip().splitlines()), 5)

    def test_the_per_category_column_still_works(self):
        self.screen._run_progress(("category", "games", "running", None))
        self.assertEqual(self.screen.progress_rows["games"].text(),
                         "collecting")
        outcome = CollectorResult("games", "Game inventory")
        outcome.status = "ok"
        self.screen._run_progress(("category", "games", "done", outcome))
        self.assertEqual(self.screen.progress_rows["games"].text(),
                         "collected")
        self.assertIn("Game inventory - collected",
                      self.screen.detail_log.toPlainText())

    def test_the_activity_line_is_far_more_prominent_than_a_status_row(self):
        self.screen.resize(760, 700)
        self.screen.show()
        APP.processEvents()
        self.screen._run_progress(
            ("detail", "games", "Reading inside image 9 of 24"))
        self.screen.layout().activate()
        APP.processEvents()
        activity = self.screen.activity_label
        row = self.screen.progress_rows["games"]
        self.assertFalse(activity.isHidden())
        self.assertGreater(activity.fontMetrics().height(),
                           row.fontMetrics().height())
        self.assertIn("font-weight: 600", activity.styleSheet())
        # And the log beside it is a panel rather than a line.
        self.assertGreaterEqual(self.screen.detail_log.height(), 120)
        self.assertFalse(self.screen.detail_log.isHidden())

    def test_a_theme_change_repaints_the_log_panel_too(self):
        self.theme.changed.emit()
        APP.processEvents()
        sheet = self.screen.detail_log.styleSheet()
        self.assertIn(COLOURS["surface_alt"], sheet)
        self.assertIn(COLOURS["text"], sheet)

    def test_starting_a_run_clears_the_last_one(self):
        self.screen._run_progress(("detail", "games", "something old"))
        self.connection.set_host("127.0.0.1")
        self.screen.transport_factory = lambda host: (None, None)
        self.screen._start()
        self.assertNotIn("something old",
                         self.screen.detail_log.toPlainText())
        self.screen._run_task.cancel()
        self.pump()


class Layering(unittest.TestCase):
    def test_importing_the_screen_loads_no_write_client(self):
        # In a clean interpreter, so a sibling screen's tests importing the
        # patcher in the same process cannot make this pass by accident.
        script = (
            "import sys\n"
            "from ps3tools.screens import diagnostics\n"
            "print('LOADED:' + ','.join(sorted(\n"
            "    name for name in sys.modules\n"
            "    if name.startswith('ps3tools.patching'))))\n"
        )
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        proc = subprocess.run([sys.executable, "-c", script], cwd=ROOT,
                              capture_output=True, text=True, timeout=120,
                              env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("LOADED:", proc.stdout)
        self.assertEqual([line for line in proc.stdout.splitlines()
                          if line.startswith("LOADED:")], ["LOADED:"])

    def test_the_two_dead_endpoints_are_not_mentioned(self):
        # /sysinfo.ps3 and /info.ps3 answer 501 on a real console and have been
        # taken out of the collectors. Nothing here may still ask for them.
        with open(os.path.join(ROOT, "ps3tools", "screens",
                               "diagnostics.py"), encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("sysinfo.ps3", source)
        self.assertNotIn("info.ps3", source)


if __name__ == "__main__":
    unittest.main()
