"""Turning a server's mod list into an active set: what to install, update,
enable, or deactivate for this join, and rendering that plan onto disk."""
import os
from dataclasses import dataclass, field

from tavern_shared.mods.cache import (
    _cache_mod_dir, _ensure_library, cache_restore_mod, cache_store_library,
    cache_store_mod,
)
from tavern_shared.mods.errors import ModManagerError, _warn
from tavern_shared.mods.install import (
    _read_mod_record, collect_library_dependencies, disable_mod, enable_mod,
    install_mod, list_installed_mods, verify_mod_files,
)
from tavern_shared.mods.layout import (
    _disabled_record_path, _safe_basename, _userlibs_dir,
)
from tavern_shared.mods.manifest import parity_required, runs_in_scene
from tavern_shared.mods.pins import _parse_pin_entry
from tavern_shared.mods.repos import _norm, _repo_shorthand
from tavern_shared.mods.resolve import (
    _fetch_manifest, _index_source, resolve_dependencies,
)
from tavern_shared.mods.version import _parse_version


@dataclass
class PlanEntry:
    """One mod in the computed active set."""
    mod_id: str
    version: str
    source_repo: str
    reason: str        # "required" | "recommended" | "dependency" | "pinned"
    cached: bool       # this exact version is in the local cache
    active: bool       # already enabled, at this version and intact: a no-op
    manifest: object   # the resolved ModManifest render_active_set installs from


@dataclass
class JoinPlan:
    entries: list           # list[PlanEntry] - the full computed active set
    to_deactivate: list      # mod ids enabled in Mods/ that aren't in the active set
    libraries: list         # list[LibraryDependency] the active set needs
    missing: list           # [(mod_id, version)] required, in no configured repo
    needs_repo: list        # [(mod_id, version, source_repo)] required, only in a repo
                             # the client hasn't added (the row names it)
    pin_conflicts: list      # [(mod_id, pinned_version, server_version)]
    declined: list = field(default_factory=list)
                             # [(mod_id, version)] recommended mods the user declined for
                             # this server; untouched on disk, shown so it can be undone
    unresolved_pins: list = field(default_factory=list)
                             # [(mod_id, version_or_"latest")] the user's own pins that
                             # resolve from nowhere. Kept apart from `missing`: no server
                             # asked for them, so they never block a join

    @property
    def blocking(self):
        """True when a mod the server requires won't be installed: missing (no
        configured repo has it) or needs_repo (only an unadded repo does). Either
        way an apply could only fail the post-render parity check.
        unresolved_pins never blocks: the server didn't ask for them."""
        return bool(self.missing or self.needs_repo)


def plan_join(game_dir, server_mods, index, repo_bases, pinned=None, declined=None,
              scene=None):
    """Computes the active set for a join: required(server) + recommended
    (server, not declined) + their dependency closure + pinned, diffed against
    disk and cache. The plan, not the render.

    server_mods: the handshake's mods list; only client_side entries matter.
    parity_required true goes in and blocks if unresolvable; false goes in by
    default but can be declined, and is never deactivated for that.
    index / repo_bases: the client's merged index and its own repos, the only
    sources anything is fetched from.
    pinned: "id" / "id@version" always-on pins. declined: ids declined for this
    server (ignored for required mods). scene: the server's scene when known; a
    pin whose manifest excludes it is left out. Mods the server runs are never
    scene-filtered."""
    pinned = pinned or []
    declined = set(declined or [])

    client_mods = [m for m in server_mods if m.get("client_side")]
    required = {m["id"]: m["version"] for m in client_mods
                if parity_required(m)}
    recommended = {m["id"]: m["version"] for m in client_mods
                   if not parity_required(m)}
    hinted_repo = {m["id"]: m.get("source_repo") for m in server_mods if m.get("source_repo")}

    # Only an optional mod can be declined.
    skipped = [(mid, ver) for mid, ver in recommended.items() if mid in declined]

    roots = []
    missing = []
    needs_repo = []
    unresolved_pins = []
    reason = {}         # mod_id -> "required" | "recommended" | "pinned", first tag wins
    exact_pin = {}      # mod_id -> pinned version (None if pinned to latest)

    def resolve_root(mod_id, version, tag):
        """Resolves one root, routing a failure by who asked: only a mod the
        server requires can make the join impossible."""
        try:
            manifest = _fetch_manifest(repo_bases, mod_id, version,
                                       _index_source(index, mod_id, repo_bases, version))
        except ModManagerError:
            if tag == "pinned":
                unresolved_pins.append((mod_id, version))
                _warn(f"pinned mod '{mod_id}' {version} isn't available from any "
                      f"configured source; it won't be installed, and it doesn't "
                      f"affect joining.")
            elif tag == "recommended":
                # Before the repo hint: needs_repo means "required", which a
                # recommendation never is.
                _warn(f"'{mod_id}' {version} is recommended by this server but "
                      f"isn't available from any configured source; skipped.")
            else:
                hint = hinted_repo.get(mod_id)
                if hint and _norm(hint) not in [_norm(b) for b in repo_bases]:
                    needs_repo.append((mod_id, version, hint))
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

    for entry in pinned:
        mod_id, version = _parse_pin_entry(entry)
        exact_pin[mod_id] = version
        if mod_id in reason:
            continue     # already resolved as a required root
        if version is None:
            matches = [s for s in index if s.id == mod_id]
            if not matches:
                unresolved_pins.append((mod_id, "latest"))
                _warn(f"pinned mod '{mod_id}' isn't in any configured source's "
                      f"index; it won't be installed, and it doesn't affect joining.")
                continue
            version = max((v for s in matches for v in s.versions), key=_parse_version)
        resolve_root(mod_id, version, "pinned")

    # A conflict is "about to install a version other than the one pinned", so
    # it keys off what resolved as a required or recommended root: a declined
    # or unresolvable mod overrules nothing.
    server_versions = {**recommended, **required}
    pin_conflicts = [
        (mod_id, pinned_version, server_versions[mod_id])
        for mod_id, pinned_version in exact_pin.items()
        if pinned_version and reason.get(mod_id) in ("required", "recommended")
        and pinned_version != server_versions[mod_id]
    ]

    def fetch_manifest(mod_id, version, source_repo):
        return _fetch_manifest(repo_bases, mod_id, version, prefer_repo=source_repo)

    # Always "client": joins only run in the client launcher.
    dependencies = resolve_dependencies(roots, index, fetch_manifest, "client") if roots else []
    for dep in dependencies:
        reason.setdefault(dep.id, "dependency")

    all_mods = roots + dependencies
    installed = {rec["id"]: rec for rec in list_installed_mods(game_dir)}

    entries = []
    for mod in all_mods:
        cached = os.path.isdir(_cache_mod_dir(mod.id, mod.version))
        rec = installed.get(mod.id)
        # A damaged copy at the right version isn't a match: the join has to
        # repair it or the game launches broken.
        active = bool(rec and rec.get("enabled") and rec.get("version") == mod.version
                      and verify_mod_files(game_dir, mod.id) is not False)
        entries.append(PlanEntry(mod_id=mod.id, version=mod.version, source_repo=mod.source_repo,
                                  reason=reason.get(mod.id, "required"), cached=cached, active=active,
                                  manifest=mod))

    active_ids = {e.mod_id for e in entries}
    # Declining is "don't add it", never "take it away".
    to_deactivate = [mod_id for mod_id, rec in installed.items()
                      if rec.get("enabled") and mod_id not in active_ids
                      and mod_id not in declined]

    libraries = collect_library_dependencies(all_mods) if all_mods else []

    return JoinPlan(entries=entries, to_deactivate=to_deactivate, libraries=libraries,
                    missing=missing, needs_repo=needs_repo, pin_conflicts=pin_conflicts,
                    declined=skipped, unresolved_pins=unresolved_pins)


# Client-vs-server comparison: what each side has and what a plan would change,
# derived from the plan and disk only (no display, no network).

# What will happen to each mod. The first six change the client; the rest are
# informational or blocking.
ACTION_INSTALL     = "install"      # not here at all, needs downloading
ACTION_ACTIVATE    = "activate"     # in the local cache at this version, a file move
ACTION_UPDATE      = "update"       # installed at a lower version
ACTION_DOWNGRADE   = "downgrade"    # installed higher; the server pins an exact version
ACTION_ENABLE      = "enable"       # installed at the right version, currently disabled
ACTION_DEACTIVATE  = "deactivate"   # active here, not part of this server's set
ACTION_MATCH       = "match"        # already correct, nothing to do
ACTION_SKIPPED     = "skipped"      # recommended, and the user has said no to it here
ACTION_SERVER_ONLY = "server-only"  # the server runs it, clients never need it
ACTION_MISSING     = "missing"      # required, and no configured repo has it
ACTION_NEEDS_REPO  = "needs-repo"   # resolvable, but only from a repo not added here

# Sort weight: problems first, then real changes, then the quiet rows.
_ACTION_ORDER = {
    ACTION_MISSING: 0, ACTION_NEEDS_REPO: 0,
    ACTION_INSTALL: 1, ACTION_ACTIVATE: 1, ACTION_UPDATE: 1,
    ACTION_DOWNGRADE: 1, ACTION_ENABLE: 1,
    ACTION_DEACTIVATE: 2, ACTION_MATCH: 3,
    ACTION_SKIPPED: 4, ACTION_SERVER_ONLY: 5,
}


@dataclass
class DiffRow:
    """One mod, as it stands on each side and what would change."""
    mod_id: str
    server_version: str     # "" when the server doesn't run this mod
    client_version: str     # "" when it isn't installed here
    client_enabled: bool    # only meaningful when client_version is set
    action: str             # one of the ACTION_* values above
    reason: str             # "required" | "recommended" | "dependency" | "pinned" | ""
    optional: bool          # True when the user may overrule this row
    source_repo: str
    note: str = ""          # extra detail, e.g. a pin disagreeing with the server
    cached: bool = False     # this exact version is already in the local cache, so
                             # applying it is a file move rather than a download
    name: str = ""           # display name; falls back to mod_id wherever no
                             # manifest or install record was around to supply one

    @property
    def recommended(self):
        """A mod this server runs but doesn't require: the user's call either way."""
        return self.reason == "recommended" or self.action == ACTION_SKIPPED

    @property
    def downloads(self):
        """True when applying this row fetches bytes; what the summary counts."""
        return (not self.cached) and self.action in (
            ACTION_INSTALL, ACTION_UPDATE, ACTION_DOWNGRADE)

    @property
    def blocking(self):
        """Only a mod the SERVER requires can stop a join: a missing row with reason
        "required", or a needs-repo row. A missing pin (reason "pinned") is shown
        but never blocks."""
        return ((self.action == ACTION_MISSING and self.reason == "required")
                or self.action == ACTION_NEEDS_REPO)


def build_mod_diff(game_dir, server_mods, plan):
    """One DiffRow per mod on either side of a JoinPlan, problems first then by
    display name. Only deactivations and recommendations are optional: the
    server's parity check can't fail over either."""
    installed = {r["id"]: r for r in list_installed_mods(game_dir)}
    required = {m["id"]: m.get("version", "") for m in server_mods if m.get("client_side")}
    conflicts = {mid: (pv, sv) for mid, pv, sv in plan.pin_conflicts}

    rows = []
    for e in plan.entries:
        rec = installed.get(e.mod_id)
        cv = rec.get("version", "") if rec else ""
        enabled = bool(rec.get("enabled")) if rec else False
        damaged = (not e.active and cv == e.version
                   and verify_mod_files(game_dir, e.mod_id) is False)
        if e.active:
            action = ACTION_MATCH
        elif rec is None or damaged:
            action = ACTION_ACTIVATE if e.cached else ACTION_INSTALL
        elif cv == e.version:
            action = ACTION_ENABLE          # right version on disk, just switched off
        else:
            try:
                older = _parse_version(cv) < _parse_version(e.version)
            except ModManagerError:
                older = True                # unparseable installed version: treat as stale
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
        elif damaged:
            note = "the installed copy is damaged; it will be reinstalled"
        else:
            note = ""
        rows.append(DiffRow(
            mod_id=e.mod_id, server_version=required.get(e.mod_id, ""),
            client_version=cv, client_enabled=enabled, action=action,
            reason=e.reason, optional=recommended and not e.active,
            source_repo=e.source_repo, note=note, cached=e.cached,
            name=e.manifest.name))

    seen = {r.mod_id for r in rows}

    def add(mod_id, **row):
        """One row per mod: the first list to mention a mod owns its row.
        Client-side columns come from the install record unless given."""
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
        if not m.get("client_side"):
            add(m["id"], server_version=m.get("version", ""), client_version="",
                client_enabled=False, action=ACTION_SERVER_ONLY, reason="",
                optional=False, source_repo=m.get("source_repo", ""),
                note="runs on the server only", name=m.get("name", m["id"]))
    for mod_id, version in plan.missing:
        add(mod_id, server_version=version, action=ACTION_MISSING, reason="required",
            optional=False, source_repo="", note="not in any source you've added")
    for mod_id, version, repo in plan.needs_repo:
        add(mod_id, server_version=version, client_version="", client_enabled=False,
            action=ACTION_NEEDS_REPO, reason="required", optional=False,
            source_repo=repo,
            note=f"add {_repo_shorthand(repo)} as a source, then check again")
    # server_version stays empty: this server never asked for it, which is
    # what tells these rows apart from plan.missing. reason "pinned" keeps
    # DiffRow.blocking false, so an unfixable pin is visible without
    # disabling Apply.
    for mod_id, version in plan.unresolved_pins:
        add(mod_id, server_version="", action=ACTION_MISSING, reason="pinned",
            optional=False, source_repo="",
            note=f"you pinned this ({version}), but no source you've added has "
                 f"it. This server doesn't run it either, so it won't stop you "
                 f"joining - remove the pin in Mod Manager to clear this.")

    return sorted(rows, key=lambda r: (_ACTION_ORDER.get(r.action, 9), r.name.lower()))


def _prune_orphaned_libraries(game_dir):
    """Removes (after caching) every UserLibs/ library no ENABLED mod's record
    pins. Enabled-only is safe here because enabling again runs a library
    pass; uninstall_mod has no such pass and keeps disabled mods' libraries."""
    still_needed = set()
    for rec in list_installed_mods(game_dir):
        if rec.get("enabled"):
            still_needed.update(rec.get("libraries", []))

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
    """The number of on_step items render_active_set will report for this plan,
    so a caller can size a progress bar that ends full."""
    return (len([e for e in plan.entries if not e.active])
            + len(plan.libraries) + len(plan.to_deactivate))


def render_active_set(game_dir, plan, on_progress=None, on_step=None):
    """Applies a JoinPlan: enable-in-place where the exact version sits on disk
    disabled and intact, cache restore where cached (falling back to a
    download if the entry fails its check), verified download otherwise, then
    the library pass, deactivations (cached, then disabled), and the orphaned
    library prune. Raises up front if plan.blocking.

    on_progress gets lines of detail; on_step(item, "start"/"done") brackets
    each item render_plan_steps counts, including libraries already in
    place."""
    if plan.blocking:
        raise ModManagerError(
            "This plan has required mods that can't be installed - not in any "
            "repo you've added, or only in one you haven't added yet; fix that "
            "before applying it.")
    progress = on_progress or (lambda *_a: None)
    step = on_step or (lambda *_a: None)

    for entry in plan.entries:
        if entry.active:
            continue
        step(entry.mod_id, "start")
        rec = _read_mod_record(game_dir, entry.mod_id)
        disabled_here = (bool(rec) and rec.get("version") == entry.version
                         and os.path.isfile(_disabled_record_path(game_dir, entry.mod_id))
                         and verify_mod_files(game_dir, entry.mod_id) is not False)
        if disabled_here:
            progress(f"Enabling {entry.mod_id} {entry.version}")
            enable_mod(game_dir, entry.mod_id)
        elif entry.cached:
            progress(f"Installing {entry.mod_id} {entry.version} (cached)")
            try:
                cache_restore_mod(game_dir, entry.mod_id, entry.version)
            except ModManagerError as e:
                # Vanished or failed its check (and was discarded) since
                # planning: download it instead.
                progress(f"Cache copy unusable ({e}) - downloading instead")
                install_mod(game_dir, entry.manifest, progress)
                cache_store_mod(game_dir, entry.mod_id)
        else:
            progress(f"Downloading {entry.mod_id} {entry.version}")
            install_mod(game_dir, entry.manifest, progress)
            cache_store_mod(game_dir, entry.mod_id)
        step(entry.mod_id, "done")

    for lib in plan.libraries:
        filename = _safe_basename(lib.filename)
        step(filename, "start")
        _ensure_library(game_dir, lib, progress, verb="Installing")
        step(filename, "done")

    for mod_id in plan.to_deactivate:
        step(mod_id, "start")
        progress(f"Deactivating {mod_id}")
        # Disabled, not uninstalled: it comes back with a rename.
        cache_store_mod(game_dir, mod_id)
        disable_mod(game_dir, mod_id)
        step(mod_id, "done")

    _prune_orphaned_libraries(game_dir)


def resolve_missing_mods(missing, index, repo_bases):
    """Resolves a rejection payload's `missing` entries ({"id", "version",
    "source_repo"}) through the client's own repos only; source_repo is a hint
    for the error message, never a source. Returns (roots, dependencies), or
    raises naming every entry that can't be resolved."""
    roots = []
    unresolved = []
    for entry in missing:
        mod_id, version = entry["id"], entry["version"]
        try:
            roots.append(_fetch_manifest(repo_bases, mod_id, version,
                                         _index_source(index, mod_id, repo_bases, version)))
        except ModManagerError:
            hint = entry.get("source_repo")
            if hint and _norm(hint) not in [_norm(b) for b in repo_bases]:
                unresolved.append(f"{mod_id} {version} (available from {hint}, "
                                  f"which you haven't added as a source)")
            else:
                unresolved.append(f"{mod_id} {version} (not found in any of "
                                  f"your configured sources)")

    if unresolved:
        raise ModManagerError("Could not resolve: " + "; ".join(unresolved))

    def fetch_manifest(mod_id, version, source_repo):
        return _fetch_manifest(repo_bases, mod_id, version, prefer_repo=source_repo)

    dependencies = resolve_dependencies(roots, index, fetch_manifest, "client") if roots else []
    return roots, dependencies
