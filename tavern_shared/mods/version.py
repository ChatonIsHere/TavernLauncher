"""Version parsing: the one place versions become comparable."""
import re

from tavern_shared.mods.errors import ModManagerError

# ASCII digits only: int() also takes "0_5" and non-ASCII digits, which C#'s
# int.TryParse refuses. Both sides skip unparseable versions, so accepting more
# here would quietly give the launcher a different version set than the host.
_SEGMENT = re.compile(r"[0-9]+")
_SEGMENT_MAX = 2147483647


def _parse_version(v):
    """"MAJOR.MINOR.PATCH" as an int tuple, so "1.10.0" orders above "1.9.0".
    Exactly three ASCII-digit segments, each within Int32; no pre-release or
    build metadata, so ordering never guesses. The grammar is the twin of
    TavernLib's ModVersion: both sides must accept the same strings."""
    if not isinstance(v, str):
        raise ModManagerError(f"Version must be a string, got {v!r}")
    parts = v.strip().split(".")
    if len(parts) != 3:
        raise ModManagerError(
            f"Version {v!r} isn't MAJOR.MINOR.PATCH: exactly three numeric "
            f"segments are required (no pre-release/build metadata).")
    if not all(_SEGMENT.fullmatch(p) for p in parts):
        raise ModManagerError(
            f"Version {v!r} has a non-numeric segment; only digits are allowed.")
    out = tuple(int(p) for p in parts)
    # TavernLib parses segments as Int32; anything wider would order here and
    # be skipped there.
    if any(n > _SEGMENT_MAX for n in out):
        raise ModManagerError(
            f"Version {v!r} has a segment larger than 2147483647.")
    return out


def _major(v):
    return _parse_version(v)[0]
