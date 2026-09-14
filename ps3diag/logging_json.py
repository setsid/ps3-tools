"""One JSON object per line, written beside the zip.

Deliberately dumb: no handlers, no configuration, no logging module. A helper
reading this file wants a plain record of what was asked for and what came
back, and the logging module's own formatting would only get in the way.

Bodies never reach here. Only the shape of a response is logged (status, byte
count, content type), so the log can be pasted into a forum post without
anything being redacted out of it first.
"""

import json
import os
import threading
import time

# Anything whose name hints at an identifier is dropped rather than redacted.
# A log is the one file nobody re-reads before pasting, so it holds no
# candidates for redaction at all.
BANNED_KEYS = ("idps", "psid", "mac", "account", "email", "user", "pass",
               "online_id", "nickname", "token")


def _clean(fields):
    out = {}
    for key, value in fields.items():
        lowered = key.lower()
        if any(banned in lowered for banned in BANNED_KEYS):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            out[key] = value
        else:
            out[key] = repr(value)
    return out


class RunLog:
    def __init__(self, path=None):
        self.path = path
        self.records = []
        self._lock = threading.Lock()
        self._handle = None
        if path:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            self._handle = open(path, "w", encoding="utf-8", newline="\n")

    def event(self, kind, **fields):
        record = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "event": kind,
        }
        record.update(_clean(fields))
        with self._lock:
            self.records.append(record)
            if self._handle:
                self._handle.write(json.dumps(record, sort_keys=True) + "\n")
                self._handle.flush()
        return record

    def close(self):
        with self._lock:
            if self._handle:
                self._handle.close()
                self._handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False
