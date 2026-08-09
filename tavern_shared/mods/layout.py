"""The on-disk install layout: Mods/<id>/ and its record, UserLibs/ sidecars,
staging paths. This shape is a contract any other installer of these mods
must match byte-for-byte -- keep it stable."""
import os

from tavern_shared.mods.errors import ModManagerError


def _tavern_data_dir():
    """Same shared appdata folder both mono files already use for their own
    config (%AppData%/TheModdingTavern) - a small, dependency-free copy rather
    than a set_helpers()-borrowed one, since it's a pure function of the
    environment, not host state. Home for the client mod cache."""
    base = os.environ.get("APPDATA", os.path.join(os.path.expanduser("~"), "AppData", "Roaming"))
    return os.path.join(base, "TheModdingTavern")


def _safe_basename(name):
    """Last-line path-traversal guard, applied to mod.filename and every
    library_dependencies.filename right before it touches disk. Raises if name
    has any directory part, contains '/' or '\\', or is '.' / '..'.

    Re-checked here at write time regardless of any upstream validation: a
    manifest fetched from an unreviewed third-party repo can't be trusted to
    carry a clean filename, so install_mod/install_library_dependency verify it
    themselves."""
    if not name or name in (".", ".."):
        raise ModManagerError(f"Unsafe filename {name!r}.")
    if "/" in name or "\\" in name:
        raise ModManagerError(f"Filename {name!r} must not contain a path separator.")
    if os.path.basename(name) != name:
        raise ModManagerError(f"Filename {name!r} must be a bare basename.")
    return name


# Installation

# The record filename inside each mod folder. MUST be exactly "manifest.json":
# recent MelonLoader only recurses into a Mods/ subfolder that contains a file by
# this name (existence check only, content unread), so this record is also the
# marker that makes the folder load. See the module docstring.
RECORD_NAME = "manifest.json"


# A disabled mod keeps its folder and files but has its record renamed to this,
# so the folder no longer contains a "manifest.json" and MelonLoader stops
# scanning it (a first-level Mods/ subfolder with no manifest.json isn't loaded),
# while this module still finds the folder by <id> and reads the record. Enabling
# renames it back. The folder is NOT renamed, so the by-id path stays stable and
# the folder isn't hidden from listing the way a dot-prefixed one would be.
DISABLED_RECORD_NAME = "manifest.disabled.json"


def _mods_base(game_dir):
    # The Mods/ path WITHOUT creating it, for building paths in read-only code
    # (status/listing) that shouldn't have the side effect of making folders.
    return os.path.join(game_dir, "Mods")


def _mods_dir(game_dir):
    d = _mods_base(game_dir)
    os.makedirs(d, exist_ok=True)
    return d


def _userlibs_dir(game_dir):
    d = os.path.join(game_dir, "UserLibs")
    os.makedirs(d, exist_ok=True)
    return d


def _mod_dir_path(game_dir, mod_id):
    # A mod's own folder: Mods/<id>/. mod_id is guarded as a single path segment
    # (an id from an unreviewed repo can't be trusted to be path-safe).
    return os.path.join(_mods_base(game_dir), _safe_basename(mod_id))


def _mod_record_path(game_dir, mod_id):
    # The record + MelonLoader marker inside the mod's folder (enabled state).
    return os.path.join(_mod_dir_path(game_dir, mod_id), RECORD_NAME)


def _disabled_record_path(game_dir, mod_id):
    # Where the record lives while the mod is disabled (no manifest.json present,
    # so MelonLoader skips the folder).
    return os.path.join(_mod_dir_path(game_dir, mod_id), DISABLED_RECORD_NAME)


def _staging_path(game_dir, mod_id):
    # Where a mod is assembled before being swapped into place atomically. Dot-
    # prefixed so MelonLoader ignores it even if a crash leaves one behind.
    return os.path.join(_mods_base(game_dir), f".{_safe_basename(mod_id)}.installing")


def _library_sidecar_path(game_dir, filename):
    # A library's record sits in UserLibs/ beside its .dll, keyed by filename
    # (libraries have no id). Kept next to the artifact it describes so each
    # folder is self-contained.
    return os.path.join(_userlibs_dir(game_dir), f"{filename}.meta.json")
