"""Putting a verified artifact on disk, and reading back what is there.
Nothing here touches the network except through _verified_download. Every
install is for one target: Mods/<id>/.<target>/ plus that target's entry in
the record (CommunityMods' docs/REPO_STRUCTURE.md, shared with TavernLib)."""
import contextlib
import hashlib
import json
import os
import shutil
import tempfile
import zipfile

from tavern_shared.mod_install import _download_with_progress
from tavern_shared.paths import _sha256_file

from tavern_shared.mods.errors import ModManagerError, _warn
from tavern_shared.mods.layout import (
    DISABLED_RECORD_NAME, RECORD_NAME, _disabled_record_path, _displaced_base,
    _library_sidecar_path, _mod_dir_path, _mod_record_path, _mods_base,
    _safe_basename, _staging_path, _target_dir_path, _userlibs_dir,
)
from tavern_shared.mods.manifest import (
    INSTALL_TARGETS, _artifacts_from_list, parity_required,
)
from tavern_shared.mods.resolve import _fetch_manifest, resolve_dependencies
from tavern_shared.mods.version import _parse_version


# Zip-bundle caps, enforced on the actual decompressed bytes so a bomb from an
# unreviewed repo can't fill the disk.
_ZIP_MAX_ENTRIES = 2000


_ZIP_MAX_TOTAL_UNCOMPRESSED = 500 * 1024 * 1024   # 500 MB extracted, summed


def _sidecar_bytes(data):
    """The spec's sidecar formatting: two-space indent, ": ", non-ASCII as
    lowercase \\uXXXX, CRLF, no trailing newline. Built as bytes so no
    text-mode newline translation can touch it; twin of TavernLib's record
    writer, which must produce the same bytes."""
    return json.dumps(data, indent=2, ensure_ascii=True).replace("\n", "\r\n").encode("ascii")


def _write_sidecar(path, data):
    # Via a temp, so a cut-short write can't leave a record that reads foreign.
    tmp = os.path.join(os.path.dirname(path), f".{os.path.basename(path)}.tmp")
    with open(tmp, "wb") as f:
        f.write(_sidecar_bytes(data))
    os.replace(tmp, path)


def _check_target(target):
    if target not in INSTALL_TARGETS:
        raise ModManagerError(
            f"An install needs target={' or '.join(map(repr, INSTALL_TARGETS))}, "
            f"got {target!r}.")


@contextlib.contextmanager
def _verified_download(url, sha256, work_dir, on_progress):
    """Downloads url to a temp file in work_dir, verifies its sha256 and
    yields the path; the temp is always removed. It lives in work_dir, not
    %TEMP%, so the caller's os.replace is a same-volume rename (a cross-volume
    one fails with WinError 17 when the game is on another drive)."""
    os.makedirs(work_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tavern_dl_", suffix=".part", dir=work_dir)
    os.close(fd)
    try:
        _download_with_progress(url, tmp, on_progress)
        actual = _sha256_file(tmp)
        if actual.lower() != (sha256 or "").lower():
            raise ModManagerError(
                f"Downloaded file failed its checksum: expected {sha256}, got "
                f"{actual}. The file may be corrupted or tampered with; nothing "
                f"was installed.")
        yield tmp
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass


def _download_verify_move(url, sha256, dest_path, on_progress):
    """Download, verify, then atomically move one file into dest_path."""
    dest_dir = os.path.dirname(dest_path)
    with _verified_download(url, sha256, dest_dir, on_progress) as tmp:
        os.replace(tmp, dest_path)


def _safe_extract_zip(zip_path, dest_dir):
    """Extracts into dest_dir, refusing any member that is absolute, holds a
    colon, is a symlink or resolves outside dest_dir, and capping the entry
    count and the decompressed total. The first bad entry raises; callers
    extract into throwaway staging."""
    dest_root = os.path.realpath(dest_dir)
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        if len(infos) > _ZIP_MAX_ENTRIES:
            raise ModManagerError(
                f"This mod archive has {len(infos)} entries, over the "
                f"{_ZIP_MAX_ENTRIES} limit; refusing to extract it.")
        # The record is written under these names after the tree is hashed, so
        # extracting an archive's own copy would leave a permanent "damaged"
        # verdict. Matched on the resolved path, case-insensitively, as NTFS
        # does. TavernLib's SafeExtractZip skips the same set.
        record_paths = {os.path.normcase(os.path.join(dest_root, n))
                        for n in (RECORD_NAME, DISABLED_RECORD_NAME)}
        written = 0
        for info in infos:
            name = info.filename
            # A colon anywhere, not just a drive letter: "mod.dll:payload" is an
            # NTFS alternate data stream, which the realpath check reads as
            # staying inside dest_root.
            if name.startswith(("/", "\\")):
                raise ModManagerError(
                    f"Unsafe archive entry {name!r}: absolute path.")
            if ":" in name:
                raise ModManagerError(
                    f"Unsafe archive entry {name!r}: contains a colon (drive "
                    f"letter or NTFS alternate data stream).")
            # Unix mode lives in the high 16 bits of external_attr.
            mode = (info.external_attr >> 16) & 0xFFFF
            if mode and (mode & 0o170000) == 0o120000:
                raise ModManagerError(
                    f"Unsafe archive entry {name!r}: symlink.")
            rel = name.replace("/", os.sep).replace("\\", os.sep)
            target = os.path.realpath(os.path.join(dest_root, rel))
            if target != dest_root and not target.startswith(dest_root + os.sep):
                raise ModManagerError(
                    f"Unsafe archive entry {name!r}: escapes the mod folder.")
            if os.path.normcase(target) in record_paths:
                continue
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            # Counted on the bytes actually produced; a bomb can understate
            # the header's file_size.
            with zf.open(info) as src, open(target, "wb") as dst:
                while True:
                    chunk = src.read(1024 * 1024)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > _ZIP_MAX_TOTAL_UNCOMPRESSED:
                        raise ModManagerError(
                            f"This mod archive extracts to more than "
                            f"{_ZIP_MAX_TOTAL_UNCOMPRESSED // (1024 * 1024)} MB; "
                            f"refusing to extract it (possible zip bomb).")
                    dst.write(chunk)


def _hash_tree(base):
    """{relative path (forward slashes): lowercase sha256} for every file under
    base. Twin of TavernLib's ModInstaller.HashTree: the map must stay
    byte-identical so either side can verify the other's records."""
    out = {}
    for root, _dirs, names in os.walk(base):
        for name in names:
            full = os.path.join(root, name)
            rel = os.path.relpath(full, base).replace(os.sep, "/")
            out[rel] = _sha256_file(full)
    return out


def _ordinal_key(path):
    # Code-unit (UTF-16) order, which C#'s ordinal sort uses: it differs from
    # Python's code-point order only above U+FFFF, but the bytes must match.
    return path.encode("utf-16-be", "surrogatepass")


def installed_targets(rec):
    """The targets a record lists under `installed`, sorted. [] for a record
    with none, including one written before manifest v2 (no `installed`)."""
    inst = (rec or {}).get("installed")
    if not isinstance(inst, dict):
        return []
    return sorted(t for t, e in inst.items() if isinstance(e, dict))


def _installed_entry(rec, target):
    inst = (rec or {}).get("installed")
    entry = inst.get(target) if isinstance(inst, dict) else None
    return entry if isinstance(entry, dict) else None


def _is_pre_v2(rec):
    """A record with no `installed` map, written before manifest v2."""
    return not isinstance(rec.get("installed"), dict)


def has_target(rec, target):
    """Whether a record lists `target` as installed."""
    return target in installed_targets(rec)


# verify_mod_files results by (mod folder, target): ((version, sha256), ok). A
# zip bundle can be hundreds of MB, so an unchanged install is hashed once; an
# install or restore of the mod invalidates its entries, and the Mod Manager's
# Refresh clears all.
_verify_memo = {}


def _invalidate_verify_memo(game_dir=None, mod_id=None):
    """Forget cached verify results: for one mod when its folder was just
    rewritten (install/cache-restore), or all of them (Refresh)."""
    if game_dir is None:
        _verify_memo.clear()
        return
    mod_dir = _mod_dir_path(game_dir, mod_id)
    for key in [k for k in _verify_memo if k[0] == mod_dir]:
        del _verify_memo[key]


def _files_intact(base, files):
    """Whether every {relative path: sha256} in `files` is present under base
    at that hash. Returns the first bad path, or None when all match."""
    for rel, expected in files.items():
        path = os.path.join(base, rel.replace("/", os.sep))
        if not os.path.isfile(path) or _sha256_file(path).lower() != str(expected).lower():
            return rel
    return None


def verify_mod_files(game_dir, mod_id, target):
    """True if every file in installed[target]'s `files` is present at its
    hash in Mods/<id>/.<target>/, False if any is missing or altered
    (damaged), None when there is nothing to check: that target isn't
    installed, or its entry has no `files`. No evidence is not damage. Twin of
    TavernLib's ModInstaller.VerifyModFiles."""
    rec = _read_mod_record(game_dir, mod_id)
    entry = _installed_entry(rec, target)
    files = entry.get("files") if entry else None
    if not isinstance(files, dict) or not files:
        return None
    key = (_mod_dir_path(game_dir, mod_id), target)
    stamp = (rec.get("version"), entry.get("sha256"))
    memo = _verify_memo.get(key)
    if memo and memo[0] == stamp:
        return memo[1]
    ok = _files_intact(_target_dir_path(game_dir, mod_id, target), files) is None
    _verify_memo[key] = (stamp, ok)
    return ok


def _record_base(mod):
    """A record's fields up to `installed`, in the spec's key order: the
    fetched manifest (minus screenshots) plus source_repo."""
    rec = {
        "manifest_version": mod.manifest_version,
        "id": mod.id,
        "name": mod.name,
        "version": mod.version,
        "author": mod.author,
        "description": mod.description,
        "parity_required": mod.parity_required,
    }
    # Only when set, as TavernLib's record does: absent means every scene.
    if mod.scenes is not None:
        rec["scenes"] = list(mod.scenes)
    rec["dependencies"] = dict(mod.dependencies or {})
    rec["artifacts"] = list(mod.record_artifacts or [])
    rec["source_repo"] = mod.source_repo
    return rec


def _installed_entry_for(artifact, files):
    """One `installed` entry: the artifact's sha256, the library filenames it
    pins in manifest order (uninstall reference-counts UserLibs/ by these),
    and {path: sha256} of what was placed, keys in ordinal order."""
    return {
        "sha256": artifact.sha256,
        "libraries": [_safe_basename(l.filename) for l in artifact.library_dependencies],
        "files": {k: files[k] for k in sorted(files, key=_ordinal_key)},
    }


def _reset_dir(path):
    if os.path.isdir(path):
        shutil.rmtree(path)
    os.makedirs(path, exist_ok=True)


def _displace_foreign(game_dir, mod_id):
    """Moves Mods/<id>/ into Mods/.displaced/ if it exists and isn't ours,
    never deleting it. TavernLib's ClearInstallPath does the same. Returns the
    displaced path, or None."""
    mod_dir = _mod_dir_path(game_dir, mod_id)
    if not os.path.isdir(mod_dir) or _owned_mod_record(game_dir, mod_id) is not None:
        return None

    base = _displaced_base(game_dir)
    os.makedirs(base, exist_ok=True)
    name = _safe_basename(mod_id)
    dest = os.path.join(base, name)
    # A counter, not a timestamp: predictable, and keeps every displaced copy.
    n = 2
    while os.path.exists(dest):
        dest = os.path.join(base, f"{name}.{n}")
        n += 1
    os.replace(mod_dir, dest)
    _warn(f"Mods/{name}/ already existed and wasn't installed by this launcher; "
          f"moved it to Mods/.displaced/{os.path.basename(dest)}/ rather than "
          f"deleting it. Nothing loads from there.")
    return dest


def _remove_best_effort(path):
    """Deletes a file or folder, leaving whatever a running game holds open.
    Returns whether it's gone."""
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            os.remove(path)
        except OSError:
            pass
    return not os.path.lexists(path)


def _place_target(game_dir, base, target, staging, entry):
    """Swaps a staged, verified tree in as Mods/<id>/.<target>/, then writes
    the record (enabled) last. At the record's version only .<target>/ and
    installed[target] change. At any other version, or over a record with no
    `installed`, the whole mod is replaced: everything else in the folder goes,
    best effort, and a folder a running game holds is left behind unlisted,
    so nothing loads it. A folder that isn't ours is displaced first."""
    mod_id = base["id"]
    _displace_foreign(game_dir, mod_id)
    rec = _owned_mod_record(game_dir, mod_id)
    keep = {}
    if rec and rec.get("version") == base["version"] and not _is_pre_v2(rec):
        keep = {t: e for t, e in rec["installed"].items() if t != target}

    mod_dir = _mod_dir_path(game_dir, mod_id)
    target_dir = _target_dir_path(game_dir, mod_id, target)
    os.makedirs(mod_dir, exist_ok=True)
    if os.path.isdir(target_dir):
        shutil.rmtree(target_dir)       # this target's own: failing fails the install
    os.replace(staging, target_dir)

    wanted = {RECORD_NAME, DISABLED_RECORD_NAME, os.path.basename(target_dir)}
    wanted |= {f".{t}" for t in keep}
    for name in sorted(os.listdir(mod_dir)):
        if name not in wanted and not _remove_best_effort(os.path.join(mod_dir, name)):
            _warn(f"Mods/{mod_id}/{name} couldn't be removed (is the game running?). "
                  f"It's no longer listed in the record, so nothing loads it.")

    installed = {**keep, target: entry}
    record = dict(base, installed={t: installed[t] for t in sorted(installed)})
    _write_sidecar(_mod_record_path(game_dir, mod_id), record)
    disabled = _disabled_record_path(game_dir, mod_id)
    if os.path.isfile(disabled):
        os.remove(disabled)
    _invalidate_verify_memo(game_dir, mod_id)


def install_mod(game_dir, mod, target, on_progress):
    """Installs one mod's `target` build and updates its record (see
    _place_target). "dll" places the file under Artifact.install_name, "zip"
    extracts the archive; either is verified and assembled in top-level
    staging first, so a failure never leaves a partial target folder."""
    _check_target(target)
    artifact = mod.artifact_for(target)
    if artifact is None:
        raise ModManagerError(f"{mod.name} {mod.version} has no {target} build.")
    staging = _staging_path(game_dir, mod.id)
    _reset_dir(staging)
    try:
        if artifact.package == "zip":
            with _verified_download(artifact.download_url, artifact.sha256,
                                    staging, on_progress) as tmp:
                _safe_extract_zip(tmp, staging)
        else:
            _download_verify_move(artifact.download_url, artifact.sha256,
                                  os.path.join(staging, artifact.install_name(mod.id)),
                                  on_progress)
        entry = _installed_entry_for(artifact, _hash_tree(staging))
        _place_target(game_dir, _record_base(mod), target, staging, entry)
    finally:
        if os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)
    _disable_shadowed_loose_dlls(game_dir, mod.id, _target_dir_path(game_dir, mod.id, target))


def _disable_shadowed_loose_dlls(game_dir, mod_id, target_dir):
    """Disables (renames, never deletes) a loose root Mods/<name>.dll matching
    a DLL this install placed at the top of its target folder, such as
    upstream 1.8.5's TavernEssentials.dll, so MelonLoader doesn't load the mod
    twice. Never fails the install. TavernLib's InstallMod applies the same
    rule, so the untracked set stays identical on both sides."""
    for name in sorted(os.listdir(target_dir)):
        if not name.lower().endswith(".dll") or not os.path.isfile(os.path.join(target_dir, name)):
            continue
        try:
            if disable_untracked_dll(game_dir, name):
                _warn(f"Mods/{name} was a loose copy of a DLL that {mod_id} just installed; "
                      f"disabled it so MelonLoader doesn't load it twice. It's still listed "
                      f"under untracked mods if you want it back.")
        except ModManagerError as e:
            _warn(f"Mods/{name} looks like a loose copy of {mod_id}, but couldn't be "
                  f"disabled ({e}); MelonLoader may load it twice until it's removed.")


def collect_library_dependencies(mods, target):
    """Every library the mods' `target` builds pin, deduplicated by filename
    (libraries have no id). The same filename at a different sha256 raises,
    naming both mods: the library form of a diamond conflict. The URL doesn't
    matter, only the bytes (TavernLib compares the same way)."""
    by_filename = {}     # filename -> (LibraryDependency, owning mod name)
    for mod in mods:
        artifact = mod.artifact_for(target)
        for lib in (artifact.library_dependencies if artifact else []):
            key = _safe_basename(lib.filename)
            if key not in by_filename:
                by_filename[key] = (lib, mod.name)
                continue
            existing, owner = by_filename[key]
            if existing.sha256.lower() != lib.sha256.lower():
                raise ModManagerError(
                    f"Library conflict on '{lib.filename}': {owner} pins "
                    f"{existing.download_url} (sha {existing.sha256[:12]}...) but "
                    f"{mod.name} pins {lib.download_url} (sha {lib.sha256[:12]}...). "
                    f"They can't both be installed.")
    return [lib for (lib, _owner) in by_filename.values()]


def _record_target_libraries(rec, target):
    """What a record's `target` artifact pins; [] when that can't be read."""
    try:
        artifacts, _kept = _artifacts_from_list(rec.get("artifacts"), rec["id"], warn=False)
    except ModManagerError:
        return []
    art = next((a for a in artifacts if target in a.targets), None)
    return art.library_dependencies if art else []


def _check_folder_libraries(game_dir, mods, target, libs, enabled_only=False, ignore=()):
    """Raises if a library `libs` needs is already pinned at another sha256 by
    a target installed in this folder, whichever target, since UserLibs/ is
    shared. Targets this install replaces don't count: `target` of a mod in
    `mods` at its record's version, every target at any other version. Nor do
    the ids in `ignore`, or disabled mods when `enabled_only`."""
    replacing = {m.id: m.version for m in mods}
    pins = {}       # filename -> (sha256, owner name)
    for rec in list_installed_mods(game_dir):
        if rec["id"] in ignore or (enabled_only and not rec.get("enabled")):
            continue
        for t in installed_targets(rec):
            new_version = replacing.get(rec["id"])
            if new_version is not None and (rec.get("version") != new_version or t == target):
                continue
            for lib in _record_target_libraries(rec, t):
                pins.setdefault(lib.filename, (lib.sha256, rec.get("name") or rec["id"]))
    for lib in libs:
        have = pins.get(lib.filename)
        if have and have[0].lower() != lib.sha256.lower():
            raise ModManagerError(
                f"Library conflict on '{lib.filename}': {have[1]}, already installed "
                f"in this game folder, pins sha {have[0][:12]}... but this install "
                f"needs sha {lib.sha256[:12]}.... UserLibs/ is shared by everything "
                f"installed here, so they can't both be.")


def install_library_dependency(game_dir, lib, on_progress):
    """Downloads and verifies lib into UserLibs/<filename>, beside a
    <filename>.meta.json sidecar {filename, sha256, download_url}. There is no
    version: a library is at its pinned hash or needs reinstalling."""
    name = _safe_basename(lib.filename)
    dest = os.path.join(_userlibs_dir(game_dir), name)
    _download_verify_move(lib.download_url, lib.sha256, dest, on_progress)
    _write_sidecar(_library_sidecar_path(game_dir, name), {
        "filename": name,
        "sha256": lib.sha256,
        "download_url": lib.download_url,
    })


def install_mod_closure(game_dir, mod, index, repo_bases, on_progress, target, version=None):
    """Installs a mod and its full closure for `target`: the flow one
    Community Mods click runs. `mod` is the picked ModSummary, fetched at
    `version` if given (Change Version), else mod.highest(). A dependency with
    no build for `target` is left out. Every cycle/depth/diamond/library
    conflict surfaces before anything touches disk; then install_mod_set puts
    dependencies in before the root (as TavernLib does), so a failure partway
    leaves a loadable set."""
    _check_target(target)
    fetched = {}     # (id, version) -> ModManifest

    def fetch(mod_id, version, source_repo):
        key = (mod_id, version)
        if key not in fetched:
            fetched[key] = _fetch_manifest(repo_bases, mod_id, version, prefer_repo=source_repo)
        return fetched[key]

    root = fetch(mod.id, version or mod.highest(), mod.source_repo)
    if root.artifact_for(target) is None:
        raise ModManagerError(f"{root.name} {root.version} has no {target} build, "
                              f"so it can't be installed here.")
    deps = resolve_dependencies([root], index, fetch, target)
    install_mod_set(game_dir, deps + [root], target, on_progress)


def _already_installed(game_dir, mod, target):
    """Mods/<id>/ is ours, enabled, at exactly mod.version, with `target`
    installed and not damaged."""
    rec = _read_sidecar(_mod_record_path(game_dir, mod.id))
    if not rec or rec.get("id") != mod.id or rec.get("version") != mod.version:
        return False
    try:
        parity_required(rec)
    except ModManagerError:
        return False
    return has_target(rec, target) and verify_mod_files(game_dir, mod.id, target) is not False


def install_mod_set(game_dir, mods, target, on_progress=None):
    """Installs an already-resolved set of mods (ModManifests, in the order
    given) for `target`, plus every library their builds pin, skipping a mod
    already enabled at that version with `target` intact and a library at its
    hash. A mod with no `target` build, or a library conflict within the set
    or with the folder, raises before anything is written. Progress lines
    read "[n/total] <name>: <detail>". Returns the ids (re)installed."""
    _check_target(target)
    progress = on_progress or (lambda *_a: None)
    for m in mods:
        if m.artifact_for(target) is None:
            raise ModManagerError(f"{m.name} {m.version} has no {target} build, "
                                  f"so it can't be installed here.")
    libs = collect_library_dependencies(mods, target)     # raises before any download
    _check_folder_libraries(game_dir, mods, target, libs)
    total = len(mods) + len(libs)

    def stepped(n, label):
        return lambda msg: progress(f"[{n}/{total}] {label}: {msg}")

    installed = []
    for n, m in enumerate(mods, 1):
        report = stepped(n, m.name)
        if _already_installed(game_dir, m, target):
            report(f"{m.version} already installed")
            continue
        install_mod(game_dir, m, target, report)
        installed.append(m.id)
    for n, lib in enumerate(libs, len(mods) + 1):
        report = stepped(n, lib.name)
        if _library_present(game_dir, _safe_basename(lib.filename), lib.sha256):
            report("already installed")
            continue
        install_library_dependency(game_dir, lib, report)
    return installed


def _record_libraries(rec):
    """Every UserLibs/ filename a record's installed targets pin (a pre-v2
    record's top-level `libraries`): what uninstall reference-counts.
    Unusable names are skipped."""
    inst = rec.get("installed")
    if isinstance(inst, dict):
        groups = [e.get("libraries") for e in inst.values() if isinstance(e, dict)]
    else:
        groups = [rec.get("libraries")]
    names = set()
    for group in groups:
        for x in group if isinstance(group, list) else []:
            try:
                names.add(_safe_basename(str(x)))
            except ModManagerError:
                pass
    return names


def uninstall_mod(game_dir, mod_id):
    """Deletes Mods/<id>/, every target in it, then each library it pinned
    that no remaining record's `installed` lists. Returns whether anything
    was deleted."""
    meta = _read_mod_record(game_dir, mod_id)
    removed = False
    libs = _record_libraries(meta) if meta else set()

    mod_dir = _mod_dir_path(game_dir, mod_id)
    if os.path.isdir(mod_dir):
        shutil.rmtree(mod_dir)
        removed = True

    if libs:
        # Counts DISABLED mods as still needing theirs, unlike the join flow's
        # enabled-only prune: no library pass follows an uninstall, so a
        # disabled mod would load without its libraries on its next enable.
        still_needed = set()
        for other in list_installed_mods(game_dir):
            still_needed |= _record_libraries(other)
        for lib in sorted(libs - still_needed):
            if _remove_library(game_dir, lib):
                removed = True
    return removed


def _remove_library(game_dir, filename):
    """Delete a UserLibs/ library and its sidecar."""
    removed = False
    for path in (os.path.join(_userlibs_dir(game_dir), filename),
                 _library_sidecar_path(game_dir, filename)):
        if os.path.isfile(path):
            os.remove(path)
            removed = True
    return removed


def _read_sidecar(path):
    """The file's top-level JSON object, or None if it's missing, unreadable
    or not an object (as TavernLib's ModRecord.ParseObject)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _library_present(game_dir, filename, sha256):
    """UserLibs/<filename> is on disk at exactly this sha256, by its sidecar
    AND by hashing the file - TavernLib's VerifyLibrary terms. A file at the
    right name with any other content needs reinstalling."""
    want = (sha256 or "").lower()
    meta = _read_sidecar(_library_sidecar_path(game_dir, filename))
    if not want or not meta or str(meta.get("sha256", "")).lower() != want:
        return False
    try:
        return _sha256_file(os.path.join(_userlibs_dir(game_dir), filename)).lower() == want
    except OSError:
        return False


def _read_mod_record(game_dir, mod_id):
    """The record in Mods/<id>/manifest.json, else manifest.disabled.json, if it
    has an id; else None. Lenient: no parity check (see _owned_mod_record)."""
    for path in (_mod_record_path(game_dir, mod_id),
                 _disabled_record_path(game_dir, mod_id)):
        rec = _read_sidecar(path)
        if rec and rec.get("id"):
            return rec
    return None


def _folder_record(folder, warn=False):
    """(record, enabled) if a Mods/<name>/ folder holds a usable record of
    ours, else None: the enabled record, else the disabled one, with an id
    and a readable parity_required. This is the "is this folder ours"
    predicate; TavernLib's ModRecord.Read is the same test."""
    def has_id(r):
        # A non-empty string, as ModRecord.Read requires: anything else can't
        # sort into the handshake alongside real ids.
        return r is not None and isinstance(r.get("id"), str) and r["id"] != ""

    rec, enabled = _read_sidecar(os.path.join(folder, RECORD_NAME)), True
    if not has_id(rec):
        rec, enabled = _read_sidecar(os.path.join(folder, DISABLED_RECORD_NAME)), False
    if not has_id(rec):
        return None
    try:
        parity_required(rec)
    except ModManagerError as e:
        if warn:
            _warn(f"installed mod {rec['id']} has an unusable record ({e}), "
                  f"so it is being ignored; reinstall it to fix.")
        return None
    return rec, enabled


def _owned_mod_record(game_dir, mod_id):
    """The record of Mods/<mod_id>/ if that folder is ours (_folder_record),
    else None. Listing, delete-vs-displace and mod_status all use this one
    predicate, so a folder can't read as installed to one and foreign to
    another."""
    found = _folder_record(_mod_dir_path(game_dir, mod_id))
    return found[0] if found else None


def disable_mod(game_dir, mod_id):
    """Renames the record to manifest.disabled.json, so MelonLoader stops loading
    the folder while every file stays. Returns False if nothing was enabled."""
    src = _mod_record_path(game_dir, mod_id)
    if not os.path.isfile(src):
        return False
    os.replace(src, _disabled_record_path(game_dir, mod_id))
    return True


def enable_mod(game_dir, mod_id):
    """Reverses disable_mod. Returns False if nothing was disabled."""
    src = _disabled_record_path(game_dir, mod_id)
    if not os.path.isfile(src):
        return False
    os.replace(src, _mod_record_path(game_dir, mod_id))
    return True


def mod_status(game_dir, mod_id, index, target):
    """missing / damaged / outdated / current / unknown, for `target`.
    'missing' covers a mod installed only for another target. 'damaged' comes
    before any version comparison, and a pre-v2 record reads 'damaged' so a
    reinstall moves it to the per-target layout. 'outdated' compares against
    the highest indexed version with a `target` build; one above everything
    indexed reads 'current'; 'unknown' means no index lists one."""
    meta = _owned_mod_record(game_dir, mod_id)
    if not meta or "version" not in meta:
        return "missing"
    if _is_pre_v2(meta):
        return "damaged"
    if not has_target(meta, target):
        return "missing"
    if verify_mod_files(game_dir, mod_id, target) is False:
        return "damaged"
    available = [s for s in index if s.id == mod_id and target in s.targets]
    if not available:
        return "unknown"
    highest = max(_parse_version(v) for s in available for v in s.versions)
    try:
        installed = _parse_version(meta["version"])
    except ModManagerError:
        return "unknown"
    return "current" if installed >= highest else "outdated"


def list_mods(index, target):
    """The merged index collapsed to one row per id (highest version), filtered
    to mods with a `target` build, sorted by name."""
    best = {}    # id -> ModSummary with the highest version seen
    for s in index:
        if target not in s.targets:
            continue
        cur = best.get(s.id)
        if cur is None or _parse_version(s.highest()) > _parse_version(cur.highest()):
            best[s.id] = s
    return sorted(best.values(), key=lambda s: s.name.lower())


def _scan_mods(game_dir):
    """One pass over Mods/: (installed records, untracked entries), the
    results of list_installed_mods and list_untracked_mods."""
    installed, untracked = {}, []
    mods_dir = _mods_base(game_dir)
    if not os.path.isdir(mods_dir):
        return [], []
    for name in os.listdir(mods_dir):
        if name.startswith((".", "~")):
            continue
        full = os.path.join(mods_dir, name)
        if os.path.isfile(full):
            lower = name.lower()
            if lower.endswith(".dll"):
                untracked.append({"name": name, "kind": "file", "enabled": True})
            elif lower.endswith(".dll.disabled"):
                untracked.append({"name": name[:-len(".disabled")], "kind": "file",
                                  "enabled": False})
        elif os.path.isdir(full):
            found = _folder_record(full, warn=True)
            if found is not None:
                rec, enabled = found
                rec["enabled"] = enabled        # in-memory only
                installed[rec["id"]] = rec
            elif os.path.isfile(os.path.join(full, RECORD_NAME)):
                untracked.append({"name": name, "kind": "folder", "enabled": True})
            elif os.path.isfile(os.path.join(full, DISABLED_RECORD_NAME)):
                untracked.append({"name": name, "kind": "folder", "enabled": False})
    return list(installed.values()), untracked


def list_installed_mods(game_dir):
    """Every installed mod's record, enabled or disabled, whichever targets it
    holds, each with an in-memory "enabled" flag; its `installed` map says
    which targets are here (installed_targets / has_target read it). A folder counts only with a usable record of
    ours (an id and a readable parity_required); one without is skipped with
    a warning rather than assumed required, and reads as untracked."""
    return _scan_mods(game_dir)[0]


def list_untracked_mods(game_dir):
    """Everything in Mods/ MelonLoader can load that this manager doesn't own,
    as {"name", "kind" ("file"/"folder"), "enabled"}: a loose root *.dll, or a
    first-level folder without a usable record of ours. A folder counts on a
    record file EXISTING, not parsing, because MelonLoader's marker is an
    existence check. TavernLib's ListUntrackedMods must report the same set."""
    return _scan_mods(game_dir)[1]


def disable_untracked_dll(game_dir, filename):
    """Renames a loose root Mods/<filename> to <filename>.disabled, since
    MelonLoader loads every root dll. Returns False if it wasn't there. Refuses
    rather than clobber an existing .disabled, as TavernLib's
    DisableUntrackedDll does."""
    src = os.path.join(_mods_base(game_dir), _safe_basename(filename))
    dest = src + ".disabled"
    if not os.path.isfile(src):
        return False
    if os.path.exists(dest):
        raise ModManagerError(f"{os.path.basename(dest)!r} already exists; "
                              f"remove or rename it first.")
    os.replace(src, dest)
    return True


def enable_untracked_dll(game_dir, filename):
    """Reverses disable_untracked_dll. Returns True if it was disabled and is
    now enabled, False if there was nothing disabled to enable. Refuses to
    overwrite an existing enabled file, same as disable_untracked_dll."""
    dest = os.path.join(_mods_base(game_dir), _safe_basename(filename))
    src = dest + ".disabled"
    if not os.path.isfile(src):
        return False
    if os.path.exists(dest):
        raise ModManagerError(f"{os.path.basename(dest)!r} already exists; "
                              f"remove or rename it first.")
    os.replace(src, dest)
    return True


def _untracked_stamp(game_dir, entry):
    """An untracked mod's mtime in whole Unix seconds, "?" if unreadable. Part of
    mods_hash and the twin of TavernLib's UntrackedStamp: mtime alone, because
    a directory has no portable size both sides agree on. Never a content
    hash, since this runs on every ping."""
    try:
        return str(int(os.stat(os.path.join(_mods_base(game_dir),
                                            entry["name"])).st_mtime))
    except OSError:
        return "?"


def _record_targets(rec):
    """(targets, {target: sha256}) from a record's copied artifacts, known
    targets only, sorted: what a handshake entry advertises."""
    try:
        artifacts, _kept = _artifacts_from_list(rec.get("artifacts"), rec["id"], warn=False)
    except ModManagerError as e:
        _warn(f"installed mod {rec['id']} has unreadable artifacts ({e}); "
              f"advertised with no targets.")
        return [], {}
    shas = {t: a.sha256 for a in artifacts for t in a.targets}
    return sorted(shas), {t: shas[t] for t in sorted(shas)}


def handshake_snapshot(game_dir, target="server"):
    """Fingerprint of the mods this game folder runs as `target`, for the
    ping/pong mod-sync check: enabled records of ours that list `target`
    under `installed` (a mod installed only for pc here is invisible to the
    server). Returns (mods_hash, mods_count, mods_list, untracked_list):
    - mods_hash: sha256 of newline-joined lines: id-sorted (ordinal)
      "id@version@req|opt" for those mods, then lowercase-name-sorted
      "untracked:name@stamp" (_untracked_stamp).
    - mods_count: managed mods only, so it matches mods_list.
    - mods_list: {"id", "version", "parity_required", "source_repo",
      "targets", "target_sha256"[, "scenes"]} by id, targets and shas from
      the record's artifacts. source_repo is a hint, never a source to pull
      from.
    - untracked_list: {"name", "kind"} by name, kept out of mods_list because
      nothing can resolve an entry with no id or version.

    This hash is a cross-language invariant: TavernLib's ModHandshake computes
    it byte-identically and a client compares the two. Changing the separator,
    the ordering, or which fields take part silently breaks every join against
    the other side's build; there is no version negotiation. parity_required is
    in it (a server can flip it without a version bump), and so are untracked
    stamps (a hand-dropped DLL must move it); targets, hashes and scenes are
    not."""
    installed, untracked = _scan_mods(game_dir)
    mods = sorted((m for m in installed if m.get("enabled") and has_target(m, target)),
                  key=lambda m: m["id"])
    untracked = sorted((u for u in untracked if u["enabled"]),
                       key=lambda u: u["name"].lower())
    lines = [f"{m['id']}@{m.get('version', '')}@{'req' if parity_required(m) else 'opt'}"
             for m in mods]
    lines += [f"untracked:{u['name']}@{_untracked_stamp(game_dir, u)}"
              for u in untracked]
    mods_hash = hashlib.sha256("\n".join(lines).encode()).hexdigest()
    mods_list = []
    for m in mods:
        targets, shas = _record_targets(m)
        entry = {"id": m["id"], "version": m.get("version", ""),
                 "parity_required": parity_required(m),
                 "source_repo": m.get("source_repo", ""),
                 # Advisory and outside mods_hash: a client offered a mod
                 # without its own target leaves it alone, and notices the
                 # same version built from different bytes.
                 "targets": targets, "target_sha256": shas}
        # Only when set, as TavernLib's Entry does; also outside mods_hash.
        if m.get("scenes") is not None:
            entry["scenes"] = list(m["scenes"])
        mods_list.append(entry)
    untracked_list = [{"name": u["name"], "kind": u["kind"]} for u in untracked]
    return mods_hash, len(mods_list), mods_list, untracked_list
