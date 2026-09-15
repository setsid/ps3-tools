"""Named consoles, and the state that belongs to each of them.

Somebody with two PS3s on one network was being shown one console's remembered
games against the other, because everything per-console was keyed by IP address
and both consoles took whatever the router handed them. A lease expires, the
addresses swap, and the program confidently reports the wrong machine.

So a console is a profile: a name the user chose, an address that may change
under it, and its own corner of the settings file. The profile's id is what
per-console state is filed under, and an id is never reused or derived from the
address.

Pure data. No Qt, no network, nothing that happens on import. The settings
dictionary is the shell's; this module reads and writes it and nothing else.
"""

import re
import uuid

#: Where the whole lot lives in the settings dictionary.
CONSOLES_KEY = "consoles"
CURRENT_KEY = "current_console"

#: The address of the console last used. Kept at the top level as well as in
#: the profile, because the shell and the older settings files both read it,
#: and a saved address that an upgrade cannot find is a saved address lost.
HOST_KEY = "host"

#: What a profile is called when the user has not named it.
DEFAULT_NAME = "My PS3"

#: Long enough to say which console, short enough for the bar it sits in.
MAX_NAME = 40


def _profiles(settings):
    stored = (settings or {}).get(CONSOLES_KEY)
    return stored if isinstance(stored, dict) else {}


def _clean_name(name):
    """A name fit to show. Control characters and runs of space go."""
    text = re.sub(r"\s+", " ", str(name or "")).strip()
    return text[:MAX_NAME]


def all_profiles(settings):
    """Every console, as {id: {"name", "host"}}. Newest last."""
    out = {}
    for key, value in _profiles(settings).items():
        if not isinstance(value, dict):
            continue
        out[str(key)] = {
            "name": _clean_name(value.get("name")) or DEFAULT_NAME,
            "host": str(value.get("host") or "").strip(),
            # A console gets a profile the moment there is something to
            # remember about it, so the state has somewhere to live. It is
            # only "saved" once the user has named it, which is what the Save
            # button is for.
            "named": bool(value.get("named")),
        }
    return out


def current_id(settings):
    """The profile in use, or "" when there is not one yet."""
    chosen = str((settings or {}).get(CURRENT_KEY) or "")
    return chosen if chosen in _profiles(settings) else ""


def current(settings):
    """The profile in use, or None."""
    return all_profiles(settings).get(current_id(settings))


def ensure(settings, host="", name=""):
    """The id of the profile for this address, making one if needed.

    An upgrade from a settings file that only ever knew one address arrives
    here: the saved host becomes the first profile, so nobody is asked to set
    up something they already had.
    """
    if settings is None:
        return ""
    host = str(host or settings.get(HOST_KEY) or "").strip()
    existing = current_id(settings)
    if existing:
        if host:
            settings[CONSOLES_KEY][existing]["host"] = host
            settings[HOST_KEY] = host
        return existing
    for key, profile in all_profiles(settings).items():
        if host and profile["host"] == host:
            settings[CURRENT_KEY] = key
            return key
    return add(settings, host=host, name=name)


def named_id(settings, host):
    """The id of a saved console at this address, or ""."""
    key = for_host(settings, host)
    if key and all_profiles(settings)[key]["named"]:
        return key
    return ""


def adopt_legacy(settings):
    """Turn a settings file from before profiles into the first console.

    Only for a file that already had an address and a remembered game list
    against it. A console somebody merely connected to once is not adopted:
    saving a console is a thing the user does, and inventing one for them
    would take the Save button away before they had pressed it.
    """
    if settings is None or all_profiles(settings):
        return ""
    host = str(settings.get(HOST_KEY) or "").strip()
    if not host:
        return ""
    legacy = settings.get("updates_seen")
    if not isinstance(legacy, dict) or host not in legacy:
        return ""
    return add(settings, host=host)


def is_saved(settings, host):
    """Whether this address belongs to a console the user has named.

    A profile on its own is not enough. One is made as soon as there is state
    to keep, which happens without the user doing anything; saving is the
    deliberate act of giving it a name.
    """
    key = for_host(settings, host)
    return bool(key) and bool(all_profiles(settings)[key]["named"])


def for_host(settings, host):
    """The profile id for an address, without inventing or moving one.

    ensure() is for "this is the console I am using": it will attach a new
    address to the current profile, which is what happens when a console
    comes back on a different lease. This is the narrower question, "do I know
    this address", and it answers "" rather than claiming one.
    """
    host = str(host or "").strip()
    if not host:
        return ""
    for key, profile in all_profiles(settings).items():
        if profile["host"] == host:
            return key
    return ""


def add(settings, host="", name=""):
    """Make a profile and select it. Returns its id."""
    if settings is None:
        return ""
    profiles = dict(_profiles(settings))
    key = uuid.uuid4().hex[:12]
    profiles[key] = {"name": _clean_name(name) or _next_name(settings),
                     "host": str(host or "").strip(),
                     "named": bool(_clean_name(name))}
    settings[CONSOLES_KEY] = profiles
    settings[CURRENT_KEY] = key
    if host:
        settings[HOST_KEY] = str(host).strip()
    return key


def _next_name(settings):
    """"My PS3", then "My PS3 2", and so on. Never a duplicate."""
    taken = {profile["name"] for profile in all_profiles(settings).values()}
    if DEFAULT_NAME not in taken:
        return DEFAULT_NAME
    index = 2
    while f"{DEFAULT_NAME} {index}" in taken:
        index += 1
    return f"{DEFAULT_NAME} {index}"


def rename(settings, key, name):
    """Give a console a name. Returns the name that was actually stored."""
    profiles = _profiles(settings)
    if key not in profiles:
        return ""
    cleaned = _clean_name(name) or _next_name(settings)
    profiles[key]["name"] = cleaned
    profiles[key]["named"] = True
    return cleaned


def select(settings, key):
    """Switch to a console. Returns its address, or "" if there is no such
    profile."""
    if key not in _profiles(settings):
        return ""
    settings[CURRENT_KEY] = key
    host = str(_profiles(settings)[key].get("host") or "").strip()
    settings[HOST_KEY] = host
    return host


def set_host(settings, key, host):
    """Record the address a console is answering on now."""
    profiles = _profiles(settings)
    if key not in profiles:
        return ""
    host = str(host or "").strip()
    profiles[key]["host"] = host
    if key == current_id(settings):
        settings[HOST_KEY] = host
    return host


def forget(settings, key):
    """Remove a console and everything filed under it."""
    profiles = dict(_profiles(settings))
    if key not in profiles:
        return False
    del profiles[key]
    settings[CONSOLES_KEY] = profiles
    state = (settings or {}).get(STATE_KEY)
    if isinstance(state, dict):
        state.pop(key, None)
    if current_id(settings) == key:
        settings[CURRENT_KEY] = next(iter(profiles), "")
        settings[HOST_KEY] = (profiles.get(settings[CURRENT_KEY], {})
                              .get("host", "") if profiles else "")
    return True


#: Per-console state lives here, filed by profile id rather than by address.
STATE_KEY = "console_state"


def state(settings, key=None):
    """The dictionary a console's own state is kept in. Made if needed.

    Everything that is about one console rather than about the program belongs
    in here: what its games were last time, and whatever else comes later.
    Anything filed by address instead goes wrong the moment a lease changes.
    """
    if settings is None:
        return {}
    key = key or current_id(settings)
    if not key:
        return {}
    store = settings.get(STATE_KEY)
    if not isinstance(store, dict):
        store = {}
        settings[STATE_KEY] = store
    entry = store.get(key)
    if not isinstance(entry, dict):
        entry = {}
        store[key] = entry
    return entry
