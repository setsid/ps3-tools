"""Crash handling. The property under test is that it cannot itself crash.

Everything else here -- the traceback lands in the file, the version is in it,
the breadcrumbs are bounded -- is ordinary. The bulk of the file is the part
that feeds the handler things it was not designed for: an exception whose
__str__ raises, a None where a type should be, a dialog that cannot be built,
a folder that cannot be written to. A crash reporter that throws replaces one
bad traceback with a worse one and loses the original, so it is tested harder
than the thing it reports on.
"""

import contextlib
import io
import os
import shutil
import sys
import tempfile
import threading
import unittest
from urllib.parse import parse_qs, unquote, urlparse

from support import ROOT  # noqa: F401  (puts the repo root on the path)

from ps3tools import VERSION, crashreport

QT = True
try:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
except ImportError:
    QT = False


# Values with the shape the redactor looks for. Nothing real: an IDPS beginning
# 0000000100 is the retail prefix and the rest is made up.
FAKE_IDPS = "00000001007A1B2C3D4E5F6071829304"
FAKE_MAC = "00:1F:A7:3C:9B:2E"


def application():
    return QApplication.instance() or QApplication([])


class QuietStderr:
    """The handler also writes the traceback to stderr, which is what a
    developer running from a terminal wants and not what a test run wants.

    Called from setUp rather than being one, because every case below defines
    its own setUp and a mixin's would simply be shadowed.
    """

    def quiet(self):
        keeper = contextlib.redirect_stderr(io.StringIO())
        keeper.__enter__()
        self.addCleanup(keeper.__exit__, None, None, None)


def raise_and_catch(exception):
    """A real exception with a real traceback, which format_exception needs.

    BaseException rather than Exception: KeyboardInterrupt is one of the cases
    the hook has to get right, and catching only Exception would let it out of
    here and take the test runner with it.
    """
    try:
        raise exception
    except BaseException:
        return sys.exc_info()


class Unprintable(Exception):
    """An exception that fights being turned into text. These exist: a message
    built from an object whose __str__ reaches something already torn down."""

    def __str__(self):
        raise RuntimeError("this exception cannot be described")

    __repr__ = __str__


class Breadcrumbs(unittest.TestCase):
    def setUp(self):
        crashreport.clear()
        self.addCleanup(crashreport.clear)

    def test_an_action_is_recorded(self):
        crashreport.note("Opened the diagnostic screen")
        trail = crashreport.breadcrumbs()
        self.assertEqual(len(trail), 1)
        self.assertIn("Opened the diagnostic screen", trail[0])

    def test_they_come_back_oldest_first(self):
        for word in ("first", "second", "third"):
            crashreport.note(word)
        trail = crashreport.breadcrumbs()
        self.assertIn("first", trail[0])
        self.assertIn("third", trail[-1])

    def test_the_trail_is_bounded(self):
        for index in range(crashreport.BREADCRUMB_LIMIT * 3):
            crashreport.note(f"action {index}")
        trail = crashreport.breadcrumbs()
        self.assertEqual(len(trail), crashreport.BREADCRUMB_LIMIT)
        # The oldest are the ones dropped.
        self.assertNotIn("action 0", " ".join(trail))
        self.assertIn(f"action {crashreport.BREADCRUMB_LIMIT * 3 - 1}",
                      " ".join(trail))

    def test_the_returned_trail_is_a_copy(self):
        crashreport.note("one")
        trail = crashreport.breadcrumbs()
        trail.append("not really")
        self.assertEqual(len(crashreport.breadcrumbs()), 1)

    def test_a_non_string_action_is_accepted(self):
        crashreport.note(1234)
        crashreport.note(None)
        crashreport.note(["a", "list"])
        self.assertEqual(len(crashreport.breadcrumbs()), 3)

    def test_an_object_that_cannot_be_described_does_not_raise(self):
        class Awkward:
            def __str__(self):
                raise ValueError("no")
            __repr__ = __str__

        crashreport.note(Awkward())
        self.assertEqual(len(crashreport.breadcrumbs()), 1)

    def test_it_is_safe_from_several_threads(self):
        def spam():
            for index in range(200):
                crashreport.note(f"thread action {index}")

        threads = [threading.Thread(target=spam) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(crashreport.breadcrumbs()),
                         crashreport.BREADCRUMB_LIMIT)


class TheReport(unittest.TestCase):
    def setUp(self):
        crashreport.clear()
        self.addCleanup(crashreport.clear)

    def test_it_carries_the_traceback(self):
        text = crashreport.compose(*raise_and_catch(ValueError("the message")))
        self.assertIn("Traceback (most recent call last)", text)
        self.assertIn("ValueError: the message", text)
        self.assertIn("raise exception", text)

    def test_it_carries_the_version(self):
        text = crashreport.compose(*raise_and_catch(ValueError("x")))
        self.assertIn(VERSION, text)

    def test_it_carries_the_environment(self):
        text = crashreport.compose(*raise_and_catch(ValueError("x")))
        self.assertIn("Python:", text)
        self.assertIn("PySide6:", text)
        self.assertIn("OS:", text)

    def test_it_carries_the_breadcrumbs(self):
        crashreport.note("Pressed Check")
        crashreport.note("Opened the patcher")
        text = crashreport.compose(*raise_and_catch(ValueError("x")))
        self.assertIn("Pressed Check", text)
        self.assertIn("Opened the patcher", text)

    def test_an_empty_trail_says_so_rather_than_nothing(self):
        text = crashreport.compose(*raise_and_catch(ValueError("x")))
        self.assertIn("nothing was recorded", text)

    def test_a_thread_name_is_included_when_there_is_one(self):
        text = crashreport.compose(*raise_and_catch(ValueError("x")),
                                   thread_name="worker-3")
        self.assertIn("worker-3", text)

    def test_composing_from_nothing_at_all_does_not_raise(self):
        text = crashreport.compose()
        self.assertIn(VERSION, text)

    def test_an_exception_that_cannot_be_described_does_not_raise(self):
        text = crashreport.compose(*raise_and_catch(Unprintable()))
        self.assertIn(VERSION, text)
        # Either the formatter coped and named it, or the fallback did.
        self.assertIn("Unprintable", text)

    def test_rubbish_arguments_do_not_raise(self):
        text = crashreport.compose("not a type", 17, object())
        self.assertIn(VERSION, text)


class Redaction(QuietStderr, unittest.TestCase):
    """The same rule as the diagnostic zip, applied to the crash log."""

    def setUp(self):
        self.quiet()
        crashreport.clear()
        self.folder = tempfile.mkdtemp(prefix="ps3-crash-test-")
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.addCleanup(crashreport.clear)

    def read(self, path):
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def test_a_mac_address_in_the_message_is_replaced(self):
        info = raise_and_catch(ValueError(f"no reply from {FAKE_MAC}"))
        path = crashreport.write_log(crashreport.compose(*info),
                                     directory=self.folder)
        body = self.read(path)
        self.assertNotIn(FAKE_MAC, body)
        self.assertIn("[MAC-", body)

    def test_an_idps_in_the_message_is_replaced(self):
        info = raise_and_catch(ValueError(f"IDPS {FAKE_IDPS} was rejected"))
        path = crashreport.write_log(crashreport.compose(*info),
                                     directory=self.folder)
        body = self.read(path)
        self.assertNotIn(FAKE_IDPS, body)
        self.assertIn("[IDPS-", body)

    def test_an_identifier_in_a_breadcrumb_is_replaced(self):
        crashreport.note(f"Connected to console, IDPS {FAKE_IDPS}")
        info = raise_and_catch(ValueError("later"))
        path = crashreport.write_log(crashreport.compose(*info),
                                     directory=self.folder)
        self.assertNotIn(FAKE_IDPS, self.read(path))

    def test_the_rest_of_the_report_survives_redaction(self):
        info = raise_and_catch(ValueError(f"MAC {FAKE_MAC} unreachable"))
        path = crashreport.write_log(crashreport.compose(*info),
                                     directory=self.folder)
        body = self.read(path)
        self.assertIn(VERSION, body)
        self.assertIn("Traceback (most recent call last)", body)
        self.assertIn("unreachable", body)

    def test_the_whole_way_through_the_handler(self):
        # The path the user actually takes, rather than write_log by hand.
        info = raise_and_catch(ValueError(f"console at {FAKE_MAC} said no"))
        path = crashreport.handle(*info, settings={"output_dir": self.folder},
                                  dialog=False)
        self.assertIsNotNone(path)
        self.assertNotIn(FAKE_MAC, self.read(path))

    def test_a_broken_redactor_withholds_the_body_rather_than_leaking(self):
        from ps3diag import redaction

        def explode(*args, **kwargs):
            raise RuntimeError("redaction is broken")

        original = redaction.redact
        redaction.redact = explode
        self.addCleanup(setattr, redaction, "redact", original)

        info = raise_and_catch(ValueError(f"IDPS {FAKE_IDPS}"))
        path = crashreport.write_log(crashreport.compose(*info),
                                     directory=self.folder)
        body = self.read(path)
        self.assertNotIn(FAKE_IDPS, body)
        self.assertIn("withheld", body)


class TheLogFile(QuietStderr, unittest.TestCase):
    def setUp(self):
        self.quiet()
        crashreport.clear()
        self.folder = tempfile.mkdtemp(prefix="ps3-crash-test-")
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.addCleanup(crashreport.clear)

    def test_the_path_is_returned(self):
        path = crashreport.write_log("a report", directory=self.folder)
        self.assertIsNotNone(path)
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(os.path.dirname(path), self.folder)

    def test_the_name_carries_a_timestamp(self):
        path = crashreport.write_log("a report", directory=self.folder)
        name = os.path.basename(path)
        self.assertTrue(name.startswith(f"{crashreport.FILE_STEM}-crash-"))
        self.assertTrue(name.endswith(".txt"))
        stamp = name[len(f"{crashreport.FILE_STEM}-crash-"):-len(".txt")]
        self.assertRegex(stamp, r"^\d{8}-\d{6}$")

    def test_the_configured_output_folder_is_preferred(self):
        info = raise_and_catch(ValueError("x"))
        path = crashreport.handle(*info, settings={"output_dir": self.folder},
                                  dialog=False)
        self.assertEqual(os.path.dirname(path), self.folder)

    def test_there_is_always_somewhere_to_fall_back_to(self):
        folders = crashreport.candidate_dirs({})
        self.assertTrue(folders)
        self.assertIn(tempfile.gettempdir(), folders)

    def test_daft_settings_do_not_stop_a_folder_being_found(self):
        self.assertTrue(crashreport.candidate_dirs(None))
        self.assertTrue(crashreport.candidate_dirs("not a dict"))
        self.assertTrue(crashreport.candidate_dirs({"output_dir": None}))

    def test_an_unwritable_folder_falls_through_to_the_next(self):
        missing = os.path.join(self.folder, "no", "\0bad")
        info = raise_and_catch(ValueError("x"))
        path = crashreport.handle(*info, settings={"output_dir": missing},
                                  dialog=False)
        self.assertIsNotNone(path)
        self.assertTrue(os.path.isfile(path))
        self.addCleanup(os.remove, path)

    def test_nowhere_to_write_returns_none_rather_than_raising(self):
        original = crashreport.candidate_dirs
        crashreport.candidate_dirs = lambda settings=None: []
        self.addCleanup(setattr, crashreport, "candidate_dirs", original)
        self.assertIsNone(crashreport.write_log("a report"))


class TheMailtoLink(unittest.TestCase):
    def test_it_is_addressed_to_the_support_address(self):
        parsed = urlparse(crashreport.mailto_url("/tmp/log.txt"))
        self.assertEqual(parsed.scheme, "mailto")
        self.assertEqual(unquote(parsed.path), crashreport.SUPPORT_ADDRESS)
        # Literal, not percent-encoded: not every mail client opens the latter.
        self.assertEqual(parsed.path, crashreport.SUPPORT_ADDRESS)

    def test_the_subject_carries_the_version(self):
        parsed = urlparse(crashreport.mailto_url("/tmp/log.txt"))
        fields = parse_qs(parsed.query)
        self.assertIn(VERSION, fields["subject"][0])

    def test_the_body_points_at_the_log(self):
        parsed = urlparse(crashreport.mailto_url("/tmp/a log.txt"))
        body = parse_qs(parsed.query)["body"][0]
        self.assertIn("/tmp/a log.txt", body)
        self.assertIn("attach", body.lower())

    def test_it_is_url_encoded(self):
        url = crashreport.mailto_url("/tmp/a log.txt")
        # Nothing after the scheme may contain a raw space, newline, or an
        # ampersand that was not put there as a separator.
        query = url.split("?", 1)[1]
        self.assertNotIn(" ", url)
        self.assertNotIn("\n", url)
        self.assertEqual(query.count("&"), 1)
        self.assertIn("%20", url)

    def test_a_body_with_an_ampersand_in_the_path_stays_one_field(self):
        url = crashreport.mailto_url("/tmp/a&b?c=d.txt")
        fields = parse_qs(urlparse(url).query)
        self.assertEqual(sorted(fields), ["body", "subject"])
        self.assertIn("/tmp/a&b?c=d.txt", fields["body"][0])

    def test_it_works_without_a_log_path(self):
        url = crashreport.mailto_url(None)
        self.assertTrue(url.startswith("mailto:"))
        self.assertIn("body=", url)

    def test_it_names_no_host_but_the_mail_address(self):
        # The whole module must never produce an http URL: there is a live
        # console on this network and nothing here may reach anything.
        url = crashreport.mailto_url("/tmp/log.txt")
        self.assertNotIn("http", url.lower())


@unittest.skipUnless(QT, "PySide6 not installed")
class TheDialog(unittest.TestCase):
    def setUp(self):
        application()
        self.folder = tempfile.mkdtemp(prefix="ps3-crash-test-")
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.path = os.path.join(self.folder, "crash.txt")
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write("a report")

    def texts(self, dialog):
        from PySide6.QtWidgets import QLabel
        return " ".join(label.text()
                        for label in dialog.findChildren(QLabel))

    def test_it_builds_headless(self):
        dialog = crashreport.build_dialog(self.path)
        self.addCleanup(dialog.deleteLater)
        self.assertIsNotNone(dialog)

    def test_it_needs_no_parent_window(self):
        dialog = crashreport.build_dialog(self.path)
        self.addCleanup(dialog.deleteLater)
        self.assertIsNone(dialog.parent())

    def test_it_says_what_happened_in_plain_english(self):
        dialog = crashreport.build_dialog(self.path)
        self.addCleanup(dialog.deleteLater)
        self.assertIn("stopped unexpectedly", self.texts(dialog))

    def test_it_says_where_the_log_is(self):
        dialog = crashreport.build_dialog(self.path)
        self.addCleanup(dialog.deleteLater)
        self.assertIn(self.path, self.texts(dialog))

    def test_it_says_nothing_has_been_sent(self):
        dialog = crashreport.build_dialog(self.path)
        self.addCleanup(dialog.deleteLater)
        words = self.texts(dialog)
        self.assertIn("Nothing has been sent", words)
        self.assertIn("attach", words.lower())

    def test_it_carries_the_mailto_link(self):
        dialog = crashreport.build_dialog(self.path)
        self.addCleanup(dialog.deleteLater)
        self.assertIn(crashreport.mailto_url(self.path), self.texts(dialog))

    def test_there_is_a_button_to_open_the_folder(self):
        from PySide6.QtWidgets import QPushButton
        dialog = crashreport.build_dialog(self.path)
        self.addCleanup(dialog.deleteLater)
        labels = [button.text() for button in dialog.findChildren(QPushButton)]
        self.assertTrue(any("folder" in text.lower() for text in labels),
                        labels)

    def test_it_builds_without_a_log_path(self):
        dialog = crashreport.build_dialog(None)
        self.addCleanup(dialog.deleteLater)
        self.assertIn("could not be written", self.texts(dialog))

    def test_without_a_log_path_the_details_are_on_the_screen_instead(self):
        # Nowhere to write the file is the case where the user is back to
        # having nothing to send, so the report has to be copyable from here.
        from PySide6.QtWidgets import QPlainTextEdit
        dialog = crashreport.build_dialog(None, report="ValueError: the detail")
        self.addCleanup(dialog.deleteLater)
        boxes = dialog.findChildren(QPlainTextEdit)
        self.assertEqual(len(boxes), 1)
        self.assertIn("ValueError: the detail", boxes[0].toPlainText())
        self.assertTrue(boxes[0].isReadOnly())

    def test_a_log_path_means_no_details_box(self):
        from PySide6.QtWidgets import QPlainTextEdit
        dialog = crashreport.build_dialog(self.path, report="not needed")
        self.addCleanup(dialog.deleteLater)
        self.assertEqual(dialog.findChildren(QPlainTextEdit), [])

    def test_a_dialog_asked_for_from_a_worker_is_not_built_there(self):
        """Building a QWidget off the GUI thread is fatal in Qt rather than an
        exception, so the check that stops it happening is worth a test: the
        failure mode it prevents cannot be caught once it has happened."""
        from PySide6.QtCore import QThread

        application()
        built = []
        original = crashreport.build_dialog

        def record(path=None, parent=None):
            built.append(QThread.currentThread())
            return original(path, parent)

        crashreport.build_dialog = record
        self.addCleanup(setattr, crashreport, "build_dialog", original)

        done = threading.Event()

        def worker():
            crashreport.show_dialog(self.path)
            done.set()

        thread = threading.Thread(target=worker, name="worker-dialog")
        thread.start()
        done.wait(5)
        thread.join()
        # Posted, not built: the worker returns without a dialog of its own.
        self.assertEqual(built, [])

    def test_no_application_is_created_from_a_worker_thread(self):
        """Creating a QApplication anywhere but the main thread is fatal in Qt.

        Checked by asking a worker while pretending none exists: it must decline
        rather than build one. The real case is a thread that fails during
        start-up, before the application has been constructed.
        """
        from PySide6.QtWidgets import QApplication

        application()
        original = QApplication.instance
        QApplication.instance = staticmethod(lambda: None)
        self.addCleanup(setattr, QApplication, "instance", original)

        answers = []
        thread = threading.Thread(
            target=lambda: answers.append(crashreport.ensure_application()))
        thread.start()
        thread.join()
        self.assertEqual(answers, [None])

    def test_it_opens_no_external_link_but_the_mail_one(self):
        from PySide6.QtWidgets import QLabel
        dialog = crashreport.build_dialog(self.path)
        self.addCleanup(dialog.deleteLater)
        for label in dialog.findChildren(QLabel):
            if label.openExternalLinks():
                self.assertIn("mailto:", label.text())
                self.assertNotIn("http", label.text().lower())


class TheHandlerNeverRaises(QuietStderr, unittest.TestCase):
    """The point of the whole file. Every one of these would otherwise be a
    crash inside the crash handler."""

    def setUp(self):
        self.quiet()
        crashreport.clear()
        self.folder = tempfile.mkdtemp(prefix="ps3-crash-test-")
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.addCleanup(crashreport.clear)
        self.settings = {"output_dir": self.folder}

    def handle(self, *args, **kwargs):
        kwargs.setdefault("settings", self.settings)
        kwargs.setdefault("dialog", False)
        return crashreport.handle(*args, **kwargs)

    def test_an_ordinary_exception_produces_a_log(self):
        path = self.handle(*raise_and_catch(ValueError("something broke")))
        self.assertIsNotNone(path)
        with open(path, encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn("ValueError: something broke", body)
        self.assertIn(VERSION, body)

    def test_an_exception_with_no_traceback(self):
        self.assertIsNotNone(self.handle(ValueError, ValueError("x"), None))

    def test_nothing_at_all(self):
        self.assertIsNotNone(self.handle(None, None, None))

    def test_no_arguments(self):
        self.assertIsNotNone(self.handle())

    def test_an_exception_that_cannot_be_described(self):
        path = self.handle(*raise_and_catch(Unprintable()))
        self.assertIsNotNone(path)

    def test_arguments_of_the_wrong_type_entirely(self):
        self.assertIsNotNone(self.handle("nonsense", 42, ["a", "list"]))

    def test_a_dialog_that_cannot_be_built(self):
        def explode(*args, **kwargs):
            raise RuntimeError("no display, no Qt, no anything")

        original = crashreport.build_dialog
        crashreport.build_dialog = explode
        self.addCleanup(setattr, crashreport, "build_dialog", original)
        path = crashreport.handle(*raise_and_catch(ValueError("x")),
                                  settings=self.settings, dialog=True)
        # The dialog failed; the file is what matters and it is still there.
        self.assertIsNotNone(path)
        self.assertTrue(os.path.isfile(path))

    def test_a_qapplication_that_cannot_be_created(self):
        def explode(*args, **kwargs):
            raise RuntimeError("no display")

        original = crashreport.ensure_application
        crashreport.ensure_application = explode
        self.addCleanup(setattr, crashreport, "ensure_application", original)
        self.assertIsNotNone(
            crashreport.handle(*raise_and_catch(ValueError("x")),
                               settings=self.settings, dialog=True))

    def test_a_writer_that_explodes(self):
        def explode(*args, **kwargs):
            raise OSError("the disk is gone")

        original = crashreport.write_log
        crashreport.write_log = explode
        self.addCleanup(setattr, crashreport, "write_log", original)
        self.assertIsNone(self.handle(*raise_and_catch(ValueError("x"))))

    def test_a_composer_that_explodes(self):
        def explode(*args, **kwargs):
            raise RuntimeError("cannot compose")

        original = crashreport.compose
        crashreport.compose = explode
        self.addCleanup(setattr, crashreport, "compose", original)
        self.assertIsNotNone(self.handle(*raise_and_catch(ValueError("x"))))

    def test_a_stderr_that_is_gone(self):
        # Happens under pythonw and in a frozen build with no console.
        original = sys.stderr
        sys.stderr = None
        self.addCleanup(setattr, sys, "stderr", original)
        self.assertIsNotNone(self.handle(*raise_and_catch(ValueError("x"))))

    def test_a_crash_inside_the_handler_does_not_recurse(self):
        calls = []

        def reentrant(text, *args, **kwargs):
            calls.append(text)
            # What a fault inside the handler would do if the hook were still
            # live: come straight back round for another go.
            crashreport.handle(*raise_and_catch(ValueError("second")),
                               settings=self.settings, dialog=False)
            return None

        original = crashreport.write_log
        crashreport.write_log = reentrant
        self.addCleanup(setattr, crashreport, "write_log", original)
        crashreport.handle(*raise_and_catch(ValueError("first")),
                           settings=self.settings, dialog=False)
        self.assertEqual(len(calls), 1)

    def test_the_guard_is_released_afterwards(self):
        self.handle(*raise_and_catch(ValueError("one")))
        self.assertIsNotNone(self.handle(*raise_and_catch(ValueError("two"))))


class TheHooks(QuietStderr, unittest.TestCase):
    def setUp(self):
        self.quiet()
        crashreport.clear()
        self.folder = tempfile.mkdtemp(prefix="ps3-crash-test-")
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.addCleanup(crashreport.clear)
        self.previous = (sys.excepthook, threading.excepthook)

    def restore(self):
        sys.excepthook, threading.excepthook = self.previous

    def install(self):
        self.addCleanup(self.restore)
        return crashreport.install(settings={"output_dir": self.folder},
                                   dialog=False)

    def test_install_returns_the_hooks_it_replaced(self):
        previous = self.install()
        self.assertEqual(previous, self.previous)
        self.assertIsNot(sys.excepthook, self.previous[0])
        self.assertIsNot(threading.excepthook, self.previous[1])

    def test_the_excepthook_writes_a_log(self):
        self.install()
        sys.excepthook(*raise_and_catch(ValueError("hooked")))
        written = os.listdir(self.folder)
        self.assertEqual(len(written), 1, written)
        with open(os.path.join(self.folder, written[0]),
                  encoding="utf-8") as handle:
            self.assertIn("ValueError: hooked", handle.read())

    def test_a_keyboard_interrupt_is_not_a_crash(self):
        called = []
        sys.excepthook = lambda *args: called.append(args)
        self.previous = (sys.excepthook, threading.excepthook)
        self.install()
        sys.excepthook(*raise_and_catch(KeyboardInterrupt()))
        self.assertEqual(os.listdir(self.folder), [])
        self.assertEqual(len(called), 1)

    def test_the_excepthook_survives_being_handed_rubbish(self):
        # issubclass raises TypeError on anything that is not a class, and the
        # hook is a public attribute anyone can call with anything.
        self.install()
        sys.excepthook("not a class", 17, None)
        self.assertEqual(len(os.listdir(self.folder)), 1)

    def test_the_thread_hook_survives_an_args_object_missing_its_fields(self):
        self.install()

        class Bare:
            pass

        threading.excepthook(Bare())
        self.assertEqual(len(os.listdir(self.folder)), 1)

    def test_a_worker_thread_exception_is_reported(self):
        self.install()

        def fail():
            raise ValueError("failed on a worker")

        thread = threading.Thread(target=fail, name="worker-1")
        thread.start()
        thread.join()
        written = os.listdir(self.folder)
        self.assertEqual(len(written), 1, written)
        with open(os.path.join(self.folder, written[0]),
                  encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn("failed on a worker", body)
        self.assertIn("worker-1", body)

    @unittest.skipUnless(QT, "PySide6 not installed")
    def test_an_exception_raised_inside_a_slot_reaches_the_hook(self):
        """The binding's behaviour, not the API's promise, so it is checked.

        PySide6 6.x catches a Python exception at the C++ boundary and hands it
        to sys.excepthook rather than losing it. If a future version stops
        doing that, this fails and somebody goes and installs whatever the
        replacement is; without this test the failure would be silent.
        """
        from PySide6.QtCore import QObject, QTimer, Signal

        application()
        self.install()

        class Emitter(QObject):
            fired = Signal()

        emitter = Emitter()
        emitter.fired.connect(self._boom)
        QTimer.singleShot(0, emitter.fired.emit)
        QTimer.singleShot(250, QApplication.instance().quit)
        QApplication.instance().exec()

        written = os.listdir(self.folder)
        self.assertEqual(len(written), 1, written)
        with open(os.path.join(self.folder, written[0]),
                  encoding="utf-8") as handle:
            self.assertIn("raised inside a slot", handle.read())

    def _boom(self):
        raise ValueError("raised inside a slot")


class TheEntryPoint(unittest.TestCase):
    def test_it_installs_the_hook_before_importing_the_shell(self):
        path = os.path.join(ROOT, "ps3-tools.py")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        install = source.index("crashreport.install()")
        shell = source.index("from ps3tools.shell.app import main")
        self.assertLess(install, shell,
                        "a crash while the shell is importing must be caught")


class Layering(unittest.TestCase):
    def test_it_does_not_import_the_write_client(self):
        import ast
        path = os.path.join(ROOT, "ps3tools", "crashreport.py")
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
        offences = [name for name in names
                    if name.startswith("ps3tools.patching")]
        self.assertEqual(offences, [])

    def test_it_imports_no_network_client(self):
        # The one rule that matters more than the rest: nothing about crash
        # reporting may send anything anywhere.
        import ast
        path = os.path.join(ROOT, "ps3tools", "crashreport.py")
        with open(path, encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module)
        forbidden = ("socket", "http", "urllib.request", "ftplib",
                     "smtplib", "ps3diag.transport", "requests")
        offences = [name for name in names if name.startswith(forbidden)]
        self.assertEqual(offences, [], f"crash reporting must not: {offences}")


if __name__ == "__main__":
    unittest.main()
