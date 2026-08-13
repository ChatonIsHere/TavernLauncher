"""Putting a verified artifact on disk, and reading back what is there.
Nothing here touches the network except through _verified_download."""
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
    _safe_basename, _staging_path, _userlibs_dir,
)
from tavern_shared.mods.manifest import parity_required
from tavern_shared.mods.repos import _norm
from tavern_shared.mods.resolve import _fetch_manifest, resolve_dependencies
from tavern_shared.mods.version import _parse_version


# Zip-bundle extraction caps (package="zip"). The official repo's validator
# (CommunityMods/tools/modindex.py) checks the same intent against the archive's
# header sizes; these run at install time against ACTUAL decompressed bytes, so
# they also protect the one path that never saw the validator: a mod from an
# unreviewed third-party repo. Extraction aborts (into throwaway staging) the
# moment either cap is crossed, so a decompression bomb can't fill the disk.
_ZIP_MAX_ENTRIES = 2000


_ZIP_MAX_TOTAL_UNCOMPRESSED = 500 * 1024 * 1024   # 500 MB extracted, summed


def _write_sidecar(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


@contextlib.contextmanager
def _verified_download(url, sha256, work_dir, on_progress):
    """Download `url` to a temp file inside `work_dir` and verify its sha256,
    then hand the temp path to the caller to move (single file) or extract
    (zip). The temp is always created in work_dir, never the system temp dir, so
    a follow-up os.replace within the same tree is a same-volume atomic rename;
    routing through %TEMP% (usually C:) would make that a cross-volume move,
    which fails with WinError 17 when the game is on another drive. The temp is
    always cleaned up, so a failed verify/move leaves nothing behind."""
    download = _download_with_progress
    hashfn = _sha256_file

    os.makedirs(work_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tavern_dl_", suffix=".part", dir=work_dir)
    os.close(fd)
    try:
        download(url, tmp, on_progress)
        actual = hashfn(tmp)
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
    """Download, verify, then atomically move one file into dest_path. Never
    leaves a half-written or unverified file behind. The temp lives in
    dest_path's own directory so the move is a same-volume rename."""
    dest_dir = os.path.dirname(dest_path)
    with _verified_download(url, sha256, dest_dir, on_progress) as tmp:
        os.replace(tmp, dest_path)


def _safe_extract_zip(zip_path, dest_dir):
    """Extract a .zip into dest_dir, refusing any member that would escape it
    (zip-slip) or that is a symlink, and capping entry count + total decompressed
    size so a decompression bomb can't fill the disk. This is the one new attack
    surface bundles add, and it matters because a mod can come from an unreviewed
    source: a member named '..\\..\\evil.dll' or an absolute path would otherwise
    write anywhere on disk. Every member's resolved path must stay within dest_dir,
    and the running decompressed total must stay under _ZIP_MAX_TOTAL_UNCOMPRESSED;
    the first unsafe/oversized entry raises, having written only whatever came
    before it (the caller builds into a throwaway staging dir, so a raise discards
    the partial extract)."""
    dest_root = os.path.realpath(dest_dir)
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        if len(infos) > _ZIP_MAX_ENTRIES:
            raise ModManagerError(
                f"This mod archive has {len(infos)} entries, over the "
                f"{_ZIP_MAX_ENTRIES} limit; refusing to extract it.")
        written = 0
        for info in infos:
            name = info.filename
            # Absolute paths never belong in a mod archive, and a colon
            # anywhere is refused rather than just a drive letter in position
            # 1: on NTFS "mod.dll:payload" writes an alternate data stream
            # hanging off mod.dll instead of a file, which the realpath
            # containment check below reads as staying inside dest_root.
            if name.startswith(("/", "\\")):
                raise ModManagerError(
                    f"Unsafe archive entry {name!r}: absolute path.")
            if ":" in name:
                raise ModManagerError(
                    f"Unsafe archive entry {name!r}: contains a colon (drive "
                    f"letter or NTFS alternate data stream).")
            # Symlinks (unix mode in the high 16 bits of external_attr) could
            # point outside the folder; refuse them outright.
            mode = (info.external_attr >> 16) & 0xFFFF
            if mode and (mode & 0o170000) == 0o120000:
                raise ModManagerError(
                    f"Unsafe archive entry {name!r}: symlink.")
            rel = name.replace("/", os.sep).replace("\\", os.sep)
            target = os.path.realpath(os.path.join(dest_root, rel))
            if target != dest_root and not target.startswith(dest_root + os.sep):
                raise ModManagerError(
                    f"Unsafe archive entry {name!r}: escapes the mod folder.")
            if info.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            # Copy in chunks against the ACTUAL decompressed size, not the header's
            # declared file_size (which a bomb can understate), aborting the moment
            # the running total crosses the cap.
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


def _mod_record(mod):
    """The record written to Mods/<id>/manifest.json: the fetched manifest
    verbatim (so status/uninstall need no re-fetch) plus install fields. Kept a
    plain JSON object on purpose: it's also MelonLoader's folder marker, and a
    future MelonLoader that parses it shouldn't choke. Keep this shape stable so
    any other installer can read it too.

    Note there is NO placed-dll filename here: every operation works on the
    Mods/<id>/ folder (install swaps it, uninstall deletes it, update replaces it),
    so nothing ever needs to find the .dll by name; recording it would be dead
    weight."""
    return {
        "manifest_version": mod.manifest_version,
        "id": mod.id,
        "name": mod.name,
        "version": mod.version,
        "author": mod.author,
        "description": mod.description,
        "client_side": mod.client_side,
        "server_side": mod.server_side,
        # Recorded so a server can report it in the handshake without re-fetching
        # the manifest, and so the client can tell what a mod it already has is.
        "parity_required": mod.parity_required,
        "dependencies": dict(mod.dependencies or {}),
        "library_dependencies": [
            {"name": l.name, "download_url": l.download_url,
             "sha256": l.sha256, "filename": l.filename}
            for l in (mod.library_dependencies or [])],
        "download_url": mod.download_url,
        "sha256": mod.sha256,
        "source_repo": mod.source_repo,
        "package": mod.package,
        # The libraries this mod pins, by filename. uninstall_mod uses these to
        # reference-count UserLibs/ files so an orphaned library is removed while
        # one another installed mod still lists stays put.
        "libraries": [_safe_basename(l.filename)
                      for l in (mod.library_dependencies or [])],
    }


def _reset_dir(path):
    if os.path.isdir(path):
        shutil.rmtree(path)
    os.makedirs(path, exist_ok=True)


def _clear_install_path(game_dir, mod_id):
    """Frees Mods/<id>/ for an install. A previous install of ours is deleted -
    it's being replaced, and it's already cached if anything wanted it kept. A
    folder we DIDN'T install is moved into Mods/.displaced/ instead, never
    deleted: those are someone's own files, and silently destroying them is the
    one outcome nothing here can undo.

    Ours vs. theirs is decided by whether a usable record is present - exactly
    the test list_installed_mods and list_untracked_mods apply, so a folder
    can't count as untracked for listing and as ours for deletion. TavernLib's
    ClearInstallPath does the same."""
    mod_dir = _mod_dir_path(game_dir, mod_id)
    if not os.path.isdir(mod_dir):
        return None

    if _owned_mod_record(game_dir, mod_id) is not None:
        shutil.rmtree(mod_dir)
        return None

    base = _displaced_base(game_dir)
    os.makedirs(base, exist_ok=True)
    name = _safe_basename(mod_id)
    dest = os.path.join(base, name)
    # Counter rather than a timestamp: displacing the same name twice has to keep
    # both, and a deterministic suffix is one a person can predict.
    n = 2
    while os.path.exists(dest):
        dest = os.path.join(base, f"{name}.{n}")
        n += 1
    os.replace(mod_dir, dest)
    _warn(f"Mods/{name}/ already existed and wasn't installed by this launcher; "
          f"moved it to Mods/.displaced/{os.path.basename(dest)}/ rather than "
          f"deleting it. Nothing loads from there.")
    return dest


def install_mod(game_dir, mod, on_progress):
    """Installs one mod into its own folder, Mods/<id>/, and writes the record +
    MelonLoader marker Mods/<id>/manifest.json inside it.

    package="dll": mod.download_url is a single .dll placed under the name it was
    published with (mod.install_name(), traversal-guarded); nothing looks it up by
    name afterwards.
    package="zip": it's an archive extracted into Mods/<id>/, so the mod can ship
    several DLLs plus Unity asset bundles / addressables. Either way one artifact
    is downloaded and verified against mod.sha256 before anything is written.

    The mod is assembled in a throwaway staging folder and swapped into place only
    once it's complete, so a failed download/verify/extract never leaves a partial
    or half-upgraded mod folder, and any pre-existing install is replaced
    atomically."""
    mod_dir = _mod_dir_path(game_dir, mod.id)
    staging = _staging_path(game_dir, mod.id)
    _reset_dir(staging)
    try:
        if mod.package == "zip":
            with _verified_download(mod.download_url, mod.sha256, staging, on_progress) as tmp:
                _safe_extract_zip(tmp, staging)
        else:
            _download_verify_move(mod.download_url, mod.sha256,
                                  os.path.join(staging, mod.install_name()), on_progress)
        _write_sidecar(os.path.join(staging, RECORD_NAME), _mod_record(mod))

        # Swap staging -> Mods/<id>/ atomically, clearing any prior install first.
        _clear_install_path(game_dir, mod.id)
        os.replace(staging, mod_dir)
    finally:
        if os.path.isdir(staging):
            shutil.rmtree(staging, ignore_errors=True)


def collect_library_dependencies(mods):
    """Gathers every library_dependencies entry across a resolved set of mods
    (typically the top-level mod plus resolve_dependencies' result), deduplicated
    by filename (no id to graph by, so filename stands in for identity). Same
    filename + matching sha256/download_url collapses to one (install once); same
    filename + different sha256/download_url raises a conflict naming both mods
    and both URLs, the library equivalent of a diamond conflict, by exact match
    since there's no version-range concept for a pinned third-party file."""
    by_filename = {}     # filename -> (LibraryDependency, owning mod name)
    for mod in mods:
        for lib in (mod.library_dependencies or []):
            key = _safe_basename(lib.filename)
            if key not in by_filename:
                by_filename[key] = (lib, mod.name)
                continue
            existing, owner = by_filename[key]
            if (existing.sha256.lower() != lib.sha256.lower()
                    or _norm(existing.download_url) != _norm(lib.download_url)):
                raise ModManagerError(
                    f"Library conflict on '{lib.filename}': {owner} pins "
                    f"{existing.download_url} (sha {existing.sha256[:12]}...) but "
                    f"{mod.name} pins {lib.download_url} (sha {lib.sha256[:12]}...). "
                    f"They can't both be installed.")
    return [lib for (lib, _owner) in by_filename.values()]


def install_library_dependency(game_dir, lib, on_progress):
    """Same shape as install_mod: downloads lib.download_url, verifies against
    lib.sha256, writes <game_dir>/UserLibs/<safe filename>. Sidecar goes to
    <game_dir>/UserLibs/<filename>.meta.json, beside the .dll it describes
    (filename is the key, no id) recording {filename, sha256, download_url}: no
    source_repo (never came from a configured index) and no version (nothing to
    compare against; a library is either at its pinned hash or needs reinstalling,
    there's no "outdated" state for it)."""
    name = _safe_basename(lib.filename)
    dest = os.path.join(_userlibs_dir(game_dir), name)
    _download_verify_move(lib.download_url, lib.sha256, dest, on_progress)
    _write_sidecar(_library_sidecar_path(game_dir, name), {
        "filename": name,
        "sha256": lib.sha256,
        "download_url": lib.download_url,
    })


def install_mod_closure(game_dir, mod, index, repo_bases, on_progress, side, version=None):
    """Installs a mod AND its full closure, the flow a single COMMUNITY MODS
    click runs. `mod` is the ModSummary the user picked; its manifest is
    fetched at `version` if given, else mod.highest() (the ordinary
    install/update path; an explicit version is how Community Mods' right-click
    "Change Version" installs an older/different release instead), then
    resolve_dependencies walks the graph fetching each dependency's manifest,
    and collect_library_dependencies gathers the pinned libraries. Every
    cycle/depth/diamond/library conflict is surfaced (and every manifest
    fetched) BEFORE anything touches disk. A mod with no dependencies is just
    the degenerate case: empty closure, one install_mod.

    `side` is this launcher's own ("client" or "server"), passed straight to
    resolve_dependencies so a dependency that can't run here is left out rather
    than installed into a process that would crash loading it.

    Manifests fetched during a single closure are cached, so a diamond (two mods
    needing the same dependency) fetches it once."""
    fetched = {}     # (id, version) -> ModManifest

    def fetch(mod_id, version, source_repo):
        key = (mod_id, version)
        if key not in fetched:
            fetched[key] = _fetch_manifest(repo_bases, mod_id, version, prefer_repo=source_repo)
        return fetched[key]

    root = fetch(mod.id, version or mod.highest(), mod.source_repo)
    deps = resolve_dependencies([root], index, fetch, side)
    all_mods = [root] + deps
    libs = collect_library_dependencies(all_mods)     # raises on conflict before any download

    total = len(all_mods) + len(libs)
    step = {"n": 0}

    def stepped(label):
        def report(msg):
            on_progress(f"[{step['n']}/{total}] {label}: {msg}")
        return report

    for m in all_mods:
        step["n"] += 1
        install_mod(game_dir, m, stepped(m.name))
    for lib in libs:
        step["n"] += 1
        install_library_dependency(game_dir, lib, stepped(lib.name))


def uninstall_mod(game_dir, mod_id):
    """Removes an installed community mod: its whole Mods/<id>/ folder. Each
    library the mod pinned is then removed from UserLibs/ too, UNLESS another
    still-installed mod's record lists it: shared libraries stay, orphaned ones go.
    Returns True if anything was deleted, False if nothing was installed under that
    id."""
    meta = _read_mod_record(game_dir, mod_id)
    removed = False
    libs = [_safe_basename(str(x)) for x in (meta.get("libraries") if meta else []) or []]

    mod_dir = _mod_dir_path(game_dir, mod_id)
    if os.path.isdir(mod_dir):
        shutil.rmtree(mod_dir)
        removed = True

    if libs:
        # After removing this mod's record, gather what the remaining mods still
        # pin, then drop any library nobody else needs.
        still_needed = set()
        for other in list_installed_mods(game_dir):
            for x in other.get("libraries", []):
                still_needed.add(_safe_basename(str(x)))
        for lib in libs:
            if lib in still_needed:
                continue
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
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _read_mod_record(game_dir, mod_id):
    """The install record for a mod, from whichever state it's in: the enabled
    folder record (Mods/<id>/manifest.json), then the disabled one
    (Mods/<id>/manifest.disabled.json). A record is only accepted if it carries an
    "id" (a bare manifest.json marker or a library sidecar without one isn't a mod
    record). Returns None if neither has it."""
    for path in (_mod_record_path(game_dir, mod_id),
                 _disabled_record_path(game_dir, mod_id)):
        rec = _read_sidecar(path)
        if rec and rec.get("id"):
            return rec
    return None


def _owned_mod_record(game_dir, mod_id):
    """_read_mod_record, further requiring a usable parity_required - the full
    "is this folder OURS?" test. It has to be the same predicate
    list_installed_mods applies (id present, parity readable), so that a folder
    can never read as installed to one caller and as foreign to another:
    list_untracked_mods uses this to decide what's already claimed, and
    _clear_install_path uses it to decide delete-vs-displace. TavernLib's
    ModRecord.Read is the same test (its parity field is Required.Always, so an
    unusable record reads as no record there too). Returns None for a folder
    that isn't ours by that test."""
    rec = _read_mod_record(game_dir, mod_id)
    if rec is None:
        return None
    try:
        parity_required(rec)
    except ModManagerError:
        return None
    return rec


def disable_mod(game_dir, mod_id):
    """Disable an installed folder-layout mod WITHOUT uninstalling it: rename its
    Mods/<id>/manifest.json to manifest.disabled.json. The folder then has no
    manifest.json, so MelonLoader stops scanning it and the mod stops loading,
    while every file stays in place. Returns True if it was enabled and is now
    disabled; False if there was nothing enabled to disable (already disabled or
    not installed)."""
    src = _mod_record_path(game_dir, mod_id)
    if not os.path.isfile(src):
        return False
    os.replace(src, _disabled_record_path(game_dir, mod_id))
    return True


def enable_mod(game_dir, mod_id):
    """Re-enable a disabled mod: rename manifest.disabled.json back to
    manifest.json so MelonLoader scans the folder again. Returns True if it was
    disabled and is now enabled; False if there was nothing disabled to enable."""
    src = _disabled_record_path(game_dir, mod_id)
    if not os.path.isfile(src):
        return False
    os.replace(src, _mod_record_path(game_dir, mod_id))
    return True


def mod_status(game_dir, mod_id, index):
    """missing / outdated / current / unknown, same shape as
    _melonloader_status, read from the mod's install record (Mods/<id>/manifest.json
    or its disabled variant; see _read_mod_record).

    "outdated" = the installed version is lower than the highest available across
    all repos (any major counts; a new major still shows as an available update,
    since the user is prompted before any update actually applies, so surfacing
    it is informative, not automatic). "current" = it equals that
    highest. "unknown" = installed but no longer present in any configured index
    (e.g. its repo was removed), nothing to compare against."""
    meta = _read_mod_record(game_dir, mod_id)
    if not meta or "version" not in meta:
        return "missing"
    available = [s for s in index if s.id == mod_id]
    if not available:
        return "unknown"
    highest = max(_parse_version(v) for s in available for v in s.versions)
    try:
        installed = _parse_version(meta["version"])
    except ModManagerError:
        return "unknown"
    return "current" if installed >= highest else "outdated"


def list_mods(index, side):
    """The merged index (list[ModSummary]) collapsed to one row per id (highest
    major), filtered to the caller's side: side="client" keeps client_side mods,
    side="server" keeps server_side ones; each launcher passes its own, so a
    server-only mod never shows in the client's browse list and vice versa. The only listing accessor the UI needs (libraries never appear in
    an index; they're inline library_dependencies on a parent manifest)."""
    best = {}    # id -> ModSummary with the highest version seen
    for s in index:
        if side == "client" and not s.client_side:
            continue
        if side == "server" and not s.server_side:
            continue
        cur = best.get(s.id)
        if cur is None or _parse_version(s.highest()) > _parse_version(cur.highest()):
            best[s.id] = s
    return sorted(best.values(), key=lambda s: s.name.lower())


def list_installed_mods(game_dir):
    """Every installed mod's record, in both enabled/disabled states: the per-mod
    folders (enabled Mods/<id>/manifest.json or disabled
    Mods/<id>/manifest.disabled.json). A record counts only if it carries an "id"
    and a usable "parity_required", so a library sidecar, a bare marker, or a
    record written before parity_required existed is ignored. Each returned record gains
    an in-memory "enabled" value, not written to disk: True (loaded) or False
    (disabled). Every installed mod's client_side/server_side flags are available
    from these records, so this is the read side for both the UI's status refresh
    and whatever assembles the server's mod list for the pre-join check.
    uninstall_mod also uses it to see which libraries the remaining mods still
    pin.

    A record whose parity_required is absent OR unreadable is skipped rather
    than assumed required, because assuming is what would let a mod the server
    actually requires be reported as something a client may decline. Skipping is
    recoverable: the mod reads as not installed, so the next plan reinstalls it
    and the new record carries a usable field."""
    out = {}     # id -> record
    mods_dir = _mods_base(game_dir)
    if not os.path.isdir(mods_dir):
        return []
    for name in os.listdir(mods_dir):
        full = os.path.join(mods_dir, name)
        if not os.path.isdir(full):
            continue
        # Skip our own staging/ignore folders (dot/tilde).
        if name.startswith((".", "~")):
            continue
        rec = _read_sidecar(os.path.join(full, RECORD_NAME))
        enabled = True
        if not (rec and rec.get("id")):
            rec = _read_sidecar(os.path.join(full, DISABLED_RECORD_NAME))
            enabled = False
        if rec and rec.get("id"):
            # Asked through parity_required rather than a bare key check, so a
            # record carrying an unusable value is skipped on the same terms as
            # one missing the field entirely - both mean nothing here can say
            # whether a joining client has to match this mod, and both are
            # fixed the same way. Also keeps handshake_snapshot below, which
            # re-reads the field on these same records, unable to raise.
            try:
                parity_required(rec)
            except ModManagerError as e:
                _warn(f"installed mod {rec['id']} has an unusable record ({e}), "
                      f"so it is being ignored; reinstall it to fix.")
                continue
            rec["enabled"] = enabled       # in-memory only; not on disk
            out[rec["id"]] = rec
    return list(out.values())


def list_untracked_mods(game_dir):
    """Everything sitting in Mods/ that MelonLoader can load but this manager
    doesn't own (docs/mod-manager-design.md "Untracked mods"): a loose root
    Mods/*.dll (the classic drag-a-dll-in manual install, invisible to
    list_installed_mods since it never looks at files, only subfolders), or a
    first-level Mods/<name>/ folder that doesn't hold a usable record of ours -
    a foreign manifest.json, or one of ours gone unusable (see the
    skip-with-warning above). Each entry is {"name", "kind" ("file"/"folder"),
    "enabled"}. Deliberately surfacing-and-toggling only, per the design doc:
    there's no manifest we trust to reconcile a version/update against, so
    this never appears anywhere near install/update, only enable/disable.
    TavernLib's ListUntrackedMods is the same listing for headless servers,
    and the two have to report the same set for the same folder.

    A folder counts on a record file EXISTING, not on it parsing: MelonLoader's
    folder marker is an existence check with the content unread, so a folder
    whose manifest.json is unparseable junk still loads and still has to be
    reported."""
    out = []
    mods_dir = _mods_base(game_dir)
    if not os.path.isdir(mods_dir):
        return out
    for name in os.listdir(mods_dir):
        full = os.path.join(mods_dir, name)
        if name.startswith((".", "~")):
            continue
        if os.path.isfile(full):
            lower = name.lower()
            if lower.endswith(".dll"):
                out.append({"name": name, "kind": "file", "enabled": True})
            elif lower.endswith(".dll.disabled"):
                out.append({"name": name[:-len(".disabled")], "kind": "file", "enabled": False})
        elif os.path.isdir(full):
            # "Claimed" is tested per folder with the exact predicate
            # list_installed_mods uses, rather than against the set of record
            # ids it returned: that lister keys its result by the id INSIDE the
            # record, so a folder whose name and record id disagree would miss
            # an id-set lookup and be reported as untracked as well as
            # installed. Same input, same question, one answer.
            if _owned_mod_record(game_dir, name) is not None:
                continue
            if os.path.isfile(os.path.join(full, RECORD_NAME)):
                out.append({"name": name, "kind": "folder", "enabled": True})
            elif os.path.isfile(os.path.join(full, DISABLED_RECORD_NAME)):
                out.append({"name": name, "kind": "folder", "enabled": False})
            # Neither present: an empty or junk folder MelonLoader wouldn't
            # load either, so there's nothing to report.
    return out


def disable_untracked_dll(game_dir, filename):
    """Takes a loose root Mods/<filename> out of rotation without deleting
    it, by renaming it out of MelonLoader's view - the file equivalent of
    disable_mod's manifest rename. A root dll has no manifest to hide behind
    and MelonLoader always loads any Mods/*.dll it finds, so the rename
    itself has to be what stops it loading. Returns True if it was there and
    enabled, False if not.

    Refuses rather than clobbering an existing .disabled: that file is someone
    else's mod, and a rename that quietly destroys it is the one outcome nothing
    here can undo. TavernLib's DisableUntrackedDll refuses the same way."""
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
    """A cheap "has this changed?" marker for one untracked mod: its mtime as
    whole Unix seconds, never a content hash. This runs on every ping, and
    hashing every loose DLL in Mods/ that often would cost far more than an
    advisory is worth. mtime misses an in-place edit that preserves it, which
    costs a client a stale advisory line until something else moves -
    acceptable, where missing a changed REQUIRED mod would not be.

    Deliberately mtime alone, not mtime+size: TavernLib's UntrackedStamp has to
    produce a byte-identical fingerprint, and a directory has no portable size
    to agree on. A directory's own mtime moves when entries are added or
    removed, so a folder mod gaining or losing files still re-fingerprints.

    Anything unreadable stamps "?" rather than raising: a mod vanishing
    mid-scan must not take the whole handshake down with it."""
    try:
        return str(int(os.stat(os.path.join(_mods_base(game_dir),
                                            entry["name"])).st_mtime))
    except OSError:
        return "?"


def handshake_snapshot(game_dir):
    """Fingerprint of every currently-ENABLED mod in Mods/, for the ping/pong
    mod-sync check. Disabled mods are excluded: they aren't loaded, so a
    joining client shouldn't be asked about them. Returns
    (mods_hash, mods_count, mods_list, untracked_list):
    - mods_hash: sha256 of the fingerprint lines, joined with newlines: the
      id-sorted managed "id@version@req|opt" lines ("req" when
      parity_required, "opt" otherwise), then the name-sorted
      "untracked:name@stamp" lines (_untracked_stamp). A client caches this
      per server; an unchanged hash means it already has the full list and
      skips the extra round trip.

      This exact format is a cross-language invariant: TavernLib computes the
      same hash in C# (Backend/Mods/ModHandshake.cs, whose own comment says it
      must stay byte-identical to this) and a client compares the two. Changing
      the separator, the ordering, or which fields take part silently breaks
      every join against a server running the other side's build -- the hash
      just never matches, there is no version negotiation, and nothing reports
      a mismatch as such. Sorting is by id, ordinal on both sides; untracked
      entries sort by lowercased name on both sides.
    - mods_count: len(mods_list) - MANAGED mods only, deliberately. It's sent
      in the pong so a client can sanity-check its cached mods list without
      decoding anything, and that list is the managed one; folding untracked
      mods into the number would make it disagree with what it's checking.
    - mods_list: every managed entry as {"id", "version", "client_side",
      "server_side", "parity_required", "source_repo"}, sorted by id: what the
      separate, length-prefixed "mods_list" request sends on a cache miss.
      source_repo is a HINT only: plan_join uses it to tell a client which repo
      a required mod not resolvable from any of the client's configured repos
      would come from, so the user can add it explicitly; it's never resolved
      into a pull on its own, same rule as everywhere else this field appears
      (the modlist's `repos`, the rejection payload's `missing` entries).
    - untracked_list: every enabled untracked mod as {"name", "kind"}, sorted
      by name. A SEPARATE list, never merged into mods_list, because plan_join
      consumes that one: an entry with no id or version would either crash it or
      have to be special-cased in the middle of resolution. Kept apart, it can
      only reach code that asked for it.

    The advisory is honest about being an advisory. All a server can say about
    an untracked mod is its name and shape - no id, version, or source - so a
    client can display it but can never resolve, install, or verify it, and is
    never blocked over one. It exists so a silent divergence becomes a visible
    one.

    Untracked entries ARE part of mods_hash, though. A client caches the whole
    list against this hash, so leaving them out would let an operator drop a new
    DLL into Mods/ and have every already-connected client keep reporting the
    old set until something else happened to move the hash."""
    mods = sorted((m for m in list_installed_mods(game_dir) if m.get("enabled")),
                  key=lambda m: m["id"])
    untracked = sorted((u for u in list_untracked_mods(game_dir) if u["enabled"]),
                       key=lambda u: u["name"].lower())
    # parity_required is part of the fingerprint, not just the list. A server
    # can flip a mod between required and recommended without its version
    # moving, and a client caches this whole list against this hash - so leaving
    # it out would let a client keep planning against the old answer until it
    # restarted, and skip a mod that had since become mandatory.
    lines = [f"{m['id']}@{m.get('version', '')}@{'req' if parity_required(m) else 'opt'}"
             for m in mods]
    lines += [f"untracked:{u['name']}@{_untracked_stamp(game_dir, u)}"
              for u in untracked]
    mods_hash = hashlib.sha256("\n".join(lines).encode()).hexdigest()
    mods_list = [
        {"id": m["id"], "version": m.get("version", ""),
         "client_side": bool(m.get("client_side", False)),
         "server_side": bool(m.get("server_side", False)),
         "parity_required": parity_required(m),
         "source_repo": m.get("source_repo", "")}
        for m in mods
    ]
    untracked_list = [{"name": u["name"], "kind": u["kind"]} for u in untracked]
    return mods_hash, len(mods_list), mods_list, untracked_list
