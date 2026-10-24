"""The client mod cache: %AppData%/TheModdingTavern/mod_cache/<id>/<version>/
and _libraries/<filename>/<sha256>/, so switching servers is a file move
rather than a download."""
import os
import shutil
import time

from tavern_shared import paths
from tavern_shared.paths import _sha256_file

from tavern_shared.mods.errors import ModManagerError, _warn
from tavern_shared.mods.install import (
    _clear_install_path, _files_intact, _invalidate_verify_memo, _library_present,
    _read_mod_record, _read_sidecar, install_library_dependency,
    list_installed_mods, verify_mod_files,
)
from tavern_shared.mods.layout import (
    DISABLED_RECORD_NAME, RECORD_NAME, _library_sidecar_path, _mod_dir_path,
    _mods_base, _safe_basename, _staging_path, _userlibs_dir,
)
from tavern_shared.mods.manifest import _library_from_dict

# Reached through the module, not imported by name, so tests can point the
# cache at a temp directory by replacing paths._tavern_data_dir.


def _cache_base():
    return os.path.join(paths._tavern_data_dir(), "mod_cache")


def _cache_mod_dir(mod_id, version):
    return os.path.join(_cache_base(), _safe_basename(mod_id), _safe_basename(version))


def _cache_library_dir(filename, sha256):
    return os.path.join(_cache_base(), "_libraries", _safe_basename(filename), sha256)


# Cached versions kept per mod: the server's current, the previous, and one
# pinned. Pruned oldest-stored-first by the entry's mtime, which
# cache_store_mod stamps on landing (copytree carries over the install's).
_CACHE_KEEP_VERSIONS = 3


def _prune_mod_cache(mod_id, keep_version):
    base = os.path.join(_cache_base(), _safe_basename(mod_id))
    if not os.path.isdir(base):
        return
    versions = [d for d in os.listdir(base)
                if os.path.isdir(os.path.join(base, d)) and not d.endswith(".caching")]
    if len(versions) <= _CACHE_KEEP_VERSIONS:
        return
    versions.sort(key=lambda d: os.path.getmtime(os.path.join(base, d)), reverse=True)
    for d in versions[_CACHE_KEEP_VERSIONS:]:
        if d == keep_version:
            continue
        shutil.rmtree(os.path.join(base, d), ignore_errors=True)


def cache_store_mod(game_dir, mod_id):
    """Copies Mods/<id>/ (enabled or disabled) into the cache under its recorded
    version unless already cached, then prunes that mod's older entries.
    Returns the version, or None if it isn't installed or is damaged."""
    rec = _read_mod_record(game_dir, mod_id)
    if not rec or not rec.get("version"):
        return None
    version = rec["version"]
    dest = _cache_mod_dir(mod_id, version)
    if os.path.isdir(dest):
        return version
    if verify_mod_files(game_dir, mod_id) is False:
        return None     # never cache a damaged copy as the good one

    src = _mod_dir_path(game_dir, mod_id)
    staging = dest + ".caching"
    if os.path.isdir(staging):
        shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(src, staging)
    # The cache has no disabled state; restore always looks for RECORD_NAME.
    disabled = os.path.join(staging, DISABLED_RECORD_NAME)
    if os.path.isfile(disabled):
        os.replace(disabled, os.path.join(staging, RECORD_NAME))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    os.replace(staging, dest)
    now = time.time_ns()
    os.utime(dest, ns=(now, now))
    _prune_mod_cache(mod_id, version)
    return version


def cache_store_library(game_dir, filename):
    """Copies an intact UserLibs/ library into the cache under its sha256 unless
    already cached. Returns the sha256, or None if it isn't present intact."""
    src = os.path.join(_userlibs_dir(game_dir), filename)
    sidecar = _library_sidecar_path(game_dir, filename)
    meta = _read_sidecar(sidecar)
    if not os.path.isfile(src) or not meta or not meta.get("sha256"):
        return None
    sha256 = meta["sha256"]
    dest = _cache_library_dir(filename, sha256)
    if os.path.isdir(dest):
        return sha256
    if not _library_present(game_dir, filename, sha256):
        return None     # the file no longer matches its sidecar

    staging = dest + ".caching"
    if os.path.isdir(staging):
        shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging)
    shutil.copy2(src, os.path.join(staging, filename))
    shutil.copy2(sidecar, os.path.join(staging, f"{filename}.meta.json"))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    os.replace(staging, dest)
    return sha256


def clear_mod_cache():
    """Deletes the whole mod cache; Mods/ is untouched. Entries are keyed on
    id+version alone, so this is the fix for one that has gone stale."""
    base = _cache_base()
    if os.path.isdir(base):
        shutil.rmtree(base)


def adopt_installed_mods(game_dir):
    """Caches everything in Mods/ (and the libraries it pins) without touching
    Mods/. The client calls it before planning a join: a mod installed straight
    into Mods/ is otherwise unknown to the cache, and rendering another version
    over it would destroy the only copy. Returns the ids newly cached."""
    adopted = []
    for rec in list_installed_mods(game_dir):
        version = rec.get("version")
        if not version:
            continue
        already_cached = os.path.isdir(_cache_mod_dir(rec["id"], version))
        if cache_store_mod(game_dir, rec["id"]) and not already_cached:
            adopted.append(rec["id"])
        for filename in rec.get("libraries", []):
            cache_store_library(game_dir, filename)
    return adopted


def cache_restore_mod(game_dir, mod_id, version):
    """Copies a cached version into Mods/<id>/, enabled, through staging and an
    atomic swap; a folder that isn't ours is displaced, as install_mod does.
    The staged copy is checked against the record's `files` first: a failure
    discards the cache entry and raises, and render_active_set falls back to a
    download. An entry without `files` is restored unchecked. Raises if that
    version isn't cached."""
    src = _cache_mod_dir(mod_id, version)
    if not os.path.isdir(src):
        raise ModManagerError(f"'{mod_id}' {version} isn't in the local cache.")

    dest = _mod_dir_path(game_dir, mod_id)
    staging = _staging_path(game_dir, mod_id)
    if os.path.isdir(staging):
        shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(src, staging)
    rec = _read_sidecar(os.path.join(staging, RECORD_NAME))
    files = (rec or {}).get("files")
    bad = _files_intact(staging, files) if isinstance(files, dict) and files else None
    if bad is not None:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(src, ignore_errors=True)
        raise ModManagerError(
            f"The cached copy of '{mod_id}' {version} no longer matches "
            f"what was stored ({bad} is missing or altered). The bad cache "
            f"entry has been discarded; this version will need to be "
            f"re-downloaded.")
    _clear_install_path(game_dir, mod_id)
    os.makedirs(_mods_base(game_dir), exist_ok=True)
    os.replace(staging, dest)
    _invalidate_verify_memo(game_dir, mod_id)


def cache_restore_library(game_dir, filename, sha256):
    """Copies a cached library (and its sidecar) into UserLibs/, unless it's
    already there intact. The copy is hashed before it's swapped in; a cache
    entry that fails is discarded and raises, like cache_restore_mod. Raises
    if that exact filename+sha256 isn't cached."""
    if _library_present(game_dir, filename, sha256):
        return

    src = _cache_library_dir(filename, sha256)
    if not os.path.isdir(src):
        raise ModManagerError(f"Library '{filename}' ({sha256[:12]}...) isn't in the local cache.")
    dest = os.path.join(_userlibs_dir(game_dir), filename)
    staging = os.path.join(_userlibs_dir(game_dir), f".{filename}.restoring")
    try:
        shutil.copy2(os.path.join(src, filename), staging)
        if _sha256_file(staging).lower() != sha256.lower():
            shutil.rmtree(src, ignore_errors=True)
            raise ModManagerError(
                f"The cached copy of library '{filename}' no longer matches its "
                f"hash. The bad cache entry has been discarded; it will need to "
                f"be re-downloaded.")
        os.replace(staging, dest)
    finally:
        if os.path.isfile(staging):
            os.remove(staging)
    shutil.copy2(os.path.join(src, f"{filename}.meta.json"), _library_sidecar_path(game_dir, filename))


def _ensure_library(game_dir, lib, progress, verb="Restoring"):
    """Puts one pinned library into UserLibs/ at its hash unless it's already
    there: from the cache when cached and intact, downloaded (and cached)
    otherwise. Returns whether anything was written. `verb` labels the cached
    path's progress line."""
    filename = _safe_basename(lib.filename)
    if _library_present(game_dir, filename, lib.sha256):
        return False
    if os.path.isdir(_cache_library_dir(filename, lib.sha256)):
        progress(f"{verb} library {filename} (cached)")
        try:
            cache_restore_library(game_dir, filename, lib.sha256)
            return True
        except ModManagerError as e:
            progress(f"Cache copy unusable ({e}) - downloading instead")
    progress(f"Downloading library {filename}")
    install_library_dependency(game_dir, lib, progress)
    cache_store_library(game_dir, filename)
    return True


def ensure_mod_libraries(game_dir, mod_id, on_progress=None):
    """Restores every library a mod's record pins (cache first, else download)
    and returns the filenames it had to restore. The manager window calls this
    after a bare enable: the join flow prunes libraries no enabled mod pins, so
    a re-enabled mod would otherwise load without them. An unparseable record
    entry is skipped with a warning."""
    progress = on_progress or (lambda *_a: None)
    rec = _read_mod_record(game_dir, mod_id)
    restored = []
    for entry in (rec.get("library_dependencies") if rec else []) or []:
        try:
            lib = _library_from_dict(dict(entry))
            filename = _safe_basename(lib.filename)
        except (ModManagerError, TypeError):
            _warn(f"'{mod_id}' pins a library its record doesn't describe "
                  f"usably ({entry!r}); skipped.")
            continue
        if _ensure_library(game_dir, lib, progress):
            restored.append(filename)
    return restored
