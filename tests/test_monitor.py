"""The Monitor tool: the arithmetic, the graph, and the screen that polls.

Three layers, tested at three levels, because the way a graph goes wrong is
that every layer passes its own test and the picture is still empty.

  * ps3tools/telemetry.py is arithmetic and no Qt. Windows, gaps, axis bounds
    and the CSV are checked here with plain numbers.
  * ps3tools/shell/chart.py is painted. It is checked by rendering it to an
    image and looking for the line, because a widget whose paintEvent is never
    reached passes every test that only asks it questions.
  * ps3tools/screens/monitor.py polls. It is built for real and driven with a
    sampler that talks to nothing, and the tests below insist that the real
    path calls the graph rather than that the graph works when called by hand.

Nothing here opens a socket. The screen's one seam is its `sampler`, and a
test that forgot to replace it would fail on a refusing stub rather than go
looking for a console.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import time
import unittest

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

from support import FixtureCase
from ps3tools import telemetry
from ps3tools.shell.chart import Chart, TitleBand, plot
from ps3tools.shell.screen import ConnectionState, Services, Theme, \
    THEME_TOKENS
from ps3tools.screens import monitor

APP = QApplication.instance() or QApplication([])

#: One flat, findable colour per token, so a test can say which token a pixel
#: was painted from. The real values are the shell's business.
COLOURS = {token: "#%06x" % (0x0000ff + index * 0x001100)
           for index, token in enumerate(THEME_TOKENS)}
COLOURS["accent"] = "#ff0000"
COLOURS["info"] = "#00ff00"
COLOURS["warn"] = "#0000ff"
COLOURS["ok"] = "#ff00ff"
COLOURS["text"] = "#222222"
COLOURS["text_dim"] = "#888888"
COLOURS["border"] = "#cccccc"


class StubTheme(Theme):
    def colour(self, token):
        return COLOURS[token]

    @property
    def dark(self):
        return False


#: What webMAN means by "572.9 GB free", in bytes. Its pages use 1024 as the
#: thousand and so does every size this program prints.
FREE_BYTES = 615_146_690_969


def facts(cpu=60.0, rsx=62.0, fan=41, free=FREE_BYTES, **extra):
    out = {"cpu_temp_c": cpu, "rsx_temp_c": rsx, "fan_speed_percent": fan,
           "hdd_free_bytes": free}
    out.update(extra)
    return out


def filled(history, count=20, start=None, step=5.0, **kwargs):
    """count readings, oldest first, step seconds apart."""
    start = time.time() - count * step if start is None else start
    for index in range(count):
        history.append(facts(cpu=55.0 + index, **kwargs),
                       at=start + index * step)
    return history


class TheFigures(unittest.TestCase):
    """What comes out of a facts dictionary, and how it is written."""

    def test_every_graphed_figure_is_read_from_the_facts(self):
        history = telemetry.History()
        reading = history.append(facts())
        self.assertEqual(sorted(reading.values),
                         ["cpu", "fan", "free", "rsx"])
        self.assertEqual(reading.value("cpu"), 60.0)
        self.assertEqual(reading.value("fan"), 41.0)
        self.assertAlmostEqual(reading.value("free"), 572.9, places=1)

    def test_a_figure_the_console_did_not_report_is_absent_rather_than_zero(self):
        series = telemetry.series_for("fan")
        self.assertIsNone(telemetry.value_of({}, series))
        self.assertIsNone(telemetry.value_of({"fan_speed_percent": None},
                                             series))
        self.assertEqual(telemetry.value_of({"fan_speed_percent": 0}, series),
                         0.0)

    def test_free_space_falls_back_to_the_mount_listing(self):
        """webMAN's sentence is not on every build; the listing is."""
        series = telemetry.series_for("free")
        devices = [{"device": "dev_hdd0", "free_bytes": FREE_BYTES}]
        self.assertAlmostEqual(
            telemetry.value_of({"devices": devices}, series), 572.9, places=3)

    def test_a_percentage_is_written_against_the_number(self):
        self.assertEqual(
            telemetry.format_value(41, telemetry.series_for("fan")), "41%")
        self.assertEqual(
            telemetry.format_value(61, telemetry.series_for("cpu")), "61 °C")
        self.assertEqual(
            telemetry.format_value(61.5, telemetry.series_for("cpu")),
            "61.5 °C")
        self.assertEqual(
            telemetry.format_value(None, telemetry.series_for("cpu")), "")


class TheHistory(unittest.TestCase):
    def test_a_console_that_said_nothing_is_a_miss_and_not_a_reading(self):
        history = telemetry.History()
        self.assertIsNone(history.append({}))
        self.assertEqual(len(history), 0)
        self.assertEqual(history.misses, 1)

    def test_a_page_with_none_of_the_figures_on_it_is_also_a_miss(self):
        """webMAN answered, and answered with nothing worth drawing."""
        history = telemetry.History()
        self.assertIsNone(history.append({"firmware_version": "4.92"}))
        self.assertEqual(len(history), 0)
        self.assertEqual(history.misses, 1)

    def test_a_partial_reading_is_kept_for_what_it_did_carry(self):
        history = telemetry.History()
        reading = history.append({"cpu_temp_c": 58.0})
        self.assertEqual(list(reading.values), ["cpu"])
        self.assertEqual(len(history), 1)

    def test_the_window_is_measured_from_the_newest_reading(self):
        """A console switched off ten minutes ago still shows its last hour."""
        history = telemetry.History()
        end = 10_000.0
        for index in range(10):
            history.append(facts(cpu=50.0 + index), at=end - (9 - index) * 60)
        points = history.points("cpu", span=300)
        self.assertEqual(len(points), 6)
        self.assertEqual(points[-1][0], end)

    def test_the_oldest_readings_are_dropped_at_the_cap(self):
        history = telemetry.History(limit=5)
        filled(history, count=12, start=0.0)
        self.assertEqual(len(history), 5)
        self.assertEqual(history.points("cpu")[0][1], 62.0)

    def test_the_extremes_come_from_the_window_on_the_graph(self):
        history = telemetry.History()
        for index in range(10):
            history.append(facts(cpu=50.0 + index), at=1000.0 + index * 10)
        self.assertEqual(history.extremes("cpu"), (50.0, 59.0))
        self.assertEqual(history.extremes("cpu", span=30), (56.0, 59.0))
        self.assertEqual(telemetry.History().extremes("cpu"), (None, None))

    def test_the_csv_has_a_row_per_reading_and_a_blank_for_a_missing_figure(self):
        history = telemetry.History()
        history.append({"cpu_temp_c": 61.0}, at=1_700_000_000.0)
        text = history.to_csv()
        lines = text.strip().split("\n")
        self.assertEqual(
            lines[0], "epoch,time,cpu (°C),rsx (°C),fan (%),free (GB),title")
        cells = lines[1].split(",")
        self.assertEqual(cells[0], "1700000000.000")
        self.assertEqual(cells[2], "61")
        self.assertEqual(cells[3:], ["", "", "", ""])


class TheAxis(unittest.TestCase):
    def test_a_flat_series_is_drawn_flat_rather_than_filling_the_graph(self):
        low, high = telemetry.bounds([61.0] * 5, floor=40.0, least_span=10.0)
        self.assertLess(low, 61.0)
        self.assertGreater(high, 61.0)
        self.assertGreaterEqual(high - low, 10.0)

    def test_a_hard_ceiling_is_never_exceeded(self):
        """A fan at 100% must not produce an axis that goes to 110."""
        low, high = telemetry.bounds([100.0, 40.0], floor=0.0, ceiling=100.0,
                                     least_span=100.0)
        self.assertEqual((low, high), (0.0, 100.0))

    def test_an_empty_series_still_has_an_axis(self):
        self.assertEqual(telemetry.bounds([], floor=0.0, ceiling=100.0),
                         (0.0, 100.0))

    def test_the_gridlines_are_round_numbers_inside_the_axis(self):
        marks = telemetry.ticks(53.8, 67.9)
        self.assertTrue(marks)
        for value in marks:
            self.assertGreaterEqual(value, 53.8)
            self.assertLessEqual(value, 67.9)
            self.assertEqual(value, round(value, 6))
        self.assertIn(60, marks)

    def test_the_time_axis_uses_steps_people_say(self):
        """A quarter of an hour labelled every 500 seconds is unreadable."""
        labels = [label for _back, label in telemetry.time_labels(900)]
        self.assertEqual(labels, ["now", "5m", "10m", "15m"])
        self.assertEqual(
            [label for _back, label in telemetry.time_labels(3600)],
            ["now", "15m", "30m", "45m", "1h"])

    def test_a_span_is_said_the_way_somebody_would_say_it(self):
        self.assertEqual(telemetry.duration_words(45), "45s")
        self.assertEqual(telemetry.duration_words(300), "5m")
        self.assertEqual(telemetry.duration_words(4800), "1h 20m")
        self.assertEqual(telemetry.duration_words(7200), "2h")

    def test_a_hole_in_the_readings_splits_the_line(self):
        points = [(0.0, 1.0), (5.0, 2.0), (60.0, 3.0), (65.0, 4.0)]
        split = telemetry.runs(points, gap=12.5)
        self.assertEqual(len(split), 2)
        self.assertEqual(len(split[0]), 2)
        self.assertEqual(len(split[1]), 2)

    def test_one_reading_between_two_silences_is_kept(self):
        split = telemetry.runs([(0.0, 1.0), (600.0, 2.0)], gap=12.5)
        self.assertEqual([len(run) for run in split], [1, 1])


class TheEpisodes(unittest.TestCase):
    """Runs of readings above a level, which is what a threshold is about."""

    def test_a_run_above_the_level_is_one_episode_with_its_peak(self):
        points = [(0.0, 60.0), (5.0, 82.0), (10.0, 85.0), (15.0, 81.0),
                  (20.0, 70.0)]
        self.assertEqual(telemetry.episodes(points, 80.0, 12.5),
                         ((5.0, 15.0, 85.0),))

    def test_dropping_below_and_climbing_back_is_two_episodes(self):
        points = [(0.0, 85.0), (5.0, 60.0), (10.0, 90.0)]
        self.assertEqual(len(telemetry.episodes(points, 80.0, 12.5)), 2)

    def test_a_break_in_the_readings_ends_an_episode(self):
        """Otherwise a console silent for three minutes is reported as having
        been above eighty for all of them."""
        points = [(0.0, 85.0), (5.0, 86.0), (600.0, 87.0)]
        found = telemetry.episodes(points, 80.0, 12.5)
        self.assertEqual(len(found), 2)
        self.assertEqual(found[0], (0.0, 5.0, 86.0))

    def test_nothing_above_the_level_is_no_episodes(self):
        points = [(0.0, 60.0), (5.0, 61.0)]
        self.assertEqual(telemetry.episodes(points, 80.0, 12.5), ())

    def test_the_two_temperatures_carry_the_strips_own_levels(self):
        for key in ("cpu", "rsx"):
            self.assertEqual([value for value, _token
                              in telemetry.series_for(key).levels],
                             [70.0, 80.0])
        self.assertEqual(telemetry.series_for("free").levels, ())


class TheTitleSpans(unittest.TestCase):
    def history_of(self, rows):
        history = telemetry.History()
        for at, title in rows:
            history.append(facts(running_title=title), at=at)
        return history

    def test_consecutive_readings_of_one_game_are_one_span(self):
        history = self.history_of([(0.0, "BLES01031"), (5.0, "BLES01031"),
                                   (10.0, "BLES01031")])
        self.assertEqual(telemetry.title_spans(history.readings, 12.5),
                         ((0.0, 10.0, "BLES01031"),))

    def test_nothing_loaded_ends_the_span(self):
        history = self.history_of([(0.0, "BLES01031"), (5.0, ""),
                                   (10.0, "BLES01717")])
        spans = telemetry.title_spans(history.readings, 12.5)
        self.assertEqual([title for _at, _to, title in spans],
                         ["BLES01031", "BLES01717"])

    def test_a_break_splits_a_span_rather_than_spanning_it(self):
        history = self.history_of([(0.0, "BLES01031"), (600.0, "BLES01031")])
        self.assertEqual(len(telemetry.title_spans(history.readings, 12.5)), 2)

    def test_a_reading_carries_what_was_loaded(self):
        history = self.history_of([(0.0, "BLES01031")])
        self.assertEqual(history.readings[0].title, "BLES01031")

    def test_a_game_this_program_knows_is_named_and_others_are_not(self):
        self.assertIn("Black Ops 1", telemetry.title_words("BLES01031"))
        self.assertIn("BLES01031", telemetry.title_words("BLES01031"))
        self.assertEqual(telemetry.title_words("BLES99999"), "BLES99999")
        self.assertEqual(telemetry.title_words(""), "")


class TheReadingsFile(unittest.TestCase):
    """What Save writes, read back."""

    def round_trip(self, history):
        return telemetry.History.from_csv(history.to_csv(), path="x.csv")

    def test_every_reading_survives_the_round_trip(self):
        history = telemetry.History()
        for index in range(5):
            history.append(facts(cpu=60.0 + index,
                                 running_title="BLES01031"),
                           at=1000.0 + index * 5)
        back = self.round_trip(history)
        self.assertEqual(len(back), len(history))
        for first, second in zip(history.readings, back.readings):
            self.assertAlmostEqual(first.at, second.at, places=3)
            self.assertEqual(first.title, second.title)
            for key, value in first.values.items():
                self.assertAlmostEqual(value, second.values[key], places=3)

    def test_a_missing_figure_stays_missing_rather_than_becoming_zero(self):
        history = telemetry.History()
        history.append({"cpu_temp_c": 61.0}, at=1000.0)
        back = self.round_trip(history)
        self.assertEqual(list(back.readings[0].values), ["cpu"])

    def test_the_columns_are_read_by_name_and_not_by_position(self):
        """A file whose columns are in another order must not graph the fan
        as a temperature."""
        text = ("time,fan (%),epoch,cpu (°C)\n"
                "2023-11-14 22:13:20,44,1700000000,61\n")
        back = telemetry.History.from_csv(text)
        self.assertEqual(back.readings[0].values, {"cpu": 61.0, "fan": 44.0})
        self.assertEqual(back.readings[0].at, 1700000000.0)

    def test_a_file_without_the_title_column_still_loads(self):
        text = ("epoch,cpu (°C)\n1700000000,61\n")
        back = telemetry.History.from_csv(text)
        self.assertEqual(back.readings[0].title, "")

    def test_the_rows_are_put_back_in_order(self):
        text = "epoch,cpu (°C)\n20,62\n10,61\n"
        back = telemetry.History.from_csv(text)
        self.assertEqual([item.at for item in back.readings], [10.0, 20.0])

    def test_a_file_that_is_not_readings_says_what_was_expected(self):
        for text, wanted in (
                ("", "a header row"),
                ("nonsense\n1\n", "a column called epoch"),
                ("epoch,time\n1700000000,x\n", "cpu"),
                ("epoch,cpu (°C)\nnot-a-time,61\n", "seconds since 1970"),
                ("epoch,cpu (°C)\n1700000000,warm\n", "a number"),
                ("epoch,cpu (°C)\n", "at least one row")):
            with self.subTest(text=text[:20]):
                with self.assertRaises(telemetry.ReadingsFileError) as caught:
                    telemetry.History.from_csv(text, path="readings.csv")
                message = str(caught.exception)
                self.assertIn(wanted, message)
                self.assertIn("readings.csv", message)

    def test_a_row_naming_the_field_that_is_wrong(self):
        with self.assertRaises(telemetry.ReadingsFileError) as caught:
            telemetry.History.from_csv(
                "epoch,cpu (°C)\n1700000000,61\n1700000005,warm\n")
        self.assertIn("row 3", str(caught.exception))


class TheChartArithmetic(unittest.TestCase):
    def setUp(self):
        self.chart = Chart(("cpu", "rsx"))
        self.chart.set_theme(StubTheme())
        self.history = telemetry.History()
        self.chart.set_history(self.history)

    def test_the_newest_reading_is_at_the_right_hand_edge(self):
        rect = QRectF(0, 0, 100, 50)
        pixels = plot([(0.0, 50.0), (10.0, 60.0)], rect, 0.0, 10.0, 50.0, 60.0)
        self.assertEqual(pixels[0], QPointF(0, 50))
        self.assertEqual(pixels[-1], QPointF(100, 0))

    def test_both_temperatures_share_one_axis(self):
        self.history.append(facts(cpu=55.0, rsx=80.0), at=1000.0)
        self.history.append(facts(cpu=56.0, rsx=81.0), at=1005.0)
        low, high = self.chart.axis()
        self.assertLessEqual(low, 55.0)
        self.assertGreaterEqual(high, 81.0)

    def test_the_window_ends_at_the_newest_reading_rather_than_the_clock(self):
        self.history.append(facts(), at=1000.0)
        self.history.append(facts(), at=1060.0)
        self.chart.set_span(300)
        start, end = self.chart.window()
        self.assertEqual(end, 1060.0)
        self.assertEqual(start, 760.0)

    def test_a_single_reading_is_given_room_either_side(self):
        self.history.append(facts(), at=1000.0)
        self.chart.set_span(None)
        start, end = self.chart.window()
        self.assertLess(start, 1000.0)
        self.assertGreater(end, start)

    def test_the_summary_quotes_the_window_it_drew(self):
        for index in range(10):
            self.history.append(facts(cpu=50.0 + index), at=1000.0 + index * 5)
        self.chart.set_span(None)
        self.assertEqual(self.chart.summary("cpu"),
                         "59 °C   low 50 °C   high 59 °C")

    def test_the_reading_under_the_pointer_is_the_nearest_one(self):
        self.history.append(facts(cpu=50.0), at=1000.0)
        self.history.append(facts(cpu=70.0), at=1010.0)
        self.assertEqual(self.chart.reading_near(1009.0).value("cpu"), 70.0)
        self.assertEqual(self.chart.reading_near(1001.0).value("cpu"), 50.0)


class TheChartPaints(unittest.TestCase):
    """Rendered and then read back, because a paintEvent nobody reaches
    passes every other kind of test."""

    def render(self, chart, width=400, height=200):
        chart.resize(width, height)
        image = QImage(width, height, QImage.Format_RGB32)
        image.fill(QColor("#ffffff"))
        chart.render(image)
        return image

    def counted(self, image, colour):
        wanted = QColor(colour).rgb()
        found = 0
        for y in range(image.height()):
            for x in range(image.width()):
                if image.pixel(x, y) == wanted:
                    found += 1
        return found

    def chart_with(self, keys=("cpu",), count=20, gap_after=None):
        chart = Chart(keys)
        chart.set_theme(StubTheme())
        history = telemetry.History()
        at = 1000.0
        for index in range(count):
            history.append(facts(cpu=50.0 + index, rsx=60.0 + index), at=at)
            at += 300.0 if gap_after == index else 5.0
        chart.set_history(history)
        chart.set_span(None)
        chart.set_interval(5)
        return chart

    def test_it_draws_the_line_in_the_series_colour(self):
        chart = self.chart_with(("cpu",))
        image = self.render(chart)
        self.assertGreater(self.counted(image, COLOURS["accent"]), 50)

    def test_each_series_is_drawn_in_its_own_colour(self):
        chart = self.chart_with(("cpu", "rsx"))
        image = self.render(chart)
        self.assertGreater(self.counted(image, COLOURS["accent"]), 50)
        self.assertGreater(self.counted(image, COLOURS["info"]), 50)

    def test_nothing_recorded_says_so_rather_than_drawing_axes(self):
        chart = Chart(("cpu",))
        chart.set_theme(StubTheme())
        chart.set_history(telemetry.History())
        image = self.render(chart)
        self.assertEqual(self.counted(image, COLOURS["accent"]), 0)
        self.assertGreater(self.counted(image, COLOURS["text_dim"]), 0)
        self.assertFalse(chart.has_anything())

    def test_a_gap_in_the_readings_leaves_a_gap_in_the_line(self):
        """The count of painted columns drops, because nothing is drawn
        across a minute the console said nothing about."""
        whole = self.chart_with(("cpu",), count=20)
        broken = self.chart_with(("cpu",), count=20, gap_after=9)
        image = self.render(broken)
        empty = [x for x in range(400)
                 if not any(image.pixel(x, y) == QColor(COLOURS["accent"]).rgb()
                            for y in range(200))]
        self.assertTrue(empty, "the broken line filled every column")
        self.assertGreater(self.counted(self.render(whole), COLOURS["accent"]),
                           0)


class ScreenCase(FixtureCase):
    def setUp(self):
        self.connection = ConnectionState()
        self.theme = StubTheme()
        self.services = Services(self.connection, self.theme, {})
        self.screen = monitor.MonitorScreen(self.services)
        self.reads = []
        self.answers = []
        self.screen.sampler = self.sampler
        self.addCleanup(self._teardown)

    def sampler(self, host, probe_factory=None, cancelled=lambda: False,
                storage=False):
        self.reads.append((host, storage))
        if self.answers:
            return self.answers.pop(0)
        return facts()

    def _teardown(self):
        self.screen.stop()
        self.services.cancel_all()
        self.services.wait(30000)
        self.screen.deleteLater()
        self.screen = None
        APP.processEvents()

    def pump(self, milliseconds=30000):
        self.services.wait(milliseconds)
        for _ in range(5):
            APP.processEvents()

    def connect(self, host="127.0.0.1"):
        self.connection.set_host(host)
        self.connection.set_connection("connected")


class TheScreen(ScreenCase):
    def test_it_registers_itself_with_the_published_attributes(self):
        self.assertEqual(monitor.MonitorScreen.key, "monitor")
        self.assertEqual(monitor.MonitorScreen.title, "Monitor")
        self.assertEqual(monitor.MonitorScreen.tile, "MO")
        self.assertTrue(monitor.MonitorScreen.blurb)

    def test_it_is_on_the_launcher(self):
        from ps3tools.shell import registry
        self.assertIn(monitor.MonitorScreen, registry.screens())

    def test_it_has_a_graph_for_every_declared_set_of_series(self):
        self.assertEqual(sorted(self.screen.charts),
                         sorted(key for key, _t, _k in telemetry.GRAPHS))
        for key, _title, keys in telemetry.GRAPHS:
            self.assertEqual(self.screen.charts[key].keys, tuple(keys))

    def test_it_reads_nothing_until_it_is_opened(self):
        self.connect()
        self.pump()
        self.assertEqual(self.reads, [])
        self.assertFalse(self.screen.running)

    def test_an_unconnected_console_is_asked_for_nothing_and_said_so(self):
        self.screen.on_enter()
        self.pump()
        self.assertEqual(self.reads, [])
        self.assertFalse(self.screen.running)
        self.assertEqual(self.screen.status_words(), monitor.NOT_CONNECTED)

    def test_opening_it_on_a_connected_console_reads_at_once(self):
        """A screen that sat empty for the first interval reads as broken."""
        self.connect()
        self.screen.on_enter()
        self.pump()
        self.assertEqual(len(self.reads), 1)
        self.assertEqual(self.reads[0][0], "127.0.0.1")
        self.assertEqual(len(self.screen.history), 1)
        self.assertTrue(self.screen.running)

    def test_the_first_read_asks_for_the_free_space_and_later_ones_do_not(self):
        """The front page lists every game, so it is read rarely."""
        self.connect()
        self.screen.on_enter()
        self.pump()
        for _ in range(3):
            self.screen._tick()
            self.pump()
        self.assertEqual([storage for _host, storage in self.reads],
                         [True, False, False, False])

    def test_leaving_stops_the_polling(self):
        self.connect()
        self.screen.on_enter()
        self.pump()
        self.screen.on_leave()
        self.assertFalse(self.screen.running)
        self.assertFalse(self.screen._timer.isActive())
        before = len(self.reads)
        self.screen._tick()
        self.pump()
        self.assertEqual(len(self.reads), before)

    def test_the_timer_is_what_asks_for_the_next_reading(self):
        """Named here because every other test calls _tick by hand, and a
        timer that was never connected would pass all of them."""
        self.connect()
        self.screen.on_enter()
        self.pump()
        self.assertTrue(self.screen._timer.isActive())
        self.assertEqual(self.screen._timer.interval(),
                         monitor.DEFAULT_INTERVAL * 1000)
        before = len(self.reads)
        self.screen._timer.timeout.emit()
        self.pump()
        self.assertEqual(len(self.reads), before + 1)

    def test_a_console_that_goes_off_stops_it(self):
        self.connect()
        self.screen.on_enter()
        self.pump()
        self.connection.set_connection("unreachable")
        self.assertFalse(self.screen.running)

    def test_a_different_console_starts_a_new_graph(self):
        """Two consoles on one line would be a graph that lied."""
        self.connect()
        self.screen.on_enter()
        self.pump()
        self.assertEqual(len(self.screen.history), 1)
        self.connection.set_host("127.0.0.2")
        self.assertEqual(len(self.screen.history), 0)
        for chart in self.screen.charts.values():
            self.assertIs(chart._history, self.screen.history)

    def test_a_silent_console_is_counted_and_reported(self):
        self.connect()
        self.answers = [{}, {}]
        self.screen.on_enter()
        self.pump()
        self.screen._tick()
        self.pump()
        self.assertEqual(len(self.screen.history), 0)
        self.assertEqual(self.screen.history.misses, 2)
        self.assertIn("2 times the console did not answer",
                      self.screen.status_words())

    def test_a_read_that_raised_is_counted_the_same_way(self):
        def angry(host, probe_factory=None, cancelled=lambda: False,
                  storage=False):
            raise OSError("the network went away")

        self.connect()
        self.screen.sampler = angry
        self.screen.on_enter()
        self.pump()
        self.assertEqual(self.screen.history.misses, 1)
        self.assertIn("network went away", self.screen.status.text())

    def test_a_tick_that_arrives_while_the_console_is_still_answering_is_skipped(self):
        self.connect()
        self.screen.on_enter()
        self.screen._tick()
        self.pump()
        self.assertEqual(self.screen._overlaps, 1)
        self.assertIn("skipped", self.screen.status_words())

    def test_choosing_a_longer_interval_retimes_the_polling(self):
        self.connect()
        self.screen.on_enter()
        self.pump()
        self.screen.interval_box.setCurrentIndex(
            [seconds for seconds, _w in monitor.INTERVALS].index(30))
        self.assertEqual(self.screen.interval, 30)
        self.assertEqual(self.screen._timer.interval(), 30000)
        for chart in self.screen.charts.values():
            self.assertEqual(chart._interval, 30)

    def test_choosing_a_window_changes_what_the_graphs_draw(self):
        self.connect()
        self.screen.on_enter()
        self.pump()
        self.screen.span_box.setCurrentIndex(
            [seconds for seconds, _w in monitor.SPANS].index(300))
        self.assertEqual(self.screen.span, 300)
        for chart in self.screen.charts.values():
            self.assertEqual(chart._span, 300)

    def test_the_readings_are_saved_as_csv(self):
        self.connect()
        self.screen.on_enter()
        self.pump()
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "readings.csv")
            self.assertTrue(self.screen.save_csv(path))
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
        self.assertIn("epoch,time,cpu (°C)", text)
        self.assertEqual(len(text.strip().split("\n")), 2)
        self.assertIn("Saved 1 readings", self.screen.status.text())

    def test_saving_somewhere_unwritable_says_so_rather_than_crashing(self):
        self.connect()
        self.screen.on_enter()
        self.pump()
        self.assertFalse(self.screen.save_csv(
            os.path.join(os.sep, "nowhere-at-all", "x.csv")))
        self.assertIn("Could not write", self.screen.status.text())

    def test_clear_empties_the_graphs_and_the_buttons_follow(self):
        self.connect()
        self.screen.on_enter()
        self.pump()
        self.assertTrue(self.screen.save_button.isEnabled())
        self.screen._on_clear_pressed()
        self.assertEqual(len(self.screen.history), 0)
        self.assertFalse(self.screen.save_button.isEnabled())
        for chart in self.screen.charts.values():
            self.assertFalse(chart.has_anything())

    def test_start_and_stop_are_the_one_button(self):
        self.connect()
        self.assertEqual(self.screen.start_button.text(), "Start")
        self.screen.on_enter()
        self.pump()
        self.assertEqual(self.screen.start_button.text(), "Stop")
        self.screen.start_button.click()
        self.assertFalse(self.screen.running)
        self.assertEqual(self.screen.start_button.text(), "Start")


class EveryGraphHasACaller(ScreenCase):
    """The reading that arrives is what has to reach the graph.

    Three times in this program something painted correctly when a test called
    the painter and never once when the program ran, because nothing on the
    real path called it. So these tests drive the arrival of a reading and
    then ask the widgets what they hold.
    """

    def test_a_reading_arriving_puts_a_point_on_every_graph(self):
        self.connect()
        self.screen.on_enter()
        self.pump()
        for key, _title, keys in telemetry.GRAPHS:
            chart = self.screen.charts[key]
            self.assertTrue(chart.has_anything(), key)
            for name in keys:
                self.assertEqual(len(chart.points_for(name)), 1, name)

    def test_a_reading_arriving_updates_the_figures_above_the_graphs(self):
        self.connect()
        self.answers = [facts(cpu=63.0, fan=44)]
        self.screen.on_enter()
        self.pump()
        self.assertEqual(self.screen.readouts["cpu"].value.text(), "63 °C")
        self.assertEqual(self.screen.readouts["fan"].value.text(), "44%")
        self.assertIn("low", self.screen.readouts["cpu"].range.text())

    def test_a_reading_arriving_updates_the_legend_over_each_graph(self):
        self.connect()
        self.answers = [facts(cpu=63.0, rsx=66.0)]
        self.screen.on_enter()
        self.pump()
        legend = self.screen.charts["temperature"].legend_label.text()
        self.assertIn("CPU 63 °C", legend)
        self.assertIn("RSX 66 °C", legend)
        self.assertIn(COLOURS["accent"], legend)

    def test_a_reading_arriving_repaints_the_graph_itself(self):
        """The whole screen is rendered afterwards and the line looked for.

        The screen rather than the graph on its own: a chart inside a layout
        is given its size by that layout, so resizing the widget by hand and
        rendering it proves nothing about what somebody sees.
        """
        self.connect()
        self.answers = [facts(cpu=61.0), facts(cpu=62.0), facts(cpu=63.0)]
        self.screen.on_enter()
        self.pump()
        for _ in range(2):
            self.screen._tick()
            self.pump()
        self.screen.resize(1000, 900)
        APP.processEvents()
        image = QImage(1000, 900, QImage.Format_RGB32)
        image.fill(QColor("#ffffff"))
        self.screen.render(image)
        wanted = QColor(COLOURS["accent"]).rgb()
        found = sum(image.pixel(x, y) == wanted
                    for y in range(900) for x in range(1000))
        self.assertGreater(found, 20)

    def test_the_temperature_colours_are_the_ones_the_home_strip_uses(self):
        self.connect()
        self.answers = [facts(cpu=85.0)]
        self.screen.on_enter()
        self.pump()
        self.assertIn(COLOURS["error"],
                      self.screen.readouts["cpu"].value.styleSheet())

    def test_the_pointer_over_a_graph_reads_that_moment_out(self):
        self.connect()
        self.answers = [facts(cpu=61.0)]
        self.screen.on_enter()
        self.pump()
        reading = self.screen.history.readings[-1]
        self.screen._on_hover(reading)
        self.assertEqual(self.screen.readouts["cpu"].value.text(), "61 °C")
        self.assertIn("at ", self.screen.readouts["cpu"].range.text())
        self.screen._on_hover(None)
        self.assertIn("low", self.screen.readouts["cpu"].range.text())

    def test_what_the_console_said_beside_the_graphs_comes_from_the_reading(self):
        self.connect()
        self.answers = [facts(firmware_version="4.92", firmware_kind="HEN",
                              uptime="3h 12m", running_title="BLES01031")]
        self.screen.on_enter()
        self.pump()
        words = self.screen.facts_label.text()
        self.assertIn("4.92", words)
        self.assertIn("BLES01031", words)
        self.assertIn("3h 12m", words)
        # free_space_text already ends in the word, so it is not said twice.
        self.assertEqual(words.count("free"), 1)


class TheHeatList(ScreenCase):
    """The list under the graphs, which is the only place an episode that
    ended is still reported."""

    def feed(self, rows, interval=5):
        """Readings straight into the history, at the times given."""
        self.screen._interval = interval
        base = 1_700_000_000.0
        for offset, cpu in rows:
            self.screen.history.append(facts(cpu=cpu), at=base + offset)
        self.screen._refresh()

    def words(self):
        return " ".join(text for _token, text in self.screen.event_lines())

    def test_nothing_crossed_says_so_with_the_level_in_it(self):
        self.feed([(0, 60.0), (5, 61.0)])
        self.assertIn("Nothing has crossed 70 °C", self.words())

    def test_an_episode_that_ended_is_still_listed(self):
        self.feed([(0, 60.0), (5, 82.0), (10, 85.0), (15, 60.0), (20, 61.0)])
        words = self.words()
        self.assertIn("CPU was above 80 °C", words)
        self.assertIn("peaking at 85 °C", words)
        # First reading above to last reading above, which is what is known.
        self.assertIn("5s", words)

    def test_an_episode_still_going_is_said_in_the_present(self):
        self.feed([(0, 60.0), (5, 82.0), (10, 85.0)])
        words = self.words()
        self.assertIn("has been above 80 °C since", words)
        self.assertIn("so far", words)

    def test_only_the_highest_level_crossed_is_listed(self):
        """An afternoon above seventy must not bury four minutes above
        eighty."""
        self.feed([(0, 72.0), (5, 74.0), (10, 85.0), (15, 86.0)])
        words = self.words()
        self.assertIn("above 80 °C", words)
        self.assertNotIn("above 70 °C", words)

    def test_the_lower_level_is_listed_when_that_is_all_that_happened(self):
        self.feed([(0, 72.0), (5, 74.0), (10, 60.0)])
        self.assertIn("above 70 °C", self.words())

    def test_the_highest_recorded_is_said_even_with_no_episodes(self):
        self.feed([(0, 60.0), (5, 66.5)])
        self.assertIn("Highest recorded: CPU 66.5 °C", self.words())

    def test_a_long_afternoon_of_episodes_is_summarised_rather_than_listed(self):
        rows = []
        for index in range(12):
            rows.append((index * 10, 85.0))
            rows.append((index * 10 + 5, 60.0))
        self.feed(rows)
        lines = [text for _token, text in self.screen.event_lines()]
        listed = [line for line in lines if "was above" in line]
        self.assertEqual(len(listed), monitor.EPISODE_LIMIT)
        self.assertTrue(any("earlier spells" in line for line in lines))

    def test_the_list_is_on_the_screen_and_coloured_by_level(self):
        self.feed([(0, 60.0), (5, 85.0), (10, 86.0), (15, 60.0)])
        text = self.screen.events_label.text()
        self.assertIn("CPU was above 80 °C", text)
        self.assertIn(COLOURS["error"], text)

    def test_the_list_follows_the_window_the_graphs_are_showing(self):
        self.feed([(0, 85.0), (5, 86.0)] + [(900 + i * 5, 60.0)
                                            for i in range(3)])
        self.screen.span_box.setCurrentIndex(
            [seconds for seconds, _w in monitor.SPANS].index(300))
        self.assertIn("Nothing has crossed", self.words())


class TheBandUnderTheGraphs(ScreenCase):
    def test_it_sits_in_the_temperature_panel_and_shares_its_window(self):
        self.assertIsNotNone(self.screen.band)
        chart = self.screen.charts[monitor.BAND_ON]
        base = 1_700_000_000.0
        for index in range(4):
            self.screen.history.append(facts(running_title="BLES01031"),
                                       at=base + index * 5)
        self.screen._refresh()
        self.assertEqual(self.screen.band.window(), chart.window())
        self.assertEqual([title for _at, _to, title
                          in self.screen.band.spans()], ["BLES01031"])

    def test_a_reading_arriving_reaches_the_band(self):
        self.connect()
        self.answers = [facts(running_title="BLES01717")]
        self.screen.on_enter()
        self.pump()
        self.assertTrue(self.screen.band.has_anything())

    def test_clearing_hands_the_band_the_new_history(self):
        self.connect()
        self.screen.on_enter()
        self.pump()
        self.screen._on_clear_pressed()
        self.assertIs(self.screen.band._history, self.screen.history)
        self.assertFalse(self.screen.band.has_anything())



class TheBandPaints(unittest.TestCase):
    """Rendered on its own, because a widget inside a layout is given its
    size by that layout and resizing it by hand proves nothing."""

    def band_with(self, rows, span=None):
        band = TitleBand()
        band.set_theme(StubTheme())
        history = telemetry.History()
        base = 1_700_000_000.0
        for offset, title in rows:
            history.append(facts(running_title=title), at=base + offset)
        band.set_history(history)
        band.set_span(span)
        band.set_interval(5)
        return band

    def render(self, band, width=400, height=40):
        band.resize(width, height)
        image = QImage(width, height, QImage.Format_RGB32)
        image.fill(QColor("#ffffff"))
        band.render(image)
        return image

    def tinted(self, image, token):
        """Whether anything is painted in this token's colour.

        Tinted rather than matched exactly: a block is a faint fill inside an
        antialiased rounded edge, so no pixel in it is the token's colour to
        the byte. The stub colours are pure primaries, so which primary a
        pixel leans towards is the whole question.
        """
        wanted = QColor(COLOURS[token])
        for y in range(image.height()):
            for x in range(image.width()):
                pixel = QColor(image.pixel(x, y))
                leans = [pixel.red() > pixel.green() + 20
                         and pixel.red() > pixel.blue() + 20,
                         pixel.green() > pixel.red() + 20
                         and pixel.green() > pixel.blue() + 20,
                         pixel.blue() > pixel.red() + 20
                         and pixel.blue() > pixel.green() + 20]
                towards = [wanted.red() > 200 and wanted.green() < 200,
                           wanted.green() > 200 and wanted.red() < 200,
                           wanted.blue() > 200 and wanted.red() < 200]
                if any(one and two for one, two in zip(leans, towards)):
                    return True
        return False

    def test_two_games_are_drawn_in_two_colours(self):
        band = self.band_with([(0, "BLES01031"), (5, "BLES01031"),
                               (10, "BLES01717"), (15, "BLES01717")])
        image = self.render(band)
        for token in ("accent", "info"):
            self.assertTrue(self.tinted(image, token), token)

    def test_nothing_loaded_says_so_rather_than_drawing_an_empty_row(self):
        band = self.band_with([(0, ""), (5, "")])
        self.assertFalse(band.has_anything())
        image = self.render(band)
        self.assertFalse(self.tinted(image, "accent"))

    def test_a_game_the_console_held_for_one_reading_is_still_visible(self):
        """A block a fraction of a pixel wide is a launch nobody can see."""
        band = self.band_with([(0, ""), (5, "BLES01031")] +
                              [(10 + index * 5, "") for index in range(60)])
        self.assertTrue(self.tinted(self.render(band), "accent"))


class TheSavedFile(ScreenCase):
    """Save writes it, Open reads it back."""

    def saved(self, folder):
        path = os.path.join(folder, "readings.csv")
        self.assertTrue(self.screen.save_csv(path))
        return path

    def test_a_file_this_screen_wrote_opens_again(self):
        self.connect()
        self.answers = [facts(cpu=63.0, running_title="BLES01031")]
        self.screen.on_enter()
        self.pump()
        with tempfile.TemporaryDirectory() as folder:
            path = self.saved(folder)
            self.screen._on_clear_pressed()
            self.assertEqual(len(self.screen.history), 0)
            self.assertTrue(self.screen.load_csv(path))
        self.assertEqual(len(self.screen.history), 1)
        self.assertEqual(self.screen.history.readings[0].title, "BLES01031")
        self.assertEqual(self.screen.readouts["cpu"].value.text(), "63 °C")

    def test_opening_a_file_stops_the_recording(self):
        self.connect()
        self.screen.on_enter()
        self.pump()
        with tempfile.TemporaryDirectory() as folder:
            path = self.saved(folder)
            self.assertTrue(self.screen.load_csv(path))
        self.assertFalse(self.screen.running)
        self.assertFalse(self.screen._timer.isActive())

    def test_the_footer_names_the_file_it_is_showing(self):
        self.connect()
        self.screen.on_enter()
        self.pump()
        with tempfile.TemporaryDirectory() as folder:
            path = self.saved(folder)
            self.screen.load_csv(path)
            words = self.screen.status_words()
        self.assertIn("readings.csv", words)
        self.assertIn("Press Start", words)

    def test_the_graphs_and_the_band_all_get_the_file(self):
        self.connect()
        self.answers = [facts(running_title="BLES01031")]
        self.screen.on_enter()
        self.pump()
        with tempfile.TemporaryDirectory() as folder:
            path = self.saved(folder)
            self.screen.load_csv(path)
        for chart in self.screen.charts.values():
            self.assertIs(chart._history, self.screen.history)
        self.assertIs(self.screen.band._history, self.screen.history)
        self.assertTrue(self.screen.band.has_anything())

    def test_a_file_that_is_not_readings_is_refused_with_the_reason(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "notes.csv")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("this is not a readings file\n")
            self.assertFalse(self.screen.load_csv(path))
        self.assertIn("epoch", self.screen.status.text())
        self.assertIn("notes.csv", self.screen.status.text())

    def test_a_file_that_is_not_there_says_so(self):
        self.assertFalse(self.screen.load_csv(
            os.path.join(os.sep, "nowhere-at-all", "x.csv")))
        self.assertIn("Could not read", self.screen.status.text())

    def test_recording_again_replaces_the_file(self):
        self.connect()
        self.screen.on_enter()
        self.pump()
        with tempfile.TemporaryDirectory() as folder:
            path = self.saved(folder)
            self.screen.load_csv(path)
        self.screen.start()
        self.pump()
        self.assertEqual(self.screen.loaded_from, "")
        self.assertTrue(self.screen.running)

    def test_coming_back_to_the_screen_leaves_a_file_alone(self):
        """Somebody looking at a saved afternoon must not lose it by
        stepping away and back."""
        self.connect()
        self.screen.on_enter()
        self.pump()
        with tempfile.TemporaryDirectory() as folder:
            path = self.saved(folder)
            self.screen.load_csv(path)
        self.screen.on_leave()
        self.screen.on_enter()
        self.pump()
        self.assertEqual(self.screen.loaded_from, path)
        self.assertFalse(self.screen.running)

    def test_a_change_of_address_leaves_a_file_alone(self):
        self.connect()
        self.screen.on_enter()
        self.pump()
        with tempfile.TemporaryDirectory() as folder:
            path = self.saved(folder)
            self.screen.load_csv(path)
        self.connection.set_host("127.0.0.9")
        self.assertEqual(len(self.screen.history), 1)
        self.assertEqual(self.screen.loaded_from, path)


class TheKeepRecordingTick(ScreenCase):
    """Off by default, and the one way this tool reads a console that is not
    the screen in front of somebody."""

    def test_it_is_off_to_start_with(self):
        self.assertFalse(self.screen.keep_box.isChecked())

    def test_leaving_stops_the_reading_when_it_is_off(self):
        self.connect()
        self.screen.on_enter()
        self.pump()
        self.screen.on_leave()
        self.assertFalse(self.screen.running)

    def test_leaving_keeps_the_reading_when_it_is_on(self):
        self.connect()
        self.screen.on_enter()
        self.pump()
        self.screen.keep_box.setChecked(True)
        self.screen.on_leave()
        self.assertTrue(self.screen.running)
        self.assertTrue(self.screen._timer.isActive())
        before = len(self.reads)
        self.screen._timer.timeout.emit()
        self.pump()
        self.assertEqual(len(self.reads), before + 1)

    def test_leaving_while_it_is_on_says_so(self):
        noted = []
        self.screen.event_noted.connect(noted.append)
        self.connect()
        self.screen.on_enter()
        self.pump()
        self.screen.keep_box.setChecked(True)
        self.screen.on_leave()
        self.assertEqual(noted, ["Monitor is still recording"])

    def test_it_keeps_nothing_when_the_reading_was_already_stopped(self):
        self.screen.keep_box.setChecked(True)
        self.screen.on_leave()
        self.assertFalse(self.screen.running)


class TheReader(unittest.TestCase):
    """read_sample, with a probe that answers from a string."""

    PAGE = ("<html>CPU: 61.5&deg;C RSX: 64&deg;C FAN: 41% "
            "Firmware: 4.92 HEN HDD: 572.9 GB free "
            "<a href='/play.ps3?BLES01031'>x</a></html>")

    class Probe:
        def __init__(self, pages, asked):
            self.pages = pages
            self.asked = asked

        def get(self, path):
            self.asked.append(path)
            body = self.pages.get(path)

            class Response:
                ok = body is not None

            Response.body = body or ""
            return Response()

    def probe_factory(self, pages, asked):
        def make(host, timeout=None):
            return self.Probe(pages, asked)
        return make

    def test_a_plain_reading_asks_for_one_page_only(self):
        asked = []
        found = monitor.read_sample(
            "10.0.0.1",
            probe_factory=self.probe_factory({"/cpursx.ps3": self.PAGE},
                                             asked))
        self.assertEqual(asked, ["/cpursx.ps3"])
        self.assertEqual(found["cpu_temp_c"], 61.5)
        self.assertEqual(found["fan_speed_percent"], 41)

    def test_a_storage_reading_asks_for_the_front_page_as_well(self):
        asked = []
        found = monitor.read_sample(
            "10.0.0.1", storage=True,
            probe_factory=self.probe_factory(
                {"/": self.PAGE, "/cpursx.ps3": self.PAGE}, asked))
        self.assertEqual(asked, ["/", "/cpursx.ps3"])
        # parse_size reads webMAN's GB as 1024 cubed, and the free space
        # series divides by the same, so the graph says 572.9 as well.
        self.assertEqual(found["hdd_free_bytes"], 615_146_690_969)
        self.assertAlmostEqual(
            telemetry.value_of(found, telemetry.series_for("free")),
            572.9, places=3)

    def test_a_console_that_did_not_answer_gives_nothing(self):
        asked = []
        self.assertEqual(
            monitor.read_sample("10.0.0.1",
                                probe_factory=self.probe_factory({}, asked)),
            {})

    def test_a_cancelled_read_stops_before_it_asks(self):
        asked = []
        self.assertEqual(
            monitor.read_sample(
                "10.0.0.1", cancelled=lambda: True,
                probe_factory=self.probe_factory(
                    {"/cpursx.ps3": self.PAGE}, asked)),
            {})
        self.assertEqual(asked, [])


if __name__ == "__main__":
    unittest.main()
