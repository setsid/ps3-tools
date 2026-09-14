"""The handful of settings that are remembered between runs.

Kept beside the exe, which is what someone who has been handed a single file
expects: delete the exe and nothing of it is left behind. Program Files is not
writable by a normal user though, so a failure to write there falls back to the
usual per-user location rather than losing the setting silently.
"""

import json
import os
import sys

from . import APP_NAME

FILENAME = f"{APP_NAME}.json"

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
    return os.path.join(base, APP_NAME)


def candidate_paths():
    return [os.path.join(app_dir(), FILENAME),
            os.path.join(user_dir(), FILENAME)]


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
