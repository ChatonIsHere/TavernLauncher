"""Verified installs into Mods/<id>/.<target>/ and reading back what is
there (REPO_STRUCTURE.md "Sidecar files"); twin of TavernLib's ModInstaller."""
import contextlib
import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from types import SimpleNamespace

from tavern_shared.mod_install import _download_with_progress
from tavern_shared.paths import _sha256_file

from tavern_shared.mods.base import ModManagerError, _parse_version, _warn
from tavern_shared.mods.layout import (
    DISABLED_RECORD_NAME, RECORD_NAME, _disabled_record_path, _displaced_base,
    _library_sidecar_path, _mod_dir_path, _mod_record_path, _mods_base,
    _safe_basename, _staging_path, _target_dir_path, _userlibs_dir,
)
from tavern_shared.mods.manifest import (
    SUPPORTED_MANIFEST_MAJOR, _artifacts_from_list, parity_required,
)
from tavern_shared.mods.repos import replacement_for
from tavern_shared.mods.resolve import _fetch_manifest, resolve_dependencies


# Enforced on the decompressed bytes, so a bomb can't fill the disk.
_ZIP_MAX_ENTRIES = 2000
_ZIP_MAX_TOTAL_UNCOMPRESSED = 500 * 1024 * 1024


def _sidecar_bytes(data):
    """The spec's record bytes, built as bytes so no newline translation can
    touch them. Twin of TavernLib's record writer."""
    return json.dumps(data, indent=2, ensure_ascii=True).replace("\n", "\r\n").encode("ascii")


def _write_sidecar(path, data):
    # Via a temp, so a cut-short write can't leave a record that reads foreign.
    tmp = os.path.join(os.path.dirname(path), f".{os.path.basename(path)}.tmp")
    with open(tmp, "wb") as f:
        f.write(_sidecar_bytes(data))
    os.replace(tmp, path)


@contextlib.contextmanager
def _verified_download(url, sha256, work_dir, on_progress):
    """Yields a temp file in work_dir holding url's verified bytes. Same
    volume as the destination, so the caller's os.replace can't fail with
    WinError 17."""
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
        try:
            os.remove(tmp)
        except OSError:
            pass


def _download_verify_move(url, sha256, dest_path, on_progress):
    with _verified_download(url, sha256, os.path.dirname(dest_path), on_progress) as tmp:
        os.replace(tmp, dest_path)


def _safe_extract_zip(zip_path, dest_dir):
    """Raises on the first absolute, colon-holding, symlinked or escaping
    member, or past the caps; callers extract into throwaway staging."""
    dest_root = os.path.realpath(dest_dir)
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        if len(infos) > _ZIP_MAX_ENTRIES:
            raise ModManagerError(
                f"This mod archive has {len(infos)} entries, over the "
                f"{_ZIP_MAX_ENTRIES} limit; refusing to extract it.")
        # Written after the tree is hashed, so an archive's own copy would
        # read as damage forever. Twin of TavernLib's SafeExtractZip.
        record_paths = {os.path.normcase(os.path.join(dest_root, n))
                        for n in (RECORD_NAME, DISABLED_RECORD_NAME)}
        written = 0
        for info in infos:
            name = info.filename
            if name.startswith(("/", "\\")):
                raise ModManagerError(
                    f"Unsafe archive entry {name!r}: absolute path.")
            # Anywhere, not just a drive: an NTFS stream passes the realpath check.
            if ":" in name:
                raise ModManagerError(
                    f"Unsafe archive entry {name!r}: contains a colon (drive "
                    f"letter or NTFS alternate data stream).")
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
            # Counted as produced: a bomb can understate the header's size.
            with zf.open(info) as src, open(target, "wb") as dst:
                for chunk in iter(lambda: src.read(1024 * 1024), b""):
                    written += len(chunk)
                    if written > _ZIP_MAX_TOTAL_UNCOMPRESSED:
                        raise ModManagerError(
                            f"This mod archive extracts to more than "
                            f"{_ZIP_MAX_TOTAL_UNCOMPRESSED // (1024 * 1024)} MB; "
                            f"refusing to extract it (possible zip bomb).")
                    dst.write(chunk)


def _hash_tree(base):
    """{path relative to base, forward slashes: sha256}. Twin of TavernLib's
    ModInstaller.HashTree."""
    out = {}
    for root, _dirs, names in os.walk(base):
        for name in names:
            full = os.path.join(root, name)
            out[os.path.relpath(full, base).replace(os.sep, "/")] = _sha256_file(full)
    return out


def _ordinal_key(path):
    # C#'s ordinal (UTF-16 code unit) order; differs from Python's above U+FFFF.
    return path.encode("utf-16-be", "surrogatepass")


def installed_targets(rec):
    inst = (rec or {}).get("installed")
    if not isinstance(inst, dict):
        return []
    return sorted(t for t, e in inst.items() if isinstance(e, dict))


def _installed_entry(rec, target):
    inst = (rec or {}).get("installed")
    entry = inst.get(target) if isinstance(inst, dict) else None
    return entry if isinstance(entry, dict) else None


def _is_pre_v2(rec):
    return not isinstance(rec.get("installed"), dict)


def has_target(rec, target):
    return target in installed_targets(rec)


# (mod folder, target) -> ((version, sha256), ok). A zip bundle can be hundreds
# of MB, so an unchanged install is hashed once; installs and restores drop a
# mod's entries, the Mod Manager's Refresh clears all.
_verify_memo = {}


def _invalidate_verify_memo(game_dir, mod_id):
    mod_dir = _mod_dir_path(game_dir, mod_id)
    for key in [k for k in _verify_memo if k[0] == mod_dir]:
        del _verify_memo[key]


def _files_intact(base, files):
    """The first path in `files` missing or altered under base, else None."""
    for rel, expected in files.items():
        path = os.path.join(base, rel.replace("/", os.sep))
        if not os.path.isfile(path) or _sha256_file(path).lower() != str(expected).lower():
            return rel
    return None


def verify_mod_files(game_dir, mod_id, target):
    """True if intact, False if damaged, None when there's no `files` to check
    (no evidence is not damage). Twin of TavernLib's VerifyModFiles."""
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
    """A record's fields before `installed`, in the spec's key order."""
    rec = {
        "manifest_version": SUPPORTED_MANIFEST_MAJOR,
        "id": mod.id,
        "name": mod.name,
        "version": mod.version,
        "author": mod.author,
        "description": mod.description,
        "parity_required": mod.parity_required,
    }
    if mod.scenes is not None:
        rec["scenes"] = mod.scenes
    rec["dependencies"] = mod.dependencies
    rec["artifacts"] = mod.record_artifacts
    rec["source_repo"] = mod.source_repo
    return rec


def _installed_entry_for(artifact, files):
    return {
        "sha256": artifact.sha256,
        "libraries": [_safe_basename(l.filename) for l in artifact.library_dependencies],
        "files": {k: files[k] for k in sorted(files, key=_ordinal_key)},
    }


def _displace_foreign(game_dir, mod_id):
    """Moves a Mods/<id>/ that isn't ours into Mods/.displaced/, never
    deleting it, as TavernLib's ClearInstallPath does."""
    mod_dir = _mod_dir_path(game_dir, mod_id)
    if not os.path.isdir(mod_dir) or _owned_mod_record(game_dir, mod_id) is not None:
        return
    base = _displaced_base(game_dir)
    os.makedirs(base, exist_ok=True)
    name = _safe_basename(mod_id)
    dest = os.path.join(base, name)
    n = 2
    while os.path.exists(dest):
        dest = os.path.join(base, f"{name}.{n}")
        n += 1
    os.replace(mod_dir, dest)
    _warn(f"Mods/{name}/ already existed and wasn't installed by this launcher; "
          f"moved it to Mods/.displaced/{os.path.basename(dest)}/ rather than "
          f"deleting it. Nothing loads from there.")


def _remove_best_effort(path):
    """Whether path is gone; whatever a running game holds open stays."""
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    else:
        try:
            os.remove(path)
        except OSError:
            pass
    return not os.path.lexists(path)


def _place_target(game_dir, base, target, staging, entry):
    """The one write path for a target folder: swaps the staged tree in, then
    writes the record (enabled) last, per the spec's one-version rule."""
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
    artifact = mod.artifact_for(target)
    if artifact is None:
        raise ModManagerError(f"{mod.name} {mod.version} has no {target} build.")
    staging = _staging_path(game_dir, mod.id)
    if os.path.isdir(staging):
        shutil.rmtree(staging)
    os.makedirs(staging)
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
        shutil.rmtree(staging, ignore_errors=True)
    _disable_shadowed_loose_dlls(game_dir, mod.id, _target_dir_path(game_dir, mod.id, target))


def _disable_shadowed_loose_dlls(game_dir, mod_id, target_dir):
    """Disables a loose Mods/<name>.dll that a DLL just placed at the top of
    the target folder shadows (upstream 1.8.5's TavernEssentials.dll), so
    MelonLoader doesn't load the mod twice. Twin of TavernLib's InstallMod."""
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
    """Every library the mods' `target` builds pin, by filename; the same
    filename at another sha256 raises, as in TavernLib."""
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
    return [lib for lib, _owner in by_filename.values()]


def _record_target_libraries(rec, target):
    try:
        artifacts, _kept = _artifacts_from_list(rec.get("artifacts"), rec["id"], warn=False)
    except ModManagerError:
        return []
    art = next((a for a in artifacts if target in a.targets), None)
    return art.library_dependencies if art else []


def _check_folder_libraries(game_dir, mods, target, libs, enabled_only=False, ignore=()):
    """Raises if a library in `libs` is pinned at another sha256 by any target
    installed in this folder (UserLibs/ is shared), except what this install
    replaces: `target` of a mod in `mods` at its record's version, every
    target at any other version."""
    replacing = {m.id: m.version for m in mods}
    pins = {}       # filename -> (sha256, owner name)
    for rec in list_installed_mods(game_dir):
        if rec["id"] in ignore or (enabled_only and not rec.get("enabled")):
            continue
        new_version = replacing.get(rec["id"])
        for t in installed_targets(rec):
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
    name = _safe_basename(lib.filename)
    _download_verify_move(lib.download_url, lib.sha256,
                          os.path.join(_userlibs_dir(game_dir), name), on_progress)
    _write_sidecar(_library_sidecar_path(game_dir, name), {
        "filename": name,
        "sha256": lib.sha256,
        "download_url": lib.download_url,
    })


def plan_mod_install(game_dir, mod, index, repo_bases, target, version=None):
    """One Get Mods install, before anything is written: (the ModSummary `mod`'s
    manifest at `version`, default its highest; its closure for `target`; the
    part of that closure not already installed as resolved)."""
    root = _fetch_manifest(repo_bases, mod.id, version or mod.highest(), mod.source_repo)
    if root.artifact_for(target) is None:
        raise ModManagerError(f"{root.name} {root.version} has no {target} build, "
                              f"so it can't be installed here.")
    deps = resolve_dependencies([root], index, repo_bases, target)
    return root, deps, [d for d in deps if not _already_installed(game_dir, d, target)]


def install_mod_closure(game_dir, mod, index, repo_bases, on_progress, target, version=None):
    """One Get Mods install: `mod` and its closure for `target`."""
    root, deps, _ = plan_mod_install(game_dir, mod, index, repo_bases, target, version)
    install_mod_set(game_dir, deps + [root], target, on_progress)


def missing_dependencies(rec, installed, index, target):
    """What an installed mod's record depends on that isn't met in this game
    folder: [(id, minimum, why)], why "missing", "disabled" or "too old". A
    dependency the index lists with no `target` build is left out, as the
    resolver skips it."""
    have = {r["id"]: r for r in installed if r.get("id") and has_target(r, target)}
    listed = {s.id for s in index}
    buildable = {s.id for s in index if target in s.targets}
    missing = []
    for dep_id, min_v in sorted((rec.get("dependencies") or {}).items()):
        if dep_id in listed and dep_id not in buildable:
            continue
        r = have.get(dep_id)
        if r is None:
            missing.append((dep_id, min_v, "missing"))
        elif not r.get("enabled", True):
            missing.append((dep_id, min_v, "disabled"))
        else:
            try:
                want, got = _parse_version(min_v), _parse_version(r.get("version", ""))
            except ModManagerError:
                continue
            if got[0] != want[0] or got < want:
                missing.append((dep_id, min_v, "too old"))
    return missing


def install_dependencies(game_dir, rec, index, repo_bases, target, on_progress):
    """Installs the closure an installed mod's record asks for, leaving the mod
    itself alone. The record stands in for its manifest, so this works for a
    version its source no longer lists."""
    root = SimpleNamespace(id=rec["id"], name=rec.get("name") or rec["id"],
                           dependencies=rec.get("dependencies") or {})
    deps = resolve_dependencies([root], index, repo_bases, target)
    return install_mod_set(game_dir, deps, target, on_progress)


def replaced_by(index, rec):
    """The Replacement that retires an installed record: only the one its own
    source declared, so another source can't take it over."""
    return replacement_for(index, rec.get("id"), rec.get("source_repo") or "")


def retire_replaced(game_dir, index, on_progress):
    """Uninstalls each installed mod its source has replaced, once the
    replacement is installed and enabled for every target the old one is, so
    the two never both load. Returns [(old id, new id)] for each removed."""
    recs = {r["id"]: r for r in list_installed_mods(game_dir)}
    removed = []
    for old_id, rec in sorted(recs.items()):
        rep = replaced_by(index, rec)
        new = recs.get(rep.by) if rep else None
        if not new or not new.get("enabled", True):
            continue
        if all(has_target(new, t) for t in installed_targets(rec)):
            on_progress(f"Removing {rec.get('name') or old_id}: {rep.by} replaces it.")
            uninstall_mod(game_dir, old_id)
            removed.append((old_id, rep.by))
    return removed


def _already_installed(game_dir, mod, target):
    rec = _read_sidecar(_mod_record_path(game_dir, mod.id))
    if not rec or rec.get("id") != mod.id or rec.get("version") != mod.version:
        return False
    try:
        parity_required(rec)
    except ModManagerError:
        return False
    return has_target(rec, target) and verify_mod_files(game_dir, mod.id, target) is not False


def install_mod_set(game_dir, mods, target, on_progress):
    """Installs resolved ModManifests in order, then their libraries, skipping
    what's already enabled and intact. Every conflict raises before anything
    is written. Returns the ids (re)installed."""
    for m in mods:
        if m.artifact_for(target) is None:
            raise ModManagerError(f"{m.name} {m.version} has no {target} build, "
                                  f"so it can't be installed here.")
    libs = collect_library_dependencies(mods, target)
    _check_folder_libraries(game_dir, mods, target, libs)
    total = len(mods) + len(libs)

    def stepped(n, label):
        return lambda msg: on_progress(f"[{n}/{total}] {label}: {msg}")

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
    """Every UserLibs/ filename a record's targets pin (a pre-v2 record's
    top-level `libraries`)."""
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
    """Deletes Mods/<id>/, then each library it pinned that no remaining
    record lists. Returns whether anything was deleted."""
    meta = _read_mod_record(game_dir, mod_id)
    removed = False
    libs = _record_libraries(meta) if meta else set()

    mod_dir = _mod_dir_path(game_dir, mod_id)
    if os.path.isdir(mod_dir):
        shutil.rmtree(mod_dir)
        removed = True

    if libs:
        # Disabled mods count too, unlike the join's prune: no library pass
        # follows an uninstall, so their next enable would lack them.
        still_needed = set()
        for other in list_installed_mods(game_dir):
            still_needed |= _record_libraries(other)
        for lib in sorted(libs - still_needed):
            if _remove_library(game_dir, lib):
                removed = True
    return removed


def _remove_library(game_dir, filename):
    removed = False
    for path in (os.path.join(_userlibs_dir(game_dir), filename),
                 _library_sidecar_path(game_dir, filename)):
        if os.path.isfile(path):
            os.remove(path)
            removed = True
    return removed


def _read_sidecar(path):
    """The file's JSON object, else None, as TavernLib's ModRecord.ParseObject."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _library_present(game_dir, filename, sha256):
    """By its sidecar and by hashing the file: TavernLib's VerifyLibrary terms."""
    want = (sha256 or "").lower()
    meta = _read_sidecar(_library_sidecar_path(game_dir, filename))
    if not want or not meta or str(meta.get("sha256", "")).lower() != want:
        return False
    try:
        return _sha256_file(os.path.join(_userlibs_dir(game_dir), filename)).lower() == want
    except OSError:
        return False


def _read_mod_record(game_dir, mod_id):
    """The enabled, else disabled, record with an id. Lenient: no ownership
    check (see _folder_record)."""
    for path in (_mod_record_path(game_dir, mod_id),
                 _disabled_record_path(game_dir, mod_id)):
        rec = _read_sidecar(path)
        if rec and rec.get("id"):
            return rec
    return None


def _folder_record(folder, warn=False):
    """(record, enabled) if the folder is ours, else None: the spec's
    ownership predicate, twin of TavernLib's ModRecord.Read."""
    def has_id(r):
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
    found = _folder_record(_mod_dir_path(game_dir, mod_id))
    return found[0] if found else None


def _rename(src, dest):
    if not os.path.isfile(src):
        return False
    os.replace(src, dest)
    return True


def disable_mod(game_dir, mod_id):
    return _rename(_mod_record_path(game_dir, mod_id), _disabled_record_path(game_dir, mod_id))


def enable_mod(game_dir, mod_id):
    return _rename(_disabled_record_path(game_dir, mod_id), _mod_record_path(game_dir, mod_id))


def mod_status(game_dir, mod_id, index, target):
    """missing / damaged / outdated / current / unknown, for `target`. A
    pre-v2 record reads damaged, so a reinstall moves it to the per-target
    layout; a version above everything indexed reads current."""
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


def _highest_by_id(summaries):
    best = {}
    for s in summaries:
        cur = best.get(s.id)
        if cur is None or _parse_version(s.highest()) > _parse_version(cur.highest()):
            best[s.id] = s
    return best


def list_mods(index, target):
    """One row per id (its highest major) with a `target` build, by name."""
    return sorted(_highest_by_id(s for s in index if target in s.targets).values(),
                  key=lambda s: s.name.lower())


def _scan_mods(game_dir):
    """(installed records, each with an in-memory "enabled"; untracked
    {"name", "kind", "enabled"}). An untracked folder counts on its record
    file existing, as MelonLoader's marker does. Twin of TavernLib's
    ListUntrackedMods."""
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
                rec["enabled"] = enabled
                installed[rec["id"]] = rec
            elif os.path.isfile(os.path.join(full, RECORD_NAME)):
                untracked.append({"name": name, "kind": "folder", "enabled": True})
            elif os.path.isfile(os.path.join(full, DISABLED_RECORD_NAME)):
                untracked.append({"name": name, "kind": "folder", "enabled": False})
    return list(installed.values()), untracked


def list_installed_mods(game_dir):
    return _scan_mods(game_dir)[0]


def list_untracked_mods(game_dir):
    return _scan_mods(game_dir)[1]


def _move_untracked_dll(src, dest):
    # Refuses rather than clobber, as TavernLib's DisableUntrackedDll does.
    if os.path.isfile(src) and os.path.exists(dest):
        raise ModManagerError(f"{os.path.basename(dest)!r} already exists; "
                              f"remove or rename it first.")
    return _rename(src, dest)


def disable_untracked_dll(game_dir, filename):
    path = os.path.join(_mods_base(game_dir), _safe_basename(filename))
    return _move_untracked_dll(path, path + ".disabled")


def enable_untracked_dll(game_dir, filename):
    path = os.path.join(_mods_base(game_dir), _safe_basename(filename))
    return _move_untracked_dll(path + ".disabled", path)


def _untracked_stamp(game_dir, entry):
    """Whole-second mtime, "?" if unreadable. Twin of TavernLib's
    UntrackedStamp."""
    try:
        return str(int(os.stat(os.path.join(_mods_base(game_dir),
                                            entry["name"])).st_mtime))
    except OSError:
        return "?"


def _record_targets(rec):
    """(targets, {target: sha256}) from a record's artifacts, known targets only."""
    try:
        artifacts, _kept = _artifacts_from_list(rec.get("artifacts"), rec["id"], warn=False)
    except ModManagerError as e:
        _warn(f"installed mod {rec['id']} has unreadable artifacts ({e}); "
              f"advertised with no targets.")
        return [], {}
    shas = {t: a.sha256 for a in artifacts for t in a.targets}
    return sorted(shas), {t: shas[t] for t in sorted(shas)}


def handshake_snapshot(game_dir, target="server"):
    """(mods_hash, mods_count, mods_list, untracked_list) for the mods this
    folder runs as `target`, per REPO_STRUCTURE.md "What a server advertises".
    Twin of TavernLib's ModHandshake: any change to the hash breaks every join
    against the other side."""
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
                 "targets": targets, "target_sha256": shas}
        if m.get("scenes") is not None:
            entry["scenes"] = list(m["scenes"])
        mods_list.append(entry)
    untracked_list = [{"name": u["name"], "kind": u["kind"]} for u in untracked]
    return mods_hash, len(mods_list), mods_list, untracked_list
