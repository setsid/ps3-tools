"""Where screens announce themselves.

The launcher reads this and nothing else, so a new tool is a new module plus
one register() call. Nothing in the launcher enumerates the screens by name.

Two things are decided here rather than there: the order the cards come in,
and which section of the home screen each one belongs to. A screen says which
section it is in and says nothing about where the section goes, so a new tool
cannot rearrange the page by accident.
"""

from collections import namedtuple

#: One section of the home screen. blurb is the line under the heading, and
#: is allowed to be empty for a section that needs no explaining.
Group = namedtuple("Group", "key heading blurb")

#: The sections, in the order they appear down the page.
#:
#: The fixes come first because they are why somebody downloads this. They are
#: also the only part of the program that writes to a console, and a heading
#: that separates them from everything else is the plainest way of saying so
#: on the page rather than only inside each tool.
GROUPS = (
    Group("fixes", "Game fixes",
          "Each one repairs a fault in one game's own files. Nothing else on "
          "the console is touched, and the original is backed up first."),
    Group("tools", "Console tools",
          "Everything else, and everything that only reads."),
)

#: What a screen that says nothing is in. The published Screen class carries
#: the same string as its default; test_launcher checks the two agree, because
#: a default that drifted would put every new tool in no section at all.
DEFAULT_GROUP = "tools"

_SCREENS = []


def groups():
    return GROUPS


def group_for(key):
    for group in GROUPS:
        if group.key == key:
            return group
    return None


def group_of(screen_class):
    """The section a screen belongs to, as a Group."""
    return group_for(getattr(screen_class, "group", "") or DEFAULT_GROUP)


def _group_index(screen_class):
    key = getattr(screen_class, "group", "") or DEFAULT_GROUP
    for index, group in enumerate(GROUPS):
        if group.key == key:
            return index
    return len(GROUPS)


def register(screen_class):
    """Decorator or plain call. Returns the class, so it can be either."""
    for attribute in ("key", "title", "blurb", "tile"):
        if not getattr(screen_class, attribute, ""):
            raise ValueError(
                f"{screen_class.__name__} has no {attribute}; the launcher "
                f"cannot draw a card for it")
    wanted = getattr(screen_class, "group", "") or DEFAULT_GROUP
    if group_for(wanted) is None:
        # Refused rather than filed under a section of its own. A typo here
        # would otherwise show as a card that is simply missing from the page,
        # which is the hardest kind of fault to go looking for.
        raise ValueError(
            f"{screen_class.__name__} says it is in the {wanted!r} section, "
            f"which does not exist; the sections are "
            f"{', '.join(group.key for group in GROUPS)}")
    if any(existing.key == screen_class.key for existing in _SCREENS):
        raise ValueError(f"two screens both claim the key "
                         f"{screen_class.key!r}")
    _SCREENS.append(screen_class)
    return screen_class


def screens():
    """Registered screens, in card order: by section, then within it."""
    return sorted(_SCREENS,
                  key=lambda item: (_group_index(item), item.order,
                                    item.title))


def grouped(items=None):
    """[(Group, [screen, ...])] in section order, empty sections left out.

    A section with nothing in it is absent rather than drawn with a heading
    over a hole. A build with only the fixes in it should look like a program
    that fixes games, instead of one whose second half failed to load.
    """
    items = screens() if items is None else list(items)
    out = []
    for group in GROUPS:
        found = [item for item in items if group_of(item) is group]
        if found:
            out.append((group, found))
    return out


def screen_for(key):
    for item in _SCREENS:
        if item.key == key:
            return item
    return None


def clear():
    """Tests only."""
    _SCREENS.clear()
