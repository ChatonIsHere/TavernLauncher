"""The on-disk install layout: Mods/<id>/, its record and per-target
.<target>/ folders, UserLibs/ sidecars, staging paths. TavernLib's ModPaths mirrors it; any other
installer must match it byte-for-byte."""
import os

from tavern_shared.mods.errors import ModManagerError


def _safe_basename(name):
    """Raises unless name is a bare, non-empty basename with no path separator
    and no colon. Re-checked at write time for every mod id and filename,
    since a manifest from an unreviewed repo can't be trusted to be clean."""
    if not name or name in (".", ".."):
        raise ModManagerError(f"Unsafe filename {name!r}.")
    if "/" in name or "\\" in name:
        raise ModManagerError(f"Filename {name!r} must not contain a path separator.")
    # A drive separator ("C:evil.dll") or an NTFS alternate data stream
    # ("mod.dll:x"); os.path.basename sees neither as a directory part.
    if ":" in name:
        raise ModManagerError(f"Filename {name!r} must not contain a colon.")
    if os.path.basename(name) != name:
        raise ModManagerError(f"Filename {name!r} must be a bare basename.")
    return name


# MUST be "manifest.json": MelonLoader only loads a Mods/ subfolder holding a
# file by this name (existence only), so the record doubles as that marker.
RECORD_NAME = "manifest.json"


# Disabling renames the record to this, so MelonLoader skips the folder while
# it keeps its by-id path.
DISABLED_RECORD_NAME = "manifest.disabled.json"


def _mods_base(game_dir):
    # Not created: read-only callers shouldn't make folders.
    return os.path.join(game_dir, "Mods")


def _userlibs_dir(game_dir):
    d = os.path.join(game_dir, "UserLibs")
    os.makedirs(d, exist_ok=True)
    return d


def _displaced_base(game_dir):
    # Dot-prefixed, so nothing that scans Mods/ sees it. Same as TavernLib's
    # ModPaths.DisplacedBase.
    return os.path.join(_mods_base(game_dir), ".displaced")


def _mod_dir_path(game_dir, mod_id):
    return os.path.join(_mods_base(game_dir), _safe_basename(mod_id))


def _target_dir_path(game_dir, mod_id, target):
    # Dot-prefixed, so MelonLoader skips it; TavernLib adds back the one
    # folder that matches the process it runs in.
    return os.path.join(_mod_dir_path(game_dir, mod_id), _safe_basename(f".{target}"))


def _mod_record_path(game_dir, mod_id):
    return os.path.join(_mod_dir_path(game_dir, mod_id), RECORD_NAME)


def _disabled_record_path(game_dir, mod_id):
    return os.path.join(_mod_dir_path(game_dir, mod_id), DISABLED_RECORD_NAME)


def _staging_path(game_dir, mod_id):
    # Dot-prefixed so nothing loads one a crash leaves behind.
    return os.path.join(_mods_base(game_dir), f".{_safe_basename(mod_id)}.installing")


def _library_sidecar_path(game_dir, filename):
    return os.path.join(_userlibs_dir(game_dir), f"{filename}.meta.json")
