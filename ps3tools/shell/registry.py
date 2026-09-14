"""Where screens announce themselves.

The launcher reads this and nothing else, so a new tool is a new module plus
one register() call. Nothing in the launcher enumerates the screens by name.
"""

_SCREENS = []


def register(screen_class):
    """Decorator or plain call. Returns the class, so it can be either."""
    for attribute in ("key", "title", "blurb", "tile"):
        if not getattr(screen_class, attribute, ""):
            raise ValueError(
                f"{screen_class.__name__} has no {attribute}; the launcher "
                f"cannot draw a card for it")
    if any(existing.key == screen_class.key for existing in _SCREENS):
        raise ValueError(f"two screens both claim the key "
                         f"{screen_class.key!r}")
    _SCREENS.append(screen_class)
    return screen_class


def screens():
    """Registered screens, in card order."""
    return sorted(_SCREENS, key=lambda item: (item.order, item.title))


def screen_for(key):
    for item in _SCREENS:
        if item.key == key:
            return item
    return None


def clear():
    """Tests only."""
    _SCREENS.clear()
