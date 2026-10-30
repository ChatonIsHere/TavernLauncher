"""Join planning: a server's mod list into an active set for the client, the
comparison rows the diff window shows, and rendering the plan onto disk."""
import os
from dataclasses import dataclass, field

from tavern_shared.mods.base import ModManagerError, _parse_version, _warn
from tavern_shared.mods.cache import (
    _cache_mod_dir, _ensure_library, cache_restore_mod, cache_store_library,
    cache_store_mod,
)
from tavern_shared.mods.install import (
    _check_folder_libraries, _read_mod_record, _record_libraries,
    collect_library_dependencies, disable_mod, enable_mod, has_target,
    install_mod, list_installed_mods, verify_mod_files,
)
from tavern_shared.mods.layout import (
    _disabled_record_path, _safe_basename, _userlibs_dir,
)
from tavern_shared.mods.manifest import parity_required, runs_in_scene
from tavern_shared.mods.pins import _parse_pin_entry
from tavern_shared.mods.repos import (
    _norm, _repo_shorthand, replacement_for, replacement_of_unlisted,
)
from tavern_shared.mods.resolve import (
    _fetch_manifest, _index_source, resolve_dependencies,
)


CLIENT_TARGET = "client_windows"


def _offers_client(server_entry):
    targets = server_entry.get("targets")
    return isinstance(targets, list) and CLIENT_TARGET in targets


def _client_records(game_dir):
    return {r["id"]: r for r in list_installed_mods(game_dir) if has_target(r, CLIENT_TARGET)}


def _unadded(hint, repo_bases):
    """Whether a payload's source_repo hint names a repo the client lacks."""
    return bool(hint) and _norm(hint) not in [_norm(b) for b in repo_bases]


@dataclass
class PlanEntry:
    mod_id: str
    version: str
    source_repo: str
    reason: str        # "required" | "recommended" | "dependency" | "pinned"
    cached: bool
    active: bool       # enabled at this version and intact: nothing to do
    manifest: object
    needed_by: str = ""     # recommended, but a mod that can't be skipped needs it


@dataclass
class JoinPlan:
    entries: list               # list[PlanEntry]
    to_deactivate: list         # enabled mod ids outside the active set
    libraries: list             # list[LibraryDependency]
    missing: list               # [(id, version)] required, in no configured repo
    needs_repo: list            # [(id, version, repo)] required, only in an unadded repo
    pin_conflicts: list         # [(id, pinned version, server version)]
    declined: list = field(default_factory=list)          # [(id, version)]
    unresolved_pins: list = field(default_factory=list)   # [(id, version or "latest")]

    @property
    def blocking(self):
        # A required mod that won't be installed could only fail the
        # post-render parity check. Pins never block: no server asked for them.
        return bool(self.missing or self.needs_repo)


def plan_join(game_dir, server_mods, index, repo_bases, pinned=None, declined=None,
              scene=None):
    """required + recommended (not declined) + their closure + pins, diffed
    against disk and cache. Only server entries with a client_windows build count, and
    everything is fetched from the client's own repos. A failure is routed by
    who asked: only a required mod can block. A pin that excludes `scene` is
    left out; mods the server runs never are."""
    pinned = pinned or []
    declined = set(declined or [])

    client_mods = [m for m in server_mods if _offers_client(m)]
    required = {m["id"]: m["version"] for m in client_mods if parity_required(m)}
    recommended = {m["id"]: m["version"] for m in client_mods if not parity_required(m)}
    hinted_repo = {m["id"]: m.get("source_repo") for m in server_mods if m.get("source_repo")}
    skipped = [(mid, ver) for mid, ver in recommended.items() if mid in declined]

    roots, missing, needs_repo, unresolved_pins = [], [], [], []
    reason = {}         # mod_id -> tag, first wins
    exact_pin = {}      # mod_id -> pinned version, None for latest

    def resolve_root(mod_id, version, tag):
        try:
            manifest = _fetch_manifest(repo_bases, mod_id, version,
                                       _index_source(index, mod_id, repo_bases, version))
            if manifest.artifact_for(CLIENT_TARGET) is None:
                raise ModManagerError(f"{mod_id} {version} has no Windows build.")
        except ModManagerError:
            if tag == "pinned":
                unresolved_pins.append((mod_id, version))
                _warn(f"pinned mod '{mod_id}' {version} isn't available from any "
                      f"configured source; it won't be installed, and it doesn't "
                      f"affect joining.")
            elif tag == "recommended":
                _warn(f"'{mod_id}' {version} is recommended by this server but "
                      f"isn't available from any configured source; skipped.")
            elif _unadded(hinted_repo.get(mod_id), repo_bases):
                needs_repo.append((mod_id, version, hinted_repo[mod_id]))
            else:
                missing.append((mod_id, version))
            return
        if tag == "pinned" and not runs_in_scene(manifest.scenes, scene):
            _warn(f"pinned mod '{mod_id}' doesn't work in the {scene} scene this "
                  f"server runs, so it's left out of this join.")
            return
        roots.append(manifest)
        reason[mod_id] = tag

    for mod_id, version in required.items():
        resolve_root(mod_id, version, "required")
    for mod_id, version in recommended.items():
        if mod_id not in declined:
            resolve_root(mod_id, version, "recommended")
    # The mods a server-run id has been replaced by: a pin on one of those
    # would load alongside the old mod the server asks for.
    replacing_server = {rep.by: mod_id for mod_id in reason
                        if (rep := replacement_for(index, mod_id)) is not None}
    for entry in pinned:
        mod_id, version = _parse_pin_entry(entry)
        if (rep := replacement_of_unlisted(index, mod_id)) is not None:
            _warn(f"pinned mod '{mod_id}' has been replaced by '{rep.by}'; pinning that "
                  f"instead.")
            mod_id, version = rep.by, None
        if mod_id in replacing_server:
            _warn(f"pinned mod '{mod_id}' replaces '{replacing_server[mod_id]}', which "
                  f"this server runs, so it's left out of this join.")
            continue
        exact_pin[mod_id] = version
        if mod_id in reason:
            continue
        if version is None:
            matches = [s for s in index if s.id == mod_id]
            if not matches:
                unresolved_pins.append((mod_id, "latest"))
                _warn(f"pinned mod '{mod_id}' isn't in any configured source's "
                      f"index; it won't be installed, and it doesn't affect joining.")
                continue
            version = max((v for s in matches for v in s.versions), key=_parse_version)
        resolve_root(mod_id, version, "pinned")

    # Only a mod that resolved as a server root can overrule a pin.
    server_versions = {**recommended, **required}
    pin_conflicts = [
        (mod_id, pinned_version, server_versions[mod_id])
        for mod_id, pinned_version in exact_pin.items()
        if pinned_version and reason.get(mod_id) in ("required", "recommended")
        and pinned_version != server_versions[mod_id]
    ]

    dependencies = resolve_dependencies(roots, index, repo_bases, CLIENT_TARGET)
    for dep in dependencies:
        reason.setdefault(dep.id, "dependency")

    # A recommended root that a required or pinned mod depends on, however
    # deeply, isn't the user's to skip: that mod would load without it.
    # resolve_dependencies leaves roots out of its result, so walk it here.
    all_mods = roots + dependencies
    by_id = {m.id: m for m in all_mods}
    needed_by = {}
    stack = [m for m in roots if reason[m.id] != "recommended"]
    while stack:
        mod = stack.pop()
        for dep_id in sorted(mod.dependencies or {}):
            if dep_id in by_id and dep_id not in needed_by:
                needed_by[dep_id] = mod.name or mod.id
                stack.append(by_id[dep_id])
    needed_by = {mid: by for mid, by in needed_by.items() if reason.get(mid) == "recommended"}
    for mod_id in needed_by:
        reason[mod_id] = "dependency"

    installed = _client_records(game_dir)
    entries = []
    for mod in all_mods:
        cached = os.path.isdir(_cache_mod_dir(mod.id, mod.version,
                                              mod.artifact_for(CLIENT_TARGET).sha256))
        rec = installed.get(mod.id)
        # A damaged copy isn't a match: the join repairs it.
        active = bool(rec and rec.get("enabled") and rec.get("version") == mod.version
                      and verify_mod_files(game_dir, mod.id, CLIENT_TARGET) is not False)
        entries.append(PlanEntry(mod_id=mod.id, version=mod.version, source_repo=mod.source_repo,
                                 reason=reason.get(mod.id, "required"), cached=cached, active=active,
                                 manifest=mod, needed_by=needed_by.get(mod.id, "")))

    active_ids = {e.mod_id for e in entries}
    # Declining is "don't add it", never "take it away".
    to_deactivate = [mod_id for mod_id, rec in installed.items()
                     if rec.get("enabled") and mod_id not in active_ids
                     and mod_id not in declined]

    libraries = collect_library_dependencies(all_mods, CLIENT_TARGET)
    # Against what stays enabled: the render prunes what it disables.
    _check_folder_libraries(game_dir, all_mods, CLIENT_TARGET, libraries,
                            enabled_only=True, ignore=set(to_deactivate))

    return JoinPlan(entries=entries, to_deactivate=to_deactivate, libraries=libraries,
                    missing=missing, needs_repo=needs_repo, pin_conflicts=pin_conflicts,
                    declined=skipped, unresolved_pins=unresolved_pins)


ACTION_INSTALL     = "install"      # needs downloading
ACTION_ACTIVATE    = "activate"     # cached at this version: a file move
ACTION_UPDATE      = "update"
ACTION_DOWNGRADE   = "downgrade"
ACTION_ENABLE      = "enable"       # right version on disk, disabled
ACTION_DEACTIVATE  = "deactivate"   # enabled here, not in this server's set
ACTION_MATCH       = "match"
ACTION_SKIPPED     = "skipped"      # recommended, declined for this server
ACTION_SERVER_ONLY = "server-only"  # no client_windows build
ACTION_MISSING     = "missing"      # in no configured repo
ACTION_NEEDS_REPO  = "needs-repo"   # only in a repo not added here

# Problems first, then changes, then the quiet rows.
_ACTION_ORDER = {
    ACTION_MISSING: 0, ACTION_NEEDS_REPO: 0,
    ACTION_INSTALL: 1, ACTION_ACTIVATE: 1, ACTION_UPDATE: 1,
    ACTION_DOWNGRADE: 1, ACTION_ENABLE: 1,
    ACTION_DEACTIVATE: 2, ACTION_MATCH: 3,
    ACTION_SKIPPED: 4, ACTION_SERVER_ONLY: 5,
}


@dataclass
class DiffRow:
    mod_id: str
    server_version: str     # "" when the server doesn't ask for it
    client_version: str     # "" when not installed
    client_enabled: bool
    action: str
    reason: str             # as PlanEntry.reason, or ""
    optional: bool          # the user may overrule it
    source_repo: str
    note: str = ""
    cached: bool = False
    name: str = ""
    requires: tuple = ()    # its manifest's dependency ids

    @property
    def recommended(self):
        return self.reason == "recommended" or self.action == ACTION_SKIPPED

    @property
    def downloads(self):
        return (not self.cached) and self.action in (
            ACTION_INSTALL, ACTION_UPDATE, ACTION_DOWNGRADE)

    @property
    def blocking(self):
        return ((self.action == ACTION_MISSING and self.reason == "required")
                or self.action == ACTION_NEEDS_REPO)


def build_mod_diff(game_dir, server_mods, plan):
    """One DiffRow per mod on either side of the plan, problems first."""
    installed = _client_records(game_dir)
    required = {m["id"]: m.get("version", "") for m in server_mods if _offers_client(m)}
    conflicts = {mid: (pv, sv) for mid, pv, sv in plan.pin_conflicts}

    rows = []
    for e in plan.entries:
        rec = installed.get(e.mod_id)
        cv = rec.get("version", "") if rec else ""
        enabled = bool(rec.get("enabled")) if rec else False
        damaged = (not e.active and cv == e.version
                   and verify_mod_files(game_dir, e.mod_id, CLIENT_TARGET) is False)
        if e.active:
            action = ACTION_MATCH
        elif rec is None or damaged:
            action = ACTION_ACTIVATE if e.cached else ACTION_INSTALL
        elif cv == e.version:
            action = ACTION_ENABLE
        else:
            try:
                older = _parse_version(cv) < _parse_version(e.version)
            except ModManagerError:
                older = True
            action = ACTION_UPDATE if older else ACTION_DOWNGRADE
        pv_sv = conflicts.get(e.mod_id)
        recommended = e.reason == "recommended"
        if pv_sv and recommended:
            note = (f"your pin says {pv_sv[0]}, this server runs {pv_sv[1]}; it "
                    f"doesn't require it, so skipping keeps your pinned version")
        elif pv_sv:
            note = f"your pin says {pv_sv[0]}, the server requires {pv_sv[1]}"
        elif recommended:
            note = ("this server recommends it but doesn't require it; skipping "
                    "it won't stop you joining")
        elif e.needed_by:
            note = (f"this server only recommends it, but {e.needed_by} needs it, "
                    f"so it can't be skipped")
        elif damaged:
            note = "the installed copy is damaged; it will be reinstalled"
        else:
            note = ""
        rows.append(DiffRow(
            mod_id=e.mod_id, server_version=required.get(e.mod_id, ""),
            client_version=cv, client_enabled=enabled, action=action,
            reason=e.reason, optional=recommended and not e.active,
            source_repo=e.source_repo, note=note, cached=e.cached,
            name=e.manifest.name, requires=tuple(sorted(e.manifest.dependencies or {}))))

    seen = {r.mod_id for r in rows}

    def add(mod_id, **row):
        # The first list to mention a mod owns its row.
        if mod_id in seen:
            return
        seen.add(mod_id)
        rec = installed.get(mod_id, {})
        row.setdefault("client_version", rec.get("version", ""))
        row.setdefault("client_enabled", bool(rec.get("enabled")))
        row.setdefault("source_repo", rec.get("source_repo", ""))
        row.setdefault("name", rec.get("name", mod_id))
        rows.append(DiffRow(mod_id=mod_id, **row))

    for mod_id in plan.to_deactivate:
        add(mod_id, server_version="", client_enabled=True, action=ACTION_DEACTIVATE,
            reason="", optional=True,
            note=("this server doesn't run it; keeping it on won't block your "
                  "join. Pin keeps it enabled on every server, so it stops "
                  "being offered for deactivation at all (same as Mod "
                  "Manager's Keep Enabled)."))
    for mod_id, version in plan.declined:
        add(mod_id, server_version=version, action=ACTION_SKIPPED,
            reason="recommended", optional=True,
            note="you've chosen not to install this one for this server")
    for m in server_mods:
        if not _offers_client(m):
            add(m["id"], server_version=m.get("version", ""), client_version="",
                client_enabled=False, action=ACTION_SERVER_ONLY, reason="",
                optional=False, source_repo=m.get("source_repo", ""),
                note="it has no Windows build, so the server runs it without you",
                name=m.get("name", m["id"]))
    for mod_id, version in plan.missing:
        add(mod_id, server_version=version, action=ACTION_MISSING, reason="required",
            optional=False, source_repo="", note="not in any source you've added")
    for mod_id, version, repo in plan.needs_repo:
        add(mod_id, server_version=version, client_version="", client_enabled=False,
            action=ACTION_NEEDS_REPO, reason="required", optional=False,
            source_repo=repo,
            note=f"add {_repo_shorthand(repo)} as a source, then check again")
    # No server_version: the server never asked for it. Reason "pinned" keeps
    # the row from blocking.
    for mod_id, version in plan.unresolved_pins:
        add(mod_id, server_version="", action=ACTION_MISSING, reason="pinned",
            optional=False, source_repo="",
            note=f"you pinned this ({version}), but no source you've added has "
                 f"it. This server doesn't run it either, so it won't stop you "
                 f"joining - remove the pin in Mod Manager to clear this.")

    return sorted(rows, key=lambda r: (_ACTION_ORDER.get(r.action, 9), r.name.lower()))


def _prune_orphaned_libraries(game_dir):
    """Removes (after caching) every library no enabled record pins, any
    target's. Enabled-only is safe here because enabling again runs a library
    pass; uninstall_mod has none."""
    still_needed = set()
    for rec in list_installed_mods(game_dir):
        if rec.get("enabled"):
            still_needed |= _record_libraries(rec)

    libs_dir = _userlibs_dir(game_dir)
    for name in os.listdir(libs_dir):
        if not name.endswith(".meta.json"):
            continue
        filename = name[:-len(".meta.json")]
        if filename in still_needed:
            continue
        cache_store_library(game_dir, filename)
        for path in (os.path.join(libs_dir, filename), os.path.join(libs_dir, name)):
            try:
                os.remove(path)
            except OSError:
                pass


def render_plan_steps(plan):
    """How many items render_active_set reports through on_step."""
    return (len([e for e in plan.entries if not e.active])
            + len(plan.libraries) + len(plan.to_deactivate))


def render_active_set(game_dir, plan, on_progress, on_step):
    """Applies a JoinPlan: enable in place, else cache restore, else download;
    then libraries, deactivations (cached, then disabled) and the orphaned
    library prune. on_step(item, "start"/"done") brackets each item."""
    if plan.blocking:
        raise ModManagerError(
            "This plan has required mods that can't be installed - not in any "
            "repo you've added, or only in one you haven't added yet; fix that "
            "before applying it.")
    def download(entry):
        install_mod(game_dir, entry.manifest, CLIENT_TARGET, on_progress)
        cache_store_mod(game_dir, entry.mod_id, CLIENT_TARGET)

    for entry in plan.entries:
        if entry.active:
            continue
        on_step(entry.mod_id, "start")
        rec = _read_mod_record(game_dir, entry.mod_id)
        disabled_here = (bool(rec) and rec.get("version") == entry.version
                         and has_target(rec, CLIENT_TARGET)
                         and os.path.isfile(_disabled_record_path(game_dir, entry.mod_id))
                         and verify_mod_files(game_dir, entry.mod_id, CLIENT_TARGET) is not False)
        if disabled_here:
            on_progress(f"Enabling {entry.mod_id} {entry.version}")
            enable_mod(game_dir, entry.mod_id)
        elif entry.cached:
            on_progress(f"Installing {entry.mod_id} {entry.version} (cached)")
            try:
                cache_restore_mod(game_dir, entry.mod_id, entry.version,
                                  entry.manifest.artifact_for(CLIENT_TARGET).sha256,
                                  CLIENT_TARGET)
            except ModManagerError as e:
                on_progress(f"Cache copy unusable ({e}) - downloading instead")
                download(entry)
        else:
            on_progress(f"Downloading {entry.mod_id} {entry.version}")
            download(entry)
        on_step(entry.mod_id, "done")

    for lib in plan.libraries:
        filename = _safe_basename(lib.filename)
        on_step(filename, "start")
        _ensure_library(game_dir, lib, on_progress, verb="Installing")
        on_step(filename, "done")

    for mod_id in plan.to_deactivate:
        on_step(mod_id, "start")
        on_progress(f"Deactivating {mod_id}")
        cache_store_mod(game_dir, mod_id, CLIENT_TARGET)
        disable_mod(game_dir, mod_id)
        on_step(mod_id, "done")

    _prune_orphaned_libraries(game_dir)


def resolve_missing_mods(missing, index, repo_bases):
    """A rejection's `missing` entries resolved through the client's own repos
    only: (roots, dependencies), or raises naming every failure."""
    roots, unresolved = [], []
    for entry in missing:
        mod_id, version = entry["id"], entry["version"]
        try:
            root = _fetch_manifest(repo_bases, mod_id, version,
                                   _index_source(index, mod_id, repo_bases, version))
        except ModManagerError:
            root = None
        if root is not None and root.artifact_for(CLIENT_TARGET) is None:
            unresolved.append(f"{mod_id} {version} (it has no Windows build)")
        elif root is not None:
            roots.append(root)
        elif _unadded(entry.get("source_repo"), repo_bases):
            unresolved.append(f"{mod_id} {version} (available from {entry['source_repo']}, "
                              f"which you haven't added as a source)")
        else:
            unresolved.append(f"{mod_id} {version} (not found in any of "
                              f"your configured sources)")

    if unresolved:
        raise ModManagerError("Could not resolve: " + "; ".join(unresolved))
    return roots, resolve_dependencies(roots, index, repo_bases, CLIENT_TARGET)
