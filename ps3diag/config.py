"""The handful of settings that are remembered between runs.

Kept in the user's own application data folder. It used to sit beside the exe,
on the reasoning that deleting the exe should leave nothing behind, and that
turned out to cost more than it was worth: a folder the program cannot write
to, a Downloads folder that gets swept, and an exe moved to a new place all
lost somebody their saved address and their remembered game lists. A file in
APPDATA survives all three.

A settings file already beside the exe is moved on the first run that finds
one, so upgrading keeps everything.

There is one function that says where the file is. Anything working it out for
itself is how two parts of one program came to disagree about which file they
were reading.
"""

import json
import os
import shutil
import sys

from . import APP_NAME

FILENAME = f"{APP_NAME}.json"

#: The folder under APPDATA, or under ~/.config away from Windows. Named for
#: the program the user runs rather than for this package: ps3diag ships
#: inside PS3 Tools, and two folders for one program is one too many.
USER_FOLDER = "ps3-tools"

DEFAULTS = {
    "ip": "",
    # Set by the application shell rather than by the diagnostic. Kept here
    # because there is one settings file beside the exe and one module that
    # knows where it lives; a second one would be a second file to lose.
    "theme": "system",
    "window_geometry": "",
    "last_screen": "",
    "include_identifiers": False,
    "categories": {},
    "http_timeout": 20.0,
    "ftp_timeout": 30.0,
    "run_timeout": 900.0,
    "output_dir": "",
}


def app_dir():
    """The folder the user sees the program in. Under PyInstaller --onefile that
    is where the exe sits, not the temporary extraction directory."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def bundle_dir():
    """Where files bundled into the exe are unpacked at run time."""
    return getattr(sys, "_MEIPASS", app_dir())


def user_dir():
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = (os.environ.get("XDG_CONFIG_HOME")
                or os.path.join(os.path.expanduser("~"), ".config"))
    return os.path.join(base, USER_FOLDER)


def settings_path(filename=None):
    """Where settings live. The one answer to that question."""
    return os.path.join(user_dir(), filename or FILENAME)


def legacy_path(filename=None):
    """Where they used to live, beside the exe."""
    return os.path.join(app_dir(), filename or FILENAME)


def migrate(filename=None):
    """Move a settings file left beside the exe into the user folder.

    Moved rather than copied, so there is exactly one file afterwards and no
    chance of a later run reading the stale one. Does nothing when the user
    folder already has a file: that one is newer by definition, and quietly
    overwriting somebody's current settings with an old copy is the one
    outcome here that cannot be undone.

    Never raises. Failing to migrate costs a saved address, and a program that
    will not start costs everything.
    """
    target = settings_path(filename)
    source = legacy_path(filename)
    if os.path.abspath(source) == os.path.abspath(target):
        return None
    if not os.path.isfile(source) or os.path.exists(target):
        return None
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.move(source, target)
        return target
    except OSError:
        return None


def candidate_paths(filename=None):
    """Every place a settings file may be, newest first.

    The user folder comes first now. The old location is still read so that an
    upgrade finds the settings even if the move could not be made, on a
    read-only folder for instance.
    """
    return [settings_path(filename), legacy_path(filename)]


def desktop_dir():
    """Where the zip goes. The Desktop if there is one, the home folder if not,
    because a user told to "send me the file on your desktop" will not go
    looking anywhere else."""
    home = os.path.expanduser("~")
    for name in ("Desktop", "desktop"):
        candidate = os.path.join(home, name)
        if os.path.isdir(candidate):
            return candidate
    if sys.platform == "win32":
        profile = os.environ.get("USERPROFILE")
        if profile and os.path.isdir(os.path.join(profile, "Desktop")):
            return os.path.join(profile, "Desktop")
    return home


def load():
    migrate()
    settings = dict(DEFAULTS)
    for path in candidate_paths():
        try:
            with open(path, encoding="utf-8") as handle:
                stored = json.load(handle)
        except (OSError, ValueError):
            continue
        if isinstance(stored, dict):
            for key, value in stored.items():
                if key in DEFAULTS:
                    settings[key] = value
            settings["_path"] = path
            break
    return settings


def save(settings):
    """Returns the path written, or None. Never raises: failing to remember an
    IP address is not worth interrupting a run over."""
    payload = {key: value for key, value in settings.items()
               if key in DEFAULTS}
    for path in candidate_paths():
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
            return path
        except OSError:
            continue
    return None
