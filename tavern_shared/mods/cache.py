"""The client mod cache: keeping a copy of what was installed so a later join
can restore it without re-downloading."""
import os
import shutil

from tavern_shared.paths import _sha256_file

from tavern_shared.mods import layout
from tavern_shared.mods.errors import ModManagerError, _warn
from tavern_shared.mods.install import (
    _clear_install_path, _invalidate_verify_memo, _read_mod_record,
    _read_sidecar, install_library_dependency, list_installed_mods,
)
from tavern_shared.mods.layout import (
    DISABLED_RECORD_NAME, RECORD_NAME, _library_sidecar_path, _mod_dir_path,
    _mods_base, _safe_basename, _staging_path, _userlibs_dir,
)
from tavern_shared.mods.manifest import _library_from_dict

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


# How many cached versions of one mod are kept. Every version ever rendered
# used to accumulate forever, with the manual "Clear Mod Cache" nuke as the
# only relief; three covers the realistic switching pattern (the server's
# current version, the previous one, and one the user pinned) while keeping
# the cache bounded. Pruned oldest-stored-first (directory mtime, stamped when
# the entry landed), never touching the version just stored.
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
    """Copies a mod currently on disk (Mods/<id>/, enabled or disabled) into
    the cache under its recorded version, if not already there. Returns the
    version stored, or None if the mod isn't actually installed. Safe to call
    repeatedly - a version already in the cache is left untouched. A new store
    also prunes that mod's oldest cached versions past _CACHE_KEEP_VERSIONS,
    so the cache stays bounded without a manual clear."""
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
    _prune_mod_cache(mod_id, version)
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
    that exact version isn't cached.

    The staged copy is verified against the record's "files" map before the
    swap, the same check downloads get via their sha256: the cache lives in
    %AppData% for months and nothing else ever re-reads it, so silent
    corruption (or tampering) there would otherwise be installed as-is. A
    failed verify discards BOTH the staging and the cache entry itself and
    raises - render_active_set catches that and falls back to a fresh
    download, so a rotten cache entry costs a download, never a broken mod.
    An entry with no files map (cached from a pre-"files" install) is
    restored unchecked, same terms as verify_mod_files' None."""
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
    if isinstance(files, dict) and files:
        for rel, expected in files.items():
            path = os.path.join(staging, rel.replace("/", os.sep))
            if os.path.isfile(path) and _sha256_file(path).lower() == str(expected).lower():
                continue
            shutil.rmtree(staging, ignore_errors=True)
            shutil.rmtree(src, ignore_errors=True)
            raise ModManagerError(
                f"The cached copy of '{mod_id}' {version} no longer matches "
                f"what was stored ({rel} is missing or altered). The bad cache "
                f"entry has been discarded; this version will need to be "
                f"re-downloaded.")
    _clear_install_path(game_dir, mod_id)
    os.makedirs(_mods_base(game_dir), exist_ok=True)
    os.replace(staging, dest)
    _invalidate_verify_memo(game_dir, mod_id)


def _library_present(game_dir, filename, sha256):
    """UserLibs/<filename> is on disk with a sidecar recording exactly this
    sha256 - the "already active" test every restore path runs before touching
    anything. The sidecar is the authority, matching how libraries have no
    version: a file at the right name with no (or a different) recorded hash
    needs reinstalling."""
    existing = _read_sidecar(_library_sidecar_path(game_dir, filename))
    return (os.path.isfile(os.path.join(_userlibs_dir(game_dir), filename))
            and bool(existing) and existing.get("sha256") == sha256)


def cache_restore_library(game_dir, filename, sha256):
    """Copies a cached library (and its sidecar) into UserLibs/, unless it's
    already there with a matching hash. Raises if that exact
    filename+sha256 isn't cached."""
    if _library_present(game_dir, filename, sha256):
        return

    src = _cache_library_dir(filename, sha256)
    if not os.path.isdir(src):
        raise ModManagerError(f"Library '{filename}' ({sha256[:12]}...) isn't in the local cache.")
    shutil.copy2(os.path.join(src, filename), os.path.join(_userlibs_dir(game_dir), filename))
    shutil.copy2(os.path.join(src, f"{filename}.meta.json"), _library_sidecar_path(game_dir, filename))


def _ensure_library(game_dir, lib, progress, verb="Restoring"):
    """Puts one pinned library into UserLibs/ at its exact hash, unless it's
    already there: from the cache when that content is cached (a file move),
    downloaded and then cached otherwise. Returns True when anything was
    written, False when the library was already in place - callers decide
    whether that counts as work done. `verb` only labels the cached-path
    progress line: the enable repair says "Restoring" where a join render
    says "Installing", and those strings are what the user sees."""
    filename = _safe_basename(lib.filename)
    if _library_present(game_dir, filename, lib.sha256):
        return False
    if os.path.isdir(_cache_library_dir(filename, lib.sha256)):
        progress(f"{verb} library {filename} (cached)")
        cache_restore_library(game_dir, filename, lib.sha256)
    else:
        progress(f"Downloading library {filename}")
        install_library_dependency(game_dir, lib, progress)
        cache_store_library(game_dir, filename)
    return True


def ensure_mod_libraries(game_dir, mod_id, on_progress=None):
    """Puts every library a mod's record pins back into UserLibs/ at its
    pinned hash - from the cache when it's there, downloaded otherwise.
    Returns the filenames it had to restore (empty when everything was
    already in place).

    This is what makes a bare re-enable safe. Enabling is just a record
    rename, but the join flow's _prune_orphaned_libraries removes any library
    no ENABLED mod pins - so a mod deactivated by one join and re-enabled
    later from the Mod Manager would load without its libraries and fail at
    runtime, with nothing on screen connecting the failure to the prune that
    caused it. The join flow itself doesn't need this (render_active_set runs
    a library pass over the whole active set); the manager window's Enable
    does, and calls it right after enable_mod succeeds.

    A record entry that doesn't parse as a library (a hand-edited record) is
    skipped with a warning rather than failing the enable: the mod's own
    files are already on disk and enabled either way."""
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
