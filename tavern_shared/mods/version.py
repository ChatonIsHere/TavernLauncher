"""Semver-ish comparison. Major-lock resolution leans on this constantly and
the stdlib has no semver, so it is the one place versions become comparable."""
from tavern_shared.mods.errors import ModManagerError


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
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        raise ModManagerError(
            f"Version {v!r} has a non-numeric segment; only digits are allowed.")


def _major(v):
    return _parse_version(v)[0]
