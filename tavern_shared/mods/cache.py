"""The client mod cache: %AppData%/TheModdingTavern/mod_cache/<id>/<version>/<sha256>/
(one target build, keyed by its artifact's sha256) and
_libraries/<filename>/<sha256>/, so switching servers is a file move rather
than a download."""
import json
import os
import shutil
import time

from tavern_shared import paths
from tavern_shared.paths import _sha256_file

from tavern_shared.mods.errors import ModManagerError
from tavern_shared.mods.install import (
    _files_intact, _installed_entry, _library_present, _place_target,
    _read_mod_record, _read_sidecar, _record_target_libraries,
    install_library_dependency, installed_targets, list_installed_mods,
    verify_mod_files,
)
from tavern_shared.mods.layout import (
    _library_sidecar_path, _safe_basename, _staging_path, _target_dir_path,
    _userlibs_dir,
)
from tavern_shared.mods.manifest import _SHA256, parity_required

# Reached through the module, not imported by name, so tests can point the
# cache at a temp directory by replacing paths._tavern_data_dir.


def _cache_base():
    return os.path.join(paths._tavern_data_dir(), "mod_cache")


def _cache_mod_dir(mod_id, version, sha256):
    return os.path.join(_cache_base(), _safe_basename(mod_id), _safe_basename(version),
                        _safe_basename(sha256))


# Inside an entry: the target folder's tree, and the record that placed it
# (its `installed` cut to that one target), which restore re-verifies the
# tree against and writes back.
_CACHE_TREE = "tree"
_CACHE_RECORD = "record.json"


def _cache_library_dir(filename, sha256):
    return os.path.join(_cache_base(), "_libraries", _safe_basename(filename), sha256)


# Cached builds kept per mod: the server's current, the previous, and one
# pinned. Pruned oldest-stored-first by the entry's mtime, which
# cache_store_mod stamps on landing (copytree carries over the install's).
_CACHE_KEEP_VERSIONS = 3


def _prune_mod_cache(mod_id, keep):
    """Drops a mod's oldest entries past _CACHE_KEEP_VERSIONS, never `keep`."""
    base = os.path.join(_cache_base(), _safe_basename(mod_id))
    if not os.path.isdir(base):
        return
    entries = [os.path.join(base, v, sha)
               for v in os.listdir(base) if os.path.isdir(os.path.join(base, v))
               for sha in os.listdir(os.path.join(base, v))
               if os.path.isdir(os.path.join(base, v, sha)) and not sha.endswith(".caching")]
    if len(entries) <= _CACHE_KEEP_VERSIONS:
        return
    entries.sort(key=os.path.getmtime, reverse=True)
    for path in entries[_CACHE_KEEP_VERSIONS:]:
        if os.path.normcase(path) == os.path.normcase(keep):
            continue
        shutil.rmtree(path, ignore_errors=True)
        try:
            os.rmdir(os.path.dirname(path))     # the version folder, once empty
        except OSError:
            pass


def cache_store_mod(game_dir, mod_id, target):
    """Copies Mods/<id>/.<target>/ (enabled or disabled) into the cache under
    its recorded version and artifact sha256, unless already cached, then
    prunes that mod's older entries. Returns the version, or None if that
    target isn't installed or is damaged."""
    rec = _read_mod_record(game_dir, mod_id)
    entry = _installed_entry(rec, target)
    if not rec or not rec.get("version") or not entry:
        return None
    sha256 = str(entry.get("sha256", "")).lower()
    if not _SHA256.fullmatch(sha256):
        return None
    version = rec["version"]
    dest = _cache_mod_dir(mod_id, version, sha256)
    if os.path.isdir(dest):
        return version
    if verify_mod_files(game_dir, mod_id, target) is False:
        return None     # never cache a damaged copy as the good one

    staging = dest + ".caching"
    if os.path.isdir(staging):
        shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(_target_dir_path(game_dir, mod_id, target),
                    os.path.join(staging, _CACHE_TREE))
    with open(os.path.join(staging, _CACHE_RECORD), "w", encoding="utf-8") as f:
        json.dump(dict(rec, installed={target: entry}), f, indent=2)
    os.replace(staging, dest)
    now = time.time_ns()
    os.utime(dest, ns=(now, now))
    _prune_mod_cache(mod_id, dest)
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
    """Deletes the whole mod cache; Mods/ is untouched. Entries are keyed by
    their build's sha256 and re-verified on restore, so this only frees
    space."""
    base = _cache_base()
    if os.path.isdir(base):
        shutil.rmtree(base)


def adopt_installed_mods(game_dir, target):
    """Caches every `target` build in Mods/ (and the libraries it pins)
    without touching Mods/. The client calls it before planning a join: a mod
    installed straight into Mods/ is otherwise unknown to the cache, and
    rendering another version over it would destroy the only copy. Returns
    the ids newly cached."""
    adopted = []
    for rec in list_installed_mods(game_dir):
        entry = _installed_entry(rec, target)
        if not rec.get("version") or not entry:
            continue
        sha256 = str(entry.get("sha256", "")).lower()
        already_cached = bool(_SHA256.fullmatch(sha256)) and os.path.isdir(
            _cache_mod_dir(rec["id"], rec["version"], sha256))
        if cache_store_mod(game_dir, rec["id"], target) and not already_cached:
            adopted.append(rec["id"])
        for filename in entry.get("libraries") or []:
            cache_store_library(game_dir, filename)
    return adopted


def cache_restore_mod(game_dir, mod_id, version, sha256, target):
    """Installs a cached build as Mods/<id>/.<target>/ and updates the record
    exactly as an install would (see install._place_target), enabled; a
    folder that isn't ours is displaced. The staged copy is checked against
    the cached record's `files` first: a failure discards the cache entry and
    raises, and render_active_set falls back to a download. Raises if that
    build isn't cached."""
    src = _cache_mod_dir(mod_id, version, sha256)
    rec = _read_sidecar(os.path.join(src, _CACHE_RECORD))
    entry = _installed_entry(rec, target)
    if not rec or not entry or rec.get("id") != mod_id or rec.get("version") != version:
        raise ModManagerError(f"'{mod_id}' {version} isn't in the local cache.")

    staging = _staging_path(game_dir, mod_id)
    if os.path.isdir(staging):
        shutil.rmtree(staging, ignore_errors=True)
    try:
        shutil.copytree(os.path.join(src, _CACHE_TREE), staging)
        files = entry.get("files")
        bad = _files_intact(staging, files) if isinstance(files, dict) and files else None
        try:
            parity_required(rec)
        except ModManagerError:
            bad = "its record"
        if bad is not None:
            shutil.rmtree(src, ignore_errors=True)
            raise ModManagerError(
                f"The cached copy of '{mod_id}' {version} no longer matches "
                f"what was stored ({bad} is missing or altered). The bad cache "
                f"entry has been discarded; this version will need to be "
                f"re-downloaded.")
        base = {k: v for k, v in rec.items() if k != "installed"}
        _place_target(game_dir, base, target, staging, entry)
    finally:
        if os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)


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
    """Restores every library the record's installed targets pin (cache first,
    else download) and returns the filenames it had to restore. The manager
    window calls this after a bare enable: the join flow prunes libraries no
    enabled mod pins, and enabling the record turns on every target in the
    folder, so a re-enabled mod would otherwise load without them."""
    progress = on_progress or (lambda *_a: None)
    rec = _read_mod_record(game_dir, mod_id)
    restored, seen = [], set()
    for target in installed_targets(rec):
        for lib in _record_target_libraries(rec, target):
            filename = _safe_basename(lib.filename)
            if filename in seen:
                continue
            seen.add(filename)
            if _ensure_library(game_dir, lib, progress):
                restored.append(filename)
    return restored
