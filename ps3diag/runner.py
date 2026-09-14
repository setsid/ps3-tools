"""Drives the collectors and hands back a RunResult.

The only orchestration rules that matter here: a category that fails is recorded
and the run carries on, and the whole-run deadline is checked between categories
as well as inside them. A console that has gone to sleep halfway through must
still produce a usable zip out of the categories that finished.
"""

import time

from . import VERSION
from .collectors import (CATEGORIES, FAILED, PARTIAL, SKIPPED, RunExpired,
                         RunStopped, Context, CollectorResult)
from .transport import DEFAULT_FTP_TIMEOUT, DEFAULT_HTTP_TIMEOUT, FtpLister, \
    HttpProbe


class RunResult:
    def __init__(self, host, requested, include_identifiers=False):
        self.host = host
        self.requested = list(requested)
        self.include_identifiers = include_identifiers
        self.results = []
        self.attempts = []
        self.started_wall = time.time()
        self.started = time.monotonic()
        self.seconds = 0.0
        self.tool_version = VERSION
        self.file_names = []
        self.redaction_counts = {}
        # True when the user pressed Stop. The run still produced a file and
        # everything in it was collected, which is why this is kept apart from
        # the per-category statuses rather than folded into them.
        self.stopped = False

    @property
    def generated_local(self):
        return time.strftime("%Y-%m-%dT%H:%M:%S",
                             time.localtime(self.started_wall))

    @property
    def generated_utc(self):
        return time.strftime("%Y-%m-%dT%H:%M:%SZ",
                             time.gmtime(self.started_wall))

    def by_key(self, key):
        for result in self.results:
            if result.key == key:
                return result
        return None

    def counts(self):
        out = {}
        for result in self.results:
            out[result.status] = out.get(result.status, 0) + 1
        return out


def run(host, categories=None, include_identifiers=False, http_timeout=None,
        ftp_timeout=None, run_timeout=900.0, log=None, on_progress=None,
        http=None, ftp=None, should_stop=None, options=None):
    """Collect everything that was asked for.

    http and ftp are injectable so the test suite can drive the whole run
    against a mock bound to 127.0.0.1 without any of this knowing the difference.

    on_progress(key, title, state, payload) is called as each category starts
    and finishes, which is what the window's per-category list is built from.
    state is "running" or "done", and payload is the CollectorResult on "done".
    There is a third state, "detail", which a collector emits from inside a long
    loop; its payload is a line of text saying what is being read right now.
    One line per category is no use on an inventory that takes minutes, and the
    detail is what turns that dead time into something a user can watch.

    options are put in front of the collectors as ctx.options. The one the
    screen sets is identify_isos, which turns the per-image ranged reads on.
    """
    wanted = list(categories) if categories is not None else \
        [key for key, _title, _run in CATEGORIES]
    result_set = RunResult(host, wanted, include_identifiers)
    probe = http or HttpProbe(host, http_timeout or DEFAULT_HTTP_TIMEOUT,
                              log=log)
    lister = ftp if ftp is not None else FtpLister(
        host, ftp_timeout or DEFAULT_FTP_TIMEOUT, log=log)
    deadline = time.monotonic() + run_timeout if run_timeout else None

    # Which category the collectors are inside right now. Held here rather than
    # passed down because a collector reports what it is doing, not where in
    # the run it is; that is this layer's business.
    current = {"key": None, "title": ""}

    def progress(key, title, state, payload=None):
        if on_progress:
            on_progress(key, title, state, payload)

    def detail(message):
        if current["key"]:
            progress(current["key"], current["title"], "detail", message)

    context = Context(probe, lister, deadline, log, progress=detail,
                      should_stop=should_stop)
    context.options["host"] = host
    context.options.update(options or {})

    try:
        for key, title, collect in CATEGORIES:
            if key not in wanted:
                continue
            if result_set.stopped or (should_stop and should_stop()):
                # Skipped rather than failed: the console did nothing wrong and
                # the user knows perfectly well why this one is not there.
                stopped = CollectorResult(key, title)
                stopped.status = SKIPPED
                stopped.stopped = True
                stopped.note("Stopped at your request before this category "
                             "was collected.")
                result_set.stopped = True
                result_set.results.append(stopped)
                progress(key, title, "done", stopped)
                continue
            current["key"], current["title"] = key, title
            progress(key, title, "running")
            started = time.monotonic()
            if log:
                log.event("category_start", category=key)
            try:
                outcome = collect(context)
            except RunExpired as exc:
                outcome = CollectorResult(key, title)
                outcome.status = FAILED
                outcome.error = (f"The overall time limit was reached before "
                                 f"this category finished ({exc}).")
            except RunStopped:
                # The net under the collectors that handle it themselves. One
                # that does not keeps nothing, which is why the long ones do.
                outcome = CollectorResult(key, title)
                outcome.status = SKIPPED
                outcome.stopped = True
                outcome.note("Stopped at your request part way through this "
                             "category.")
            except Exception as exc:
                # A collector raising is a bug in this tool, not a console
                # problem. It is recorded as such and the run continues, because
                # six good categories are worth more than a clean stack trace.
                outcome = CollectorResult(key, title)
                outcome.status = FAILED
                outcome.error = (f"ps3-diag hit an internal error collecting "
                                 f"this category: {exc.__class__.__name__}: "
                                 f"{exc}")
            outcome.seconds = time.monotonic() - started
            current["key"] = None
            if getattr(outcome, "stopped", False):
                result_set.stopped = True
            result_set.results.append(outcome)
            if log:
                log.event("category_done", category=key, status=outcome.status,
                          seconds=round(outcome.seconds, 2),
                          artefacts=len(outcome.artefacts))
            progress(key, title, "done", outcome)

            # Once the inventory knows which devices exist, the game collector
            # should look at all of them rather than only the internal drive.
            if key == "storage" and outcome.status in ("ok", PARTIAL):
                names = [device["device"]
                         for device in outcome.facts.get("devices", [])
                         if device["device"] not in ("dev_flash", "dev_bdvd")]
                if names:
                    context.options["devices"] = names
    finally:
        if lister is not None:
            try:
                lister.close()
            except Exception:
                pass
        result_set.attempts = list(probe.attempts)
        result_set.seconds = time.monotonic() - result_set.started
    return result_set
