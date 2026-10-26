"""The client's mod cache, so switching servers is a file move rather than a
download: mod_cache/<id>/<version>/<artifact sha256>/ holds one target's tree
and the record that placed it (cut to that target); _libraries/<filename>/<sha256>/
holds a library and its sidecar."""
import json
import os
import shutil
import time

from tavern_shared import paths
from tavern_shared.paths import _sha256_file

from tavern_shared.mods.base import ModManagerError
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


_CACHE_TREE = "tree"
_CACHE_RECORD = "record.json"
# Per mod: the server's current, the previous, and one pinned.
_CACHE_KEEP_VERSIONS = 3


def _cache_base():
    return os.path.join(paths._tavern_data_dir(), "mod_cache")


def _cache_mod_dir(mod_id, version, sha256):
    return os.path.join(_cache_base(), _safe_basename(mod_id), _safe_basename(version),
                        _safe_basename(sha256))


def _cache_library_dir(filename, sha256):
    return os.path.join(_cache_base(), "_libraries", _safe_basename(filename), sha256)


def _prune_mod_cache(mod_id, keep):
    """Drops a mod's oldest entries past _CACHE_KEEP_VERSIONS, by the mtime
    cache_store_mod stamps (copytree keeps the install's), never `keep`."""
    base = os.path.join(_cache_base(), _safe_basename(mod_id))
    if not os.path.isdir(base):
        return
    entries = [os.path.join(base, v, sha)
               for v in os.listdir(base) if os.path.isdir(os.path.join(base, v))
               for sha in os.listdir(os.path.join(base, v))
               if os.path.isdir(os.path.join(base, v, sha)) and not sha.endswith(".caching")]
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
    """Caches an intact Mods/<id>/.<target>/; returns whether it was new."""
    rec = _read_mod_record(game_dir, mod_id)
    entry = _installed_entry(rec, target)
    if not rec or not rec.get("version") or not entry:
        return False
    sha256 = str(entry.get("sha256", "")).lower()
    if not _SHA256.fullmatch(sha256):
        return False
    dest = _cache_mod_dir(mod_id, rec["version"], sha256)
    if os.path.isdir(dest) or verify_mod_files(game_dir, mod_id, target) is False:
        return False

    staging = dest + ".caching"
    shutil.rmtree(staging, ignore_errors=True)
    shutil.copytree(_target_dir_path(game_dir, mod_id, target),
                    os.path.join(staging, _CACHE_TREE))
    with open(os.path.join(staging, _CACHE_RECORD), "w", encoding="utf-8") as f:
        json.dump(dict(rec, installed={target: entry}), f, indent=2)
    os.replace(staging, dest)
    now = time.time_ns()
    os.utime(dest, ns=(now, now))
    _prune_mod_cache(mod_id, dest)
    return True


def cache_store_library(game_dir, filename):
    src = os.path.join(_userlibs_dir(game_dir), filename)
    sidecar = _library_sidecar_path(game_dir, filename)
    meta = _read_sidecar(sidecar)
    if not os.path.isfile(src) or not meta or not meta.get("sha256"):
        return
    sha256 = meta["sha256"]
    dest = _cache_library_dir(filename, sha256)
    if os.path.isdir(dest) or not _library_present(game_dir, filename, sha256):
        return

    staging = dest + ".caching"
    shutil.rmtree(staging, ignore_errors=True)
    os.makedirs(staging)
    shutil.copy2(src, os.path.join(staging, filename))
    shutil.copy2(sidecar, os.path.join(staging, f"{filename}.meta.json"))
    os.replace(staging, dest)


def clear_mod_cache():
    if os.path.isdir(_cache_base()):
        shutil.rmtree(_cache_base())


def adopt_installed_mods(game_dir, target):
    """Caches every `target` build in Mods/ and its libraries, so rendering
    another version over a mod installed straight into Mods/ can't destroy
    the only copy. Returns the ids newly cached."""
    adopted = []
    for rec in list_installed_mods(game_dir):
        entry = _installed_entry(rec, target)
        if not rec.get("version") or not entry:
            continue
        if cache_store_mod(game_dir, rec["id"], target):
            adopted.append(rec["id"])
        for filename in entry.get("libraries") or []:
            cache_store_library(game_dir, filename)
    return adopted


def cache_restore_mod(game_dir, mod_id, version, sha256, target):
    """Places a cached build through _place_target, after re-verifying the
    staged copy against the cached record; a bad entry is discarded and
    raises, so the render falls back to a download."""
    src = _cache_mod_dir(mod_id, version, sha256)
    rec = _read_sidecar(os.path.join(src, _CACHE_RECORD))
    entry = _installed_entry(rec, target)
    if not rec or not entry or rec.get("id") != mod_id or rec.get("version") != version:
        raise ModManagerError(f"'{mod_id}' {version} isn't in the local cache.")

    staging = _staging_path(game_dir, mod_id)
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
        shutil.rmtree(staging, ignore_errors=True)


def cache_restore_library(game_dir, filename, sha256):
    """Restores a cached library unless present; the copy is hashed before
    it's swapped in, and a bad entry is discarded and raises."""
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
    """Puts a library in UserLibs/ unless present: from the cache, else
    downloaded (and cached). Returns whether anything was written."""
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


def ensure_mod_libraries(game_dir, mod_id, on_progress):
    """Restores the libraries a mod's targets pin; returns the filenames. For
    a bare re-enable, since the join prunes libraries no enabled mod pins."""
    rec = _read_mod_record(game_dir, mod_id)
    restored, seen = [], set()
    for target in installed_targets(rec):
        for lib in _record_target_libraries(rec, target):
            filename = _safe_basename(lib.filename)
            if filename in seen:
                continue
            seen.add(filename)
            if _ensure_library(game_dir, lib, on_progress):
                restored.append(filename)
    return restored
