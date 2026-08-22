"""Semver-ish comparison. Major-lock resolution leans on this constantly and
the stdlib has no semver, so it is the one place versions become comparable."""
import re

from tavern_shared.mods.errors import ModManagerError

# int() is looser than C#'s int.TryParse, which is what TavernLib parses the
# same version strings with: int("0_5") is 5 and int("١٢") is 12, both of which
# TryParse refuses. Neither side throws on an unparseable version - they skip it
# with a warning - so accepting more here doesn't error, it silently gives the
# launcher a different version set (and a different highest()/major) than the
# host resolving the same index. ASCII digits only is the intersection.
_SEGMENT = re.compile(r"[0-9]+")


def _parse_version(v):
    """Major-lock resolution compares versions constantly and the stdlib has no
    semver, so this is the one place versions become comparable.
    Splits "MAJOR.MINOR.PATCH" into an int tuple so ordering is numeric, not
    lexical. The trap this closes is that "1.10.0" < "1.9.0" as strings but
    1.10 > 1.9 as versions.

    Pre-release/build metadata ("1.2.0-rc1", "1.2.0+build") is NOT supported: a
    version that isn't exactly three numeric segments is rejected outright
    (below), so ordering never has to guess how to rank a pre-release tag."""
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
    return tuple(int(p) for p in parts)


def _major(v):
    return _parse_version(v)[0]
