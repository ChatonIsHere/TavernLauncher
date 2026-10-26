"""The mod manager's error type, warn channel, version grammar and host config."""
import re
import sys


class ModManagerError(Exception):
    """Raised with a message the UI can show verbatim."""


def _warn(msg):
    try:
        print(f"[modmanager] {msg}", file=sys.stderr)
    except Exception:
        pass


# ASCII digits only, as C#'s int.TryParse: int() also takes "0_5" and
# non-ASCII digits, which would give the launcher a different version set
# than the host. Twin of TavernLib's ModVersion.
_SEGMENT = re.compile(r"[0-9]+")
_SEGMENT_MAX = 2147483647


def _parse_version(v):
    """"MAJOR.MINOR.PATCH" as an int tuple, each segment within Int32."""
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
    if any(n > _SEGMENT_MAX for n in out):
        raise ModManagerError(
            f"Version {v!r} has a segment larger than 2147483647.")
    return out


def _major(v):
    return _parse_version(v)[0]


# Which app's config file the mod manager reads and writes; each app's main.py
# wires its own at startup.
_load = _save = None


def set_config_accessors(load, save):
    global _load, _save
    _load, _save = load, save


def load_cfg():
    return _load()


def save_cfg(cfg):
    _save(cfg)
