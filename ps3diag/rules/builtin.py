"""Every rule ps3-diag ships with, gathered into one import.

The rules themselves live in modules grouped by what they read, because a rule
about split game files and a rule about console temperature have nothing to say
to each other. This module exists so that there is still one name to import
when what you want is "all of them", and so that the registry is populated by
the time anybody calls run_rules.

Each name below is also the function itself, still callable with a single
ArtefactSet, which is how the tests exercise one rule at a time.
"""

from .games_rules import (iso_folders_nested, iso_too_small, iso_zero_bytes,
                          split_iso_missing_part)
from .storage_rules import (file_too_big_for_fat32, folder_games_on_ntfs,
                            internal_space_low_for_ps2, usb_not_mounted)
from .system_rules import (cobra_disabled_with_isos, manager_plugin_conflict,
                           temperature_high)

__all__ = [
    "usb_not_mounted",
    "iso_folders_nested",
    "folder_games_on_ntfs",
    "split_iso_missing_part",
    "file_too_big_for_fat32",
    "internal_space_low_for_ps2",
    "manager_plugin_conflict",
    "cobra_disabled_with_isos",
    "temperature_high",
    "iso_zero_bytes",
    "iso_too_small",
]
