"""Readings from a console over time, and the arithmetic a graph needs.

No Qt in here. Everything the Monitor screen draws is decided by the functions
below and then painted by ps3tools/shell/chart.py, so the parts that can be
wrong in a way nobody notices -- where a line goes, what the axis says, where a
gap is -- can be checked without a window.

Three things this deliberately does not do.

It does not interpolate. A reading that never arrived is a hole in the series
and the graph draws a break, because a straight line across four missing
minutes is the program inventing a temperature it was never told.

It does not resample on to a fixed grid. Every point carries the moment it was
read, and the horizontal position comes from that moment, so a console that
answered late is drawn late and a slow answer looks like one.

It keeps nothing after the program closes. A history that outlived the window
would be a file of somebody's console habits written without being asked for,
and the Monitor screen has a CSV button for the case where somebody does want
one.
"""

import math
import time
from dataclasses import dataclass

from .updates import free_bytes_for

#: How many readings are kept. Two hours at the shortest interval the screen
#: offers and five and a half at the default. The cap exists so that a window
#: left open overnight cannot grow without limit.
DEFAULT_LIMIT = 4000

#: A reading further than this many times the sampling interval from the one
#: before it is a gap rather than the next point of the same line. Somewhere
#: above two, because a console under load answers late by a fair fraction of
#: a short interval and a line that broke every time that happened would be
#: dots.
GAP_FACTOR = 2.5


@dataclass(frozen=True)
class Series:
    """One line on a graph.

    floor and ceiling bound the axis where the figure has a natural range. A
    fan is a percentage and its axis is 0 to 100 whatever the readings did,
    because a fan that sat between 39% and 41% for an hour should look flat
    rather than fill the graph with noise. A temperature has no natural top,
    so it gets a floor to stop the axis starting at zero and nothing above.
    """

    key: str
    label: str
    unit: str
    token: str                  #: theme token the line is drawn in
    source: str                 #: the facts key it is read from
    floor: float = None         #: axis never goes above this at the bottom
    ceiling: float = None       #: axis never goes below this at the top
    least_span: float = 5.0     #: smallest axis range, so flat is flat
    decimals: int = 0
    scale: float = 1.0          #: what the fact is multiplied by


#: The temperatures share a graph, so they share an axis and the same bounds.
#: A PS3 idles in the fifties, so an axis from zero would draw every reading
#: in the top third of the graph.
TEMPERATURE_FLOOR = 40.0

SERIES = (
    Series("cpu", "CPU", "°C", "accent", "cpu_temp_c",
           floor=TEMPERATURE_FLOOR, least_span=10.0),
    Series("rsx", "RSX", "°C", "info", "rsx_temp_c",
           floor=TEMPERATURE_FLOOR, least_span=10.0),
    Series("fan", "Fan", "%", "warn", "fan_speed_percent",
           floor=0.0, ceiling=100.0, least_span=100.0),
    # Divided the way human_size divides, so the figure on the graph and the
    # figure the rest of the program prints for the same console are the same
    # number. webMAN's own pages say "572.9 GB free" of a drive whose free
    # space is 572.9 times 1024 cubed, and a graph reading 615 beside a bar
    # reading 572.9 would be read as one of the two being broken.
    Series("free", "HDD free", "GB", "ok", "hdd_free_bytes",
           least_span=1.0, decimals=1, scale=1.0 / 1024 ** 3),
)

#: The graphs the screen draws, and which lines are on each. Temperatures
#: together because they are the same quantity in the same unit and the
#: interesting thing about them is the distance between the two.
GRAPHS = (
    ("temperature", "Temperature", ("cpu", "rsx")),
    ("fan", "Fan speed", ("fan",)),
    ("free", "Free space", ("free",)),
)


def series_for(key):
    for item in SERIES:
        if item.key == key:
            return item
    raise KeyError(f"no series called {key!r}")


def value_of(facts, series):
    """One figure out of a facts dictionary, or None if it is not in there.

    None is the answer for a console that did not report this figure, and it
    is never turned into a zero. webMAN 1.47.48q does not print everything
    every build prints, and a fan speed of zero is a claim about a console
    rather than an admission that nothing was said.
    """
    raw = (facts or {}).get(series.source)
    if series.key == "free" and raw is None:
        # The mount listing carries it when webMAN's own sentence does not.
        # Same reader the downloads use, so one answer in this program to how
        # much room is left.
        raw = free_bytes_for((facts or {}).get("devices"), "dev_hdd0")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return float(raw) * series.scale


@dataclass(frozen=True)
class Reading:
    """What one sample of the console said, and when it was taken."""

    at: float
    values: dict

    def value(self, key):
        return self.values.get(key)


#: Units written hard against the number. A percentage is said "41%" and a
#: temperature "61 °C", and the strip on the home screen writes both that way,
#: so a figure here reads the same as the same figure there.
TIGHT_UNITS = ("%",)


def format_value(value, series):
    """One figure as it is written on screen, or "" for a figure nobody gave."""
    if value is None:
        return ""
    gap = "" if series.unit in TIGHT_UNITS else " "
    if series.decimals:
        return f"{value:.{series.decimals}f}{gap}{series.unit}"
    if float(value).is_integer():
        return f"{int(value)}{gap}{series.unit}"
    return f"{value:.1f}{gap}{series.unit}"


class History:
    """Every reading this session, oldest first.

    A miss is counted rather than stored. The graph has nothing to draw for a
    request that failed, but the footer says how many failed, because a
    console that answered eleven times out of forty is telling somebody
    something about their network and a graph with a few breaks in it does
    not say it loudly enough.
    """

    def __init__(self, limit=DEFAULT_LIMIT):
        self.limit = limit
        self._readings = []
        self.misses = 0
        self.last_miss_at = None

    def __len__(self):
        return len(self._readings)

    @property
    def readings(self):
        return tuple(self._readings)

    def append(self, facts, at=None):
        """Records one sample. Returns the Reading, or None for a miss.

        An empty facts dictionary is what the reader returns for a console
        that did not answer, and a sample that carried none of the figures
        the graphs draw is the same thing wearing a hat: webMAN answered with
        a page that had nothing on it. Both count as misses.
        """
        at = time.time() if at is None else at
        values = {}
        for series in SERIES:
            found = value_of(facts, series)
            if found is not None:
                values[series.key] = found
        if not values:
            self.misses += 1
            self.last_miss_at = at
            return None
        reading = Reading(at, values)
        self._readings.append(reading)
        # Oldest first out. The cap is a window length rather than a budget,
        # so the recent end is the end worth keeping.
        if len(self._readings) > self.limit:
            del self._readings[:len(self._readings) - self.limit]
        return reading

    @property
    def first_at(self):
        return self._readings[0].at if self._readings else None

    @property
    def last_at(self):
        return self._readings[-1].at if self._readings else None

    def points(self, key, span=None, now=None):
        """(at, value) for every reading that carried this figure.

        span limits it to the last so many seconds, measured from the newest
        reading rather than from the clock: a screen that was left open with
        the console switched off should still show the hour before it went
        off, instead of an empty graph with the readings just off the left
        edge.
        """
        points = [(item.at, item.values[key])
                  for item in self._readings if key in item.values]
        if span is None or not points:
            return tuple(points)
        end = points[-1][0] if now is None else now
        return tuple(point for point in points if point[0] >= end - span)

    def latest(self, key):
        for item in reversed(self._readings):
            if key in item.values:
                return item.values[key]
        return None

    def extremes(self, key, span=None, now=None):
        """(lowest, highest) of what is on the graph, or (None, None).

        Read off the same window the graph draws so that the figures beside it
        describe what somebody is looking at.
        """
        values = [value for _at, value in self.points(key, span, now)]
        if not values:
            return None, None
        return min(values), max(values)

    def to_csv(self):
        """The whole history as text, one row per reading.

        Written for a spreadsheet and for an issue report. The epoch column
        is there because the local time column loses an hour twice a year and
        a graph drawn from the file should not have a step in it.
        """
        keys = [series.key for series in SERIES]
        head = ["epoch", "time"] + [
            f"{series.key} ({series.unit})" for series in SERIES]
        rows = [",".join(head)]
        for item in self._readings:
            stamp = time.strftime("%Y-%m-%d %H:%M:%S",
                                  time.localtime(item.at))
            cells = [f"{item.at:.3f}", stamp]
            for key in keys:
                value = item.values.get(key)
                cells.append("" if value is None else f"{value:g}")
            rows.append(",".join(cells))
        return "\n".join(rows) + "\n"


def runs(points, gap):
    """The points split into unbroken lines wherever a reading is missing.

    A run of one point is kept and drawn as a dot, because a single reading
    between two long silences is the only evidence there is that the console
    was awake at that moment.
    """
    out = []
    current = []
    for point in points:
        if current and point[0] - current[-1][0] > gap:
            out.append(tuple(current))
            current = []
        current.append(point)
    if current:
        out.append(tuple(current))
    return tuple(out)


def bounds(values, floor=None, ceiling=None, least_span=1.0, pad=0.1):
    """(low, high) for a vertical axis that has to hold these values.

    Widened to least_span so a figure that did not move is a flat line across
    the middle rather than a full-height wobble, padded so a line never runs
    along the frame, and then fitted inside whatever hard limits the series
    has.

    Fitted by sliding rather than by cutting. A fan reading of 100% would
    otherwise give an axis that went to 110, and cutting the top off that
    leaves an axis too small to hold the least_span it was given, which is
    how a fan sitting at 100% came out drawn as a wobble across the whole
    graph. Where the limits are tighter than the span wanted, the limits win
    and the axis is exactly them.
    """
    values = [value for value in values if value is not None]
    if not values:
        low = 0.0 if floor is None else floor
        high = low + least_span if ceiling is None else ceiling
        return low, high
    low, high = float(min(values)), float(max(values))
    want = max((high - low) * (1 + 2 * pad), least_span)
    if floor is not None and ceiling is not None \
            and want >= ceiling - floor:
        return float(floor), float(ceiling)
    middle = (high + low) / 2
    low, high = middle - want / 2, middle + want / 2
    # Slide the window rather than shorten it, so the axis keeps the range it
    # was asked for and the readings stay off the frame.
    if floor is not None and low < floor:
        high += floor - low
        low = floor
    if ceiling is not None and high > ceiling:
        low -= high - ceiling
        high = ceiling
    if floor is not None:
        low = max(low, floor)
    if ceiling is not None:
        high = min(high, ceiling)
    if high - low <= 0:
        high = low + least_span
    return low, high


def nice_step(span, count):
    """A step of 1, 2 or 5 times a power of ten that gives about count gaps."""
    if span <= 0 or count <= 0:
        return 1.0
    rough = span / count
    power = 10 ** math.floor(math.log10(rough))
    for multiple in (1, 2, 5, 10):
        if multiple * power >= rough:
            return multiple * power
    return 10 * power


def ticks(low, high, count=4):
    """Gridline values inside [low, high], on round numbers.

    Round numbers because the point of the labels is to read a value off the
    line, and 57.3 on an axis tells somebody nothing that 60 does not.
    """
    if high <= low:
        return (low,)
    step = nice_step(high - low, count)
    first = math.ceil(low / step) * step
    out = []
    value = first
    while value <= high + step * 1e-9:
        # Steps are binary fractions of a power of ten, so the accumulated
        # error shows up as 59.99999999999999 on a label without this.
        out.append(round(value, 10))
        value += step
    return tuple(out)


def duration_words(seconds):
    """A span as somebody would say it: 45s, 5m, 1h 20m, 2h."""
    seconds = int(round(max(0.0, seconds)))
    if seconds < 60:
        return f"{seconds}s"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m" if rest < 30 else f"{minutes + 1}m"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h" if not minutes else f"{hours}h {minutes}m"


#: The steps a time axis is allowed to use, in seconds. Round numbers as a
#: person says them, because the decimal steps that suit a temperature axis
#: give a graph labelled every 500 seconds, and nobody reads a clock that way.
TIME_STEPS = (5, 10, 15, 30, 60, 120, 300, 600, 900, 1800,
              3600, 7200, 10800, 21600, 43200, 86400)


def time_step(span, count):
    """The step for a time axis of this length, from TIME_STEPS."""
    if span <= 0 or count <= 0:
        return TIME_STEPS[0]
    rough = span / count
    for step in TIME_STEPS:
        if step >= rough:
            return step
    return TIME_STEPS[-1]


def time_labels(span, count=4):
    """(seconds before now, label) for the horizontal axis.

    Counted back from the right-hand edge, because the right-hand edge is the
    newest reading and that is what somebody is looking at. The newest gets
    the word "now" rather than "0s".
    """
    if span <= 0:
        return ((0.0, "now"),)
    step = time_step(span, count)
    out = [(0.0, "now")]
    value = step
    while value <= span + step * 1e-9:
        out.append((float(value), duration_words(value)))
        value += step
    return tuple(out)
