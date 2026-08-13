"""The client mod cache: keeping a copy of what was installed so a later join
can restore it without re-downloading."""
import os
import shutil

from tavern_shared.mods import layout
from tavern_shared.mods.errors import ModManagerError
from tavern_shared.mods.install import (
    _clear_install_path, _read_mod_record, _read_sidecar, list_installed_mods,
)
from tavern_shared.mods.layout import (
    DISABLED_RECORD_NAME, RECORD_NAME, _library_sidecar_path, _mod_dir_path,
    _mods_base, _safe_basename, _staging_path, _userlibs_dir,
)

# _tavern_data_dir goes through the module (layout._tavern_data_dir) rather
# than being imported by name: tests redirect the cache to a temp directory by
# replacing it, and a by-name import here would keep pointing at the real
# %AppData% regardless.


def _cache_base():
    return os.path.join(layout._tavern_data_dir(), "mod_cache")


def _cache_mod_dir(mod_id, version):
    return os.path.join(_cache_base(), _safe_basename(mod_id), _safe_basename(version))


def _cache_library_dir(filename, sha256):
    return os.path.join(_cache_base(), "_libraries", _safe_basename(filename), sha256)


def cache_store_mod(game_dir, mod_id):
    """Copies a mod currently on disk (Mods/<id>/, enabled or disabled) into
    the cache under its recorded version, if not already there. Returns the
    version stored, or None if the mod isn't actually installed. Safe to call
    repeatedly - a version already in the cache is left untouched."""
    rec = _read_mod_record(game_dir, mod_id)
    if not rec or not rec.get("version"):
        return None
    version = rec["version"]
    dest = _cache_mod_dir(mod_id, version)
    if os.path.isdir(dest):
        return version

    src = _mod_dir_path(game_dir, mod_id)
    staging = dest + ".caching"
    if os.path.isdir(staging):
        shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(src, staging)
    # The cache doesn't track enabled/disabled (that's a Mods/-only concept) -
    # normalize to the plain record name so cache_restore_mod always finds it.
    disabled = os.path.join(staging, DISABLED_RECORD_NAME)
    if os.path.isfile(disabled):
        os.replace(disabled, os.path.join(staging, RECORD_NAME))
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    os.replace(staging, dest)
    return version


def cache_store_library(game_dir, filename):
    """Copies a UserLibs/ library currently on disk into the cache under its
    recorded sha256, if not already there. Returns the sha256 stored, or None
    if the library or its sidecar isn't actually present."""
    src = os.path.join(_userlibs_dir(game_dir), filename)
    sidecar = _library_sidecar_path(game_dir, filename)
    meta = _read_sidecar(sidecar)
    if not os.path.isfile(src) or not meta or not meta.get("sha256"):
        return None
    sha256 = meta["sha256"]
    dest = _cache_library_dir(filename, sha256)
    if os.path.isdir(dest):
        return sha256

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
    """Deletes the entire client mod cache (every cached mod version and
    library). Mods/ itself is untouched - this only resets the
    switch-servers-without-redownloading optimization, forcing the next
    install or join to fetch fresh from source instead of reusing a local
    copy. The fix for a cache entry that's gone stale, e.g. one written
    before a manifest field parity_required() now requires existed - since
    the cache is keyed on id+version alone, a version bump upstream is the
    only thing that normally invalidates an entry."""
    base = _cache_base()
    if os.path.isdir(base):
        shutil.rmtree(base)


def adopt_installed_mods(game_dir):
    """Copies everything currently installed in Mods/ (and the UserLibs/
    libraries they pin) into the cache, without touching Mods/ itself.
    Idempotent - already-cached versions are skipped - so it's safe to call on
    every join; the client launcher calls it from _build_mod_plan, before any
    plan is computed or applied.

    Running it before a render is what makes the cache's "switching servers is
    a file move, never a re-download" promise actually hold. Anything that
    installed straight to Mods/ (the Community Mods window, or an older
    launcher with no cache) is otherwise unknown to the cache, so rendering a
    different version over it destroys the only copy of the version that was
    there. Returns the list of mod ids newly adopted (already-cached ones don't
    count)."""
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
    """Copies a cached mod version into Mods/<id>/, enabled, replacing
    whatever's currently there (assembled in staging, swapped in atomically -
    same pattern install_mod uses). A folder we didn't install is displaced
    rather than replaced, same as install_mod: this runs on every join that
    switches versions, so it's the likeliest of the two to meet one. Raises if
    that exact version isn't cached."""
    src = _cache_mod_dir(mod_id, version)
    if not os.path.isdir(src):
        raise ModManagerError(f"'{mod_id}' {version} isn't in the local cache.")

    dest = _mod_dir_path(game_dir, mod_id)
    staging = _staging_path(game_dir, mod_id)
    if os.path.isdir(staging):
        shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(src, staging)
    _clear_install_path(game_dir, mod_id)
    os.makedirs(_mods_base(game_dir), exist_ok=True)
    os.replace(staging, dest)


def cache_restore_library(game_dir, filename, sha256):
    """Copies a cached library (and its sidecar) into UserLibs/, unless it's
    already there with a matching hash. Raises if that exact
    filename+sha256 isn't cached."""
    dest = os.path.join(_userlibs_dir(game_dir), filename)
    existing = _read_sidecar(_library_sidecar_path(game_dir, filename))
    if os.path.isfile(dest) and existing and existing.get("sha256") == sha256:
        return

    src = _cache_library_dir(filename, sha256)
    if not os.path.isdir(src):
        raise ModManagerError(f"Library '{filename}' ({sha256[:12]}...) isn't in the local cache.")
    shutil.copy2(os.path.join(src, filename), dest)
    shutil.copy2(os.path.join(src, f"{filename}.meta.json"), _library_sidecar_path(game_dir, filename))
