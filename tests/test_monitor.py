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
from ps3tools.shell.chart import Chart, plot
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
        self.assertEqual(lines[0],
                         "epoch,time,cpu (°C),rsx (°C),fan (%),free (GB)")
        cells = lines[1].split(",")
        self.assertEqual(cells[0], "1700000000.000")
        self.assertEqual(cells[2], "61")
        self.assertEqual(cells[3:], ["", "", ""])


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
