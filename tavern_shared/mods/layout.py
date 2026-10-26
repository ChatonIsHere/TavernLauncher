"""The on-disk layout (REPO_STRUCTURE.md "Install destinations"); twin of
TavernLib's ModPaths."""
import os

from tavern_shared.mods.base import ModManagerError


def _safe_basename(name):
    """Raises unless name is a bare basename. Re-checked at write time, since a
    manifest from an unreviewed repo can't be trusted to be clean."""
    if not name or name in (".", ".."):
        raise ModManagerError(f"Unsafe filename {name!r}.")
    if "/" in name or "\\" in name:
        raise ModManagerError(f"Filename {name!r} must not contain a path separator.")
    # A drive ("C:evil.dll") or an NTFS stream ("mod.dll:x"); basename sees neither.
    if ":" in name:
        raise ModManagerError(f"Filename {name!r} must not contain a colon.")
    if os.path.basename(name) != name:
        raise ModManagerError(f"Filename {name!r} must be a bare basename.")
    return name


# Also MelonLoader's marker: it loads a Mods/ subfolder only if this file exists.
RECORD_NAME = "manifest.json"
DISABLED_RECORD_NAME = "manifest.disabled.json"


def _mods_base(game_dir):
    return os.path.join(game_dir, "Mods")


def _userlibs_dir(game_dir):
    d = os.path.join(game_dir, "UserLibs")
    os.makedirs(d, exist_ok=True)
    return d


def _displaced_base(game_dir):
    return os.path.join(_mods_base(game_dir), ".displaced")


def _mod_dir_path(game_dir, mod_id):
    return os.path.join(_mods_base(game_dir), _safe_basename(mod_id))


def _target_dir_path(game_dir, mod_id, target):
    return os.path.join(_mod_dir_path(game_dir, mod_id), _safe_basename(f".{target}"))


def _mod_record_path(game_dir, mod_id):
    return os.path.join(_mod_dir_path(game_dir, mod_id), RECORD_NAME)


def _disabled_record_path(game_dir, mod_id):
    return os.path.join(_mod_dir_path(game_dir, mod_id), DISABLED_RECORD_NAME)


def _staging_path(game_dir, mod_id):
    return os.path.join(_mods_base(game_dir), f".{_safe_basename(mod_id)}.installing")


def _library_sidecar_path(game_dir, filename):
    return os.path.join(_userlibs_dir(game_dir), f"{filename}.meta.json")
